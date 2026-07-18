"""Per-product smoke against the FULL live stack: live engine + live MD + seeded md.* rows.

This is the close-out test that catches the class of bug. The
``orchestrator_engine_*`` siblings wire a stub ``MdClient`` in-process
and never exercise the MD-service deployment; this module instead lets
the lifespan build the real ``MdClient`` (httpx → MD service over HTTP)
and the real gRPC ``EngineClient``, then seeds the rows each product's
known-good reference body needs into ``md.canonical_ids`` + ``md.quote_points``
under the ``md_rw`` role. Each smoke seeds, POSTs to ``/v1/price/<product>``,
asserts HTTP 200 + a finite NPV + the seeded value in the response's
``resolved_quotes`` echo (so a future MD-service regression that silently
loses a row still trips the smoke), then DELETEs the seeded ids on
teardown so the dev DB is left as we found it.

Gated on the ``orchestrator_md_smoke`` marker. To run set all three env
vars at once:

* ``QUANTRA_ORCHESTRATOR_TEST_ENGINE_GRPC_TARGET`` — gRPC ``host:port``
  of the live pricing engine (e.g. ``localhost:50051``). Shared with
  the ``orchestrator_engine_*`` family.
* ``QUANTRA_ORCHESTRATOR_TEST_MD_BASE_URL`` — base URL of the live MD
  service (e.g. ``http://localhost:8082``). Shared with the
  ``orchestrator_md`` family.
* ``QUANTRA_ORCHESTRATOR_MD_SMOKE_DSN`` — asyncpg DSN for the ``md_rw``
  role (e.g. ``postgresql+asyncpg://md_rw:md_rw@localhost:5434/quantra``).
  Seeding-only; the orchestrator's read path goes through MD over HTTP,
  not through this DSN.

Skips that are intentional (documented escalations, not gaps):

* ``cds`` only exercises the par-spread bootstrap variant; the upfront
  variant is an open engine escalation — ``fairUpfront`` throws
  on upfront-variant CDS.
* ``swaps_inflation`` only exercises ZCIIS; YYIIS is an open
  engine escalation — QuantLib ABORTs on the YYIIS path.

Numeric parity is **not** the goal here — this is a wiring liveness gate
(MD reachable, MD writes propagate to the read path, orchestrator
threads the value through to the engine, engine returns a usable
result). Numeric parity stays the layer-4 / Phase-7 deferred lane.
"""

from __future__ import annotations

import json
import math
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime
from http import HTTPStatus
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from quantra_common.auth.context import ApiKeyRecord
from quantra_common.auth.lookup import ApiKeyLookup
from quantra_common.settings import Environment, LogLevel
from quantra_md_ingester.models import QuoteRecord
from quantra_md_ingester.pipeline import build_snapshot, run_pipeline
from quantra_orchestrator.app import create_app
from quantra_orchestrator.settings import OrchestratorSettings

_ENGINE_TARGET_ENV = "QUANTRA_ORCHESTRATOR_TEST_ENGINE_GRPC_TARGET"
_MD_BASE_URL_ENV = "QUANTRA_ORCHESTRATOR_TEST_MD_BASE_URL"
_MD_SMOKE_DSN_ENV = "QUANTRA_ORCHESTRATOR_MD_SMOKE_DSN"
# The snapshot-lifecycle test additionally needs an ``app_rw``
# pool so it can write a one-off ``app.users`` + ``app.snapshots`` row
# that pins ``content.md_version_etag`` to the etag the ingester just
# produced. The per-product smokes above don't need ``app.*`` writes
# (they pass curves inline with ``snapshot_id=None``) so the env var
# is optional — the lifecycle test skips if it's unset.
_APP_SMOKE_DSN_ENV = "QUANTRA_ORCHESTRATOR_APP_SMOKE_DSN"

_engine_target = os.environ.get(_ENGINE_TARGET_ENV)
_md_base_url = os.environ.get(_MD_BASE_URL_ENV)
_md_smoke_dsn = os.environ.get(_MD_SMOKE_DSN_ENV)
_app_smoke_dsn = os.environ.get(_APP_SMOKE_DSN_ENV)

_missing = [
    name
    for name, value in (
        (_ENGINE_TARGET_ENV, _engine_target),
        (_MD_BASE_URL_ENV, _md_base_url),
        (_MD_SMOKE_DSN_ENV, _md_smoke_dsn),
    )
    if not value
]

pytestmark = [
    pytest.mark.orchestrator_md_smoke,
    pytest.mark.skipif(
        bool(_missing),
        reason=(
            "Set "
            + ", ".join(_missing)
            + " to run the O21 per-product live MD + live engine smokes."
        ),
    ),
]

_AS_OF = "2025-01-15"
_AS_OF_TS = datetime(2025, 1, 15, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Seeding plumbing
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def md_rw_engine() -> AsyncIterator[AsyncEngine]:
    """Async engine bound to the ``md_rw`` role for seeding + cleanup."""

    assert _md_smoke_dsn is not None
    engine = create_async_engine(_md_smoke_dsn, pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _insert_canonical(
    conn: Any, canonical_id: str, *, asset_class: str, currency: str
) -> None:
    """Insert one ``md.canonical_ids`` row (idempotent on the PK)."""

    await conn.execute(
        text(
            """
            INSERT INTO md.canonical_ids
              (canonical_id, asset_class, family, instrument, currency, tenor, field,
               frequency, units, description)
            VALUES
              (:cid, :asset_class, 'o21-smoke', 'smoke', :currency, NULL, 'rate',
               'daily', 'decimal_rate', 'O21 smoke seed')
            ON CONFLICT (canonical_id) DO NOTHING
            """
        ),
        {"cid": canonical_id, "asset_class": asset_class, "currency": currency},
    )


async def _insert_quote_point(conn: Any, canonical_id: str, value: float) -> None:
    """Insert one ``md.quote_points`` row at the canonical ``as_of``."""

    await conn.execute(
        text(
            """
            INSERT INTO md.quote_points
              (canonical_id, as_of, value, source, vendor_id, units)
            VALUES
              (:cid, :as_of, :value, 'o21-smoke', 'O21', 'decimal_rate')
            ON CONFLICT (canonical_id, as_of) DO UPDATE
              SET value = EXCLUDED.value,
                  source = EXCLUDED.source,
                  vendor_id = EXCLUDED.vendor_id
            """
        ),
        {"cid": canonical_id, "as_of": _AS_OF_TS, "value": value},
    )


@pytest_asyncio.fixture
async def seed_quotes(md_rw_engine: AsyncEngine) -> AsyncIterator[Any]:
    """Yield a callable that seeds quote rows and registers them for cleanup.

    Cleanup deletes only the canonical_ids the test seeded (narrow
    ``WHERE canonical_id = ANY(:ids)``) — never a blanket ``DELETE
    FROM md.*``, so any operator-loaded production-ish data in the
    dev DB survives the test run untouched.
    """

    seeded_ids: list[str] = []

    async def _seed(rows: list[tuple[str, float]], *, asset_class: str, currency: str) -> None:
        async with md_rw_engine.begin() as conn:
            for canonical_id, value in rows:
                await _insert_canonical(
                    conn, canonical_id, asset_class=asset_class, currency=currency
                )
                await _insert_quote_point(conn, canonical_id, value)
                seeded_ids.append(canonical_id)

    try:
        yield _seed
    finally:
        if seeded_ids:
            async with md_rw_engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM md.quote_points WHERE canonical_id = ANY(:ids)"),
                    {"ids": seeded_ids},
                )
                await conn.execute(
                    text("DELETE FROM md.canonical_ids WHERE canonical_id = ANY(:ids)"),
                    {"ids": seeded_ids},
                )


# ---------------------------------------------------------------------------
# Orchestrator app fixture — real MdClient + real gRPC EngineClient
# ---------------------------------------------------------------------------


_API_KEY = "o21-smoke-key"


def _api_key_headers() -> dict[str, str]:
    return {"X-API-Key": _API_KEY}


def _seeded_api_keys() -> dict[str, ApiKeyRecord]:
    return {
        _API_KEY: ApiKeyRecord(
            api_key_id=_API_KEY,
            owner_uid="o21-smoke-uid",
            name="O21 Smoke Test Key",
            email="o21-smoke@example.com",
            tier="free",
            active=True,
        )
    }


@pytest.fixture
def live_smoke_app(
    fake_api_key_lookup: ApiKeyLookup,
    fake_firebase_verifier: tuple[Any, dict[str, dict[str, Any]]],
    fake_api_keys: dict[str, ApiKeyRecord],
) -> Iterator[TestClient]:
    """Orchestrator app wired against the live engine + live MD service.

    Function-scoped so each test gets a fresh TtlBoundedQuoteCache (the
    lifespan builds one per app); otherwise a stale cached resolved-
    quote from a prior test could shadow a re-seeded value.
    """

    fake_api_keys.update(_seeded_api_keys())
    verifier, _ = fake_firebase_verifier
    assert _engine_target is not None
    assert _md_base_url is not None
    settings = OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="testsha",
        engine_grpc_target=_engine_target,
        md_service_url=_md_base_url,
    )
    app: FastAPI = create_app(
        settings,
        api_key_lookup=fake_api_key_lookup,
        firebase_verifier=verifier,
    )
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Curve builders — mirror the gated ``orchestrator_engine_*`` shapes
# ---------------------------------------------------------------------------


def _deposit_point(quote_id: str, *, tenor: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "point_type": "DepositHelper",
        "point": {
            "tenor": tenor or {"n": 1, "unit": "Years"},
            "fixing_days": 2,
            "calendar": "TARGET",
            "business_day_convention": "ModifiedFollowing",
            "day_counter": "Actual365Fixed",
            "quote_id": quote_id,
        },
    }


def _swap_point(years: int, quote_id: str) -> dict[str, Any]:
    return {
        "point_type": "SwapHelper",
        "point": {
            "tenor": {"n": years, "unit": "Years"},
            "calendar": "TARGET",
            "sw_fixed_leg_frequency": "Annual",
            "sw_fixed_leg_convention": "ModifiedFollowing",
            "sw_fixed_leg_day_counter": "Thirty360",
            "quote_id": quote_id,
        },
    }


def _usd_discount_curve(*, with_long_swap: bool = False) -> dict[str, Any]:
    """USD discount curve spanning to 5Y (or 12Y when ``with_long_swap``).

    The 12Y span is used by CDS (10Y IMM-rolled CDS helper needs the curve
    to extend past ~10.18y).
    """

    points: list[dict[str, Any]] = [
        _deposit_point("USD.IRS.1Y"),
        _swap_point(2, "USD.IRS.2Y"),
        _swap_point(5, "USD.IRS.5Y"),
    ]
    if with_long_swap:
        points += [_swap_point(10, "USD.IRS.10Y"), _swap_point(12, "USD.IRS.12Y")]
    return {
        "name": "USD-OIS",
        "currency": "USD",
        "day_counter": "Actual365Fixed",
        "reference_date": _AS_OF,
        "points": points,
    }


def _usd_floating_discount_curve() -> dict[str, Any]:
    """USD discount curve to 10Y with a ``discount`` role tag."""

    return {
        "name": "USD-OIS",
        "currency": "USD",
        "day_counter": "Actual365Fixed",
        "reference_date": _AS_OF,
        "points": [
            _deposit_point("USD.IRS.1Y"),
            _swap_point(2, "USD.IRS.2Y"),
            _swap_point(5, "USD.IRS.5Y"),
            _swap_point(10, "USD.IRS.10Y"),
        ],
        "body": {"role": "discount"},
    }


def _usd_projection_curve() -> dict[str, Any]:
    """USD SOFR projection curve (role=projection)."""

    return {
        "name": "USD-SOFR-3M",
        "currency": "USD",
        "day_counter": "Actual365Fixed",
        "reference_date": _AS_OF,
        "points": [
            _deposit_point("USD.IRS.3M", tenor={"n": 3, "unit": "Months"}),
            _swap_point(2, "USD.SOFR.2Y"),
            _swap_point(5, "USD.SOFR.5Y"),
        ],
        "body": {"role": "projection"},
    }


# Quote-id sets per product — kept here so the per-product test bodies
# only carry the request payload, not the seeding boilerplate.
_USD_IRS_3PT = [
    ("USD.IRS.1Y", 0.045),
    ("USD.IRS.2Y", 0.045),
    ("USD.IRS.5Y", 0.045),
]
_USD_IRS_5PT = [
    *_USD_IRS_3PT,
    ("USD.IRS.10Y", 0.045),
    ("USD.IRS.12Y", 0.045),
]
_USD_IRS_4PT = [*_USD_IRS_3PT, ("USD.IRS.10Y", 0.045)]
_USD_FLOATING = [
    *_USD_IRS_4PT,
    ("USD.IRS.3M", 0.045),
    ("USD.SOFR.2Y", 0.045),
    ("USD.SOFR.5Y", 0.045),
]
_SWAPTION_QUOTES = [*_USD_IRS_4PT, ("USD.SWPTN.ATM.5Y10Y.VOL", 0.20)]
_EQUITY_QUOTES = [("USD.IRS.1Y", 0.045)]
_EUR_INFLATION_QUOTES = [
    ("EUR.IRS.1Y", 0.025),
    ("EUR.IRS.2Y", 0.025),
    ("EUR.IRS.5Y", 0.025),
    ("EUR.IRS.10Y", 0.025),
    ("EUR.HICP.5Y", 0.022),
]


def _assert_resolved(body: dict[str, Any], expected: dict[str, float]) -> None:
    """The MD-service round-trip placed our seeded values in ``resolved_quotes``."""

    resolved = {q["canonical_id"]: q["value"] for q in body["assembled_request"]["resolved_quotes"]}
    for cid, value in expected.items():
        assert resolved.get(cid) == pytest.approx(value), (
            f"resolved_quotes echo missing seeded {cid}={value} "
            f"(got {resolved.get(cid)!r}); MD path may have dropped the row."
        )


def _post_price(
    client: TestClient, endpoint: str, payload: dict[str, Any], *, product: str
) -> dict[str, Any]:
    response = client.post(
        endpoint,
        json=payload,
        headers={**_api_key_headers(), "X-Request-Id": f"o21-{product}-{uuid.uuid4()}"},
    )
    assert response.status_code == HTTPStatus.OK, response.text
    body: dict[str, Any] = response.json()
    return body


# ---------------------------------------------------------------------------
# Per-product smokes
# ---------------------------------------------------------------------------


async def test_swap_ir_smoke(live_smoke_app: TestClient, seed_quotes: Any) -> None:
    """``/v1/price/swap/ir`` against live MD + live engine + seeded USD.IRS quotes."""

    await seed_quotes(_USD_IRS_3PT, asset_class="rates", currency="USD")
    payload = {
        "swap": {
            "notional": 1_000_000.0,
            "effective_date": "2025-01-17",
            "termination_date": "2030-01-17",
        },
        "curves": [_usd_discount_curve()],
        "as_of": _AS_OF,
    }
    body = _post_price(live_smoke_app, "/v1/price/swap/ir", payload, product="swap-ir")
    npv = float(body["result"]["npv"])
    assert math.isfinite(npv)
    assert npv != 0.0
    fair_rate = float(body["result"]["extras"]["fair_rate"])
    assert math.isfinite(fair_rate)
    assert fair_rate == pytest.approx(0.045, abs=5e-3), (
        f"fair_rate={fair_rate} far from the seeded 4.5% curve; the engine may "
        "be pricing a fixture rather than the MD-resolved curve."
    )
    _assert_resolved(body, dict(_USD_IRS_3PT))


async def test_cds_smoke(live_smoke_app: TestClient, seed_quotes: Any) -> None:
    """``/v1/price/cds`` (par-spread bootstrap variant — upfront skipped)."""

    await seed_quotes(_USD_IRS_5PT, asset_class="rates", currency="USD")
    payload = {
        "cds": {
            "notional": 10_000_000.0,
            "running_coupon": 0.01,
            "schedule": {
                "effective_date": _AS_OF,
                "termination_date": "2030-01-15",
            },
        },
        "curves": [_usd_discount_curve(with_long_swap=True)],
        "credit_curve": {
            "name": "ACME-SR bootstrapped",
            "recovery_rate": 0.4,
            "source": "manual",
            "body": {
                "points": [
                    {"tenor": "1Y", "quoted_par_spread": 0.010},
                    {"tenor": "3Y", "quoted_par_spread": 0.015},
                    {"tenor": "5Y", "quoted_par_spread": 0.020},
                    {"tenor": "10Y", "quoted_par_spread": 0.025},
                ]
            },
        },
        "as_of": _AS_OF,
    }
    body = _post_price(live_smoke_app, "/v1/price/cds", payload, product="cds")
    result = body["result"]
    assert math.isfinite(float(result["npv"]))
    fair_spread = float(result["fair_spread"])
    assert math.isfinite(fair_spread)
    assert fair_spread > 0.0
    assert float(result["protection_leg_npv"]) != 0.0
    assert float(result["premium_leg_npv"]) != 0.0
    # credit curves bypass MD — only the rates curve quotes should
    # appear in the resolved echo (no credit_curve quote_ids leaked).
    _assert_resolved(body, dict(_USD_IRS_5PT))


async def test_bonds_fixed_smoke(live_smoke_app: TestClient, seed_quotes: Any) -> None:
    await seed_quotes(_USD_IRS_4PT, asset_class="rates", currency="USD")
    payload = {
        "bond": {"face_amount": 100.0, "coupon_rate": 0.045},
        "curves": [_usd_floating_discount_curve()],
        "as_of": _AS_OF,
    }
    body = _post_price(live_smoke_app, "/v1/price/bonds/fixed", payload, product="bonds-fixed")
    result = body["result"]
    npv = float(result["npv"])
    assert math.isfinite(npv)
    clean_price = float(result["clean_price"])
    assert math.isfinite(clean_price)
    # 4.5% coupon on a 4.5% flat discount curve ⇒ ~par ($100). A wide
    # band catches the silent-default-class silent placeholder (5% default coupon
    # on the engine_io side would push clean_price > $103).
    assert 95.0 < clean_price < 105.0, (
        f"clean_price={clean_price} outside the at-par band for a 4.5% bond "
        "on a 4.5% flat curve; engine may not be reading the seeded coupon."
    )
    _assert_resolved(body, dict(_USD_IRS_4PT))


async def test_bonds_floating_smoke(live_smoke_app: TestClient, seed_quotes: Any) -> None:
    await seed_quotes(_USD_FLOATING, asset_class="rates", currency="USD")
    payload = {
        "bond": {"face_amount": 100.0, "spread": 0.0025},
        "curves": [_usd_floating_discount_curve(), _usd_projection_curve()],
        "index": {
            "kind": "OvernightIndex",
            "currency": "USD",
            "calendar": "UnitedStates::GovernmentBond",
            "day_counter": "Actual/360",
            "body": {"fixingDays": 2},
            "name": "USD-SOFR",
        },
        "as_of": _AS_OF,
    }
    body = _post_price(
        live_smoke_app, "/v1/price/bonds/floating", payload, product="bonds-floating"
    )
    result = body["result"]
    npv = float(result["npv"])
    assert math.isfinite(npv)
    clean_price = float(result["clean_price"])
    assert math.isfinite(clean_price)
    assert clean_price > 0.0
    _assert_resolved(body, dict(_USD_FLOATING))


async def test_swaption_smoke(live_smoke_app: TestClient, seed_quotes: Any) -> None:
    await seed_quotes(_USD_IRS_4PT, asset_class="rates", currency="USD")
    await seed_quotes(
        [("USD.SWPTN.ATM.5Y10Y.VOL", 0.20)],
        asset_class="volatility",
        currency="USD",
    )
    payload = {
        "swaption": {"exercise_type": "European"},
        "curves": [
            {
                "name": "USD-OIS",
                "currency": "USD",
                "day_counter": "Actual365Fixed",
                "reference_date": _AS_OF,
                "points": [
                    _deposit_point("USD.IRS.1Y"),
                    _swap_point(2, "USD.IRS.2Y"),
                    _swap_point(5, "USD.IRS.5Y"),
                    _swap_point(10, "USD.IRS.10Y"),
                ],
            }
        ],
        "vol_surface": {
            "kind": "SwaptionVolSpec",
            "payload": {"base": {"reference_date": _AS_OF, "quote_id": "USD.SWPTN.ATM.5Y10Y.VOL"}},
        },
        "swaption_model": {
            "kind": "HullWhiteLattice",
            "payload": {"hw_a": 0.05, "hw_sigma": 0.01},
        },
        "as_of": _AS_OF,
    }
    body = _post_price(live_smoke_app, "/v1/price/swaption", payload, product="swaption")
    result = body["result"]
    npv = float(result["npv"])
    assert math.isfinite(npv)
    assert npv != 0.0
    iv = float(result["extras"]["implied_volatility"])
    assert math.isfinite(iv)
    _assert_resolved(body, dict(_SWAPTION_QUOTES))


async def test_equity_options_smoke(live_smoke_app: TestClient, seed_quotes: Any) -> None:
    await seed_quotes(_EQUITY_QUOTES, asset_class="rates", currency="USD")
    payload = {
        "equity_option": {
            "option": {
                "trade_id": "o21-smoke-AAPL",
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
                "reference_date": _AS_OF,
                "role": "discount",
                "points": [
                    {
                        "point_type": "DepositHelper",
                        "tenor": {"n": 1, "unit": "Years"},
                        "fixing_days": 2,
                        "calendar": "TARGET",
                        "business_day_convention": "ModifiedFollowing",
                        "day_counter": "Actual365Fixed",
                        "quote_id": "USD.IRS.1Y",
                    }
                ],
            },
            {
                "name": "AAPL-DIV",
                "currency": "USD",
                "day_counter": "Actual/365",
                "reference_date": _AS_OF,
                "role": "dividend",
                "points": [
                    {
                        "point_type": "DepositHelper",
                        "tenor": {"n": 1, "unit": "Years"},
                        "fixing_days": 2,
                        "calendar": "TARGET",
                        "business_day_convention": "ModifiedFollowing",
                        "day_counter": "Actual365Fixed",
                        "rate": 0.01,
                    }
                ],
            },
        ],
        "vol_surface": {
            "name": "AAPL-vol",
            "kind": "BlackVolSpec",
            "payload": {"base": {"reference_date": _AS_OF, "constant_vol": 0.20}},
        },
        "spot": {"value": 100.0},
        "as_of": _AS_OF,
    }
    body = _post_price(live_smoke_app, "/v1/price/equity-option", payload, product="equity-option")
    result = body["result"]
    npv = float(result["npv"])
    assert math.isfinite(npv)
    assert npv > 0.0  # ATM 1Y call at 20% vol is unambiguously positive.
    for key in ("delta", "gamma", "vega", "theta", "rho", "implied_volatility"):
        assert math.isfinite(float(result[key]))
    _assert_resolved(body, dict(_EQUITY_QUOTES))


async def test_swaps_inflation_zciis_smoke(live_smoke_app: TestClient, seed_quotes: Any) -> None:
    """ZCIIS only — YYIIS (engine ABORT) is intentionally skipped."""

    await seed_quotes(_EUR_INFLATION_QUOTES, asset_class="rates", currency="EUR")
    payload = {
        "swap": {
            "swap_kind": "zero_coupon",
            "swaps": [
                {
                    "zero_coupon_inflation_swap": {
                        "swap_type": "Payer",
                        "notional": 1_000_000.0,
                        "start_date": _AS_OF,
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
                "reference_date": _AS_OF,
                "role": "nominal",
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
                "reference_date": _AS_OF,
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
        "as_of": _AS_OF,
    }
    body = _post_price(
        live_smoke_app, "/v1/price/swaps/inflation", payload, product="swaps-inflation"
    )
    result = body["result"]
    assert result["swap_kind"] == "zero_coupon"
    for key in ("npv", "fair_rate", "fixed_leg_npv", "inflation_leg_npv"):
        assert math.isfinite(float(result[key]))
    _assert_resolved(body, dict(_EUR_INFLATION_QUOTES))


# ---------------------------------------------------------------------------
# Negative test — empty MD ⇒ 422 ``swap_ir_quote_resolution_failed``
# ---------------------------------------------------------------------------


async def test_negative_unseeded_quote_returns_422_quote_resolution_failed(
    live_smoke_app: TestClient,
) -> None:
    """A fresh canonical_id with no md.* row surfaces the 422 envelope live.

    Previously this path was unreachable end-to-end because MD itself was
    502'ing on every request — this test is the cousin gate that proves
    the ``<product>_quote_resolution_failed`` error code actually
    fires against the live MD service, not just under the in-process
    stub.
    """

    fresh_id = f"USD.IRS.O21NEG.{uuid.uuid4().hex[:8]}"
    payload = {
        "swap": {
            "notional": 1_000_000.0,
            "effective_date": "2025-01-17",
            "termination_date": "2030-01-17",
        },
        "curves": [
            {
                "name": "USD-OIS",
                "currency": "USD",
                "day_counter": "Actual365Fixed",
                "reference_date": _AS_OF,
                "points": [_deposit_point(fresh_id)],
            }
        ],
        "as_of": _AS_OF,
    }
    response = live_smoke_app.post(
        "/v1/price/swap/ir",
        json=payload,
        headers={**_api_key_headers(), "X-Request-Id": f"o21-negative-{uuid.uuid4()}"},
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, response.text
    body = response.json()
    assert body["code"] == "swap_ir_quote_resolution_failed", body
    # The envelope must surface the unresolved id so a client can fix it.
    missing = [entry["canonical_id"] for entry in body["details"]]
    assert fresh_id in missing, body


# ---------------------------------------------------------------------------
# End-to-end snapshot lifecycle: ingester build → soft-pin → priced
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def app_rw_engine() -> AsyncIterator[AsyncEngine]:
    """Async engine bound to ``app_rw`` for the snapshot-lifecycle test.

    Skips the fixture (and any test that depends on it) if the env var
    is unset — the per-product smokes above don't require it.
    """

    if _app_smoke_dsn is None:
        pytest.skip(
            f"Set {_APP_SMOKE_DSN_ENV} to a postgres+asyncpg DSN with app_rw "
            "access to run the 17b snapshot-lifecycle live test.",
        )
    engine = create_async_engine(_app_smoke_dsn, pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_snapshot_lifecycle_pin_resolves_via_md_snapshot(  # noqa: PLR0915
    seed_quotes: Any,
    md_rw_engine: AsyncEngine,
    app_rw_engine: AsyncEngine,
    fake_api_key_lookup: ApiKeyLookup,
    fake_firebase_verifier: tuple[Any, dict[str, dict[str, Any]]],
    fake_api_keys: dict[str, ApiKeyRecord],
) -> None:
    """End-to-end snapshot-lifecycle proof:

    1. Seed ``md.canonical_ids`` + ``md.quote_points`` with value ``X``.
    2. Run ``pipeline.build_snapshot`` → ``md.snapshots`` row appears
       with a non-empty ``version_etag``; ``snapshot_quotes`` carries
       value ``X``.
    3. Idempotency proof — re-run for the same ``(name, as_of)``: same
       ``snapshot_id``, ``quotes_changed == 0`` (no content delta), and
       ``version_etag`` advances anyway (STATEMENT-trigger fires
       per DELETE + INSERT, by design).
    4. Mutate ``md.quote_points`` to a distinct value ``Y`` so the
       snapshot-pinned value (``X``) and the live ``quote_points``
       fallback value (``Y``) are observationally distinct downstream.
    5. Insert a one-off ``app.users`` + ``app.snapshots`` row whose
       ``content`` carries ``{"md_version_etag": <etag>}`` — the
       soft-link without a schema migration.
    6. ``POST /v1/price/swap/ir`` with ``snapshot_id`` set → assert the
       echoed ``resolved_quotes`` carries value ``X`` (snapshot path,
       routed through MD's etag-scoped lookup), NOT ``Y`` (which would
       prove the pin leaked back to the live ``quote_points`` path).
    7. Narrow cleanup — every row this test wrote, in reverse
       FK order, regardless of whether the assertions tripped.
    """

    assert _engine_target is not None
    assert _md_base_url is not None

    # Three canonical_ids — minimum to bootstrap a swap_ir curve (one
    # deposit + two swap helpers). A short, distinct test-only namespace
    # keeps these isolated from the per-product smoke vocabulary so
    # parallel runs and operator-loaded seed data don't collide.
    suffix = uuid.uuid4().hex[:8]
    cid_dep = f"USD.IRS.SNAP1Y.{suffix}"
    cid_sw2 = f"USD.IRS.SNAP2Y.{suffix}"
    cid_sw5 = f"USD.IRS.SNAP5Y.{suffix}"
    canonical_ids = [cid_dep, cid_sw2, cid_sw5]
    # Distinct snapshot values per canonical so a misrouted resolution
    # (live quote_points instead of the snapshot) is unmistakable.
    snapshot_values = {cid_dep: 0.0405, cid_sw2: 0.0410, cid_sw5: 0.0415}
    mutated_value = 0.999  # nonsense; must NOT surface if the pin scopes

    target_date = date.fromisoformat(_AS_OF)
    snapshot_name = f"O21_SNAP_LIFECYCLE_{suffix}"

    # Cleanup callbacks accumulate as we go so a mid-test failure still
    # leaves zero residue in either DB. Reverse order on teardown.
    cleanups: list[Any] = []

    api_key = f"o21-snap-{uuid.uuid4().hex[:8]}"
    owner_uid = f"o21-snap-uid-{uuid.uuid4().hex[:8]}"
    app_snapshot_id = uuid.uuid4()
    md_snapshot_id: uuid.UUID | None = None

    try:
        # Step 1 — canonical_ids + quote_points seeded by the existing
        # ``seed_quotes`` fixture (it registers its own cleanup).
        await seed_quotes(
            [(cid, snapshot_values[cid]) for cid in canonical_ids],
            asset_class="rates",
            currency="USD",
        )

        # Step 2 — build the snapshot via the ingester pipeline directly.
        first = await build_snapshot(
            engine=md_rw_engine,
            as_of=target_date,
            snapshot_name=snapshot_name,
        )
        md_snapshot_id = first.snapshot_id

        async def _drop_md_snapshot() -> None:
            assert md_snapshot_id is not None
            async with md_rw_engine.begin() as conn:
                # snapshot_quotes cascade-deletes via md.snapshots FK.
                await conn.execute(
                    text("DELETE FROM md.snapshots WHERE id = :id"),
                    {"id": md_snapshot_id},
                )

        cleanups.append(_drop_md_snapshot)

        assert first.quotes_written >= len(canonical_ids)
        assert first.version_etag_after
        assert len(first.version_etag_after) >= 32, (
            "version_etag must be the 32-char UUID-hex token the schema's "
            "server_default produces; got " + repr(first.version_etag_after)
        )
        assert first.quotes_changed >= len(canonical_ids), (
            "First build (prior empty) must report every seeded row as changed"
        )

        # Step 3 — idempotency proof. Re-running for the same (name, as_of)
        # must reuse the snapshot row and produce the same content, while
        # the trigger fires anyway (one statement per DELETE + INSERT).
        second = await build_snapshot(
            engine=md_rw_engine,
            as_of=target_date,
            snapshot_name=snapshot_name,
        )
        assert second.snapshot_id == first.snapshot_id, (
            "Re-running for the same (name, as_of) must reuse the snapshot row"
        )
        assert second.version_etag_after != first.version_etag_after, (
            "D40 trigger advances version_etag on every rebuild, by design"
        )
        assert second.quotes_changed == 0, (
            "Re-running with unchanged quote_points must report zero content "
            "delta even though version_etag advanced (plan §17b risk #5)"
        )

        pin_etag = second.version_etag_after

        # Step 4 — mutate quote_points to a nonsense value. If the pin
        # scopes correctly, this value MUST NOT surface in the priced
        # response. The snapshot remains frozen because md.snapshot_quotes
        # is a separate table from md.quote_points; only a rebuild of the
        # snapshot (which we don't do here) would propagate the change.
        async with md_rw_engine.begin() as conn:
            await conn.execute(
                text("UPDATE md.quote_points SET value = :v WHERE canonical_id = ANY(:ids)"),
                {"v": mutated_value, "ids": canonical_ids},
            )

        # Step 5 — seed the app-side soft pin. owner_uid is unique to this
        # test so the existing smoke user-space (``o21-smoke-uid``) is
        # untouched. Use the explicit snapshot UUID so cleanup is exact.
        async with app_rw_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO app.users (uid, email, tier) "
                    "VALUES (:uid, :email, 'free') "
                    "ON CONFLICT (uid) DO NOTHING"
                ),
                {"uid": owner_uid, "email": f"{owner_uid}@example.test"},
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO app.snapshots
                      (id, owner_uid, name, as_of, content)
                    VALUES (:id, :uid, :name, :as_of, CAST(:content AS JSONB))
                    """
                ),
                {
                    "id": app_snapshot_id,
                    "uid": owner_uid,
                    "name": f"O21-SNAP-LIFECYCLE-{uuid.uuid4().hex[:6]}",
                    "as_of": target_date,
                    "content": json.dumps({"md_version_etag": pin_etag}),
                },
            )

        async def _drop_app_rows() -> None:
            async with app_rw_engine.begin() as conn:
                # The orchestrator persists every priced call into
                # app.pricing_history under the request's owner_uid;
                # that FK is ON DELETE RESTRICT, so the per-test user
                # can't be removed until its history rows are. Narrow
                # delete by owner_uid keeps other users' history intact.
                await conn.execute(
                    text("DELETE FROM app.pricing_history WHERE owner_uid = :uid"),
                    {"uid": owner_uid},
                )
                await conn.execute(
                    text("DELETE FROM app.snapshots WHERE id = :id"),
                    {"id": app_snapshot_id},
                )
                await conn.execute(
                    text("DELETE FROM app.users WHERE uid = :uid"),
                    {"uid": owner_uid},
                )

        cleanups.append(_drop_app_rows)

        # Register the API key in the in-process fake lookup so the live
        # orchestrator app accepts it under ``owner_uid``.
        fake_api_keys[api_key] = ApiKeyRecord(
            api_key_id=api_key,
            owner_uid=owner_uid,
            name="O21 snapshot-lifecycle key",
            email=f"{owner_uid}@example.test",
            tier="free",
            active=True,
        )

        # Step 6 — build the live-smoke app fresh here (not the module
        # fixture) so the API-key map is seeded BEFORE the app reads it.
        verifier, _ = fake_firebase_verifier
        settings = OrchestratorSettings(
            env=Environment.DEV,
            log_level=LogLevel.WARNING,
            build_sha="testsha",
            engine_grpc_target=_engine_target,
            md_service_url=_md_base_url,
        )
        app: FastAPI = create_app(
            settings,
            api_key_lookup=fake_api_key_lookup,
            firebase_verifier=verifier,
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            payload = {
                "swap": {
                    "notional": 1_000_000.0,
                    "effective_date": "2025-01-17",
                    "termination_date": "2030-01-17",
                },
                "curves": [
                    {
                        "name": "USD-OIS",
                        "currency": "USD",
                        "day_counter": "Actual365Fixed",
                        "reference_date": _AS_OF,
                        "points": [
                            _deposit_point(cid_dep),
                            _swap_point(2, cid_sw2),
                            _swap_point(5, cid_sw5),
                        ],
                    }
                ],
                "as_of": _AS_OF,
                "snapshot_id": str(app_snapshot_id),
            }
            response = client.post(
                "/v1/price/swap/ir",
                json=payload,
                headers={
                    "X-API-Key": api_key,
                    "X-Request-Id": f"o21-snap-lifecycle-{uuid.uuid4()}",
                },
            )
            assert response.status_code == HTTPStatus.OK, response.text
            body = response.json()
            resolved = {
                q["canonical_id"]: q["value"] for q in body["assembled_request"]["resolved_quotes"]
            }
            for cid, expected in snapshot_values.items():
                got = resolved.get(cid)
                assert got == pytest.approx(expected), (
                    f"snapshot-pinned resolve should have returned the "
                    f"snapshot value ({expected}) for {cid}; got {got!r}. "
                    f"mutated_value ({mutated_value}) appearing here would "
                    f"mean the etag pin leaked back to the live quote_points "
                    f"fallback."
                )
            npv = float(body["result"]["npv"])
            assert math.isfinite(npv)

    finally:
        # Step 7 — narrow cleanup, reverse FK order. Fixture
        # teardown handles canonical_ids + quote_points afterwards.
        for cleanup in reversed(cleanups):
            await cleanup()
        fake_api_keys.pop(api_key, None)


# ---------------------------------------------------------------------------
# Scheduler-driven snapshot lifecycle: run_pipeline → soft-pin → priced
# ---------------------------------------------------------------------------


async def test_scheduler_driven_pipeline_resolves_via_md_snapshot(  # noqa: PLR0915
    md_rw_engine: AsyncEngine,
    app_rw_engine: AsyncEngine,
    fake_api_key_lookup: ApiKeyLookup,
    fake_firebase_verifier: tuple[Any, dict[str, dict[str, Any]]],
    fake_api_keys: dict[str, ApiKeyRecord],
) -> None:
    """End-to-end scheduler-driven proof:

    Same shape as the snapshot-lifecycle test, but the canonical_ids land in
    ``md.*`` via ``pipeline.run_pipeline`` (the ofelia scheduled job's
    invocation entrypoint) driving a fixture fetcher — NOT a manual
    ``INSERT`` into ``md.quote_points``. The orchestrator-side priced
    response must echo the freshly-ingested values back, proving:

    1. Scheduled ingest writes propagate through ``md.quote_points``
       to ``md.snapshot_quotes`` via ``build_snapshot``.
    2. The orchestrator's ``snapshot_version`` wire
       scopes the MD read to the etag the scheduler just produced.
    3. The full chain — scheduler → md.* → orchestrator → engine —
       round-trips without manual seed at any step.

    Fixture fetcher (not live FRED) so the test never burns vendor
    quota; the ``ofelia`` scheduler in compose will invoke the live
    connector when the operator brings up the ``scheduled`` profile.
    """

    assert _engine_target is not None
    assert _md_base_url is not None

    suffix = uuid.uuid4().hex[:8]
    cid_dep = f"USD.IRS.SCHED1Y.{suffix}"
    cid_sw2 = f"USD.IRS.SCHED2Y.{suffix}"
    cid_sw5 = f"USD.IRS.SCHED5Y.{suffix}"
    canonical_ids = [cid_dep, cid_sw2, cid_sw5]
    fetched_values = {cid_dep: 0.0438, cid_sw2: 0.0442, cid_sw5: 0.0447}

    target_date = date.fromisoformat(_AS_OF)
    target_dt = datetime(target_date.year, target_date.month, target_date.day, tzinfo=UTC)
    snapshot_name = f"D135_SCHED_LIFECYCLE_{suffix}"

    cleanups: list[Any] = []
    api_key = f"o21-sched-{uuid.uuid4().hex[:8]}"
    owner_uid = f"o21-sched-uid-{uuid.uuid4().hex[:8]}"
    app_snapshot_id = uuid.uuid4()
    md_snapshot_id: uuid.UUID | None = None

    def _record(canonical_id: str, value: float) -> QuoteRecord:
        return QuoteRecord(
            canonical_id=canonical_id,
            as_of=target_dt,
            value=value,
            source="FRED",
            vendor_id=f"SCHED-{canonical_id.rsplit('.', 1)[-1]}",
            quality_flags={},
            meta={
                "series_id": "SCHED",
                "vendor_unit": "decimal_rate",
                "normalized_unit": "decimal_rate",
                "raw_value_percent": value * 100.0,
                "tenor": canonical_id.rsplit(".", 1)[-1].replace("SCHED", ""),
            },
        )

    async def _scheduler_fixture_fetcher(
        sources: set[str],
        as_of: date,
        start: date,
    ) -> list[QuoteRecord]:
        _ = (sources, as_of, start)
        return [_record(cid, val) for cid, val in fetched_values.items()]

    try:
        # Step 1 — scheduler-style invocation: run_pipeline with the
        # fixture fetcher. This is what the ofelia ``ingest`` + 23:00
        # ``build-snapshot`` jobs collectively produce; run-pipeline
        # exercises the same code path in one call.
        result = await run_pipeline(
            engine=md_rw_engine,
            as_of=target_date,
            sources={"fred"},
            snapshot_name=snapshot_name,
            fetcher=_scheduler_fixture_fetcher,
        )
        md_snapshot_id = result.snapshot.snapshot_id

        async def _drop_md_rows() -> None:
            assert md_snapshot_id is not None
            async with md_rw_engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM md.snapshots WHERE id = :id"),
                    {"id": md_snapshot_id},
                )
                await conn.execute(
                    text("DELETE FROM md.quote_points WHERE canonical_id = ANY(:ids)"),
                    {"ids": canonical_ids},
                )
                await conn.execute(
                    text("DELETE FROM md.vendor_mappings WHERE canonical_id = ANY(:ids)"),
                    {"ids": canonical_ids},
                )
                await conn.execute(
                    text("DELETE FROM md.canonical_ids WHERE canonical_id = ANY(:ids)"),
                    {"ids": canonical_ids},
                )

        cleanups.append(_drop_md_rows)

        assert result.ingest.upserted == len(fetched_values)
        assert result.snapshot.version_etag_after
        assert len(result.snapshot.version_etag_after) >= 32

        pin_etag = result.snapshot.version_etag_after

        # Step 2 — soft-pin via app.snapshots.content.md_version_etag,
        # exactly mirroring the snapshot-lifecycle test.
        async with app_rw_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO app.users (uid, email, tier) "
                    "VALUES (:uid, :email, 'free') "
                    "ON CONFLICT (uid) DO NOTHING"
                ),
                {"uid": owner_uid, "email": f"{owner_uid}@example.test"},
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO app.snapshots
                      (id, owner_uid, name, as_of, content)
                    VALUES (:id, :uid, :name, :as_of, CAST(:content AS JSONB))
                    """
                ),
                {
                    "id": app_snapshot_id,
                    "uid": owner_uid,
                    "name": f"D135-SCHED-LIFECYCLE-{uuid.uuid4().hex[:6]}",
                    "as_of": target_date,
                    "content": json.dumps({"md_version_etag": pin_etag}),
                },
            )

        async def _drop_app_rows() -> None:
            async with app_rw_engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM app.pricing_history WHERE owner_uid = :uid"),
                    {"uid": owner_uid},
                )
                await conn.execute(
                    text("DELETE FROM app.snapshots WHERE id = :id"),
                    {"id": app_snapshot_id},
                )
                await conn.execute(
                    text("DELETE FROM app.users WHERE uid = :uid"),
                    {"uid": owner_uid},
                )

        cleanups.append(_drop_app_rows)

        # Step 3 — register API key and bring up the live-smoke app.
        fake_api_keys[api_key] = ApiKeyRecord(
            api_key_id=api_key,
            owner_uid=owner_uid,
            name="D135 scheduler-driven key",
            email=f"{owner_uid}@example.test",
            tier="free",
            active=True,
        )
        verifier, _ = fake_firebase_verifier
        settings = OrchestratorSettings(
            env=Environment.DEV,
            log_level=LogLevel.WARNING,
            build_sha="testsha",
            engine_grpc_target=_engine_target,
            md_service_url=_md_base_url,
        )
        app: FastAPI = create_app(
            settings,
            api_key_lookup=fake_api_key_lookup,
            firebase_verifier=verifier,
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            payload = {
                "swap": {
                    "notional": 1_000_000.0,
                    "effective_date": "2025-01-17",
                    "termination_date": "2030-01-17",
                },
                "curves": [
                    {
                        "name": "USD-OIS",
                        "currency": "USD",
                        "day_counter": "Actual365Fixed",
                        "reference_date": _AS_OF,
                        "points": [
                            _deposit_point(cid_dep),
                            _swap_point(2, cid_sw2),
                            _swap_point(5, cid_sw5),
                        ],
                    }
                ],
                "as_of": _AS_OF,
                "snapshot_id": str(app_snapshot_id),
            }
            response = client.post(
                "/v1/price/swap/ir",
                json=payload,
                headers={
                    "X-API-Key": api_key,
                    "X-Request-Id": f"d135-sched-{uuid.uuid4()}",
                },
            )
            assert response.status_code == HTTPStatus.OK, response.text
            body = response.json()
            resolved = {
                q["canonical_id"]: q["value"] for q in body["assembled_request"]["resolved_quotes"]
            }
            # The freshly-ingested values (from the fixture fetcher,
            # not a manual seed) reach the engine via the snapshot
            # etag scope.
            for cid, expected in fetched_values.items():
                got = resolved.get(cid)
                assert got == pytest.approx(expected), (
                    f"scheduler-driven snapshot pin should have resolved "
                    f"{cid}={expected}; got {got!r}. The MD path may have "
                    f"dropped the row or the etag scope leaked back to "
                    f"the live quote_points fallback."
                )
            npv = float(body["result"]["npv"])
            assert math.isfinite(npv)

    finally:
        for cleanup in reversed(cleanups):
            await cleanup()
        fake_api_keys.pop(api_key, None)
