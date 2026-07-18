"""Unit tests for ``pricing/equity_options/assembler.py``.

The equity-options assembler walks three distinct market-data
families in one pass: ``app.curves`` for discount + dividend
(refinement same-bundle-type / different-role with the
role pinned in ``details``), ``app.vol_surfaces`` for the equity
Black-vol surface (``kind = 'BlackVolSpec'`` invariant), and the
underlier spot reference (inline literal or canonical-id pin).
These tests pin the contracts the 10-plan calls out:

* Inline mode (``equity_option=...``) skips ``app.equity_options``
  entirely.
* By-reference mode (``equity_option_id=...``) hits
  ``app.equity_options`` once with the right WHERE clause
  (``owner_uid`` + ``deleted_at IS NULL``).
* Soft-deleted / cross-tenant / missing rows surface as
  ``equity_option_not_found`` (404).
* ``request.curves`` / ``request.vol_surface`` / ``request.spot``
  overrides win over the saved option's pricing block.
* Per-bundle-stage 404 codes:
  ``equity_option_discount_curve_not_found`` (refinement —
  one code with role in ``details``) vs
  ``equity_option_vol_surface_not_found`` vs
  ``equity_option_snapshot_not_found``.
* Per-bundle-stage 422 codes:
  ``equity_option_curve_resolution_failed`` /
  ``equity_option_surface_resolution_failed`` /
  ``equity_option_spot_resolution_failed``.
* ``BlackVolSpec`` invariant — referenced surface with kind
  ``SwaptionVolSpec`` raises
  ``equity_option_vol_surface_wrong_kind`` (422).
* batching hook: assembler output carries ``equity_surface_id`` /
  ``discount_curve_id`` / ``dividend_curve_id`` so the route
  handler can pack them into ``shared_inputs``.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_orchestrator.pricing.equity_options.assembler import (
    AssemblerOutput,
    assemble,
)
from quantra_orchestrator.pricing.equity_options.errors import (
    EquityOptionCurveResolutionFailedError,
    EquityOptionDiscountCurveNotFoundError,
    EquityOptionInvalidRequestError,
    EquityOptionNotFoundError,
    EquityOptionSnapshotNotFoundError,
    EquityOptionSpotResolutionFailedError,
    EquityOptionSurfaceResolutionFailedError,
    EquityOptionVolSurfaceNotFoundError,
    EquityOptionVolSurfaceWrongKindError,
)
from quantra_orchestrator.pricing.equity_options.models import (
    CurveRef,
    EquityOptionPriceRequest,
    SpotQuoteRef,
    VolSurfaceRef,
)

from .conftest import FakeEngine

OWNER = "user-test"


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------


def _curve_row(
    *,
    curve_id: uuid.UUID,
    name: str = "USD-OIS",
    quote_id: str = "USD.IRS.1Y",
) -> dict[str, Any]:
    return {
        "id": str(curve_id),
        "name": name,
        "currency": "USD",
        "day_counter": "Actual/365",
        "helper_kind": "Discount",
        "reference_date": date(2026, 5, 13),
        "points": [{"tenor": "1Y", "quote_id": quote_id}],
        "body": {"interpolator": "LogLinear"},
    }


def _vol_surface_row(
    *,
    vol_surface_id: uuid.UUID,
    name: str = "AAPL-vol",
    kind: str = "BlackVolSpec",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": str(vol_surface_id),
        "name": name,
        "kind": kind,
        "payload": (payload if payload is not None else {"base": {"constant_vol": 0.25}}),
    }


def _equity_option_row(
    *,
    equity_option_id: uuid.UUID,
    request_json: dict[str, Any],
    name: str = "demo-eq",
) -> dict[str, Any]:
    return {
        "id": str(equity_option_id),
        "name": name,
        "request": request_json,
    }


def _snapshot_row(*, snapshot_id: uuid.UUID, content: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(snapshot_id), "name": "EOD-AAPL", "content": content}


def _ro(engine: FakeEngine) -> AsyncEngine:
    return cast(AsyncEngine, engine)


def _inline_request(
    *,
    curves: list[CurveRef] | None = None,
    vol_surface: VolSurfaceRef | None = None,
    spot: SpotQuoteRef | None = None,
    snapshot_id: uuid.UUID | None = None,
) -> EquityOptionPriceRequest:
    """Build a minimal inline ``EquityOptionPriceRequest`` for tests."""

    return EquityOptionPriceRequest(
        equity_option_id=None,
        equity_option={"option": {"option_type": "Call", "strike": 100.0}},
        curves=curves
        if curves is not None
        else [
            CurveRef(id=None, name="d", role="discount", points=[{"rate": 0.04}]),
            CurveRef(id=None, name="q", role="dividend", points=[{"rate": 0.01}]),
        ],
        vol_surface=vol_surface
        if vol_surface is not None
        else VolSurfaceRef(
            id=None,
            kind="BlackVolSpec",
            payload={"base": {"constant_vol": 0.20}},
        ),
        spot=spot if spot is not None else SpotQuoteRef(value=100.0),
        as_of=date(2026, 5, 13),
        snapshot_id=snapshot_id,
    )


# ---------------------------------------------------------------------------
# Inline + by-reference: trade loading
# ---------------------------------------------------------------------------


async def test_inline_does_not_touch_equity_options_table(
    fake_ro_engine: FakeEngine,
) -> None:
    """Inline mode skips ``app.equity_options``; no SQL hits the table."""

    fake_ro_engine.set_handler(lambda _sql, _params: [])

    request = _inline_request()
    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    assert isinstance(out, AssemblerOutput)
    assert out.trade.equity_option_id is None
    assert out.discount_curve.role == "discount"
    assert out.dividend_curve.role == "dividend"
    assert out.equity_surface_id is None
    assert out.spot.value == 100.0
    assert all("FROM app.equity_options" not in rec.sql for rec in fake_ro_engine.recordings)


async def test_by_reference_loads_equity_option_and_curves_and_surface(
    fake_ro_engine: FakeEngine,
) -> None:
    eq_id = uuid.uuid4()
    discount_id = uuid.uuid4()
    dividend_id = uuid.uuid4()
    surface_id = uuid.uuid4()
    saved = _equity_option_row(
        equity_option_id=eq_id,
        request_json={
            "pricing": {
                "curves": [
                    {"id": str(discount_id), "role": "discount"},
                    {"id": str(dividend_id), "role": "dividend"},
                ],
                "vol_surface_id": str(surface_id),
                "spot": {"value": 152.30},
            },
            "option": {"option_type": "Call", "strike": 150.0},
        },
    )

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.equity_options" in sql:
            return [saved]
        if "FROM app.curves" in sql:
            requested = uuid.UUID(params["curve_id"])
            if requested == discount_id:
                return [_curve_row(curve_id=discount_id, name="discount")]
            if requested == dividend_id:
                return [_curve_row(curve_id=dividend_id, name="dividend")]
            return []
        if "FROM app.vol_surfaces" in sql:
            return [_vol_surface_row(vol_surface_id=surface_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    out = await assemble(
        EquityOptionPriceRequest(
            equity_option_id=eq_id,
            as_of=date(2026, 5, 13),
        ),
        owner_uid=OWNER,
        ro_engine=_ro(fake_ro_engine),
    )

    assert out.trade.equity_option_id == eq_id
    assert out.discount_curve_id == discount_id
    assert out.dividend_curve_id == dividend_id
    assert out.equity_surface_id == surface_id
    assert out.spot.value == 152.30
    eq_sqls = [rec for rec in fake_ro_engine.recordings if "FROM app.equity_options" in rec.sql]
    assert len(eq_sqls) == 1
    assert "owner_uid" in eq_sqls[0].sql
    assert "deleted_at IS NULL" in eq_sqls[0].sql
    assert eq_sqls[0].params["owner_uid"] == OWNER


async def test_equity_option_id_missing_returns_not_found(
    fake_ro_engine: FakeEngine,
) -> None:
    fake_ro_engine.set_handler(lambda _sql, _params: [])

    request = EquityOptionPriceRequest(
        equity_option_id=uuid.uuid4(),
        as_of=date(2026, 5, 13),
    )

    with pytest.raises(EquityOptionNotFoundError) as excinfo:
        await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert excinfo.value.code == "equity_option_not_found"
    assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# Curve family — discount + dividend (refinement)
# ---------------------------------------------------------------------------


async def test_request_curves_override_skip_saved_pricing(
    fake_ro_engine: FakeEngine,
) -> None:
    """Request-side ``curves`` win over ``pricing.curves`` in the saved row."""

    eq_id = uuid.uuid4()
    discount_id = uuid.uuid4()
    dividend_id = uuid.uuid4()
    surface_id = uuid.uuid4()
    saved = _equity_option_row(
        equity_option_id=eq_id,
        request_json={
            "pricing": {
                "curves": [{"id": str(uuid.uuid4()), "role": "discount"}],
                "vol_surface_id": str(surface_id),
                "spot": {"value": 152.30},
            }
        },
    )

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.equity_options" in sql:
            return [saved]
        if "FROM app.curves" in sql:
            requested = uuid.UUID(params["curve_id"])
            if requested == discount_id:
                return [_curve_row(curve_id=discount_id, name="override-d")]
            if requested == dividend_id:
                return [_curve_row(curve_id=dividend_id, name="override-q")]
            return []
        if "FROM app.vol_surfaces" in sql:
            return [_vol_surface_row(vol_surface_id=surface_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = EquityOptionPriceRequest(
        equity_option_id=eq_id,
        curves=[
            CurveRef(id=discount_id, role="discount"),
            CurveRef(id=dividend_id, role="dividend"),
        ],
        as_of=date(2026, 5, 13),
    )
    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    assert out.discount_curve.name == "override-d"
    assert out.dividend_curve.name == "override-q"


async def test_discount_curve_missing_returns_404_with_role_detail(
    fake_ro_engine: FakeEngine,
) -> None:
    """Missing discount curve → 404 with ``role=discount`` in ``details``."""

    eq_id = uuid.uuid4()
    saved = _equity_option_row(
        equity_option_id=eq_id,
        request_json={
            "pricing": {
                "curves": [
                    {"id": str(uuid.uuid4()), "role": "discount"},
                    {"id": str(uuid.uuid4()), "role": "dividend"},
                ],
                "vol_surface_id": str(uuid.uuid4()),
                "spot": {"value": 152.30},
            }
        },
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.equity_options" in sql:
            return [saved]
        if "FROM app.curves" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    with pytest.raises(EquityOptionDiscountCurveNotFoundError) as excinfo:
        await assemble(
            EquityOptionPriceRequest(equity_option_id=eq_id, as_of=date(2026, 5, 13)),
            owner_uid=OWNER,
            ro_engine=_ro(fake_ro_engine),
        )
    err = excinfo.value
    assert err.code == "equity_option_discount_curve_not_found"
    assert err.status_code == 404
    assert err.details is not None
    assert err.details[0]["role"] == "discount"


async def test_missing_pricing_block_raises_curve_resolution_failed(
    fake_ro_engine: FakeEngine,
) -> None:
    """No ``pricing`` block on a saved option → 422 curve_resolution_failed."""

    eq_id = uuid.uuid4()
    saved = _equity_option_row(
        equity_option_id=eq_id,
        request_json={"option": {"option_type": "Call", "strike": 100.0}},
    )
    fake_ro_engine.set_handler(lambda _sql, _params: [saved])

    with pytest.raises(EquityOptionCurveResolutionFailedError) as excinfo:
        await assemble(
            EquityOptionPriceRequest(equity_option_id=eq_id, as_of=date(2026, 5, 13)),
            owner_uid=OWNER,
            ro_engine=_ro(fake_ro_engine),
        )
    err = excinfo.value
    assert err.code == "equity_option_curve_resolution_failed"
    assert err.status_code == 422
    assert err.details is not None
    assert err.details[0]["role"] == "discount"


async def test_dividend_curve_synthesised_when_only_discount_supplied(
    fake_ro_engine: FakeEngine,
) -> None:
    """Single-curve request → dividend role gets a flat-zero placeholder."""

    discount_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=discount_id)]
        return []

    fake_ro_engine.set_handler(handler)

    request = EquityOptionPriceRequest(
        equity_option_id=None,
        equity_option={"option": {"strike": 100.0}},
        curves=[CurveRef(id=discount_id, role="discount")],
        vol_surface=VolSurfaceRef(
            id=None,
            kind="BlackVolSpec",
            payload={"base": {"constant_vol": 0.20}},
        ),
        spot=SpotQuoteRef(value=100.0),
        as_of=date(2026, 5, 13),
    )
    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert out.discount_curve.id == discount_id
    assert out.dividend_curve.id is None
    assert out.dividend_curve.role == "dividend"
    assert out.dividend_curve_id is None


# ---------------------------------------------------------------------------
# Vol-surface family — BlackVolSpec invariant
# ---------------------------------------------------------------------------


async def test_vol_surface_missing_returns_404(
    fake_ro_engine: FakeEngine,
) -> None:
    eq_id = uuid.uuid4()
    discount_id = uuid.uuid4()
    surface_id = uuid.uuid4()
    saved = _equity_option_row(
        equity_option_id=eq_id,
        request_json={
            "pricing": {
                "curves": [{"id": str(discount_id), "role": "discount"}],
                "vol_surface_id": str(surface_id),
                "spot": {"value": 152.30},
            }
        },
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.equity_options" in sql:
            return [saved]
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=discount_id)]
        if "FROM app.vol_surfaces" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    with pytest.raises(EquityOptionVolSurfaceNotFoundError) as excinfo:
        await assemble(
            EquityOptionPriceRequest(equity_option_id=eq_id, as_of=date(2026, 5, 13)),
            owner_uid=OWNER,
            ro_engine=_ro(fake_ro_engine),
        )
    assert excinfo.value.code == "equity_option_vol_surface_not_found"


async def test_referenced_surface_with_swaption_kind_raises_wrong_kind(
    fake_ro_engine: FakeEngine,
) -> None:
    """A ``SwaptionVolSpec`` row referenced from an equity option is rejected.

    The orchestrator refuses rather than letting the engine emit
    a confusing bootstrap error for a rates surface flowing into
    the BlackVol-only equity pricer.
    """

    eq_id = uuid.uuid4()
    discount_id = uuid.uuid4()
    surface_id = uuid.uuid4()
    saved = _equity_option_row(
        equity_option_id=eq_id,
        request_json={
            "pricing": {
                "curves": [{"id": str(discount_id), "role": "discount"}],
                "vol_surface_id": str(surface_id),
                "spot": {"value": 152.30},
            }
        },
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.equity_options" in sql:
            return [saved]
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=discount_id)]
        if "FROM app.vol_surfaces" in sql:
            return [_vol_surface_row(vol_surface_id=surface_id, kind="SwaptionVolSpec")]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    with pytest.raises(EquityOptionVolSurfaceWrongKindError) as excinfo:
        await assemble(
            EquityOptionPriceRequest(equity_option_id=eq_id, as_of=date(2026, 5, 13)),
            owner_uid=OWNER,
            ro_engine=_ro(fake_ro_engine),
        )
    err = excinfo.value
    assert err.code == "equity_option_vol_surface_wrong_kind"
    assert err.status_code == 422
    assert err.details is not None
    assert err.details[0]["actual_kind"] == "SwaptionVolSpec"
    assert err.details[0]["expected_kind"] == "BlackVolSpec"


async def test_inline_surface_with_wrong_kind_is_invalid_request(
    fake_ro_engine: FakeEngine,
) -> None:
    """Inline surface with kind != BlackVolSpec → 400 invalid_request."""

    fake_ro_engine.set_handler(lambda _sql, _params: [])

    request = EquityOptionPriceRequest(
        equity_option_id=None,
        equity_option={"option": {"strike": 100.0}},
        curves=[
            CurveRef(id=None, name="d", role="discount", points=[{"rate": 0.04}]),
        ],
        vol_surface=VolSurfaceRef(
            id=None,
            kind="OptionletVolSpec",
            payload={"base": {"constant_vol": 0.20}},
        ),
        spot=SpotQuoteRef(value=100.0),
        as_of=date(2026, 5, 13),
    )

    with pytest.raises(EquityOptionInvalidRequestError) as excinfo:
        await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    err = excinfo.value
    assert err.code == "equity_option_invalid_request"
    assert err.status_code == 400


async def test_missing_surface_id_and_inline_list_raises_surface_resolution_failed(
    fake_ro_engine: FakeEngine,
) -> None:
    eq_id = uuid.uuid4()
    discount_id = uuid.uuid4()
    saved = _equity_option_row(
        equity_option_id=eq_id,
        request_json={
            "pricing": {
                "curves": [{"id": str(discount_id), "role": "discount"}],
                "spot": {"value": 152.30},
            }
        },
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.equity_options" in sql:
            return [saved]
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=discount_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    with pytest.raises(EquityOptionSurfaceResolutionFailedError) as excinfo:
        await assemble(
            EquityOptionPriceRequest(equity_option_id=eq_id, as_of=date(2026, 5, 13)),
            owner_uid=OWNER,
            ro_engine=_ro(fake_ro_engine),
        )
    assert excinfo.value.code == "equity_option_surface_resolution_failed"


# ---------------------------------------------------------------------------
# Spot family
# ---------------------------------------------------------------------------


async def test_request_spot_overrides_saved_spot(
    fake_ro_engine: FakeEngine,
) -> None:
    eq_id = uuid.uuid4()
    discount_id = uuid.uuid4()
    surface_id = uuid.uuid4()
    saved = _equity_option_row(
        equity_option_id=eq_id,
        request_json={
            "pricing": {
                "curves": [{"id": str(discount_id), "role": "discount"}],
                "vol_surface_id": str(surface_id),
                "spot": {"value": 100.0},
            }
        },
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.equity_options" in sql:
            return [saved]
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=discount_id)]
        if "FROM app.vol_surfaces" in sql:
            return [_vol_surface_row(vol_surface_id=surface_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = EquityOptionPriceRequest(
        equity_option_id=eq_id,
        spot=SpotQuoteRef(canonical_id="AAPL.SPOT", value=152.30),
        as_of=date(2026, 5, 13),
    )
    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert out.spot.canonical_id == "AAPL.SPOT"
    assert out.spot.value == 152.30


async def test_missing_spot_raises_spot_resolution_failed(
    fake_ro_engine: FakeEngine,
) -> None:
    eq_id = uuid.uuid4()
    discount_id = uuid.uuid4()
    surface_id = uuid.uuid4()
    saved = _equity_option_row(
        equity_option_id=eq_id,
        request_json={
            "pricing": {
                "curves": [{"id": str(discount_id), "role": "discount"}],
                "vol_surface_id": str(surface_id),
            }
        },
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.equity_options" in sql:
            return [saved]
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=discount_id)]
        if "FROM app.vol_surfaces" in sql:
            return [_vol_surface_row(vol_surface_id=surface_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    with pytest.raises(EquityOptionSpotResolutionFailedError) as excinfo:
        await assemble(
            EquityOptionPriceRequest(equity_option_id=eq_id, as_of=date(2026, 5, 13)),
            owner_uid=OWNER,
            ro_engine=_ro(fake_ro_engine),
        )
    assert excinfo.value.code == "equity_option_spot_resolution_failed"


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


async def test_snapshot_id_loads_pin_map(fake_ro_engine: FakeEngine) -> None:
    snapshot_id = uuid.uuid4()
    discount_id = uuid.uuid4()
    surface_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=discount_id)]
        if "FROM app.vol_surfaces" in sql:
            return [_vol_surface_row(vol_surface_id=surface_id)]
        if "FROM app.snapshots" in sql:
            return [
                _snapshot_row(
                    snapshot_id=snapshot_id,
                    content={
                        "USD.IRS.1Y": 4.25,
                        "AAPL.SPOT": {"value": 150.0, "source": "vendor_x"},
                    },
                )
            ]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = EquityOptionPriceRequest(
        equity_option_id=None,
        equity_option={"option": {"strike": 150.0}},
        curves=[CurveRef(id=discount_id, role="discount")],
        vol_surface=VolSurfaceRef(id=surface_id),
        spot=SpotQuoteRef(canonical_id="AAPL.SPOT"),
        as_of=date(2026, 5, 13),
        snapshot_id=snapshot_id,
    )
    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert out.snapshot is not None
    assert out.snapshot.id == snapshot_id
    assert out.snapshot.pins["USD.IRS.1Y"]["value"] == 4.25
    assert out.snapshot.pins["AAPL.SPOT"]["value"] == 150.0
    assert out.snapshot.pins["AAPL.SPOT"]["source"] == "vendor_x"


async def test_missing_snapshot_returns_snapshot_not_found(
    fake_ro_engine: FakeEngine,
) -> None:
    snapshot_id = uuid.uuid4()
    discount_id = uuid.uuid4()
    surface_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=discount_id)]
        if "FROM app.vol_surfaces" in sql:
            return [_vol_surface_row(vol_surface_id=surface_id)]
        if "FROM app.snapshots" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = EquityOptionPriceRequest(
        equity_option_id=None,
        equity_option={"option": {"strike": 100.0}},
        curves=[CurveRef(id=discount_id, role="discount")],
        vol_surface=VolSurfaceRef(id=surface_id),
        spot=SpotQuoteRef(value=100.0),
        as_of=date(2026, 5, 13),
        snapshot_id=snapshot_id,
    )
    with pytest.raises(EquityOptionSnapshotNotFoundError) as excinfo:
        await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert excinfo.value.code == "equity_option_snapshot_not_found"
