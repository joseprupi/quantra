"""Tests for `quantra_common.db`."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import QueuePool

from quantra_common.db import (
    DbRole,
    DbSchema,
    alembic_version_table,
    make_app_engine,
    make_engine,
    make_md_engine,
    pool_kwargs_for,
    pool_stats,
)
from quantra_common.settings.base import Settings


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_pool_kwargs_per_role_independent() -> None:
    """Each (schema, role) pair pulls its own size + overflow from settings."""

    s = _settings(
        pg_pool_size_app_rw=20,
        pg_pool_max_overflow_app_rw=10,
        pg_pool_size_app_ro=8,
        pg_pool_max_overflow_app_ro=4,
        pg_pool_size_md_rw=4,
        pg_pool_max_overflow_md_rw=4,
        pg_pool_size_md_ro=12,
        pg_pool_max_overflow_md_ro=6,
        pg_pool_timeout_s=7.5,
    )
    app_rw = pool_kwargs_for(DbSchema.APP, DbRole.RW, s)
    app_ro = pool_kwargs_for(DbSchema.APP, DbRole.RO, s)
    md_rw = pool_kwargs_for(DbSchema.MD, DbRole.RW, s)
    md_ro = pool_kwargs_for(DbSchema.MD, DbRole.RO, s)

    assert (app_rw["pool_size"], app_rw["max_overflow"]) == (20, 10)
    assert (app_ro["pool_size"], app_ro["max_overflow"]) == (8, 4)
    assert (md_rw["pool_size"], md_rw["max_overflow"]) == (4, 4)
    assert (md_ro["pool_size"], md_ro["max_overflow"]) == (12, 6)
    assert all(kw["pool_timeout"] == 7.5 for kw in (app_rw, app_ro, md_rw, md_ro))
    assert all(kw["pool_pre_ping"] is True for kw in (app_rw, app_ro, md_rw, md_ro))


def test_make_engine_raises_when_dsn_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("POSTGRES_DSN_APP_RW", "POSTGRES_DSN_APP_RO"):
        monkeypatch.delenv(var, raising=False)

    s = _settings()
    with pytest.raises(RuntimeError, match="POSTGRES_DSN_APP_RW"):
        make_app_engine(DbRole.RW, settings=s)
    with pytest.raises(RuntimeError, match="POSTGRES_DSN_APP_RO"):
        make_app_engine("ro", settings=s)


def test_make_engine_routes_md_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_DSN_MD_RW", "postgresql+asyncpg://md_rw:p@h/db")
    monkeypatch.setenv("POSTGRES_DSN_MD_RO", "postgresql+asyncpg://md_ro:p@h/db")
    s = _settings()

    captured: dict[str, Any] = {}

    def fake_create(url: str, **kwargs: Any) -> AsyncEngine:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return MagicMock(spec=AsyncEngine)

    with patch("quantra_common.db.engine.create_async_engine", side_effect=fake_create):
        make_md_engine(DbRole.RW, settings=s)
    assert captured["url"] == "postgresql+asyncpg://md_rw:p@h/db"
    assert captured["kwargs"]["pool_size"] == s.pg_pool_size_md_rw
    assert captured["kwargs"]["max_overflow"] == s.pg_pool_max_overflow_md_rw

    captured.clear()
    with patch("quantra_common.db.engine.create_async_engine", side_effect=fake_create):
        make_engine(DbSchema.MD, DbRole.RO, settings=s)
    assert captured["url"] == "postgresql+asyncpg://md_ro:p@h/db"
    assert captured["kwargs"]["pool_size"] == s.pg_pool_size_md_ro


def test_pool_stats_returns_zero_for_non_queue_pools() -> None:
    """Engines without a counting pool (e.g. NullPool in tests) yield zeros.

    The shape stays stable so consumers don't have to special-case the
    "pool not yet warmed" or "pool not a QueuePool" paths.
    """

    engine = MagicMock(spec=AsyncEngine)
    sync_engine = MagicMock()
    sync_engine.pool = MagicMock()  # NOT a QueuePool instance.
    engine.sync_engine = sync_engine

    stats = pool_stats(engine)
    assert stats == {"pool_size": 0, "checked_out": 0, "checked_in": 0, "overflow": 0}


def test_pool_stats_reads_queue_pool_counters() -> None:
    """When the engine wraps a real QueuePool, pool_stats forwards the counters."""

    engine = MagicMock(spec=AsyncEngine)
    sync_engine = MagicMock()
    pool = MagicMock(spec=QueuePool)
    pool.size.return_value = 4
    pool.checkedout.return_value = 3
    pool.checkedin.return_value = 1
    pool.overflow.return_value = 2
    sync_engine.pool = pool
    engine.sync_engine = sync_engine

    assert pool_stats(engine) == {
        "pool_size": 4,
        "checked_out": 3,
        "checked_in": 1,
        "overflow": 2,
    }


def test_db_role_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="rwx"):
        DbRole("rwx")


# ----- alembic helpers -------------------------------------------------------


def test_alembic_version_table_per_schema() -> None:
    assert alembic_version_table(DbSchema.APP) == "alembic_version_app"
    assert alembic_version_table(DbSchema.MD) == "alembic_version_md"
