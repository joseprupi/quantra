"""quotes_timeseries compatibility view

MD-drift fix. The ``0003_quote_points`` migration renamed the legacy
``quotes_timeseries`` time-series table to ``md.quote_points`` (collapsing
the ``(canonical_id, as_of, source)`` key to ``(canonical_id, as_of)``),
but the read-only market-data service was never cut over: every SELECT in
``routes/quotes.py`` / ``routes/sources.py`` / ``routes/treasury.py`` still
targets the unqualified name ``quotes_timeseries`` (resolved via the
``md`` pinned ``search_path``). Against a freshly-migrated ``md``
schema that relation does not exist, so the resolver's
``POST /quotes/resolved`` path raises
``relation "quotes_timeseries" does not exist``. The orchestrator surfaces
that as ``md_upstream_error`` (HTTP 500) and MD-backed (quote_id) pricing
is dead in the self-hosted bundle, contradicting the design intent
("always price via backend + MD server").

Fix: re-expose the legacy name as a **read-only VIEW** over the
authoritative ``md.quote_points`` table. The service only ever *reads*
``quotes_timeseries`` (all writes go to ``md.quote_points`` via the
ingester's ``md_rw`` upsert), so a view is the minimal, idiomatic
compatibility shim — no data is duplicated and ``quote_points`` stays the
single source of truth. The projected columns are exactly the union the
service consumers select (``canonical_id, as_of, value, source,
vendor_id, quality_flags, meta`` plus ``raw_value, units, ingested_at``
for completeness), matching the legacy table shape asserted by the MD
service's DB-integration fixture.

Grants: ``ALTER DEFAULT PRIVILEGES... ON TABLES`` (0001_init) also covers
views, but we grant explicitly here so the read path does not depend on
that subtlety and so the intent is auditable. ``md_ro`` (the MD service
role) gets SELECT; ``md_rw`` (the ingester) gets SELECT too for symmetry
with the base table. Role isolation is unchanged — the view is
SELECT-only and confers no write path.

Idempotent: ``CREATE OR REPLACE VIEW`` + ``GRANT`` are safe to re-run on
the fresh-volume bundle bring-up.

Revision ID: 0007_quotes_timeseries_view
Revises: 0006_role_connection_limits
Create Date: 2026-07-06
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_quotes_timeseries_view"
down_revision: str | None = "0006_role_connection_limits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS md.quotes_timeseries")
