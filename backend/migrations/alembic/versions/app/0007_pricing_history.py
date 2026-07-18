"""pricing_history (stub)

Records every pricing call so users (and admins) can see the audit
trail. This is deliberately a stub — the full design (retention,
sampling, indexing on response fields, etc.) is future work. We
provision only the minimum needed for "we have a place to write rows."

Differences from the named-entity template:

* **Immutable.** No ``updated_at``, no trigger, no soft delete; pricing
  history rows shouldn't ever change in place.
* **No human name, no soft delete.** Rows are addressed by ``id`` (UUID).
  Partial-unique-on-name doesn't apply.
* **``product_id`` is nullable** to support the inline-or-reference
  flow — an inline-only pricing call has no stored product to point at.
* **No FKs to product tables.** ``product_kind`` + ``product_id`` is a
  soft reference: the product row may be (soft-)deleted later while
  the history row survives.

Revision ID: 0007_pricing_history
Revises: 0006_products
Create Date: 2026-05-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_pricing_history"
down_revision: str | None = "0006_products"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pricing_history",
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
        # `product_kind` matches the product table suffix (e.g. "swaps_ir",
        # "swaps_inflation", "bonds_fixed"). Free-form text + CHECK list so
        # adding a new product is one code change.
        sa.Column("product_kind", sa.Text(), nullable=False),
        # NULL means the call was inline-only. When set, this is the
        # `id` of the matching row in the product table named by
        # `product_kind` — soft reference, no FK.
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("as_of", sa.Date(), nullable=True),
        sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            (
                "product_kind IN ("
                "'swaps_ir', 'swaps_inflation', 'swaptions', "
                "'bonds_fixed', 'bonds_floating', 'cds', 'equity_options'"
                ")"
            ),
            name="pricing_history_product_kind_valid",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(request) = 'object'",
            name="pricing_history_request_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(response) = 'object'",
            name="pricing_history_response_object",
        ),
        schema="app",
    )
    op.create_index("ix_pricing_history_owner_uid", "pricing_history", ["owner_uid"], schema="app")
    # The two access patterns we already know we want: "list a user's
    # history newest-first" and "find every call against a saved product".
    op.create_index(
        "ix_pricing_history_owner_created_at",
        "pricing_history",
        ["owner_uid", sa.text("created_at DESC")],
        schema="app",
    )
    op.create_index(
        "ix_pricing_history_product",
        "pricing_history",
        ["product_kind", "product_id"],
        schema="app",
        postgresql_where=sa.text("product_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_pricing_history_product", table_name="pricing_history", schema="app")
    op.drop_index(
        "ix_pricing_history_owner_created_at",
        table_name="pricing_history",
        schema="app",
    )
    op.drop_index("ix_pricing_history_owner_uid", table_name="pricing_history", schema="app")
    op.drop_table("pricing_history", schema="app")
