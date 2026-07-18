"""MD-client construction + per-request dependency wiring.

The orchestrator owns one :class:`quantra_common.md_client.MdClient`
per process. The lifespan in ``quantra_orchestrator.app.create_app``
constructs it from settings, parks it on ``app.state.md_client``, and
disposes the underlying ``httpx.AsyncClient`` on shutdown. Routes pull
the same instance via :func:`get_md_client` so the HTTP connection
pool is shared across every request.

Per-request construction is a perf bug, not a correctness fix — every
PR touching this module should keep the singleton invariant.

The orchestrator forwards the inbound ``X-Request-Id`` header to the
upstream MD service via an ``httpx`` request event hook so MD logs
correlate with the originating orchestrator request without changing
the public ``MdClient`` API (stability guarantee).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final

import httpx
import structlog
from fastapi import HTTPException, Request, status

from quantra_common.md_client import (
    MdClient,
    MdClientConfig,
    QuoteCache,
)
from quantra_orchestrator.md.cache import TtlBoundedQuoteCache
from quantra_orchestrator.settings import OrchestratorSettings

MD_CLIENT_UNAVAILABLE_CODE: Final[str] = "md_client_unavailable"


class MdClientUnavailableError(HTTPException):
    """503 raised when no ``MdClient`` is attached to ``app.state``.

    Same fail-closed posture as ``AppDbUnavailableError`` from the
    data layer: a misconfigured deploy that forgot to set
    ``MD_SERVICE_URL`` must surface as an explicit "the dependency is
    unconfigured" rather than an opaque 500 on first traffic.
    """

    code: Final[str] = MD_CLIENT_UNAVAILABLE_CODE

    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "MD client is not configured (md_service_url missing). "
                "Set MD_SERVICE_URL to enable orchestrator MD reads."
            ),
        )


def build_md_client(
    settings: OrchestratorSettings,
    *,
    cache: QuoteCache,
    request_id_header: str,
) -> tuple[MdClient, httpx.AsyncClient]:
    """Construct the lifespan-owned :class:`MdClient` + its transport.

    Wires the underlying ``httpx.AsyncClient`` ourselves (instead of
    using :meth:`MdClient.from_settings`) so we can attach the
    request-id event hook without changing the public client API. The
    returned ``MdClient`` does **not** own the httpx client lifecycle
    (``MdClient.aclose`` is a no-op when ``client=`` is passed in); the
    lifespan that builds the pair is responsible for closing the
    transport on shutdown.
    """

    config = MdClientConfig(
        base_url=settings.require_md_service_url(),
        timeout_s=settings.md_service_timeout_s,
        max_retries=settings.md_client_max_retries,
        backoff_base_s=settings.md_client_backoff_base_s,
        backoff_cap_s=settings.md_client_backoff_cap_s,
    )
    transport = httpx.AsyncClient(
        base_url=config.base_url.rstrip("/"),
        timeout=config.timeout_s,
        event_hooks={"request": [_make_request_id_hook(request_id_header)]},
    )
    client = MdClient(config, cache=cache, client=transport)
    return client, transport


def _make_request_id_hook(
    header_name: str,
) -> Callable[[httpx.Request], Awaitable[None]]:
    """Build an ``httpx`` request hook that forwards the request-id header.

    Reads ``request_id`` from ``structlog.contextvars`` (bound by
    ``RequestIdMiddleware``); if absent (e.g. an MD call from a
    background task with no request context), the hook is a no-op so
    the upstream call still proceeds.
    """

    async def _attach_request_id(request: httpx.Request) -> None:
        ctx = structlog.contextvars.get_contextvars()
        request_id = ctx.get("request_id")
        if isinstance(request_id, str) and request_id:
            request.headers[header_name] = request_id

    return _attach_request_id


def get_md_client(request: Request) -> MdClient:
    """Resolve the lifespan-owned ``MdClient`` or raise 503.

    Mirrors the data layer's ``get_app_*_engine`` shape: read from
    ``app.state``, fail closed when missing. Tests inject a fake
    ``MdClient`` by setting ``app.state.md_client`` before the request
    runs (the same settings-override seam the engine dependencies
    use).
    """

    client = getattr(request.app.state, "md_client", None)
    if client is None:
        raise MdClientUnavailableError()
    if not isinstance(client, MdClient):
        msg = f"app.state.md_client is the wrong type: {type(client).__name__}"
        raise TypeError(msg)
    return client


def get_quote_cache(request: Request) -> TtlBoundedQuoteCache:
    """Resolve the lifespan-owned :class:`TtlBoundedQuoteCache` or raise 503.

    Routes that only want stats (``GET /debug/md/cache/stats``) take
    this dependency directly so they don't need to peek into the
    client. Production deploys always wire the cache when the MD
    client is wired; the 503 surface is a deploy-config safety net,
    not a runtime path.
    """

    cache = getattr(request.app.state, "md_cache", None)
    if cache is None:
        raise MdClientUnavailableError()
    if not isinstance(cache, TtlBoundedQuoteCache):
        msg = f"app.state.md_cache is the wrong type: {type(cache).__name__}"
        raise TypeError(msg)
    return cache


__all__ = [
    "MD_CLIENT_UNAVAILABLE_CODE",
    "MdClientUnavailableError",
    "build_md_client",
    "get_md_client",
    "get_quote_cache",
]
