"""pricing_traces (in-app pricing trace / investigation)

the orchestrator records *its own view* of every pipeline stage
of a pricing call, keyed by ``request_id`` (the same id the
``RequestIdMiddleware`` threads end-to-end). A user can later
paste a transaction id and see a timeline: inputs, entities loaded, what
market data was pulled + resolved, the exact JSON sent to the engine,
the engine's real response or error, and the history-write outcome.

Design notes (mirrors the tracing design):

* **Orchestrator is the SOLE writer** (north-star invariant #3). The
  engine and MD service write nothing here — they only read/log the
  ``x-request-id``. The orchestrator records what it asked + got, from
  its own vantage point.
* **Append-only, best-effort.** Rows are written fire-and-forget off the
  hot path; a write failure must never fail or slow a price. We do NOT
  add an FK to ``app.users`` (unlike ``pricing_history``): a trace is a
  diagnostic artefact and must never be rejected because the principal
  row is missing (e.g. the dev-bypass ``dev-user``).
* **Owner-scoped reads** (invariant #5). Every row carries ``owner_uid``;
  the read endpoint filters by the requesting principal and 404s when
  the caller owns no rows for that id.
* **bigserial id** so the natural insert order is also a stable
  tiebreaker when two stages share a ``ts``.

Revision ID: 0009_pricing_traces
Revises: 0008_role_connection_limits
Create Date: 2026-06-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_pricing_traces"
down_revision: str | None = "0008_role_connection_limits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pricing_traces",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        # The end-to-end transaction id. NOT unique — one call
        # emits many stage rows that share the same request_id.
        sa.Column("request_id", sa.Text(), nullable=False),
        # No FK to app.users on purpose (see module docstring): a
        # best-effort diagnostic write must never be rejected for a
        # missing principal row.
        sa.Column("owner_uid", sa.Text(), nullable=False),
        sa.Column("ts", sa.TIMESTAMP(timezone=True), nullable=False),
        # The orchestrator's view of each hop — 'orchestrator' today.
        sa.Column("service", sa.Text(), nullable=False),
        # input|load_entities|md_resolve|engine_request|engine_response|
        # history_write|error
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(
            "level IN ('info', 'warn', 'error')",
            name="pricing_traces_level_valid",
        ),
        schema="app",
    )
    # "fetch every stage for a transaction id" (the read endpoint).
    op.create_index(
        "ix_pricing_traces_request_id",
        "pricing_traces",
        ["request_id"],
        schema="app",
    )
    # "list a user's recent traces newest-first" (the optional later
    # list endpoint + retention prune scans).
    op.create_index(
        "ix_pricing_traces_owner_ts",
        "pricing_traces",
        ["owner_uid", "ts"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pricing_traces_owner_ts", table_name="pricing_traces", schema="app"
    )
    op.drop_index(
        "ix_pricing_traces_request_id", table_name="pricing_traces", schema="app"
    )
    op.drop_table("pricing_traces", schema="app")
