"""Async ingest + snapshot-build pipeline.

The pipeline orchestration is async (we hold a single ``AsyncEngine``
from :func:`quantra_common.db.make_md_engine` for all writes — D6 +
pool isolation), but the vendor connectors stay synchronous and
are run via :func:`asyncio.to_thread` so the event loop is never
blocked by network I/O.

SQL targets the new ``md.*`` schema introduced by
``database/03_md_schema.md``. Bound parameters use
``md_rw``'s pinned ``search_path`` so table names are
unqualified in the statements; that keeps the SQL one rename away
from a future schema reshuffle and matches what the lifted MD read
service does on the read side.

Notably:

* ``md.canonical_ids`` replaces legacy ``canonical_instruments``.
* ``md.quote_points`` PK is ``(canonical_id, as_of)`` — drops
  ``source`` from the conflict key; late vendor corrections upsert
  in place with a trigger-bumped ``ingested_at``.
* ``md.snapshot_quotes`` writes fire the ``md.bump_snapshot_etag``
  STATEMENT-level trigger, which rolls a fresh
  ``snapshots.version_etag`` per write batch.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_common.db import make_md_engine
from quantra_common.logging import get_logger
from quantra_common.settings.base import Settings
from quantra_md_ingester.canonical import validate_canonical_id
from quantra_md_ingester.connectors import (
    BoeAdapter,
    BoeGovtCurveAdapter,
    BoeOisCurveAdapter,
    EcbAdapter,
    FredAdapter,
    SyntheticAdapter,
    TreasuryAdapter,
)
from quantra_md_ingester.models import QuoteRecord
from quantra_md_ingester.specs import (
    default_boe_config_path,
    default_boe_govt_curve_config_path,
    default_boe_ois_curve_config_path,
    default_ecb_config_paths,
    default_fred_config_paths,
    default_synthetic_config_paths,
    default_treasury_config_paths,
    load_boe_govt_curve_series,
    load_boe_ois_curve_series,
    load_boe_series,
    load_ecb_series,
    load_fred_series,
    load_synthetic_series,
    load_treasury_series,
)
from quantra_md_ingester.writer import (
    upsert_canonical,
    upsert_quote,
    upsert_vendor_mapping,
)

logger = get_logger(__name__)

# Connector identifiers, matched exactly to the legacy ``--sources`` CLI
# values so configs and snapshots remain interchangeable across the lift.
# ``synthetic`` is in the *allow-list* (``ALL_SOURCES`` — what
# ``--source synthetic`` validates against) but NOT in ``DEFAULT_SOURCES``
# (what a bare ``run`` / ``ingest`` with no ``--source`` pulls). That keeps
# demo synthetic data strictly opt-in so it never silently mixes into a
# real-vendor ingest run.
DEFAULT_SOURCES: Final[frozenset[str]] = frozenset({"fred", "ecb", "boe", "treasury"})
# ``boe_ois`` (real GBP SONIA OIS curve) and ``synthetic`` are both
# opt-in via an explicit ``--source`` so a bare vendor ingest run's surface
# stays unchanged; the operator schedules the OIS pull separately.
ALL_SOURCES: Final[frozenset[str]] = DEFAULT_SOURCES | frozenset({"synthetic", "boe_ois"})

DEFAULT_SNAPSHOT_NAME: Final[str] = "PUBLIC_USD_EOD"


# ---------------------------------------------------------------------------
# Connector fetch — sync work wrapped onto a thread.
# ---------------------------------------------------------------------------


# ``QuoteFetcher`` is the dependency-injection seam the tests use to
# replace network calls with deterministic fixtures.
QuoteFetcher = Callable[
    [set[str], date, date],
    Awaitable[list[QuoteRecord]],
]


async def fetch_quotes_default(
    sources: set[str],
    as_of: date,
    start: date,
) -> list[QuoteRecord]:
    """Fetch every requested source via the legacy connector adapters.

    Each adapter is sync (urllib + stdlib parsers) so this function
    schedules them through ``asyncio.to_thread``. Adapter-level errors
    are *not* swallowed — the connector classes already log and skip
    per-series failures, and a hard failure (e.g. ``FRED_API_KEY``
    missing) is something the caller wants to see, not hide.
    """

    out: list[QuoteRecord] = []

    if "fred" in sources:
        fred_specs = _coalesce_specs(default_fred_config_paths(), load_fred_series)
        if fred_specs:
            out.extend(
                await asyncio.to_thread(FredAdapter().fetch_series, fred_specs, as_of, start)
            )

    if "ecb" in sources:
        ecb_specs = _coalesce_specs(default_ecb_config_paths(), load_ecb_series)
        if ecb_specs:
            out.extend(await asyncio.to_thread(EcbAdapter().fetch_series, ecb_specs, as_of, start))

    if "boe" in sources:
        boe_path = default_boe_config_path()
        if boe_path.exists():
            boe_specs = load_boe_series(boe_path)
            out.extend(await asyncio.to_thread(BoeAdapter().fetch_series, boe_specs, as_of, start))
        boe_curve_path = default_boe_govt_curve_config_path()
        if boe_curve_path.exists():
            curve_specs = load_boe_govt_curve_series(boe_curve_path)
            out.extend(
                await asyncio.to_thread(
                    BoeGovtCurveAdapter().fetch_series, curve_specs, as_of, start
                )
            )

    if "boe_ois" in sources:
        # Real GBP SONIA OIS spot curve (BoE daily public feed). Opt-in
        # source so it never rides a bare vendor ingest; emits
        # ``GBP.RATES.BOE.OIS.{tenor}.YIELD`` series.
        out.extend(await _fetch_boe_ois(as_of, start))

    if "treasury" in sources:
        treasury_specs = _coalesce_specs(default_treasury_config_paths(), load_treasury_series)
        if treasury_specs:
            out.extend(
                await asyncio.to_thread(
                    TreasuryAdapter().fetch_series, treasury_specs, as_of, start
                )
            )

    if "synthetic" in sources:
        # no network; the adapter generates deterministic standing
        # values. Run on a thread anyway for symmetry with the vendor
        # adapters (and to keep the event loop free if the spec list grows).
        synthetic_specs = _coalesce_specs(default_synthetic_config_paths(), load_synthetic_series)
        if synthetic_specs:
            out.extend(
                await asyncio.to_thread(
                    SyntheticAdapter().fetch_series, synthetic_specs, as_of, start
                )
            )
    return out


async def _fetch_boe_ois(as_of: date, start: date) -> list[QuoteRecord]:
    """Fetch the real BoE SONIA OIS spot curve (opt-in ``boe_ois`` source)."""

    path = default_boe_ois_curve_config_path()
    if not path.exists():
        return []
    ois_specs = load_boe_ois_curve_series(path)
    if not ois_specs:
        return []
    return await asyncio.to_thread(BoeOisCurveAdapter().fetch_series, ois_specs, as_of, start)


def _coalesce_specs[T](
    paths: tuple[Path, ...],
    loader: Callable[[Path], list[T]],
) -> list[T]:
    specs: list[T] = []
    for path in paths:
        if path.exists():
            specs.extend(loader(path))
    return specs


# ---------------------------------------------------------------------------
# Public pipeline entry points.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class IngestResult:
    """Counters returned by :func:`ingest_quotes`."""

    fetched: int
    upserted: int
    skipped_invalid_canonical_id: int


@dataclass(slots=True)
class SnapshotBuildResult:
    """Counters returned by :func:`build_snapshot`."""

    snapshot_id: uuid.UUID
    name: str
    as_of: datetime
    quotes_written: int
    quotes_changed: int
    version_etag_before: str | None
    version_etag_after: str


@dataclass(slots=True)
class PipelineResult:
    """Combined result of :func:`run_pipeline`."""

    ingest: IngestResult
    snapshot: SnapshotBuildResult


async def ingest_quotes(
    *,
    engine: AsyncEngine,
    as_of: date,
    start_date: date | None = None,
    sources: set[str] | None = None,
    fetcher: QuoteFetcher = fetch_quotes_default,
) -> IngestResult:
    """Fetch + upsert quotes for ``sources`` between ``start_date`` and ``as_of``.

    This is the ``ingest`` / ``backfill`` CLI surface. It writes to
    ``canonical_ids``, ``vendor_mappings``, and ``quote_points`` and
    nothing else — no snapshot is built.
    """

    start = start_date or as_of
    if start > as_of:
        msg = f"start_date ({start.isoformat()}) cannot be after as_of ({as_of.isoformat()})"
        raise RuntimeError(msg)
    selected = _normalize_sources(sources)
    # wrap the fetcher in a try/except so a vendor outage (or
    # any other unexpected fetcher failure) emits a structured
    # ``md_ingester.ingest.failed`` event *before* the exception
    # propagates. Critically, we have not opened a write transaction
    # yet, so the failure cannot corrupt ``md.quote_points`` and the
    # next scheduled tick resumes cleanly. The exception is re-raised
    # so the cron's exit code surfaces the failure to the scheduler.
    try:
        quotes = await fetcher(selected, as_of, start)
    except Exception as exc:
        logger.error(
            "md_ingester.ingest.failed",
            as_of=as_of.isoformat(),
            start_date=start.isoformat(),
            sources=sorted(selected),
            error_class=type(exc).__name__,
            error_message=str(exc),
        )
        raise
    fetched = len(quotes)
    upserted, skipped = await _write_quote_batch(engine, quotes)
    logger.info(
        "md_ingester.ingest.complete",
        as_of=as_of.isoformat(),
        start_date=start.isoformat(),
        sources=sorted(selected),
        fetched=fetched,
        upserted=upserted,
        skipped_invalid_canonical_id=skipped,
    )
    return IngestResult(
        fetched=fetched,
        upserted=upserted,
        skipped_invalid_canonical_id=skipped,
    )


async def build_snapshot(
    *,
    engine: AsyncEngine,
    as_of: date,
    snapshot_name: str = DEFAULT_SNAPSHOT_NAME,
) -> SnapshotBuildResult:
    """Rebuild the ``snapshot_name`` snapshot for ``as_of`` from current quotes.

    Picks the latest ``quote_points`` row per ``canonical_id`` with
    ``as_of <= :as_of`` (the same query the legacy worker used) and
    writes the resulting row set as the snapshot's contents. The
    STATEMENT-level ``md.bump_snapshot_etag`` trigger fires on the
    ``DELETE`` + ``INSERT`` we issue, so ``version_etag`` is fresh
    once the transaction commits.
    """

    as_of_dt = _as_utc_datetime(as_of)

    async with engine.begin() as conn:
        existing_etag = (
            await conn.execute(
                text(
                    """
                    SELECT version_etag FROM snapshots
                    WHERE name = :name AND as_of = :as_of
                    """
                ),
                {"name": snapshot_name, "as_of": as_of_dt},
            )
        ).scalar_one_or_none()

        explicit_id = uuid.uuid4()
        row = (
            await conn.execute(
                text(
                    """
                    INSERT INTO snapshots (id, name, as_of, status, meta)
                    VALUES (:id, :name, :as_of, 'ready', '{}'::jsonb)
                    ON CONFLICT (name, as_of)
                    DO UPDATE SET status = EXCLUDED.status
                    RETURNING id
                    """
                ),
                {"id": explicit_id, "name": snapshot_name, "as_of": as_of_dt},
            )
        ).first()
        if row is None:
            msg = "snapshots upsert returned no row — unexpected"
            raise RuntimeError(msg)
        snapshot_id = row.id

        # Capture the prior (canonical_id -> value) map before DELETE so
        # we can compute ``quotes_changed`` after the rebuild — the
        # operational signal for "did the etag bump correspond to a real
        # value change?". A no-op rebuild still
        # advances ``version_etag`` (trigger fires per statement,
        # not per content delta) and we want telemetry to disambiguate.
        prior_rows = (
            await conn.execute(
                text(
                    """
                    SELECT canonical_id, value
                    FROM snapshot_quotes
                    WHERE snapshot_id = :snapshot_id
                    """
                ),
                {"snapshot_id": snapshot_id},
            )
        ).all()
        prior_values: dict[str, float] = {row.canonical_id: float(row.value) for row in prior_rows}

        await conn.execute(
            text("DELETE FROM snapshot_quotes WHERE snapshot_id = :snapshot_id"),
            {"snapshot_id": snapshot_id},
        )
        result = await conn.execute(
            text(
                """
                INSERT INTO snapshot_quotes
                  (snapshot_id, canonical_id, value, resolved_as_of, source,
                   vendor_id, meta)
                SELECT :snapshot_id,
                       latest.canonical_id,
                       latest.value,
                       latest.as_of,
                       latest.source,
                       latest.vendor_id,
                       jsonb_build_object(
                         'source', latest.source,
                         'vendor_id', latest.vendor_id,
                         'as_of', latest.as_of
                       )
                FROM (
                  SELECT DISTINCT ON (canonical_id)
                         canonical_id, value, source, vendor_id, as_of
                  FROM quote_points
                  WHERE as_of <= :as_of
                  ORDER BY canonical_id, as_of DESC
                ) latest
                """
            ),
            {"snapshot_id": snapshot_id, "as_of": as_of_dt},
        )
        quotes_written = result.rowcount or 0

        new_rows = (
            await conn.execute(
                text(
                    """
                    SELECT canonical_id, value
                    FROM snapshot_quotes
                    WHERE snapshot_id = :snapshot_id
                    """
                ),
                {"snapshot_id": snapshot_id},
            )
        ).all()
        new_values: dict[str, float] = {row.canonical_id: float(row.value) for row in new_rows}
        quotes_changed = _diff_count(prior_values, new_values)

        new_etag = (
            await conn.execute(
                text("SELECT version_etag FROM snapshots WHERE id = :id"),
                {"id": snapshot_id},
            )
        ).scalar_one()

    logger.info(
        "md_ingester.snapshot.built",
        snapshot_id=str(snapshot_id),
        name=snapshot_name,
        as_of=as_of.isoformat(),
        quotes_written=quotes_written,
        quotes_changed=quotes_changed,
        version_etag_before=existing_etag,
        version_etag_after=new_etag,
    )
    return SnapshotBuildResult(
        snapshot_id=snapshot_id,
        name=snapshot_name,
        as_of=as_of_dt,
        quotes_written=quotes_written,
        quotes_changed=quotes_changed,
        version_etag_before=existing_etag,
        version_etag_after=new_etag,
    )


async def run_pipeline(
    *,
    engine: AsyncEngine,
    as_of: date,
    start_date: date | None = None,
    sources: set[str] | None = None,
    snapshot_name: str = DEFAULT_SNAPSHOT_NAME,
    fetcher: QuoteFetcher = fetch_quotes_default,
) -> PipelineResult:
    """Run ``ingest_quotes`` then ``build_snapshot`` against the same engine.

    This is the legacy ``worker.py`` entry point — same SQL effects,
    same default sources, same ``--snapshot-name`` default. Splitting
    ingest and snapshot-build into separate transactions matches the
    new trigger semantics: the snapshot rebuild produces one
    ``version_etag`` bump per affected snapshot rather than mingling
    quote upserts with snapshot writes.
    """

    ingest_result = await ingest_quotes(
        engine=engine,
        as_of=as_of,
        start_date=start_date,
        sources=sources,
        fetcher=fetcher,
    )
    snapshot_result = await build_snapshot(engine=engine, as_of=as_of, snapshot_name=snapshot_name)
    return PipelineResult(ingest=ingest_result, snapshot=snapshot_result)


# ---------------------------------------------------------------------------
# Engine plumbing (kept tiny so tests can build their own engine).
# ---------------------------------------------------------------------------


def build_engine(settings: Settings | None = None) -> AsyncEngine:
    """Return an ``md_rw`` async engine ready for the pipeline to write through."""

    return make_md_engine(role="rw", settings=settings)


# ---------------------------------------------------------------------------
# Internals.
# ---------------------------------------------------------------------------


def _normalize_sources(sources: Iterable[str] | None) -> set[str]:
    if sources is None:
        # Bare invocations pull the real-vendor default set only; synthetic
        # is opt-in via an explicit ``--source synthetic``.
        return set(DEFAULT_SOURCES)
    normalized = {s.strip().lower() for s in sources if s.strip()}
    if not normalized:
        msg = "At least one source must be provided"
        raise RuntimeError(msg)
    unknown = normalized.difference(ALL_SOURCES)
    if unknown:
        msg = f"Unsupported source(s): {', '.join(sorted(unknown))}"
        raise RuntimeError(msg)
    return normalized


async def _write_quote_batch(
    engine: AsyncEngine,
    quotes: Sequence[QuoteRecord],
) -> tuple[int, int]:
    upserted = 0
    skipped = 0
    async with engine.begin() as conn:
        for quote in quotes:
            if not validate_canonical_id(quote.canonical_id):
                skipped += 1
                continue
            await upsert_canonical(conn, quote.canonical_id, units=quote.units)
            await upsert_vendor_mapping(conn, quote)
            await upsert_quote(conn, quote)
            upserted += 1
    return upserted, skipped


def _as_utc_datetime(value: date) -> datetime:
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


def _diff_count(
    prior: dict[str, float],
    new: dict[str, float],
) -> int:
    """Count canonical_ids whose snapshot contribution actually changed.

    A canonical_id contributes one to ``quotes_changed`` if it is added,
    removed, or has a different value. First builds (``prior`` empty)
    count every newly written row as changed.
    """

    changed = 0
    seen: set[str] = set()
    for cid, value in new.items():
        seen.add(cid)
        if prior.get(cid) != value:
            changed += 1
    for cid in prior:
        if cid not in seen:
            changed += 1
    return changed
