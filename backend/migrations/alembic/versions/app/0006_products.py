"""products: swaps_ir, swaps_inflation, swaptions, bonds_fixed, bonds_floating, cds, equity_options

Saved per-product configurations. Per plan principle #2 each row stores
the entire portal "request" payload (the wrapped-store ``request: R``)
in a JSONB ``request`` column; the body is large and evolving and we
deliberately don't pull product-specific fields up to top-level columns
yet — the orchestrator's per-product subplans will decide which fields
deserve indexed promotion once query shapes are known.

All seven tables share the same template:

* ``id`` UUID PK with ``public.gen_random_uuid()`` default.
* ``owner_uid`` → ``app.users(uid) ON DELETE RESTRICT``, indexed.
* ``name`` (user-supplied; the portal derives a default via
  ``deriveName(request)`` when none is provided).
* ``request`` JSONB with a cheap ``jsonb_typeof = 'object'`` CHECK.
* ``created_at`` / ``updated_at`` (trigger-maintained) / ``deleted_at``.
* Partial unique index on ``(owner_uid, name) WHERE deleted_at IS NULL``.

Cross-entity refs inside ``request`` (curve IDs, vol-surface IDs,
swaption-model IDs, credit-curve IDs, snapshot IDs) are soft
references — those entities may be deleted independently, and D9's
inline-or-reference shape means the same ``request`` blob may carry
inline definitions instead of stored IDs.

Revision ID: 0006_products
Revises: 0005_vol_surfaces_and_models
Create Date: 2026-05-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_products"
down_revision: str | None = "0005_vol_surfaces_and_models"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Ordered table list so create/drop loops cover the same set. Plural,
# category-prefixed per plan ("Product table names plural and prefixed
# by category — swaps_*, bonds_*").
_PRODUCT_TABLES: tuple[str, ...] = (
    "swaps_ir",
    "swaps_inflation",
    "swaptions",
    "bonds_fixed",
    "bonds_floating",
    "cds",
    "equity_options",
)


def _create_product_table(table: str) -> None:
    op.create_table(
        table,
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
        sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
        sa.CheckConstraint("length(trim(name)) > 0", name=f"{table}_name_nonempty"),
        sa.CheckConstraint("jsonb_typeof(request) = 'object'", name=f"{table}_request_object"),
        schema="app",
    )
    op.create_index(f"ix_{table}_owner_uid", table, ["owner_uid"], schema="app")
    op.create_index(
        f"uq_{table}_owner_name_active",
        table,
        ["owner_uid", "name"],
        unique=True,
        schema="app",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.execute(
        f"""
        CREATE TRIGGER {table}_set_updated_at
          BEFORE UPDATE ON app.{table}
          FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();
        """  # noqa: S608 -- table name is from a fixed allow-list above
    )


def upgrade() -> None:
    for table in _PRODUCT_TABLES:
        _create_product_table(table)


def downgrade() -> None:
    for table in reversed(_PRODUCT_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_set_updated_at ON app.{table}")
        op.drop_index(f"uq_{table}_owner_name_active", table_name=table, schema="app")
        op.drop_index(f"ix_{table}_owner_uid", table_name=table, schema="app")
        op.drop_table(table, schema="app")
