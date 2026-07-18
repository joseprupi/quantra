"""Live-engine integration tests for the bonds pricing endpoints.

Two opt-in tests (one per variant) gated on the
``orchestrator_engine_bonds_fixed`` /
``orchestrator_engine_bonds_floating`` markers so they stay skipped
in default CI runs (only opt-in CI runs that ship a live
``quantra-engine`` container should hit these tests).

Required environment (export to opt in):

* ``QUANTRA_ORCHESTRATOR_TEST_ENGINE_GRPC_TARGET`` — gRPC
  ``host:port`` of a reachable pricing engine (e.g.
  ``localhost:50051``). Shared with the cross-product gated test
  ``test_engine_integration.py`` because a single live engine
  serves every RPC the orchestrator forwards.

Per Part C
 each test now expects a real engine round-trip: HTTP 200, a
typed fixed/floating bond result Pydantic shape with a finite
NPV, and the caller's ``X-Request-Id`` echoed back in the
response body.
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
_skip_reason = (
    f"Set {_TARGET_ENV} (e.g. localhost:50051) to run the orchestrator "
    "live-engine bonds integration tests."
)


class _StaticMdClient(MdClient):
    """Returns one resolved-quote per requested canonical ID (flat 3 %).

    Lets the integration tests exercise the full vertical
    (assemble → MD resolve → engine call) without provisioning a
    live MD service. Every curve quote id resolves to 0.03 so the
    faithful translator bootstraps a ~flat 3 % curve that
    can value the 5Y bonds.
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
    return {"X-API-Key": "live-bonds-key"}


def _seeded_api_keys() -> dict[str, ApiKeyRecord]:
    return {
        "live-bonds-key": ApiKeyRecord(
            api_key_id="live-bonds-key",
            owner_uid="bonds-live-uid",
            name="Live Bonds Test Key",
            email="bonds-live@example.com",
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
    """Orchestrator app wired against the live-engine target."""

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


def _deposit_point(
    tenor: dict[str, Any], *, quote_id: str | None = None, rate: float | None = None
) -> dict[str, Any]:
    """A DepositHelper point the faithful translator can bootstrap."""

    point: dict[str, Any] = {
        "tenor": tenor,
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


def _discount_curve(*, level: float | None = None, role: str | None = None) -> dict[str, Any]:
    """A bootstrappable discount curve spanning to 10Y.

    With ``level`` set every point carries that inline rate (a flat curve,
    bypassing MD); otherwise the points reference quote ids the stub MD client
    resolves to a flat 3 %. A ``role`` tags the curve for the role split.
    """

    year = {"n": 1, "unit": "Years"}
    if level is not None:
        points = [
            _deposit_point(year, rate=level),
            _swap_point(2, rate=level),
            _swap_point(5, rate=level),
            _swap_point(10, rate=level),
        ]
    else:
        points = [
            _deposit_point(year, quote_id="USD.IRS.1Y"),
            _swap_point(2, quote_id="USD.IRS.2Y"),
            _swap_point(5, quote_id="USD.IRS.5Y"),
            _swap_point(10, quote_id="USD.IRS.10Y"),
        ]
    curve: dict[str, Any] = {
        "name": "USD-OIS",
        "currency": "USD",
        "day_counter": "Actual365Fixed",
        "reference_date": "2025-01-15",
        "points": points,
    }
    if role is not None:
        curve["body"] = {"role": role}
    return curve


def _projection_curve() -> dict[str, Any]:
    """A bootstrappable projection curve (role=projection) spanning to 5Y."""

    return {
        "name": "USD-SOFR-3M",
        "currency": "USD",
        "day_counter": "Actual365Fixed",
        "reference_date": "2025-01-15",
        "points": [
            _deposit_point({"n": 3, "unit": "Months"}, quote_id="USD.IRS.3M"),
            _swap_point(2, quote_id="USD.SOFR.2Y"),
            _swap_point(5, quote_id="USD.SOFR.5Y"),
        ],
        "body": {"role": "projection"},
    }


@pytest.mark.orchestrator_engine_bonds_fixed
@pytest.mark.skipif(not _target, reason=_skip_reason)
def test_price_bonds_fixed_round_trip_against_live_engine(
    live_engine_app: TestClient,
) -> None:
    """End-to-end fixed-rate round-trip against a live engine target.

    A later revision shape: the gRPC backend dispatches to the live engine
    over /quantra.QuantraServer/PriceFixedRateBond, which returns
    a real :class:`PriceFixedRateBondResponse` flatbuffer. The
    route validates the response into a typed
    :class:`FixedBondResult` and echoes the assembled request back
    in the response body.

    Pins finiteness of the engine NPV + the dirty-price /
    clean-price diagnostic the engine emits, plus the
    ``X-Request-Id`` echo. Numeric parity is layer-4 territory.
    """

    payload = {
        "bond": {"face_amount": 100.0, "coupon_rate": 0.045},
        "curves": [_discount_curve()],
        "as_of": "2025-01-15",
    }
    request_id = f"bonds-fixed-live-{uuid.uuid4()}"
    response = live_engine_app.post(
        "/v1/price/bonds/fixed",
        json=payload,
        headers={**_api_key_headers(), "X-Request-Id": request_id},
    )

    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["pricing_history_id"] is None

    result = body["result"]
    assert isinstance(result["npv"], float)
    assert math.isfinite(result["npv"])
    assert "clean_price" in result
    assert math.isfinite(float(result["clean_price"]))
    assert math.isfinite(float(result["dirty_price"]))

    assembled = body["assembled_request"]
    assert assembled["as_of"] == "2025-01-15"
    assert assembled["trade"]["bond"]["face_amount"] == 100.0
    assert assembled["discount_curve"]["name"] == "USD-OIS"
    # Every curve quote resolved to the flat 3 % stub value (no quote id leaks).
    resolved = {q["canonical_id"]: q["value"] for q in assembled["resolved_quotes"]}
    assert resolved["USD.IRS.1Y"] == pytest.approx(0.03)
    assert all(v == pytest.approx(0.03) for v in resolved.values())


@pytest.mark.orchestrator_engine_bonds_fixed
@pytest.mark.skipif(not _target, reason=_skip_reason)
def test_price_bonds_fixed_is_input_sensitive_against_live_engine(
    live_engine_app: TestClient,
) -> None:
    """The engine prices the caller's curve, not a fixture.

    Two requests differ only in the supplied discount-curve level: a flat 3 %
    curve vs a flat 6 % curve. A fixed-rate bond's price falls as the discount
    curve rises, so the 6 % request must report a strictly lower ``clean_price``.
    Previously the canonical encoder discarded the curve and both priced the same
    fixture — an identical price; now the faithful translator forwards the
    supplied curve, so the price moves. This is the bonds input-sensitivity
    strengthening; the finiteness pins stay in the sibling round-trip tests.
    """

    def _clean_price(level: float) -> float:
        response = live_engine_app.post(
            "/v1/price/bonds/fixed",
            json={
                "bond": {"face_amount": 100.0, "coupon_rate": 0.045},
                "curves": [_discount_curve(level=level)],
                "as_of": "2025-01-15",
            },
            headers=_api_key_headers(),
        )
        assert response.status_code == HTTPStatus.OK, response.text
        clean_price = float(response.json()["result"]["clean_price"])
        assert math.isfinite(clean_price)
        return clean_price

    clean_low_rate = _clean_price(0.03)
    clean_high_rate = _clean_price(0.06)
    # A higher discount curve ⇒ a strictly lower bond price. If these were equal
    # the engine would be pricing a fixed canonical fixture, not the caller's
    # curve (the failure mode the faithful translator closes).
    assert clean_high_rate < clean_low_rate


@pytest.mark.orchestrator_engine_bonds_fixed
@pytest.mark.skipif(not _target, reason=_skip_reason)
def test_price_bonds_fixed_is_input_sensitive_to_coupon_rate(
    live_engine_app: TestClient,
) -> None:
    """The engine prices the caller's ``coupon_rate``, not the engine_io default.

    Previously ``bonds/engine_io.py`` read ``body.get("rate", _DEFAULT_FIXED_RATE)``
    while the gated fixture posted ``coupon_rate`` — a silent-default-class silent
    placeholder: HTTP 200 + a default-priced 5 % bond regardless of what the
    caller asked for, masked because the round-trip test pinned only
    finiteness on a 3 % curve where a 5 % coupon happens to land near par.

    This guard rules that out by seeding a distinctive (``coupon_rate=0.07``)
    and contrastive (``coupon_rate=0.01``) coupon on the *same* flat-4 %
    discount curve. The high-coupon bond's clean price must be (a) strictly
    greater than the low-coupon bond's price, and (b) far enough above
    par-on-default-coupon (~104 for a 5 % bond on a 4 % curve) that the bug
    cannot re-hide via fixture / default collusion. Analytic anchor on the
    5Y window: a 7 %-coupon bond on a 4 %-discount curve prices to
    ~113 (7 * annuity(4 %, 5Y) + 100 * DF(4 %, 5Y)); a 1 %-coupon bond on
    the same curve prices to ~87.
    """

    def _clean_price(coupon: float) -> float:
        response = live_engine_app.post(
            "/v1/price/bonds/fixed",
            json={
                "bond": {"face_amount": 100.0, "coupon_rate": coupon},
                "curves": [_discount_curve(level=0.04)],
                "as_of": "2025-01-15",
            },
            headers=_api_key_headers(),
        )
        assert response.status_code == HTTPStatus.OK, response.text
        clean_price = float(response.json()["result"]["clean_price"])
        assert math.isfinite(clean_price)
        return clean_price

    clean_high_coupon = _clean_price(0.07)
    clean_low_coupon = _clean_price(0.01)

    # Monotonic in the seeded coupon — the cardinal proof the engine reads it.
    assert clean_high_coupon > clean_low_coupon, (
        f"clean_price did not move with coupon_rate: 0.07 → {clean_high_coupon}, "
        f"0.01 → {clean_low_coupon}. Engine is pricing a default coupon, not the seed."
    )

    # Discriminator bands: rule out the default-collusion failure mode.
    # On a flat-4 % discount curve, a 5 %-coupon (the engine_io default)
    # bond prices to ~104; the 0.07 seed must price clearly above that
    # band, and the 0.01 seed clearly below par.
    assert clean_high_coupon > 110.0, (
        f"clean_price={clean_high_coupon} for coupon_rate=0.07 is below the band a "
        "real 7 %-coupon bond on a 4 % curve produces (~113). The engine is "
        "still pricing the default 5 % coupon — coupon_rate is not being read."
    )
    assert clean_low_coupon < 95.0, (
        f"clean_price={clean_low_coupon} for coupon_rate=0.01 is above the band a "
        "real 1 %-coupon bond on a 4 % curve produces (~87). The engine is "
        "still pricing the default 5 % coupon — coupon_rate is not being read."
    )


@pytest.mark.orchestrator_engine_bonds_floating
@pytest.mark.skipif(not _target, reason=_skip_reason)
def test_price_bonds_floating_round_trip_against_live_engine(
    live_engine_app: TestClient,
) -> None:
    """End-to-end floating-rate round-trip against a live engine target.

    Same shape as the fixed-rate sibling. The floating
    variant additionally exercises the projection-curve + index
    resolution path; the engine returns a
    :class:`PriceFloatingRateBondResponse` flatbuffer with both
    NPV and per-leg + per-fixing diagnostics.
    """

    payload = {
        "bond": {"face_amount": 100.0, "spread": 0.0025},
        "curves": [
            _discount_curve(role="discount"),
            _projection_curve(),
        ],
        "index": {
            "kind": "OvernightIndex",
            "currency": "USD",
            "calendar": "UnitedStates::GovernmentBond",
            "day_counter": "Actual/360",
            "body": {"fixingDays": 2},
            "name": "USD-SOFR",
        },
        "as_of": "2025-01-15",
    }
    request_id = f"bonds-floating-live-{uuid.uuid4()}"
    response = live_engine_app.post(
        "/v1/price/bonds/floating",
        json=payload,
        headers={**_api_key_headers(), "X-Request-Id": request_id},
    )

    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["pricing_history_id"] is None

    result = body["result"]
    assert isinstance(result["npv"], float)
    assert math.isfinite(result["npv"])
    assert "clean_price" in result
    assert math.isfinite(float(result["clean_price"]))
    assert math.isfinite(float(result["dirty_price"]))

    assembled = body["assembled_request"]
    assert assembled["as_of"] == "2025-01-15"
    assert assembled["discount_curve"]["name"] == "USD-OIS"
    assert assembled["projection_curve"]["name"] == "USD-SOFR-3M"
    assert assembled["index"]["kind"] == "OvernightIndex"
    canonical_ids = [q["canonical_id"] for q in assembled["resolved_quotes"]]
    assert "USD.IRS.1Y" in canonical_ids
    assert "USD.IRS.3M" in canonical_ids


def _floating_index() -> dict[str, Any]:
    """The inline USD-SOFR overnight projection index the floating bond references."""

    return {
        "kind": "OvernightIndex",
        "currency": "USD",
        "calendar": "UnitedStates::GovernmentBond",
        "day_counter": "Actual/360",
        "body": {"fixingDays": 2},
        "name": "USD-SOFR",
    }


@pytest.mark.orchestrator_engine_bonds_floating
@pytest.mark.skipif(not _target, reason=_skip_reason)
def test_price_bonds_floating_is_input_sensitive_against_live_engine(
    live_engine_app: TestClient,
) -> None:
    """The engine prices the caller's discount curve, not a fixture.

    Two requests differ only in the supplied discount-curve level (a flat 3 % vs
    a flat 6 % discount curve); the projection curve and the USD-SOFR index are
    held fixed. Raising the discounting curve lowers the present value of the
    floating bond's cashflows, so the 6 % request must report a strictly lower
    ``clean_price``. Previously the canonical encoder discarded the curve and both
    priced the same fixture (identical price); this is also the regression guard
    for the shared-registry fix — before it the floating path could not
    reach the engine at all (``Unknown index id: forwarding_index``).
    """

    def _clean_price(level: float) -> float:
        response = live_engine_app.post(
            "/v1/price/bonds/floating",
            json={
                "bond": {"face_amount": 100.0, "spread": 0.0025},
                "curves": [
                    _discount_curve(level=level, role="discount"),
                    _projection_curve(),
                ],
                "index": _floating_index(),
                "as_of": "2025-01-15",
            },
            headers=_api_key_headers(),
        )
        assert response.status_code == HTTPStatus.OK, response.text
        clean_price = float(response.json()["result"]["clean_price"])
        assert math.isfinite(clean_price)
        return clean_price

    clean_low_rate = _clean_price(0.03)
    clean_high_rate = _clean_price(0.06)
    # A higher discount curve ⇒ a strictly lower bond price. Equal prices would
    # mean the engine priced a fixed canonical fixture, not the caller's curve.
    assert clean_high_rate < clean_low_rate
