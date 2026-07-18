"""md.set_updated_at, canonical_ids, vendor_mappings

First table-bearing migration on the ``md.*`` side (plan
``database/03_md_schema.md``). The market-data plane is global to the
deployment — no ``owner_uid`` anywhere — so the table shape is much
simpler than its ``app.*`` cousin.

Tables:

* ``md.canonical_ids`` — registry of canonical identifiers and the
  parsed structural descriptors that come along for the ride
  (``asset_class``, ``family``, ``instrument``, ``currency``, ``tenor``,
  ``field``) plus descriptive metadata (``frequency``, ``units``,
  ``description``). The legacy MD service splits these between
  ``canonical_instruments`` and ``series_metadata``; we collapse them
  into one table because every public API endpoint consumes them as a
  single row and the optional ``series_metadata`` table in the plan
  exists only to absorb extension fields we don't yet need.

* ``md.vendor_mappings`` — many-to-one vendor → canonical_id mapping.
  Modeled as a child table (not a column on ``canonical_ids``) because
  one canonical identifier can be sourced from multiple vendors with
  different priorities, and the existing MD service exposes
  ``GET /sources`` and ``GET /series?source=...`` that scan this
  table directly.

Shared plumbing:

* ``md.set_updated_at()`` — same shape as ``app.set_updated_at()`` from
  ``app/0002_users_and_api_keys.py``. Lives in ``md`` so the role's
  pinned ``search_path`` finds it without qualification.

The ``md_rw`` / ``md_ro`` grants come from ``ALTER DEFAULT PRIVILEGES``
in ``md/0001_init.py`` — anything created here by the migration runner
inherits the right grants automatically.

Revision ID: 0002_catalog
Revises: 0001_init
Create Date: 2026-05-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_catalog"
down_revision: str | None = "0001_init"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Shared trigger function reused by every md.* table that carries
    # ``updated_at``. Mirrors app.set_updated_at() in shape and intent
    # but lives in `md` so the md role's pinned search_path
    # resolves it without qualification.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION md.set_updated_at() RETURNS trigger AS $$
        BEGIN
          NEW.updated_at = now();
          RETURN NEW;
        END
        $$ LANGUAGE plpgsql;
        """
    )

    # `canonical_ids` — one row per canonical identifier. The legacy
    # `canonical_instruments` table in quantra-market-data is the same
    # shape; renamed here to match the plan's deliverable name.
    op.create_table(
        "canonical_ids",
        sa.Column("canonical_id", sa.Text(), primary_key=True),
        sa.Column("asset_class", sa.Text(), nullable=False),
        sa.Column("family", sa.Text(), nullable=True),
        sa.Column("instrument", sa.Text(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("tenor", sa.Text(), nullable=True),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column(
            "frequency",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'daily'"),
        ),
        sa.Column(
            "units",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'decimal_rate'"),
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "length(trim(canonical_id)) > 0",
            name="canonical_ids_canonical_id_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(asset_class)) > 0",
            name="canonical_ids_asset_class_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(instrument)) > 0",
            name="canonical_ids_instrument_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(currency)) > 0",
            name="canonical_ids_currency_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(field)) > 0",
            name="canonical_ids_field_nonempty",
        ),
        schema="md",
    )
    op.create_index(
        "ix_canonical_ids_asset_class",
        "canonical_ids",
        ["asset_class"],
        schema="md",
    )
    op.create_index(
        "ix_canonical_ids_currency",
        "canonical_ids",
        ["currency"],
        schema="md",
    )
    op.execute(
        """
        CREATE TRIGGER canonical_ids_set_updated_at
          BEFORE UPDATE ON md.canonical_ids
          FOR EACH ROW EXECUTE FUNCTION md.set_updated_at();
        """
    )

    # `vendor_mappings` — vendor (e.g. "fred", "ecb", "boe", "treasury")
    # plus that vendor's identifier mapping to a canonical_id. One
    # canonical_id may have many vendor mappings; `(vendor, vendor_id)`
    # is unique. `priority` and `active` exist so a fallback vendor can
    # be configured without deleting the primary mapping.
    op.create_table(
        "vendor_mappings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("public.gen_random_uuid()"),
        ),
        sa.Column("vendor", sa.Text(), nullable=False),
        sa.Column("vendor_id", sa.Text(), nullable=False),
        sa.Column("vendor_field", sa.Text(), nullable=True),
        sa.Column(
            "priority",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("100"),
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "canonical_id",
            sa.Text(),
            sa.ForeignKey("md.canonical_ids.canonical_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("length(trim(vendor)) > 0", name="vendor_mappings_vendor_nonempty"),
        sa.CheckConstraint(
            "length(trim(vendor_id)) > 0", name="vendor_mappings_vendor_id_nonempty"
        ),
        schema="md",
    )
    op.create_index(
        "uq_vendor_mappings_vendor_vendor_id",
        "vendor_mappings",
        ["vendor", "vendor_id"],
        unique=True,
        schema="md",
    )
    op.create_index(
        "ix_vendor_mappings_canonical_id",
        "vendor_mappings",
        ["canonical_id"],
        schema="md",
    )
    op.create_index(
        "ix_vendor_mappings_vendor_active",
        "vendor_mappings",
        ["vendor", "active"],
        schema="md",
        postgresql_where=sa.text("active"),
    )
    op.execute(
        """
        CREATE TRIGGER vendor_mappings_set_updated_at
          BEFORE UPDATE ON md.vendor_mappings
          FOR EACH ROW EXECUTE FUNCTION md.set_updated_at();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS vendor_mappings_set_updated_at ON md.vendor_mappings")
    op.drop_index(
        "ix_vendor_mappings_vendor_active",
        table_name="vendor_mappings",
        schema="md",
    )
    op.drop_index(
        "ix_vendor_mappings_canonical_id",
        table_name="vendor_mappings",
        schema="md",
    )
    op.drop_index(
        "uq_vendor_mappings_vendor_vendor_id",
        table_name="vendor_mappings",
        schema="md",
    )
    op.drop_table("vendor_mappings", schema="md")

    op.execute("DROP TRIGGER IF EXISTS canonical_ids_set_updated_at ON md.canonical_ids")
    op.drop_index("ix_canonical_ids_currency", table_name="canonical_ids", schema="md")
    op.drop_index("ix_canonical_ids_asset_class", table_name="canonical_ids", schema="md")
    op.drop_table("canonical_ids", schema="md")

    op.execute("DROP FUNCTION IF EXISTS md.set_updated_at()")
