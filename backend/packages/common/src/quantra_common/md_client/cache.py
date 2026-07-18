"""Pluggable cache hook for resolved-quote results.

Cache keys combine ``(canonical_id, as_of_iso, snapshot_version)``. A no-op
implementation (`NullQuoteCache`) is provided so MdClient can run without
caching when the caller wants raw behaviour (tests, ingester debugging).
The orchestrator supplies its own TTL-bounded LRU implementation
(``quantra_orchestrator.md.cache``) against this Protocol.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from quantra_common.types import ResolvedQuote


def quote_cache_key(
    canonical_id: str,
    as_of: datetime | None,
    snapshot_version: str | None,
) -> str:
    """Stable string key used by every `QuoteCache` implementation."""

    iso = as_of.isoformat() if as_of is not None else "latest"
    sv = snapshot_version or "-"
    return f"{canonical_id}|{iso}|{sv}"


class QuoteCache(Protocol):
    """Get/put surface that any cache backend must implement."""

    async def get(self, key: str) -> ResolvedQuote | None: ...

    async def put(self, key: str, value: ResolvedQuote) -> None: ...

    async def clear(self) -> None: ...


class NullQuoteCache:
    """No-op cache. Useful for tests and debug paths that want fresh reads."""

    async def get(self, key: str) -> ResolvedQuote | None:
        del key
        return None

    async def put(self, key: str, value: ResolvedQuote) -> None:
        del key, value

    async def clear(self) -> None:
        return
