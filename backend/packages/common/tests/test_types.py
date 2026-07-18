"""Tests for `quantra_common.types`."""

from __future__ import annotations

from datetime import UTC, datetime

from quantra_common.types import ResolvedQuote


def test_resolved_quote_miss() -> None:
    rq = ResolvedQuote(canonical_id="X.Y.Z", found=False)
    assert rq.value is None
    assert not rq.is_exact


def test_resolved_quote_hit_round_trip() -> None:
    rq = ResolvedQuote(
        canonical_id="USD.RATES.UST.DGS.10Y.YIELD",
        requested_as_of=datetime(2026, 5, 14, tzinfo=UTC),
        found=True,
        is_exact=True,
        resolved_as_of=datetime(2026, 5, 14, tzinfo=UTC),
        value=0.041,
        source="treasury.gov",
    )
    assert rq.found
    assert rq.model_dump()["value"] == 0.041
