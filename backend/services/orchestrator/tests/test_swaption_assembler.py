"""Unit tests for ``pricing/swaption/assembler.py``.

The assembler is the only place in the swaption path that touches
``app_ro``; these tests exercise every SQL site + every branch of
the ref/inline collapser without spinning up a real database.

Coverage:

* Inline mode (``swaption=...``) skips ``app.swaptions`` entirely;
  only ``app.curves`` / ``app.vol_surfaces`` / ``app.swaption_models``
  get touched for explicit refs.
* By-reference mode (``swaption_id=...``) hits ``app.swaptions`` once
  with the right WHERE clause (owner_uid, deleted_at IS NULL).
* Soft-deleted / cross-tenant / missing swaption rows surface as
  ``swaption_not_found`` (404).
* ``request.curves`` / ``request.vol_surface`` / ``request.swaption_model``
  overrides win over the saved swaption's pricing block.
* ``pricing.curve_set_id`` loads ``app.curve_sets`` and projects
  ``body.curve_refs`` into curve ids, then loads each from
  ``app.curves``.
* ``pricing.vol_surface_id`` loads ``app.vol_surfaces`` directly.
* ``pricing.vol_surfaces[0]`` (with no id) is treated as inline.
* ``pricing.swaption_model_id`` loads ``app.swaption_models`` directly.
* ``pricing.models[0]`` (with no id) is treated as inline.
* Missing curve_set / vol_surface / model / snapshot all collapse
  onto the right product code (404 + ``swaption_*_not_found``).
* Bad inline shapes surface as ``swaption_*_resolution_failed`` (422)
  or ``swaption_invalid_request`` (400).
* ``snapshot_id`` loads ``app.snapshots`` and projects content into
  pins (same three observed shapes as swap_ir).
* Every SQL site carries ``owner_uid`` + ``deleted_at IS NULL``
  (north-star #5).
"""

from __future__ import annotations

import uuid
from datetime import date
from http import HTTPStatus
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_common.engine_client._generated.quantra.enums.Frequency import Frequency
from quantra_orchestrator.pricing._translator import (
    ResolvedMarketData,
    float_leg_frequency,
)
from quantra_orchestrator.pricing.swaption.assembler import (
    AssemblerOutput,
    ResolvedSnapshot,
    assemble,
)
from quantra_orchestrator.pricing.swaption.errors import (
    SwaptionCurveResolutionFailedError,
    SwaptionCurveSetNotFoundError,
    SwaptionIndexNotFoundError,
    SwaptionIndexResolutionFailedError,
    SwaptionInvalidRequestError,
    SwaptionModelNotFoundError,
    SwaptionNotFoundError,
    SwaptionSnapshotNotFoundError,
    SwaptionSurfaceResolutionFailedError,
    SwaptionVolSurfaceNotFoundError,
)
from quantra_orchestrator.pricing.swaption.models import (
    CurveRef,
    IndexRef,
    SwaptionModelRef,
    SwaptionPriceRequest,
    VolSurfaceRef,
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


def _swaption_row(*, swaption_id: uuid.UUID, request_json: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(swaption_id),
        "name": "demo-swaption",
        "request": request_json,
    }


def _curve_set_row(*, curve_set_id: uuid.UUID, refs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": str(curve_set_id),
        "name": "USD set",
        "body": {"curve_refs": refs},
    }


def _vol_surface_row(
    *,
    vol_surface_id: uuid.UUID,
    name: str = "USD ATM cube",
    kind: str = "SwaptionVolSpec",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": str(vol_surface_id),
        "name": name,
        "kind": kind,
        "payload": payload or {"axes_expiries": [], "axes_tenors": []},
    }


def _swaption_model_row(
    *,
    model_id: uuid.UUID,
    name: str = "USD HW",
    kind: str = "HullWhiteLattice",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": str(model_id),
        "name": name,
        "kind": kind,
        "payload": payload or {"hw_a": 0.05, "hw_sigma": 0.01},
    }


def _snapshot_row(*, snapshot_id: uuid.UUID, content: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(snapshot_id), "name": "EOD-USD", "content": content}


def _ro(engine: FakeEngine) -> AsyncEngine:
    return cast(AsyncEngine, engine)


def _inline_request(
    *,
    curve_id: uuid.UUID,
    snapshot_id: uuid.UUID | None = None,
    vol_surface: VolSurfaceRef | None = None,
    swaption_model: SwaptionModelRef | None = None,
) -> SwaptionPriceRequest:
    return SwaptionPriceRequest(
        swaption_id=None,
        swaption={"exercise_type": "European"},
        curves=[CurveRef(id=curve_id)],
        vol_surface=vol_surface
        or VolSurfaceRef(
            id=None,
            name="ad-hoc",
            kind="SwaptionVolSpec",
            payload={"axes_expiries": []},
        ),
        swaption_model=swaption_model
        or SwaptionModelRef(
            id=None,
            name="ad-hoc",
            kind="HullWhiteLattice",
            payload={"hw_a": 0.05, "hw_sigma": 0.01},
        ),
        as_of=date(2026, 5, 13),
        snapshot_id=snapshot_id,
    )


# ---------------------------------------------------------------------------
# Inline mode (no app.swaptions read)
# ---------------------------------------------------------------------------


async def test_inline_swaption_does_not_touch_swaptions_table(
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

    request = _inline_request(curve_id=curve_id)
    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    assert isinstance(out, AssemblerOutput)
    assert out.trade.swaption_id is None
    assert out.trade.swaption == {"exercise_type": "European"}
    assert len(out.curves) == 1
    assert out.curves[0].id == curve_id
    assert out.vol_surface.kind == "SwaptionVolSpec"
    assert out.swaption_model.kind == "HullWhiteLattice"
    assert out.curve_set_id is None
    assert out.vol_surface_id is None
    assert out.swaption_model_id is None
    assert out.snapshot is None
    sqls = [rec.sql for rec in fake_ro_engine.recordings]
    assert all("FROM app.swaptions" not in sql for sql in sqls)
    assert all("FROM app.vol_surfaces" not in sql for sql in sqls)
    assert all("FROM app.swaption_models" not in sql for sql in sqls)


async def test_fully_inline_request_skips_all_db(
    fake_ro_engine: FakeEngine,
) -> None:
    request = SwaptionPriceRequest(
        swaption_id=None,
        swaption={"exercise_type": "European"},
        curves=[
            CurveRef(
                name="ad-hoc",
                points=[{"tenor": "1Y", "quote_id": "USD.IRS.1Y"}],
            )
        ],
        vol_surface=VolSurfaceRef(
            id=None,
            name="ad-hoc",
            kind="SwaptionVolSpec",
            payload={"axes_expiries": []},
        ),
        swaption_model=SwaptionModelRef(
            id=None,
            name="ad-hoc",
            kind="HullWhiteLattice",
            payload={"hw_a": 0.05, "hw_sigma": 0.01},
        ),
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


async def test_by_reference_loads_swaption_curve_set_vol_surface_model(
    fake_ro_engine: FakeEngine,
) -> None:
    swaption_id = uuid.uuid4()
    curve_set_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    vol_surface_id = uuid.uuid4()
    model_id = uuid.uuid4()

    swaption = _swaption_row(
        swaption_id=swaption_id,
        request_json={
            "pricing": {
                "curve_set_id": str(curve_set_id),
                "vol_surface_id": str(vol_surface_id),
                "swaption_model_id": str(model_id),
            }
        },
    )
    curve_set = _curve_set_row(curve_set_id=curve_set_id, refs=[{"curve_id": str(curve_id)}])
    curve = _curve_row(curve_id=curve_id)
    vol_surface = _vol_surface_row(vol_surface_id=vol_surface_id)
    model = _swaption_model_row(model_id=model_id)

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.swaptions" in sql:
            return [swaption]
        if "FROM app.curve_sets" in sql:
            return [curve_set]
        if "FROM app.curves" in sql:
            return [curve]
        if "FROM app.vol_surfaces" in sql:
            return [vol_surface]
        if "FROM app.swaption_models" in sql:
            return [model]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = SwaptionPriceRequest(
        swaption_id=swaption_id,
        swaption=None,
        curves=None,
        vol_surface=None,
        swaption_model=None,
        as_of=date(2026, 5, 13),
        snapshot_id=None,
    )
    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    assert out.trade.swaption_id == swaption_id
    assert out.trade.name == "demo-swaption"
    assert out.curve_set_id == curve_set_id
    assert len(out.curves) == 1
    assert out.curves[0].id == curve_id
    assert out.vol_surface_id == vol_surface_id
    assert out.vol_surface.kind == "SwaptionVolSpec"
    assert out.swaption_model_id == model_id
    assert out.swaption_model.kind == "HullWhiteLattice"


async def test_swaption_row_missing_raises_swaption_not_found(
    fake_ro_engine: FakeEngine,
) -> None:
    swaption_id = uuid.uuid4()
    fake_ro_engine.set_handler(lambda _sql, _params: [])

    request = SwaptionPriceRequest(
        swaption_id=swaption_id,
        swaption=None,
        curves=None,
        vol_surface=None,
        swaption_model=None,
        as_of=date(2026, 5, 13),
        snapshot_id=None,
    )
    with pytest.raises(SwaptionNotFoundError) as excinfo:
        await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert excinfo.value.status_code == HTTPStatus.NOT_FOUND
    assert excinfo.value.code == "swaption_not_found"


async def test_request_overrides_win_over_saved_swaption(
    fake_ro_engine: FakeEngine,
) -> None:
    """Explicit ``curves`` / ``vol_surface`` / ``swaption_model`` overrides take precedence."""

    swaption_id = uuid.uuid4()
    override_curve_id = uuid.uuid4()
    # Saved swaption points at IDs that should be ignored.
    swaption = _swaption_row(
        swaption_id=swaption_id,
        request_json={
            "pricing": {
                "curve_set_id": str(uuid.uuid4()),
                "vol_surface_id": str(uuid.uuid4()),
                "swaption_model_id": str(uuid.uuid4()),
            }
        },
    )
    curve = _curve_row(curve_id=override_curve_id, name="override")

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.swaptions" in sql:
            return [swaption]
        if "FROM app.curves" in sql:
            return [curve]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = SwaptionPriceRequest(
        swaption_id=swaption_id,
        swaption=None,
        curves=[CurveRef(id=override_curve_id)],
        vol_surface=VolSurfaceRef(
            id=None,
            name="override",
            kind="SwaptionVolSpec",
            payload={"axes_expiries": []},
        ),
        swaption_model=SwaptionModelRef(
            id=None,
            name="override",
            kind="HullWhiteLattice",
            payload={"hw_a": 0.05, "hw_sigma": 0.01},
        ),
        as_of=date(2026, 5, 13),
        snapshot_id=None,
    )
    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    assert out.curve_set_id is None
    assert out.curves[0].name == "override"
    assert out.vol_surface.name == "override"
    assert out.swaption_model.name == "override"
    sqls = [rec.sql for rec in fake_ro_engine.recordings]
    assert all("FROM app.curve_sets" not in sql for sql in sqls)
    assert all("FROM app.vol_surfaces" not in sql for sql in sqls)
    assert all("FROM app.swaption_models" not in sql for sql in sqls)


async def test_pricing_vol_surfaces_first_entry_inline_path(
    fake_ro_engine: FakeEngine,
) -> None:
    """Saved swaption with inline ``pricing.vol_surfaces[0]`` (no id) coerces fine.

    Saved-row shape follows the portal's flat ``VolSurfaceSpec``
    (kind + grid + axes at the top level); the assembler re-shapes
    it into the typed ``payload``-wrapped form so the resolver
    downstream sees one canonical layout.
    """

    swaption_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    model_id = uuid.uuid4()
    swaption = _swaption_row(
        swaption_id=swaption_id,
        request_json={
            "pricing": {
                "curves": [{"id": str(curve_id)}],
                "vol_surfaces": [
                    {
                        "kind": "SwaptionVolSpec",
                        "axes_expiries": [{"tenor": "1Y"}],
                        "axes_tenors": [],
                    }
                ],
                "swaption_model_id": str(model_id),
            }
        },
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.swaptions" in sql:
            return [swaption]
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.swaption_models" in sql:
            return [_swaption_model_row(model_id=model_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = SwaptionPriceRequest(
        swaption_id=swaption_id,
        swaption=None,
        curves=None,
        vol_surface=None,
        swaption_model=None,
        as_of=date(2026, 5, 13),
        snapshot_id=None,
    )
    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    assert out.vol_surface_id is None
    assert out.vol_surface.kind == "SwaptionVolSpec"
    assert out.vol_surface.payload["axes_expiries"] == [{"tenor": "1Y"}]
    sqls = [rec.sql for rec in fake_ro_engine.recordings]
    assert all("FROM app.vol_surfaces" not in sql for sql in sqls)


async def test_pricing_models_first_entry_inline_path(
    fake_ro_engine: FakeEngine,
) -> None:
    swaption_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    vol_surface_id = uuid.uuid4()
    swaption = _swaption_row(
        swaption_id=swaption_id,
        request_json={
            "pricing": {
                "curves": [{"id": str(curve_id)}],
                "vol_surface_id": str(vol_surface_id),
                "models": [{"kind": "HullWhiteLattice", "hw_a": 0.06, "hw_sigma": 0.02}],
            }
        },
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.swaptions" in sql:
            return [swaption]
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.vol_surfaces" in sql:
            return [_vol_surface_row(vol_surface_id=vol_surface_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = SwaptionPriceRequest(
        swaption_id=swaption_id,
        swaption=None,
        curves=None,
        vol_surface=None,
        swaption_model=None,
        as_of=date(2026, 5, 13),
        snapshot_id=None,
    )
    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    assert out.swaption_model_id is None
    assert out.swaption_model.kind == "HullWhiteLattice"
    assert out.swaption_model.payload["hw_a"] == pytest.approx(0.06)
    sqls = [rec.sql for rec in fake_ro_engine.recordings]
    assert all("FROM app.swaption_models" not in sql for sql in sqls)


# ---------------------------------------------------------------------------
# Error surfaces
# ---------------------------------------------------------------------------


async def test_missing_curve_set_raises_swaption_curve_set_not_found(
    fake_ro_engine: FakeEngine,
) -> None:
    swaption_id = uuid.uuid4()
    curve_set_id = uuid.uuid4()
    vol_surface_id = uuid.uuid4()
    model_id = uuid.uuid4()
    swaption = _swaption_row(
        swaption_id=swaption_id,
        request_json={
            "pricing": {
                "curve_set_id": str(curve_set_id),
                "vol_surface_id": str(vol_surface_id),
                "swaption_model_id": str(model_id),
            }
        },
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.swaptions" in sql:
            return [swaption]
        if "FROM app.curve_sets" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = SwaptionPriceRequest(
        swaption_id=swaption_id,
        swaption=None,
        curves=None,
        vol_surface=None,
        swaption_model=None,
        as_of=date(2026, 5, 13),
        snapshot_id=None,
    )
    with pytest.raises(SwaptionCurveSetNotFoundError) as excinfo:
        await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert excinfo.value.code == "swaption_curve_set_not_found"
    assert excinfo.value.status_code == HTTPStatus.NOT_FOUND


async def test_missing_vol_surface_raises_swaption_vol_surface_not_found(
    fake_ro_engine: FakeEngine,
) -> None:
    swaption_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    vol_surface_id = uuid.uuid4()
    model_id = uuid.uuid4()
    swaption = _swaption_row(
        swaption_id=swaption_id,
        request_json={
            "pricing": {
                "curves": [{"id": str(curve_id)}],
                "vol_surface_id": str(vol_surface_id),
                "swaption_model_id": str(model_id),
            }
        },
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.swaptions" in sql:
            return [swaption]
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.vol_surfaces" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = SwaptionPriceRequest(
        swaption_id=swaption_id,
        swaption=None,
        curves=None,
        vol_surface=None,
        swaption_model=None,
        as_of=date(2026, 5, 13),
        snapshot_id=None,
    )
    with pytest.raises(SwaptionVolSurfaceNotFoundError) as excinfo:
        await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert excinfo.value.code == "swaption_vol_surface_not_found"


async def test_missing_swaption_model_raises_swaption_model_not_found(
    fake_ro_engine: FakeEngine,
) -> None:
    swaption_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    vol_surface_id = uuid.uuid4()
    model_id = uuid.uuid4()
    swaption = _swaption_row(
        swaption_id=swaption_id,
        request_json={
            "pricing": {
                "curves": [{"id": str(curve_id)}],
                "vol_surface_id": str(vol_surface_id),
                "swaption_model_id": str(model_id),
            }
        },
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.swaptions" in sql:
            return [swaption]
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.vol_surfaces" in sql:
            return [_vol_surface_row(vol_surface_id=vol_surface_id)]
        if "FROM app.swaption_models" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = SwaptionPriceRequest(
        swaption_id=swaption_id,
        swaption=None,
        curves=None,
        vol_surface=None,
        swaption_model=None,
        as_of=date(2026, 5, 13),
        snapshot_id=None,
    )
    with pytest.raises(SwaptionModelNotFoundError) as excinfo:
        await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert excinfo.value.code == "swaption_model_not_found"


async def test_missing_referenced_curve_raises_resolution_failed(
    fake_ro_engine: FakeEngine,
) -> None:
    swaption_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    swaption = _swaption_row(
        swaption_id=swaption_id,
        request_json={
            "pricing": {
                "curves": [{"id": str(curve_id)}],
                "vol_surface_id": str(uuid.uuid4()),
                "swaption_model_id": str(uuid.uuid4()),
            }
        },
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.swaptions" in sql:
            return [swaption]
        if "FROM app.curves" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = SwaptionPriceRequest(
        swaption_id=swaption_id,
        swaption=None,
        curves=None,
        vol_surface=None,
        swaption_model=None,
        as_of=date(2026, 5, 13),
        snapshot_id=None,
    )
    with pytest.raises(SwaptionCurveResolutionFailedError) as excinfo:
        await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert excinfo.value.code == "swaption_curve_resolution_failed"


async def test_saved_swaption_without_pricing_block_raises_curve_resolution_failed(
    fake_ro_engine: FakeEngine,
) -> None:
    swaption_id = uuid.uuid4()
    swaption = _swaption_row(swaption_id=swaption_id, request_json={"exercise_type": "European"})

    fake_ro_engine.set_handler(lambda _sql, _params: [swaption])

    request = SwaptionPriceRequest(
        swaption_id=swaption_id,
        swaption=None,
        curves=None,
        vol_surface=None,
        swaption_model=None,
        as_of=date(2026, 5, 13),
        snapshot_id=None,
    )
    with pytest.raises(SwaptionCurveResolutionFailedError) as excinfo:
        await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert excinfo.value.code == "swaption_curve_resolution_failed"


async def test_saved_swaption_with_no_vol_surface_raises_surface_resolution_failed(
    fake_ro_engine: FakeEngine,
) -> None:
    swaption_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    swaption = _swaption_row(
        swaption_id=swaption_id,
        request_json={
            "pricing": {
                "curves": [{"id": str(curve_id)}],
                # no vol_surface_id, no vol_surfaces list
                "swaption_model_id": str(uuid.uuid4()),
            }
        },
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.swaptions" in sql:
            return [swaption]
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = SwaptionPriceRequest(
        swaption_id=swaption_id,
        swaption=None,
        curves=None,
        vol_surface=None,
        swaption_model=None,
        as_of=date(2026, 5, 13),
        snapshot_id=None,
    )
    with pytest.raises(SwaptionSurfaceResolutionFailedError) as excinfo:
        await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert excinfo.value.code == "swaption_surface_resolution_failed"


async def test_saved_inline_vol_surface_without_kind_raises_surface_resolution_failed(
    fake_ro_engine: FakeEngine,
) -> None:
    """Inline-but-malformed ``pricing.vol_surfaces[0]`` should 422, not 500."""

    swaption_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    swaption = _swaption_row(
        swaption_id=swaption_id,
        request_json={
            "pricing": {
                "curves": [{"id": str(curve_id)}],
                "vol_surfaces": [{"axes_expiries": []}],  # missing kind
                "swaption_model_id": str(uuid.uuid4()),
            }
        },
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.swaptions" in sql:
            return [swaption]
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = SwaptionPriceRequest(
        swaption_id=swaption_id,
        swaption=None,
        curves=None,
        vol_surface=None,
        swaption_model=None,
        as_of=date(2026, 5, 13),
        snapshot_id=None,
    )
    with pytest.raises(SwaptionSurfaceResolutionFailedError):
        await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))


async def test_saved_inline_model_without_kind_raises_invalid_request(
    fake_ro_engine: FakeEngine,
) -> None:
    """Inline-but-malformed ``pricing.models[0]`` should 400 ``swaption_invalid_request``."""

    swaption_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    vol_surface_id = uuid.uuid4()
    swaption = _swaption_row(
        swaption_id=swaption_id,
        request_json={
            "pricing": {
                "curves": [{"id": str(curve_id)}],
                "vol_surface_id": str(vol_surface_id),
                "models": [{"hw_a": 0.05}],  # missing kind
            }
        },
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.swaptions" in sql:
            return [swaption]
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.vol_surfaces" in sql:
            return [_vol_surface_row(vol_surface_id=vol_surface_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = SwaptionPriceRequest(
        swaption_id=swaption_id,
        swaption=None,
        curves=None,
        vol_surface=None,
        swaption_model=None,
        as_of=date(2026, 5, 13),
        snapshot_id=None,
    )
    with pytest.raises(SwaptionInvalidRequestError) as excinfo:
        await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert excinfo.value.code == "swaption_invalid_request"


# ---------------------------------------------------------------------------
# Snapshot pinning (mirrors swap_ir; the snapshot logic is duplicated
# verbatim per the 07-plan's "do not prematurely factor" rule)
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

    request = _inline_request(curve_id=curve_id, snapshot_id=snapshot_id)
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

    request = _inline_request(curve_id=curve_id, snapshot_id=snapshot_id)
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

    request = _inline_request(curve_id=curve_id, snapshot_id=snapshot_id)
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

    request = _inline_request(curve_id=curve_id, snapshot_id=snapshot_id)
    with pytest.raises(SwaptionSnapshotNotFoundError) as excinfo:
        await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert excinfo.value.code == "swaption_snapshot_not_found"


# ---------------------------------------------------------------------------
# Cross-cutting invariants
# ---------------------------------------------------------------------------


async def test_every_read_carries_owner_uid_and_soft_delete_filter(
    fake_ro_engine: FakeEngine,
) -> None:
    """All five SQL sites must filter by ``owner_uid`` + ``deleted_at IS NULL``.

    Pin every read site so a regression that drops one filter shows
    up as a test failure rather than as a cross-tenant /
    soft-delete-visibility bug (north-star #5).
    """

    swaption_id = uuid.uuid4()
    curve_set_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    vol_surface_id = uuid.uuid4()
    model_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()

    swaption = _swaption_row(
        swaption_id=swaption_id,
        request_json={
            "pricing": {
                "curve_set_id": str(curve_set_id),
                "vol_surface_id": str(vol_surface_id),
                "swaption_model_id": str(model_id),
            }
        },
    )
    curve_set = _curve_set_row(curve_set_id=curve_set_id, refs=[{"curve_id": str(curve_id)}])
    curve = _curve_row(curve_id=curve_id)
    vol_surface = _vol_surface_row(vol_surface_id=vol_surface_id)
    model = _swaption_model_row(model_id=model_id)
    snapshot = _snapshot_row(snapshot_id=snapshot_id, content={})

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.swaptions" in sql:
            return [swaption]
        if "FROM app.curve_sets" in sql:
            return [curve_set]
        if "FROM app.curves" in sql:
            return [curve]
        if "FROM app.vol_surfaces" in sql:
            return [vol_surface]
        if "FROM app.swaption_models" in sql:
            return [model]
        if "FROM app.snapshots" in sql:
            return [snapshot]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = SwaptionPriceRequest(
        swaption_id=swaption_id,
        swaption=None,
        curves=None,
        vol_surface=None,
        swaption_model=None,
        as_of=date(2026, 5, 13),
        snapshot_id=snapshot_id,
    )
    await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    sites = {
        "app.swaptions": False,
        "app.curve_sets": False,
        "app.curves": False,
        "app.vol_surfaces": False,
        "app.swaption_models": False,
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


# ---------------------------------------------------------------------------
# underlying-swap float index resolution (drives tenor→frequency)
# ---------------------------------------------------------------------------


def _index_row(*, index_id: uuid.UUID, tenor_months: int = 3) -> dict[str, Any]:
    return {
        "id": str(index_id),
        "name": "EUR-EURIBOR-3M",
        "kind": "IborIndex",
        "currency": "EUR",
        "calendar": "TARGET",
        "day_counter": "Actual/360",
        "body": {"tenor": {"n": tenor_months, "unit": "Months"}},
    }


def _inline_index_ref(*, n: int, unit: str = "Months") -> IndexRef:
    return IndexRef(
        id=None,
        name=f"EUR-{n}{unit[0]}",
        kind="IborIndex",
        currency="EUR",
        calendar="TARGET",
        day_counter="Actual/360",
        body={"tenor": {"n": n, "unit": unit}},
    )


def _freq_for(index: Any) -> int:
    """Feed a resolved index through the *production* float_leg_frequency."""

    resolved = ResolvedMarketData(as_of="2025-01-15", curves=(), quotes=(), index=index)
    return float_leg_frequency(resolved)


async def test_no_index_leaves_resolved_index_none_and_default_frequency(
    fake_ro_engine: FakeEngine,
) -> None:
    """Back-compat: omitting ``index`` → ``None`` → Semiannual (today's behaviour)."""
    curve_id = uuid.uuid4()
    curve = _curve_row(curve_id=curve_id)

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [curve]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = _inline_request(curve_id=curve_id)
    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    assert out.index is None
    # No app.indices read when the field is absent.
    assert all("FROM app.indices" not in rec.sql for rec in fake_ro_engine.recordings)
    assert _freq_for(out.index) == Frequency.Semiannual


async def test_inline_index_resolves_and_float_frequency_follows_tenor(
    fake_ro_engine: FakeEngine,
) -> None:
    """3M inline index → quarterly float leg; 6M → semiannual (activation)."""
    curve_id = uuid.uuid4()
    curve = _curve_row(curve_id=curve_id)

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [curve]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    req_3m = _inline_request(curve_id=curve_id)
    req_3m = req_3m.model_copy(update={"index": _inline_index_ref(n=3)})
    out_3m = await assemble(req_3m, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert out_3m.index is not None
    assert out_3m.index.body == {"tenor": {"n": 3, "unit": "Months"}}
    # Inline index needs no DB read.
    assert all("FROM app.indices" not in rec.sql for rec in fake_ro_engine.recordings)
    assert _freq_for(out_3m.index) == Frequency.Quarterly

    req_6m = _inline_request(curve_id=curve_id)
    req_6m = req_6m.model_copy(update={"index": _inline_index_ref(n=6)})
    out_6m = await assemble(req_6m, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert out_6m.index is not None
    assert _freq_for(out_6m.index) == Frequency.Semiannual


async def test_index_id_ref_loads_app_indices(
    fake_ro_engine: FakeEngine,
) -> None:
    """An ``index.id`` ref loads ``app.indices`` (owner-scoped, deleted_at guard)."""
    curve_id = uuid.uuid4()
    index_id = uuid.uuid4()
    curve = _curve_row(curve_id=curve_id)
    index = _index_row(index_id=index_id, tenor_months=3)

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [curve]
        if "FROM app.indices" in sql:
            return [index]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = _inline_request(curve_id=curve_id)
    request = request.model_copy(update={"index": IndexRef(id=index_id)})
    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    assert out.index is not None
    assert out.index.id == index_id
    assert _freq_for(out.index) == Frequency.Quarterly
    idx_reads = [rec for rec in fake_ro_engine.recordings if "FROM app.indices" in rec.sql]
    assert len(idx_reads) == 1
    assert "owner_uid = :owner_uid" in idx_reads[0].sql
    assert "deleted_at IS NULL" in idx_reads[0].sql
    assert idx_reads[0].params["owner_uid"] == OWNER


async def test_index_id_ref_missing_raises_index_not_found(
    fake_ro_engine: FakeEngine,
) -> None:
    """A dangling ``index.id`` → 404 ``swaption_index_not_found``."""
    curve_id = uuid.uuid4()
    index_id = uuid.uuid4()
    curve = _curve_row(curve_id=curve_id)

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [curve]
        if "FROM app.indices" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = _inline_request(curve_id=curve_id)
    request = request.model_copy(update={"index": IndexRef(id=index_id)})
    with pytest.raises(SwaptionIndexNotFoundError) as excinfo:
        await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert excinfo.value.status_code == HTTPStatus.NOT_FOUND
    assert excinfo.value.code == "swaption_index_not_found"


async def test_inline_index_missing_body_raises_resolution_failed(
    fake_ro_engine: FakeEngine,
) -> None:
    """An inline index (id=None) with no body → 422 ``swaption_index_resolution_failed``.

    Constructed via ``model_construct`` to bypass the pydantic branch validator
    so the assembler's own the error-code convention guard is exercised directly.
    """
    curve_id = uuid.uuid4()
    curve = _curve_row(curve_id=curve_id)

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [curve]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = _inline_request(curve_id=curve_id)
    bad_ref = IndexRef.model_construct(id=None, kind=None, body=None)
    request = request.model_copy(update={"index": bad_ref})
    with pytest.raises(SwaptionIndexResolutionFailedError) as excinfo:
        await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "swaption_index_resolution_failed"
