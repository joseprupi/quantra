"""init app schema, roles, and grants

Provisions the user-data side of the single Postgres instance:

* Schema ``app`` with PUBLIC stripped of all rights.
* Roles ``app_rw`` (DML) and ``app_ro`` (SELECT only), each pinned to a
  ``search_path`` of ``app``.
* Default privileges so future tables created in this schema by the
  migration runner inherit the right grants automatically.
* The shared extensions ``pgcrypto``, ``pg_trgm`` and ``btree_gist``
  installed in ``public``. Both schemas need them; whichever migration
  runs first creates them, the other no-ops via ``IF NOT EXISTS``.

The migration is fully idempotent so a partially-applied attempt can be
re-run without manual cleanup.

Revision ID: 0001_init
Revises:
Create Date: 2026-05-14
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_init"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Dev-only credentials. Production secrets are rotated by the deploy
# pipeline immediately after the migration completes (out of scope here);
# this migration only guarantees the role exists with *some* password.
_DEV_PASSWORDS = {
    "app_rw": "app_rw",
    "app_ro": "app_ro",
}


def upgrade() -> None:
    # Shared extensions live in `public` so both schemas can reference them
    # via fully-qualified calls (e.g. `public.gen_random_uuid()`). Splitting
    # them per-schema would force two installs of the same extension.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public")
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public")

    op.execute(
        f"""
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_rw') THEN
            CREATE ROLE app_rw LOGIN PASSWORD '{_DEV_PASSWORDS["app_rw"]}';
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_ro') THEN
            CREATE ROLE app_ro LOGIN PASSWORD '{_DEV_PASSWORDS["app_ro"]}';
          END IF;
        END
        $$;
        """  # noqa: S608
    )

    op.execute("CREATE SCHEMA IF NOT EXISTS app")
    op.execute("REVOKE ALL ON SCHEMA app FROM PUBLIC")

    op.execute("GRANT USAGE ON SCHEMA app TO app_rw, app_ro")

    # Default privileges apply only to objects created by the role that
    # ran ALTER DEFAULT PRIVILEGES. Run all migrations as the same admin
    # role (the one in POSTGRES_DSN_ADMIN) so future entity tables in
    # 02_app_schema.md inherit these grants without re-stating them.
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA app
          GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_rw
        """
    )
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA app
          GRANT SELECT ON TABLES TO app_ro
        """
    )
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA app
          GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO app_rw
        """
    )
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA app
          GRANT USAGE, SELECT ON SEQUENCES TO app_ro
        """
    )
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA app
          GRANT EXECUTE ON FUNCTIONS TO app_rw, app_ro
        """
    )

    # Pin search_path so a connecting role only sees its own schema.
    # Cluster-wide (no IN DATABASE) so prod / staging / dev all behave
    # identically regardless of database name.
    op.execute("ALTER ROLE app_rw SET search_path TO app")
    op.execute("ALTER ROLE app_ro SET search_path TO app")


def downgrade() -> None:
    # Best-effort teardown. We deliberately leave the shared extensions
    # in place because md.0001_init relies on them too.
    op.execute("ALTER ROLE app_rw RESET search_path")
    op.execute("ALTER ROLE app_ro RESET search_path")
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA app
          REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM app_rw
        """
    )
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA app
          REVOKE SELECT ON TABLES FROM app_ro
        """
    )
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA app
          REVOKE USAGE, SELECT, UPDATE ON SEQUENCES FROM app_rw
        """
    )
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA app
          REVOKE USAGE, SELECT ON SEQUENCES FROM app_ro
        """
    )
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA app
          REVOKE EXECUTE ON FUNCTIONS FROM app_rw, app_ro
        """
    )
    op.execute("REVOKE USAGE ON SCHEMA app FROM app_rw, app_ro")
    op.execute("DROP SCHEMA IF EXISTS app CASCADE")
    op.execute("DROP ROLE IF EXISTS app_rw")
    op.execute("DROP ROLE IF EXISTS app_ro")
