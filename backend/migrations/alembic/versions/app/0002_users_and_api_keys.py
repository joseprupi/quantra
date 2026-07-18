"""users, api_keys, and the shared updated_at trigger function

Foundation tables for the `app.*` schema (the schema design):

* ``app.users`` — one row per Firebase-authenticated user. ``uid`` (the
  Firebase UID) is the natural PK; everything else in the schema FKs to it
  with ``ON DELETE RESTRICT`` so a user cannot be removed while owned
  records linger. Hard-delete remains available through an admin path.
* ``app.api_keys`` — programmatic credentials for non-portal clients
  (Python CLI, Excel, external API). ``key_hash`` is the SHA-256 of the
  raw key; raw keys are never persisted. ``active`` + ``revoked_at``
  carries the lifecycle without soft-delete on the row itself (these
  rows aren't human-named so no name uniqueness is needed).
* ``app.set_updated_at`` — generic ``BEFORE UPDATE`` trigger function
  reused by every entity table in subsequent 0003+ migrations.

The schema-creation, role-creation, grant-issuance, and search_path
plumbing all happened in ``0001_init.py``; this migration only adds
tables, indexes, the trigger function and the per-table triggers.

Revision ID: 0002_users_and_api_keys
Revises: 0001_init
Create Date: 2026-05-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_users_and_api_keys"
down_revision: str | None = "0001_init"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Shared trigger function used by every entity table that carries an
    # ``updated_at`` column. Lives in `app` so the role's pinned
    # search_path can find it without qualification.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.set_updated_at() RETURNS trigger AS $$
        BEGIN
          NEW.updated_at = now();
          RETURN NEW;
        END
        $$ LANGUAGE plpgsql;
        """
    )

    # `app.users` — Firebase UIDs are the natural PK; we never mint our own
    # user IDs. `tier` is free-form text today (e.g. "free", "pro", "internal");
    # tightening to an enum can wait until the orchestrator actually uses it.
    op.create_table(
        "users",
        sa.Column("uid", sa.Text(), primary_key=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("tier", sa.Text(), nullable=False, server_default=sa.text("'free'")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("length(uid) > 0", name="users_uid_nonempty"),
        sa.CheckConstraint(
            "email IS NULL OR position('@' in email) > 1",
            name="users_email_shape",
        ),
        schema="app",
    )
    op.execute(
        """
        CREATE TRIGGER users_set_updated_at
          BEFORE UPDATE ON app.users
          FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();
        """
    )

    # `app.api_keys` — `key_hash` stores a SHA-256 (hex) of the raw key. The
    # raw key is shown to the user exactly once at creation time and never
    # persisted. `active`/`revoked_at` is the lifecycle;
    # there's no soft-delete because these rows aren't user-named.
    op.create_table(
        "api_keys",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("public.gen_random_uuid()"),
        ),
        sa.Column(
            "owner_uid",
            sa.Text(),
            sa.ForeignKey("app.users.uid", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("length(trim(name)) > 0", name="api_keys_name_nonempty"),
        sa.CheckConstraint("length(key_hash) > 0", name="api_keys_key_hash_nonempty"),
        schema="app",
    )
    op.create_index("ix_api_keys_owner_uid", "api_keys", ["owner_uid"], schema="app")
    op.create_index(
        "ix_api_keys_owner_active",
        "api_keys",
        ["owner_uid", "active"],
        schema="app",
        postgresql_where=sa.text("active"),
    )
    op.execute(
        """
        CREATE TRIGGER api_keys_set_updated_at
          BEFORE UPDATE ON app.api_keys
          FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS api_keys_set_updated_at ON app.api_keys")
    op.drop_index("ix_api_keys_owner_active", table_name="api_keys", schema="app")
    op.drop_index("ix_api_keys_owner_uid", table_name="api_keys", schema="app")
    op.drop_table("api_keys", schema="app")

    op.execute("DROP TRIGGER IF EXISTS users_set_updated_at ON app.users")
    op.drop_table("users", schema="app")

    op.execute("DROP FUNCTION IF EXISTS app.set_updated_at()")
