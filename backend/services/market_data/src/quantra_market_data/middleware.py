"""Read-only enforcement middleware.

Lifted verbatim from the legacy service. The MD service is read-only on the
request path (north-star invariant 3) — writes belong to the ingester. This
middleware is the runtime safety net even though all currently-registered
routes are GET-only or compute-only POSTs. It is kept as a defense-in-depth
layer so a future stray ``@app.put`` or ``@app.delete`` is blocked at the
boundary instead of reaching the database.

The legacy 404 on ``/md/...`` is preserved: the historical proxy used to
forward ``/md/*`` to this service unchanged, so the service rejected the
deprecated prefix to make migration noisy.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse, Response

# Compute-only POST endpoints that take a JSON body but never mutate state.
# Anything not in this allow-list is rejected with HTTP 403.
READ_ONLY_ALLOWED_POST_PATHS: frozenset[str] = frozenset(
    {
        "/quotes/resolved",
    }
)


async def enforce_read_only_api(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Block writes and the deprecated ``/md/*`` prefix at the boundary."""

    method = request.method.upper()
    path = request.url.path
    if path == "/md" or path.startswith("/md/"):
        return JSONResponse(
            status_code=404,
            content={"detail": "Deprecated path prefix. Use top-level endpoints."},
        )
    if method in {"PUT", "PATCH", "DELETE"}:
        return JSONResponse(
            status_code=403,
            content={"detail": "Market data API is read-only for this deployment."},
        )
    if method == "POST" and path not in READ_ONLY_ALLOWED_POST_PATHS:
        return JSONResponse(
            status_code=403,
            content={"detail": "POST is disabled on read-only market data API."},
        )
    return await call_next(request)
