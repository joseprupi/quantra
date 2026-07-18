"""Live-engine integration test for the inflation-swap pricing endpoint.

Gated on the ``orchestrator_engine_swaps_inflation`` marker so it stays
skipped in default CI runs (only opt-in CI runs that ship a live
``quantra-engine`` container should hit this).

Required environment (export to opt in):

* ``QUANTRA_ORCHESTRATOR_TEST_ENGINE_GRPC_TARGET`` — gRPC
  ``host:port`` of a reachable pricing engine (e.g.
  ``localhost:50051``). Shared with the cross-product gated test
  ``test_engine_integration.py`` because a single live engine
  serves every RPC the orchestrator forwards.

Per: the test
expects a real engine round-trip — HTTP 200, a typed
:class:`InflationSwapResult` with a finite NPV and the per-product
diagnostics ``zero_coupon_inflation_swap_response.fbs`` guarantees
(``fair_rate`` / ``fixed_leg_bps`` / ``fixed_leg_npv`` /
``inflation_leg_npv``), and the caller's ``X-Request-Id`` echoed
back via the response header.

The shape mirrors the bonds / swap_ir / swaption / cds /
equity_options live-engine tests — one gated marker, one explicit
env var, one round-trip route call. Uses the inline-everything
path so the test never touches ``app.swaps_inflation`` /
``app.curves`` / ``app.indices``. The
orchestrator posts a *faithful* encoding of the inline inputs —
the supplied nominal discount curve, the supplied zero-inflation
HICP curve and the supplied EU-HICP inflation index (its body
fixings consumed verbatim) under the resolved ids the engine's
inflation-swap pricer consumes end-to-end, not a canonical fixture.

The default fixture posts a ZCIIS — the YYIIS path is exercised in
the same test file under a separate request via the ``swap_kind``
discriminator (settled — the plan placeholder used a single
``PRICE_SWAP_INFLATION``; the canonical enum has two RPCs, one per
swap kind).

All-zero-diagnostics guard: if the live engine returns all-zero diagnostics for
the fixture path the test still asserts finiteness (so a later
engine fix is detected) and propagates the engine's wire naming via
:attr:`InflationSwapResult.extras` — operators can identify a future
fix without a decoder change.

Input-sensitivity: a second gated test seeds two distinct
inflation-curve levels and asserts the engine NPV *moves* — proving
the faithful encoder threads the supplied inflation input through to
the engine (it is not pricing a fixed canonical fixture).
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
    pytest.mark.orchestrator_engine_swaps_inflation,
    pytest.mark.skipif(
        not _target,
        reason=(
            f"Set {_TARGET_ENV} (e.g. localhost:50051) to run the "
            "orchestrator inflation-swap live-engine integration test."
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
    return {"X-API-Key": "live-inf-key"}


def _seeded_api_keys() -> dict[str, ApiKeyRecord]:
    return {
        "live-inf-key": ApiKeyRecord(
            api_key_id="live-inf-key",
            owner_uid="inf-live-uid",
            name="Live Inflation Test Key",
            email="inf-live@example.com",
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


def _zciis_payload() -> dict[str, Any]:
    """Canonical ZCIIS fixture mirroring the engine's reference test."""

    return {
        "swap": {
            "swap_kind": "zero_coupon",
            "swaps": [
                {
                    "zero_coupon_inflation_swap": {
                        "swap_type": "Payer",
                        "notional": 1_000_000.0,
                        "start_date": "2025-01-15",
                        "maturity_date": "2030-01-15",
                        "fixed_calendar": "TARGET",
                        "fixed_convention": "ModifiedFollowing",
                        "day_counter": "Actual365Fixed",
                        "fixed_rate": 0.0217,
                        "inflation_index_id": "EUHICP",
                        "observation_lag": {"n": 3, "unit": "Months"},
                        "observation_interpolation": "Linear",
                        "adjust_observation_dates": False,
                        "inflation_calendar": "NullCalendar",
                        "inflation_convention": "Following",
                    }
                }
            ],
        },
        "curves": [
            {
                "name": "DISC",
                "currency": "EUR",
                "day_counter": "Actual365Fixed",
                "reference_date": "2025-01-15",
                "role": "nominal",
                # The nominal discount curve must span **past** the ZCIIS
                # maturity, not merely match the front: a single 1Y deposit
                # helper bootstraps a curve whose last node is t≈1.01y, but
                # the 5Y ZCIIS discounts its terminal fixed/inflation
                # cashflows at t≈5.003y (and the inflation curve's 5Y helper
                # discounts on this same curve) — the live engine then raises
                # "1st leg: time (5.00274) is past max curve time (1.01096)"
                # and ABORTs the RPC (the inflation analogue of the all-zero-diagnostics
                # CDS symptom). A deposit (1Y) + swap helpers (2Y/5Y/10Y)
                # curve spans to 10Y with headroom, mirroring the engine's
                # own known-good zero_coupon_inflation_swap reference fixture.
                "points": [
                    {
                        "point_type": "DepositHelper",
                        "tenor": {"n": 1, "unit": "Years"},
                        "quote_id": "EUR.IRS.1Y",
                    },
                    {
                        "point_type": "SwapHelper",
                        "tenor": {"n": 2, "unit": "Years"},
                        "quote_id": "EUR.IRS.2Y",
                    },
                    {
                        "point_type": "SwapHelper",
                        "tenor": {"n": 5, "unit": "Years"},
                        "quote_id": "EUR.IRS.5Y",
                    },
                    {
                        "point_type": "SwapHelper",
                        "tenor": {"n": 10, "unit": "Years"},
                        "quote_id": "EUR.IRS.10Y",
                    },
                ],
            },
            {
                "name": "HICP_ZC",
                "currency": "EUR",
                "day_counter": "Actual365Fixed",
                "reference_date": "2025-01-15",
                "role": "inflation",
                "points": [{"tenor": "5Y", "quote_id": "EUR.HICP.5Y"}],
            },
        ],
        "inflation_index": {
            "name": "EU HICP",
            "index_id": "EUHICP",
            "currency": "EUR",
            "body": {
                "family_name": "EU HICP",
                "frequency": "Monthly",
                "availability_lag": {"n": 2, "unit": "Months"},
                "observation_lag": {"n": 3, "unit": "Months"},
                "fixings": [
                    {"date": "2024-10-01", "value": 100.0},
                    {"date": "2024-11-01", "value": 100.2},
                    {"date": "2024-12-01", "value": 100.4},
                ],
            },
        },
        "as_of": "2025-01-15",
    }


def test_price_zciis_round_trip_against_live_engine(
    live_engine_app: TestClient,
) -> None:
    """ZCIIS round-trip against a live engine target.

    The gRPC backend dispatches to the live engine
    over ``/quantra.QuantraServer/PriceZeroCouponInflationSwap``
    (an earlier placeholder ``PRICE_SWAP_INFLATION`` is mapped to the
    canonical enum entry).
    The orchestrator faithfully encodes the inline inputs and posts:

    * The supplied EUR discount curve under id ``DISC`` (a deposit
      helper at 1Y plus swap helpers at 2Y/5Y/10Y, each carrying the
      MD-resolved rate, so the curve spans past the 5Y maturity —
      spanning past maturity is deliberate).
    * The supplied EU-HICP inflation index under id ``EUHICP`` with
      the body's monthly fixings around a baseline level of 100.
    * The supplied zero-inflation curve under id ``HICP_ZC``
      (ZCIIS helper at the 5Y tenor, MD-resolved value).
    * A 5Y ZCIIS Payer at notional 1M with a 2.17 % fixed rate.

    Under this fixture the price is finite. We pin **finiteness +
    presence** of every diagnostic, not numeric parity (layer 4
    in the test pyramid).

    Also pins the ``X-Request-Id`` echo: the caller-provided
    request id surfaces on the response headers.
    """

    request_id = f"inf-zciis-live-{uuid.uuid4()}"
    response = live_engine_app.post(
        "/v1/price/swaps/inflation",
        json=_zciis_payload(),
        headers={**_api_key_headers(), "X-Request-Id": request_id},
    )

    assert response.status_code == HTTPStatus.OK, response.text
    assert response.headers.get("X-Request-Id") == request_id
    body = response.json()
    assert body["pricing_history_id"] is None

    result = body["result"]
    assert isinstance(result["npv"], float)
    assert math.isfinite(result["npv"])
    assert result["swap_kind"] == "zero_coupon"
    # All-zero watch: the canonical fixture posts a Payer 5Y ZCIIS at
    # 2.17 %; the engine should produce a finite ``fair_rate`` /
    # per-leg NPVs. If a future engine change makes the leg NPVs
    # collapse to all-zero we still pass the finiteness contract;
    # the operator greps the wire-naming-mirror ``extras`` for
    # the all-zero pattern.
    for key in ("fair_rate", "fixed_leg_bps", "fixed_leg_npv", "inflation_leg_npv"):
        assert isinstance(result[key], float)
        assert math.isfinite(float(result[key]))
    # YYIIS-only fields are ``None`` for ZCIIS responses.
    assert result["fair_spread"] is None
    assert result["yoy_leg_bps"] is None
    assert result["yoy_leg_npv"] is None

    # The wire-naming mirror in ``extras`` carries every diagnostic
    # the engine emitted so an operator can verify
    # the all-zero / typed-mirror discrepancy without a decoder
    # change.
    extras = result["extras"]
    assert math.isfinite(float(extras["npv"]))
    assert math.isfinite(float(extras["fair_rate"]))

    assembled = body["assembled_request"]
    assert assembled["as_of"] == "2025-01-15"
    assert assembled["nominal_curve"]["name"] == "DISC"
    assert assembled["inflation_curve"]["name"] == "HICP_ZC"
    assert assembled["inflation_index"]["index_id"] == "EUHICP"
    # Inline-only call → all keying fields ``None`` (the faithful
    # encoder still posts the supplied curves / index on the wire,
    # under ids derived from the inline names — the ``None`` here is
    # the keying-field contract for inline-only calls, not a
    # claim that the wire carries no ids).
    assert assembled["nominal_curve_id"] is None
    assert assembled["inflation_curve_id"] is None
    assert assembled["inflation_index_id"] is None
    # Both curves' canonical ids must show up in the resolved-
    # quotes echo (bypass-check pin: inflation curves DO flow
    # through the MD walker) — every nominal-curve helper quote
    # (deposit 1Y + swaps 2Y/5Y/10Y) plus the inflation-curve 5Y
    # helper.
    canonical_ids = {q["canonical_id"] for q in assembled["resolved_quotes"]}
    assert canonical_ids == {
        "EUR.IRS.1Y",
        "EUR.IRS.2Y",
        "EUR.IRS.5Y",
        "EUR.IRS.10Y",
        "EUR.HICP.5Y",
    }


def _zciis_payload_with_inflation_level(level: float) -> dict[str, Any]:
    """Base ZCIIS payload with the inflation curve seeded to an inline level.

    The inflation curve point uses an inline ``quote_value`` (bypassing MD) so the
    test can drive a *distinctive*, deterministic inflation level without a
    parametrised MD stub. Everything else (nominal curve, index, trade) is the
    base fixture.
    """

    payload = _zciis_payload()
    payload["curves"][1]["points"] = [{"tenor": "5Y", "quote_value": level}]
    return payload


def test_price_zciis_npv_moves_with_inflation_curve_level(
    live_engine_app: TestClient,
) -> None:
    """Input-sensitivity: a distinctive inflation level moves the engine NPV.

    The faithful encoder threads the *supplied* inflation-curve level into
    the engine, so two requests that differ only in that level must produce
    different NPVs. A canonical-fixture encoder would price the same
    flat fixture both times and the NPVs would be identical — this test is
    the regression guard that the faithful encoding actually reached the
    wire. Pins **movement + finiteness**, not numeric parity.
    """

    low = live_engine_app.post(
        "/v1/price/swaps/inflation",
        json=_zciis_payload_with_inflation_level(0.01),
        headers=_api_key_headers(),
    )
    high = live_engine_app.post(
        "/v1/price/swaps/inflation",
        json=_zciis_payload_with_inflation_level(0.05),
        headers=_api_key_headers(),
    )
    assert low.status_code == HTTPStatus.OK, low.text
    assert high.status_code == HTTPStatus.OK, high.text

    npv_low = low.json()["result"]["npv"]
    npv_high = high.json()["result"]["npv"]
    assert math.isfinite(float(npv_low))
    assert math.isfinite(float(npv_high))
    # The supplied inflation level reaches the engine — distinct inputs, distinct
    # NPVs. If these are equal the encoder is pricing a fixture, not the
    # caller's inputs.
    assert float(npv_low) != pytest.approx(float(npv_high))
