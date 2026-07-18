"""TTL-bounded LRU cache used by the orchestrator's MD client wiring.

The orchestrator's resolved-quote cache implements the
:class:`quantra_common.md_client.QuoteCache` Protocol and adds two
behaviors on top of a plain LRU:

1. **Per-entry TTL.** Entries expire after ``ttl_s`` seconds and are
   surfaced as cache misses on the next ``get``. The default 5-minute
   TTL comes from ``OrchestratorSettings.md_cache_ttl_s``.
2. **Hit / miss accounting.** A small set of counters feeds
   ``GET /debug/md/cache/stats`` so operators can observe cache
   effectiveness without process introspection.

This wrapper keeps its own ``OrderedDict`` so the LRU position and the
inserted-at timestamp stay in lock-step under one lock-free
single-threaded async lifecycle.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from quantra_common.types import ResolvedQuote


@dataclass(frozen=True, slots=True)
class QuoteCacheStats:
    """Read-only snapshot of cache counters returned by ``/debug/md/cache/stats``."""

    hits: int
    misses: int
    expirations: int
    size: int
    max_entries: int
    ttl_s: float


@dataclass(slots=True)
class _Entry:
    """One cached resolved quote with the wall-clock instant we cached it."""

    value: ResolvedQuote
    inserted_at: float


class TtlBoundedQuoteCache:
    """Bounded LRU + TTL cache satisfying the ``QuoteCache`` Protocol.

    Single-process, async-safe under Quantra's "one event loop per
    service" model (distributed caches are deliberately out of scope).
    Eviction order: explicit ``ttl_s`` expiry on read, then LRU eviction
    on write when ``max_entries`` is exceeded.
    """

    def __init__(
        self,
        *,
        max_entries: int,
        ttl_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries <= 0:
            msg = "max_entries must be > 0"
            raise ValueError(msg)
        if ttl_s <= 0:
            msg = "ttl_s must be > 0"
            raise ValueError(msg)
        self._max_entries = max_entries
        self._ttl_s = ttl_s
        self._clock = clock
        self._store: OrderedDict[str, _Entry] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._expirations = 0

    async def get(self, key: str) -> ResolvedQuote | None:
        entry = self._store.get(key)
        if entry is None:
            self._misses += 1
            return None
        if (self._clock() - entry.inserted_at) > self._ttl_s:
            # Drop the stale entry so the next ``put`` refreshes both
            # the LRU position and the timestamp. Counted as a miss
            # *and* an expiration so ops can tell churn from cold
            # start.
            del self._store[key]
            self._misses += 1
            self._expirations += 1
            return None
        self._store.move_to_end(key)
        self._hits += 1
        return entry.value

    async def put(self, key: str, value: ResolvedQuote) -> None:
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = _Entry(value=value, inserted_at=self._clock())
        while len(self._store) > self._max_entries:
            self._store.popitem(last=False)

    async def clear(self) -> None:
        self._store.clear()

    def stats(self) -> QuoteCacheStats:
        """Snapshot of counters; cheap, read-only."""

        return QuoteCacheStats(
            hits=self._hits,
            misses=self._misses,
            expirations=self._expirations,
            size=len(self._store),
            max_entries=self._max_entries,
            ttl_s=self._ttl_s,
        )

    def reset_stats(self) -> None:
        """Zero the hit / miss / expiration counters; cache contents kept."""

        self._hits = 0
        self._misses = 0
        self._expirations = 0

    def __len__(self) -> int:
        return len(self._store)


__all__ = [
    "QuoteCacheStats",
    "TtlBoundedQuoteCache",
]
