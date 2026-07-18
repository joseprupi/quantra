"""ingestion_log

Per-batch ingestion audit record (plan ``database/03_md_schema.md``).
Each row is one invocation of the MD ingester — typically one row
per nightly run per connector. Writes happen from the ingester via
``md_rw``; the MD service (``md_ro``) only reads to expose
"last successful ingestion" diagnostics.

Shape:

* ``vendor`` is the connector identifier the ingester uses today
  (``"fred"`` / ``"ecb"`` / ``"boe"`` / ``"treasury"``). ``source``
  is the more specific file/URL/dataset the connector pulled from —
  e.g. an FRED series id or a Treasury page type. The two are
  intentionally distinct so a single vendor running multiple feeds
  is still attributable per feed.

* ``status`` is constrained to a small set so the read-side can
  filter on it with confidence (``running`` / ``success`` /
  ``partial`` / ``failed``).

* ``errors`` is a JSONB array of error records. Free-form so the
  ingester can include a stack trace / vendor-side error code
  without a schema migration.

* No ``updated_at`` trigger — the ingester writes the row twice at
  most: once at start (``status='running'``, ``finished_at NULL``)
  and once at end with the final counts. Application code maintains
  ``finished_at`` directly.

Revision ID: 0005_ingestion_log
Revises: 0004_snapshots
Create Date: 2026-05-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_ingestion_log"
down_revision: str | None = "0004_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingestion_log",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("public.gen_random_uuid()"),
        ),
        sa.Column("vendor", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'running'"),
        ),
        sa.Column(
            "row_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "error_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "errors",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("length(trim(vendor)) > 0", name="ingestion_log_vendor_nonempty"),
        sa.CheckConstraint(
            "status IN ('running', 'success', 'partial', 'failed')",
            name="ingestion_log_status_valid",
        ),
        sa.CheckConstraint("row_count >= 0", name="ingestion_log_row_count_nonneg"),
        sa.CheckConstraint("error_count >= 0", name="ingestion_log_error_count_nonneg"),
        sa.CheckConstraint("jsonb_typeof(errors) = 'array'", name="ingestion_log_errors_array"),
        sa.CheckConstraint("jsonb_typeof(meta) = 'object'", name="ingestion_log_meta_object"),
        # `finished_at` is NULL while a run is in flight; once set, it
        # must not predate `started_at`.
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ingestion_log_finished_after_started",
        ),
        schema="md",
    )
    op.create_index("ix_ingestion_log_vendor", "ingestion_log", ["vendor"], schema="md")
    op.create_index(
        "ix_ingestion_log_started_at_desc",
        "ingestion_log",
        [sa.text("started_at DESC")],
        schema="md",
    )
    op.create_index(
        "ix_ingestion_log_status_started_at",
        "ingestion_log",
        ["status", sa.text("started_at DESC")],
        schema="md",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ingestion_log_status_started_at",
        table_name="ingestion_log",
        schema="md",
    )
    op.drop_index(
        "ix_ingestion_log_started_at_desc",
        table_name="ingestion_log",
        schema="md",
    )
    op.drop_index("ix_ingestion_log_vendor", table_name="ingestion_log", schema="md")
    op.drop_table("ingestion_log", schema="md")
