"""``md.canonical_ids`` SERIES CRUD — the structured catalog rows behind the
Quote Book (``canonical_ids`` create / edit / delete).

This module is the explicit, connector-free SERIES-definition write/read
surface the orchestrator mounts under ``/v1/market-data/series`` so a user
can define / edit / delete a series (its structured metadata) from the app.
The ingester connectors still auto-register the series they ingest; the
user-import path does NOT — it rejects values for a series that has not
been created here first.

Kept in ``md_ingester`` alongside :mod:`quantra_md_ingester.writer` so all
``md.*`` SQL is single-sourced (no MD SQL duplicated in the orchestrator) and
so the module depends only on SQLAlchemy — never the network vendor
connectors / openpyxl import graph the orchestrator must not pull.

Bound parameters rely on ``md_rw``'s pinned ``search_path`` so the table
names are unqualified — identical to :mod:`quantra_md_ingester.writer`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

# Columns a PATCH may edit — every ``md.canonical_ids`` metadata column
# except the immutable primary key (``canonical_id``) and the DB-managed
# timestamps. Whitelisted so the dynamic UPDATE below can never interpolate
# an attacker-controlled identifier into the SQL text.
EDITABLE_COLUMNS: Final[tuple[str, ...]] = (
    "asset_class",
    "family",
    "instrument",
    "currency",
    "tenor",
    "field",
    "frequency",
    "units",
    "description",
)

# The full projection returned by every read / RETURNING clause, in a stable
# column order the API response model mirrors. Inlined verbatim into each
# static SQL string below (never interpolated from a variable) so the query
# text is a compile-time literal — no user input reaches the SQL grammar.
_COLUMNS: Final[str] = (
    "canonical_id, asset_class, family, instrument, currency, tenor, field, "
    "frequency, units, description, created_at, updated_at"
)

_INSERT_SERIES_SQL: Final[str] = """
    INSERT INTO canonical_ids
      (canonical_id, asset_class, family, instrument, currency, tenor, field,
       frequency, units, description)
    VALUES
      (:canonical_id, :asset_class, :family, :instrument, :currency, :tenor,
       :field, :frequency, :units, :description)
    ON CONFLICT (canonical_id) DO NOTHING
    RETURNING canonical_id, asset_class, family, instrument, currency, tenor,
      field, frequency, units, description, created_at, updated_at
"""

_SELECT_ONE_SQL: Final[str] = """
    SELECT canonical_id, asset_class, family, instrument, currency, tenor,
      field, frequency, units, description, created_at, updated_at
    FROM canonical_ids WHERE canonical_id = :canonical_id
"""

_SELECT_ALL_SQL: Final[str] = """
    SELECT canonical_id, asset_class, family, instrument, currency, tenor,
      field, frequency, units, description, created_at, updated_at
    FROM canonical_ids ORDER BY canonical_id
"""

_SELECT_EXISTING_SQL: Final[str] = """
    SELECT canonical_id FROM canonical_ids WHERE canonical_id = ANY(:ids)
"""

# ``WITH ... RETURNING`` so a single round-trip yields the count of cascaded
# quote rows even though the canonical row is deleted separately below.
_DELETE_QUOTES_SQL: Final[str] = """
    WITH deleted AS (
        DELETE FROM quote_points WHERE canonical_id = :canonical_id RETURNING 1
    )
    SELECT count(*) AS deleted_quote_points FROM deleted
"""

_DELETE_SERIES_SQL: Final[str] = """
    DELETE FROM canonical_ids WHERE canonical_id = :canonical_id
    RETURNING canonical_id
"""


async def create_series(conn: AsyncConnection, fields: Mapping[str, Any]) -> dict[str, Any] | None:
    """Insert one ``md.canonical_ids`` row; return it, or ``None`` on conflict.

    ``fields`` must carry every insertable column (the caller derives the
    structural columns from the canonical-id grammar and applies defaults).
    ``ON CONFLICT DO NOTHING`` makes the uniqueness check atomic: a return of
    ``None`` means the ``canonical_id`` already exists (the caller maps that
    to a 409).
    """

    result = await conn.execute(text(_INSERT_SERIES_SQL), dict(fields))
    row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


async def get_series(conn: AsyncConnection, canonical_id: str) -> dict[str, Any] | None:
    """Return one series row (all catalog columns), or ``None`` if missing."""

    result = await conn.execute(text(_SELECT_ONE_SQL), {"canonical_id": canonical_id})
    row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


async def list_series(conn: AsyncConnection) -> list[dict[str, Any]]:
    """Return every series row, ordered by ``canonical_id``."""

    result = await conn.execute(text(_SELECT_ALL_SQL))
    return [dict(row) for row in result.mappings().all()]


async def existing_canonical_ids(conn: AsyncConnection, ids: Sequence[str]) -> set[str]:
    """Return the subset of ``ids`` that already exist in ``md.canonical_ids``.

    Used by the user-import path to REJECT (not auto-create) a value whose
    series has not been defined yet.
    """

    if not ids:
        return set()
    result = await conn.execute(text(_SELECT_EXISTING_SQL), {"ids": list(ids)})
    return {row["canonical_id"] for row in result.mappings().all()}


async def update_series(
    conn: AsyncConnection, canonical_id: str, patch: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Update the given metadata columns of a series; return the new row.

    ``patch`` keys MUST be a subset of :data:`EDITABLE_COLUMNS` (the caller
    validates this). Returns ``None`` when no such series exists (→ 404). An
    empty ``patch`` is treated as a no-op read-back so a caller that sent no
    editable field still gets the current row.
    """

    updates = {k: v for k, v in patch.items() if k in EDITABLE_COLUMNS}
    if not updates:
        return await get_series(conn, canonical_id)
    # ``set_clause`` interpolates only whitelisted (EDITABLE_COLUMNS) column
    # names — never a value — and ``_COLUMNS`` is a compile-time literal, so
    # no user input reaches the SQL grammar (values are bound parameters).
    set_clause = ", ".join(f"{col} = :{col}" for col in updates)
    sql = (
        f"UPDATE canonical_ids SET {set_clause} "  # noqa: S608
        f"WHERE canonical_id = :canonical_id RETURNING {_COLUMNS}"
    )
    params = {**updates, "canonical_id": canonical_id}
    result = await conn.execute(text(sql), params)
    row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


async def delete_series(conn: AsyncConnection, canonical_id: str) -> int | None:
    """Delete a series AND its ``md.quote_points`` values.

    Deletes the values first (so their count is captured — the FK
    ``ON DELETE CASCADE`` would otherwise remove them silently), then the
    canonical row. Returns the number of deleted quote points, or ``None`` if
    the series did not exist (→ 404; the caller's transaction rolls back so no
    partial delete is committed).
    """

    count_result = await conn.execute(text(_DELETE_QUOTES_SQL), {"canonical_id": canonical_id})
    deleted_quote_points = int(count_result.mappings().one()["deleted_quote_points"])
    series_result = await conn.execute(text(_DELETE_SERIES_SQL), {"canonical_id": canonical_id})
    if series_result.mappings().one_or_none() is None:
        return None
    return deleted_quote_points


__all__ = [
    "EDITABLE_COLUMNS",
    "create_series",
    "delete_series",
    "existing_canonical_ids",
    "get_series",
    "list_series",
    "update_series",
]
