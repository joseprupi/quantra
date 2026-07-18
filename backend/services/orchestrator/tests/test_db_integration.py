"""DB-backed integration tests for the orchestrator's auth lookup.

Skipped by default behind the ``orchestrator_db`` marker (same
pattern). To run locally:

1. Boot the monorepo Postgres and apply migrations once::

       docker compose up -d postgres
       uv run alembic -n app upgrade head

2. Export the two DSNs the fixtures need::

       export QUANTRA_ORCHESTRATOR_TEST_APP_RO_DSN=\\
           postgresql+asyncpg://app_ro:app_ro@localhost:5434/quantra
       export QUANTRA_ORCHESTRATOR_TEST_ADMIN_DSN=\\
           postgresql+asyncpg://quantra:quantra@localhost:5434/quantra

3. Run the marker::

       uv run pytest -m orchestrator_db services/orchestrator

The admin DSN is required because the fixture inserts a synthetic
``app.users`` row + ``app.api_keys`` row (the test owns its lifecycle:
insert before, delete after). The orchestrator runtime itself never
needs admin — the lookup path uses ``app_ro`` only.
"""

from __future__ import annotations

import os
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from quantra_common.settings import Environment, LogLevel
from quantra_orchestrator.app import create_app
from quantra_orchestrator.auth.api_keys import SqlApiKeyLookup, hash_api_key
from quantra_orchestrator.settings import OrchestratorSettings

pytestmark = pytest.mark.orchestrator_db

QUANTRA_ORCHESTRATOR_TEST_APP_RO_DSN_ENV: str = "QUANTRA_ORCHESTRATOR_TEST_APP_RO_DSN"
QUANTRA_ORCHESTRATOR_TEST_ADMIN_DSN_ENV: str = "QUANTRA_ORCHESTRATOR_TEST_ADMIN_DSN"


def _app_ro_dsn_or_skip() -> str:
    dsn = os.environ.get(QUANTRA_ORCHESTRATOR_TEST_APP_RO_DSN_ENV)
    if not dsn:
        pytest.skip(
            f"Set {QUANTRA_ORCHESTRATOR_TEST_APP_RO_DSN_ENV} to a postgres+asyncpg DSN "
            "with app_ro access to run the orchestrator auth integration tests.",
            allow_module_level=False,
        )
    return dsn


def _admin_dsn_or_skip() -> str:
    dsn = os.environ.get(QUANTRA_ORCHESTRATOR_TEST_ADMIN_DSN_ENV)
    if not dsn:
        pytest.skip(
            f"Set {QUANTRA_ORCHESTRATOR_TEST_ADMIN_DSN_ENV} to a postgres+asyncpg "
            "admin DSN so the fixture can insert + clean up app.* rows.",
            allow_module_level=False,
        )
    return dsn


@pytest.fixture
def app_ro_dsn() -> str:
    return _app_ro_dsn_or_skip()


@pytest.fixture
def admin_dsn() -> str:
    return _admin_dsn_or_skip()


@pytest_asyncio.fixture
async def app_ro_engine(app_ro_dsn: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(app_ro_dsn, pool_pre_ping=True)
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


@asynccontextmanager
async def _seeded_api_key(
    engine: AsyncEngine,
    *,
    uid: str,
    email: str,
    key_name: str,
    raw_key: str,
    active: bool = True,
) -> AsyncIterator[uuid.UUID]:
    """Insert a (user, api_key) pair and ``DELETE`` it on exit.

    Lives as a contextmanager rather than a pytest fixture so an
    individual test can seed multiple keys / users without fighting
    fixture scope. The cleanup runs even if the body raises.
    """

    digest = hash_api_key(raw_key)
    api_key_id = uuid.uuid4()

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO app.users (uid, email, display_name, tier) "
                "VALUES (:uid, :email, :name, 'free') "
                "ON CONFLICT (uid) DO NOTHING"
            ),
            {"uid": uid, "email": email, "name": "Integration Test User"},
        )
        await conn.execute(
            text(
                "INSERT INTO app.api_keys (id, owner_uid, name, key_hash, active) "
                "VALUES (:id, :uid, :name, :key_hash, :active)"
            ),
            {
                "id": str(api_key_id),
                "uid": uid,
                "name": key_name,
                "key_hash": digest,
                "active": active,
            },
        )

    try:
        yield api_key_id
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM app.api_keys WHERE id = :id"),
                {"id": str(api_key_id)},
            )
            await conn.execute(
                text("DELETE FROM app.users WHERE uid = :uid"),
                {"uid": uid},
            )


async def test_sql_lookup_resolves_active_key_against_real_row(
    app_ro_engine: AsyncEngine,
    admin_engine: AsyncEngine,
) -> None:
    raw_key = f"qkk_test_{secrets.token_urlsafe(24)}"
    async with _seeded_api_key(
        admin_engine,
        uid="orchestrator-test-uid-1",
        email="orch-int@example.com",
        key_name="Integration Test Key",
        raw_key=raw_key,
    ) as api_key_id:
        lookup = SqlApiKeyLookup(app_ro_engine)

        record = await lookup(raw_key)

    assert record is not None
    assert record.api_key_id == str(api_key_id)
    assert record.owner_uid == "orchestrator-test-uid-1"
    assert record.email == "orch-int@example.com"
    assert record.tier == "free"
    assert record.active is True
    assert record.name == "Integration Test Key"


async def test_sql_lookup_returns_none_for_unknown_key(
    app_ro_engine: AsyncEngine,
) -> None:
    lookup = SqlApiKeyLookup(app_ro_engine)
    assert await lookup(f"qkk_unknown_{secrets.token_urlsafe(16)}") is None


async def test_sql_lookup_returns_inactive_record_for_revoked_key(
    app_ro_engine: AsyncEngine,
    admin_engine: AsyncEngine,
) -> None:
    raw_key = f"qkk_test_{secrets.token_urlsafe(24)}"
    async with _seeded_api_key(
        admin_engine,
        uid="orchestrator-test-uid-2",
        email="orch-int-revoked@example.com",
        key_name="Revoked Key",
        raw_key=raw_key,
        active=False,
    ):
        lookup = SqlApiKeyLookup(app_ro_engine)
        record = await lookup(raw_key)

    assert record is not None
    assert record.active is False


async def test_whoami_round_trips_against_real_api_key_row(
    app_ro_engine: AsyncEngine,
    admin_engine: AsyncEngine,
) -> None:
    raw_key = f"qkk_test_{secrets.token_urlsafe(24)}"
    settings = OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="int-testsha",
    )
    async with _seeded_api_key(
        admin_engine,
        uid="orchestrator-test-uid-3",
        email="orch-int-whoami@example.com",
        key_name="Whoami Test Key",
        raw_key=raw_key,
    ):
        lookup = SqlApiKeyLookup(app_ro_engine)
        instance: FastAPI = create_app(settings, api_key_lookup=lookup)
        transport = ASGITransport(app=instance)
        async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
            ok_response = await ac.get("/auth/whoami", headers={"X-API-Key": raw_key})
            bad_response = await ac.get(
                "/auth/whoami", headers={"X-API-Key": "definitely-not-a-real-key"}
            )

    assert ok_response.status_code == 200
    assert ok_response.json() == {
        "uid": "orchestrator-test-uid-3",
        "email": "orch-int-whoami@example.com",
        "auth_method": "api_key",
    }
    assert bad_response.status_code == 401
    assert bad_response.json()["code"] == "unauthenticated"
