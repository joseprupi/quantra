"""indices, curves, curve_sets, credit_curves

Reference-data tables — the shared building blocks pricing requests
compose into. All four are human-named, so they get the soft-delete +
partial-unique-on-(owner_uid, name) treatment from the schema design.

Cross-entity references (e.g. ``curve_sets`` listing member curves,
``credit_curves`` pointing at quote_book IDs) live inside the JSONB
``body`` column as soft references rather than FKs. The portal already
allows a curve to be deleted while a curve_set that references it is
preserved (the missing curve simply becomes ``null`` on resolve), and
the inline-or-reference payload rule means the same fields can
hold either a stored ID or an inline definition with no DB row to
point at. Hard FKs would break both behaviors.

Top-level columns are limited to the fields the API will routinely
filter or sort on (``owner_uid``, ``name``, ``currency``, ``kind``,
``reference_date``…); everything else stays in ``body`` to avoid
schema churn as products evolve.

Revision ID: 0003_curves_and_indices
Revises: 0002_users_and_api_keys
Create Date: 2026-05-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_curves_and_indices"
down_revision: str | None = "0002_users_and_api_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Tables this migration owns, in the order they should be created (parents
# first so a stray FK in a future revision doesn't have to reorder). All four
# share the same template so we drive them from a list rather than inlining
# eight ``create_table`` calls.
_NAMED_ENTITIES: tuple[str, ...] = ("indices", "curves", "curve_sets", "credit_curves")


def _ts_columns() -> list[sa.Column[sa.types.TypeEngine[object]]]:
    """Standard ``created_at`` / ``updated_at`` / ``deleted_at`` columns.

    Same shape on every named entity table so the trigger + partial unique
    index installed below applies uniformly.
    """

    return [
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
    ]


def _named_entity_indexes(table: str) -> None:
    """Apply the standard owner/uniqueness/trigger plumbing for a named entity."""

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
    op.create_table(
        "indices",
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
        # IBOR / Overnight / Inflation — kept as a free-form string with a
        # CHECK constraint so adding a new family in code doesn't need a
        # migration. Mirrors `StoredIndexType` in lib/storage/indices.ts.
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=True),
        sa.Column("calendar", sa.Text(), nullable=True),
        sa.Column("day_counter", sa.Text(), nullable=True),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        *_ts_columns(),
        sa.CheckConstraint("length(trim(name)) > 0", name="indices_name_nonempty"),
        sa.CheckConstraint(
            "kind IN ('IBOR', 'Overnight', 'Inflation')",
            name="indices_kind_valid",
        ),
        sa.CheckConstraint("jsonb_typeof(body) = 'object'", name="indices_body_object"),
        schema="app",
    )
    _named_entity_indexes("indices")

    op.create_table(
        "curves",
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
        sa.Column("currency", sa.Text(), nullable=True),
        sa.Column("day_counter", sa.Text(), nullable=True),
        # `helper_kind` is the curve-point discriminator;
        # the portal's `Curve.role` (discount / forward / inflation / etc.)
        # is the same concept. Stored free-form so per-engine helper kinds
        # can be added without a migration.
        sa.Column("helper_kind", sa.Text(), nullable=True),
        sa.Column("reference_date", sa.Date(), nullable=True),
        # Points are an array of {tenor, rate, quote_id?} objects. Stored
        # separately from `body` so per-point queries (e.g. count, validate)
        # don't have to navigate through the rest of the curve definition.
        sa.Column("points", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        # Catch-all for the rest of the curve spec (interpolator,
        # bootstrap_trait, inflation_curve config, role hints, …). Stays
        # JSONB to absorb product evolution without DDL churn.
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        *_ts_columns(),
        sa.CheckConstraint("length(trim(name)) > 0", name="curves_name_nonempty"),
        sa.CheckConstraint("jsonb_typeof(points) = 'array'", name="curves_points_array"),
        sa.CheckConstraint("jsonb_typeof(body) = 'object'", name="curves_body_object"),
        schema="app",
    )
    _named_entity_indexes("curves")

    op.create_table(
        "curve_sets",
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
        sa.Column("currency", sa.Text(), nullable=True),
        # `body` holds:
        #   - curve_refs: [{id, curve_id, role, label?}, …]
        #   - credit_curve_ids: [uuid, …]
        #   - quote_ids: [string, …]
        # The portal's `curveSets.ts` already stores them all as one
        # contiguous JSON shape, and the curve/credit-curve targets are
        # soft references (a curve can be deleted while a curve_set
        # survives with that ref unresolved).
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        *_ts_columns(),
        sa.CheckConstraint("length(trim(name)) > 0", name="curve_sets_name_nonempty"),
        sa.CheckConstraint("jsonb_typeof(body) = 'object'", name="curve_sets_body_object"),
        schema="app",
    )
    _named_entity_indexes("curve_sets")

    op.create_table(
        "credit_curves",
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
        # The portal allows credit curves without an explicit name (it
        # falls back to `reference_entity`); we require a non-empty name
        # here so the soft-delete partial-unique still has a key to lean on.
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("reference_entity", sa.Text(), nullable=True),
        sa.Column("currency", sa.Text(), nullable=True),
        sa.Column("seniority", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("recovery_rate", sa.Numeric(6, 4), nullable=False),
        # `body` holds flat_hazard_rate, points (with quote_id soft refs
        # into app.quote_book), etc.
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        *_ts_columns(),
        sa.CheckConstraint("length(trim(name)) > 0", name="credit_curves_name_nonempty"),
        sa.CheckConstraint(
            "source IN ('flat', 'manual', 'quote_book')",
            name="credit_curves_source_valid",
        ),
        sa.CheckConstraint(
            "recovery_rate >= 0 AND recovery_rate <= 1",
            name="credit_curves_recovery_rate_unit",
        ),
        sa.CheckConstraint("jsonb_typeof(body) = 'object'", name="credit_curves_body_object"),
        schema="app",
    )
    _named_entity_indexes("credit_curves")


def downgrade() -> None:
    for table in reversed(_NAMED_ENTITIES):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_set_updated_at ON app.{table}")
        op.drop_index(f"uq_{table}_owner_name_active", table_name=table, schema="app")
        op.drop_index(f"ix_{table}_owner_uid", table_name=table, schema="app")
        op.drop_table(table, schema="app")
