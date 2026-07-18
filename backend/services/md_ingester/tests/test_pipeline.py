"""Pipeline-shape tests that don't touch a real Postgres.

The expensive end-to-end tests live behind the ``md_ingester_db``
marker (see ``test_db_integration.py``). These cover the input-side
validations that the DB-backed tests would otherwise duplicate:

* source allow-list,
* start-date / as-of ordering,
* the fetcher seam.

All tests are async and run inside the pytest-asyncio default loop;
nesting ``asyncio.run`` inside async fixtures triggers loop-cleanup
``ResourceWarning`` and the workspace pytest config turns warnings
into errors (``filterwarnings = ["error"]``).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_md_ingester.models import QuoteRecord
from quantra_md_ingester.pipeline import (
    _diff_count,
    fetch_quotes_default,
    ingest_quotes,
)


class _UnusedEngine:
    """Stand-in `AsyncEngine` for code paths that must reject inputs early."""

    async def begin(self) -> Any:
        msg = "Pipeline should reject before ever opening a transaction"
        raise AssertionError(msg)


def _engine_stub() -> AsyncEngine:
    return _UnusedEngine()  # type: ignore[return-value]


async def _empty_fetcher(
    sources: set[str],
    as_of: date,
    start: date,
) -> list[QuoteRecord]:
    _ = (sources, as_of, start)
    return []


async def test_ingest_rejects_start_after_as_of() -> None:
    with pytest.raises(RuntimeError, match="cannot be after"):
        await ingest_quotes(
            engine=_engine_stub(),
            as_of=date(2026, 5, 1),
            start_date=date(2026, 5, 10),
            fetcher=_empty_fetcher,
        )


async def test_ingest_rejects_unknown_source() -> None:
    with pytest.raises(RuntimeError, match="Unsupported source"):
        await ingest_quotes(
            engine=_engine_stub(),
            as_of=date(2026, 5, 13),
            sources={"bloomberg"},
            fetcher=_empty_fetcher,
        )


async def test_ingest_rejects_empty_sources_set() -> None:
    with pytest.raises(RuntimeError, match="At least one source"):
        await ingest_quotes(
            engine=_engine_stub(),
            as_of=date(2026, 5, 13),
            sources=set(),
            fetcher=_empty_fetcher,
        )


async def test_fetch_quotes_default_short_circuits_on_empty_source_set() -> None:
    """`fetch_quotes_default` with an empty allowed-set must not call any adapter."""

    out = await fetch_quotes_default(set(), date(2026, 5, 13), date(2026, 5, 13))
    assert out == []


def test_diff_count_first_build_counts_every_new_row() -> None:
    # A first build (prior empty) should report every
    # row as changed so the etag bump is interpretable.
    assert _diff_count({}, {"a": 1.0, "b": 2.0}) == 2


def test_diff_count_unchanged_rows_are_zero() -> None:
    # A daily rebuild that touched zero values
    # still bumps the etag (trigger fires per statement). The
    # ``quotes_changed=0`` signal lets ops disambiguate.
    assert _diff_count({"a": 1.0, "b": 2.0}, {"a": 1.0, "b": 2.0}) == 0


def test_diff_count_mixes_adds_removes_and_value_changes() -> None:
    prior = {"a": 1.0, "b": 2.0, "c": 3.0}
    new = {"a": 1.0, "b": 2.5, "d": 4.0}
    # b changed value, c removed, d added → 3.
    assert _diff_count(prior, new) == 3


async def test_ingest_records_failure_and_does_not_open_transaction() -> None:
    """broken fetcher → ``md_ingester.ingest.failed`` + no DB write.

    Vendor-downtime behavior: "ingester records the failure
    structurally but does NOT corrupt md.quote_points or build a partial
    snapshot; subsequent retry resumes cleanly." This test stubs the
    fetcher to raise (the closest hermetic analogue of a vendor 500 /
    malformed payload) and asserts:

    1. ``ingest_quotes`` propagates the exception (so the cron exit
       code surfaces the failure).
    2. The DB transaction is never opened (``_UnusedEngine.begin``
       asserts) — proves no row in ``md.quote_points`` could have
       been touched.
    3. The structured ``md_ingester.ingest.failed`` event is emitted
       *before* the re-raise so log aggregation sees the failure even
       when the cron exits non-zero.
    """

    class _VendorOutage(RuntimeError):
        pass

    async def _broken_fetcher(
        sources: set[str],
        as_of: date,
        start: date,
    ) -> list[QuoteRecord]:
        _ = (sources, as_of, start)
        msg = "simulated vendor HTTP 500"
        raise _VendorOutage(msg)

    with (
        structlog.testing.capture_logs() as captured,
        pytest.raises(_VendorOutage, match="simulated vendor HTTP 500"),
    ):
        await ingest_quotes(
            engine=_engine_stub(),
            as_of=date(2026, 5, 13),
            sources={"fred"},
            fetcher=_broken_fetcher,
        )

    failure_events = [e for e in captured if e.get("event") == "md_ingester.ingest.failed"]
    assert len(failure_events) == 1, captured
    event = failure_events[0]
    assert event["log_level"] == "error"
    assert event["sources"] == ["fred"]
    assert event["as_of"] == "2026-05-13"
    assert event["error_class"] == "_VendorOutage"
    assert "simulated vendor HTTP 500" in event["error_message"]
    # No completion event — the write path was never entered.
    complete_events = [e for e in captured if e.get("event") == "md_ingester.ingest.complete"]
    assert complete_events == []


def test_quote_record_carries_meta_dict() -> None:
    rec = QuoteRecord(
        canonical_id="USD.RATES.UST.DGS.10Y.YIELD",
        as_of=datetime(2026, 5, 13, tzinfo=UTC),
        value=0.0432,
        source="FRED",
        vendor_id="DGS10",
        meta={"raw_value_percent": 4.32},
    )
    assert rec.meta["raw_value_percent"] == pytest.approx(4.32)
