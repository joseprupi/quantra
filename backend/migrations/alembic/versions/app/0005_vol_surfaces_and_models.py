"""vol_surfaces, swaption_models

* ``vol_surfaces`` — saved volatility surface specs. The portal stores
  Swaption (Lognormal/Normal/ShiftedLognormal vol cubes, SABR grids,
  SABR calibration market vols + cached diagnostics) and Equity
  (BlackVolSpec, SurfaceFromPrices) shapes through a single
  ``VolSurfaceSpec``. ``kind`` lifts ``payload_type`` (``SwaptionVolSpec``,
  ``OptionletVolSpec``, ``BlackVolSpec``) to a first-class column for
  filtering; everything else stays in ``payload``.
* ``swaption_models`` — calibrated short-rate model rows
  (``HullWhiteLattice`` today; the discriminator is ``kind``). Numeric
  calibration outputs (``hw_a``, ``hw_sigma``, ``rmse``) plus the
  refs that produced them (``vol_surface_id``, ``discount_curve_id``,
  …) all stay inside ``payload`` because the consuming code already
  unpacks the whole record.

Both tables use the named-entity template: ``owner_uid`` FK with
``RESTRICT``, soft delete with partial unique on
``(owner_uid, name)``, trigger-maintained ``updated_at``.

Revision ID: 0005_vol_surfaces_and_models
Revises: 0004_snapshots_and_quote_book
Create Date: 2026-05-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_vol_surfaces_and_models"
down_revision: str | None = "0004_snapshots_and_quote_book"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "vol_surfaces",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("public.gen_random_uuid()"),
        ),
        sa.Column(
            "owner_uid",
            sa.Text(),
            sa.ForeignKey("app.users.uid", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        # Mirrors `VolPayloadType` in lib/storage/volSurfaces.ts —
        # `SwaptionVolSpec`, `OptionletVolSpec`, `BlackVolSpec`. Kept as
        # free-form text + CHECK so adding a payload type later (e.g.
        # `CapFloorVolSpec`) is a code change, not a migration.
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("length(trim(name)) > 0", name="vol_surfaces_name_nonempty"),
        sa.CheckConstraint(
            "kind IN ('SwaptionVolSpec', 'OptionletVolSpec', 'BlackVolSpec')",
            name="vol_surfaces_kind_valid",
        ),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="vol_surfaces_payload_object"),
        schema="app",
    )
    op.create_index("ix_vol_surfaces_owner_uid", "vol_surfaces", ["owner_uid"], schema="app")
    op.create_index(
        "uq_vol_surfaces_owner_name_active",
        "vol_surfaces",
        ["owner_uid", "name"],
        unique=True,
        schema="app",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.execute(
        """
        CREATE TRIGGER vol_surfaces_set_updated_at
          BEFORE UPDATE ON app.vol_surfaces
          FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();
        """
    )

    op.create_table(
        "swaption_models",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("public.gen_random_uuid()"),
        ),
        sa.Column(
            "owner_uid",
            sa.Text(),
            sa.ForeignKey("app.users.uid", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        # `HullWhiteLattice` today; future short-rate models slot in via
        # this discriminator without a migration.
        sa.Column("kind", sa.Text(), nullable=False, server_default=sa.text("'HullWhiteLattice'")),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("length(trim(name)) > 0", name="swaption_models_name_nonempty"),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="swaption_models_payload_object",
        ),
        schema="app",
    )
    op.create_index("ix_swaption_models_owner_uid", "swaption_models", ["owner_uid"], schema="app")
    op.create_index(
        "uq_swaption_models_owner_name_active",
        "swaption_models",
        ["owner_uid", "name"],
        unique=True,
        schema="app",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.execute(
        """
        CREATE TRIGGER swaption_models_set_updated_at
          BEFORE UPDATE ON app.swaption_models
          FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS swaption_models_set_updated_at ON app.swaption_models")
    op.drop_index(
        "uq_swaption_models_owner_name_active",
        table_name="swaption_models",
        schema="app",
    )
    op.drop_index("ix_swaption_models_owner_uid", table_name="swaption_models", schema="app")
    op.drop_table("swaption_models", schema="app")

    op.execute("DROP TRIGGER IF EXISTS vol_surfaces_set_updated_at ON app.vol_surfaces")
    op.drop_index("uq_vol_surfaces_owner_name_active", table_name="vol_surfaces", schema="app")
    op.drop_index("ix_vol_surfaces_owner_uid", table_name="vol_surfaces", schema="app")
    op.drop_table("vol_surfaces", schema="app")
