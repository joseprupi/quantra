"""MD-client wiring for the orchestrator.

Owns the singleton :class:`quantra_common.md_client.MdClient` instance,
the bounded in-process resolved-quote cache, and the FastAPI dependencies
(``get_md_client`` / ``get_quote_cache``) that hand both to routes via
``app.state`` (the same settings-override seam used by the data
layer's engines).

The MD service is the only upstream the orchestrator reads on the
request path; the client is constructed once per process in the
FastAPI lifespan so every request shares one HTTP connection pool.
Per-request construction is a perf bug, not a correctness fix —
reject it in review.
"""

from quantra_orchestrator.md.cache import (
    QuoteCacheStats,
    TtlBoundedQuoteCache,
)
from quantra_orchestrator.md.client import (
    MD_CLIENT_UNAVAILABLE_CODE,
    MdClientUnavailableError,
    build_md_client,
    get_md_client,
    get_quote_cache,
)
from quantra_orchestrator.md.errors import (
    MD_UNREACHABLE_CODE,
    MD_UPSTREAM_ERROR_CODE,
    QUOTE_NOT_FOUND_CODE,
    MdUnreachableError,
    MdUpstreamError,
    QuoteNotFoundError,
    map_md_client_error,
)

__all__ = [
    "MD_CLIENT_UNAVAILABLE_CODE",
    "MD_UNREACHABLE_CODE",
    "MD_UPSTREAM_ERROR_CODE",
    "QUOTE_NOT_FOUND_CODE",
    "MdClientUnavailableError",
    "MdUnreachableError",
    "MdUpstreamError",
    "QuoteCacheStats",
    "QuoteNotFoundError",
    "TtlBoundedQuoteCache",
    "build_md_client",
    "get_md_client",
    "get_quote_cache",
    "map_md_client_error",
]
