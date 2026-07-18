"""Async SQLAlchemy 2.x engine helpers.

Two logical schemas live in one Postgres instance:

- ``app.*`` — user-facing data, owned by the orchestrator.
- ``md.*``  — market data, owned by the MD service (read) and ingester (write).

This module exposes engine factories per *(schema, role)* tuple.
Connection pool sizing is read from ``Settings`` and capped per role so a
spike on one schema cannot starve the other.

Migrations are managed by a single Alembic environment (``migrations/alembic/``)
with two version tables (``alembic_version_app`` and ``alembic_version_md``)
so they can advance independently — see :mod:`quantra_common.db.alembic`.
"""

from __future__ import annotations

from quantra_common.db.alembic import (
    DbSchema,
    alembic_version_table,
)
from quantra_common.db.engine import (
    DbRole,
    make_app_engine,
    make_engine,
    make_md_engine,
    pool_kwargs_for,
    pool_stats,
)

__all__ = [
    "DbRole",
    "DbSchema",
    "alembic_version_table",
    "make_app_engine",
    "make_engine",
    "make_md_engine",
    "pool_kwargs_for",
    "pool_stats",
]
