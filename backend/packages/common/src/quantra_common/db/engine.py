"""Engine factories for the two schemas x two roles.

Engines are intentionally NOT cached at module level — services own the
lifecycle (start: build engines; shutdown: ``await engine.dispose()``).
A small ``functools.lru_cache`` would tie engine teardown to interpreter
exit, which is the wrong behavior for tests and reload servers.

Connection pool sizing is **per role**, not per schema, so a spike on
``md_rw`` (the ingester) cannot exhaust the slots reserved for
``app_rw`` (orchestrator writes). The numbers come from
``Settings.pg_pool_size_<schema>_<role>`` /
``Settings.pg_pool_max_overflow_<schema>_<role>``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    create_async_engine,
)
from sqlalchemy.pool import Pool, QueuePool

from quantra_common.db.alembic import DbSchema
from quantra_common.settings.base import Settings


class DbRole(StrEnum):
    """Postgres role to use for an engine.

    The ``rw`` and ``ro`` roles are provisioned per schema; pool sizing
    is per role per process.
    """

    RW = "rw"
    RO = "ro"


def pool_kwargs_for(schema: DbSchema, role: DbRole, settings: Settings) -> dict[str, Any]:
    """Return engine pool kwargs for a given (schema, role), sourced from settings.

    Each of the four (schema, role) pairs gets its own ``pool_size`` /
    ``max_overflow`` / ``pool_timeout`` so the four pools are independent.
    SQLAlchemy raises ``sqlalchemy.exc.TimeoutError`` (a subclass of
    ``builtins.TimeoutError``) when a checkout exceeds ``pool_timeout``,
    which is the contract the saturation runbook relies on.
    """

    pool_size, max_overflow = _pool_caps(schema, role, settings)
    return {
        "pool_size": pool_size,
        "max_overflow": max_overflow,
        "pool_timeout": settings.pg_pool_timeout_s,
        "pool_pre_ping": True,
    }


def _pool_caps(schema: DbSchema, role: DbRole, settings: Settings) -> tuple[int, int]:
    if schema is DbSchema.APP:
        if role is DbRole.RW:
            return settings.pg_pool_size_app_rw, settings.pg_pool_max_overflow_app_rw
        return settings.pg_pool_size_app_ro, settings.pg_pool_max_overflow_app_ro
    if role is DbRole.RW:
        return settings.pg_pool_size_md_rw, settings.pg_pool_max_overflow_md_rw
    return settings.pg_pool_size_md_ro, settings.pg_pool_max_overflow_md_ro


def make_engine(
    schema: DbSchema,
    role: DbRole,
    *,
    settings: Settings | None = None,
    echo: bool = False,
) -> AsyncEngine:
    """Build an async SQLAlchemy engine for a (schema, role).

    Reads the matching DSN from settings and applies the per-role pool config.
    Raises ``RuntimeError`` if the DSN environment variable is unset.
    """

    cfg = settings or Settings()
    dsn = _select_dsn(cfg, schema, role)
    return create_async_engine(dsn, echo=echo, **pool_kwargs_for(schema, role, cfg))


def make_app_engine(
    role: DbRole | str,
    *,
    settings: Settings | None = None,
    echo: bool = False,
) -> AsyncEngine:
    """Engine for the ``app.*`` schema. ``role`` may be ``"rw"`` or ``"ro"``."""

    return make_engine(DbSchema.APP, DbRole(role), settings=settings, echo=echo)


def make_md_engine(
    role: DbRole | str,
    *,
    settings: Settings | None = None,
    echo: bool = False,
) -> AsyncEngine:
    """Engine for the ``md.*`` schema. ``role`` may be ``"rw"`` or ``"ro"``."""

    return make_engine(DbSchema.MD, DbRole(role), settings=settings, echo=echo)


def pool_stats(engine: AsyncEngine) -> dict[str, int]:
    """Return a snapshot of the engine's pool counters.

    Light-weight on purpose. The numbers come straight from SQLAlchemy's
    ``QueuePool`` and are useful both for a debug endpoint and for asserting
    that ``app_rw`` checkouts succeed while ``md_rw`` is fully checked out.

    Falls back to zeros if the engine uses a non-counting pool (e.g. NullPool
    in tests). The return shape is stable so callers don't have to branch.
    """

    pool: Pool = engine.sync_engine.pool
    if not isinstance(pool, QueuePool):
        return {
            "pool_size": 0,
            "checked_out": 0,
            "checked_in": 0,
            "overflow": 0,
        }
    return {
        "pool_size": pool.size(),
        "checked_out": pool.checkedout(),
        "checked_in": pool.checkedin(),
        # ``overflow()`` returns the *current* overflow, which can be
        # negative when fewer than ``pool_size`` connections are open.
        "overflow": pool.overflow(),
    }


def _select_dsn(settings: Settings, schema: DbSchema, role: DbRole) -> str:
    if schema is DbSchema.APP:
        if role is DbRole.RW:
            return settings.require_postgres_dsn_app_rw()
        return settings.require_postgres_dsn_app_ro()
    if role is DbRole.RW:
        return settings.require_postgres_dsn_md_rw()
    return settings.require_postgres_dsn_md_ro()
