"""fx_spots (FX spot market data) + fx_forwards (FX forward trades)

Engine 0.9.0 introduced FX market data (``pricing.fx.spots[]``) and the FX
forward product. Two new ``app.*`` tables:

* ``fx_spots`` — the orchestrator-side ``FxSpotSpec``: a currency pair as
  two ISO codes, an inline ``rate`` OR an MD ``quote_id`` (resolved
  server-side at pricing time; exactly one of the two), the spot lag
  (``spot_days``), the joint settlement calendars (JSONB array of calendar
  names) and the business-day convention. Named entity template (soft
  delete, partial unique on ``(owner_uid, name)``, ``updated_at`` trigger,
  enrolled in the ``entity_versions`` audit trail by the repository).
* ``fx_forwards`` — the eighth product table, same ``(name, request)``
  template as the other seven (``0006_products``).

``pricing_history.product_kind`` CHECK gains ``'fx_forwards'``.

Revision ID: 0015_fx_spots_and_fx_forwards
Revises: 0014_pricing_hist_trade_version
Create Date: 2026-10-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_fx_spots_and_fx_forwards"
down_revision: str | None = "0014_pricing_hist_trade_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PRODUCT_KINDS_OLD = (
    "'swaps_ir', 'swaps_inflation', 'swaptions', "
    "'bonds_fixed', 'bonds_floating', 'cds', 'equity_options'"
)
_PRODUCT_KINDS_NEW = _PRODUCT_KINDS_OLD + ", 'fx_forwards'"


def _ts_columns() -> list[sa.Column[sa.types.TypeEngine[object]]]:
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
        """  # noqa: S608 -- table name is from a fixed allow-list in this file
    )


def _drop_named_entity(table: str) -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {table}_set_updated_at ON app.{table}")
    op.drop_index(f"uq_{table}_owner_name_active", table_name=table, schema="app")
    op.drop_index(f"ix_{table}_owner_uid", table_name=table, schema="app")
    op.drop_table(table, schema="app")


def upgrade() -> None:
    op.create_table(
        "fx_spots",
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
        sa.Column("base_currency", sa.Text(), nullable=False),
        sa.Column("quote_currency", sa.Text(), nullable=False),
        # Exactly one of ``rate`` (inline) / ``quote_id`` (MD canonical id,
        # resolved at pricing time).
        sa.Column("rate", sa.Float(precision=53), nullable=True),
        sa.Column("quote_id", sa.Text(), nullable=True),
        sa.Column("spot_days", sa.Integer(), nullable=False),
        sa.Column("business_day_convention", sa.Text(), nullable=False),
        sa.Column(
            "settlement_calendars", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "body",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        *_ts_columns(),
        sa.CheckConstraint("length(trim(name)) > 0", name="fx_spots_name_nonempty"),
        sa.CheckConstraint(
            "base_currency <> quote_currency", name="fx_spots_pair_distinct"
        ),
        sa.CheckConstraint(
            "(rate IS NULL) <> (quote_id IS NULL)", name="fx_spots_rate_xor_quote_id"
        ),
        sa.CheckConstraint("rate IS NULL OR rate > 0", name="fx_spots_rate_positive"),
        sa.CheckConstraint("spot_days >= 0", name="fx_spots_spot_days_nonnegative"),
        sa.CheckConstraint(
            "jsonb_typeof(settlement_calendars) = 'array'",
            name="fx_spots_settlement_calendars_array",
        ),
        sa.CheckConstraint("jsonb_typeof(body) = 'object'", name="fx_spots_body_object"),
        schema="app",
    )
    _named_entity_indexes("fx_spots")

    op.create_table(
        "fx_forwards",
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
        *_ts_columns(),
        sa.CheckConstraint("length(trim(name)) > 0", name="fx_forwards_name_nonempty"),
        sa.CheckConstraint(
            "jsonb_typeof(request) = 'object'", name="fx_forwards_request_object"
        ),
        schema="app",
    )
    _named_entity_indexes("fx_forwards")

    op.drop_constraint(
        "pricing_history_product_kind_valid", "pricing_history", schema="app", type_="check"
    )
    op.create_check_constraint(
        "pricing_history_product_kind_valid",
        "pricing_history",
        f"product_kind IN ({_PRODUCT_KINDS_NEW})",
        schema="app",
    )


def downgrade() -> None:
    op.execute("DELETE FROM app.pricing_history WHERE product_kind = 'fx_forwards'")
    op.drop_constraint(
        "pricing_history_product_kind_valid", "pricing_history", schema="app", type_="check"
    )
    op.create_check_constraint(
        "pricing_history_product_kind_valid",
        "pricing_history",
        f"product_kind IN ({_PRODUCT_KINDS_OLD})",
        schema="app",
    )
    _drop_named_entity("fx_forwards")
    _drop_named_entity("fx_spots")
