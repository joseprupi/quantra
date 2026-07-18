"""init md schema, roles, and grants

Mirror of ``versions/app/0001_init.py`` for the market-data side:

* Schema ``md`` with PUBLIC stripped of all rights.
* Roles ``md_rw`` (DML, used by the ingester) and ``md_ro`` (SELECT only,
  used by the read-only MD service), each pinned to ``search_path = md``.
* Default privileges so future ``md.*`` tables inherit grants from the
  migration runner's role automatically.
* The same three shared extensions installed in ``public`` — repeated
  here so this migration is safe to run before (or without) the app one.

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

_DEV_PASSWORDS = {
    "md_rw": "md_rw",
    "md_ro": "md_ro",
}


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public")
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public")

    op.execute(
        f"""
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'md_rw') THEN
            CREATE ROLE md_rw LOGIN PASSWORD '{_DEV_PASSWORDS["md_rw"]}';
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'md_ro') THEN
            CREATE ROLE md_ro LOGIN PASSWORD '{_DEV_PASSWORDS["md_ro"]}';
          END IF;
        END
        $$;
        """  # noqa: S608
    )

    op.execute("CREATE SCHEMA IF NOT EXISTS md")
    op.execute("REVOKE ALL ON SCHEMA md FROM PUBLIC")

    op.execute("GRANT USAGE ON SCHEMA md TO md_rw, md_ro")

    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA md
          GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO md_rw
        """
    )
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA md
          GRANT SELECT ON TABLES TO md_ro
        """
    )
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA md
          GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO md_rw
        """
    )
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA md
          GRANT USAGE, SELECT ON SEQUENCES TO md_ro
        """
    )
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA md
          GRANT EXECUTE ON FUNCTIONS TO md_rw, md_ro
        """
    )

    op.execute("ALTER ROLE md_rw SET search_path TO md")
    op.execute("ALTER ROLE md_ro SET search_path TO md")


def downgrade() -> None:
    op.execute("ALTER ROLE md_rw RESET search_path")
    op.execute("ALTER ROLE md_ro RESET search_path")
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA md
          REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM md_rw
        """
    )
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA md
          REVOKE SELECT ON TABLES FROM md_ro
        """
    )
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA md
          REVOKE USAGE, SELECT, UPDATE ON SEQUENCES FROM md_rw
        """
    )
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA md
          REVOKE USAGE, SELECT ON SEQUENCES FROM md_ro
        """
    )
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA md
          REVOKE EXECUTE ON FUNCTIONS FROM md_rw, md_ro
        """
    )
    op.execute("REVOKE USAGE ON SCHEMA md FROM md_rw, md_ro")
    op.execute("DROP SCHEMA IF EXISTS md CASCADE")
    op.execute("DROP ROLE IF EXISTS md_rw")
    op.execute("DROP ROLE IF EXISTS md_ro")
