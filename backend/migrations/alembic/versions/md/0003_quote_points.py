"""quote_points

Time-series quote storage (plan ``database/03_md_schema.md``). The
single largest table in ``md.*`` by row count.

Primary key ``(canonical_id, as_of)`` collapses what the legacy
``quotes_timeseries`` table keyed as ``(canonical_id, as_of, source)``.
The plan picks one authoritative row per ``(canonical_id, as_of)``;
late vendor corrections **upsert** the row with a fresh
``ingested_at``, overwriting ``value`` / ``source`` / ``vendor_id`` /
``quality_flags`` / ``meta``. This matches what the existing MD
worker's ``_upsert_quote`` already does
(``ON CONFLICT (canonical_id, as_of, source) DO UPDATE``) — we just
drop ``source`` from the conflict key so the new schema has a
single canonical value per timestamp regardless of which feed wrote
it. Picking upsert over a ``quote_corrections`` companion table is
the simpler choice and matches actual vendor behavior observed in
the connectors (one row replaces another, no history is kept).

Volume estimate (plan's "Key decisions"): the existing MD service
maintains O(200) canonical IDs across FRED / ECB / BoE / Treasury at
**daily** frequency. That is roughly 200 IDs × 260 trading days
≈ 50k rows/year — three orders of magnitude below the plan's ~100M
partitioning threshold. Plain table, no partitioning. Revisit when
intraday tick ingestion lands.

Indexing:

* Primary key ``(canonical_id, as_of)`` already covers
  ``WHERE canonical_id = ? ORDER BY as_of DESC`` lookups.
* Explicit ``(canonical_id, as_of DESC)`` btree index added so
  "latest value at-or-before" hits a sorted index without a sort.
* BRIN on ``as_of`` for cheap full-range scans. BRIN is preferred
  over a second btree here because ``quote_points`` is append-mostly
  in time order — BRIN's per-block-summary indexing is ~100x smaller
  than the equivalent btree and equally fast on range scans against
  naturally-ordered data. If we ever start backfilling out-of-order
  history at scale, REINDEX the BRIN.

Revision ID: 0003_quote_points
Revises: 0002_catalog
Create Date: 2026-05-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_quote_points"
down_revision: str | None = "0002_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "quote_points",
        sa.Column(
            "canonical_id",
            sa.Text(),
            sa.ForeignKey("md.canonical_ids.canonical_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("as_of", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        # `source` is the connector identifier (e.g. "fred", "ecb",
        # "boe", "treasury"). `vendor_id` is whatever string that vendor
        # uses internally to name the series. Both are de-normalized
        # from `md.vendor_mappings` for read-path simplicity — the
        # existing API surface returns them unjoined.
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("vendor_id", sa.Text(), nullable=True),
        # Raw upstream value before unit normalization (e.g. percent
        # before convert-to-decimal). Optional — many feeds publish
        # already-normalized values.
        sa.Column("raw_value", sa.Float(), nullable=True),
        sa.Column(
            "units",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'decimal_rate'"),
        ),
        sa.Column(
            "quality_flags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # `ingested_at` advances on every upsert (see
        # `quote_points_touch_ingested_at` trigger below). Cache layers
        # can use it as an "is this row stale?" signal alongside the
        # snapshot version_etag.
        sa.Column(
            "ingested_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("canonical_id", "as_of", name="quote_points_pkey"),
        sa.CheckConstraint("length(trim(source)) > 0", name="quote_points_source_nonempty"),
        sa.CheckConstraint(
            "jsonb_typeof(quality_flags) = 'object'",
            name="quote_points_quality_flags_object",
        ),
        sa.CheckConstraint("jsonb_typeof(meta) = 'object'", name="quote_points_meta_object"),
        schema="md",
    )
    # "Latest value at-or-before X" lookup — the single hottest query
    # path on the MD service today. The PK already orders by
    # (canonical_id, as_of) ASC; this index gives the planner an
    # explicit DESC sort for `ORDER BY as_of DESC LIMIT 1` so it
    # never has to sort.
    op.create_index(
        "ix_quote_points_canonical_asof_desc",
        "quote_points",
        ["canonical_id", sa.text("as_of DESC")],
        schema="md",
    )
    # BRIN for snapshot-scale full-range scans (e.g. "show me every
    # quote in this month"). Cheap to maintain on append-mostly data.
    op.execute(
        """
        CREATE INDEX ix_quote_points_asof_brin
          ON md.quote_points USING BRIN (as_of)
        """
    )

    # `ingested_at` is set on every write — both first insert and any
    # late correction — so cache invalidation logic can rely on
    # "max(ingested_at) for canonical_id" without re-reading meta.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION md.touch_ingested_at() RETURNS trigger AS $$
        BEGIN
          NEW.ingested_at = now();
          RETURN NEW;
        END
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER quote_points_touch_ingested_at
          BEFORE INSERT OR UPDATE ON md.quote_points
          FOR EACH ROW EXECUTE FUNCTION md.touch_ingested_at();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS quote_points_touch_ingested_at ON md.quote_points")
    op.execute("DROP FUNCTION IF EXISTS md.touch_ingested_at()")
    op.execute("DROP INDEX IF EXISTS md.ix_quote_points_asof_brin")
    op.drop_index(
        "ix_quote_points_canonical_asof_desc",
        table_name="quote_points",
        schema="md",
    )
    op.drop_table("quote_points", schema="md")
