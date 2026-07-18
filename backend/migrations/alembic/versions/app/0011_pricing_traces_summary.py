"""pricing_traces.summary (human-readable per-stage one-liner)

Adds a nullable ``summary`` column to ``app.pricing_traces`` so the
investigate screen (``GET /v1/traces/{request_id}``) can render each
stage as a plain-English sentence instead of forcing the reader to parse
raw JSON. The orchestrator generates the sentence inside the shared
trace-stage helpers from the data each stage already has — so every
product inherits summaries at once — and ``GET /v1/traces`` surfaces it
as a top-level field per stage.

A dedicated column (rather than a reserved payload key) is chosen
because:

* It survives the recorder's size-cap truncation: an oversized engine
  payload is replaced by a ``__truncated__`` marker, but the summary
  lives outside the JSON blob and is never lost.
* It reads back as a clean top-level field with no payload surgery in
  the endpoint.

Nullable on purpose (mirrors ``0010_pricing_traces_product``): existing
rows carry no summary and must not be rejected, and summary generation
is best-effort — a stage must never fail to record because its summary
could not be built.

Revision ID: 0011_pricing_traces_summary
Revises: 0010_pricing_traces_product
Create Date: 2026-06-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_pricing_traces_summary"
down_revision: str | None = "0010_pricing_traces_product"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pricing_traces",
        sa.Column("summary", sa.Text(), nullable=True),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("pricing_traces", "summary", schema="app")
