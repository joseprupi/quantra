"""Live-engine integration test for the equity-option pricing endpoint.

Gated on the ``orchestrator_engine_equity_options`` marker so it stays
skipped in default CI runs (only opt-in CI runs that ship a live
``quantra-engine`` container should hit this test).

Required environment (export to opt in):

* ``QUANTRA_ORCHESTRATOR_TEST_ENGINE_GRPC_TARGET`` — gRPC
  ``host:port`` of a reachable pricing engine (e.g.
  ``localhost:50051``). Shared with the cross-product gated test
  ``test_engine_integration.py`` because a single live engine
  serves every RPC the orchestrator forwards.

Per: the test
expects a real engine round-trip — HTTP 200, a typed
:class:`EquityOptionResult` with a finite NPV and the per-product
diagnostics ``equity_option_response.fbs`` guarantees (``delta`` /
``gamma`` / ``vega`` / ``theta`` / ``rho`` / ``implied_volatility``
/ ``used_spot`` / ``used_strike`` / ``used_settlement``), and the
caller's ``X-Request-Id`` echoed back via the response header.

Mirrors the shape established by the bonds / swap_ir / swaption /
cds live-engine tests — one gated marker, one explicit env var,
one round-trip route call. Uses the inline-everything path so the
test never touches ``app.equity_options`` /``app.curves`` /
``app.vol_surfaces``: the orchestrator's
*faithful* translator builds the engine payload from the caller's
supplied discount + dividend curves, BlackVol surface and underlier
spot (not a canonical fixture), and the engine's equity-option pricer
consumes them end-to-end.

This module carries two gated tests:

* a finite-round-trip test — HTTP 200 + a finite NPV + every
  diagnostic present;
* an input-sensitivity test — two requests that differ only in
  the supplied vol level + spot must move the NPV accordingly, proving
  the engine prices the *caller's* inputs rather than a fixture.

All-zero-diagnostics guard: every diagnostic must be finite; the engine's wire
naming is propagated via :attr:`EquityOptionResult.extras` so a future
engine change is detectable without a decoder change.
"""

from __future__ import annotations

import math
import os
import uuid
from collections.abc import Iterator
from datetime import datetime
from http import HTTPStatus
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from quantra_common.auth.context import ApiKeyRecord
from quantra_common.auth.lookup import ApiKeyLookup
from quantra_common.md_client import MdClient, MdClientConfig
from quantra_common.settings import Environment, LogLevel
from quantra_common.types.market_data import ResolvedQuote
from quantra_orchestrator.app import create_app
from quantra_orchestrator.settings import OrchestratorSettings

_TARGET_ENV = "QUANTRA_ORCHESTRATOR_TEST_ENGINE_GRPC_TARGET"
_target = os.environ.get(_TARGET_ENV)
pytestmark = [
    pytest.mark.orchestrator_engine_equity_options,
    pytest.mark.skipif(
        not _target,
        reason=(
            f"Set {_TARGET_ENV} (e.g. localhost:50051) to run the "
            "orchestrator equity-option live-engine integration test."
        ),
    ),
]


class _StaticMdClient(MdClient):
    """Returns one resolved-quote per requested canonical ID."""

    def __init__(self) -> None:
        super().__init__(
            MdClientConfig(base_url="http://stub", timeout_s=1.0, max_retries=0),
            client=httpx.AsyncClient(base_url="http://stub"),
        )

    async def resolve_quotes(
        self,
        canonical_ids: list[str],
        as_of: Any,
        *,
        snapshot_version: str | None = None,
    ) -> list[ResolvedQuote]:
        del as_of, snapshot_version
        return [
            ResolvedQuote(
                canonical_id=cid,
                requested_as_of=datetime(2025, 1, 15, 0, 0),
                resolved_as_of=datetime(2025, 1, 15, 0, 0),
                found=True,
                is_exact=True,
                value=0.03,
                source="live-engine-test",
                vendor_id="FAKE",
            )
            for cid in canonical_ids
        ]


def _api_key_headers() -> dict[str, str]:
    return {"X-API-Key": "live-eq-key"}


def _seeded_api_keys() -> dict[str, ApiKeyRecord]:
    return {
        "live-eq-key": ApiKeyRecord(
            api_key_id="live-eq-key",
            owner_uid="eq-live-uid",
            name="Live EQ Test Key",
            email="eq-live@example.com",
            tier="free",
            active=True,
        )
    }


@pytest.fixture
def live_engine_app(
    fake_api_key_lookup: ApiKeyLookup,
    fake_firebase_verifier: tuple[Any, dict[str, dict[str, Any]]],
    fake_api_keys: dict[str, ApiKeyRecord],
) -> Iterator[TestClient]:
    """Orchestrator app wired against the live-engine target.

    Uses the stub MD client + the lifespan-owned gRPC engine
    pointed at the live target. No ``app_ro`` engine is needed —
    the inline everything-path skips ``app.*`` entirely.
    """

    fake_api_keys.update(_seeded_api_keys())
    verifier, _ = fake_firebase_verifier
    target = _target
    assert target is not None
    settings = OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="testsha",
        engine_grpc_target=target,
    )
    app: FastAPI = create_app(
        settings,
        api_key_lookup=fake_api_key_lookup,
        firebase_verifier=verifier,
        md_client=_StaticMdClient(),
    )
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _deposit_point(*, quote_id: str | None = None, rate: float | None = None) -> dict[str, Any]:
    """A 1Y DepositHelper point the faithful translator can bootstrap.

    Carries the ``point_type`` discriminator + structural / convention fields the
    rates-curve translator requires; the value is either a ``quote_id`` (resolved
    by the stub MD client) or an inline ``rate``.
    """

    point: dict[str, Any] = {
        "point_type": "DepositHelper",
        "tenor": {"n": 1, "unit": "Years"},
        "fixing_days": 2,
        "calendar": "TARGET",
        "business_day_convention": "ModifiedFollowing",
        "day_counter": "Actual365Fixed",
    }
    if quote_id is not None:
        point["quote_id"] = quote_id
    if rate is not None:
        point["rate"] = rate
    return point


def _equity_payload(*, constant_vol: float, spot_value: float) -> dict[str, Any]:
    """A fully inline equity-option request the faithful translator consumes.

    A 100-strike 1Y European call on a discount + dividend curve, a flat
    BlackVol surface at ``constant_vol`` and an inline ``spot_value``. The
    discount curve point references ``USD.IRS.1Y`` (the stub MD client resolves
    every id to 0.03); the dividend curve is a flat 1 %.
    """

    return {
        "equity_option": {
            "option": {
                "trade_id": "demo-AAPL",
                "option_type": "Call",
                "strike": 100.0,
                "quantity": 1.0,
                "expiry_date": "2026-01-15",
                "settlement": "Physical",
            }
        },
        "curves": [
            {
                "name": "USD-OIS",
                "currency": "USD",
                "day_counter": "Actual/365",
                "reference_date": "2025-01-15",
                "role": "discount",
                "points": [_deposit_point(quote_id="USD.IRS.1Y")],
            },
            {
                "name": "AAPL-DIV",
                "currency": "USD",
                "day_counter": "Actual/365",
                "reference_date": "2025-01-15",
                "role": "dividend",
                "points": [_deposit_point(rate=0.01)],
            },
        ],
        "vol_surface": {
            "name": "AAPL-vol",
            "kind": "BlackVolSpec",
            "payload": {"base": {"reference_date": "2025-01-15", "constant_vol": constant_vol}},
        },
        "spot": {"value": spot_value},
        "as_of": "2025-01-15",
    }


def test_price_equity_option_round_trip_against_live_engine(
    live_engine_app: TestClient,
) -> None:
    """End-to-end round-trip against a live engine target.

    The gRPC backend dispatches to the live engine
    over ``/quantra.QuantraServer/PriceEquityOption`` (canonical
    RPC name pinned in :attr:`EngineRpc.PRICE_EQUITY_OPTION`),
    which returns a real ``PriceEquityOptionResponse``
    flatbuffer carrying one :class:`EquityOptionResponse` per
    trade. The route validates the response into a typed
    :class:`EquityOptionResult` and echoes the assembled request
    (curves + vol surface + spot — three distinct market-data
    families) back in the response body.

    The *faithful* translator posts the caller's supplied
    discount + dividend curves, the flat 20 % BlackVol surface and the
    100.0 spot — not a canonical fixture. Under these inputs a
    100-strike 1Y call is positive; we pin **finiteness + presence** of
    every diagnostic, not numeric parity (layer 4 in the test pyramid).

    Also pins the ``X-Request-Id`` echo: the caller-provided
    request id surfaces on the response headers.
    """

    payload = _equity_payload(constant_vol=0.20, spot_value=100.0)
    request_id = f"eq-live-{uuid.uuid4()}"
    response = live_engine_app.post(
        "/v1/price/equity-option",
        json=payload,
        headers={**_api_key_headers(), "X-Request-Id": request_id},
    )

    assert response.status_code == HTTPStatus.OK, response.text
    assert response.headers.get("X-Request-Id") == request_id
    body = response.json()
    assert body["pricing_history_id"] is None

    result = body["result"]
    assert isinstance(result["npv"], float)
    assert math.isfinite(result["npv"])
    # All-zero watch: every diagnostic must be finite. If the live engine
    # surfaces all zeros we still pass (the contract is "engine
    # consumed the canonical fixture and emitted a finite shape")
    # but the operator can grep the response for an all-zero pattern.
    for key in (
        "delta",
        "gamma",
        "vega",
        "theta",
        "rho",
        "implied_volatility",
        "used_spot",
        "used_strike",
    ):
        assert isinstance(result[key], float)
        assert math.isfinite(float(result[key]))
    assert result["used_settlement"] in {"Physical", "Cash"}

    assembled = body["assembled_request"]
    assert assembled["as_of"] == "2025-01-15"
    assert assembled["discount_curve"]["name"] == "USD-OIS"
    assert assembled["dividend_curve"]["name"] == "AAPL-DIV"
    assert assembled["vol_surface"]["name"] == "AAPL-vol"
    assert assembled["spot"]["value"] == pytest.approx(100.0)
    # Inline-only surface → ``equity_surface_id`` is None (the surface is
    # translated faithfully from the inline payload, keying-field contract).
    assert assembled["equity_surface_id"] is None
    assert len(assembled["resolved_quotes"]) == 1
    assert assembled["resolved_quotes"][0]["canonical_id"] == "USD.IRS.1Y"
    assert assembled["resolved_quotes"][0]["value"] == pytest.approx(0.03)


def test_price_equity_option_is_input_sensitive_against_live_engine(
    live_engine_app: TestClient,
) -> None:
    """The engine prices the caller's inputs, not a fixture.

    Two requests differ only in the supplied BlackVol level and the underlier
    spot: a low-vol / at-the-money request vs a high-vol / deeper in-the-money
    request. For a call, both a higher vol and a higher spot strictly raise the
    price, so the second NPV must exceed the first. Previously the canonical encoder
    discarded these inputs and both requests priced the *same* flat-20 % / 100.0
    fixture — an identical NPV; now the faithful translator forwards the
    caller's vol + spot, so the response moves accordingly. This is the equity
    analogue of the input-sensitivity strengthening (a finite NPV alone no
    longer proves the engine consumed the request).
    """

    low = live_engine_app.post(
        "/v1/price/equity-option",
        json=_equity_payload(constant_vol=0.15, spot_value=100.0),
        headers=_api_key_headers(),
    )
    high = live_engine_app.post(
        "/v1/price/equity-option",
        json=_equity_payload(constant_vol=0.45, spot_value=130.0),
        headers=_api_key_headers(),
    )

    assert low.status_code == HTTPStatus.OK, low.text
    assert high.status_code == HTTPStatus.OK, high.text
    npv_low = low.json()["result"]["npv"]
    npv_high = high.json()["result"]["npv"]
    assert math.isfinite(npv_low)
    assert math.isfinite(npv_high)
    # Higher vol + higher spot ⇒ a strictly more valuable call. If these were
    # equal the engine would be pricing a fixed canonical fixture, not the
    # caller's inputs (the failure mode the faithful translator closes).
    assert npv_high > npv_low
    # The faithful surface / spot reach the assembled echo too.
    assert high.json()["assembled_request"]["spot"]["value"] == pytest.approx(130.0)
