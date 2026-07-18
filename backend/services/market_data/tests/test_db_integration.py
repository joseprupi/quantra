"""DB-backed integration tests for the lifted MD service.

Skipped by default. To run locally:

1. Boot the monorepo Postgres (``docker compose up -d postgres``) and apply
   the bootstrap migrations once (``uv run alembic -n app upgrade head &&
   uv run alembic -n md upgrade head``) — this creates the ``md_ro`` role
   and the ``quantra`` admin role we use to provision the legacy-shaped
   fixture tables.
2. Export the DSNs the fixture needs:

   ::

       export QUANTRA_MD_TEST_DSN=postgresql+asyncpg://md_ro:md_ro@localhost:5434/quantra
       export QUANTRA_MD_TEST_ADMIN_DSN=postgresql+asyncpg://quantra:quantra@localhost:5434/quantra

3. Run the marker:

   ::

       uv run pytest -m md_db services/market_data

The fixture creates a dedicated ``mdtest`` schema, mirrors the canonical
``md`` table layout (``quote_points``, ``canonical_ids``, ...) into it,
grants ``md_ro`` read access, then **temporarily** re-points ``md_ro``'s
``search_path`` at ``mdtest`` for the duration of the test. The schema is
dropped and the role's ``search_path`` is restored on teardown so the
test leaves no residue. This exercises exactly the canonical relation
names the service now SELECTs (the ``quotes_timeseries`` /
``canonical_instruments`` compatibility views were dropped) against a
real Postgres without touching the ``md.*`` schema we provisioned for
the schema migration.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from quantra_common.settings import Environment, LogLevel
from quantra_market_data.app import create_app
from quantra_market_data.settings import MdServiceSettings

pytestmark = pytest.mark.md_db

_ADMIN_DSN_ENV: str = "QUANTRA_MD_TEST_ADMIN_DSN"
_TEST_SCHEMA: str = "mdtest"

_LEGACY_DDL: tuple[str, ...] = (
    f"CREATE SCHEMA IF NOT EXISTS {_TEST_SCHEMA}",
    f"GRANT USAGE ON SCHEMA {_TEST_SCHEMA} TO md_ro",
    f"""
    CREATE TABLE {_TEST_SCHEMA}.snapshots (
        id UUID PRIMARY KEY,
        name TEXT NOT NULL,
        as_of TIMESTAMPTZ NOT NULL,
        status TEXT NOT NULL,
        meta JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (name, as_of)
    )
    """,
    f"""
    CREATE TABLE {_TEST_SCHEMA}.snapshot_quotes (
        snapshot_id UUID NOT NULL,
        canonical_id TEXT NOT NULL,
        value DOUBLE PRECISION NOT NULL,
        meta JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (snapshot_id, canonical_id)
    )
    """,
    f"""
    CREATE TABLE {_TEST_SCHEMA}.canonical_ids (
        canonical_id TEXT PRIMARY KEY,
        asset_class TEXT NOT NULL,
        family TEXT,
        instrument TEXT NOT NULL,
        currency TEXT NOT NULL,
        tenor TEXT,
        field TEXT NOT NULL,
        frequency TEXT NOT NULL DEFAULT 'daily',
        units TEXT NOT NULL DEFAULT 'decimal_rate',
        description TEXT
    )
    """,
    f"""
    CREATE TABLE {_TEST_SCHEMA}.vendor_mappings (
        vendor TEXT NOT NULL,
        vendor_id TEXT NOT NULL,
        canonical_id TEXT NOT NULL REFERENCES {_TEST_SCHEMA}.canonical_ids(canonical_id),
        active BOOLEAN NOT NULL DEFAULT TRUE,
        PRIMARY KEY (vendor, vendor_id)
    )
    """,
    f"""
    CREATE TABLE {_TEST_SCHEMA}.quote_points (
        canonical_id TEXT NOT NULL,
        as_of TIMESTAMPTZ NOT NULL,
        value DOUBLE PRECISION NOT NULL,
        source TEXT,
        vendor_id TEXT,
        quality_flags JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        meta JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        PRIMARY KEY (canonical_id, as_of)
    )
    """,
    f"GRANT SELECT ON ALL TABLES IN SCHEMA {_TEST_SCHEMA} TO md_ro",
)


def _admin_dsn_or_skip() -> str:
    dsn = os.environ.get(_ADMIN_DSN_ENV)
    if not dsn:
        pytest.skip(
            f"Set {_ADMIN_DSN_ENV} to a postgres+asyncpg DSN with admin "
            "privileges so the integration fixture can provision a schema.",
            allow_module_level=False,
        )
    return dsn


def _admin_engine() -> AsyncEngine:
    return create_async_engine(_admin_dsn_or_skip(), isolation_level="AUTOCOMMIT")


@pytest_asyncio.fixture
async def mdtest_schema() -> AsyncIterator[str]:
    """Provision the legacy-shaped tables in ``mdtest`` and drop on exit."""

    admin_engine = _admin_engine()
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {_TEST_SCHEMA} CASCADE"))
            for stmt in _LEGACY_DDL:
                await conn.execute(text(stmt))
            await conn.execute(text(f"ALTER ROLE md_ro SET search_path TO {_TEST_SCHEMA}"))
        yield _TEST_SCHEMA
    finally:
        async with admin_engine.connect() as conn:
            await conn.execute(text("ALTER ROLE md_ro SET search_path TO md"))
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {_TEST_SCHEMA} CASCADE"))
        await admin_engine.dispose()


_SEED_INSTRUMENT_SQL: str = (
    "INSERT INTO {schema}.canonical_ids "
    "(canonical_id, asset_class, family, instrument, currency, tenor, field, description) "
    "VALUES ('USD.RATES.UST.DGS.10Y.YIELD', 'RATES', 'UST', 'DGS', 'USD', "
    "'10Y', 'YIELD', 'UST 10Y yield')"
)
_SEED_QUOTE_SQL: str = (
    "INSERT INTO {schema}.quote_points "
    "(canonical_id, as_of, value, source, vendor_id) "
    "VALUES ('USD.RATES.UST.DGS.10Y.YIELD', '2026-05-13T00:00:00Z', "
    "0.0432, 'fred', 'DGS10')"
)


@pytest_asyncio.fixture
async def seeded_app(mdtest_schema: str, md_test_dsn: str) -> AsyncIterator[Any]:
    """Boot the FastAPI app via its real lifespan and seed a tiny dataset."""

    settings = MdServiceSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        postgres_dsn_md_ro=md_test_dsn,
    )
    app = create_app(settings)

    admin_engine = _admin_engine()
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(text(_SEED_INSTRUMENT_SQL.format(schema=mdtest_schema)))
            await conn.execute(text(_SEED_QUOTE_SQL.format(schema=mdtest_schema)))
    finally:
        await admin_engine.dispose()

    with TestClient(app, raise_server_exceptions=True) as test_client:
        app.state.test_client = test_client
        yield app


def test_health_works_with_real_lifespan(seeded_app: Any) -> None:
    response = seeded_app.state.test_client.get("/health")
    assert response.status_code == 200


def test_series_range_returns_seeded_value(seeded_app: Any) -> None:
    response = seeded_app.state.test_client.get(
        "/series/USD.RATES.UST.DGS.10Y.YIELD",
        params={
            "start": "2026-05-01T00:00:00Z",
            "end": "2026-05-31T23:59:59Z",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["canonical_id"] == "USD.RATES.UST.DGS.10Y.YIELD"
    assert body[0]["value"] == pytest.approx(0.0432)
    assert body[0]["source"] == "fred"
    assert body[0]["vendor_id"] == "DGS10"


def test_catalog_series_returns_seeded_canonical(seeded_app: Any) -> None:
    response = seeded_app.state.test_client.get("/catalog/series")
    assert response.status_code == 200
    body = response.json()
    canonical_ids = {row["canonical_id"] for row in body}
    assert "USD.RATES.UST.DGS.10Y.YIELD" in canonical_ids
