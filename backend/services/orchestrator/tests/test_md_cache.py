"""Unit tests for ``TtlBoundedQuoteCache``.

Covers the orchestrator-side cache wrapper without going through the
HTTP layer:

* TTL eviction surfaces an entry as a miss after ``ttl_s`` seconds.
* LRU eviction kicks in at ``max_entries`` and drops the
  least-recently-used entry first.
* Stats counters track hits / misses / expirations / size.
* Construction rejects non-positive bounds (defensive).

The wrapper is the only orchestrator-owned cache implementation.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from quantra_common.types import ResolvedQuote
from quantra_orchestrator.md import QuoteCacheStats, TtlBoundedQuoteCache


def _quote(cid: str, value: float = 1.0) -> ResolvedQuote:
    return ResolvedQuote(
        canonical_id=cid,
        requested_as_of=datetime(2026, 5, 13),
        found=True,
        is_exact=True,
        resolved_as_of=datetime(2026, 5, 13),
        value=value,
        source="test",
    )


class _ManualClock:
    """Monotonic-clock substitute the cache calls instead of time.monotonic."""

    def __init__(self, *, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.parametrize("bad_max", [0, -1])
def test_constructor_rejects_non_positive_max_entries(bad_max: int) -> None:
    with pytest.raises(ValueError, match="max_entries"):
        TtlBoundedQuoteCache(max_entries=bad_max, ttl_s=10.0)


@pytest.mark.parametrize("bad_ttl", [0.0, -1.0])
def test_constructor_rejects_non_positive_ttl(bad_ttl: float) -> None:
    with pytest.raises(ValueError, match="ttl_s"):
        TtlBoundedQuoteCache(max_entries=10, ttl_s=bad_ttl)


async def test_cache_returns_none_for_unknown_key() -> None:
    cache = TtlBoundedQuoteCache(max_entries=4, ttl_s=10.0)
    assert await cache.get("missing") is None
    assert cache.stats() == QuoteCacheStats(
        hits=0, misses=1, expirations=0, size=0, max_entries=4, ttl_s=10.0
    )


async def test_cache_hit_advances_hit_counter() -> None:
    cache = TtlBoundedQuoteCache(max_entries=4, ttl_s=10.0)
    await cache.put("k", _quote("k"))
    assert (await cache.get("k")) is not None
    assert cache.stats().hits == 1
    assert cache.stats().misses == 0
    assert cache.stats().size == 1


async def test_cache_evicts_after_ttl_expires() -> None:
    clock = _ManualClock(start=1000.0)
    cache = TtlBoundedQuoteCache(max_entries=4, ttl_s=5.0, clock=clock)
    await cache.put("k", _quote("k"))
    clock.advance(4.99)
    assert (await cache.get("k")) is not None
    clock.advance(0.02)
    # Now we're past 5.01s since insertion → miss + expiration counter.
    assert (await cache.get("k")) is None
    stats = cache.stats()
    assert stats.expirations == 1
    assert stats.misses == 1
    # Stale entry was dropped from the store.
    assert stats.size == 0


async def test_cache_lru_evicts_oldest_on_overflow() -> None:
    cache = TtlBoundedQuoteCache(max_entries=2, ttl_s=60.0)
    await cache.put("a", _quote("a"))
    await cache.put("b", _quote("b"))
    # Touch "a" so "b" becomes the LRU.
    assert (await cache.get("a")) is not None
    await cache.put("c", _quote("c"))

    # "b" should have been evicted; "a" + "c" survive.
    assert (await cache.get("a")) is not None
    assert (await cache.get("c")) is not None
    assert (await cache.get("b")) is None
    assert cache.stats().size == 2


async def test_put_refreshes_inserted_at_for_existing_key() -> None:
    """Re-putting a key resets its TTL clock so it survives the next window."""

    clock = _ManualClock(start=1000.0)
    cache = TtlBoundedQuoteCache(max_entries=2, ttl_s=5.0, clock=clock)
    await cache.put("k", _quote("k", value=1.0))
    clock.advance(4.0)
    await cache.put("k", _quote("k", value=2.0))  # refreshes inserted_at
    clock.advance(4.0)
    # Total elapsed since first put = 8s, but only 4s since the refresh.
    fetched = await cache.get("k")
    assert fetched is not None
    assert fetched.value == 2.0


async def test_clear_drops_entries_and_keeps_counters() -> None:
    cache = TtlBoundedQuoteCache(max_entries=4, ttl_s=10.0)
    await cache.put("k", _quote("k"))
    await cache.get("k")
    await cache.clear()
    assert cache.stats().size == 0
    # ``clear`` drops the contents but the historical counters stay so
    # ops can keep tracking aggregate effectiveness.
    assert cache.stats().hits == 1


async def test_reset_stats_zeros_counters_keeps_contents() -> None:
    cache = TtlBoundedQuoteCache(max_entries=4, ttl_s=10.0)
    await cache.put("k", _quote("k"))
    await cache.get("k")
    await cache.get("missing")
    cache.reset_stats()
    stats = cache.stats()
    assert stats.hits == 0
    assert stats.misses == 0
    assert stats.expirations == 0
    assert stats.size == 1
    # Cache contents preserved.
    assert (await cache.get("k")) is not None
