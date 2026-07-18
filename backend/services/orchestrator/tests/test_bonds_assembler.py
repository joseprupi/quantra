"""Unit tests for ``pricing/bonds/assembler.py``.

The assembler ships two public entry points (``assemble_fixed`` /
``assemble_floating``) that share most of their plumbing and diverge
only where the floating-rate variant needs an extra curve role
(projection) plus an index. The two entry points are tested
together so the shared SQL sites + shared branches are exercised
once per variant.

Coverage:

* Inline mode (``bond=...``) skips ``app.bonds_*`` entirely.
* By-reference mode (``bond_id=...``) hits ``app.bonds_*`` once
  with the right WHERE clause (owner_uid + deleted_at IS NULL).
* Soft-deleted / cross-tenant / missing bond rows surface as
  ``bond_fixed_not_found`` / ``bond_floating_not_found`` (404).
* ``request.curves`` override wins over the saved bond's pricing
  block.
* ``pricing.discount_curve_id`` loads ``app.curves`` directly.
* ``pricing.curve_set_id`` loads ``app.curve_sets`` and projects
  ``body.curve_refs`` into curve ids, then loads each from
  ``app.curves``.
* ``pricing.curves`` (per-curve refs or inline definitions) is the
  fallback when no curve set is pinned.
* Floating role mapping: explicit ``role`` markers,
  ``use_same_curve`` shorthand, position-based fallback, and the
  per-role 404s (``bond_discount_curve_not_found`` vs.
  ``bond_projection_curve_not_found``).
* Floating index resolution: request override → saved bond
  pricing block → top-level index_id → ``bond_index_not_found``
  (404) or ``bond_curve_resolution_failed`` (422) for inline /
  missing shapes.
* ``snapshot_id`` loads ``app.snapshots`` and projects its
  content into the snapshot's ``pins`` map.
* Snapshot misses surface as ``bond_snapshot_not_found`` (404).
* batching hook: assembler output carries ``curve_set_id`` so the
  route handler can pack it into ``shared_inputs``.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_orchestrator.pricing.bonds.assembler import (
    FixedAssemblerOutput,
    FloatingAssemblerOutput,
    assemble_fixed,
    assemble_floating,
    collect_curves,
)
from quantra_orchestrator.pricing.bonds.errors import (
    BondCurveResolutionFailedError,
    BondDiscountCurveNotFoundError,
    BondFixedNotFoundError,
    BondFloatingNotFoundError,
    BondIndexNotFoundError,
    BondProjectionCurveNotFoundError,
    BondSnapshotNotFoundError,
)
from quantra_orchestrator.pricing.bonds.models import (
    CurveRef,
    FixedBondPriceRequest,
    FloatingBondPriceRequest,
    IndexRef,
    ResolvedCurve,
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
        "day_counter": "Actual/360",
        "helper_kind": "Discount",
        "reference_date": date(2026, 5, 13),
        "points": [{"tenor": "1Y", "quote_id": quote_id}],
        "body": {"interpolator": "Cubic"},
    }


def _bond_row(
    *,
    bond_id: uuid.UUID,
    request_json: dict[str, Any],
    name: str = "demo-bond",
) -> dict[str, Any]:
    return {"id": str(bond_id), "name": name, "request": request_json}


def _curve_set_row(*, curve_set_id: uuid.UUID, refs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": str(curve_set_id),
        "name": "USD set",
        "body": {"curve_refs": refs},
    }


def _index_row(
    *,
    index_id: uuid.UUID,
    name: str = "USD-SOFR",
    kind: str = "OvernightIndex",
) -> dict[str, Any]:
    return {
        "id": str(index_id),
        "name": name,
        "kind": kind,
        "currency": "USD",
        "calendar": "UnitedStates::GovernmentBond",
        "day_counter": "Actual/360",
        "body": {"fixingDays": 2},
    }


def _snapshot_row(*, snapshot_id: uuid.UUID, content: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(snapshot_id), "name": "EOD-USD", "content": content}


def _ro(engine: FakeEngine) -> AsyncEngine:
    return cast(AsyncEngine, engine)


# ---------------------------------------------------------------------------
# Fixed-rate: inline + by-ref + 404 + snapshot
# ---------------------------------------------------------------------------


async def test_fixed_inline_does_not_touch_bonds_fixed(
    fake_ro_engine: FakeEngine,
) -> None:
    curve_id = uuid.uuid4()
    fake_ro_engine.set_handler(lambda _sql, _params: [_curve_row(curve_id=curve_id)])

    request = FixedBondPriceRequest(
        bond_id=None,
        bond={"face_amount": 100.0},
        curves=[CurveRef(id=curve_id)],
        as_of=date(2026, 5, 13),
        snapshot_id=None,
    )
    out = await assemble_fixed(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    assert isinstance(out, FixedAssemblerOutput)
    assert out.trade.bond_id is None
    assert out.trade.bond == {"face_amount": 100.0}
    assert out.discount_curve.id == curve_id
    assert out.curve_set_id is None
    assert out.discount_curve_id == curve_id
    assert out.snapshot is None
    assert all("FROM app.bonds_fixed" not in rec.sql for rec in fake_ro_engine.recordings)


async def test_fixed_by_reference_loads_bond_and_curve(
    fake_ro_engine: FakeEngine,
) -> None:
    bond_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    bond = _bond_row(
        bond_id=bond_id,
        request_json={"pricing": {"discount_curve_id": str(curve_id)}},
    )
    curve = _curve_row(curve_id=curve_id)

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.bonds_fixed" in sql:
            return [bond]
        if "FROM app.curves" in sql:
            return [curve]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = FixedBondPriceRequest(bond_id=bond_id, as_of=date(2026, 5, 13))
    out = await assemble_fixed(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    assert out.trade.bond_id == bond_id
    assert out.discount_curve.id == curve_id
    assert out.discount_curve_id == curve_id
    bond_sqls = [rec for rec in fake_ro_engine.recordings if "FROM app.bonds_fixed" in rec.sql]
    assert len(bond_sqls) == 1
    assert "owner_uid" in bond_sqls[0].sql
    assert "deleted_at IS NULL" in bond_sqls[0].sql
    assert bond_sqls[0].params["owner_uid"] == OWNER


async def test_fixed_by_reference_loads_via_curve_set(
    fake_ro_engine: FakeEngine,
) -> None:
    bond_id = uuid.uuid4()
    curve_set_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    bond = _bond_row(
        bond_id=bond_id,
        request_json={"pricing": {"curve_set_id": str(curve_set_id)}},
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.bonds_fixed" in sql:
            return [bond]
        if "FROM app.curve_sets" in sql:
            return [
                _curve_set_row(
                    curve_set_id=curve_set_id,
                    refs=[{"curve_id": str(curve_id)}],
                )
            ]
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = FixedBondPriceRequest(bond_id=bond_id, as_of=date(2026, 5, 13))
    out = await assemble_fixed(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    assert out.curve_set_id == curve_set_id
    assert out.discount_curve_id == curve_id
    assert out.discount_curve.id == curve_id


async def test_fixed_bond_id_missing_returns_bond_fixed_not_found(
    fake_ro_engine: FakeEngine,
) -> None:
    fake_ro_engine.set_handler(lambda _sql, _params: [])
    request = FixedBondPriceRequest(bond_id=uuid.uuid4(), as_of=date(2026, 5, 13))

    with pytest.raises(BondFixedNotFoundError) as excinfo:
        await assemble_fixed(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert excinfo.value.code == "bond_fixed_not_found"
    assert excinfo.value.status_code == 404


async def test_fixed_discount_curve_missing_returns_404(
    fake_ro_engine: FakeEngine,
) -> None:
    bond_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    bond = _bond_row(
        bond_id=bond_id,
        request_json={"pricing": {"discount_curve_id": str(curve_id)}},
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.bonds_fixed" in sql:
            return [bond]
        if "FROM app.curves" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    with pytest.raises(BondDiscountCurveNotFoundError) as excinfo:
        await assemble_fixed(
            FixedBondPriceRequest(bond_id=bond_id, as_of=date(2026, 5, 13)),
            owner_uid=OWNER,
            ro_engine=_ro(fake_ro_engine),
        )
    assert excinfo.value.code == "bond_discount_curve_not_found"


async def test_fixed_missing_pricing_block_raises_curve_resolution_failed(
    fake_ro_engine: FakeEngine,
) -> None:
    bond_id = uuid.uuid4()
    bond = _bond_row(bond_id=bond_id, request_json={"face_amount": 100.0})
    fake_ro_engine.set_handler(lambda _sql, _params: [bond])

    with pytest.raises(BondCurveResolutionFailedError) as excinfo:
        await assemble_fixed(
            FixedBondPriceRequest(bond_id=bond_id, as_of=date(2026, 5, 13)),
            owner_uid=OWNER,
            ro_engine=_ro(fake_ro_engine),
        )
    assert excinfo.value.code == "bond_curve_resolution_failed"
    assert excinfo.value.status_code == 422


async def test_fixed_snapshot_loaded_and_pinned(
    fake_ro_engine: FakeEngine,
) -> None:
    curve_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.snapshots" in sql:
            return [_snapshot_row(snapshot_id=snapshot_id, content={"USD.IRS.1Y": 4.25})]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    out = await assemble_fixed(
        FixedBondPriceRequest(
            bond_id=None,
            bond={"face_amount": 100.0},
            curves=[CurveRef(id=curve_id)],
            as_of=date(2026, 5, 13),
            snapshot_id=snapshot_id,
        ),
        owner_uid=OWNER,
        ro_engine=_ro(fake_ro_engine),
    )
    assert out.snapshot is not None
    assert out.snapshot.pins == {"USD.IRS.1Y": {"value": 4.25, "source": None}}


async def test_fixed_snapshot_missing_returns_snapshot_not_found(
    fake_ro_engine: FakeEngine,
) -> None:
    curve_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.snapshots" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    with pytest.raises(BondSnapshotNotFoundError) as excinfo:
        await assemble_fixed(
            FixedBondPriceRequest(
                bond_id=None,
                bond={"face_amount": 100.0},
                curves=[CurveRef(id=curve_id)],
                as_of=date(2026, 5, 13),
                snapshot_id=uuid.uuid4(),
            ),
            owner_uid=OWNER,
            ro_engine=_ro(fake_ro_engine),
        )
    assert excinfo.value.code == "bond_snapshot_not_found"


# ---------------------------------------------------------------------------
# Floating-rate: inline + by-ref + role mapping + 404s
# ---------------------------------------------------------------------------


async def test_floating_inline_with_two_distinct_curves(
    fake_ro_engine: FakeEngine,
) -> None:
    """Inline mode with two role-tagged curves + inline index.

    The two refs use ``role=discount`` / ``role=projection`` on
    ``body`` so the assembler maps them deterministically without
    needing to consult any saved bond.
    """

    discount_id = uuid.uuid4()
    projection_id = uuid.uuid4()
    discount = _curve_row(curve_id=discount_id, name="USD-OIS")
    projection = _curve_row(curve_id=projection_id, name="USD-SOFR-3M", quote_id="USD.IRS.3M")

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            curve_id = params["curve_id"]
            if curve_id == str(discount_id):
                return [discount]
            if curve_id == str(projection_id):
                return [projection]
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = FloatingBondPriceRequest(
        bond_id=None,
        bond={"face_amount": 100.0},
        curves=[
            CurveRef(id=discount_id, body={"role": "discount"}),
            CurveRef(id=projection_id, body={"role": "projection"}),
        ],
        index=IndexRef(
            kind="OvernightIndex",
            body={"fixingDays": 2},
            name="ad-hoc",
        ),
        as_of=date(2026, 5, 13),
        snapshot_id=None,
    )
    out = await assemble_floating(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    assert isinstance(out, FloatingAssemblerOutput)
    assert out.discount_curve.id == discount_id
    assert out.projection_curve.id == projection_id
    assert out.discount_curve_id == discount_id
    assert out.projection_curve_id == projection_id
    assert out.index.kind == "OvernightIndex"
    assert out.index_id is None


async def test_floating_by_reference_uses_pricing_block_fields(
    fake_ro_engine: FakeEngine,
) -> None:
    bond_id = uuid.uuid4()
    discount_id = uuid.uuid4()
    projection_id = uuid.uuid4()
    index_id = uuid.uuid4()
    bond = _bond_row(
        bond_id=bond_id,
        request_json={
            "pricing": {
                "discount_curve_id": str(discount_id),
                "forecast_curve_id": str(projection_id),
                "index_id": str(index_id),
            }
        },
    )

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.bonds_floating" in sql:
            return [bond]
        if "FROM app.curves" in sql:
            cid = params["curve_id"]
            if cid == str(discount_id):
                return [_curve_row(curve_id=discount_id, name="USD-OIS")]
            if cid == str(projection_id):
                return [
                    _curve_row(
                        curve_id=projection_id,
                        name="USD-SOFR-3M",
                        quote_id="USD.IRS.3M",
                    )
                ]
            return []
        if "FROM app.indices" in sql:
            return [_index_row(index_id=index_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    out = await assemble_floating(
        FloatingBondPriceRequest(bond_id=bond_id, as_of=date(2026, 5, 13)),
        owner_uid=OWNER,
        ro_engine=_ro(fake_ro_engine),
    )
    assert out.discount_curve_id == discount_id
    assert out.projection_curve_id == projection_id
    assert out.index_id == index_id
    assert out.index.kind == "OvernightIndex"


async def test_floating_use_same_curve_reuses_discount_for_projection(
    fake_ro_engine: FakeEngine,
) -> None:
    """``use_same_curve`` shorthand: one DB curve row plays both roles.

    The saved bond carries only a ``discount_curve_id`` + the
    ``use_same_curve`` flag (no separate forecast curve). The
    assembler must NOT issue a second curve fetch for the projection
    role; it reuses the already-materialised discount curve.
    """

    bond_id = uuid.uuid4()
    discount_id = uuid.uuid4()
    index_id = uuid.uuid4()
    bond = _bond_row(
        bond_id=bond_id,
        request_json={
            "use_same_curve": True,
            "pricing": {
                "discount_curve_id": str(discount_id),
                "index_id": str(index_id),
            },
        },
    )

    curve_fetch_count = 0

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        nonlocal curve_fetch_count
        if "FROM app.bonds_floating" in sql:
            return [bond]
        if "FROM app.curves" in sql:
            curve_fetch_count += 1
            return [_curve_row(curve_id=discount_id)]
        if "FROM app.indices" in sql:
            return [_index_row(index_id=index_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    out = await assemble_floating(
        FloatingBondPriceRequest(bond_id=bond_id, as_of=date(2026, 5, 13)),
        owner_uid=OWNER,
        ro_engine=_ro(fake_ro_engine),
    )
    assert curve_fetch_count == 1
    assert out.discount_curve_id == discount_id
    assert out.projection_curve_id == discount_id
    assert out.discount_curve is out.projection_curve


async def test_floating_bond_id_missing_returns_bond_floating_not_found(
    fake_ro_engine: FakeEngine,
) -> None:
    fake_ro_engine.set_handler(lambda _sql, _params: [])
    with pytest.raises(BondFloatingNotFoundError) as excinfo:
        await assemble_floating(
            FloatingBondPriceRequest(bond_id=uuid.uuid4(), as_of=date(2026, 5, 13)),
            owner_uid=OWNER,
            ro_engine=_ro(fake_ro_engine),
        )
    assert excinfo.value.code == "bond_floating_not_found"


async def test_floating_missing_projection_curve_role_raises_422(
    fake_ro_engine: FakeEngine,
) -> None:
    """Saved bond with only a discount curve and no use_same_curve → 422.

    The 422's ``details`` must carry ``role=projection`` so the
    operator knows which slot was empty (single code per
    bundle stage, role-disambiguated via details).
    """

    bond_id = uuid.uuid4()
    discount_id = uuid.uuid4()
    bond = _bond_row(
        bond_id=bond_id,
        request_json={
            "pricing": {"discount_curve_id": str(discount_id)},
        },
    )
    fake_ro_engine.set_handler(lambda _sql, _params: [bond])

    with pytest.raises(BondCurveResolutionFailedError) as excinfo:
        await assemble_floating(
            FloatingBondPriceRequest(bond_id=bond_id, as_of=date(2026, 5, 13)),
            owner_uid=OWNER,
            ro_engine=_ro(fake_ro_engine),
        )
    err = excinfo.value
    assert err.code == "bond_curve_resolution_failed"
    assert err.status_code == 422
    assert err.details is not None
    assert any(d.get("role") == "projection" for d in err.details)


async def test_floating_projection_curve_missing_returns_per_role_404(
    fake_ro_engine: FakeEngine,
) -> None:
    """When the projection curve row is invisible, the per-role 404 fires.

    Distinct from the shared 422 — a 404 for the projection curve
    role means "the row you referenced does not exist", which the
    client fixes differently than "we have a row but its shape is
    unusable" (the 422 case).
    """

    bond_id = uuid.uuid4()
    discount_id = uuid.uuid4()
    projection_id = uuid.uuid4()
    index_id = uuid.uuid4()
    bond = _bond_row(
        bond_id=bond_id,
        request_json={
            "pricing": {
                "discount_curve_id": str(discount_id),
                "forecast_curve_id": str(projection_id),
                "index_id": str(index_id),
            }
        },
    )

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.bonds_floating" in sql:
            return [bond]
        if "FROM app.curves" in sql:
            if params["curve_id"] == str(discount_id):
                return [_curve_row(curve_id=discount_id)]
            return []
        if "FROM app.indices" in sql:
            return [_index_row(index_id=index_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    with pytest.raises(BondProjectionCurveNotFoundError) as excinfo:
        await assemble_floating(
            FloatingBondPriceRequest(bond_id=bond_id, as_of=date(2026, 5, 13)),
            owner_uid=OWNER,
            ro_engine=_ro(fake_ro_engine),
        )
    assert excinfo.value.code == "bond_projection_curve_not_found"


async def test_floating_index_missing_returns_bond_index_not_found(
    fake_ro_engine: FakeEngine,
) -> None:
    bond_id = uuid.uuid4()
    discount_id = uuid.uuid4()
    projection_id = uuid.uuid4()
    index_id = uuid.uuid4()
    bond = _bond_row(
        bond_id=bond_id,
        request_json={
            "pricing": {
                "discount_curve_id": str(discount_id),
                "forecast_curve_id": str(projection_id),
                "index_id": str(index_id),
            }
        },
    )

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.bonds_floating" in sql:
            return [bond]
        if "FROM app.curves" in sql:
            cid = params["curve_id"]
            if cid == str(discount_id):
                return [_curve_row(curve_id=discount_id)]
            if cid == str(projection_id):
                return [_curve_row(curve_id=projection_id, quote_id="USD.IRS.3M")]
            return []
        if "FROM app.indices" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    with pytest.raises(BondIndexNotFoundError) as excinfo:
        await assemble_floating(
            FloatingBondPriceRequest(bond_id=bond_id, as_of=date(2026, 5, 13)),
            owner_uid=OWNER,
            ro_engine=_ro(fake_ro_engine),
        )
    assert excinfo.value.code == "bond_index_not_found"


async def test_floating_curve_set_with_role_markers(
    fake_ro_engine: FakeEngine,
) -> None:
    """Curve-set refs carry ``role`` markers that map to discount/projection.

    Mirrors how the saved-shape stores curve set entries with role
    annotations. The assembler reads ``role`` off each
    ``curve_refs[]`` entry and uses it to discriminate.
    """

    bond_id = uuid.uuid4()
    curve_set_id = uuid.uuid4()
    discount_id = uuid.uuid4()
    projection_id = uuid.uuid4()
    index_id = uuid.uuid4()
    bond = _bond_row(
        bond_id=bond_id,
        request_json={
            "pricing": {
                "curve_set_id": str(curve_set_id),
                "index_id": str(index_id),
            }
        },
    )

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.bonds_floating" in sql:
            return [bond]
        if "FROM app.curve_sets" in sql:
            return [
                _curve_set_row(
                    curve_set_id=curve_set_id,
                    refs=[
                        {"curve_id": str(projection_id), "role": "projection"},
                        {"curve_id": str(discount_id), "role": "discount"},
                    ],
                )
            ]
        if "FROM app.curves" in sql:
            cid = params["curve_id"]
            if cid == str(discount_id):
                return [_curve_row(curve_id=discount_id)]
            if cid == str(projection_id):
                return [_curve_row(curve_id=projection_id, quote_id="USD.IRS.3M")]
            return []
        if "FROM app.indices" in sql:
            return [_index_row(index_id=index_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    out = await assemble_floating(
        FloatingBondPriceRequest(bond_id=bond_id, as_of=date(2026, 5, 13)),
        owner_uid=OWNER,
        ro_engine=_ro(fake_ro_engine),
    )
    assert out.curve_set_id == curve_set_id
    assert out.discount_curve_id == discount_id
    assert out.projection_curve_id == projection_id


# ---------------------------------------------------------------------------
# collect_curves helper
# ---------------------------------------------------------------------------


def _resolved(id_: uuid.UUID | None) -> ResolvedCurve:
    return ResolvedCurve(id=id_, name="c", points=[], body={})


def test_collect_curves_dedupes_by_id_and_drops_none() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    curves = collect_curves(None, _resolved(a), _resolved(a), _resolved(b))
    assert [c.id for c in curves] == [a, b]


def test_collect_curves_preserves_order_of_first_appearance() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    curves = collect_curves(_resolved(b), _resolved(a), _resolved(b))
    assert [c.id for c in curves] == [b, a]


def test_collect_curves_keeps_inline_only_curves_separate() -> None:
    """Inline-only curves (id=None) are never deduplicated.

    Two inline curves with the same content still occupy two slots
    in the walker's input; the dedupe key is the soft-ref id, not
    the content (which we don't hash).
    """

    curves = collect_curves(_resolved(None), _resolved(None))
    assert len(curves) == 2
