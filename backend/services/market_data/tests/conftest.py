"""Shared fixtures for the MD service test suite."""

from __future__ import annotations

import os
from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_common.settings import Environment, LogLevel
from quantra_market_data.app import create_app
from quantra_market_data.db import get_md_engine
from quantra_market_data.settings import MdServiceSettings


@pytest.fixture
def md_settings() -> MdServiceSettings:
    """Settings instance that bypasses any real DSNs.

    Tests that do not use the DB-backed integration fixture rely on
    ``app.dependency_overrides`` to swap the engine, so we can leave the
    DSN unset here. ``md_engine_lifespan`` is bypassed by overriding
    ``get_md_engine`` before any request fires.
    """

    return MdServiceSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        postgres_dsn_md_ro=None,
    )


@pytest.fixture
def stub_engine() -> AsyncEngine:
    """A stub `AsyncEngine` that satisfies the dependency type contract.

    Routes that hit this fixture without an additional override (e.g.
    ``/health``) never call into it, so ``MagicMock`` is enough.
    """

    return MagicMock(spec=AsyncEngine)


@pytest.fixture
def app(
    md_settings: MdServiceSettings,
    stub_engine: AsyncEngine,
) -> Iterator[FastAPI]:
    """FastAPI app with the engine dependency stubbed out.

    The lifespan handler is bypassed because ``TestClient`` only triggers
    it inside ``with TestClient(app) as ...``; tests that need the real
    lifecycle use the DB-backed fixture below instead.
    """

    instance = create_app(md_settings)
    instance.dependency_overrides[get_md_engine] = lambda: stub_engine
    try:
        yield instance
    finally:
        instance.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Stateless TestClient bound to the stubbed app.

    We *deliberately* do not use ``TestClient`` as a context manager so the
    real lifespan (which would build a live ``md_ro`` engine and fail with
    ``POSTGRES_DSN_MD_RO unset``) is bypassed. The dependency override on
    ``get_md_engine`` returns the stub instead.
    """

    test_client = TestClient(app, raise_server_exceptions=True)
    try:
        yield test_client
    finally:
        test_client.close()


# ---------------------------------------------------------------------------
# DB-backed integration fixtures (skipped unless QUANTRA_MD_TEST_DSN is set).
# ---------------------------------------------------------------------------

QUANTRA_MD_TEST_DSN_ENV: str = "QUANTRA_MD_TEST_DSN"


def _md_dsn_or_skip() -> str:
    dsn = os.environ.get(QUANTRA_MD_TEST_DSN_ENV)
    if not dsn:
        pytest.skip(
            f"Set {QUANTRA_MD_TEST_DSN_ENV} to a postgres+asyncpg DSN with "
            "read access to the legacy MD schema to run integration tests.",
            allow_module_level=False,
        )
    return dsn


@pytest.fixture
def md_test_dsn() -> str:
    """Async DSN of a Postgres reachable by the test process.

    The DSN must point at a database whose tables match the canonical
    ``md`` schema (``snapshots``, ``snapshot_quotes``, ``quote_points``,
    ``canonical_ids``, ``vendor_mappings``). Skips if the env var
    is unset so contributors without that database can still run the
    fast suite.
    """

    return _md_dsn_or_skip()
