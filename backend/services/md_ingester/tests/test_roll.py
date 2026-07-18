"""Tests for the ``roll-curve-dates`` logic.

Hermetic: a tiny fake AsyncEngine replays the two SQL round-trips (the
``max(as_of)`` read on ``md.quote_points`` and the marker-scoped UPDATE on
``app.curves``) so no Postgres is touched. These pin:

* the latest date is read per ``series_prefix`` and written to the matching
  curve;
* a target with no quote data is skipped (``latest_date=None``, no UPDATE);
* the UPDATE is owner- + marker-scoped and idempotent (params carried through);
* ``roll_curve_dates`` returns one result per target.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_md_ingester.roll import (
    DEFAULT_ROLL_TARGETS,
    RollTarget,
    roll_curve_dates,
)


@dataclass
class _Recording:
    sql: str
    params: dict[str, Any]
    mode: str


class _FakeMappings:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def one_or_none(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _FakeMappings:
        return _FakeMappings(self._rows)


class _FakeConn:
    def __init__(self, engine: _FakeEngine, mode: str) -> None:
        self._engine = engine
        self._mode = mode

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        sql = str(stmt)
        bound = dict(params or {})
        self._engine.recordings.append(_Recording(sql=sql, params=bound, mode=self._mode))
        return _FakeResult(self._engine.handler(sql, bound))


@dataclass
class _FakeEngine:
    """Replays SQL via a handler; records every statement + its bound params."""

    handler: Any
    recordings: list[_Recording] = field(default_factory=list)

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[_FakeConn]:
        yield _FakeConn(self, "read")

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[_FakeConn]:
        yield _FakeConn(self, "write")


_GBP = RollTarget(
    label="GBP SONIA OIS (BoE)",
    curve_local_id="md-gbp-boe-ois",
    series_prefix="GBP.RATES.BOE.OIS.",
)


def _md_handler(latest: Mapping[str, date | None]) -> Any:
    """Handler returning per-prefix latest dates and echoing UPDATE rows."""

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        s = sql.strip()
        if "max(as_of)" in s:
            prefix = params["prefix"].rstrip("%")
            value = latest.get(prefix)
            return [{"latest_date": value}]
        if s.startswith("UPDATE"):
            # Simulate one matching curve row being bumped.
            return [{"id": "curve-uuid"}]
        return []

    return handler


@pytest.mark.asyncio
async def test_roll_bumps_matching_curve_to_latest_date() -> None:
    latest = {"GBP.RATES.BOE.OIS.": date(2026, 7, 14)}
    engine = _FakeEngine(handler=_md_handler(latest))

    results = await roll_curve_dates(
        app_engine=cast(AsyncEngine, engine),
        md_engine=cast(AsyncEngine, engine),
        owner_uid="dev-user",
        targets=[_GBP],
    )

    assert len(results) == 1
    res = results[0]
    assert res.latest_date == date(2026, 7, 14)
    assert res.curves_updated == 1

    updates = [r for r in engine.recordings if r.mode == "write"]
    assert len(updates) == 1
    up = updates[0]
    assert up.params["ref_date"] == date(2026, 7, 14)
    assert up.params["owner_uid"] == "dev-user"
    assert up.params["local_id"] == "md-gbp-boe-ois"
    # Idempotency + scoping guards must be present in the SQL text.
    assert "reference_date IS DISTINCT FROM :ref_date" in up.sql
    assert "body->>'local_id' = :local_id" in up.sql
    assert "owner_uid = :owner_uid" in up.sql


@pytest.mark.asyncio
async def test_roll_skips_target_with_no_quotes() -> None:
    engine = _FakeEngine(handler=_md_handler({}))  # no prefix has data

    results = await roll_curve_dates(
        app_engine=cast(AsyncEngine, engine),
        md_engine=cast(AsyncEngine, engine),
        owner_uid="dev-user",
        targets=[_GBP],
    )

    assert results[0].latest_date is None
    assert results[0].curves_updated == 0
    # No UPDATE should have been issued.
    assert [r for r in engine.recordings if r.mode == "write"] == []


@pytest.mark.asyncio
async def test_roll_reads_per_prefix_with_like_pattern() -> None:
    latest = {"GBP.RATES.BOE.OIS.": date(2026, 7, 14)}
    engine = _FakeEngine(handler=_md_handler(latest))

    await roll_curve_dates(
        app_engine=cast(AsyncEngine, engine),
        md_engine=cast(AsyncEngine, engine),
        owner_uid="dev-user",
        targets=[_GBP],
    )

    reads = [r for r in engine.recordings if r.mode == "read"]
    assert len(reads) == 1
    assert reads[0].params["prefix"] == "GBP.RATES.BOE.OIS.%"


@pytest.mark.asyncio
async def test_roll_returns_one_result_per_default_target() -> None:
    # Only the GBP prefix has data; the (unseeded) treasury target is a no-op.
    latest = {"GBP.RATES.BOE.OIS.": date(2026, 7, 14)}
    engine = _FakeEngine(handler=_md_handler(latest))

    results = await roll_curve_dates(
        app_engine=cast(AsyncEngine, engine), md_engine=cast(AsyncEngine, engine)
    )

    assert len(results) == len(DEFAULT_ROLL_TARGETS)
    by_marker = {r.curve_local_id: r for r in results}
    assert by_marker["md-gbp-boe-ois"].curves_updated == 1
    assert by_marker["md-usd-treasury"].latest_date is None
    assert by_marker["md-usd-treasury"].curves_updated == 0
