"""DB-backed integration tests for the lifted MD ingester.

Skipped by default. To run locally:

1. Boot the monorepo Postgres (``docker compose up -d postgres``) and
   apply migrations once::

       uv run alembic -n app upgrade head
       uv run alembic -n md  upgrade head

2. Export the two DSNs the fixtures need::

       export QUANTRA_MD_INGESTER_TEST_DSN=\\
           postgresql+asyncpg://md_rw:md_rw@localhost:5434/quantra
       export QUANTRA_MD_INGESTER_TEST_ADMIN_DSN=\\
           postgresql+asyncpg://quantra:quantra@localhost:5434/quantra

3. Run the marker::

       uv run pytest -m md_ingester_db services/md_ingester

The fixture truncates the ingester-owned ``md.*`` tables in
dependency order between tests (spirit). It never touches
``app.*``; if a test attempted an ``app.*`` write the ``md_rw`` role
would lack USAGE on the schema and the statement would fail before
any rows were inserted.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_md_ingester.models import QuoteRecord
from quantra_md_ingester.pipeline import (
    build_snapshot,
    ingest_quotes,
    run_pipeline,
)

pytestmark = pytest.mark.md_ingester_db


def _stub_fred_record(as_of: date, value_percent: float = 4.32) -> QuoteRecord:
    return QuoteRecord(
        canonical_id="USD.RATES.UST.DGS.10Y.YIELD",
        as_of=datetime(as_of.year, as_of.month, as_of.day, tzinfo=UTC),
        value=value_percent / 100.0,
        source="FRED",
        vendor_id="DGS10",
        meta={
            "series_id": "DGS10",
            "raw_value_percent": value_percent,
            "vendor_unit": "percent",
            "normalized_unit": "decimal_rate",
            "tenor": "10Y",
        },
    )


async def test_ingest_writes_canonical_vendor_and_quote(
    md_rw_engine: AsyncEngine,
    clean_md_schema: None,
) -> None:
    as_of = date(2026, 5, 13)

    async def _fetcher(
        sources: set[str],
        as_of_arg: date,
        start: date,
    ) -> list[QuoteRecord]:
        _ = (sources, as_of_arg, start)
        return [_stub_fred_record(as_of)]

    result = await ingest_quotes(
        engine=md_rw_engine,
        as_of=as_of,
        sources={"fred"},
        fetcher=_fetcher,
    )
    assert result.fetched == 1
    assert result.upserted == 1
    assert result.skipped_invalid_canonical_id == 0

    async with md_rw_engine.begin() as conn:
        canon = (
            await conn.execute(
                text(
                    "SELECT canonical_id, asset_class, currency, tenor, field "
                    "FROM canonical_ids "
                    "WHERE canonical_id = :cid"
                ),
                {"cid": "USD.RATES.UST.DGS.10Y.YIELD"},
            )
        ).first()
        assert canon is not None
        assert canon.asset_class == "RATES"
        assert canon.currency == "USD"
        assert canon.tenor == "10Y"
        assert canon.field == "YIELD"

        mapping = (
            await conn.execute(
                text(
                    "SELECT vendor, vendor_id, canonical_id FROM vendor_mappings "
                    "WHERE vendor = 'FRED' AND vendor_id = 'DGS10'"
                )
            )
        ).first()
        assert mapping is not None
        assert mapping.canonical_id == "USD.RATES.UST.DGS.10Y.YIELD"

        quote = (
            await conn.execute(
                text(
                    "SELECT value, source, vendor_id, raw_value, ingested_at "
                    "FROM quote_points "
                    "WHERE canonical_id = :cid AND as_of = :as_of"
                ),
                {
                    "cid": "USD.RATES.UST.DGS.10Y.YIELD",
                    "as_of": datetime(2026, 5, 13, tzinfo=UTC),
                },
            )
        ).first()
        assert quote is not None
        assert quote.value == pytest.approx(0.0432)
        assert quote.source == "FRED"
        assert quote.vendor_id == "DGS10"
        assert quote.raw_value == pytest.approx(4.32)
        assert quote.ingested_at is not None


async def test_ingest_skips_invalid_canonical_ids(
    md_rw_engine: AsyncEngine,
    clean_md_schema: None,
) -> None:
    as_of = date(2026, 5, 13)

    bad = QuoteRecord(
        canonical_id="garbage",
        as_of=datetime(as_of.year, as_of.month, as_of.day, tzinfo=UTC),
        value=0.01,
        source="FRED",
        vendor_id="BAD",
        meta={},
    )

    async def _fetcher(
        sources: set[str],
        as_of_arg: date,
        start: date,
    ) -> list[QuoteRecord]:
        _ = (sources, as_of_arg, start)
        return [bad, _stub_fred_record(as_of)]

    result = await ingest_quotes(
        engine=md_rw_engine,
        as_of=as_of,
        sources={"fred"},
        fetcher=_fetcher,
    )
    assert result.fetched == 2
    assert result.upserted == 1
    assert result.skipped_invalid_canonical_id == 1


async def test_build_snapshot_advances_version_etag(
    md_rw_engine: AsyncEngine,
    clean_md_schema: None,
) -> None:
    """The trigger must roll ``version_etag`` on snapshot_quotes rebuilds.

    Strategy: ingest one quote, build the snapshot, then ingest an
    out-of-order correction for the *same* (name, as_of) window and
    rebuild. The ``uq_snapshots_name_as_of`` unique constraint forces
    the second build's ``ON CONFLICT`` branch, so we observe the
    in-place rebuild semantics and the etag bump from the trigger.

    The snapshot builder issues a ``DELETE`` followed by an
    ``INSERT`` into ``snapshot_quotes``; either statement firing the
    STATEMENT-level ``md.bump_snapshot_etag`` trigger is enough — we
    only require the etag to differ across rebuilds.
    """

    target_as_of = date(2026, 5, 13)

    async def _fetcher_one(
        sources: set[str],
        as_of_arg: date,
        start: date,
    ) -> list[QuoteRecord]:
        _ = (sources, as_of_arg, start)
        return [_stub_fred_record(target_as_of, value_percent=4.30)]

    await ingest_quotes(
        engine=md_rw_engine,
        as_of=target_as_of,
        sources={"fred"},
        fetcher=_fetcher_one,
    )

    first = await build_snapshot(
        engine=md_rw_engine,
        as_of=target_as_of,
        snapshot_name="MD_INGESTER_TEST",
    )
    assert first.quotes_written == 1
    # First build with an empty prior must report every written row as
    # changed (rebuild telemetry).
    assert first.quotes_changed == 1
    assert first.version_etag_before is None
    assert first.version_etag_after
    assert len(first.version_etag_after) >= 32

    # Corrected quote for the same as_of overwrites the existing
    # quote_points row (upsert) — the in-place behavior the
    # legacy worker has always relied on.
    async def _fetcher_two(
        sources: set[str],
        as_of_arg: date,
        start: date,
    ) -> list[QuoteRecord]:
        _ = (sources, as_of_arg, start)
        return [_stub_fred_record(target_as_of, value_percent=4.35)]

    await ingest_quotes(
        engine=md_rw_engine,
        as_of=target_as_of,
        sources={"fred"},
        fetcher=_fetcher_two,
    )

    second = await build_snapshot(
        engine=md_rw_engine,
        as_of=target_as_of,
        snapshot_name="MD_INGESTER_TEST",
    )
    assert second.snapshot_id == first.snapshot_id, (
        "Same (name, as_of) must reuse the existing snapshot row through ON CONFLICT"
    )
    assert second.version_etag_before == first.version_etag_after, (
        "Pre-rebuild etag must equal the previous rebuild's post-etag"
    )
    assert second.version_etag_after != first.version_etag_after, (
        "D40 trigger should have bumped version_etag on snapshot_quotes rebuild"
    )
    # The corrected value (4.30 -> 4.35) means one real-content change.
    assert second.quotes_changed == 1


async def test_quote_points_upserts_in_place(
    md_rw_engine: AsyncEngine,
    clean_md_schema: None,
) -> None:
    """Late corrections should overwrite the row, not append a second one."""

    as_of = date(2026, 5, 13)

    async def _fetcher_first(
        sources: set[str],
        as_of_arg: date,
        start: date,
    ) -> list[QuoteRecord]:
        _ = (sources, as_of_arg, start)
        return [_stub_fred_record(as_of, value_percent=4.20)]

    async def _fetcher_correction(
        sources: set[str],
        as_of_arg: date,
        start: date,
    ) -> list[QuoteRecord]:
        _ = (sources, as_of_arg, start)
        return [_stub_fred_record(as_of, value_percent=4.32)]

    await ingest_quotes(
        engine=md_rw_engine,
        as_of=as_of,
        sources={"fred"},
        fetcher=_fetcher_first,
    )
    await ingest_quotes(
        engine=md_rw_engine,
        as_of=as_of,
        sources={"fred"},
        fetcher=_fetcher_correction,
    )

    async with md_rw_engine.begin() as conn:
        rows = (
            await conn.execute(
                text("SELECT value, raw_value FROM quote_points WHERE canonical_id = :cid"),
                {"cid": "USD.RATES.UST.DGS.10Y.YIELD"},
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].value == pytest.approx(0.0432)
        assert rows[0].raw_value == pytest.approx(4.32)


async def test_run_pipeline_writes_and_snapshots(
    md_rw_engine: AsyncEngine,
    clean_md_schema: None,
) -> None:
    async def _fetcher(
        sources: set[str],
        as_of_arg: date,
        start: date,
    ) -> list[QuoteRecord]:
        _ = (sources, as_of_arg, start)
        return [
            _stub_fred_record(date(2026, 5, 13), value_percent=4.32),
            QuoteRecord(
                canonical_id="USD.RATES.UST.DGS.2Y.YIELD",
                as_of=datetime(2026, 5, 13, tzinfo=UTC),
                value=0.0428,
                source="FRED",
                vendor_id="DGS2",
                meta={"series_id": "DGS2", "raw_value_percent": 4.28},
            ),
        ]

    result = await run_pipeline(
        engine=md_rw_engine,
        as_of=date(2026, 5, 13),
        sources={"fred"},
        snapshot_name="MD_INGESTER_RUN_PIPELINE_TEST",
        fetcher=_fetcher,
    )
    assert result.ingest.upserted == 2
    assert result.snapshot.quotes_written == 2
    assert result.snapshot.version_etag_after


async def test_reingest_preserves_user_edited_description(
    md_rw_engine: AsyncEngine,
    clean_md_schema: None,
) -> None:
    """A nightly ingest tick must NOT clobber a user-edited series description.

    Reproduces the production bug: the canonical-ids upsert did
    ``DO UPDATE SET description = EXCLUDED.description`` on every
    ingested quote, so a Quote Book description edit (PATCH
    ``/v1/market-data/series``) was silently reverted by the next tick.
    """

    as_of = date(2026, 5, 13)

    async def _fetcher(
        sources: set[str],
        as_of_arg: date,
        start: date,
    ) -> list[QuoteRecord]:
        _ = (sources, as_of_arg, start)
        return [_stub_fred_record(as_of)]

    await ingest_quotes(engine=md_rw_engine, as_of=as_of, sources={"fred"}, fetcher=_fetcher)

    # Simulate the user's PATCH /v1/market-data/series description edit.
    async with md_rw_engine.begin() as conn:
        await conn.execute(
            text("UPDATE canonical_ids SET description = :d WHERE canonical_id = :cid"),
            {"d": "My 10Y benchmark (user note)", "cid": "USD.RATES.UST.DGS.10Y.YIELD"},
        )

    # Next nightly tick for the same series.
    await ingest_quotes(engine=md_rw_engine, as_of=as_of, sources={"fred"}, fetcher=_fetcher)

    async with md_rw_engine.begin() as conn:
        description = (
            await conn.execute(
                text("SELECT description FROM canonical_ids WHERE canonical_id = :cid"),
                {"cid": "USD.RATES.UST.DGS.10Y.YIELD"},
            )
        ).scalar_one()
    assert description == "My 10Y benchmark (user note)"

    # But an emptied-out description IS refilled with the auto label.
    async with md_rw_engine.begin() as conn:
        await conn.execute(
            text("UPDATE canonical_ids SET description = '' WHERE canonical_id = :cid"),
            {"cid": "USD.RATES.UST.DGS.10Y.YIELD"},
        )
    await ingest_quotes(engine=md_rw_engine, as_of=as_of, sources={"fred"}, fetcher=_fetcher)
    async with md_rw_engine.begin() as conn:
        description = (
            await conn.execute(
                text("SELECT description FROM canonical_ids WHERE canonical_id = :cid"),
                {"cid": "USD.RATES.UST.DGS.10Y.YIELD"},
            )
        ).scalar_one()
    assert description
    assert description != ""


async def test_ingest_stores_honest_units_for_level_series(
    md_rw_engine: AsyncEngine,
    clean_md_schema: None,
) -> None:
    """A level series (VIX/CPI) must land with units='level', not decimal_rate."""

    as_of = date(2026, 5, 13)
    level_record = QuoteRecord(
        canonical_id="USD.VOL.EQ.VIXCLS.1D.LEVEL",
        as_of=datetime(as_of.year, as_of.month, as_of.day, tzinfo=UTC),
        value=16.7,
        source="FRED",
        vendor_id="VIXCLS",
        meta={"series_id": "VIXCLS", "vendor_unit": "level", "normalized_unit": "level"},
        units="level",
    )

    async def _fetcher(
        sources: set[str],
        as_of_arg: date,
        start: date,
    ) -> list[QuoteRecord]:
        _ = (sources, as_of_arg, start)
        return [level_record, _stub_fred_record(as_of)]

    await ingest_quotes(engine=md_rw_engine, as_of=as_of, sources={"fred"}, fetcher=_fetcher)

    async with md_rw_engine.begin() as conn:
        quote = (
            await conn.execute(
                text("SELECT value, units FROM quote_points WHERE canonical_id = :cid"),
                {"cid": "USD.VOL.EQ.VIXCLS.1D.LEVEL"},
            )
        ).first()
        assert quote is not None
        assert quote.value == pytest.approx(16.7)
        assert quote.units == "level"

        canon_units = (
            await conn.execute(
                text("SELECT units FROM canonical_ids WHERE canonical_id = :cid"),
                {"cid": "USD.VOL.EQ.VIXCLS.1D.LEVEL"},
            )
        ).scalar_one()
        assert canon_units == "level"

        rate = (
            await conn.execute(
                text("SELECT value, units FROM quote_points WHERE canonical_id = :cid"),
                {"cid": "USD.RATES.UST.DGS.10Y.YIELD"},
            )
        ).first()
        assert rate is not None
        assert rate.units == "decimal_rate"


async def test_md_rw_cannot_write_to_app_schema(
    md_rw_engine: AsyncEngine,
    clean_md_schema: None,
) -> None:
    """Belt-and-braces check on role isolation.

    The ingester's role is granted access to ``md.*`` only. An INSERT
    targeting ``app.*`` must fail with a permission error before the
    statement runs. The exact SQLSTATE varies; we only require that
    *some* exception is raised — passing the line below would
    indicate D6 isolation has regressed.
    """

    expected = (DBAPIError, ProgrammingError)
    with pytest.raises(expected):
        async with md_rw_engine.begin() as conn:
            await conn.execute(text("INSERT INTO app.users (uid) VALUES ('rogue')"))
