"""DB-backed integration tests for the append-only entity audit trail.

Gated behind the ``orchestrator_db`` marker (same DSN convention as
``test_data_db_integration.py``). Proves against a real Postgres what
the hermetic suite cannot:

* Real ``max+1`` version numbering across a full HTTP lifecycle
  (create → amend → amend → delete → restore ⇒ v1..v5).
* ``payload`` snapshots round-trip through real JSONB.
* **DB-level immutability**: ``UPDATE`` / ``DELETE`` on
  ``app.entity_versions`` are DENIED to both app roles — the trail is
  append-only even to a compromised app process.
* Same-transaction atomicity: a failed head write (409 name conflict)
  leaves no version row behind.
* The pricing-history link: ``record_pricing_call`` for a saved
  product resolves the entity's head ``version_no`` into
  ``trade_version``.
"""

from __future__ import annotations

import os
import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import date

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from quantra_common.settings import Environment, LogLevel
from quantra_orchestrator.app import create_app
from quantra_orchestrator.auth.api_keys import SqlApiKeyLookup, hash_api_key
from quantra_orchestrator.pricing.history import record_pricing_call
from quantra_orchestrator.settings import OrchestratorSettings

pytestmark = pytest.mark.orchestrator_db

APP_RO_DSN_ENV: str = "QUANTRA_ORCHESTRATOR_TEST_APP_RO_DSN"
APP_RW_DSN_ENV: str = "QUANTRA_ORCHESTRATOR_TEST_APP_RW_DSN"
ADMIN_DSN_ENV: str = "QUANTRA_ORCHESTRATOR_TEST_ADMIN_DSN"


def _env_or_skip(name: str, reason: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(reason, allow_module_level=False)
    return value


@pytest.fixture
def app_ro_dsn() -> str:
    return _env_or_skip(APP_RO_DSN_ENV, f"Set {APP_RO_DSN_ENV} to an app_ro DSN.")


@pytest.fixture
def app_rw_dsn() -> str:
    return _env_or_skip(APP_RW_DSN_ENV, f"Set {APP_RW_DSN_ENV} to an app_rw DSN.")


@pytest.fixture
def admin_dsn() -> str:
    return _env_or_skip(ADMIN_DSN_ENV, f"Set {ADMIN_DSN_ENV} to an admin DSN.")


@pytest_asyncio.fixture
async def app_ro_engine(app_ro_dsn: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(app_ro_dsn, pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def app_rw_engine(app_rw_dsn: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(app_rw_dsn, pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def admin_engine(admin_dsn: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(admin_dsn, isolation_level="AUTOCOMMIT")
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seeded_user(admin_engine: AsyncEngine) -> AsyncIterator[tuple[str, str]]:
    """Provision an ``app.users`` + ``app.api_keys`` pair; clean up owned rows."""

    uid = f"orch-ev-it-{uuid.uuid4().hex}"
    raw_key = f"qkk_orch_ev_{secrets.token_urlsafe(20)}"
    api_key_id = str(uuid.uuid4())

    async with admin_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO app.users (uid, email, display_name, tier) "
                "VALUES (:uid, :email, :name, 'free') ON CONFLICT (uid) DO NOTHING"
            ),
            {"uid": uid, "email": f"{uid}@example.com", "name": "Entity Versions IT"},
        )
        await conn.execute(
            text(
                "INSERT INTO app.api_keys (id, owner_uid, name, key_hash) "
                "VALUES (:id, :uid, :name, :digest)"
            ),
            {
                "id": api_key_id,
                "uid": uid,
                "name": "entity-versions integration",
                "digest": hash_api_key(raw_key),
            },
        )

    try:
        yield uid, raw_key
    finally:
        async with admin_engine.begin() as conn:
            await conn.execute(text("DELETE FROM app.api_keys WHERE id = :id"), {"id": api_key_id})
            # The admin/migration role is the only one allowed to prune
            # the audit trail (retention ops); app roles cannot.
            for table in ("entity_versions", "pricing_history", "swaps_ir", "curves"):
                await conn.execute(
                    text(f"DELETE FROM app.{table} WHERE owner_uid = :uid"),  # noqa: S608
                    {"uid": uid},
                )
            await conn.execute(text("DELETE FROM app.users WHERE uid = :uid"), {"uid": uid})


@pytest.fixture
def settings() -> OrchestratorSettings:
    return OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="it-entity-versions",
    )


@pytest_asyncio.fixture
async def asgi_client(
    settings: OrchestratorSettings,
    app_rw_engine: AsyncEngine,
    app_ro_engine: AsyncEngine,
) -> AsyncIterator[AsyncClient]:
    lookup = SqlApiKeyLookup(app_ro_engine)
    instance: FastAPI = create_app(
        settings,
        api_key_lookup=lookup,
        app_rw_engine=app_rw_engine,
        app_ro_engine=app_ro_engine,
    )
    transport = ASGITransport(app=instance)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Lifecycle: create → amend → amend → delete → restore ⇒ v1..v5
# ---------------------------------------------------------------------------


async def test_full_lifecycle_versions(
    asgi_client: AsyncClient,
    seeded_user: tuple[str, str],
) -> None:
    uid, raw_key = seeded_user
    headers = {"X-API-Key": raw_key}
    name = f"EV-IT-{uuid.uuid4().hex[:8]}"

    create_resp = await asgi_client.post(
        "/v1/curves",
        json={"name": name, "currency": "EUR", "points": [], "body": {}},
        headers=headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    curve_id = create_resp.json()["id"]

    patch1 = await asgi_client.patch(
        f"/v1/curves/{curve_id}",
        json={"currency": "USD"},
        headers=headers,
    )
    assert patch1.status_code == 200
    patch2 = await asgi_client.patch(
        f"/v1/curves/{curve_id}",
        json={"body": {"interpolator": "Linear"}},
        headers={**headers, "X-Change-Reason": "notional corrected"},
    )
    assert patch2.status_code == 200
    delete_resp = await asgi_client.delete(f"/v1/curves/{curve_id}", headers=headers)
    assert delete_resp.status_code == 204
    restore_resp = await asgi_client.post(f"/v1/curves/{curve_id}:restore", headers=headers)
    assert restore_resp.status_code == 200

    versions_resp = await asgi_client.get(f"/v1/curves/{curve_id}/versions", headers=headers)
    assert versions_resp.status_code == 200
    items = versions_resp.json()["items"]
    assert [i["version_no"] for i in items] == [5, 4, 3, 2, 1]
    assert [i["change_type"] for i in items] == [
        "restore",
        "delete",
        "amend",
        "amend",
        "create",
    ]
    by_no = {i["version_no"]: i for i in items}
    assert by_no[3]["change_reason"] == "notional corrected"
    assert by_no[1]["change_reason"] is None
    assert all(i["changed_by_uid"] == uid for i in items)
    # Every HTTP write ran under RequestIdMiddleware → grouped ids.
    assert all(i["request_id"] for i in items)
    # Distinct user actions ⇒ distinct request ids.
    assert len({i["request_id"] for i in items}) == 5

    # Snapshots: v2 carries the amended currency; v4 the deleted state;
    # v5 the restored (live) state.
    v2 = (await asgi_client.get(f"/v1/curves/{curve_id}/versions/2", headers=headers)).json()
    assert v2["payload"]["currency"] == "USD"
    v4 = (await asgi_client.get(f"/v1/curves/{curve_id}/versions/4", headers=headers)).json()
    assert v4["payload"]["deleted_at"] is not None
    v5 = (await asgi_client.get(f"/v1/curves/{curve_id}/versions/5", headers=headers)).json()
    assert v5["payload"]["deleted_at"] is None
    assert v5["payload"]["body"] == {"interpolator": "Linear"}


async def test_versions_are_tenant_scoped_404(
    asgi_client: AsyncClient,
    seeded_user: tuple[str, str],
    admin_engine: AsyncEngine,
) -> None:
    """Another owner's entity id 404s (not 403) on the versions surface."""

    _, raw_key = seeded_user
    headers = {"X-API-Key": raw_key}

    foreign_uid = f"orch-ev-foreign-{uuid.uuid4().hex}"
    async with admin_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO app.users (uid, email, display_name, tier) "
                "VALUES (:uid, :email, 'Foreign', 'free')"
            ),
            {"uid": foreign_uid, "email": f"{foreign_uid}@example.com"},
        )
        row = (
            (
                await conn.execute(
                    text(
                        "INSERT INTO app.curves (owner_uid, name, points, body) "
                        "VALUES (:uid, :name, '[]'::jsonb, '{}'::jsonb) RETURNING id::text AS id"
                    ),
                    {"uid": foreign_uid, "name": f"foreign-{uuid.uuid4().hex[:8]}"},
                )
            )
            .mappings()
            .one()
        )
    foreign_curve_id = row["id"]

    try:
        resp = await asgi_client.get(f"/v1/curves/{foreign_curve_id}/versions", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"
    finally:
        async with admin_engine.begin() as conn:
            for table in ("entity_versions", "curves"):
                await conn.execute(
                    text(f"DELETE FROM app.{table} WHERE owner_uid = :uid"),  # noqa: S608
                    {"uid": foreign_uid},
                )
            await conn.execute(text("DELETE FROM app.users WHERE uid = :uid"), {"uid": foreign_uid})


async def test_failed_head_write_leaves_no_version_row(
    asgi_client: AsyncClient,
    seeded_user: tuple[str, str],
    admin_engine: AsyncEngine,
) -> None:
    """A 409 name-conflict create rolls back atomically: no orphan versions."""

    uid, raw_key = seeded_user
    headers = {"X-API-Key": raw_key}
    name = f"EV-CONFLICT-{uuid.uuid4().hex[:8]}"
    body = {"name": name, "points": [], "body": {}}

    first = await asgi_client.post("/v1/curves", json=body, headers=headers)
    assert first.status_code == 201
    second = await asgi_client.post("/v1/curves", json=body, headers=headers)
    assert second.status_code == 409

    async with admin_engine.connect() as conn:
        count = (
            (
                await conn.execute(
                    text(
                        "SELECT count(*) AS n FROM app.entity_versions "
                        "WHERE owner_uid = :uid AND entity_type = 'curves'"
                    ),
                    {"uid": uid},
                )
            )
            .mappings()
            .one()["n"]
        )
    assert count == 1  # only the successful create's v1


# ---------------------------------------------------------------------------
# DB-level immutability: app roles cannot rewrite history
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE app.entity_versions SET change_reason = 'tampered' WHERE FALSE",
        "DELETE FROM app.entity_versions WHERE FALSE",
        "TRUNCATE app.entity_versions",
    ],
)
async def test_app_rw_cannot_mutate_entity_versions(
    app_rw_engine: AsyncEngine,
    statement: str,
) -> None:
    with pytest.raises(ProgrammingError, match=r"(?i)permission denied"):
        async with app_rw_engine.begin() as conn:
            await conn.execute(text(statement))


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE app.entity_versions SET change_reason = 'tampered' WHERE FALSE",
        "DELETE FROM app.entity_versions WHERE FALSE",
    ],
)
async def test_app_ro_cannot_mutate_entity_versions(
    app_ro_engine: AsyncEngine,
    statement: str,
) -> None:
    with pytest.raises(ProgrammingError, match=r"(?i)permission denied"):
        async with app_ro_engine.begin() as conn:
            await conn.execute(text(statement))


# ---------------------------------------------------------------------------
# Pricing ↔ version link
# ---------------------------------------------------------------------------


async def test_record_pricing_call_resolves_head_trade_version(
    asgi_client: AsyncClient,
    seeded_user: tuple[str, str],
    app_rw_engine: AsyncEngine,
    admin_engine: AsyncEngine,
) -> None:
    """A by-reference pricing-history row carries the entity's head version."""

    uid, raw_key = seeded_user
    headers = {"X-API-Key": raw_key}

    create = await asgi_client.post(
        "/v1/swaps/ir",
        json={"name": f"EV-SWAP-{uuid.uuid4().hex[:8]}", "request": {"k": 1}},
        headers=headers,
    )
    assert create.status_code == 201
    swap_id = uuid.UUID(create.json()["id"])
    patch = await asgi_client.patch(
        f"/v1/swaps/ir/{swap_id}",
        json={"request": {"k": 2}},
        headers=headers,
    )
    assert patch.status_code == 200  # head version is now 2

    history_id = await record_pricing_call(
        rw_engine=app_rw_engine,
        owner_uid=uid,
        product_kind="swaps_ir",
        product_id=swap_id,
        as_of=date(2026, 7, 19),
        request_payload={"swap_id": str(swap_id)},
        response_payload={"npv": 42.0},
    )
    assert history_id is not None

    async with admin_engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT trade_entity_type, trade_entity_id::text AS trade_entity_id, "
                        "trade_version FROM app.pricing_history WHERE id = :id"
                    ),
                    {"id": str(history_id)},
                )
            )
            .mappings()
            .one()
        )
    assert row["trade_entity_type"] == "swaps_ir"
    assert row["trade_entity_id"] == str(swap_id)
    assert row["trade_version"] == 2
