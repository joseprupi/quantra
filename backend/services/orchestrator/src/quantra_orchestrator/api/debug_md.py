"""``/debug/md/*`` — verification-only routes for the MD client wiring.

These endpoints exist so ops and smoke-test scripts can confirm the
orchestrator can reach the MD service end-to-end without standing up
a per-product pricing endpoint. Per
they are **not** part of the public API and may change or be removed
at any time; the per-product endpoints and the engine
forwarder are the only sanctioned public consumers of the
MD client.

Both routes require auth (any authenticated identity — the cache stats
are non-sensitive but only authenticated callers should be poking at
this surface). Errors flow through the shared error envelope with the
codes documented on each route.
"""

from __future__ import annotations

from datetime import date

import structlog
from fastapi import APIRouter, Depends, Path, Query

from quantra_common.auth.context import AuthContext
from quantra_common.md_client import MdClient, MdClientError
from quantra_common.types import ResolvedQuote
from quantra_orchestrator.auth.dependencies import get_auth_context
from quantra_orchestrator.md import (
    QuoteCacheStats,
    QuoteNotFoundError,
    TtlBoundedQuoteCache,
    get_md_client,
    get_quote_cache,
    map_md_client_error,
)

# Dev tooling — keep the routes but hide them from the public OpenAPI.
router = APIRouter(prefix="/debug/md", tags=["debug"], include_in_schema=False)

_log = structlog.get_logger(__name__)


@router.get(
    "/quote/{canonical_id}",
    response_model=ResolvedQuote,
    summary="Fetch a single resolved quote (verification only — not a public API).",
    description=(
        "Round-trips a single canonical ID through the orchestrator's "
        "MD client and returns the upstream payload. Intended for ops "
        "smoke tests and the bring-up of the MD wiring; this is "
        "**not** a public market-data read surface. "
        "Errors map to the structured error envelope:\n\n"
        "* `quote_not_found` (404) — the MD service has no row for this "
        "canonical ID at the requested as-of.\n"
        "* `md_unreachable` (502) — the MD service is unreachable or "
        "timed out.\n"
        "* `md_upstream_error` (502) — the MD service returned an "
        "error response (5xx or unparseable body)."
    ),
    responses={
        401: {"description": "Missing or invalid credentials."},
        404: {"description": "Quote not found at the requested as_of."},
        502: {"description": "MD service unreachable or returned an error."},
        503: {"description": "MD client not configured (md_service_url missing)."},
    },
)
async def get_md_quote(
    canonical_id: str = Path(..., min_length=1, description="MD canonical_id."),
    as_of: date = Query(..., description="Date the quote is requested for (YYYY-MM-DD)."),
    md_client: MdClient = Depends(get_md_client),
    _: AuthContext = Depends(get_auth_context),
) -> ResolvedQuote:
    """Resolve one quote through the MD client and return it.

    Cache hit → no upstream call; cache miss → one ``POST
    /quotes/resolved`` to the MD service. Either way the response body
    is the single ``ResolvedQuote`` for ``canonical_id`` at ``as_of``.
    A miss inside the upstream response (``found=False``) is surfaced
    as a 404 with ``code="quote_not_found"`` rather than returned
    silently.
    """

    as_of_iso = as_of.isoformat()
    try:
        results = await md_client.resolve_quotes([canonical_id], as_of)
    except MdClientError as exc:
        _log.warning(
            "orchestrator.debug_md.upstream_error",
            canonical_id=canonical_id,
            as_of=as_of_iso,
            exc_type=type(exc).__name__,
        )
        raise map_md_client_error(exc, canonical_id=canonical_id, as_of=as_of_iso) from exc

    if not results:  # pragma: no cover — defensive; resolve_quotes always echoes one item
        raise QuoteNotFoundError(canonical_id=canonical_id, as_of=as_of_iso)
    quote = results[0]
    if not quote.found:
        raise QuoteNotFoundError(canonical_id=canonical_id, as_of=as_of_iso)
    return quote


@router.get(
    "/cache/stats",
    response_model=QuoteCacheStats,
    summary="Inspect the MD quote cache (verification only — not a public API).",
    description=(
        "Returns the orchestrator's in-process resolved-quote cache "
        "counters: hit / miss / expiration totals plus current size, "
        "configured max entries, and TTL. Useful for confirming that "
        "the cache is being exercised end-to-end and for sizing tuning. "
        "Same not-public-API caveat as `/debug/md/quote/...`."
    ),
    responses={
        401: {"description": "Missing or invalid credentials."},
        503: {"description": "MD cache not configured."},
    },
)
async def get_md_cache_stats(
    cache: TtlBoundedQuoteCache = Depends(get_quote_cache),
    _: AuthContext = Depends(get_auth_context),
) -> QuoteCacheStats:
    """Return the cache's current counters + bounds."""

    return cache.stats()
