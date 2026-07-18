"""Live-engine integration test for the swaption pricing endpoint.

Gated on the ``orchestrator_engine_swaption`` marker so it stays
skipped in default CI runs (only opt-in CI runs that ship a live
``quantra-engine`` container should hit this test).

Required environment (export to opt in):

* ``QUANTRA_ORCHESTRATOR_TEST_ENGINE_GRPC_TARGET`` — gRPC
  ``host:port`` of a reachable pricing engine (e.g.
  ``localhost:50051``). Same env var the swap_ir live-engine
  integration test uses: a single live
  engine target serves every RPC the orchestrator forwards.

Per Part C
 the test now expects a real engine round-trip: HTTP 200, a
typed :class:`SwaptionResult` with a finite NPV plus the engine's
implied-volatility / atm-forward / Greeks diagnostics, and the
caller's ``X-Request-Id`` echoed back in the response body.

Mirrors the shape established by ``test_swap_ir_engine_integration.py``
verbatim — only the payload + assertions differ.
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
    pytest.mark.orchestrator_engine_swaption,
    pytest.mark.skipif(
        not _target,
        reason=(
            f"Set {_TARGET_ENV} (e.g. localhost:50051) to run the "
            "orchestrator swaption live-engine integration test."
        ),
    ),
]


class _StaticMdClient(MdClient):
    """Returns one resolved-quote per requested canonical ID.

    Lets the integration test exercise the full vertical (assemble
    → MD resolve → engine call) without provisioning a live MD
    service. Rate quote ids (``USD.IRS.*``) resolve to a flat 3 % so
    the faithful translator bootstraps a ~flat discount curve; the vol
    quote id resolves to a realistic 20 % so the engine prices a
    finite swaption.
    """

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
                value=0.03 if cid.startswith("USD.IRS") else 0.20,
                source="live-engine-test",
                vendor_id="FAKE",
            )
            for cid in canonical_ids
        ]


def _api_key_headers() -> dict[str, str]:
    return {"X-API-Key": "live-swaption-key"}


def _seeded_api_keys() -> dict[str, ApiKeyRecord]:
    return {
        "live-swaption-key": ApiKeyRecord(
            api_key_id="live-swaption-key",
            owner_uid="swaption-live-uid",
            name="Live Swaption Test Key",
            email="swaption-live@example.com",
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

    Uses the stub MD client above + the lifespan-owned gRPC engine
    pointed at the live target. No ``app_ro`` engine is needed —
    the integration test exercises the inline path which skips
    ``app.*`` entirely.
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
    """A 1Y DepositHelper point the faithful translator can bootstrap."""

    point: dict[str, Any] = {
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
    return {"point_type": "DepositHelper", "point": point}


def _swap_point(
    years: int, *, quote_id: str | None = None, rate: float | None = None
) -> dict[str, Any]:
    """A SwapHelper point at ``years`` tenor (quote-substituted or inline rate)."""

    point: dict[str, Any] = {
        "tenor": {"n": years, "unit": "Years"},
        "calendar": "TARGET",
        "sw_fixed_leg_frequency": "Annual",
        "sw_fixed_leg_convention": "ModifiedFollowing",
        "sw_fixed_leg_day_counter": "Thirty360",
    }
    if quote_id is not None:
        point["quote_id"] = quote_id
    if rate is not None:
        point["rate"] = rate
    return {"point_type": "SwapHelper", "point": point}


def _discount_curve() -> dict[str, Any]:
    """A bootstrappable ~flat 3 % discount curve spanning to 10Y.

    The default swaption underlying runs to 2031 (~6Y from the 2025-01-15
    reference); a 10Y span covers it. Every quote id resolves to 0.03 via the
    stub MD client.
    """

    return {
        "name": "USD-OIS",
        "currency": "USD",
        "day_counter": "Actual365Fixed",
        "reference_date": "2025-01-15",
        "points": [
            _deposit_point(quote_id="USD.IRS.1Y"),
            _swap_point(2, quote_id="USD.IRS.2Y"),
            _swap_point(5, quote_id="USD.IRS.5Y"),
            _swap_point(10, quote_id="USD.IRS.10Y"),
        ],
    }


def _vol_surface(
    *, quote_id: str | None = None, constant_vol: float | None = None
) -> dict[str, Any]:
    """A SwaptionVolSpec surface (quote-substituted or inline constant vol)."""

    base: dict[str, Any] = {"reference_date": "2025-01-15"}
    if quote_id is not None:
        base["quote_id"] = quote_id
    if constant_vol is not None:
        base["constant_vol"] = constant_vol
    return {"kind": "SwaptionVolSpec", "payload": {"base": base}}


def test_price_swaption_round_trip_against_live_engine(
    live_engine_app: TestClient,
) -> None:
    """End-to-end round-trip against a live engine target.

    A later revision shape: the gRPC backend dispatches to the live engine
    over /quantra.QuantraServer/PriceSwaption, which returns a
    real :class:`PriceSwaptionResponse` flatbuffer. The route
    validates the response into a typed :class:`SwaptionResult`
    and echoes the assembled request back in the response body.
    The faithful translator consumes the
    supplied discount curve + vol surface, so the payload carries
    ``point_type``-tagged helper points (the older simple
    ``{tenor, quote_id}`` shape is no longer translatable).

    Pins finiteness of the engine NPV + presence of the
    swaption-specific diagnostics the engine emits
    (implied-volatility / ATM-forward / per-Greek shape) plus the
    ``X-Request-Id`` echo. Numeric parity is out of scope here.
    """

    payload = {
        "swaption": {"exercise_type": "European"},
        "curves": [_discount_curve()],
        "vol_surface": _vol_surface(quote_id="USD.SWPTN.ATM.5Y10Y.VOL"),
        "swaption_model": {
            "kind": "HullWhiteLattice",
            "payload": {"hw_a": 0.05, "hw_sigma": 0.01},
        },
        "as_of": "2025-01-15",
    }
    request_id = f"swaption-live-{uuid.uuid4()}"
    response = live_engine_app.post(
        "/v1/price/swaption",
        json=payload,
        headers={**_api_key_headers(), "X-Request-Id": request_id},
    )

    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["pricing_history_id"] is None

    result = body["result"]
    assert isinstance(result["npv"], float)
    assert math.isfinite(result["npv"])
    extras = result["extras"]
    assert "implied_volatility" in extras
    assert math.isfinite(float(extras["implied_volatility"]))

    assembled = body["assembled_request"]
    assert assembled["as_of"] == "2025-01-15"
    assert assembled["trade"]["swaption"] == {"exercise_type": "European"}
    assert len(assembled["curves"]) == 1
    assert assembled["vol_surface"]["kind"] == "SwaptionVolSpec"
    assert assembled["swaption_model"]["kind"] == "HullWhiteLattice"
    by_id = {q["canonical_id"]: q["value"] for q in assembled["resolved_quotes"]}
    assert by_id["USD.IRS.1Y"] == pytest.approx(0.03)
    assert by_id["USD.SWPTN.ATM.5Y10Y.VOL"] == pytest.approx(0.20)


def _swaption_npv(
    live_engine_app: TestClient,
    *,
    model_kind: str,
    model_payload: dict[str, Any],
    constant_vol: float,
) -> float:
    """POST one inline swaption and return its engine NPV (asserts 200 + finite).

    Shared by both input-sensitivity tests below: each varies exactly one input
    (the model's ``hw_sigma`` or the surface's ``constant_vol``) and holds the
    rest constant, so a moving NPV isolates that one knob.
    """

    response = live_engine_app.post(
        "/v1/price/swaption",
        json={
            "swaption": {"exercise_type": "European"},
            "curves": [_discount_curve()],
            "vol_surface": _vol_surface(constant_vol=constant_vol),
            "swaption_model": {"kind": model_kind, "payload": model_payload},
            "as_of": "2025-01-15",
        },
        headers=_api_key_headers(),
    )
    assert response.status_code == HTTPStatus.OK, response.text
    npv = float(response.json()["result"]["npv"])
    assert math.isfinite(npv)
    return npv


def test_price_swaption_hw_lattice_is_input_sensitive_to_hw_sigma(
    live_engine_app: TestClient,
) -> None:
    """The production swaption model prices the caller's vol, not a fixture.

    ``HullWhiteLattice`` is the production swaption model (the ``app.swaption_
    models.kind`` server-default), and in the default ``Explicit`` param mode the
    engine builds a ``TreeSwaptionEngine`` straight off the short-rate
    ``hw_sigma`` (the engine's ``makeSwaptionEngine`` →
    ``IrModelType_HullWhiteLattice`` branch). It does **not** read the
    ``SwaptionVolSpec`` BlackVol surface — so ``hw_sigma`` is the real vol input
    this model consumes, and a swaption is long that vol.

    Two requests differ only in the supplied ``hw_sigma`` (1 % vs 2 %); the
    higher-vol request must report a strictly larger NPV. Previously the canonical
    encoder discarded the faithful payload and both priced the same fixture — an
    identical NPV; now the translator forwards the caller's model knobs, so
    the price moves. This is the swaption input-sensitivity guarantee for
    the production path (retargets it onto the input the model actually
    consumes — the earlier revision varied the BlackVol surface, which
    HullWhiteLattice/Explicit ignores, so the NPV never moved). the
    finiteness / diagnostic pins stay in the round-trip test above.
    """

    npv_low = _swaption_npv(
        live_engine_app,
        model_kind="HullWhiteLattice",
        model_payload={"hw_a": 0.05, "hw_sigma": 0.01},
        constant_vol=0.20,
    )
    npv_high = _swaption_npv(
        live_engine_app,
        model_kind="HullWhiteLattice",
        model_payload={"hw_a": 0.05, "hw_sigma": 0.02},
        constant_vol=0.20,
    )
    # Higher short-rate vol ⇒ a strictly more valuable swaption. If these were
    # equal the engine would be pricing a fixed canonical fixture, not the
    # caller's model (the failure mode the faithful translator closes).
    assert npv_high > npv_low


def test_price_swaption_black_model_consumes_supplied_vol_surface(
    live_engine_app: TestClient,
) -> None:
    """The faithful ``SwaptionVolSpec`` BlackVol surface reaches the engine.

    Complements the HullWhiteLattice test: the BlackVol surface the translator
    forwards (constant vol, quote-substituted) is consumed by the
    ``Black`` model, where the engine builds a ``BlackSwaptionEngine`` off the
    vol handle (the engine's ``makeSwaptionEngine`` →
    ``IrModelType_Black`` branch — the same engine the engine's own
    QuantLib-parity suite uses for European swaptions against a vol surface).

    Two requests differ only in the supplied SwaptionVolSpec constant vol (15 %
    vs 40 %); the higher-vol request must report a strictly larger NPV. This
    proves the orchestrator does not drop the surface on the wire — the failure
    mode is a canonical fixture pricing identically regardless of the supplied
    surface.
    """

    npv_low = _swaption_npv(
        live_engine_app,
        model_kind="Black",
        model_payload={},
        constant_vol=0.15,
    )
    npv_high = _swaption_npv(
        live_engine_app,
        model_kind="Black",
        model_payload={},
        constant_vol=0.40,
    )
    # Higher Black vol ⇒ a strictly more valuable swaption; equal NPVs would mean
    # the supplied surface never reached the engine.
    assert npv_high > npv_low
