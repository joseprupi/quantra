"""Async HTTP client for the internal Quantra market-data service.

Wraps the read-only MD service surface so the orchestrator and ingester
don't re-implement retries / caching / error mapping. The client is
async-first (`httpx.AsyncClient`), retries idempotent reads with
exponential backoff, and accepts a pluggable :class:`QuoteCache` so
callers can layer their own storage (the orchestrator supplies a
TTL-bounded LRU; pass ``NullQuoteCache`` for uncached reads).
"""

from __future__ import annotations

from quantra_common.md_client.cache import (
    NullQuoteCache,
    QuoteCache,
    quote_cache_key,
)
from quantra_common.md_client.client import MdClient, MdClientConfig
from quantra_common.md_client.errors import (
    MdClientError,
    MdHttpStatusError,
    MdNotFoundError,
    MdResponseError,
    MdTimeoutError,
    MdTransportError,
)

__all__ = [
    "MdClient",
    "MdClientConfig",
    "MdClientError",
    "MdHttpStatusError",
    "MdNotFoundError",
    "MdResponseError",
    "MdTimeoutError",
    "MdTransportError",
    "NullQuoteCache",
    "QuoteCache",
    "quote_cache_key",
]
