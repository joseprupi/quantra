"""In-app pricing trace / investigation.

Public surface:

* :class:`TraceRecorder` — per-request buffer + best-effort flush.
* :class:`TraceFlushMiddleware` — flushes the recorder after the
  response is sent.
* :func:`start_trace` — build a recorder from the request context
  (reads the end-to-end ``request_id`` off ``structlog.contextvars``)
  and register it on ``request.state`` so the middleware can flush it.

See for the design.
"""

from __future__ import annotations

import structlog
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_orchestrator.settings import OrchestratorSettings
from quantra_orchestrator.tracing.middleware import (
    TRACE_RECORDER_STATE_KEY,
    TraceFlushMiddleware,
)
from quantra_orchestrator.tracing.recorder import (
    SERVICE_ORCHESTRATOR,
    STAGE_ENGINE_REQUEST,
    STAGE_ENGINE_RESPONSE,
    STAGE_ERROR,
    STAGE_HISTORY_WRITE,
    STAGE_INPUT,
    STAGE_LOAD_ENTITIES,
    STAGE_MD_RESOLVE,
    TRACE_WRITE_FAILED_CODE,
    TRUNCATION_MARKER,
    TraceRecorder,
)
from quantra_orchestrator.tracing.stages import (
    ResolvedQuoteLike,
    build_engine_request_payload,
    elapsed_ms,
    failure_envelope,
    record_engine_error,
    record_engine_request,
    record_engine_request_wire,
    record_engine_response,
    record_error_stage,
    record_history_write,
    record_input,
    record_load_entities,
    record_md_resolve,
    serialize_fb_object,
)


def current_request_id() -> str:
    """Return the request id bound by ``RequestIdMiddleware``.

    Falls back to ``"unknown"`` if no middleware bound one (should not
    happen on the request path, but keeps the recorder constructable in
    isolation).
    """

    bound = structlog.contextvars.get_contextvars()
    request_id = bound.get("request_id")
    return request_id if isinstance(request_id, str) and request_id else "unknown"


def start_trace(
    request: Request,
    *,
    owner_uid: str,
    rw_engine: AsyncEngine | None,
    settings: OrchestratorSettings,
    product: str | None = None,
) -> TraceRecorder:
    """Build a :class:`TraceRecorder` and register it for post-response flush.

    The recorder is stashed on ``request.state`` (backed by
    ``scope["state"]``) so :class:`TraceFlushMiddleware` can read it
    after the handler returns — on the success path and the error path
    alike. ``product`` lands in the dedicated
    ``app.pricing_traces.product`` column so a future recent-calls list
    can show the product without parsing any stage payload.
    """

    recorder = TraceRecorder(
        request_id=current_request_id(),
        owner_uid=owner_uid,
        rw_engine=rw_engine,
        enabled=settings.trace_capture,
        max_payload_bytes=settings.trace_max_payload_bytes,
        product=product,
    )
    setattr(request.state, TRACE_RECORDER_STATE_KEY, recorder)
    return recorder


__all__ = [
    "SERVICE_ORCHESTRATOR",
    "STAGE_ENGINE_REQUEST",
    "STAGE_ENGINE_RESPONSE",
    "STAGE_ERROR",
    "STAGE_HISTORY_WRITE",
    "STAGE_INPUT",
    "STAGE_LOAD_ENTITIES",
    "STAGE_MD_RESOLVE",
    "TRACE_RECORDER_STATE_KEY",
    "TRACE_WRITE_FAILED_CODE",
    "TRUNCATION_MARKER",
    "ResolvedQuoteLike",
    "TraceFlushMiddleware",
    "TraceRecorder",
    "build_engine_request_payload",
    "elapsed_ms",
    "failure_envelope",
    "record_engine_error",
    "record_engine_request",
    "record_engine_request_wire",
    "record_engine_response",
    "record_error_stage",
    "record_history_write",
    "record_input",
    "record_load_entities",
    "record_md_resolve",
    "serialize_fb_object",
    "start_trace",
]
