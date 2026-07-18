"""Unit tests for ``pricing/swap_ir/assembler.py``.

The assembler is the only place in the swap_ir path that touches
``app_ro``; these tests exercise every SQL site + every branch of
the ref/inline collapser without spinning up a real database.

Coverage:

* Inline mode (``swap=...``) skips ``app.swaps_ir`` entirely; only
  ``app.curves`` is touched for explicit curve refs.
* By-reference mode (``swap_id=...``) hits ``app.swaps_ir`` once
  with the right WHERE clause (owner_uid, deleted_at IS NULL).
* Soft-deleted / cross-tenant / missing swap rows surface as
  ``swap_ir_not_found`` (404).
* ``request.curves`` override wins over the saved swap's own
  pricing block.
* ``pricing.curve_set_id`` loads ``app.curve_sets`` and projects
  ``body.curve_refs`` into curve ids, then loads each from
  ``app.curves``.
* ``pricing.curves`` (per-curve refs or inline definitions) is the
  fallback when no curve set is pinned.
* Missing curve_set / missing inferred-from-saved-swap shape /
  missing curves all collapse onto the right product code:
  ``swap_ir_curve_set_not_found`` (404) or
  ``swap_ir_curve_resolution_failed`` (422).
* ``snapshot_id`` loads ``app.snapshots`` and projects its content
  into the snapshot's ``pins`` map (all three observed shapes).
* Snapshot misses surface as ``swap_ir_snapshot_not_found`` (404).
* Every SQL site carries the ``owner_uid`` predicate and the
  ``deleted_at IS NULL`` filter (north-star #5).
"""

from __future__ import annotations

import uuid
from datetime import date
from http import HTTPStatus
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_orchestrator.pricing.swap_ir.assembler import (
    AssemblerOutput,
    ResolvedSnapshot,
    assemble,
)
from quantra_orchestrator.pricing.swap_ir.errors import (
    SwapIrCurveResolutionFailedError,
    SwapIrCurveSetNotFoundError,
    SwapIrNotFoundError,
    SwapIrSnapshotNotFoundError,
)
from quantra_orchestrator.pricing.swap_ir.models import (
    CurveRef,
    IrSwapPriceRequest,
)

from .conftest import FakeEngine

OWNER = "user-test"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _curve_row(*, curve_id: uuid.UUID, name: str = "USD-OIS") -> dict[str, Any]:
    return {
        "id": str(curve_id),
        "name": name,
        "currency": "USD",
        "day_counter": "Actual/360",
        "helper_kind": "Discount",
        "reference_date": date(2026, 5, 13),
        "points": [{"tenor": "1Y", "quote_id": "USD.IRS.1Y"}],
        "body": {"interpolator": "Cubic"},
    }


def _swap_row(*, swap_id: uuid.UUID, request_json: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(swap_id), "name": "demo-swap", "request": request_json}


def _curve_set_row(*, curve_set_id: uuid.UUID, refs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": str(curve_set_id),
        "name": "USD set",
        "body": {"curve_refs": refs},
    }


def _snapshot_row(*, snapshot_id: uuid.UUID, content: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(snapshot_id), "name": "EOD-USD", "content": content}


def _ro(engine: FakeEngine) -> AsyncEngine:
    """Cast the test FakeEngine to AsyncEngine so mypy stays happy."""

    return cast(AsyncEngine, engine)


# ---------------------------------------------------------------------------
# Inline mode (no app.swaps_ir read)
# ---------------------------------------------------------------------------


async def test_inline_swap_does_not_touch_swaps_ir(
    fake_ro_engine: FakeEngine,
) -> None:
    curve_id = uuid.uuid4()
    curve = _curve_row(curve_id=curve_id)

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [curve]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = IrSwapPriceRequest(
        swap_id=None,
        swap={"notional": 1_000_000.0},
        curves=[CurveRef(id=curve_id)],
        as_of=date(2026, 5, 13),
        snapshot_id=None,
    )
    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    assert isinstance(out, AssemblerOutput)
    assert out.trade.swap_id is None
    assert out.trade.swap == {"notional": 1_000_000.0}
    assert len(out.curves) == 1
    assert out.curves[0].id == curve_id
    assert out.curve_set_id is None
    assert out.snapshot is None
    sqls = [rec.sql for rec in fake_ro_engine.recordings]
    assert all("FROM app.swaps_ir" not in sql for sql in sqls)


async def test_inline_swap_with_inline_curves_skips_all_db(
    fake_ro_engine: FakeEngine,
) -> None:
    request = IrSwapPriceRequest(
        swap_id=None,
        swap={"notional": 1.0},
        curves=[
            CurveRef(
                name="ad-hoc",
                points=[{"tenor": "1Y", "quote_id": "USD.IRS.1Y"}],
            )
        ],
        as_of=date(2026, 5, 13),
        snapshot_id=None,
    )

    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    assert out.curves[0].id is None
    assert out.curves[0].name == "ad-hoc"
    assert fake_ro_engine.recordings == []


# ---------------------------------------------------------------------------
# By-reference mode
# ---------------------------------------------------------------------------


async def test_by_reference_loads_swap_and_curve_set(
    fake_ro_engine: FakeEngine,
) -> None:
    swap_id = uuid.uuid4()
    curve_set_id = uuid.uuid4()
    curve_id = uuid.uuid4()

    swap = _swap_row(
        swap_id=swap_id,
        request_json={"pricing": {"curve_set_id": str(curve_set_id)}},
    )
    curve_set = _curve_set_row(curve_set_id=curve_set_id, refs=[{"curve_id": str(curve_id)}])
    curve = _curve_row(curve_id=curve_id)

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.swaps_ir" in sql:
            return [swap]
        if "FROM app.curve_sets" in sql:
            return [curve_set]
        if "FROM app.curves" in sql:
            return [curve]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = IrSwapPriceRequest(
        swap_id=swap_id,
        swap=None,
        curves=None,
        as_of=date(2026, 5, 13),
        snapshot_id=None,
    )
    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    assert out.trade.swap_id == swap_id
    assert out.trade.name == "demo-swap"
    assert out.curve_set_id == curve_set_id
    assert len(out.curves) == 1
    assert out.curves[0].id == curve_id


async def test_swap_row_missing_raises_swap_ir_not_found(
    fake_ro_engine: FakeEngine,
) -> None:
    swap_id = uuid.uuid4()
    fake_ro_engine.set_handler(lambda _sql, _params: [])

    request = IrSwapPriceRequest(
        swap_id=swap_id,
        swap=None,
        curves=None,
        as_of=date(2026, 5, 13),
        snapshot_id=None,
    )
    with pytest.raises(SwapIrNotFoundError) as excinfo:
        await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert excinfo.value.status_code == HTTPStatus.NOT_FOUND
    assert excinfo.value.code == "swap_ir_not_found"


async def test_request_curves_override_skips_curve_set_lookup(
    fake_ro_engine: FakeEngine,
) -> None:
    """An explicit ``curves`` override on the request wins over the saved swap.

    Hits ``app.swaps_ir`` once (to populate the trade body) and
    each requested curve once; never touches ``app.curve_sets``.
    """

    swap_id = uuid.uuid4()
    override_curve_id = uuid.uuid4()

    swap = _swap_row(
        swap_id=swap_id,
        request_json={
            "pricing": {"curve_set_id": str(uuid.uuid4())},
        },
    )
    curve = _curve_row(curve_id=override_curve_id, name="override")

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.swaps_ir" in sql:
            return [swap]
        if "FROM app.curves" in sql:
            return [curve]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = IrSwapPriceRequest(
        swap_id=swap_id,
        swap=None,
        curves=[CurveRef(id=override_curve_id)],
        as_of=date(2026, 5, 13),
        snapshot_id=None,
    )
    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    assert out.curve_set_id is None
    assert out.curves[0].name == "override"
    sqls = [rec.sql for rec in fake_ro_engine.recordings]
    assert all("FROM app.curve_sets" not in sql for sql in sqls)


async def test_pricing_curves_fallback_when_no_curve_set(
    fake_ro_engine: FakeEngine,
) -> None:
    swap_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    swap = _swap_row(
        swap_id=swap_id,
        request_json={
            "pricing": {"curves": [{"id": str(curve_id)}]},
        },
    )
    curve = _curve_row(curve_id=curve_id)

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.swaps_ir" in sql:
            return [swap]
        if "FROM app.curves" in sql:
            return [curve]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = IrSwapPriceRequest(
        swap_id=swap_id,
        swap=None,
        curves=None,
        as_of=date(2026, 5, 13),
        snapshot_id=None,
    )
    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    assert out.curve_set_id is None
    assert len(out.curves) == 1


# ---------------------------------------------------------------------------
# Error surfaces
# ---------------------------------------------------------------------------


async def test_missing_curve_set_raises_swap_ir_curve_set_not_found(
    fake_ro_engine: FakeEngine,
) -> None:
    swap_id = uuid.uuid4()
    curve_set_id = uuid.uuid4()
    swap = _swap_row(
        swap_id=swap_id,
        request_json={"pricing": {"curve_set_id": str(curve_set_id)}},
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.swaps_ir" in sql:
            return [swap]
        if "FROM app.curve_sets" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = IrSwapPriceRequest(
        swap_id=swap_id,
        swap=None,
        curves=None,
        as_of=date(2026, 5, 13),
        snapshot_id=None,
    )
    with pytest.raises(SwapIrCurveSetNotFoundError) as excinfo:
        await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert excinfo.value.code == "swap_ir_curve_set_not_found"
    assert excinfo.value.status_code == HTTPStatus.NOT_FOUND


async def test_missing_referenced_curve_raises_resolution_failed(
    fake_ro_engine: FakeEngine,
) -> None:
    swap_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    swap = _swap_row(
        swap_id=swap_id,
        request_json={"pricing": {"curves": [{"id": str(curve_id)}]}},
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.swaps_ir" in sql:
            return [swap]
        if "FROM app.curves" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = IrSwapPriceRequest(
        swap_id=swap_id,
        swap=None,
        curves=None,
        as_of=date(2026, 5, 13),
        snapshot_id=None,
    )
    with pytest.raises(SwapIrCurveResolutionFailedError) as excinfo:
        await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert excinfo.value.code == "swap_ir_curve_resolution_failed"


async def test_saved_swap_without_pricing_block_raises_curve_resolution_failed(
    fake_ro_engine: FakeEngine,
) -> None:
    swap_id = uuid.uuid4()
    swap = _swap_row(swap_id=swap_id, request_json={"notional": 1.0})

    fake_ro_engine.set_handler(lambda _sql, _params: [swap])

    request = IrSwapPriceRequest(
        swap_id=swap_id,
        swap=None,
        curves=None,
        as_of=date(2026, 5, 13),
        snapshot_id=None,
    )
    with pytest.raises(SwapIrCurveResolutionFailedError) as excinfo:
        await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert excinfo.value.code == "swap_ir_curve_resolution_failed"


async def test_saved_swap_with_empty_pricing_block_raises_curve_resolution_failed(
    fake_ro_engine: FakeEngine,
) -> None:
    swap_id = uuid.uuid4()
    swap = _swap_row(swap_id=swap_id, request_json={"pricing": {}})

    fake_ro_engine.set_handler(lambda _sql, _params: [swap])

    request = IrSwapPriceRequest(
        swap_id=swap_id,
        swap=None,
        curves=None,
        as_of=date(2026, 5, 13),
        snapshot_id=None,
    )
    with pytest.raises(SwapIrCurveResolutionFailedError):
        await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))


# ---------------------------------------------------------------------------
# Snapshot pinning
# ---------------------------------------------------------------------------


async def test_snapshot_id_loads_pins_from_bare_value_content(
    fake_ro_engine: FakeEngine,
) -> None:
    snapshot_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    snapshot = _snapshot_row(
        snapshot_id=snapshot_id,
        content={"USD.IRS.1Y": 4.25, "USD.IRS.5Y": 4.10},
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.snapshots" in sql:
            return [snapshot]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = IrSwapPriceRequest(
        swap_id=None,
        swap={"notional": 1.0},
        curves=[CurveRef(id=curve_id)],
        as_of=date(2026, 5, 13),
        snapshot_id=snapshot_id,
    )
    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    assert isinstance(out.snapshot, ResolvedSnapshot)
    assert out.snapshot.id == snapshot_id
    assert out.snapshot.pins["USD.IRS.1Y"]["value"] == pytest.approx(4.25)
    assert out.snapshot.pins["USD.IRS.5Y"]["value"] == pytest.approx(4.10)


async def test_snapshot_id_loads_pins_from_object_content(
    fake_ro_engine: FakeEngine,
) -> None:
    snapshot_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    snapshot = _snapshot_row(
        snapshot_id=snapshot_id,
        content={"USD.IRS.1Y": {"value": 4.25, "source": "vendor_x"}},
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.snapshots" in sql:
            return [snapshot]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = IrSwapPriceRequest(
        swap_id=None,
        swap={"notional": 1.0},
        curves=[CurveRef(id=curve_id)],
        as_of=date(2026, 5, 13),
        snapshot_id=snapshot_id,
    )
    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    assert out.snapshot is not None
    assert out.snapshot.pins["USD.IRS.1Y"]["value"] == pytest.approx(4.25)
    assert out.snapshot.pins["USD.IRS.1Y"]["source"] == "vendor_x"


async def test_snapshot_id_loads_pins_from_list_of_objects_content(
    fake_ro_engine: FakeEngine,
) -> None:
    snapshot_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    snapshot = _snapshot_row(
        snapshot_id=snapshot_id,
        content={"quotes": [{"canonical_id": "USD.IRS.1Y", "value": 4.25, "source": "vendor_x"}]},
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.snapshots" in sql:
            return [snapshot]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = IrSwapPriceRequest(
        swap_id=None,
        swap={"notional": 1.0},
        curves=[CurveRef(id=curve_id)],
        as_of=date(2026, 5, 13),
        snapshot_id=snapshot_id,
    )
    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    assert out.snapshot is not None
    assert out.snapshot.pins["USD.IRS.1Y"]["source"] == "vendor_x"


async def test_missing_snapshot_raises_snapshot_not_found(
    fake_ro_engine: FakeEngine,
) -> None:
    snapshot_id = uuid.uuid4()
    curve_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.snapshots" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = IrSwapPriceRequest(
        swap_id=None,
        swap={"notional": 1.0},
        curves=[CurveRef(id=curve_id)],
        as_of=date(2026, 5, 13),
        snapshot_id=snapshot_id,
    )
    with pytest.raises(SwapIrSnapshotNotFoundError) as excinfo:
        await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert excinfo.value.code == "swap_ir_snapshot_not_found"


async def test_snapshot_loads_md_version_etag_soft_pin(
    fake_ro_engine: FakeEngine,
) -> None:
    # content.md_version_etag is read into
    # ResolvedSnapshot.version_etag; the reserved key MUST NOT appear
    # in pins (risk gate).
    snapshot_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    snapshot = _snapshot_row(
        snapshot_id=snapshot_id,
        content={
            "md_version_etag": "etag-abc",
            "USD.IRS.1Y": 4.25,
        },
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.snapshots" in sql:
            return [snapshot]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = IrSwapPriceRequest(
        swap_id=None,
        swap={"notional": 1.0},
        curves=[CurveRef(id=curve_id)],
        as_of=date(2026, 5, 13),
        snapshot_id=snapshot_id,
    )
    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    assert isinstance(out.snapshot, ResolvedSnapshot)
    assert out.snapshot.version_etag == "etag-abc"
    assert "md_version_etag" not in out.snapshot.pins
    assert out.snapshot.pins["USD.IRS.1Y"]["value"] == pytest.approx(4.25)


async def test_snapshot_without_md_version_etag_leaves_version_none(
    fake_ro_engine: FakeEngine,
) -> None:
    # Backward compat: today's app.snapshots rows have no
    # md_version_etag key -> ResolvedSnapshot.version_etag is None ->
    # routes pass snapshot_version=None and the MD service stays on
    # the live quote_points fallback.
    snapshot_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    snapshot = _snapshot_row(
        snapshot_id=snapshot_id,
        content={"USD.IRS.1Y": 4.25},
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.snapshots" in sql:
            return [snapshot]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = IrSwapPriceRequest(
        swap_id=None,
        swap={"notional": 1.0},
        curves=[CurveRef(id=curve_id)],
        as_of=date(2026, 5, 13),
        snapshot_id=snapshot_id,
    )
    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    assert out.snapshot is not None
    assert out.snapshot.version_etag is None
    assert out.snapshot.pins["USD.IRS.1Y"]["value"] == pytest.approx(4.25)


# ---------------------------------------------------------------------------
# Cross-cutting invariants
# ---------------------------------------------------------------------------


async def test_every_read_carries_owner_uid_and_soft_delete_filter(
    fake_ro_engine: FakeEngine,
) -> None:
    """All four SQL sites must filter by ``owner_uid`` + ``deleted_at IS NULL``.

    Pin every read site so a regression that drops one filter
    shows up as a test failure rather than as a cross-tenant /
    soft-delete-visibility bug.
    """

    swap_id = uuid.uuid4()
    curve_set_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()

    swap = _swap_row(
        swap_id=swap_id,
        request_json={"pricing": {"curve_set_id": str(curve_set_id)}},
    )
    curve_set = _curve_set_row(curve_set_id=curve_set_id, refs=[{"curve_id": str(curve_id)}])
    curve = _curve_row(curve_id=curve_id)
    snapshot = _snapshot_row(snapshot_id=snapshot_id, content={})

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.swaps_ir" in sql:
            return [swap]
        if "FROM app.curve_sets" in sql:
            return [curve_set]
        if "FROM app.curves" in sql:
            return [curve]
        if "FROM app.snapshots" in sql:
            return [snapshot]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = IrSwapPriceRequest(
        swap_id=swap_id,
        swap=None,
        curves=None,
        as_of=date(2026, 5, 13),
        snapshot_id=snapshot_id,
    )
    await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    sites = {
        "app.swaps_ir": False,
        "app.curve_sets": False,
        "app.curves": False,
        "app.snapshots": False,
    }
    for rec in fake_ro_engine.recordings:
        for table in sites:
            if f"FROM {table}" in rec.sql:
                assert "owner_uid = :owner_uid" in rec.sql
                assert "deleted_at IS NULL" in rec.sql
                assert rec.params["owner_uid"] == OWNER
                sites[table] = True
    assert all(sites.values()), sites


async def test_inline_curve_in_pricing_curves_resolves(
    fake_ro_engine: FakeEngine,
) -> None:
    """``pricing.curves[*]`` may carry an inline definition (not just a ref).

    Saved swaps that bundle ad-hoc curves bypass ``app.curves``
    entirely. The assembler must materialise the inline definition
    via the same ``ResolvedCurve`` shape so MD resolution doesn't
    need to know which source the curve came from.
    """

    swap_id = uuid.uuid4()
    inline_curve = {
        "name": "inline-ois",
        "points": [{"tenor": "1Y", "quote_id": "USD.IRS.1Y"}],
    }
    swap = _swap_row(swap_id=swap_id, request_json={"pricing": {"curves": [inline_curve]}})

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.swaps_ir" in sql:
            return [swap]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = IrSwapPriceRequest(
        swap_id=swap_id,
        swap=None,
        curves=None,
        as_of=date(2026, 5, 13),
        snapshot_id=None,
    )
    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    assert out.curves[0].name == "inline-ois"
    assert out.curves[0].id is None
