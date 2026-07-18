"""app role CONNECTION LIMIT

Postgres-side cap on simultaneous connections per app.* role. The cap
is the safety net behind the per-process pool sizing in
``quantra_common.db.engine``: SQLAlchemy enforces the application-level
ceiling, and these limits enforce the cluster-level ceiling so a
mis-configured ``PG_POOL_SIZE_*`` env var (or a forgotten ``await
engine.dispose()``) cannot starve the other roles' slots.

The values match the documented "Connection pool math" in the repo
README — see that section for the inequality
``Σ(per-role limit) + admin_headroom ≤ max_connections``. Sized for one
orchestrator process (single dev / single prod replica). When you run
more processes per role, raise the per-role limit to ``processes ×
(pool_size + max_overflow)`` and bump Postgres' ``max_connections`` if
the new sum exceeds it.

Idempotent: ``ALTER ROLE … CONNECTION LIMIT`` is a no-op on re-apply.
The downgrade resets each role to ``-1`` (unlimited), the
``CREATE ROLE`` default.

Revision ID: 0008_role_connection_limits
Revises: 0007_pricing_history
Create Date: 2026-05-14
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_role_connection_limits"
down_revision: str | None = "0007_pricing_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Defaults match `Settings.pg_pool_size_<role>` + `pg_pool_max_overflow_<role>`
# in `packages/common/src/quantra_common/settings/base.py`. Update both sides
# together when re-tuning. Holding two sources of truth in sync is the cost
# of keeping the cap enforced on both sides — the alternative (reading env
# vars from a migration) couples DDL to runtime config.
_APP_RW_LIMIT = 30  # 20 (pool_size) + 10 (max_overflow), single process
_APP_RO_LIMIT = 15  # 10 + 5, single process


def upgrade() -> None:
    op.execute(f"ALTER ROLE app_rw CONNECTION LIMIT {_APP_RW_LIMIT}")
    op.execute(f"ALTER ROLE app_ro CONNECTION LIMIT {_APP_RO_LIMIT}")


def downgrade() -> None:
    op.execute("ALTER ROLE app_rw CONNECTION LIMIT -1")
    op.execute("ALTER ROLE app_ro CONNECTION LIMIT -1")
