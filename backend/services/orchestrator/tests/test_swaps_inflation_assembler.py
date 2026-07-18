"""Unit tests for ``pricing/swaps_inflation/assembler.py``.

The inflation-swap assembler walks three distinct market-data
families in one pass: ``app.curves`` for the nominal discount and
the inflation curve (refinement same-bundle-type /
different-role with the role pinned in ``details``),
``app.indices`` for the inflation index (``kind = 'Inflation'``
invariant), and ``app.snapshots`` for the optional MD pin.

These tests pin the contracts the 11-plan calls out:

* Inline mode (``swap=...`` plus ``curves`` + ``inflation_index``)
  skips ``app.swaps_inflation`` entirely.
* By-reference mode (``swap_id=...``) hits ``app.swaps_inflation``
  once with the right WHERE clause (``owner_uid`` +
  ``deleted_at IS NULL``).
* Soft-deleted / cross-tenant / missing rows surface as
  ``swap_inflation_not_found`` (404).
* ``request.curves`` / ``request.inflation_index`` overrides win
  over the saved swap's pricing block.
* Per-bundle-stage 404 codes:
  ``swap_inflation_nominal_curve_not_found`` (refinement —
  one **per role** since the role is structurally distinct
  from the inflation curve) vs
  ``swap_inflation_inflation_curve_not_found`` vs
  ``swap_inflation_index_not_found``.
* Per-bundle-stage 422 codes:
  ``swap_inflation_curve_resolution_failed`` (curves family —
  ONE shared code with role in ``details`` refinement /
  the plan's hard constraint) vs
  ``swap_inflation_index_resolution_failed`` (index is its own
  bundle).
* ``Inflation`` invariant — referenced index with kind
  ``Rates`` raises ``swap_inflation_index_not_found`` with the
  actual_kind + expected_kind in ``details``.
* ``swap_kind`` discriminator surfaces from the request override
  / the trade body / the engine-canonical nested shape /
  defaults to ``"zero_coupon"``.
* batching hook: assembler output carries ``nominal_curve_id`` /
  ``inflation_curve_id`` / ``inflation_index_id`` so the route
  handler can pack them into ``shared_inputs``.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_orchestrator.pricing.swaps_inflation.assembler import (
    AssemblerOutput,
    assemble,
)
from quantra_orchestrator.pricing.swaps_inflation.errors import (
    SwapInflationCurveResolutionFailedError,
    SwapInflationIndexNotFoundError,
    SwapInflationIndexResolutionFailedError,
    SwapInflationInflationCurveNotFoundError,
    SwapInflationNominalCurveNotFoundError,
    SwapInflationNotFoundError,
    SwapInflationSnapshotNotFoundError,
)
from quantra_orchestrator.pricing.swaps_inflation.models import (
    CurveRef,
    InflationIndexRef,
    InflationSwapPriceRequest,
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
        "reference_date": date(2025, 1, 15),
        "points": [{"tenor": "1Y", "quote_id": quote_id}],
        "body": {"interpolator": "LogLinear"},
    }


def _index_row(
    *,
    index_id: uuid.UUID,
    name: str = "EU HICP",
    kind: str = "Inflation",
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": str(index_id),
        "name": name,
        "kind": kind,
        "currency": "EUR",
        "day_counter": "Actual/365",
        "body": body if body is not None else {"id": "EUHICP"},
    }


def _swap_row(
    *,
    swap_id: uuid.UUID,
    request_json: dict[str, Any],
    name: str = "demo-inf",
) -> dict[str, Any]:
    return {
        "id": str(swap_id),
        "name": name,
        "request": request_json,
    }


def _snapshot_row(*, snapshot_id: uuid.UUID, content: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(snapshot_id), "name": "EOD-EUR", "content": content}


def _ro(engine: FakeEngine) -> AsyncEngine:
    return cast(AsyncEngine, engine)


def _inline_request(
    *,
    curves: list[CurveRef] | None = None,
    inflation_index: InflationIndexRef | None = None,
    snapshot_id: uuid.UUID | None = None,
    swap_kind: str | None = None,
) -> InflationSwapPriceRequest:
    """Build a minimal inline ``InflationSwapPriceRequest`` for tests."""

    return InflationSwapPriceRequest(
        swap_id=None,
        swap={
            "swap_kind": swap_kind or "zero_coupon",
            "swaps": [{"zero_coupon_inflation_swap": {"fixed_rate": 0.02}}],
        },
        swap_kind=swap_kind,
        curves=curves
        if curves is not None
        else [
            CurveRef(
                id=None,
                name="DISC",
                role="nominal",
                points=[{"rate": 0.03}],
            ),
            CurveRef(
                id=None,
                name="HICP_ZC",
                role="inflation",
                points=[{"rate": 0.02}],
            ),
        ],
        inflation_index=inflation_index
        if inflation_index is not None
        else InflationIndexRef(id=None, name="EU HICP", index_id="EUHICP", currency="EUR"),
        as_of=date(2025, 1, 15),
        snapshot_id=snapshot_id,
    )


# ---------------------------------------------------------------------------
# Inline + by-reference: trade loading
# ---------------------------------------------------------------------------


async def test_inline_does_not_touch_swaps_inflation_table(
    fake_ro_engine: FakeEngine,
) -> None:
    """Inline mode skips ``app.swaps_inflation``; no SQL hits the table."""

    fake_ro_engine.set_handler(lambda _sql, _params: [])

    request = _inline_request()
    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    assert isinstance(out, AssemblerOutput)
    assert out.trade.swap_id is None
    assert out.nominal_curve.role == "nominal"
    assert out.inflation_curve.role == "inflation"
    assert out.nominal_curve_id is None
    assert out.inflation_curve_id is None
    assert out.inflation_index_id is None
    assert out.inflation_index.index_id == "EUHICP"
    assert out.swap_kind == "zero_coupon"
    assert all("FROM app.swaps_inflation" not in rec.sql for rec in fake_ro_engine.recordings)


async def test_by_reference_loads_swap_curves_and_index(
    fake_ro_engine: FakeEngine,
) -> None:
    swap_id = uuid.uuid4()
    nominal_id = uuid.uuid4()
    inflation_id = uuid.uuid4()
    index_id = uuid.uuid4()
    saved = _swap_row(
        swap_id=swap_id,
        request_json={
            "pricing": {
                "curves": [
                    {"id": str(nominal_id), "role": "nominal"},
                    {"id": str(inflation_id), "role": "inflation"},
                ],
                "inflation_index": {"id": str(index_id)},
            },
            "swap_kind": "year_on_year",
        },
    )

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.swaps_inflation" in sql:
            return [saved]
        if "FROM app.curves" in sql:
            requested = uuid.UUID(params["curve_id"])
            if requested == nominal_id:
                return [_curve_row(curve_id=nominal_id, name="DISC")]
            if requested == inflation_id:
                return [_curve_row(curve_id=inflation_id, name="HICP_YY")]
            return []
        if "FROM app.indices" in sql:
            return [_index_row(index_id=index_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    out = await assemble(
        InflationSwapPriceRequest(swap_id=swap_id, as_of=date(2025, 1, 15)),
        owner_uid=OWNER,
        ro_engine=_ro(fake_ro_engine),
    )

    assert out.trade.swap_id == swap_id
    assert out.nominal_curve_id == nominal_id
    assert out.inflation_curve_id == inflation_id
    assert out.inflation_index_id == index_id
    assert out.swap_kind == "year_on_year"
    swap_sqls = [rec for rec in fake_ro_engine.recordings if "FROM app.swaps_inflation" in rec.sql]
    assert len(swap_sqls) == 1
    assert "owner_uid" in swap_sqls[0].sql
    assert "deleted_at IS NULL" in swap_sqls[0].sql
    assert swap_sqls[0].params["owner_uid"] == OWNER


async def test_swap_id_missing_returns_not_found(
    fake_ro_engine: FakeEngine,
) -> None:
    fake_ro_engine.set_handler(lambda _sql, _params: [])

    request = InflationSwapPriceRequest(swap_id=uuid.uuid4(), as_of=date(2025, 1, 15))

    with pytest.raises(SwapInflationNotFoundError) as excinfo:
        await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert excinfo.value.code == "swap_inflation_not_found"
    assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# Curve family — nominal + inflation (refinement)
# ---------------------------------------------------------------------------


async def test_request_curves_override_skip_saved_pricing(
    fake_ro_engine: FakeEngine,
) -> None:
    """Request-side ``curves`` win over ``pricing.curves`` in the saved row."""

    swap_id = uuid.uuid4()
    nominal_id = uuid.uuid4()
    inflation_id = uuid.uuid4()
    index_id = uuid.uuid4()
    saved = _swap_row(
        swap_id=swap_id,
        request_json={
            "pricing": {
                "curves": [{"id": str(uuid.uuid4()), "role": "nominal"}],
                "inflation_index": {"id": str(index_id)},
            },
        },
    )

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.swaps_inflation" in sql:
            return [saved]
        if "FROM app.curves" in sql:
            requested = uuid.UUID(params["curve_id"])
            if requested == nominal_id:
                return [_curve_row(curve_id=nominal_id, name="override-N")]
            if requested == inflation_id:
                return [_curve_row(curve_id=inflation_id, name="override-I")]
            return []
        if "FROM app.indices" in sql:
            return [_index_row(index_id=index_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = InflationSwapPriceRequest(
        swap_id=swap_id,
        curves=[
            CurveRef(id=nominal_id, role="nominal"),
            CurveRef(id=inflation_id, role="inflation"),
        ],
        as_of=date(2025, 1, 15),
    )
    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    assert out.nominal_curve.name == "override-N"
    assert out.inflation_curve.name == "override-I"


async def test_nominal_curve_missing_returns_404_with_role_detail(
    fake_ro_engine: FakeEngine,
) -> None:
    """Missing nominal curve → role-tagged 404."""

    swap_id = uuid.uuid4()
    saved = _swap_row(
        swap_id=swap_id,
        request_json={
            "pricing": {
                "curves": [
                    {"id": str(uuid.uuid4()), "role": "nominal"},
                    {"id": str(uuid.uuid4()), "role": "inflation"},
                ],
                "inflation_index": {"id": str(uuid.uuid4())},
            }
        },
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.swaps_inflation" in sql:
            return [saved]
        if "FROM app.curves" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    with pytest.raises(SwapInflationNominalCurveNotFoundError) as excinfo:
        await assemble(
            InflationSwapPriceRequest(swap_id=swap_id, as_of=date(2025, 1, 15)),
            owner_uid=OWNER,
            ro_engine=_ro(fake_ro_engine),
        )
    err = excinfo.value
    assert err.code == "swap_inflation_nominal_curve_not_found"
    assert err.status_code == 404
    assert err.details is not None
    assert err.details[0]["role"] == "nominal"


async def test_inflation_curve_missing_returns_404_with_role_detail(
    fake_ro_engine: FakeEngine,
) -> None:
    """Missing inflation curve (when the nominal one is found) → role-tagged 404."""

    nominal_id = uuid.uuid4()
    inflation_id = uuid.uuid4()

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            requested = uuid.UUID(params["curve_id"])
            if requested == nominal_id:
                return [_curve_row(curve_id=nominal_id, name="N")]
            return []
        return []

    fake_ro_engine.set_handler(handler)

    request = InflationSwapPriceRequest(
        swap_id=None,
        swap={
            "swap_kind": "zero_coupon",
            "swaps": [{"zero_coupon_inflation_swap": {"fixed_rate": 0.02}}],
        },
        curves=[
            CurveRef(id=nominal_id, role="nominal"),
            CurveRef(id=inflation_id, role="inflation"),
        ],
        inflation_index=InflationIndexRef(id=None, index_id="EUHICP"),
        as_of=date(2025, 1, 15),
    )
    with pytest.raises(SwapInflationInflationCurveNotFoundError) as excinfo:
        await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    err = excinfo.value
    assert err.code == "swap_inflation_inflation_curve_not_found"
    assert err.status_code == 404
    assert err.details is not None
    assert err.details[0]["role"] == "inflation"


async def test_missing_pricing_block_raises_curve_resolution_failed(
    fake_ro_engine: FakeEngine,
) -> None:
    """No ``pricing`` block on a saved swap → 422 curve_resolution_failed."""

    swap_id = uuid.uuid4()
    saved = _swap_row(
        swap_id=swap_id,
        request_json={"swap_kind": "zero_coupon"},
    )
    fake_ro_engine.set_handler(lambda _sql, _params: [saved])

    with pytest.raises(SwapInflationCurveResolutionFailedError) as excinfo:
        await assemble(
            InflationSwapPriceRequest(swap_id=swap_id, as_of=date(2025, 1, 15)),
            owner_uid=OWNER,
            ro_engine=_ro(fake_ro_engine),
        )
    err = excinfo.value
    assert err.code == "swap_inflation_curve_resolution_failed"
    assert err.status_code == 422
    assert err.details is not None
    assert err.details[0]["role"] == "nominal"


async def test_only_one_curve_supplied_raises_inflation_role_failure(
    fake_ro_engine: FakeEngine,
) -> None:
    """Single-curve request → 422 with ``role=inflation`` in ``details``.

    Unlike equity options where a missing dividend curve gets a
    flat-zero placeholder, inflation swaps require both curves —
    no canonical default for the inflation-leg projection.
    """

    nominal_id = uuid.uuid4()

    fake_ro_engine.set_handler(lambda _sql, _params: [_curve_row(curve_id=nominal_id, name="N")])

    request = InflationSwapPriceRequest(
        swap_id=None,
        swap={
            "swap_kind": "zero_coupon",
            "swaps": [{"zero_coupon_inflation_swap": {}}],
        },
        # Pydantic requires >=2 inline curves; we still test the
        # split-by-role failure when both have the same role pinned.
        curves=[
            CurveRef(id=nominal_id, role="nominal"),
            CurveRef(id=nominal_id, role="nominal"),
        ],
        inflation_index=InflationIndexRef(id=None, index_id="EUHICP"),
        as_of=date(2025, 1, 15),
    )

    with pytest.raises(SwapInflationCurveResolutionFailedError) as excinfo:
        await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    err = excinfo.value
    assert err.code == "swap_inflation_curve_resolution_failed"
    assert err.details is not None
    assert err.details[0]["role"] == "inflation"


# ---------------------------------------------------------------------------
# Inflation-index family — Inflation kind invariant
# ---------------------------------------------------------------------------


async def test_index_missing_returns_404(
    fake_ro_engine: FakeEngine,
) -> None:
    nominal_id = uuid.uuid4()
    inflation_id = uuid.uuid4()
    index_id = uuid.uuid4()

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            requested = uuid.UUID(params["curve_id"])
            if requested == nominal_id:
                return [_curve_row(curve_id=nominal_id, name="N")]
            if requested == inflation_id:
                return [_curve_row(curve_id=inflation_id, name="I")]
            return []
        if "FROM app.indices" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = InflationSwapPriceRequest(
        swap_id=None,
        swap={
            "swap_kind": "zero_coupon",
            "swaps": [{"zero_coupon_inflation_swap": {}}],
        },
        curves=[
            CurveRef(id=nominal_id, role="nominal"),
            CurveRef(id=inflation_id, role="inflation"),
        ],
        inflation_index=InflationIndexRef(id=index_id),
        as_of=date(2025, 1, 15),
    )
    with pytest.raises(SwapInflationIndexNotFoundError) as excinfo:
        await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert excinfo.value.code == "swap_inflation_index_not_found"


async def test_referenced_index_with_rates_kind_raises_index_not_found(
    fake_ro_engine: FakeEngine,
) -> None:
    """An ``app.indices`` row with ``kind != 'Inflation'`` is rejected.

    The orchestrator refuses rather than letting the engine emit
    a confusing bootstrap error for a rates index flowing into
    the inflation pricer.
    """

    nominal_id = uuid.uuid4()
    inflation_id = uuid.uuid4()
    index_id = uuid.uuid4()

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            requested = uuid.UUID(params["curve_id"])
            if requested == nominal_id:
                return [_curve_row(curve_id=nominal_id, name="N")]
            if requested == inflation_id:
                return [_curve_row(curve_id=inflation_id, name="I")]
            return []
        if "FROM app.indices" in sql:
            return [_index_row(index_id=index_id, kind="Ibor")]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = InflationSwapPriceRequest(
        swap_id=None,
        swap={
            "swap_kind": "zero_coupon",
            "swaps": [{"zero_coupon_inflation_swap": {}}],
        },
        curves=[
            CurveRef(id=nominal_id, role="nominal"),
            CurveRef(id=inflation_id, role="inflation"),
        ],
        inflation_index=InflationIndexRef(id=index_id),
        as_of=date(2025, 1, 15),
    )
    with pytest.raises(SwapInflationIndexNotFoundError) as excinfo:
        await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    err = excinfo.value
    assert err.code == "swap_inflation_index_not_found"
    assert err.status_code == 404
    assert err.details is not None
    assert err.details[0]["actual_kind"] == "Ibor"
    assert err.details[0]["expected_kind"] == "Inflation"


async def test_missing_inflation_index_raises_index_resolution_failed(
    fake_ro_engine: FakeEngine,
) -> None:
    swap_id = uuid.uuid4()
    nominal_id = uuid.uuid4()
    inflation_id = uuid.uuid4()
    saved = _swap_row(
        swap_id=swap_id,
        request_json={
            "pricing": {
                "curves": [
                    {"id": str(nominal_id), "role": "nominal"},
                    {"id": str(inflation_id), "role": "inflation"},
                ],
            }
        },
    )

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.swaps_inflation" in sql:
            return [saved]
        if "FROM app.curves" in sql:
            requested = uuid.UUID(params["curve_id"])
            if requested == nominal_id:
                return [_curve_row(curve_id=nominal_id, name="N")]
            if requested == inflation_id:
                return [_curve_row(curve_id=inflation_id, name="I")]
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    with pytest.raises(SwapInflationIndexResolutionFailedError) as excinfo:
        await assemble(
            InflationSwapPriceRequest(swap_id=swap_id, as_of=date(2025, 1, 15)),
            owner_uid=OWNER,
            ro_engine=_ro(fake_ro_engine),
        )
    assert excinfo.value.code == "swap_inflation_index_resolution_failed"


# ---------------------------------------------------------------------------
# swap_kind discriminator
# ---------------------------------------------------------------------------


async def test_swap_kind_request_override_wins(
    fake_ro_engine: FakeEngine,
) -> None:
    fake_ro_engine.set_handler(lambda _sql, _params: [])

    out = await assemble(
        _inline_request(swap_kind="year_on_year"),
        owner_uid=OWNER,
        ro_engine=_ro(fake_ro_engine),
    )
    assert out.swap_kind == "year_on_year"


async def test_swap_kind_inferred_from_engine_canonical_shape(
    fake_ro_engine: FakeEngine,
) -> None:
    """``swap_kind`` left unset → infers from ``swaps[*].year_on_year_inflation_swap``."""

    fake_ro_engine.set_handler(lambda _sql, _params: [])

    request = InflationSwapPriceRequest(
        swap_id=None,
        swap={
            "swaps": [
                {
                    "year_on_year_inflation_swap": {"fixed_rate": 0.02},
                }
            ]
        },
        curves=[
            CurveRef(id=None, name="N", role="nominal", points=[{"rate": 0.03}]),
            CurveRef(id=None, name="I", role="inflation", points=[{"rate": 0.02}]),
        ],
        inflation_index=InflationIndexRef(id=None, index_id="EUHICP_YY"),
        as_of=date(2025, 1, 15),
    )
    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert out.swap_kind == "year_on_year"


async def test_swap_kind_defaults_to_zero_coupon(
    fake_ro_engine: FakeEngine,
) -> None:
    fake_ro_engine.set_handler(lambda _sql, _params: [])

    request = InflationSwapPriceRequest(
        swap_id=None,
        swap={"swaps": [{"some_unknown_thing": {}}]},
        curves=[
            CurveRef(id=None, name="N", role="nominal", points=[{"rate": 0.03}]),
            CurveRef(id=None, name="I", role="inflation", points=[{"rate": 0.02}]),
        ],
        inflation_index=InflationIndexRef(id=None, index_id="EUHICP"),
        as_of=date(2025, 1, 15),
    )
    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert out.swap_kind == "zero_coupon"


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


async def test_snapshot_id_loads_pin_map(fake_ro_engine: FakeEngine) -> None:
    snapshot_id = uuid.uuid4()
    nominal_id = uuid.uuid4()
    inflation_id = uuid.uuid4()
    index_id = uuid.uuid4()

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            requested = uuid.UUID(params["curve_id"])
            if requested == nominal_id:
                return [_curve_row(curve_id=nominal_id, name="N")]
            if requested == inflation_id:
                return [_curve_row(curve_id=inflation_id, name="I")]
            return []
        if "FROM app.indices" in sql:
            return [_index_row(index_id=index_id)]
        if "FROM app.snapshots" in sql:
            return [
                _snapshot_row(
                    snapshot_id=snapshot_id,
                    content={
                        "USD.IRS.1Y": 4.25,
                        "EUR.HICP.1Y": {"value": 0.02, "source": "vendor_x"},
                    },
                )
            ]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = InflationSwapPriceRequest(
        swap_id=None,
        swap={
            "swap_kind": "zero_coupon",
            "swaps": [{"zero_coupon_inflation_swap": {}}],
        },
        curves=[
            CurveRef(id=nominal_id, role="nominal"),
            CurveRef(id=inflation_id, role="inflation"),
        ],
        inflation_index=InflationIndexRef(id=index_id),
        as_of=date(2025, 1, 15),
        snapshot_id=snapshot_id,
    )
    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert out.snapshot is not None
    assert out.snapshot.id == snapshot_id
    assert out.snapshot.pins["USD.IRS.1Y"]["value"] == 4.25
    assert out.snapshot.pins["EUR.HICP.1Y"]["value"] == 0.02


async def test_missing_snapshot_returns_snapshot_not_found(
    fake_ro_engine: FakeEngine,
) -> None:
    snapshot_id = uuid.uuid4()
    nominal_id = uuid.uuid4()
    inflation_id = uuid.uuid4()
    index_id = uuid.uuid4()

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            requested = uuid.UUID(params["curve_id"])
            if requested == nominal_id:
                return [_curve_row(curve_id=nominal_id, name="N")]
            if requested == inflation_id:
                return [_curve_row(curve_id=inflation_id, name="I")]
            return []
        if "FROM app.indices" in sql:
            return [_index_row(index_id=index_id)]
        if "FROM app.snapshots" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = InflationSwapPriceRequest(
        swap_id=None,
        swap={
            "swap_kind": "zero_coupon",
            "swaps": [{"zero_coupon_inflation_swap": {}}],
        },
        curves=[
            CurveRef(id=nominal_id, role="nominal"),
            CurveRef(id=inflation_id, role="inflation"),
        ],
        inflation_index=InflationIndexRef(id=index_id),
        as_of=date(2025, 1, 15),
        snapshot_id=snapshot_id,
    )
    with pytest.raises(SwapInflationSnapshotNotFoundError) as excinfo:
        await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert excinfo.value.code == "swap_inflation_snapshot_not_found"
