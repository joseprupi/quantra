"""md role CONNECTION LIMIT

Mirror of ``versions/app/0008_role_connection_limits.py`` for the
market-data side. Keeping the connection cap co-located with the role's
``0001_init.py`` makes "where does this role's lifecycle live?" a
single answer per schema.

The values match the documented "Connection pool math" in the repo
README — see that section for the inequality
``Σ(per-role limit) + admin_headroom ≤ max_connections``. The ingester
is batch-oriented (a single worker process is the steady state today)
and the MD service is sized for one read-replica process; both can be
raised together with their pool settings as we scale.

Revision ID: 0006_role_connection_limits
Revises: 0005_ingestion_log
Create Date: 2026-05-14
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_role_connection_limits"
down_revision: str | None = "0005_ingestion_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MD_RW_LIMIT = 8   # 4 (pool_size) + 4 (max_overflow), single ingester process
_MD_RO_LIMIT = 25  # 15 + 10, single MD service process


def upgrade() -> None:
    op.execute(f"ALTER ROLE md_rw CONNECTION LIMIT {_MD_RW_LIMIT}")
    op.execute(f"ALTER ROLE md_ro CONNECTION LIMIT {_MD_RO_LIMIT}")


def downgrade() -> None:
    op.execute("ALTER ROLE md_rw CONNECTION LIMIT -1")
    op.execute("ALTER ROLE md_ro CONNECTION LIMIT -1")
