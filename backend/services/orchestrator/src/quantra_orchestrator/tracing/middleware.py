"""ASGI middleware that flushes a request's :class:`TraceRecorder`.

A pricing route stashes its recorder on ``request.state.trace_recorder``
(backed by ``scope["state"]``). This middleware flushes that recorder
*after* the wrapped app finishes — i.e. after the response body has been
sent — so the best-effort trace write happens off the caller's latency
path and on BOTH the success and the error path (the recorder is read
from the scope regardless of how the handler returned).

Implemented as raw ASGI (like
:class:`quantra_common.logging.RequestIdMiddleware`) so it shares the
scope object the route mutated and avoids ``BaseHTTPMiddleware``'s
exception-swallowing pitfalls. The flush itself can never raise
(:meth:`TraceRecorder.flush` swallows everything), so the ``finally``
here cannot turn a served response into an error.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

from quantra_orchestrator.tracing.recorder import TraceRecorder

TRACE_RECORDER_STATE_KEY: str = "trace_recorder"


class TraceFlushMiddleware:
    """Flush the per-request :class:`TraceRecorder` after the response."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        try:
            await self._app(scope, receive, send)
        finally:
            recorder = self._recorder_from_scope(scope)
            if recorder is not None:
                await recorder.flush()

    @staticmethod
    def _recorder_from_scope(scope: Scope) -> TraceRecorder | None:
        state = scope.get("state")
        if not isinstance(state, dict):
            return None
        recorder = state.get(TRACE_RECORDER_STATE_KEY)
        return recorder if isinstance(recorder, TraceRecorder) else None


__all__ = ["TRACE_RECORDER_STATE_KEY", "TraceFlushMiddleware"]
