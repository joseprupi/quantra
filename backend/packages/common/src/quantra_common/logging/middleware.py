"""ASGI middleware that threads a request ID through `structlog.contextvars`."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from quantra_common.settings.base import Settings

_HeaderList = list[tuple[bytes, bytes]]
_AsgiSendCallable = Callable[[Message], Awaitable[None]]


class RequestIdMiddleware:
    """Pure-ASGI request-id middleware.

    Reads the inbound request-id header (configurable via Settings, defaults
    to ``X-Request-Id``). If present, propagates it; otherwise generates a
    new UUID4. The ID is bound into ``structlog.contextvars`` so every log
    line emitted during the request is tagged, and is echoed back on the
    response header for the caller to correlate.

    FastAPI services install this with::

        app.add_middleware(RequestIdMiddleware)

    The middleware is implemented as raw ASGI (not Starlette's
    ``BaseHTTPMiddleware``) so it works under any ASGI app and avoids the
    well-known issues of ``BaseHTTPMiddleware`` swallowing exceptions.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        header_name: str | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._app = app
        cfg = settings or Settings()
        self._header_name = (header_name or cfg.request_id_header).lower()
        self._header_bytes = self._header_name.encode("latin-1")

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request_id = self._extract_id(scope) or uuid.uuid4().hex

        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            send_with_header = self._wrap_send(send, request_id)
            await self._app(scope, receive, send_with_header)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")

    def _extract_id(self, scope: MutableMapping[str, Any]) -> str | None:
        headers: _HeaderList = scope.get("headers", [])
        for name, value in headers:
            if name.lower() == self._header_bytes:
                try:
                    return value.decode("latin-1").strip() or None
                except UnicodeDecodeError:
                    return None
        return None

    def _wrap_send(self, send: Send, request_id: str) -> _AsgiSendCallable:
        header_bytes = self._header_bytes
        value_bytes = request_id.encode("latin-1")

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers: _HeaderList = list(message.get("headers", []))
                headers = [(k, v) for (k, v) in headers if k.lower() != header_bytes]
                headers.append((header_bytes, value_bytes))
                message["headers"] = headers
            await send(message)

        return _send
