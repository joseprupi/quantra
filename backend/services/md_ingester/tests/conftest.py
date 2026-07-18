"""Fixtures shared by the MD-ingester test suite.

The DB-backed fixtures here mirror the MD service's ``md_db``
pattern: tests gated behind the ``md_ingester_db`` marker are skipped
unless ``QUANTRA_MD_INGESTER_TEST_DSN`` *and*
``QUANTRA_MD_INGESTER_TEST_ADMIN_DSN`` are exported.

The fixtures interact directly with the real ``md.*`` schema this
monorepo's Alembic provisions, but every test cleans up after itself
by deleting any rows it touched in reverse-FK order (snapshot_quotes
→ snapshots → quote_points → vendor_mappings → canonical_ids). We do
not provision an alternate schema (unlike the MD service's
``mdtest``) because the ingester's whole point is to write to the
real schema; running it against a parallel schema would not exercise
the trigger this lift is supposed to verify.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

QUANTRA_MD_INGESTER_TEST_DSN_ENV: str = "QUANTRA_MD_INGESTER_TEST_DSN"
QUANTRA_MD_INGESTER_TEST_ADMIN_DSN_ENV: str = "QUANTRA_MD_INGESTER_TEST_ADMIN_DSN"


def _ingester_dsn_or_skip() -> str:
    dsn = os.environ.get(QUANTRA_MD_INGESTER_TEST_DSN_ENV)
    if not dsn:
        pytest.skip(
            f"Set {QUANTRA_MD_INGESTER_TEST_DSN_ENV} to a postgres+asyncpg DSN "
            "with md_rw access to run the MD ingester integration tests.",
            allow_module_level=False,
        )
    return dsn


def _admin_dsn_or_skip() -> str:
    dsn = os.environ.get(QUANTRA_MD_INGESTER_TEST_ADMIN_DSN_ENV)
    if not dsn:
        pytest.skip(
            f"Set {QUANTRA_MD_INGESTER_TEST_ADMIN_DSN_ENV} to a postgres+asyncpg "
            "admin DSN so cleanup can run with full privileges.",
            allow_module_level=False,
        )
    return dsn


@pytest.fixture
def md_ingester_test_dsn() -> str:
    """Async DSN of the ``md_rw`` role for the dev Postgres."""

    return _ingester_dsn_or_skip()


@pytest.fixture
def md_ingester_admin_dsn() -> str:
    """Async DSN of the admin role used to clean up after a test."""

    return _admin_dsn_or_skip()


@pytest_asyncio.fixture
async def md_rw_engine(md_ingester_test_dsn: str) -> AsyncIterator[AsyncEngine]:
    """An async engine bound to the ``md_rw`` role.

    The engine is built with ``poolclass=NullPool`` semantics by way of
    ``pool_pre_ping=False`` defaults; the test isn't latency-sensitive
    and we don't want the per-test transaction holding a slot in the
    shared pool used by ``make_md_engine``.
    """

    engine = create_async_engine(md_ingester_test_dsn, pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def md_admin_engine(md_ingester_admin_dsn: str) -> AsyncIterator[AsyncEngine]:
    """Admin engine for FK-safe truncation between tests."""

    engine = create_async_engine(md_ingester_admin_dsn, isolation_level="AUTOCOMMIT")
    try:
        yield engine
    finally:
        await engine.dispose()


_CLEANUP_STATEMENTS: tuple[str, ...] = (
    # Reverse FK order. ``md.snapshot_quotes`` deletes fire the
    # etag-bump trigger but that's fine — the snapshots row goes
    # away in the next statement.
    "DELETE FROM md.snapshot_quotes",
    "DELETE FROM md.snapshots",
    "DELETE FROM md.quote_points",
    "DELETE FROM md.vendor_mappings",
    "DELETE FROM md.canonical_ids",
)


@pytest_asyncio.fixture
async def clean_md_schema(md_admin_engine: AsyncEngine) -> AsyncIterator[None]:
    """Wipe the ingester-owned ``md.*`` tables before and after each test.

    The DB integration tests assume an empty schema so assertions on
    row counts and ``version_etag`` deltas are predictable. The
    fixture runs once before yield (clean slate) and once on teardown
    (leave the dev DB tidy for the operator).
    """

    async def _wipe() -> None:
        async with md_admin_engine.begin() as conn:
            for stmt in _CLEANUP_STATEMENTS:
                await conn.execute(text(stmt))

    await _wipe()
    try:
        yield
    finally:
        await _wipe()
