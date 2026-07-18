"""snapshots, snapshot_quotes, and the version_etag trigger

Named market-data snapshots — the orchestrator's "pin this point in
time" primitive. A snapshot collects a single resolved
value per ``canonical_id`` at a particular ``as_of``, frozen at
ingestion time. Re-running the ingester for the same
``(name, as_of)`` rewrites the snapshot's contents.

Schema highlights:

* ``snapshots.version_etag`` is the cache-invalidation primitive
  promised by O1. It is an opaque random token (UUIDv4 stripped
  of hyphens), regenerated whenever any row in ``snapshot_quotes`` for
  this snapshot changes — see ``md.bump_snapshot_etag()`` below.
  Stored as text so cache keys can use it directly as part of an HTTP
  ``ETag`` header without any encoding step.

* ``snapshots.status`` mirrors the legacy MD service's free-form
  status column (``"ready"`` / ``"pending"`` / ``"failed"``) since
  endpoints like ``/snapshots`` and ``/snapshots/latest`` return it.

* ``snapshot_quotes.resolved_as_of`` is the actual ``quote_points.as_of``
  that satisfied the snapshot's request, distinct from
  ``snapshots.as_of`` (the user-requested cut-off). Same data the
  legacy ingester writes into the ``meta`` JSON; promoting it to a
  first-class column makes the "is this row's value exact for the
  snapshot's as_of?" check a simple equality.

* ``meta`` JSONB on both tables carries the open-ended payload the
  current API returns (source, vendor_id, etc.).

etag implementation choice (per plan): **trigger** on
``snapshot_quotes`` mutations, not an explicit ``UPDATE`` in the
ingester. The trigger fires for ``INSERT``, ``UPDATE``, ``DELETE`` so
any write that affects a snapshot's effective contents — including
ad-hoc ``psql`` fixups and the ingester's
``DELETE; INSERT-from-quote_points`` rebuild — bumps the etag. An
explicit-update path would silently miss the ``DELETE`` case and
would be skippable in ad-hoc maintenance. The cost (one extra
``UPDATE snapshots`` per row-batch transaction, coalesced by
``STATEMENT`` triggers) is acceptable for a write path that runs
once per ingest cycle.

Revision ID: 0004_snapshots
Revises: 0003_quote_points
Create Date: 2026-05-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_snapshots"
down_revision: str | None = "0003_quote_points"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# A 32-character hex string (UUIDv4 with the hyphens stripped). Long
# enough to be globally unique without coordination; short enough to
# sit nicely in an HTTP ETag header.
_ETAG_EXPR = "replace(public.gen_random_uuid()::text, '-', '')"


def upgrade() -> None:
    op.create_table(
        "snapshots",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("public.gen_random_uuid()"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("as_of", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'ready'"),
        ),
        # `version_etag` is generated server-side on every insert and
        # bumped on every snapshot_quotes mutation by the trigger
        # installed below. Application code must never set it.
        sa.Column(
            "version_etag",
            sa.Text(),
            nullable=False,
            server_default=sa.text(_ETAG_EXPR),
        ),
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
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
        sa.UniqueConstraint("name", "as_of", name="uq_snapshots_name_as_of"),
        sa.CheckConstraint("length(trim(name)) > 0", name="snapshots_name_nonempty"),
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'failed')",
            name="snapshots_status_valid",
        ),
        sa.CheckConstraint("length(version_etag) > 0", name="snapshots_version_etag_nonempty"),
        sa.CheckConstraint("jsonb_typeof(meta) = 'object'", name="snapshots_meta_object"),
        schema="md",
    )
    op.create_index("ix_snapshots_name", "snapshots", ["name"], schema="md")
    op.create_index(
        "ix_snapshots_name_as_of_desc",
        "snapshots",
        ["name", sa.text("as_of DESC")],
        schema="md",
    )
    op.execute(
        """
        CREATE TRIGGER snapshots_set_updated_at
          BEFORE UPDATE ON md.snapshots
          FOR EACH ROW EXECUTE FUNCTION md.set_updated_at();
        """
    )

    op.create_table(
        "snapshot_quotes",
        sa.Column(
            "snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("md.snapshots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "canonical_id",
            sa.Text(),
            sa.ForeignKey("md.canonical_ids.canonical_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("value", sa.Float(), nullable=False),
        # The actual quote_points.as_of that produced this row. Differs
        # from snapshots.as_of when no exact match existed and a
        # previous-business-day value was carried forward.
        sa.Column("resolved_as_of", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("vendor_id", sa.Text(), nullable=True),
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("snapshot_id", "canonical_id", name="snapshot_quotes_pkey"),
        sa.CheckConstraint("jsonb_typeof(meta) = 'object'", name="snapshot_quotes_meta_object"),
        schema="md",
    )
    # The PK already covers `WHERE snapshot_id = ?` so no extra index
    # is needed for that case. We add a `(canonical_id)` index for
    # "which snapshots include this canonical?" queries that the
    # orchestrator may run when invalidating per-quote caches.
    op.create_index(
        "ix_snapshot_quotes_canonical_id",
        "snapshot_quotes",
        ["canonical_id"],
        schema="md",
    )

    # version_etag bump trigger. We use a STATEMENT-level trigger (one
    # invocation per modifying statement, not per row) so a bulk
    # rebuild of a snapshot's quotes results in a single etag change,
    # not one per row. The transition tables let us touch every
    # affected snapshot in a single UPDATE.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION md.bump_snapshot_etag() RETURNS trigger AS $$
        BEGIN
          IF (TG_OP = 'DELETE') THEN
            UPDATE md.snapshots s
               SET version_etag = {_ETAG_EXPR},
                   updated_at = now()
             WHERE s.id IN (SELECT DISTINCT snapshot_id FROM old_table);
          ELSIF (TG_OP = 'INSERT') THEN
            UPDATE md.snapshots s
               SET version_etag = {_ETAG_EXPR},
                   updated_at = now()
             WHERE s.id IN (SELECT DISTINCT snapshot_id FROM new_table);
          ELSE
            UPDATE md.snapshots s
               SET version_etag = {_ETAG_EXPR},
                   updated_at = now()
             WHERE s.id IN (
                 SELECT snapshot_id FROM new_table
                 UNION
                 SELECT snapshot_id FROM old_table
             );
          END IF;
          RETURN NULL;
        END
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER snapshot_quotes_bump_etag_ins
          AFTER INSERT ON md.snapshot_quotes
          REFERENCING NEW TABLE AS new_table
          FOR EACH STATEMENT EXECUTE FUNCTION md.bump_snapshot_etag();
        """
    )
    op.execute(
        """
        CREATE TRIGGER snapshot_quotes_bump_etag_upd
          AFTER UPDATE ON md.snapshot_quotes
          REFERENCING NEW TABLE AS new_table OLD TABLE AS old_table
          FOR EACH STATEMENT EXECUTE FUNCTION md.bump_snapshot_etag();
        """
    )
    op.execute(
        """
        CREATE TRIGGER snapshot_quotes_bump_etag_del
          AFTER DELETE ON md.snapshot_quotes
          REFERENCING OLD TABLE AS old_table
          FOR EACH STATEMENT EXECUTE FUNCTION md.bump_snapshot_etag();
        """
    )


def downgrade() -> None:
    for trig in (
        "snapshot_quotes_bump_etag_ins",
        "snapshot_quotes_bump_etag_upd",
        "snapshot_quotes_bump_etag_del",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trig} ON md.snapshot_quotes")
    op.execute("DROP FUNCTION IF EXISTS md.bump_snapshot_etag()")

    op.drop_index(
        "ix_snapshot_quotes_canonical_id",
        table_name="snapshot_quotes",
        schema="md",
    )
    op.drop_table("snapshot_quotes", schema="md")

    op.execute("DROP TRIGGER IF EXISTS snapshots_set_updated_at ON md.snapshots")
    op.drop_index("ix_snapshots_name_as_of_desc", table_name="snapshots", schema="md")
    op.drop_index("ix_snapshots_name", table_name="snapshots", schema="md")
    op.drop_table("snapshots", schema="md")
