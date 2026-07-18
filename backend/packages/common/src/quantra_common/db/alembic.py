"""Alembic helpers shared between services and the migration environment.

One Alembic environment (``migrations/alembic/``) serves the whole monorepo
with *two* independent version tables — one per schema — so app and md
migrations can advance independently.
"""

from __future__ import annotations

from enum import StrEnum


class DbSchema(StrEnum):
    """The two logical schemas inside the single Postgres instance."""

    APP = "app"
    MD = "md"


def alembic_version_table(schema: DbSchema) -> str:
    """Name of the per-schema version table.

    Two separate tables let app and md migrations advance independently —
    deploying an MD-only ingester change must not block an unrelated app
    migration and vice versa.
    """

    return f"alembic_version_{schema.value}"
