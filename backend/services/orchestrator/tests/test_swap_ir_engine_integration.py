"""Live-engine integration test for the IR-swap pricing endpoint.

Gated on the ``orchestrator_engine_swap_ir`` marker so it stays
skipped in default CI runs (only opt-in CI runs that ship a live
``quantra-engine`` container should hit this test).

Required environment (export to opt in):

* ``QUANTRA_ORCHESTRATOR_TEST_ENGINE_GRPC_TARGET`` — gRPC
  ``host:port`` of a reachable pricing engine (e.g.
  ``localhost:50051``). Shared with the cross-product gated test
  ``test_engine_integration.py`` because a single live engine
  serves every RPC the orchestrator forwards.

Per Part C the
test now expects a real engine round-trip: HTTP 200, a typed
:class:`IrSwapResult` with a finite NPV and per-leg NPVs, and the
caller's ``X-Request-Id`` echoed back in the response body. Plan
08c flipped this contract from the placeholder
"engine_unavailable + assembled-request echo" assertion that lived
here while the gRPC backend was still a stub.

Mirrors the shape established by ``test_engine_integration.py`` /
``test_md_integration.py``: one gated marker, one explicit
env var, one round-trip route call.
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
    pytest.mark.orchestrator_engine_swap_ir,
    pytest.mark.skipif(
        not _target,
        reason=(
            f"Set {_TARGET_ENV} (e.g. localhost:50051) to run the "
            "orchestrator IR-swap live-engine integration test."
        ),
    ),
]


class _StaticMdClient(MdClient):
    """Returns one resolved-quote per requested canonical ID (flat 3 %).

    Lets the integration test exercise the full vertical (assemble
    → MD resolve → engine call) without provisioning a live MD
    service. Every discount-curve quote id resolves to 0.03 so the
    faithful translator bootstraps a ~flat 3 % curve that
    can value a 5Y swap.
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
                value=0.03,
                source="live-engine-test",
                vendor_id="FAKE",
            )
            for cid in canonical_ids
        ]


def _api_key_headers() -> dict[str, str]:
    return {"X-API-Key": "live-swap-ir-key"}


def _seeded_api_keys() -> dict[str, ApiKeyRecord]:
    return {
        "live-swap-ir-key": ApiKeyRecord(
            api_key_id="live-swap-ir-key",
            owner_uid="swap-ir-live-uid",
            name="Live Swap-IR Test Key",
            email="swap-ir-live@example.com",
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
    the integration test exercises the inline-curve path which
    skips ``app.*`` entirely.
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

    Carries the ``point_type`` discriminator + convention fields the rates-curve
    translator requires; the value is either a ``quote_id`` (resolved by the stub
    MD client) or an inline ``rate``.
    """

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


def _discount_curve(*, level: float | None = None) -> dict[str, Any]:
    """A bootstrappable discount curve spanning to 5Y.

    With ``level`` set every point carries that inline rate (a flat curve,
    bypassing MD); otherwise the points reference quote ids the stub MD client
    resolves to a flat 3 %.
    """

    if level is not None:
        points = [
            _deposit_point(rate=level),
            _swap_point(2, rate=level),
            _swap_point(5, rate=level),
        ]
    else:
        points = [
            _deposit_point(quote_id="USD.IRS.1Y"),
            _swap_point(2, quote_id="USD.IRS.2Y"),
            _swap_point(5, quote_id="USD.IRS.5Y"),
        ]
    return {
        "name": "USD-OIS",
        "currency": "USD",
        "day_counter": "Actual365Fixed",
        "reference_date": "2025-01-15",
        "points": points,
    }


def _ois_point(years: int, *, rate: float) -> dict[str, Any]:
    """An OISHelper point referencing the ESTR overnight index by id.

    Mirrors the EUR OIS curve the live portal builds: each point references
    ``overnight_index: {"id": "ESTR"}``. The faithful translator must register an
    ESTR ``IndexDef`` in ``rates.indices`` (from its known-overnight-index
    catalog) or the engine rejects the curve with ``NOT_FOUND: Unknown index id:
    ESTR``.
    """

    return {
        "point_type": "OISHelper",
        "point": {
            "tenor": {"n": years, "unit": "Years"},
            "overnight_index": {"id": "ESTR"},
            "settlement_days": 2,
            "calendar": "TARGET",
            "fixed_leg_frequency": "Annual",
            "fixed_leg_convention": "ModifiedFollowing",
            "fixed_leg_day_counter": "Actual360",
            "rate": rate,
        },
    }


def _eur_estr_ois_curve() -> dict[str, Any]:
    """A bootstrappable EUR ESTR OIS curve (1 deposit + OIS helpers to 10Y).

    Reproduces the user's failing request shape: OIS helpers whose
    ``overnight_index`` references the ESTR id. Inline rates (flat 2.5 %) keep the
    test independent of MD provisioning while still exercising the index-registry
    path the fix closes.
    """

    # A short (1M) cash deposit anchors the front of the curve; the OIS pillars
    # start at 1Y so no two helpers share a maturity date (overlapping pillars
    # break the QuantLib bootstrap).
    short_deposit = {
        "point_type": "DepositHelper",
        "point": {
            "tenor": {"n": 1, "unit": "Months"},
            "fixing_days": 2,
            "calendar": "TARGET",
            "business_day_convention": "ModifiedFollowing",
            "day_counter": "Actual360",
            "rate": 0.025,
        },
    }
    points: list[dict[str, Any]] = [
        short_deposit,
        *[_ois_point(years, rate=0.025) for years in (1, 2, 3, 4, 5, 7, 10)],
    ]
    return {
        "name": "EUR-ESTR-OIS",
        "currency": "EUR",
        "day_counter": "Actual365Fixed",
        "reference_date": "2026-02-09",
        "points": points,
    }


def test_price_swap_ir_eur_estr_ois_curve_against_live_engine(
    live_engine_app: TestClient,
) -> None:
    """A EUR/ESTR OIS curve prices to a real finite NPV against the live engine.

    Reproduces the user's failing request: an EUR OIS curve whose ``OISHelper``
    points reference ``overnight_index: {"id": "ESTR"}``. Before the fix this
    failed with ``engine RPC failed: NOT_FOUND (Unknown index id: ESTR)`` because
    the orchestrator never registered an ESTR ``IndexDef`` in ``rates.indices``.
    The fix registers it from the known-overnight-index catalog, so the engine
    bootstraps the curve and returns a real finite NPV.
    """

    payload = {
        "swap": {
            "notional": 10_000_000.0,
            "fixed_rate": 0.025,
            "swap_type": "payer",
            "effective_date": "2026-02-11",
            "termination_date": "2031-02-11",
        },
        "curves": [_eur_estr_ois_curve()],
        "as_of": "2026-02-09",
    }
    response = live_engine_app.post(
        "/v1/price/swap/ir",
        json=payload,
        headers=_api_key_headers(),
    )

    assert response.status_code == HTTPStatus.OK, response.text
    result = response.json()["result"]
    assert isinstance(result["npv"], float)
    assert math.isfinite(result["npv"])
    assert math.isfinite(float(result["extras"]["fair_rate"]))


def test_price_swap_ir_round_trip_against_live_engine(
    live_engine_app: TestClient,
) -> None:
    """End-to-end round-trip against a live engine target.

    A later revision shape: the gRPC backend dispatches to the live
    engine over /quantra.QuantraServer/PriceVanillaSwap, which
    returns a real :class:`PriceVanillaSwapResponse` flatbuffer.
    The route validates the response into a typed
    :class:`IrSwapResult` and echoes the assembled request back
    in the response body. The faithful
    translator consumes the supplied discount curve, so the payload
    carries ``point_type``-tagged helper points the rates-curve
    translator can bootstrap (the older simple ``{tenor, quote_id}``
    shape is no longer translatable). The fixed_rate default (3.5%) is
    above the ~3% curve so the payer NPV is negative — the test pins
    finiteness + presence of per-leg NPVs + the fair-rate diagnostic,
    not numeric parity (that's layer 4 in the test pyramid).

    Also pins the ``X-Request-Id`` echo: the caller-provided
    request id surfaces in the response body. The
    :class:`RequestIdInterceptor` forwards it as gRPC metadata; the
    engine's structured-log surface is the layer-3 source of truth
    for "did the engine see the same id" but we don't assert on
    engine logs from the orchestrator suite.
    """

    payload = {
        "swap": {
            "notional": 1_000_000.0,
            "effective_date": "2025-01-17",
            "termination_date": "2030-01-17",
        },
        "curves": [_discount_curve()],
        "as_of": "2025-01-15",
    }
    request_id = f"swap-ir-live-{uuid.uuid4()}"
    response = live_engine_app.post(
        "/v1/price/swap/ir",
        json=payload,
        headers={**_api_key_headers(), "X-Request-Id": request_id},
    )

    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["pricing_history_id"] is None

    result = body["result"]
    assert isinstance(result["npv"], float)
    assert math.isfinite(result["npv"])
    leg_roles = [entry["role"] for entry in result["leg_npvs"]]
    assert leg_roles == ["fixed", "floating"]
    for leg in result["leg_npvs"]:
        assert math.isfinite(float(leg["npv"]))
    assert "fair_rate" in result["extras"]
    assert math.isfinite(float(result["extras"]["fair_rate"]))

    assembled = body["assembled_request"]
    assert assembled["as_of"] == "2025-01-15"
    assert assembled["trade"]["swap"]["notional"] == 1_000_000.0
    assert len(assembled["curves"]) == 1
    # Every curve quote resolved to the flat 3 % stub value (no quote id leaks).
    resolved = {q["canonical_id"]: q["value"] for q in assembled["resolved_quotes"]}
    assert resolved == {
        "USD.IRS.1Y": pytest.approx(0.03),
        "USD.IRS.2Y": pytest.approx(0.03),
        "USD.IRS.5Y": pytest.approx(0.03),
    }


def test_price_swap_ir_is_input_sensitive_against_live_engine(
    live_engine_app: TestClient,
) -> None:
    """The engine prices the caller's curve, not a fixture.

    Two requests differ only in the supplied discount-curve level: a flat 3 %
    curve vs a flat 5 % curve. The par (fair) swap rate tracks the curve, so the
    5 % request must report a strictly higher ``fair_rate`` than the 3 % request.
    Previously the canonical encoder discarded the curve and both priced the same
    flat-3 % fixture — an identical ``fair_rate``; now the faithful
    translator forwards the supplied curve, so the diagnostic moves. This is the
    swap_ir input-sensitivity strengthening (a finite NPV alone no longer
    proves the engine consumed the request); the round-trip / finiteness pins
    stay in the sibling test above.
    """

    def _fair_rate(level: float) -> float:
        response = live_engine_app.post(
            "/v1/price/swap/ir",
            json={
                "swap": {
                    "notional": 1_000_000.0,
                    "effective_date": "2025-01-17",
                    "termination_date": "2030-01-17",
                },
                "curves": [_discount_curve(level=level)],
                "as_of": "2025-01-15",
            },
            headers=_api_key_headers(),
        )
        assert response.status_code == HTTPStatus.OK, response.text
        fair_rate = float(response.json()["result"]["extras"]["fair_rate"])
        assert math.isfinite(fair_rate)
        return fair_rate

    fair_rate_low = _fair_rate(0.03)
    fair_rate_high = _fair_rate(0.05)
    # A higher discount/forward curve ⇒ a higher par swap rate. If these were
    # equal the engine would be pricing a fixed canonical fixture, not the
    # caller's curve (the failure mode the faithful translator closes).
    assert fair_rate_high > fair_rate_low


def _flat_curve(name: str, *, level: float) -> dict[str, Any]:
    """A flat, bootstrappable curve at ``level`` spanning to 5Y (inline rates, no MD).

    Same helper shape as ``_discount_curve`` but with a caller-chosen name so a
    request can carry two *distinct* curves (a genuine discount-vs-forwarding
    basis) rather than reusing one.
    """

    return {
        "name": name,
        "currency": "USD",
        "day_counter": "Actual365Fixed",
        "reference_date": "2025-01-15",
        "points": [
            _deposit_point(rate=level),
            _swap_point(2, rate=level),
            _swap_point(5, rate=level),
        ],
    }


def _ibor_index(*, months: int) -> dict[str, Any]:
    """An inline float-leg index body at an ``months``-month tenor (pure config).

       The assembler projects ``swap.pricing.indices[0]`` into the resolved
       float-leg index; the translator builds a faithful ``IndexDef`` whose tenor
    now drives the float coupon schedule frequency.
    """

    return {
        "name": "USD-IBOR",
        "kind": "IborIndex",
        "currency": "USD",
        "calendar": "TARGET",
        "day_counter": "Actual360",
        "tenor": {"n": months, "unit": "Months"},
    }


def _swap_body_with_index(*, index_months: int) -> dict[str, Any]:
    """A 5Y payer swap body carrying an inline float index at the given tenor."""

    return {
        # Off-par fixed rate (2 % vs the 3 % discount curve) so the single-curve
        # NPV is a non-trivial non-zero value — the tenor-invariance assertion
        # then genuinely tests "equal", not "both happen to be zero at par".
        "notional": 10_000_000.0,
        "fixed_rate": 0.02,
        "swap_type": "payer",
        "effective_date": "2025-01-17",
        "termination_date": "2030-01-17",
        "pricing": {"indices": [_ibor_index(months=index_months)]},
    }


def test_price_swap_ir_single_curve_is_tenor_invariant_against_live_engine(
    live_engine_app: TestClient,
) -> None:
    """SINGLE-curve: 3M vs 6M float index ⇒ the SAME NPV (correct finance, not a bug).

    With one curve serving BOTH the discount and forwarding roles (discount ==
    forwarding), the floating leg telescopes to
    ``notional * (DF_start - DF_maturity)`` -- every projected forward coupon is
    exactly offset by the discounting, so the coupon frequency / index tenor
    cancels out. Changing the float index tenor from 3M to 6M therefore MUST NOT
    move the NPV on a single flat curve. This documents that the fix does not
    perturb the (correct) single-curve behaviour; the multi-curve sibling test
    below is where the tenor legitimately matters.
    """

    def _npv(index_months: int) -> float:
        response = live_engine_app.post(
            "/v1/price/swap/ir",
            json={
                "swap": _swap_body_with_index(index_months=index_months),
                "curves": [_flat_curve("USD-SINGLE", level=0.03)],
                "as_of": "2025-01-15",
            },
            headers=_api_key_headers(),
        )
        assert response.status_code == HTTPStatus.OK, response.text
        npv = float(response.json()["result"]["npv"])
        assert math.isfinite(npv)
        return npv

    npv_6m = _npv(6)
    npv_3m = _npv(3)
    # Single flat curve ⇒ float leg is frequency-independent ⇒ identical NPV.
    assert npv_3m == pytest.approx(npv_6m, rel=1e-9, abs=1e-6)


def test_price_swap_ir_dual_curve_is_tenor_sensitive_against_live_engine(
    live_engine_app: TestClient,
) -> None:
    """DUAL-curve: 3M vs 6M float index ⇒ DIFFERENT NPVs (fix proof).

    Two DISTINCT curves — a flat 3 % discount curve and a flat 5 % forwarding
    curve (a +200bp basis) — break the single-curve telescoping, so the floating
    leg's coupon schedule (its frequency, derived from the index tenor) now
    affects the PV. A 3M index (quarterly float coupons) and a 6M index
    (semi-annual) therefore price to DIFFERENT NPVs.

    Before the fix the float schedule was hardcoded ``Frequency.Semiannual``
    regardless of the index tenor, so both requests produced an IDENTICAL NPV
    even on this dual-curve setup — the tenor was invisible to the engine. This
    test is the regression guard: it fails on the pre-fix code and passes now.
    """

    def _npv(index_months: int) -> float:
        response = live_engine_app.post(
            "/v1/price/swap/ir",
            json={
                "swap": _swap_body_with_index(index_months=index_months),
                "curves": [
                    _flat_curve("USD-DISCOUNT", level=0.03),
                    _flat_curve("USD-FORWARDING", level=0.05),
                ],
                "as_of": "2025-01-15",
            },
            headers=_api_key_headers(),
        )
        assert response.status_code == HTTPStatus.OK, response.text
        npv = float(response.json()["result"]["npv"])
        assert math.isfinite(npv)
        return npv

    npv_6m = _npv(6)
    npv_3m = _npv(3)
    # Distinct discount vs forwarding curve ⇒ the float coupon frequency (index
    # tenor) genuinely moves the PV. Previously these were byte-identical.
    assert npv_3m != pytest.approx(npv_6m, rel=1e-9, abs=1e-6)
