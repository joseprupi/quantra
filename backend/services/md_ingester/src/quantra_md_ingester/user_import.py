"""User-supplied market-data import.

A *sanctioned user-import write path* into the shared ``md.*`` catalog.
Today ``md.quote_points`` is written only by the ingester's vendor /
synthetic connectors (``pipeline.fetch_quotes_default``); this module
adds the second, auth-gated, provenance-tagged writer that lets a user
load their own quotes — from a CSV upload or a manual JSON batch —
through the orchestrator's ``POST /v1/market-data/import`` endpoint.

Two public entry points, one shared writer:

* :func:`parse_csv` — the **CSV connector / validator** (the long-deferred
  CSV import). Parses rows ``canonical_id, as_of (YYYY-MM-DD), value[, source,
  meta_json]`` into :class:`UserQuoteInput` records plus a list of
  per-row :class:`ImportRowError` for rows it could not parse. Pure
  (no DB, no I/O) so it is trivially unit-testable and deterministic.
* :func:`import_user_quotes` — the **shared upsert**. Validates each
  :class:`UserQuoteInput` (canonical-id grammar, in-batch duplicate keys)
  and upserts the survivors into ``md.quote_points`` by reusing the
  *exact* SQL the synthetic/vendor connectors write through
  (:func:`quantra_md_ingester.writer.upsert_quote`) — no SQL is
  duplicated here. Deterministic and idempotent: re-importing the same
  batch upserts in place (PK ``(canonical_id, as_of)``) with no new rows.

Provenance. Every imported row carries a ``source`` tag — the
default is passed by the caller (``"csv"`` for uploads, ``"manual"`` for
the JSON batch) and a per-row ``source`` overrides it — plus
``meta["user_import"] = True`` so the row is never mistaken for a vendor
or synthetic observation. The ``source`` column is the operational flag
the MD read path (``/quotes/resolved``) surfaces back to the caller.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_common.logging import get_logger
from quantra_md_ingester.canonical import validate_canonical_id
from quantra_md_ingester.models import QuoteRecord
from quantra_md_ingester.series import existing_canonical_ids
from quantra_md_ingester.writer import upsert_quote

logger = get_logger(__name__)

# Default provenance tags. The orchestrator route passes one of these as
# ``default_source``; a per-row ``source`` (CSV column / JSON field)
# overrides it.
SOURCE_CSV: Final[str] = "csv"
SOURCE_MANUAL: Final[str] = "manual"

_AS_OF_FORMAT: Final[str] = "%Y-%m-%d"
_CSV_HEADER_FIRST_CELL: Final[str] = "canonical_id"
_MIN_CSV_COLUMNS: Final[int] = 3
_SOURCE_COLUMN_INDEX: Final[int] = 3
_META_COLUMN_INDEX: Final[int] = 4
_MAX_CSV_COLUMNS: Final[int] = 5


@dataclass(slots=True)
class UserQuoteInput:
    """One user-supplied quote, prior to validation against the DB grammar.

    ``row`` is the 1-based position in the caller's batch (CSV data row or
    JSON array index + 1) so a per-row error report points the user at the
    offending line. ``as_of`` is the raw ``YYYY-MM-DD`` string; ``value``
    is already numeric (CSV parses it; JSON supplies it).
    """

    row: int
    canonical_id: str
    as_of: str
    value: float
    source: str | None = None
    meta: dict[str, Any] | None = None


@dataclass(slots=True)
class ImportRowError:
    """A single soft, per-row failure. Reported in the body, never a 5xx."""

    row: int
    reason: str


@dataclass(slots=True)
class ImportReport:
    """Outcome of an import batch.

    ``imported`` counts rows upserted into ``md.quote_points``;
    ``skipped`` counts rows dropped for a per-row reason (bad grammar,
    unparseable field, in-batch duplicate key). ``errors`` carries the
    reason for every skipped row. ``imported + skipped`` equals the total
    number of input rows the caller submitted.
    """

    imported: int = 0
    skipped: int = 0
    errors: list[ImportRowError] = field(default_factory=list)


# ---------------------------------------------------------------------------
# CSV connector / validator. Pure parsing — no DB, no I/O.
# ---------------------------------------------------------------------------


def parse_csv(data: str) -> tuple[list[UserQuoteInput], list[ImportRowError]]:
    """Parse a CSV document into ``UserQuoteInput`` records + per-row errors.

    Expected columns (an optional header row whose first cell is
    ``canonical_id`` is detected and skipped):

        ``canonical_id, as_of (YYYY-MM-DD), value[, source, meta_json]``

    Blank lines are ignored. A row with too few / too many columns, a
    non-numeric ``value``, or a ``meta_json`` that is not a JSON object is
    returned as an :class:`ImportRowError` rather than aborting the parse,
    so one bad line never rejects the whole file. Canonical-id grammar and
    date validity are checked later in :func:`import_user_quotes` (the DB
    stage) so parsing stays purely structural.
    """

    quotes: list[UserQuoteInput] = []
    errors: list[ImportRowError] = []
    reader = csv.reader(io.StringIO(data))
    row_number = 0
    for raw in reader:
        # Drop trailing empty cells (trailing commas / padding) then skip a
        # wholly blank line without consuming a data-row number.
        cells = [c.strip() for c in raw]
        while cells and cells[-1] == "":
            cells.pop()
        if not cells:
            continue
        row_number += 1
        if row_number == 1 and cells[0].lower() == _CSV_HEADER_FIRST_CELL:
            # Header line — not a data row; don't count it.
            row_number = 0
            continue

        if len(cells) < _MIN_CSV_COLUMNS or len(cells) > _MAX_CSV_COLUMNS:
            errors.append(
                ImportRowError(
                    row=row_number,
                    reason=(
                        f"expected {_MIN_CSV_COLUMNS}-{_MAX_CSV_COLUMNS} columns "
                        f"(canonical_id, as_of, value[, source, meta_json]); "
                        f"got {len(cells)}"
                    ),
                )
            )
            continue

        canonical_id, as_of, raw_value = cells[0], cells[1], cells[2]
        has_source = len(cells) > _SOURCE_COLUMN_INDEX and bool(cells[_SOURCE_COLUMN_INDEX])
        source = cells[_SOURCE_COLUMN_INDEX] if has_source else None
        meta: dict[str, Any] | None = None
        if len(cells) == _MAX_CSV_COLUMNS and cells[_META_COLUMN_INDEX]:
            parsed_meta = _parse_meta_json(cells[_META_COLUMN_INDEX])
            if parsed_meta is None:
                errors.append(
                    ImportRowError(
                        row=row_number,
                        reason=f"meta_json is not a JSON object: {cells[4]!r}",
                    )
                )
                continue
            meta = parsed_meta

        try:
            value = float(raw_value)
        except ValueError:
            errors.append(
                ImportRowError(
                    row=row_number,
                    reason=f"value is not a number: {raw_value!r}",
                )
            )
            continue

        quotes.append(
            UserQuoteInput(
                row=row_number,
                canonical_id=canonical_id,
                as_of=as_of,
                value=value,
                source=source,
                meta=meta,
            )
        )
    return quotes, errors


def _parse_meta_json(raw: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


# ---------------------------------------------------------------------------
# Shared upsert into the SHARED md.* catalog. Reuses the ingester's
# writer SQL verbatim (writer.upsert_quote).
# ---------------------------------------------------------------------------


async def import_user_quotes(
    engine: AsyncEngine,
    quotes: Sequence[UserQuoteInput],
    *,
    default_source: str,
) -> ImportReport:
    """Validate + upsert ``quotes`` into the shared ``md.*`` catalog.

    Per-row validation (soft errors, reported not raised):

    * ``canonical_id`` must satisfy the canonical-id grammar (strict
      six-field or portal short form) — see
      :func:`quantra_md_ingester.canonical.validate_canonical_id`.
    * ``as_of`` must parse as ``YYYY-MM-DD``.
    * ``(canonical_id, as_of)`` must be unique within the batch (the DB PK);
      the first occurrence wins and later duplicates are skipped.
    * the ``canonical_id`` must already exist in ``md.canonical_ids`` — import
      ADDS VALUES to a defined series, it does NOT create one. A value for an
      unknown series is a per-row soft error (create the series first via
      ``POST /v1/market-data/series`` / Quote Book → New series); it is
      **not** auto-created.

    Survivors are upserted into ``md.quote_points`` on the
    ``(canonical_id, as_of)`` PK inside a single transaction, reusing the
    exact ingester writer SQL. Idempotent: a re-run of the same batch changes
    no row counts and adds no rows.
    """

    report = ImportReport()
    valid: list[tuple[UserQuoteInput, QuoteRecord]] = []
    seen: set[tuple[str, str]] = set()

    for q in quotes:
        canonical_id = q.canonical_id.strip()
        as_of_raw = q.as_of.strip()
        if not canonical_id or not validate_canonical_id(canonical_id):
            report.errors.append(
                ImportRowError(row=q.row, reason=f"invalid canonical_id: {q.canonical_id!r}")
            )
            continue
        as_of_dt = _parse_as_of(as_of_raw)
        if as_of_dt is None:
            report.errors.append(
                ImportRowError(
                    row=q.row,
                    reason=f"as_of is not a valid YYYY-MM-DD date: {q.as_of!r}",
                )
            )
            continue
        key = (canonical_id, as_of_raw)
        if key in seen:
            report.errors.append(
                ImportRowError(
                    row=q.row,
                    reason=(
                        f"duplicate key (canonical_id={canonical_id}, as_of={as_of_raw}) "
                        "within batch; first occurrence kept"
                    ),
                )
            )
            continue
        seen.add(key)

        source = (q.source or "").strip() or default_source
        meta: dict[str, Any] = dict(q.meta or {})
        meta["user_import"] = True
        meta.setdefault("import_default_source", default_source)
        record = QuoteRecord(
            canonical_id=canonical_id,
            as_of=as_of_dt,
            value=q.value,
            source=source,
            vendor_id=None,
            quality_flags={"user_import": True},
            meta=meta,
        )
        valid.append((q, record))

    if not valid:
        report.skipped = len(report.errors)
        logger.info(
            "md_user_import.complete",
            default_source=default_source,
            imported=0,
            skipped=report.skipped,
        )
        return report

    async with engine.begin() as conn:
        # Import ADDS VALUES to an existing series; it never defines one.
        # Resolve which of the batch's series already exist, then reject
        # (soft error) any value whose series is unknown.
        wanted = {record.canonical_id for _q, record in valid}
        known = await existing_canonical_ids(conn, sorted(wanted))
        for q, record in valid:
            if record.canonical_id not in known:
                report.errors.append(
                    ImportRowError(
                        row=q.row,
                        reason=(
                            f"series {record.canonical_id!r} does not exist — "
                            "create it first (Quote Book → New series)"
                        ),
                    )
                )
                continue
            await upsert_quote(conn, record)
            report.imported += 1

    report.errors.sort(key=lambda e: e.row)
    report.skipped = len(report.errors)
    logger.info(
        "md_user_import.complete",
        default_source=default_source,
        imported=report.imported,
        skipped=report.skipped,
    )
    return report


def _parse_as_of(value: str) -> datetime | None:
    try:
        parsed = datetime.strptime(value, _AS_OF_FORMAT)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC)


__all__ = [
    "SOURCE_CSV",
    "SOURCE_MANUAL",
    "ImportReport",
    "ImportRowError",
    "UserQuoteInput",
    "import_user_quotes",
    "parse_csv",
]
