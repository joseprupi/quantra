"""DB-backed integration tests for the orchestrator data layer.

Gated behind the ``orchestrator_db`` marker (same fixture
convention as the auth integration tests in ``test_db_integration.py``).
Requires both:

* ``QUANTRA_ORCHESTRATOR_TEST_APP_RO_DSN`` — read-only DSN against the
  ``app.*`` schema. Production-equivalent role.
* ``QUANTRA_ORCHESTRATOR_TEST_ADMIN_DSN`` — admin DSN for fixture
  setup / teardown of synthetic ``app.users`` rows.

We deliberately *do not* truncate the entire schema between tests
(orchestrator tests own per-row lifecycle so contributors can
seed unrelated data without interference). Each test creates its own
``app.users`` row, inserts its product / curve / pricing-history rows,
and tears them down in reverse-FK order.

The DSN injection runs through ``create_app(..., app_rw_engine=,
app_ro_engine=)`` so the same code path the production lifespan
exercises also lights up here — no special test routing.

Coverage hits the three entities the plan calls out: ``curves`` (the
canonical named-template case), ``swaps_ir`` (representative of the
seven product tables), and ``pricing_history`` (immutable view).
"""

from __future__ import annotations

import json
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
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    create_async_engine,
)

from quantra_common.settings import Environment, LogLevel
from quantra_orchestrator.app import create_app
from quantra_orchestrator.auth.api_keys import SqlApiKeyLookup, hash_api_key
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
    return _env_or_skip(
        APP_RO_DSN_ENV,
        f"Set {APP_RO_DSN_ENV} to a postgres+asyncpg DSN with app_ro access.",
    )


@pytest.fixture
def app_rw_dsn() -> str:
    # ``app_rw`` defaults to ``app_ro``'s value if unset so a contributor
    # who only has one DSN handy can still smoke the read path. The
    # orchestrator's write endpoints will obviously fail under that
    # configuration; the tests below that mutate data require both DSNs.
    return os.environ.get(APP_RW_DSN_ENV, "") or _env_or_skip(
        APP_RW_DSN_ENV,
        f"Set {APP_RW_DSN_ENV} to a postgres+asyncpg DSN with app_rw access.",
    )


@pytest.fixture
def admin_dsn() -> str:
    return _env_or_skip(
        ADMIN_DSN_ENV,
        f"Set {ADMIN_DSN_ENV} to a postgres+asyncpg admin DSN for fixture set-up.",
    )


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
    """Provision an ``app.users`` row + matching ``app.api_keys`` row.

    Yields ``(uid, raw_key)``; the caller authenticates with ``X-API-Key:
    <raw_key>``. Cleanup deletes the api key, every owned row across the
    tables this suite exercises, and finally the user itself in reverse
    FK order.
    """

    uid = f"orch-data-it-{uuid.uuid4().hex}"
    raw_key = f"qkk_orch_data_{secrets.token_urlsafe(20)}"
    api_key_id = str(uuid.uuid4())

    async with admin_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO app.users (uid, email, display_name, tier) "
                "VALUES (:uid, :email, :name, 'free') "
                "ON CONFLICT (uid) DO NOTHING"
            ),
            {"uid": uid, "email": f"{uid}@example.com", "name": "Data Layer IT"},
        )
        await conn.execute(
            text(
                "INSERT INTO app.api_keys (id, owner_uid, name, key_hash) "
                "VALUES (:id, :uid, :name, :digest)"
            ),
            {
                "id": api_key_id,
                "uid": uid,
                "name": "data-layer integration",
                "digest": hash_api_key(raw_key),
            },
        )

    try:
        yield uid, raw_key
    finally:
        async with admin_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM app.api_keys WHERE id = :id"),
                {"id": api_key_id},
            )
            # Reverse-FK order so anything the test left behind goes
            # before the user itself. We also clean ``pricing_history``
            # because tests below seed it directly via the admin engine.
            for table in (
                "pricing_history",
                "swaps_ir",
                "curves",
            ):
                await conn.execute(
                    text(
                        f"DELETE FROM app.{table} WHERE owner_uid = :uid"  # noqa: S608
                    ),
                    {"uid": uid},
                )
            await conn.execute(
                text("DELETE FROM app.users WHERE uid = :uid"),
                {"uid": uid},
            )


@pytest.fixture
def settings() -> OrchestratorSettings:
    return OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="it-data-layer",
    )


@pytest_asyncio.fixture
async def asgi_client(
    settings: OrchestratorSettings,
    app_rw_engine: AsyncEngine,
    app_ro_engine: AsyncEngine,
) -> AsyncIterator[AsyncClient]:
    """ASGI-mounted httpx client wired with the real Postgres engines."""

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
# curves: full lifecycle
# ---------------------------------------------------------------------------


async def test_curves_full_lifecycle(
    asgi_client: AsyncClient,
    seeded_user: tuple[str, str],
) -> None:
    _, raw_key = seeded_user
    headers = {"X-API-Key": raw_key}
    payload = {
        "name": f"EUR-OIS-{uuid.uuid4().hex[:8]}",
        "currency": "EUR",
        "day_counter": "Actual/360",
        "helper_kind": "Discount",
        "reference_date": "2026-01-15",
        "points": [{"tenor": "1Y", "rate": 0.025}],
        "body": {"interpolator": "Cubic"},
    }

    create_resp = await asgi_client.post("/v1/curves", json=payload, headers=headers)
    assert create_resp.status_code == 201, create_resp.text
    created = create_resp.json()
    curve_id = created["id"]
    uuid.UUID(curve_id)  # must be a UUID string
    assert created["name"] == payload["name"]
    assert created["points"] == payload["points"]
    assert created["body"] == payload["body"]
    assert created["deleted_at"] is None

    list_resp = await asgi_client.get("/v1/curves", headers=headers)
    assert list_resp.status_code == 200
    page = list_resp.json()
    assert any(item["id"] == curve_id for item in page["items"])
    assert page["page"]["offset"] == 0

    read_resp = await asgi_client.get(f"/v1/curves/{curve_id}", headers=headers)
    assert read_resp.status_code == 200
    assert read_resp.json()["id"] == curve_id

    patch_resp = await asgi_client.patch(
        f"/v1/curves/{curve_id}",
        json={"currency": "USD", "body": {"interpolator": "Linear"}},
        headers=headers,
    )
    assert patch_resp.status_code == 200
    patched = patch_resp.json()
    assert patched["currency"] == "USD"
    assert patched["body"]["interpolator"] == "Linear"

    delete_resp = await asgi_client.delete(f"/v1/curves/{curve_id}", headers=headers)
    assert delete_resp.status_code == 204

    # After soft delete: read is 404, list excludes the row.
    read_after_delete = await asgi_client.get(f"/v1/curves/{curve_id}", headers=headers)
    assert read_after_delete.status_code == 404
    assert read_after_delete.json()["code"] == "not_found"

    list_after_delete = await asgi_client.get("/v1/curves", headers=headers)
    assert all(item["id"] != curve_id for item in list_after_delete.json()["items"])

    # DELETE is idempotent — second call returns 204 still.
    delete_again = await asgi_client.delete(f"/v1/curves/{curve_id}", headers=headers)
    assert delete_again.status_code == 204

    restore_resp = await asgi_client.post(f"/v1/curves/{curve_id}:restore", headers=headers)
    assert restore_resp.status_code == 200
    restored = restore_resp.json()
    assert restored["id"] == curve_id
    assert restored["deleted_at"] is None

    # Restore is idempotent on an already-live row.
    restore_again = await asgi_client.post(f"/v1/curves/{curve_id}:restore", headers=headers)
    assert restore_again.status_code == 200


async def test_curves_restore_409_on_name_conflict(
    asgi_client: AsyncClient,
    seeded_user: tuple[str, str],
) -> None:
    _, raw_key = seeded_user
    headers = {"X-API-Key": raw_key}
    shared_name = f"conflict-{uuid.uuid4().hex[:8]}"

    a = await asgi_client.post(
        "/v1/curves",
        json={"name": shared_name, "points": [], "body": {}},
        headers=headers,
    )
    assert a.status_code == 201
    a_id = a.json()["id"]

    # Soft-delete the first, then create another live row with the
    # same name, then attempt to restore the first.
    delete_a = await asgi_client.delete(f"/v1/curves/{a_id}", headers=headers)
    assert delete_a.status_code == 204

    b = await asgi_client.post(
        "/v1/curves",
        json={"name": shared_name, "points": [], "body": {}},
        headers=headers,
    )
    assert b.status_code == 201

    conflict = await asgi_client.post(f"/v1/curves/{a_id}:restore", headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "name_conflict"


async def test_curves_post_409_on_duplicate_live_name(
    asgi_client: AsyncClient,
    seeded_user: tuple[str, str],
) -> None:
    _, raw_key = seeded_user
    headers = {"X-API-Key": raw_key}
    name = f"dup-{uuid.uuid4().hex[:8]}"
    payload = {"name": name, "points": [], "body": {}}

    first = await asgi_client.post("/v1/curves", json=payload, headers=headers)
    assert first.status_code == 201

    duplicate = await asgi_client.post("/v1/curves", json=payload, headers=headers)
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "name_conflict"


# ---------------------------------------------------------------------------
# swaps_ir: product table sample
# ---------------------------------------------------------------------------


async def test_swaps_ir_full_lifecycle(
    asgi_client: AsyncClient,
    seeded_user: tuple[str, str],
) -> None:
    _, raw_key = seeded_user
    headers = {"X-API-Key": raw_key}
    payload = {
        "name": f"USD-5Y-{uuid.uuid4().hex[:6]}",
        "request": {
            "notional": 1_000_000,
            "fixed_rate": 0.04,
            "maturity": "2031-05-15",
        },
    }

    create_resp = await asgi_client.post("/v1/swaps/ir", json=payload, headers=headers)
    assert create_resp.status_code == 201
    swap = create_resp.json()
    swap_id = swap["id"]
    assert swap["request"]["fixed_rate"] == 0.04

    read_resp = await asgi_client.get(f"/v1/swaps/ir/{swap_id}", headers=headers)
    assert read_resp.status_code == 200

    patch_resp = await asgi_client.patch(
        f"/v1/swaps/ir/{swap_id}",
        json={"request": {"notional": 2_000_000, "fixed_rate": 0.04}},
        headers=headers,
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["request"]["notional"] == 2_000_000

    delete_resp = await asgi_client.delete(f"/v1/swaps/ir/{swap_id}", headers=headers)
    assert delete_resp.status_code == 204

    after_delete = await asgi_client.get(f"/v1/swaps/ir/{swap_id}", headers=headers)
    assert after_delete.status_code == 404

    restore_resp = await asgi_client.post(f"/v1/swaps/ir/{swap_id}:restore", headers=headers)
    assert restore_resp.status_code == 200
    assert restore_resp.json()["deleted_at"] is None


async def test_wrapper_delete_cascades_graph_row_no_orphan(
    asgi_client: AsyncClient,
    seeded_user: tuple[str, str],
    admin_engine: AsyncEngine,
) -> None:
    """deleting a wrapper product row leaves no orphan graph row.

    Mirrors the portal Save shape: first the by-reference save-graph row
    is persisted (a plain, non-wrapper-marked product row), then the
    wrapper row the user sees (``request.__wrapper__ = true``) is
    persisted pointing at the graph row via ``request.appId``. Deleting
    the wrapper via the orchestrator must soft-delete BOTH rows, while an
    unrelated product row stays live.
    """

    owner_uid, raw_key = seeded_user
    headers = {"X-API-Key": raw_key}

    # 1) The save-graph row (the by-reference price arm's target) —
    #    a plain product row, NOT wrapper-marked.
    graph_resp = await asgi_client.post(
        "/v1/swaps/ir",
        json={
            "name": f"graph-{uuid.uuid4().hex[:8]}",
            "request": {"notional": 1_000_000, "fixed_rate": 0.03},
        },
        headers=headers,
    )
    assert graph_resp.status_code == 201
    graph_id = graph_resp.json()["id"]

    # 2) The wrapper row (what the saved-product store lists) — carries
    #    __wrapper__ + the appId bridge to the graph row.
    wrapper_resp = await asgi_client.post(
        "/v1/swaps/ir",
        json={
            "name": f"wrapper-{uuid.uuid4().hex[:8]}",
            "request": {
                "__wrapper__": True,
                "appId": graph_id,
                "appGraph": {"root": graph_id},
                "request": {"notional": 1_000_000, "fixed_rate": 0.03},
            },
        },
        headers=headers,
    )
    assert wrapper_resp.status_code == 201
    wrapper_id = wrapper_resp.json()["id"]

    # 3) An unrelated product row that must survive the delete.
    other_resp = await asgi_client.post(
        "/v1/swaps/ir",
        json={
            "name": f"other-{uuid.uuid4().hex[:8]}",
            "request": {"notional": 500_000, "fixed_rate": 0.05},
        },
        headers=headers,
    )
    assert other_resp.status_code == 201
    other_id = other_resp.json()["id"]

    async def _deleted_at(row_id: str) -> object:
        async with admin_engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT deleted_at FROM app.swaps_ir "
                    "WHERE id = CAST(:id AS uuid) AND owner_uid = :owner_uid"
                ),
                {"id": row_id, "owner_uid": owner_uid},
            )
            return result.scalar_one()

    # Before delete: all three rows are live.
    assert await _deleted_at(graph_id) is None
    assert await _deleted_at(wrapper_id) is None
    assert await _deleted_at(other_id) is None

    # Delete the wrapper via the orchestrator.
    delete_resp = await asgi_client.delete(f"/v1/swaps/ir/{wrapper_id}", headers=headers)
    assert delete_resp.status_code == 204

    # After delete: wrapper AND its graph sibling are soft-deleted; the
    # unrelated row is untouched (no orphan, no collateral damage).
    assert await _deleted_at(wrapper_id) is not None
    assert await _deleted_at(graph_id) is not None
    assert await _deleted_at(other_id) is None


# ---------------------------------------------------------------------------
# Cross-tenant: user A's row is invisible to user B (404, not 403).
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def two_seeded_users(
    admin_engine: AsyncEngine,
) -> AsyncIterator[tuple[tuple[str, str], tuple[str, str]]]:
    """Provision two users with their own api keys; clean up in tear-down."""

    pair: list[tuple[str, str, str]] = []  # (uid, raw_key, api_key_id)
    for label in ("A", "B"):
        uid = f"orch-tenant-{label}-{uuid.uuid4().hex}"
        raw_key = f"qkk_tenant_{label}_{secrets.token_urlsafe(20)}"
        api_key_id = str(uuid.uuid4())
        async with admin_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO app.users (uid, email, display_name, tier) "
                    "VALUES (:uid, :email, :name, 'free') "
                    "ON CONFLICT (uid) DO NOTHING"
                ),
                {
                    "uid": uid,
                    "email": f"{uid}@example.com",
                    "name": f"Tenant {label}",
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO app.api_keys (id, owner_uid, name, key_hash) "
                    "VALUES (:id, :uid, :name, :digest)"
                ),
                {
                    "id": api_key_id,
                    "uid": uid,
                    "name": f"tenant {label}",
                    "digest": hash_api_key(raw_key),
                },
            )
        pair.append((uid, raw_key, api_key_id))

    try:
        yield (
            (pair[0][0], pair[0][1]),
            (pair[1][0], pair[1][1]),
        )
    finally:
        async with admin_engine.begin() as conn:
            for uid, _, api_key_id in pair:
                await conn.execute(
                    text("DELETE FROM app.api_keys WHERE id = :id"),
                    {"id": api_key_id},
                )
                for table in ("pricing_history", "swaps_ir", "curves"):
                    await conn.execute(
                        text(
                            f"DELETE FROM app.{table} WHERE owner_uid = :uid"  # noqa: S608
                        ),
                        {"uid": uid},
                    )
                await conn.execute(
                    text("DELETE FROM app.users WHERE uid = :uid"),
                    {"uid": uid},
                )


async def test_cross_tenant_curve_is_invisible_not_forbidden(
    asgi_client: AsyncClient,
    two_seeded_users: tuple[tuple[str, str], tuple[str, str]],
) -> None:
    """User A creates a curve; user B sees 404 (not 403)."""

    (uid_a, key_a), (uid_b, key_b) = two_seeded_users
    assert uid_a != uid_b

    payload = {
        "name": f"private-{uuid.uuid4().hex[:8]}",
        "points": [],
        "body": {"private": True},
    }
    create_a = await asgi_client.post("/v1/curves", json=payload, headers={"X-API-Key": key_a})
    assert create_a.status_code == 201
    curve_id = create_a.json()["id"]

    # User B reading user A's row sees 404, not 403. The envelope
    # must surface ``code="not_found"`` (the row is invisible, not
    # forbidden).
    read_b = await asgi_client.get(f"/v1/curves/{curve_id}", headers={"X-API-Key": key_b})
    assert read_b.status_code == 404
    assert read_b.json()["code"] == "not_found"

    # User B's list excludes user A's curve.
    list_b = await asgi_client.get("/v1/curves", headers={"X-API-Key": key_b})
    assert list_b.status_code == 200
    assert all(item["id"] != curve_id for item in list_b.json()["items"])

    # User B trying to patch / delete / restore the row also sees 404,
    # never 403 — the row simply does not exist from B's vantage.
    for method, url in (
        ("patch", f"/v1/curves/{curve_id}"),
        ("delete", f"/v1/curves/{curve_id}"),
        ("post", f"/v1/curves/{curve_id}:restore"),
    ):
        send = getattr(asgi_client, method)
        kwargs: dict[str, object] = {"headers": {"X-API-Key": key_b}}
        if method == "patch":
            kwargs["json"] = {"currency": "USD"}
        resp = await send(url, **kwargs)
        assert resp.status_code == 404, (method, resp.text)
        assert resp.json()["code"] == "not_found"


# ---------------------------------------------------------------------------
# pricing_history: read-only view
# ---------------------------------------------------------------------------


async def test_pricing_history_read_only_endpoints(
    asgi_client: AsyncClient,
    seeded_user: tuple[str, str],
    admin_engine: AsyncEngine,
) -> None:
    uid, raw_key = seeded_user
    headers = {"X-API-Key": raw_key}

    # Seed two rows directly via the admin engine (no API write path).
    # ``CAST(... AS jsonb)`` instead of the ``::jsonb`` shorthand —
    # SQLAlchemy's ``text()`` parameter parser eats the last character
    # of a name immediately followed by ``::type``.
    seeded_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    async with admin_engine.begin() as conn:
        for resource_id in seeded_ids:
            await conn.execute(
                text(
                    "INSERT INTO app.pricing_history "
                    "  (id, owner_uid, product_kind, product_id, as_of, request, response) "
                    "VALUES "
                    "  (:id, :uid, :kind, NULL, :as_of, "
                    "   CAST(:request AS jsonb), CAST(:response AS jsonb))"
                ),
                {
                    "id": resource_id,
                    "uid": uid,
                    "kind": "swaps_ir",
                    "as_of": date(2026, 5, 15),
                    "request": json.dumps({"sample": "request"}),
                    "response": json.dumps({"sample": "response"}),
                },
            )

    list_resp = await asgi_client.get("/v1/pricing-history", headers=headers)
    assert list_resp.status_code == 200
    items = list_resp.json()["items"]
    listed_ids = {item["id"] for item in items}
    assert set(seeded_ids).issubset(listed_ids)

    read_resp = await asgi_client.get(f"/v1/pricing-history/{seeded_ids[0]}", headers=headers)
    assert read_resp.status_code == 200
    row = read_resp.json()
    assert row["product_kind"] == "swaps_ir"
    assert row["request"] == {"sample": "request"}
    assert row["response"] == {"sample": "response"}
    assert row["product_id"] is None

    # No POST / PATCH / DELETE for the immutable view.
    method_post = await asgi_client.post(
        "/v1/pricing-history",
        json={"product_kind": "swaps_ir"},
        headers=headers,
    )
    assert method_post.status_code == 405

    method_patch = await asgi_client.patch(
        f"/v1/pricing-history/{seeded_ids[0]}",
        json={},
        headers=headers,
    )
    assert method_patch.status_code == 405

    method_delete = await asgi_client.delete(
        f"/v1/pricing-history/{seeded_ids[0]}", headers=headers
    )
    assert method_delete.status_code == 405
