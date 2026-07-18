"""pricing_traces.product (product tag for the in-app trace)

Adds a nullable ``product`` column to ``app.pricing_traces`` so a future
recent-calls list (``GET /v1/traces?limit=N``) can show which product a
pricing call was for *without parsing every stage payload*. The column
is the orchestrator's product kind for the call (``swaps_ir`` /
``swaptions`` / ``bonds_fixed`` / ``bonds_floating`` / ``cds`` /
``equity_options`` / ``swaps_inflation``).

Nullable on purpose:

* Existing rows (the swap_ir pilot's traces) carry no product and must
  not be rejected.
* The recorder writes the tag best-effort; a trace must never fail
  because the product is unknown. Same diagnostic-artefact stance as the
  no-FK-to-app.users decision in ``0009_pricing_traces``.

No CHECK list on ``product`` (mirrors the no-CHECK-on-stage decision)
so a new product can start tagging traces without a migration.

Revision ID: 0010_pricing_traces_product
Revises: 0009_pricing_traces
Create Date: 2026-06-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_pricing_traces_product"
down_revision: str | None = "0009_pricing_traces"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pricing_traces",
        sa.Column("product", sa.Text(), nullable=True),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("pricing_traces", "product", schema="app")
