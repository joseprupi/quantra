"""pricing_history ↔ entity_versions link

When a pricing call references a SAVED product row (``swap_id`` /
``bond_id`` / …), the pricing-history row records exactly which
VERSION of that entity was priced, so an auditor can replay "what did
this trade look like when this number was produced".

Three nullable columns (inline pricing calls leave them NULL):

* ``trade_entity_type`` — the entity-versions type key. Today this is
  always equal to ``product_kind`` (the product-table name); kept as a
  separate column so the link stays explicit and self-describing.
* ``trade_entity_id`` — the priced entity's id (same value as
  ``product_id`` today).
* ``trade_version`` — the entity's head ``version_no`` in
  ``app.entity_versions`` at pricing time.

Revision ID: 0014_pricing_hist_trade_version
Revises: 0013_entity_versions
Create Date: 2026-07-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_pricing_hist_trade_version"
down_revision: str | None = "0013_entity_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pricing_history",
        sa.Column("trade_entity_type", sa.Text(), nullable=True),
        schema="app",
    )
    op.add_column(
        "pricing_history",
        sa.Column("trade_entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema="app",
    )
    op.add_column(
        "pricing_history",
        sa.Column("trade_version", sa.Integer(), nullable=True),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("pricing_history", "trade_version", schema="app")
    op.drop_column("pricing_history", "trade_entity_id", schema="app")
    op.drop_column("pricing_history", "trade_entity_type", schema="app")
