"""Unit tests for the append-only entity-versioning write hook + read API.

Covers, hermetically (FakeEngine, no Postgres):

1. **The write hook** — every ``CrudRepository`` mutation (create /
   patch / soft-delete / restore) appends an ``app.entity_versions``
   row on the SAME connection/transaction as the head-row write, with
   the right ``change_type``, actor, reason, request id, and a full
   post-change snapshot in ``payload``.
2. **Atomicity ordering** — a failed head write emits no version row;
   a not-found patch/delete emits no version row; the idempotent
   "already deleted" / "already live" paths emit no version row.
3. **The read API** — ``list_versions`` / ``get_version`` SQL is
   owner-scoped (404-not-403), and the FastAPI routes expose the
   documented JSON shapes, including the ``X-Change-Reason`` header
   plumbing on every mutating route.

The DB-backed proof (real max+1 numbering, DB-level immutability of
the audit table, the backfill) lives in ``test_entity_versions_db.py``
behind the ``orchestrator_db`` marker.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from http import HTTPStatus
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from quantra_common.auth.context import ApiKeyRecord
from quantra_orchestrator.app import create_app
from quantra_orchestrator.data.errors import NotFoundError
from quantra_orchestrator.data.repository import (
    CrudRepository,
    EntitySpec,
)
from quantra_orchestrator.data.specs import (
    CURVES_SPEC,
    NAMED_ENTITY_SPECS,
    PRICING_HISTORY_SPEC,
    SWAPS_IR_SPEC,
)

from .conftest import FakeEngine, Recording


@pytest.fixture
def owner_a() -> str:
    return "user-A-uid"


def _now() -> datetime:
    return datetime(2026, 7, 19, 9, 0, 0, tzinfo=UTC)


def _curve_row(resource_id: uuid.UUID, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": str(resource_id),
        "name": "EUR-OIS",
        "currency": "EUR",
        "day_counter": "Actual/360",
        "helper_kind": "Discount",
        "reference_date": date(2026, 1, 15),
        "points": [{"tenor": "1Y", "rate": 0.02}],
        "body": {"interpolator": "Cubic"},
        "created_at": _now(),
        "updated_at": _now(),
        "deleted_at": None,
    }
    row.update(overrides)
    return row


def _version_writes(engine: FakeEngine) -> list[Recording]:
    return [r for r in engine.recordings if "entity_versions" in r.sql and r.mode == "write"]


def _head_writes(engine: FakeEngine) -> list[Recording]:
    return [r for r in engine.recordings if "entity_versions" not in r.sql and r.mode == "write"]


# ---------------------------------------------------------------------------
# Write hook: change types + binds
# ---------------------------------------------------------------------------


async def test_create_appends_version_row_with_full_snapshot(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    resource_id = uuid.uuid4()
    fake_rw_engine.set_handler(lambda _sql, _params: [_curve_row(resource_id)])
    repo = CrudRepository(CURVES_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)

    await repo.create(
        owner_uid=owner_a,
        values={
            "name": "EUR-OIS",
            "currency": "EUR",
            "day_counter": "Actual/360",
            "helper_kind": "Discount",
            "reference_date": None,
            "points": [],
            "body": {},
        },
        actor_uid=owner_a,
        actor_email="a@example.com",
        change_reason="initial booking",
    )

    versions = _version_writes(fake_rw_engine)
    assert len(versions) == 1
    rec = versions[0]
    assert "INSERT INTO app.entity_versions" in rec.sql
    # version_no = max + 1 computed in the same statement, unique-guarded.
    assert "COALESCE(MAX(v.version_no), 0) + 1" in rec.sql
    assert rec.params["entity_type"] == "curves"
    assert rec.params["entity_id"] == str(resource_id)
    assert rec.params["change_type"] == "create"
    assert rec.params["change_reason"] == "initial booking"
    assert rec.params["changed_by_uid"] == owner_a
    assert rec.params["changed_by_email"] == "a@example.com"
    assert rec.params["owner_uid"] == owner_a
    # Full post-change snapshot, JSON-serialised (timestamps → isoformat).
    payload = json.loads(rec.params["payload"])
    assert payload["id"] == str(resource_id)
    assert payload["points"] == [{"tenor": "1Y", "rate": 0.02}]
    assert payload["reference_date"] == "2026-01-15"
    assert payload["created_at"].startswith("2026-07-19T09:00:00")


async def test_patch_appends_amend_version(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    resource_id = uuid.uuid4()
    fake_rw_engine.set_handler(lambda _sql, _params: [_curve_row(resource_id, currency="USD")])
    repo = CrudRepository(CURVES_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)

    await repo.patch(
        owner_uid=owner_a,
        resource_id=resource_id,
        updates={"currency": "USD"},
        change_reason="notional corrected",
    )

    versions = _version_writes(fake_rw_engine)
    assert len(versions) == 1
    assert versions[0].params["change_type"] == "amend"
    assert versions[0].params["change_reason"] == "notional corrected"
    # Actor defaults to the owner when the caller passes none.
    assert versions[0].params["changed_by_uid"] == owner_a
    assert versions[0].params["changed_by_email"] is None
    assert json.loads(versions[0].params["payload"])["currency"] == "USD"


async def test_soft_delete_appends_delete_version_with_row_state(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    resource_id = uuid.uuid4()
    deleted_row = _curve_row(resource_id, deleted_at=_now())
    fake_rw_engine.set_handler(lambda _sql, _params: [deleted_row])
    repo = CrudRepository(CURVES_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)

    await repo.soft_delete(owner_uid=owner_a, resource_id=resource_id)

    # The delete RETURNING now carries the full projection so the
    # version payload is the row state at deletion.
    head = _head_writes(fake_rw_engine)[0]
    assert "RETURNING" in head.sql
    assert "name" in head.sql  # projection, not just id

    versions = _version_writes(fake_rw_engine)
    assert len(versions) == 1
    assert versions[0].params["change_type"] == "delete"
    payload = json.loads(versions[0].params["payload"])
    assert payload["deleted_at"] is not None


async def test_restore_appends_restore_version(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    resource_id = uuid.uuid4()
    fake_ro_engine.set_handler(lambda _sql, _params: [_curve_row(resource_id, deleted_at=_now())])
    fake_rw_engine.set_handler(lambda _sql, _params: [_curve_row(resource_id, deleted_at=None)])
    repo = CrudRepository(CURVES_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)

    await repo.restore(owner_uid=owner_a, resource_id=resource_id, change_reason="undo delete")

    versions = _version_writes(fake_rw_engine)
    assert len(versions) == 1
    assert versions[0].params["change_type"] == "restore"
    assert versions[0].params["change_reason"] == "undo delete"
    assert json.loads(versions[0].params["payload"])["deleted_at"] is None


async def test_wrapper_snapshot_excludes_cascade_plumbing_keys(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    """The cascade helper columns never leak into the persisted snapshot."""

    resource_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "RETURNING" in sql:
            return [
                {
                    "id": str(resource_id),
                    "name": "wrapper",
                    "request": {"__wrapper__": "true"},
                    "created_at": _now(),
                    "updated_at": _now(),
                    "deleted_at": _now(),
                    "_wrapper_flag": "true",
                    "_linked_app_id": None,
                }
            ]
        return []

    fake_rw_engine.set_handler(handler)
    repo = CrudRepository(SWAPS_IR_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)
    await repo.soft_delete(owner_uid=owner_a, resource_id=resource_id)

    versions = _version_writes(fake_rw_engine)
    assert len(versions) == 1
    payload = json.loads(versions[0].params["payload"])
    assert "_wrapper_flag" not in payload
    assert "_linked_app_id" not in payload
    assert payload["name"] == "wrapper"


async def test_request_id_from_middleware_contextvar_lands_in_version_row(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    resource_id = uuid.uuid4()
    fake_rw_engine.set_handler(lambda _sql, _params: [_curve_row(resource_id)])
    repo = CrudRepository(CURVES_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)

    structlog.contextvars.bind_contextvars(request_id="req-abc-123")
    try:
        await repo.create(
            owner_uid=owner_a,
            values={
                "name": "EUR-OIS",
                "currency": None,
                "day_counter": None,
                "helper_kind": None,
                "reference_date": None,
                "points": [],
                "body": {},
            },
        )
    finally:
        structlog.contextvars.unbind_contextvars("request_id")

    assert _version_writes(fake_rw_engine)[0].params["request_id"] == "req-abc-123"


async def test_version_row_has_null_request_id_outside_request_context(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    resource_id = uuid.uuid4()
    fake_rw_engine.set_handler(lambda _sql, _params: [_curve_row(resource_id)])
    repo = CrudRepository(CURVES_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)

    await repo.patch(owner_uid=owner_a, resource_id=resource_id, updates={"currency": "USD"})

    assert _version_writes(fake_rw_engine)[0].params["request_id"] is None


# ---------------------------------------------------------------------------
# Write hook: paths that must NOT version
# ---------------------------------------------------------------------------


async def test_failed_head_create_emits_no_version_write(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    """Head-write failure aborts before the version insert (same txn)."""

    def failing_handler(_sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        raise IntegrityError(statement=_sql, params=_params, orig=Exception("boom"))

    fake_rw_engine.set_handler(failing_handler)
    repo = CrudRepository(CURVES_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)

    with pytest.raises(IntegrityError):
        await repo.create(
            owner_uid=owner_a,
            values={
                "name": "X",
                "currency": None,
                "day_counter": None,
                "helper_kind": None,
                "reference_date": None,
                "points": [],
                "body": {},
            },
        )

    assert _version_writes(fake_rw_engine) == []


async def test_not_found_patch_emits_no_version_write(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    fake_rw_engine.set_handler(lambda _sql, _params: [])
    repo = CrudRepository(CURVES_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)

    with pytest.raises(NotFoundError):
        await repo.patch(owner_uid=owner_a, resource_id=uuid.uuid4(), updates={"currency": "EUR"})

    assert _version_writes(fake_rw_engine) == []


async def test_idempotent_redelete_emits_no_version_write(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    resource_id = uuid.uuid4()
    fake_rw_engine.set_handler(lambda _sql, _params: [])  # UPDATE matches nothing
    fake_ro_engine.set_handler(lambda _sql, _params: [_curve_row(resource_id, deleted_at=_now())])
    repo = CrudRepository(CURVES_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)

    await repo.soft_delete(owner_uid=owner_a, resource_id=resource_id)

    assert _version_writes(fake_rw_engine) == []


async def test_restore_of_live_row_emits_no_version_write(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    resource_id = uuid.uuid4()
    fake_ro_engine.set_handler(lambda _sql, _params: [_curve_row(resource_id, deleted_at=None)])
    repo = CrudRepository(CURVES_SPEC, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)

    await repo.restore(owner_uid=owner_a, resource_id=resource_id)

    assert fake_rw_engine.recordings == []


async def test_unversioned_spec_emits_no_version_write(
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    unversioned = EntitySpec(
        entity="widget",
        table="widgets",
        scalar_columns=("name",),
        jsonb_columns=("body",),
        versioned=False,
    )
    fake_rw_engine.set_handler(
        lambda _sql, _params: [
            {
                "id": str(uuid.uuid4()),
                "name": "w",
                "body": {},
                "created_at": _now(),
                "updated_at": _now(),
                "deleted_at": None,
            }
        ]
    )
    repo = CrudRepository(unversioned, rw_engine=fake_rw_engine, ro_engine=fake_ro_engine)

    await repo.create(owner_uid=owner_a, values={"name": "w", "body": {}})

    assert _version_writes(fake_rw_engine) == []


def test_all_named_entity_specs_are_versioned() -> None:
    """Every mutable named entity participates in the audit trail."""

    assert all(spec.versioned for spec in NAMED_ENTITY_SPECS)
    assert PRICING_HISTORY_SPEC.versioned is False


# ---------------------------------------------------------------------------
# Read API (repository level): owner scoping + 404s
# ---------------------------------------------------------------------------


async def test_list_versions_scopes_by_owner_and_orders_newest_first(
    owner_a: str,
    fake_ro_engine: Any,
) -> None:
    resource_id = uuid.uuid4()
    rows = [
        {
            "version_no": 2,
            "change_type": "amend",
            "change_reason": None,
            "changed_by_uid": owner_a,
            "changed_by_email": None,
            "changed_at": _now(),
            "request_id": "r2",
        },
        {
            "version_no": 1,
            "change_type": "create",
            "change_reason": None,
            "changed_by_uid": owner_a,
            "changed_by_email": None,
            "changed_at": _now(),
            "request_id": "r1",
        },
    ]

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        assert "FROM app.entity_versions" in sql
        return rows

    fake_ro_engine.set_handler(handler)
    repo = CrudRepository(CURVES_SPEC, ro_engine=fake_ro_engine)

    result = await repo.list_versions(owner_uid=owner_a, resource_id=resource_id)

    assert [r["version_no"] for r in result] == [2, 1]
    rec = fake_ro_engine.recordings[0]
    assert "v.owner_uid = :owner_uid" in rec.sql
    assert "v.entity_type = :entity_type" in rec.sql
    assert "ORDER BY v.version_no DESC" in rec.sql
    assert rec.params["entity_type"] == "curves"
    assert rec.params["owner_uid"] == owner_a


async def test_list_versions_404s_for_unknown_or_foreign_entity(
    owner_a: str,
    fake_ro_engine: Any,
) -> None:
    fake_ro_engine.set_handler(lambda _sql, _params: [])
    repo = CrudRepository(CURVES_SPEC, ro_engine=fake_ro_engine)

    with pytest.raises(NotFoundError):
        await repo.list_versions(owner_uid=owner_a, resource_id=uuid.uuid4())


async def test_get_version_404s_when_version_missing(
    owner_a: str,
    fake_ro_engine: Any,
) -> None:
    fake_ro_engine.set_handler(lambda _sql, _params: [])
    repo = CrudRepository(CURVES_SPEC, ro_engine=fake_ro_engine)

    with pytest.raises(NotFoundError):
        await repo.get_version(owner_uid=owner_a, resource_id=uuid.uuid4(), version_no=7)

    rec = fake_ro_engine.recordings[0]
    assert "v.version_no = :version_no" in rec.sql
    assert rec.params["version_no"] == 7
    assert rec.params["owner_uid"] == owner_a


# ---------------------------------------------------------------------------
# Read API (HTTP level): response shapes + header plumbing
# ---------------------------------------------------------------------------
# Reuses the app/client fixtures from test_data_routers-style wiring via
# the shared conftest FakeEngine fixtures.


@pytest.fixture
def api_key_a() -> str:
    return "key-a"


@pytest.fixture
def http_app(
    orchestrator_settings: Any,
    api_key_a: str,
    owner_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> Any:
    records = {
        api_key_a: ApiKeyRecord(
            api_key_id="ak-a",
            owner_uid=owner_a,
            name="A",
            email="a@example.com",
            tier="free",
            active=True,
        )
    }

    async def _lookup(key: str) -> ApiKeyRecord | None:
        return records.get(key)

    def _verify(_token: str) -> dict[str, Any]:
        msg = "no firebase in this suite"
        raise ValueError(msg)

    return create_app(
        orchestrator_settings,
        api_key_lookup=_lookup,
        firebase_verifier=_verify,
        app_rw_engine=fake_rw_engine,
        app_ro_engine=fake_ro_engine,
    )


@pytest.fixture
def client(http_app: Any) -> Any:
    with TestClient(http_app, raise_server_exceptions=False) as tc:
        yield tc


def test_versions_list_route_returns_documented_shape(
    client: TestClient,
    api_key_a: str,
    owner_a: str,
    fake_ro_engine: Any,
) -> None:
    resource_id = uuid.uuid4()
    fake_ro_engine.set_handler(
        lambda _sql, _params: [
            {
                "version_no": 3,
                "change_type": "amend",
                "change_reason": "notional corrected",
                "changed_by_uid": owner_a,
                "changed_by_email": "a@example.com",
                "changed_at": _now(),
                "request_id": "req-3",
            }
        ]
    )

    response = client.get(f"/v1/curves/{resource_id}/versions", headers={"X-API-Key": api_key_a})

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body == {
        "items": [
            {
                "version_no": 3,
                "change_type": "amend",
                "change_reason": "notional corrected",
                "changed_by_uid": owner_a,
                "changed_by_email": "a@example.com",
                "changed_at": "2026-07-19T09:00:00Z",
                "request_id": "req-3",
            }
        ]
    }


def test_versions_detail_route_includes_payload_snapshot(
    client: TestClient,
    api_key_a: str,
    owner_a: str,
    fake_ro_engine: Any,
) -> None:
    resource_id = uuid.uuid4()
    snapshot = {"id": str(resource_id), "name": "EUR-OIS", "currency": "EUR"}
    fake_ro_engine.set_handler(
        lambda _sql, _params: [
            {
                "version_no": 2,
                "change_type": "amend",
                "change_reason": None,
                "changed_by_uid": owner_a,
                "changed_by_email": None,
                "changed_at": _now(),
                "request_id": None,
                "payload": snapshot,
            }
        ]
    )

    response = client.get(f"/v1/curves/{resource_id}/versions/2", headers={"X-API-Key": api_key_a})

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["version_no"] == 2
    assert body["payload"] == snapshot


def test_versions_route_404_not_403_for_foreign_entity(
    client: TestClient,
    api_key_a: str,
    fake_ro_engine: Any,
) -> None:
    # The owner-scoped WHERE returns nothing for someone else's id →
    # indistinguishable from "does not exist".
    fake_ro_engine.set_handler(lambda _sql, _params: [])

    response = client.get(f"/v1/curves/{uuid.uuid4()}/versions", headers={"X-API-Key": api_key_a})

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "not_found"


def test_versions_routes_require_auth(client: TestClient) -> None:
    response = client.get(f"/v1/curves/{uuid.uuid4()}/versions")
    assert response.status_code == HTTPStatus.UNAUTHORIZED


def test_change_reason_header_reaches_version_row_on_patch(
    client: TestClient,
    api_key_a: str,
    owner_a: str,
    fake_rw_engine: Any,
) -> None:
    resource_id = uuid.uuid4()
    fake_rw_engine.set_handler(lambda _sql, _params: [_curve_row(resource_id, currency="USD")])

    response = client.patch(
        f"/v1/curves/{resource_id}",
        json={"currency": "USD"},
        headers={"X-API-Key": api_key_a, "X-Change-Reason": "notional corrected"},
    )

    assert response.status_code == HTTPStatus.OK
    versions = _version_writes(fake_rw_engine)
    assert len(versions) == 1
    assert versions[0].params["change_reason"] == "notional corrected"
    # Actor identity comes from the authenticated principal.
    assert versions[0].params["changed_by_uid"] == owner_a
    assert versions[0].params["changed_by_email"] == "a@example.com"
    # The route runs under RequestIdMiddleware → the version row groups
    # under the request id the middleware bound.
    assert versions[0].params["request_id"]


def test_change_reason_header_is_optional_on_create(
    client: TestClient,
    api_key_a: str,
    fake_rw_engine: Any,
) -> None:
    resource_id = uuid.uuid4()
    fake_rw_engine.set_handler(lambda _sql, _params: [_curve_row(resource_id)])

    response = client.post(
        "/v1/curves",
        json={"name": "EUR-OIS", "points": [], "body": {}},
        headers={"X-API-Key": api_key_a},
    )

    assert response.status_code == HTTPStatus.CREATED
    versions = _version_writes(fake_rw_engine)
    assert len(versions) == 1
    assert versions[0].params["change_type"] == "create"
    assert versions[0].params["change_reason"] is None


def test_change_reason_header_documented_in_openapi(http_app: Any) -> None:
    """The OpenAPI spec advertises X-Change-Reason on mutating routes."""

    spec = http_app.openapi()
    patch_op = spec["paths"]["/v1/curves/{resource_id}"]["patch"]
    header_params = [p for p in patch_op.get("parameters", []) if p.get("in") == "header"]
    assert any(p["name"] == "X-Change-Reason" for p in header_params)
    reason = next(p for p in header_params if p["name"] == "X-Change-Reason")
    assert "audit trail" in reason["description"]
    # And the versions read routes exist for every versioned entity.
    assert "/v1/curves/{resource_id}/versions" in spec["paths"]
    assert "/v1/swaps/ir/{resource_id}/versions/{version_no}" in spec["paths"]
