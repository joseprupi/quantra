"""entity_versions — append-only amendment history for app.* entities

Every mutable named entity (indices, curves, …, the seven product
tables) gets a full audit trail: one ``app.entity_versions`` row per
create / amend / delete / restore, carrying the actor, the timestamp,
an optional human reason, and the FULL row snapshot after the change.

Design notes:

* **Append-only at the DATABASE level.** The app roles receive only
  ``SELECT`` (+ ``INSERT`` for ``app_rw``); ``UPDATE`` / ``DELETE`` /
  ``TRUNCATE`` are revoked, so even a compromised app process cannot
  rewrite history. Only the migration/admin role can (for retention
  ops).
* **``entity_type``** is the spec/table key (``swaps_ir``, ``curves``,
  ``indices``, …) — the same token ``pricing_history.product_kind``
  uses for the product tables, so the two audit surfaces join
  naturally.
* **``payload``** is the full row snapshot *after* the change; for
  deletes it is the row state at deletion (``deleted_at`` set).
* **``request_id``** groups all writes performed by one user action
  (e.g. a portal product Save persisting several rows).
* **Backfill.** Every existing row of every versioned table — including
  soft-deleted ones — gets version 1 (``change_type='create'``,
  ``changed_by_uid=owner_uid``, ``changed_at=updated_at``) so the
  "no versions ⇒ row does not exist" invariant holds from day one.

Revision ID: 0013_entity_versions
Revises: 0012_drop_quotes_saved
Create Date: 2026-07-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_entity_versions"
down_revision: str | None = "0012_drop_quotes_saved"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The versioned tables, i.e. every named entity the generic CRUD
# repository serves. Kept as a frozen snapshot here (migrations must not
# import application code); mirrors ``NAMED_ENTITY_SPECS`` in
# ``quantra_orchestrator.data.specs`` at the time of this revision.
_VERSIONED_TABLES: tuple[str, ...] = (
    "indices",
    "curves",
    "curve_sets",
    "credit_curves",
    "snapshots",
    "vol_surfaces",
    "swaption_models",
    "swaps_ir",
    "swaps_inflation",
    "swaptions",
    "bonds_fixed",
    "bonds_floating",
    "cds",
    "equity_options",
)


def upgrade() -> None:
    op.create_table(
        "entity_versions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("public.gen_random_uuid()"),
        ),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        # Full row snapshot AFTER the change (row state at deletion for
        # deletes). Clients diff two versions themselves; no server-side
        # diff is stored.
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("change_type", sa.Text(), nullable=False),
        sa.Column("change_reason", sa.Text(), nullable=True),
        sa.Column("changed_by_uid", sa.Text(), nullable=False),
        sa.Column("changed_by_email", sa.Text(), nullable=True),
        sa.Column(
            "changed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Groups all version rows written by one user action (bound by
        # RequestIdMiddleware). Nullable: writes from scripts / seeds
        # have no request context.
        sa.Column("request_id", sa.Text(), nullable=True),
        # Tenancy scope — matches the head row's owner_uid so the read
        # API can enforce the 404-not-403 rule with one WHERE clause.
        sa.Column("owner_uid", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "change_type IN ('create', 'amend', 'delete', 'restore')",
            name="entity_versions_change_type_valid",
        ),
        sa.CheckConstraint("version_no >= 1", name="entity_versions_version_no_positive"),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="entity_versions_payload_object",
        ),
        sa.UniqueConstraint(
            "entity_type",
            "entity_id",
            "version_no",
            name="uq_entity_versions_entity_version",
        ),
        schema="app",
    )
    op.create_index(
        "ix_entity_versions_entity",
        "entity_versions",
        ["entity_type", "entity_id"],
        schema="app",
    )
    op.create_index(
        "ix_entity_versions_owner_uid",
        "entity_versions",
        ["owner_uid"],
        schema="app",
    )

    # -- DB-level immutability -------------------------------------------------
    # 0001_init's ALTER DEFAULT PRIVILEGES granted app_rw
    # SELECT/INSERT/UPDATE/DELETE on every new table in schema app.
    # Strip that back to append-only: app_rw may INSERT + SELECT,
    # app_ro may SELECT, and NOBODY but the admin role can UPDATE /
    # DELETE / TRUNCATE history.
    op.execute("REVOKE ALL ON TABLE app.entity_versions FROM app_rw, app_ro")
    op.execute("GRANT SELECT, INSERT ON TABLE app.entity_versions TO app_rw")
    op.execute("GRANT SELECT ON TABLE app.entity_versions TO app_ro")

    # -- Backfill: version 1 for every existing row ----------------------------
    # Includes soft-deleted rows (their trail starts at the same v1;
    # the delete itself predates the audit table and is not recoverable).
    for table in _VERSIONED_TABLES:
        op.execute(
            f"""
            INSERT INTO app.entity_versions (
                entity_type, entity_id, version_no, payload, change_type,
                change_reason, changed_by_uid, changed_by_email, changed_at,
                request_id, owner_uid
            )
            SELECT
                '{table}', t.id, 1, to_jsonb(t), 'create',
                'backfilled at audit-trail introduction', t.owner_uid, NULL,
                t.updated_at, NULL, t.owner_uid
            FROM app.{table} AS t
            """  # noqa: S608 -- table name from the fixed allow-list above
        )


def downgrade() -> None:
    op.drop_index("ix_entity_versions_owner_uid", table_name="entity_versions", schema="app")
    op.drop_index("ix_entity_versions_entity", table_name="entity_versions", schema="app")
    op.drop_table("entity_versions", schema="app")
