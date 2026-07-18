"""Unit tests for the user-import parser + shared upsert.

Hermetic: :func:`parse_csv` is pure, and :func:`import_user_quotes` is
exercised against a tiny in-memory fake connection that records the SQL it
would run, so no Postgres is needed. The live idempotency proof runs in the
isolated self-hosted bundle.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_md_ingester.user_import import (
    SOURCE_CSV,
    SOURCE_MANUAL,
    UserQuoteInput,
    import_user_quotes,
    parse_csv,
)

# ---------------------------------------------------------------------------
# CSV connector / validator (parse_csv)
# ---------------------------------------------------------------------------


def test_parse_csv_happy_minimal_and_full_rows() -> None:
    data = (
        "USD.IRS.5Y,2025-01-15,0.031\n"
        "USD.SOFR.2Y,2025-01-15,0.0295,mydesk\n"
        'EUR.HICP.5Y,2025-01-15,0.022,mydesk,{"note":"cpi"}\n'
    )
    quotes, errors = parse_csv(data)
    assert errors == []
    assert [q.row for q in quotes] == [1, 2, 3]
    assert quotes[0].canonical_id == "USD.IRS.5Y"
    assert quotes[0].value == pytest.approx(0.031)
    assert quotes[0].source is None
    assert quotes[1].source == "mydesk"
    assert quotes[2].meta == {"note": "cpi"}


def test_parse_csv_header_row_is_detected_and_skipped() -> None:
    data = "canonical_id,as_of,value,source,meta_json\nUSD.IRS.5Y,2025-01-15,0.031\n"
    quotes, errors = parse_csv(data)
    assert errors == []
    assert len(quotes) == 1
    # Data row numbering starts at 1 even though a header preceded it.
    assert quotes[0].row == 1


def test_parse_csv_blank_lines_ignored() -> None:
    data = "\nUSD.IRS.5Y,2025-01-15,0.031\n\n"
    quotes, errors = parse_csv(data)
    assert errors == []
    assert len(quotes) == 1
    assert quotes[0].row == 1


def test_parse_csv_bad_rows_reported_not_raised() -> None:
    data = (
        "USD.IRS.5Y,2025-01-15,0.031\n"  # row 1 ok
        "USD.IRS.2Y,2025-01-15\n"  # row 2 too few columns
        "USD.IRS.7Y,2025-01-15,not-a-number\n"  # row 3 bad value
        "USD.IRS.10Y,2025-01-15,0.04,desk,notjson\n"  # row 4 bad meta json
        'USD.IRS.30Y,2025-01-15,0.05,desk,["a"]\n'  # row 5 meta not an object
    )
    quotes, errors = parse_csv(data)
    assert [q.row for q in quotes] == [1]
    bad_rows = {e.row for e in errors}
    assert bad_rows == {2, 3, 4, 5}
    reasons = {e.row: e.reason for e in errors}
    assert "columns" in reasons[2]
    assert "not a number" in reasons[3]
    assert "JSON object" in reasons[4]
    assert "JSON object" in reasons[5]


# ---------------------------------------------------------------------------
# Shared upsert (import_user_quotes) against a recording fake connection
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _FakeResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _RecordingConn:
    def __init__(self, calls: list[tuple[str, dict[str, Any]]], existing: set[str]) -> None:
        self._calls = calls
        self._existing = existing

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        sql = str(stmt)
        bound = dict(params or {})
        self._calls.append((sql, bound))
        if "SELECT canonical_id FROM canonical_ids WHERE canonical_id = ANY" in sql:
            ids = bound.get("ids", [])
            return _FakeResult([{"canonical_id": c} for c in ids if c in self._existing])
        return _FakeResult([])


class _RecordingEngine:
    """Minimal ``AsyncEngine.begin()`` stand-in that records executes.

    ``existing`` is the set of ``canonical_id`` values the fake reports as
    already defined in ``md.canonical_ids`` (the existence probe the import
    path now runs before adding values).
    """

    def __init__(self, existing: set[str] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.existing = existing if existing is not None else set()

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[_RecordingConn]:
        yield _RecordingConn(self.calls, self.existing)


def _as_engine(engine: _RecordingEngine) -> AsyncEngine:
    """Present the recording fake at the ``import_user_quotes`` boundary."""

    return cast(AsyncEngine, engine)


async def test_import_user_quotes_upserts_valid_rows_with_provenance() -> None:
    engine = _RecordingEngine(existing={"USD.IRS.5Y", "USD.SOFR.2Y"})
    quotes = [
        UserQuoteInput(row=1, canonical_id="USD.IRS.5Y", as_of="2025-01-15", value=0.031),
        UserQuoteInput(
            row=2,
            canonical_id="USD.SOFR.2Y",
            as_of="2025-01-15",
            value=0.0295,
            source="mydesk",
        ),
    ]
    report = await import_user_quotes(_as_engine(engine), quotes, default_source=SOURCE_CSV)
    assert report.imported == 2
    assert report.skipped == 0
    assert report.errors == []
    # Import ADDS VALUES only: no canonical_ids row is ever inserted/upserted.
    assert not any("INTO canonical_ids" in sql for sql, _ in engine.calls)
    quote_params = [p for sql, p in engine.calls if "quote_points" in sql]
    assert len(quote_params) == 2
    # Row 1 falls back to the default provenance tag; row 2 overrides it.
    by_id = {p["canonical_id"]: p for p in quote_params}
    assert by_id["USD.IRS.5Y"]["source"] == SOURCE_CSV
    assert by_id["USD.SOFR.2Y"]["source"] == "mydesk"
    assert '"user_import": true' in by_id["USD.IRS.5Y"]["meta"]


async def test_import_user_quotes_rejects_value_for_unknown_series() -> None:
    # USD.IRS.5Y exists; USD.MYDESK.7Y is a valid id but has no series row yet.
    engine = _RecordingEngine(existing={"USD.IRS.5Y"})
    quotes = [
        UserQuoteInput(row=1, canonical_id="USD.IRS.5Y", as_of="2025-01-15", value=0.031),
        UserQuoteInput(row=2, canonical_id="USD.MYDESK.7Y", as_of="2025-01-15", value=0.05),
    ]
    report = await import_user_quotes(_as_engine(engine), quotes, default_source=SOURCE_MANUAL)
    assert report.imported == 1
    assert report.skipped == 1
    assert report.errors[0].row == 2
    assert "does not exist" in report.errors[0].reason
    # The unknown series was NOT auto-created and got NO quote row.
    assert not any("INTO canonical_ids" in sql for sql, _ in engine.calls)
    quote_ids = [p["canonical_id"] for sql, p in engine.calls if "quote_points" in sql]
    assert quote_ids == ["USD.IRS.5Y"]


async def test_import_user_quotes_reports_invalid_canonical_id() -> None:
    engine = _RecordingEngine(existing={"USD.IRS.5Y"})
    quotes = [
        UserQuoteInput(row=1, canonical_id="not a canonical id", as_of="2025-01-15", value=1.0),
        UserQuoteInput(row=2, canonical_id="USD.IRS.5Y", as_of="2025-01-15", value=0.031),
    ]
    report = await import_user_quotes(_as_engine(engine), quotes, default_source=SOURCE_MANUAL)
    assert report.imported == 1
    assert report.skipped == 1
    assert report.errors[0].row == 1
    assert "invalid canonical_id" in report.errors[0].reason


async def test_import_user_quotes_reports_bad_date_and_dupe_key() -> None:
    engine = _RecordingEngine(existing={"USD.IRS.5Y"})
    quotes = [
        UserQuoteInput(row=1, canonical_id="USD.IRS.5Y", as_of="15-01-2025", value=1.0),
        UserQuoteInput(row=2, canonical_id="USD.IRS.5Y", as_of="2025-01-15", value=0.031),
        UserQuoteInput(row=3, canonical_id="USD.IRS.5Y", as_of="2025-01-15", value=0.032),
    ]
    report = await import_user_quotes(_as_engine(engine), quotes, default_source=SOURCE_MANUAL)
    assert report.imported == 1
    assert report.skipped == 2
    reasons = {e.row: e.reason for e in report.errors}
    assert "YYYY-MM-DD" in reasons[1]
    assert "duplicate key" in reasons[3]


async def test_import_user_quotes_empty_batch_opens_no_transaction() -> None:
    engine = _RecordingEngine()
    report = await import_user_quotes(_as_engine(engine), [], default_source=SOURCE_CSV)
    assert report.imported == 0
    assert report.skipped == 0
    assert engine.calls == []
