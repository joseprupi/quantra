"""Unit tests for ``CrudRepository`` / ``ImmutableRepository``.

These tests cover the privilege boundary: every read and write the
repository emits *must* carry ``WHERE owner_uid = :owner_uid``. A
missing clause is a tenant-isolation bug, not a performance one
(north_star.md). The matrix walks every named entity + a synthetic
soft-delete-less business-key spec + pricing_history.

We exercise the repository directly (not via FastAPI) so the assertion
surface stays focused on SQL shape and parameter binding.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from quantra_orchestrator.data.errors import (
    NameConflictError,
    NotFoundError,
)
from quantra_orchestrator.data.repository import (
    CrudRepository,
    EntitySpec,
    ImmutableRepository,
)
from quantra_orchestrator.data.specs import (
    CURVES_SPEC,
    INDICES_SPEC,
    NAMED_ENTITY_SPECS,
    PRICING_HISTORY_SPEC,
    SWAPS_IR_SPEC,
)

# The fixture defined in conftest.py provides FakeEngine; we reference
# the class via the fixture by way of type aliasing through the
# imported sentinel — pytest passes the actual instance at call time.


@pytest.fixture
def owner_a() -> str:
    return "user-A-uid"


@pytest.fixture
def owner_b() -> str:
    return "user-B-uid"


def _now() -> datetime:
    return datetime(2026, 5, 15, 9, 0, 0)


def _projection_row(spec: EntitySpec, **overrides: Any) -> dict[str, Any]:
    """Build a row matching the spec's projection shape."""

    row: dict[str, Any] = {"id": str(uuid.uuid4())}
    for column in spec.scalar_columns:
        row[column] = overrides.get(column, f"sample-{column}")
    for column in spec.jsonb_columns:
        row[column] = overrides.get(column, [] if column in {"points", "entries"} else {})
    row["created_at"] = _now()
    if spec.has_updated_at:
        row["updated_at"] = _now()
    if spec.has_soft_delete:
        row["deleted_at"] = overrides.get("deleted_at")
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# Cross-cutting: every entity's read paths must scope by owner_uid.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", NAMED_ENTITY_SPECS, ids=lambda s: s.table)
async def test_list_query_filters_by_owner_uid(
    spec: EntitySpec,
    owner_a: str,
    fake_ro_engine: Any,
) -> None:
    """Privilege-boundary check: ``SELECT`` always filters ``owner_uid``."""

    fake_ro_engine.set_handler(lambda _sql, _params: [_projection_row(spec)])
    repo = CrudRepository(spec, ro_engine=fake_ro_engine)

    await repo.list(owner_uid=owner_a, limit=10, offset=0)

    assert len(fake_ro_engine.recordings) == 1
    rec = fake_ro_engine.recordings[0]
    assert rec.mode == "read"
    assert "WHERE t.owner_uid = :owner_uid" in rec.sql
    if spec.has_soft_delete:
        assert "t.deleted_at IS NULL" in rec.sql
    assert rec.params["owner_uid"] == owner_a
    assert rec.params["limit"] == 10
    assert rec.params["offset"] == 0


@pytest.mark.parametrize("spec", NAMED_ENTITY_SPECS, ids=lambda s: s.table)
async def test_get_query_filters_by_owner_uid_and_visibility(
    spec: EntitySpec,
    owner_a: str,
    fake_ro_engine: Any,
) -> None:
    resource_id = uuid.uuid4()
    fake_ro_engine.set_handler(lambda _sql, _params: [_projection_row(spec, id=str(resource_id))])
    repo = CrudRepository(spec, ro_engine=fake_ro_engine)

    row = await repo.get(owner_uid=owner_a, resource_id=resource_id)
    assert row["id"] == str(resource_id)

    rec = fake_ro_engine.recordings[0]
    assert rec.mode == "read"
    assert "t.id = :resource_id" in rec.sql
    assert "t.owner_uid = :owner_uid" in rec.sql
    if spec.has_soft_delete:
        assert "t.deleted_at IS NULL" in rec.sql
    assert rec.params == {"owner_uid": owner_a, "resource_id": str(resource_id)}


# ---------------------------------------------------------------------------
# Create / patch
# ---------------------------------------------------------------------------


async def test_create_index_writes_owner_uid_and_jsonb_cast(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    row = _projection_row(INDICES_SPEC, name="EUR-EURIBOR-3M", kind="IBOR")
    fake_rw_engine.set_handler(lambda _sql, _params: [row])

    repo = CrudRepository(INDICES_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)
    result = await repo.create(
        owner_uid=owner_a,
        values={
            "name": "EUR-EURIBOR-3M",
            "kind": "IBOR",
            "currency": "EUR",
            "calendar": "TARGET",
            "day_counter": "Actual/360",
            "body": {"foo": "bar"},
        },
    )

    assert result["name"] == "EUR-EURIBOR-3M"
    rec = fake_rw_engine.recordings[0]
    assert rec.mode == "write"
    assert "INSERT INTO app.indices" in rec.sql
    # JSONB cast goes through ``CAST(:body AS jsonb)`` because
    # SQLAlchemy's ``text()`` parser mangles ``:name::type``.
    assert "CAST(:body AS jsonb)" in rec.sql
    assert rec.params["owner_uid"] == owner_a
    # JSONB column is dumped to a JSON string before binding.
    assert rec.params["body"] == '{"foo": "bar"}'


async def test_create_curve_writes_two_jsonb_columns(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    row = _projection_row(
        CURVES_SPEC,
        name="EUR-OIS",
        points=[],
        body={},
    )
    fake_rw_engine.set_handler(lambda _sql, _params: [row])

    repo = CrudRepository(CURVES_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)
    await repo.create(
        owner_uid=owner_a,
        values={
            "name": "EUR-OIS",
            "currency": "EUR",
            "day_counter": None,
            "helper_kind": None,
            "reference_date": None,
            "points": [{"tenor": "1Y", "rate": 0.02}],
            "body": {"interpolator": "Cubic"},
        },
    )

    rec = fake_rw_engine.recordings[0]
    assert "CAST(:points AS jsonb)" in rec.sql
    assert "CAST(:body AS jsonb)" in rec.sql
    assert rec.params["points"] == '[{"tenor": "1Y", "rate": 0.02}]'
    assert rec.params["body"] == '{"interpolator": "Cubic"}'


async def test_patch_includes_owner_uid_and_visibility(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    resource_id = uuid.uuid4()
    row = _projection_row(CURVES_SPEC, id=str(resource_id))
    fake_rw_engine.set_handler(lambda _sql, _params: [row])

    repo = CrudRepository(CURVES_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)
    await repo.patch(
        owner_uid=owner_a,
        resource_id=resource_id,
        updates={"currency": "EUR", "body": {"interpolator": "Linear"}},
    )

    rec = fake_rw_engine.recordings[0]
    assert rec.mode == "write"
    assert "UPDATE app.curves" in rec.sql
    assert "id = :resource_id" in rec.sql
    assert "owner_uid = :owner_uid" in rec.sql
    assert "deleted_at IS NULL" in rec.sql
    assert "CAST(:body AS jsonb)" in rec.sql
    assert rec.params["owner_uid"] == owner_a
    assert rec.params["resource_id"] == str(resource_id)


async def test_patch_with_empty_updates_returns_current_row(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    resource_id = uuid.uuid4()
    row = _projection_row(CURVES_SPEC, id=str(resource_id))
    fake_ro_engine.set_handler(lambda _sql, _params: [row])

    repo = CrudRepository(CURVES_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)
    result = await repo.patch(owner_uid=owner_a, resource_id=resource_id, updates={})

    assert result["id"] == str(resource_id)
    # No write was performed; only the visibility read.
    assert all(r.mode == "read" for r in fake_ro_engine.recordings)
    assert not fake_rw_engine.recordings


async def test_patch_rejects_unknown_fields(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    repo = CrudRepository(CURVES_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)
    with pytest.raises(KeyError, match="Unknown PATCH fields"):
        await repo.patch(
            owner_uid=owner_a,
            resource_id=uuid.uuid4(),
            updates={"id": "not-allowed"},
        )


async def test_patch_returns_not_found_for_missing_row(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    fake_rw_engine.set_handler(lambda _sql, _params: [])
    repo = CrudRepository(CURVES_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)
    with pytest.raises(NotFoundError):
        await repo.patch(
            owner_uid=owner_a,
            resource_id=uuid.uuid4(),
            updates={"currency": "EUR"},
        )


# ---------------------------------------------------------------------------
# Soft delete + restore
# ---------------------------------------------------------------------------


async def test_soft_delete_sets_deleted_at(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    resource_id = uuid.uuid4()
    fake_rw_engine.set_handler(lambda _sql, _params: [{"id": str(resource_id)}])

    repo = CrudRepository(CURVES_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)
    await repo.soft_delete(owner_uid=owner_a, resource_id=resource_id)

    rec = fake_rw_engine.recordings[0]
    assert rec.mode == "write"
    assert "SET deleted_at = now()" in rec.sql
    assert "owner_uid = :owner_uid" in rec.sql
    assert "deleted_at IS NULL" in rec.sql  # idempotency: only set if still live
    assert rec.params == {"owner_uid": owner_a, "resource_id": str(resource_id)}


async def test_soft_delete_is_idempotent_when_already_deleted(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    resource_id = uuid.uuid4()
    deleted_row = _projection_row(CURVES_SPEC, id=str(resource_id), deleted_at=_now())

    def write_handler(_sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        return []  # UPDATE matched nothing — row already soft-deleted.

    def read_handler(_sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        return [deleted_row]  # _fetch_any sees the soft-deleted row.

    fake_rw_engine.set_handler(write_handler)
    fake_ro_engine.set_handler(read_handler)

    repo = CrudRepository(CURVES_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)
    # Should NOT raise even though no row was updated.
    await repo.soft_delete(owner_uid=owner_a, resource_id=resource_id)


async def test_soft_delete_404s_when_row_missing(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    fake_rw_engine.set_handler(lambda _sql, _params: [])
    fake_ro_engine.set_handler(lambda _sql, _params: [])
    repo = CrudRepository(CURVES_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)
    with pytest.raises(NotFoundError):
        await repo.soft_delete(owner_uid=owner_a, resource_id=uuid.uuid4())


async def test_soft_delete_cascades_wrapper_graph_row(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    """Deleting a wrapper row cascades to its ``appId`` graph sibling.

    a product-table wrapper row (``request.__wrapper__ = true``)
    points at the by-reference save-graph row via ``request.appId``.
    Deleting the wrapper must also soft-delete that sibling — atomically,
    owner-scoped — so no orphan graph row is left behind.
    """

    resource_id = uuid.uuid4()
    app_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "RETURNING" in sql:
            return [
                {
                    "id": str(resource_id),
                    "_wrapper_flag": "true",
                    "_linked_app_id": str(app_id),
                }
            ]
        return []

    fake_rw_engine.set_handler(handler)

    repo = CrudRepository(SWAPS_IR_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)
    await repo.soft_delete(owner_uid=owner_a, resource_id=resource_id)

    all_writes = [r for r in fake_rw_engine.recordings if r.mode == "write"]
    writes = [r for r in all_writes if "entity_versions" not in r.sql]
    version_writes = [r for r in all_writes if "entity_versions" in r.sql]
    assert len(writes) == 2  # wrapper delete + cascaded graph delete
    # 1) main wrapper delete captures the wrapper marker + appId link.
    assert writes[0].params == {"owner_uid": owner_a, "resource_id": str(resource_id)}
    assert "request->>'__wrapper__'" in writes[0].sql
    assert "request->>'appId'" in writes[0].sql
    # 2) cascade delete of the appId sibling, same owner, same table.
    assert "app.swaps_ir" in writes[1].sql
    assert "SET deleted_at = now()" in writes[1].sql
    assert "CAST(:app_id AS uuid)" in writes[1].sql
    assert "owner_uid = :owner_uid" in writes[1].sql
    assert "deleted_at IS NULL" in writes[1].sql
    assert writes[1].params["app_id"] == str(app_id)
    assert writes[1].params["owner_uid"] == owner_a
    # 3) BOTH rows got an audit-trail 'delete' version in the same txn.
    assert len(version_writes) == 2
    assert all(r.params["change_type"] == "delete" for r in version_writes)
    assert {r.params["entity_id"] for r in version_writes} == {
        str(resource_id),
        str(resource_id),  # handler returns the wrapper row for both RETURNINGs
    }


async def test_soft_delete_graph_row_does_not_cascade(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    """A non-wrapper (graph) product row deletes without cascading."""

    resource_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "RETURNING" in sql:
            # Graph/save-graph rows are NOT wrapper-marked and carry no
            # appId of their own.
            return [{"id": str(resource_id), "_wrapper_flag": None, "_linked_app_id": None}]
        return []

    fake_rw_engine.set_handler(handler)

    repo = CrudRepository(SWAPS_IR_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)
    await repo.soft_delete(owner_uid=owner_a, resource_id=resource_id)

    writes = [
        r for r in fake_rw_engine.recordings if r.mode == "write" and "entity_versions" not in r.sql
    ]
    assert len(writes) == 1  # no cascade when the row is not a wrapper


async def test_soft_delete_non_product_spec_has_no_wrapper_cascade(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    """Non-product entities keep the plain single-row delete (no cascade)."""

    resource_id = uuid.uuid4()
    fake_rw_engine.set_handler(lambda _sql, _params: [{"id": str(resource_id)}])

    repo = CrudRepository(CURVES_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)
    await repo.soft_delete(owner_uid=owner_a, resource_id=resource_id)

    writes = [
        r for r in fake_rw_engine.recordings if r.mode == "write" and "entity_versions" not in r.sql
    ]
    assert len(writes) == 1
    assert "__wrapper__" not in writes[0].sql
    assert "appId" not in writes[0].sql


async def test_restore_clears_deleted_at(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    resource_id = uuid.uuid4()
    deleted_row = _projection_row(
        CURVES_SPEC, id=str(resource_id), deleted_at=_now(), name="EUR-OIS"
    )
    restored_row = _projection_row(
        CURVES_SPEC, id=str(resource_id), deleted_at=None, name="EUR-OIS"
    )

    fake_ro_engine.set_handler(lambda _sql, _params: [deleted_row])
    fake_rw_engine.set_handler(lambda _sql, _params: [restored_row])

    repo = CrudRepository(CURVES_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)
    result = await repo.restore(owner_uid=owner_a, resource_id=resource_id)

    assert result["deleted_at"] is None
    rec = fake_rw_engine.recordings[0]
    assert "SET deleted_at = NULL" in rec.sql
    assert "owner_uid = :owner_uid" in rec.sql


async def test_restore_is_idempotent_on_already_live(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    resource_id = uuid.uuid4()
    live_row = _projection_row(CURVES_SPEC, id=str(resource_id), deleted_at=None)
    fake_ro_engine.set_handler(lambda _sql, _params: [live_row])

    repo = CrudRepository(CURVES_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)
    result = await repo.restore(owner_uid=owner_a, resource_id=resource_id)

    assert result["id"] == str(resource_id)
    # No write was attempted because the row was already live.
    assert not fake_rw_engine.recordings


async def test_restore_404s_when_row_missing(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    fake_ro_engine.set_handler(lambda _sql, _params: [])
    repo = CrudRepository(CURVES_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)
    with pytest.raises(NotFoundError):
        await repo.restore(owner_uid=owner_a, resource_id=uuid.uuid4())


async def test_restore_translates_unique_violation_to_name_conflict(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    resource_id = uuid.uuid4()
    deleted_row = _projection_row(
        CURVES_SPEC, id=str(resource_id), deleted_at=_now(), name="EUR-OIS"
    )
    fake_ro_engine.set_handler(lambda _sql, _params: [deleted_row])

    def write_handler(_sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        msg = 'duplicate key value violates unique constraint "uq_curves_owner_name_active"'
        # Wrap in an IntegrityError just like asyncpg/SQLAlchemy would.
        raise IntegrityError(statement=_sql, params=_params, orig=Exception(msg))

    fake_rw_engine.set_handler(write_handler)

    repo = CrudRepository(CURVES_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)
    with pytest.raises(NameConflictError) as excinfo:
        await repo.restore(owner_uid=owner_a, resource_id=resource_id)
    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "name_conflict"


async def test_create_translates_unique_violation_to_name_conflict(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    def write_handler(_sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        msg = 'duplicate key value violates unique constraint "uq_swaps_ir_owner_name_active"'
        raise IntegrityError(statement=_sql, params=_params, orig=Exception(msg))

    fake_rw_engine.set_handler(write_handler)
    repo = CrudRepository(SWAPS_IR_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)
    with pytest.raises(NameConflictError):
        await repo.create(
            owner_uid=owner_a,
            values={"name": "USD 5Y Vanilla", "request": {}},
        )


# ---------------------------------------------------------------------------
# Cross-tenant attempt: user A's WHERE clause never sees user B's rows.
# ---------------------------------------------------------------------------


async def test_get_404s_when_owner_uid_mismatches(
    owner_a: str,
    owner_b: str,
    fake_ro_engine: Any,
) -> None:
    # The handler returns no rows because the SQL's WHERE excludes
    # owner_b's records when the caller is owner_a.
    fake_ro_engine.set_handler(lambda _sql, _params: [])
    repo = CrudRepository(CURVES_SPEC, ro_engine=fake_ro_engine)

    with pytest.raises(NotFoundError):
        await repo.get(owner_uid=owner_a, resource_id=uuid.uuid4())

    rec = fake_ro_engine.recordings[0]
    assert rec.params["owner_uid"] == owner_a
    assert "t.owner_uid = :owner_uid" in rec.sql


# ---------------------------------------------------------------------------
# Soft-delete-less, business-keyed spec (generic capability)
# ---------------------------------------------------------------------------

# ``quotes_saved`` — the last production spec of this shape — was dropped
# in the quote-book consolidation, but the CrudRepository capabilities it exercised
# (soft-delete-less CRUD, an immutable business key, unique-violation →
# 409 translation on a non-``name`` column) are still live code. A
# synthetic spec keeps them covered.
KEYED_SPEC = EntitySpec(
    entity="widget",
    table="widgets",
    scalar_columns=("widget_key",),
    jsonb_columns=("body",),
    has_soft_delete=False,
    name_column="widget_key",
    unique_constraint="uq_widgets_owner_key",
    immutable_after_create=("widget_key",),
)


async def test_keyed_spec_list_does_not_filter_deleted_at(
    owner_a: str,
    fake_ro_engine: Any,
) -> None:
    fake_ro_engine.set_handler(lambda _sql, _params: [])
    repo = CrudRepository(KEYED_SPEC, ro_engine=fake_ro_engine)

    await repo.list(owner_uid=owner_a, limit=5, offset=0)

    rec = fake_ro_engine.recordings[0]
    assert "deleted_at" not in rec.sql
    assert "WHERE t.owner_uid = :owner_uid" in rec.sql


async def test_keyed_spec_patch_does_not_check_deleted_at(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    resource_id = uuid.uuid4()
    row = _projection_row(KEYED_SPEC, id=str(resource_id))
    fake_rw_engine.set_handler(lambda _sql, _params: [row])

    repo = CrudRepository(
        KEYED_SPEC,
        rw_engine=fake_rw_engine,
        ro_engine=fake_ro_engine,
    )
    await repo.patch(
        owner_uid=owner_a,
        resource_id=resource_id,
        updates={"body": {"foo": "bar"}},
    )

    rec = fake_rw_engine.recordings[0]
    assert "UPDATE app.widgets" in rec.sql
    assert "deleted_at" not in rec.sql
    assert "owner_uid = :owner_uid" in rec.sql


async def test_keyed_spec_does_not_allow_business_key_patch(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    repo = CrudRepository(
        KEYED_SPEC,
        rw_engine=fake_rw_engine,
        ro_engine=fake_ro_engine,
    )
    with pytest.raises(KeyError):
        await repo.patch(
            owner_uid=owner_a,
            resource_id=uuid.uuid4(),
            updates={"widget_key": "renamed"},
        )


async def test_keyed_spec_translates_business_key_unique_to_conflict(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    def write_handler(_sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        msg = 'duplicate key value violates unique constraint "uq_widgets_owner_key"'
        raise IntegrityError(statement=_sql, params=_params, orig=Exception(msg))

    fake_rw_engine.set_handler(write_handler)
    repo = CrudRepository(
        KEYED_SPEC,
        rw_engine=fake_rw_engine,
        ro_engine=fake_ro_engine,
    )

    with pytest.raises(NameConflictError) as excinfo:
        await repo.create(
            owner_uid=owner_a,
            values={"widget_key": "sprocket-5", "body": {}},
        )
    assert excinfo.value.status_code == 409


# ---------------------------------------------------------------------------
# pricing_history (immutable view)
# ---------------------------------------------------------------------------


async def test_pricing_history_list_is_owner_scoped(
    owner_a: str,
    fake_ro_engine: Any,
) -> None:
    fake_ro_engine.set_handler(lambda _sql, _params: [])
    repo = ImmutableRepository(PRICING_HISTORY_SPEC, ro_engine=fake_ro_engine)

    await repo.list(owner_uid=owner_a, limit=20, offset=0)

    rec = fake_ro_engine.recordings[0]
    assert rec.mode == "read"
    assert "FROM app.pricing_history" in rec.sql
    assert "WHERE t.owner_uid = :owner_uid" in rec.sql
    # No deleted_at predicate — table is immutable / no soft delete.
    assert "deleted_at" not in rec.sql
    assert rec.params["owner_uid"] == owner_a


async def test_pricing_history_get_is_owner_scoped(
    owner_a: str,
    fake_ro_engine: Any,
) -> None:
    resource_id = uuid.uuid4()
    row: dict[str, Any] = {
        "id": str(resource_id),
        "product_kind": "swaps_ir",
        "product_id": None,
        "as_of": None,
        "request": {},
        "response": {},
        "created_at": _now(),
    }
    fake_ro_engine.set_handler(lambda _sql, _params: [row])

    repo = ImmutableRepository(PRICING_HISTORY_SPEC, ro_engine=fake_ro_engine)
    result = await repo.get(owner_uid=owner_a, resource_id=resource_id)

    assert result["id"] == str(resource_id)
    rec = fake_ro_engine.recordings[0]
    assert "WHERE t.id = :resource_id AND t.owner_uid = :owner_uid" in rec.sql


async def test_pricing_history_get_404s_when_owner_mismatches(
    owner_a: str,
    fake_ro_engine: Any,
) -> None:
    fake_ro_engine.set_handler(lambda _sql, _params: [])
    repo = ImmutableRepository(PRICING_HISTORY_SPEC, ro_engine=fake_ro_engine)
    with pytest.raises(NotFoundError):
        await repo.get(owner_uid=owner_a, resource_id=uuid.uuid4())


# ---------------------------------------------------------------------------
# Roles: writes go through rw_engine; reads through ro_engine.
# ---------------------------------------------------------------------------


async def test_create_uses_rw_engine_not_ro(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    fake_rw_engine.set_handler(
        lambda _sql, _params: [_projection_row(SWAPS_IR_SPEC, name="X", request={})]
    )
    repo = CrudRepository(SWAPS_IR_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)
    await repo.create(owner_uid=owner_a, values={"name": "X", "request": {}})

    # Head insert + the audit-trail version insert, BOTH on rw.
    assert len(fake_rw_engine.recordings) == 2
    assert all(rec.mode == "write" for rec in fake_rw_engine.recordings)
    assert "entity_versions" in fake_rw_engine.recordings[1].sql
    assert not fake_ro_engine.recordings


async def test_list_uses_ro_engine_not_rw(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    fake_ro_engine.set_handler(lambda _sql, _params: [])
    repo = CrudRepository(SWAPS_IR_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)
    await repo.list(owner_uid=owner_a, limit=10, offset=0)

    assert len(fake_ro_engine.recordings) == 1
    assert fake_ro_engine.recordings[0].mode == "read"
    assert not fake_rw_engine.recordings
