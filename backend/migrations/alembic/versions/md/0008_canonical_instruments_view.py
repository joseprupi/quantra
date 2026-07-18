"""canonical_instruments compatibility view

MD-drift fix (same class as ``0007_quotes_timeseries_view``). The
``0002_catalog`` migration renamed the legacy ``canonical_instruments``
registry to ``md.canonical_ids`` (collapsing the legacy split between
``canonical_instruments`` and ``series_metadata`` into one table), but the
read-only market-data service's catalog surface was never cut over:
``routes/sources.py`` still targets the unqualified name
``canonical_instruments`` (resolved via the ``md`` pinned ``search_path``) in both ``list_series`` and — via ``routes/public.py`` delegation —
the ``/catalog/series`` alias. Against a freshly-migrated ``md`` schema
that relation does not exist, so ``GET /series`` and ``GET /catalog/series``
raise ``relation "canonical_instruments" does not exist``. This breaks the
portal's read-only Quote Book page in the self-hosted bundle.

Fix: re-expose the legacy name as a **read-only VIEW** over the
authoritative ``md.canonical_ids`` table. The service only ever *reads*
``canonical_instruments`` (all writes go to ``md.canonical_ids`` via the
ingester's ``md_rw`` upsert), so a view is the minimal, idiomatic
compatibility shim — no data is duplicated and ``canonical_ids`` stays the
single source of truth. The legacy and canonical column names are
identical (``canonical_id, asset_class, family, instrument, currency,
tenor, field, frequency, units, description``), so no aliasing is needed;
the view projects exactly the legacy ``canonical_instruments`` shape
asserted by the MD service's DB-integration fixture.

Grants: ``ALTER DEFAULT PRIVILEGES... ON TABLES`` (0001_init) also covers
views, but we grant explicitly here so the read path does not depend on
that subtlety and so the intent is auditable. ``md_ro`` (the MD service
role) gets SELECT; ``md_rw`` (the ingester) gets SELECT too for symmetry
with the base table. Role isolation is unchanged — the view is
SELECT-only and confers no write path.

Idempotent: ``CREATE OR REPLACE VIEW`` + ``GRANT`` are safe to re-run on
the fresh-volume bundle bring-up.

Revision ID: 0008_canonical_instruments_view
Revises: 0007_quotes_timeseries_view
Create Date: 2026-07-06
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_canonical_instruments_view"
down_revision: str | None = "0007_quotes_timeseries_view"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS md.canonical_instruments")
