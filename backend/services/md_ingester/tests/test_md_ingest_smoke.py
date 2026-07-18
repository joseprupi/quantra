"""Scheduler-driven smoke against a live ``md_rw`` Postgres pool.

This is the close-out test that proves the ofelia-style scheduled
invocation (``run-pipeline --source fred``) lands rows in
``md.quote_points`` *and* advances ``md.snapshots.version_etag`` through
the trigger. Scope: small CLI smoke;
**mocked vendor** (we substitute the fetcher seam, NOT hit live FRED) so
the test never burns vendor quota or depends on FRED being reachable.

Gated on ``md_ingest_smoke``. Skipped unless
``QUANTRA_MD_INGESTER_TEST_DSN`` is exported (same env-var shape as the
existing ``md_ingester_db`` lane). Cleanup is narrow — every
row this test writes is deleted in reverse-FK order on teardown, so the
dev DB is left as we found it (no blanket TRUNCATE).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from quantra_md_ingester.models import QuoteRecord
from quantra_md_ingester.pipeline import run_pipeline

_DSN_ENV = "QUANTRA_MD_INGESTER_TEST_DSN"
_dsn = os.environ.get(_DSN_ENV)

pytestmark = [
    pytest.mark.md_ingest_smoke,
    pytest.mark.skipif(
        not _dsn,
        reason=(
            f"Set {_DSN_ENV} to a postgres+asyncpg DSN with md_rw access "
            "to run the D135 scheduler-driven smoke."
        ),
    ),
]


@pytest_asyncio.fixture
async def md_rw_smoke_engine() -> AsyncIterator[AsyncEngine]:
    """Async engine bound to ``md_rw`` for the smoke."""

    assert _dsn is not None
    engine = create_async_engine(_dsn, pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_scheduler_driven_pipeline_populates_md_and_advances_etag(
    md_rw_smoke_engine: AsyncEngine,
) -> None:
    """End-to-end scheduler proof.

    1. Invoke ``run_pipeline`` (the CLI's ``run-pipeline`` subcommand
       routes through this same entrypoint) with a fixture fetcher
       that returns deterministic FRED-shape ``QuoteRecord``s for a
       distinct test-only canonical-id namespace.
    2. Assert one ``md.quote_points`` row per fixture entry landed.
    3. Assert the snapshot row exists with a 32+-char ``version_etag``
       and ``quotes_written`` >= the fixture row count (trigger
       fired, etag advanced from the server_default UUID).
    4. Assert structured ``md_ingester.ingest.complete`` +
       ``md_ingester.snapshot.built`` events fire (the observability
       signal the runbook + downstream log aggregation consume).
    5. Narrow cleanup in reverse FK order.
    """

    suffix = uuid.uuid4().hex[:8]
    target_date = date(2026, 5, 27)
    target_dt = datetime(target_date.year, target_date.month, target_date.day, tzinfo=UTC)
    snapshot_name = f"D135_SMOKE_{suffix}"

    # Two distinct canonical_ids in a test-only namespace. Per-tenor
    # values are picked far from common defaults so a misrouted seed
    # is obvious in assertions.
    cid_one = f"USD.RATES.SMOKE.D135.A.{suffix}"
    cid_two = f"USD.RATES.SMOKE.D135.B.{suffix}"
    expected_values = {cid_one: 0.04321, cid_two: 0.05123}
    canonical_ids = list(expected_values)

    def _record(canonical_id: str, value: float) -> QuoteRecord:
        return QuoteRecord(
            canonical_id=canonical_id,
            as_of=target_dt,
            value=value,
            source="FRED",
            vendor_id=f"SMOKE-{canonical_id.rsplit('.', 1)[-1]}",
            quality_flags={},
            meta={
                "series_id": "SMOKE",
                "vendor_unit": "decimal_rate",
                "normalized_unit": "decimal_rate",
                "raw_value_percent": value * 100.0,
                "tenor": "1Y",
            },
        )

    async def _fixture_fetcher(
        sources: set[str],
        as_of: date,
        start: date,
    ) -> list[QuoteRecord]:
        # Mirrors the FRED connector's shape (returns FRED-source records)
        # without touching the network. ``ofelia`` will invoke the live
        # connector under the ``scheduled`` profile; this seam covers the
        # scheduler-wiring class without burning vendor quota.
        _ = (sources, as_of, start)
        return [_record(cid, val) for cid, val in expected_values.items()]

    md_snapshot_id: uuid.UUID | None = None

    try:
        with structlog.testing.capture_logs() as captured:
            result = await run_pipeline(
                engine=md_rw_smoke_engine,
                as_of=target_date,
                sources={"fred"},
                snapshot_name=snapshot_name,
                fetcher=_fixture_fetcher,
            )

        md_snapshot_id = result.snapshot.snapshot_id

        # 1. Ingest counters reflect the fixture.
        assert result.ingest.fetched == len(expected_values)
        assert result.ingest.upserted == len(expected_values)
        assert result.ingest.skipped_invalid_canonical_id == 0

        # 2. ``md.quote_points`` carries one row per fixture entry.
        async with md_rw_smoke_engine.begin() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT canonical_id, value FROM md.quote_points "
                        "WHERE canonical_id = ANY(:ids) "
                        "ORDER BY canonical_id"
                    ),
                    {"ids": canonical_ids},
                )
            ).all()
        observed = {row.canonical_id: float(row.value) for row in rows}
        for cid, expected in expected_values.items():
            assert observed.get(cid) == pytest.approx(expected), (
                f"md.quote_points missing seeded {cid}={expected} (got {observed.get(cid)!r})"
            )

        # 3. ``md.snapshots`` row has a 32-char hex etag (trigger
        # fired during snapshot_quotes write). ``quotes_written`` covers
        # everything in quote_points at ``as_of`` — at least our two.
        assert result.snapshot.quotes_written >= len(expected_values)
        assert result.snapshot.version_etag_after
        # ``gen_random_uuid()::text`` returns a 36-char dashed UUID.
        # Either the dashed form or a packed 32-char hex satisfies the
        # "non-empty fresh etag" contract.
        assert len(result.snapshot.version_etag_after) >= 32, (
            "version_etag_after looks unset / unexpectedly short: "
            f"{result.snapshot.version_etag_after!r}"
        )
        assert result.snapshot.version_etag_after != result.snapshot.version_etag_before

        # 4. Structured events fired — log aggregation will see them.
        events = {e.get("event") for e in captured}
        assert "md_ingester.ingest.complete" in events
        assert "md_ingester.snapshot.built" in events
        # No failure event under happy path.
        assert "md_ingester.ingest.failed" not in events

    finally:
        # 5. Narrow cleanup, reverse FK order. The snapshot row CASCADE-
        # deletes md.snapshot_quotes via the schema FK; canonical_ids
        # depend on vendor_mappings which we also wrote (the ingester
        # writes a per-source mapping). Delete in dependency order.
        async with md_rw_smoke_engine.begin() as conn:
            if md_snapshot_id is not None:
                await conn.execute(
                    text("DELETE FROM md.snapshots WHERE id = :id"),
                    {"id": md_snapshot_id},
                )
            await conn.execute(
                text("DELETE FROM md.quote_points WHERE canonical_id = ANY(:ids)"),
                {"ids": canonical_ids},
            )
            await conn.execute(
                text("DELETE FROM md.vendor_mappings WHERE canonical_id = ANY(:ids)"),
                {"ids": canonical_ids},
            )
            await conn.execute(
                text("DELETE FROM md.canonical_ids WHERE canonical_id = ANY(:ids)"),
                {"ids": canonical_ids},
            )


async def test_scheduler_driven_pipeline_propagates_vendor_failure(
    md_rw_smoke_engine: AsyncEngine,
) -> None:
    """Failure mode — broken fetcher leaves md.* untouched.

    Vendor-downtime behavior exercised end-to-end against the
    live md_rw pool. A raising fetcher is the hermetic analogue of a
    vendor returning HTTP 500 / malformed body; we assert:

    1. ``run_pipeline`` propagates the exception (cron exit non-zero).
    2. ``md.quote_points`` has no rows for the test namespace (no
       partial corruption).
    3. ``md.snapshots`` for this snapshot_name has no row (no partial
       snapshot built — the build-snapshot step is never reached
       because ``ingest_quotes`` raised first).
    4. Structured ``md_ingester.ingest.failed`` event emitted before
       the re-raise.
    """

    suffix = uuid.uuid4().hex[:8]
    target_date = date(2026, 5, 27)
    snapshot_name = f"D135_BROKEN_{suffix}"
    cid = f"USD.RATES.SMOKE.D135.BROKEN.{suffix}"

    class _VendorOutage(RuntimeError):
        pass

    async def _broken_fetcher(
        sources: set[str],
        as_of: date,
        start: date,
    ) -> list[QuoteRecord]:
        _ = (sources, as_of, start)
        msg = "simulated FRED HTTP 500"
        raise _VendorOutage(msg)

    try:
        with (
            structlog.testing.capture_logs() as captured,
            pytest.raises(_VendorOutage, match="simulated FRED HTTP 500"),
        ):
            await run_pipeline(
                engine=md_rw_smoke_engine,
                as_of=target_date,
                sources={"fred"},
                snapshot_name=snapshot_name,
                fetcher=_broken_fetcher,
            )

        # Failure event emitted before the re-raise.
        failure_events = [e for e in captured if e.get("event") == "md_ingester.ingest.failed"]
        assert len(failure_events) == 1, captured
        assert failure_events[0]["sources"] == ["fred"]
        assert failure_events[0]["error_class"] == "_VendorOutage"

        # build-snapshot step was never entered.
        built_events = [e for e in captured if e.get("event") == "md_ingester.snapshot.built"]
        assert built_events == []

        # No quote_points rows for this namespace — no corruption.
        async with md_rw_smoke_engine.begin() as conn:
            quote_rows = (
                await conn.execute(
                    text("SELECT COUNT(*) FROM md.quote_points WHERE canonical_id = :cid"),
                    {"cid": cid},
                )
            ).scalar_one()
            snapshot_rows = (
                await conn.execute(
                    text("SELECT COUNT(*) FROM md.snapshots WHERE name = :name"),
                    {"name": snapshot_name},
                )
            ).scalar_one()
        assert quote_rows == 0
        assert snapshot_rows == 0

    finally:
        # Defensive cleanup — should be a no-op given the failure path
        # never wrote anything, but kept symmetric with the happy-path
        # test in case a future regression accidentally writes.
        async with md_rw_smoke_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM md.snapshots WHERE name = :name"),
                {"name": snapshot_name},
            )
            await conn.execute(
                text("DELETE FROM md.quote_points WHERE canonical_id = :cid"),
                {"cid": cid},
            )
            await conn.execute(
                text("DELETE FROM md.vendor_mappings WHERE canonical_id = :cid"),
                {"cid": cid},
            )
            await conn.execute(
                text("DELETE FROM md.canonical_ids WHERE canonical_id = :cid"),
                {"cid": cid},
            )
