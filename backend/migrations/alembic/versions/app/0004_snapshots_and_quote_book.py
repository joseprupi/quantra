"""snapshots, quote_book, quotes_saved

User-owned market data (says the orchestrator resolves vendor values,
but *user* quote books and saved snapshots are still user data — they
live in ``app.*``, not ``md.*``).

* ``snapshots`` — a named, dated bundle of quote references. Each row is
  one snapshot the user composed.
* ``quote_book`` — the portal stores a **single** quote book per user
  (one localStorage key); this table mirrors that with a ``UNIQUE
  (owner_uid)`` constraint and an ``entries`` JSONB array. Rows aren't
  human-named so the standard partial-unique-on-name pattern doesn't
  apply.
* ``quotes_saved`` — ad-hoc per-quote saved values (the legacy
  ``quantra_quotes`` localStorage list). Keyed by
  ``(owner_uid, quote_id)``; ``quote_id`` is the user-facing string ID
  the rest of the system uses to refer to a vendor quote, not a UUID.

All cross-references (quote IDs in ``snapshots.content``, curve IDs in
``quote_book.entries[*]``) are soft references — the linked entity
(an md.* quote, an app.curves row) may legitimately go away while the
snapshot/quote book persists.

Revision ID: 0004_snapshots_and_quote_book
Revises: 0003_curves_and_indices
Create Date: 2026-05-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_snapshots_and_quote_book"
down_revision: str | None = "0003_curves_and_indices"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "snapshots",
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
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        # `content` is the bundle of quote references — typically a
        # {quote_id -> value or {quote_id, value}} shape; kept as JSONB
        # because the orchestrator's snapshot import paths will likely
        # iterate on it before this schema is final.
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
        sa.CheckConstraint("length(trim(name)) > 0", name="snapshots_name_nonempty"),
        sa.CheckConstraint("jsonb_typeof(content) = 'object'", name="snapshots_content_object"),
        schema="app",
    )
    op.create_index("ix_snapshots_owner_uid", "snapshots", ["owner_uid"], schema="app")
    op.create_index(
        "ix_snapshots_owner_as_of",
        "snapshots",
        ["owner_uid", "as_of"],
        schema="app",
    )
    op.create_index(
        "uq_snapshots_owner_name_active",
        "snapshots",
        ["owner_uid", "name"],
        unique=True,
        schema="app",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.execute(
        """
        CREATE TRIGGER snapshots_set_updated_at
          BEFORE UPDATE ON app.snapshots
          FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();
        """
    )

    # `quote_book` — one row per user. UNIQUE on owner_uid enforces that;
    # the row isn't human-named so the standard partial-unique on
    # (owner_uid, name) doesn't apply. `entries` is the JSONB array of
    # {id, kind, series: [{date, value}, …], resolution_mode?, …}.
    op.create_table(
        "quote_book",
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
            unique=True,
        ),
        sa.Column(
            "resolution_mode",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'previous'"),
        ),
        sa.Column("entries", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
            "resolution_mode IN ('previous', 'exact')",
            name="quote_book_resolution_mode_valid",
        ),
        sa.CheckConstraint("jsonb_typeof(entries) = 'array'", name="quote_book_entries_array"),
        schema="app",
    )
    op.execute(
        """
        CREATE TRIGGER quote_book_set_updated_at
          BEFORE UPDATE ON app.quote_book
          FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();
        """
    )

    # `quotes_saved` — the legacy `quantra_quotes` localStorage list.
    # Each row is one saved QuoteSpec for an owner. `quote_id` is the
    # user-visible quote ID string (e.g. "EURIBOR_3M_2026-01-15"), not a
    # UUID, so it stays TEXT. UNIQUE (owner_uid, quote_id) is the
    # business key per plan; we still mint a row UUID for stable
    # identity in API responses.
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


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS quotes_saved_set_updated_at ON app.quotes_saved")
    op.drop_index("ix_quotes_saved_owner_uid", table_name="quotes_saved", schema="app")
    op.drop_table("quotes_saved", schema="app")

    op.execute("DROP TRIGGER IF EXISTS quote_book_set_updated_at ON app.quote_book")
    op.drop_table("quote_book", schema="app")

    op.execute("DROP TRIGGER IF EXISTS snapshots_set_updated_at ON app.snapshots")
    op.drop_index("uq_snapshots_owner_name_active", table_name="snapshots", schema="app")
    op.drop_index("ix_snapshots_owner_as_of", table_name="snapshots", schema="app")
    op.drop_index("ix_snapshots_owner_uid", table_name="snapshots", schema="app")
    op.drop_table("snapshots", schema="app")
