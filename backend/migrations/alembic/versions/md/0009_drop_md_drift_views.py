"""drop MD-drift compatibility views

The ``0007_quotes_timeseries_view`` and ``0008_canonical_instruments_view``
migrations added read-only VIEWs (``md.quotes_timeseries`` over
``md.quote_points``; ``md.canonical_instruments`` over ``md.canonical_ids``)
as a stopgap so the read-only market-data service — which still SELECTed the
legacy relation names renamed by ``0002_catalog`` / ``0003_quote_points`` —
kept working against a freshly-migrated ``md`` schema.

The service has now been cut over: every SELECT in ``routes/quotes.py`` /
``routes/sources.py`` / ``routes/treasury.py`` targets the CANONICAL names
directly (``md.quote_points`` / ``md.canonical_ids``), resolved via the ``md``
pinned ``search_path``. The columns projected by the views were an exact
1:1 pass-through of the base tables (no aliasing), so the swap is a pure name
change. With the service no longer depending on the legacy names, the
compatibility shims are dead weight — drop them so ``quote_points`` /
``canonical_ids`` are the single, unambiguous source of truth.

``DROP VIEW IF EXISTS`` keeps this idempotent / re-runnable on fresh-volume
bundle bring-up. The downgrade re-creates the views (identical to 0007/0008)
so this migration is reversible.

Revision ID: 0009_drop_md_drift_views
Revises: 0008_canonical_instruments_view
Create Date: 2026-07-07
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009_drop_md_drift_views"
down_revision: str | None = "0008_canonical_instruments_view"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS md.quotes_timeseries")
    op.execute("DROP VIEW IF EXISTS md.canonical_instruments")


def downgrade() -> None:
    # Re-expose the legacy names as read-only views over the canonical
    # tables (mirrors 0007 / 0008) so the migration is reversible.
    op.execute(
        """
        CREATE OR REPLACE VIEW md.quotes_timeseries AS
          SELECT canonical_id,
                 as_of,
                 value,
                 source,
                 vendor_id,
                 raw_value,
                 units,
                 quality_flags,
                 meta,
                 ingested_at
          FROM md.quote_points
        """
    )
    op.execute("GRANT SELECT ON md.quotes_timeseries TO md_ro, md_rw")
    op.execute(
        """
        CREATE OR REPLACE VIEW md.canonical_instruments AS
          SELECT canonical_id,
                 asset_class,
                 family,
                 instrument,
                 currency,
                 tenor,
                 field,
                 frequency,
                 units,
                 description
          FROM md.canonical_ids
        """
    )
    op.execute("GRANT SELECT ON md.canonical_instruments TO md_ro, md_rw")
