"""Alembic environment for the Quantra single-Postgres, dual-schema setup.

One env, two schemas: ``app.*`` (user data) and ``md.*`` (market
data). Each has its own version table and its own
``versions/<schema>/`` directory so migrations advance independently.

Schema selection is done with Alembic's named-section flag, which is the
only way to vary ``version_locations`` and ``version_table`` per
invocation (those are read from config *before* env.py executes):

    uv run alembic -n app upgrade head
    uv run alembic -n md  upgrade head

The connection URL comes from ``Settings.require_postgres_dsn_admin()`` —
migrations need superuser/owner privileges (CREATE ROLE, CREATE EXTENSION,
CREATE SCHEMA). Runtime services keep using their per-role DSNs.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from quantra_common.db.alembic import DbSchema, alembic_version_table
from quantra_common.settings.base import Settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _resolve_schema() -> DbSchema:
    section = config.config_ini_section
    valid = {s.value for s in DbSchema}
    if section not in valid:
        msg = (
            f"Run alembic with `-n {{{','.join(sorted(valid))}}}` "
            f"(got section {section!r}). The default `[alembic]` section "
            "intentionally has no version_locations to prevent applying "
            "migrations against the wrong version table."
        )
        raise RuntimeError(msg)
    return DbSchema(section)


def _admin_url() -> str:
    """Resolve the admin DSN, normalized to an asyncpg URL."""

    url = Settings().require_postgres_dsn_admin()
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


SCHEMA: DbSchema = _resolve_schema()
EXPECTED_TABLE: str = alembic_version_table(SCHEMA)
_INI_TABLE: str | None = config.get_main_option("version_table")
if _INI_TABLE != EXPECTED_TABLE:  # defence in depth — keep ini and code in sync
    msg = (
        f"alembic.ini section [{SCHEMA.value}] declares "
        f"version_table={_INI_TABLE!r}; expected {EXPECTED_TABLE!r}."
    )
    raise RuntimeError(msg)

VERSION_TABLE_SCHEMA: str = config.get_main_option("version_table_schema") or "public"

config.set_main_option("sqlalchemy.url", _admin_url())


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        version_table=EXPECTED_TABLE,
        # Per-schema version table lives in `public` so neither runtime
        # role needs to see it (the app/md roles' search_path is locked
        # to their own schema).
        version_table_schema=VERSION_TABLE_SCHEMA,
        # Useful once entity tables land in 02_app_schema / 03_md_schema;
        # harmless for the bootstrap migration.
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    section: dict[str, Any] = dict(config.get_section(config.config_ini_section, {}) or {})
    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        version_table=EXPECTED_TABLE,
        version_table_schema=VERSION_TABLE_SCHEMA,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
