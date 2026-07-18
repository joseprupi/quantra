"""drop quotes_saved

This migration consolidates the two per-user quote surfaces onto the single
``app.quote_book`` (dated-series) singleton. ``quotes_saved`` — the
legacy ``quantra_quotes`` localStorage list of *static* single-value
quotes exposed as ``/v1/quotes`` — is a strict subset of what the quote
book expresses (a one-entry series), and the pricing path never read
it (resolution = inline → app.snapshot pin → MD service). The entity's
CRUD spec / router are removed in the same change; this migration drops
the backing table.

The table holds dev-only data (no production users), so a plain DROP is
acceptable. ``downgrade`` recreates the table exactly as
``0004_snapshots_and_quote_book`` built it (grants flow from the
``ALTER DEFAULT PRIVILEGES`` set up in app 0001, so no grant surgery is
needed in either direction).

``app.quote_book`` and ``app.snapshots`` are untouched.

Revision ID: 0012_drop_quotes_saved
Revises: 0011_pricing_traces_summary
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_drop_quotes_saved"
down_revision: str | None = "0011_pricing_traces_summary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS quotes_saved_set_updated_at ON app.quotes_saved")
    op.drop_index("ix_quotes_saved_owner_uid", table_name="quotes_saved", schema="app")
    op.drop_table("quotes_saved", schema="app")


def downgrade() -> None:
    # Mirror of the 0004 create — see that revision for the column notes.
    op.create_table(
        "quotes_saved",
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
        sa.Column("quote_id", sa.Text(), nullable=False),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
        sa.UniqueConstraint("owner_uid", "quote_id", name="uq_quotes_saved_owner_quote"),
        sa.CheckConstraint("length(trim(quote_id)) > 0", name="quotes_saved_quote_id_nonempty"),
        sa.CheckConstraint("jsonb_typeof(body) = 'object'", name="quotes_saved_body_object"),
        schema="app",
    )
    op.create_index("ix_quotes_saved_owner_uid", "quotes_saved", ["owner_uid"], schema="app")
    op.execute(
        """
        CREATE TRIGGER quotes_saved_set_updated_at
          BEFORE UPDATE ON app.quotes_saved
          FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();
        """
    )
