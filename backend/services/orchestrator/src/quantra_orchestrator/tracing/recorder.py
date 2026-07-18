"""In-app pricing-trace recorder.

A :class:`TraceRecorder` buffers the orchestrator's *own view* of every
stage of one pricing call (``input`` / ``load_entities`` / ``md_resolve``
/ ``engine_request`` / ``engine_response`` / ``history_write`` / ``error``),
keyed by the end-to-end ``request_id``. At the end of the request
the buffered stages are flushed in a single best-effort background insert
into ``app.pricing_traces``.

Hard constraints (carried verbatim from the tracing design/
north-star invariants):

* **Orchestrator is the SOLE writer** (invariant #3). The engine + MD
  service write nothing here; this records what the orchestrator asked
  and got, from its own vantage point.
* **Best-effort, off the hot path.** :meth:`flush` is fire-and-forget —
  it is scheduled to run *after* the response body has been sent (via
  :class:`~quantra_orchestrator.tracing.middleware.TraceFlushMiddleware`)
  and it swallows every exception (Postgres down, role mis-grant, a
  stale CHECK list, …), logging at ``warning``. A trace-write failure
  must NEVER turn a successful price into an error or slow it down.
* **Owner-scoped** (invariant #5). Every row carries ``owner_uid`` so
  the read endpoint can filter by the requesting principal.
* **Gated + size-capped.** Capture only happens when ``TRACE_CAPTURE``
  is on; each stage payload is JSON-encoded and truncated past
  ``trace_max_payload_bytes`` so a giant engine payload can't bloat a
  row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_log = structlog.get_logger(__name__)

TRACE_WRITE_FAILED_CODE: str = "pricing_trace_write_failed"
"""Log-only error-style token emitted when a trace flush fails. Never
surfaced to a client — tracing is invisible to the pricing response."""

TRUNCATION_MARKER: str = "__truncated__"
"""Key present in a stored payload when it exceeded the size cap."""

SERVICE_ORCHESTRATOR: str = "orchestrator"

# Valid stage tokens. Free-form text in the column (no DB CHECK on
# ``stage`` so a new product can add a stage without a migration), but
# pinned here so a typo at a call site is greppable.
STAGE_INPUT: str = "input"
STAGE_LOAD_ENTITIES: str = "load_entities"
STAGE_MD_RESOLVE: str = "md_resolve"
STAGE_ENGINE_REQUEST: str = "engine_request"
STAGE_ENGINE_RESPONSE: str = "engine_response"
STAGE_HISTORY_WRITE: str = "history_write"
STAGE_ERROR: str = "error"

_VALID_LEVELS: frozenset[str] = frozenset({"info", "warn", "error"})

_INSERT_SQL = text(
    "INSERT INTO app.pricing_traces ("
    "request_id, owner_uid, product, ts, service, stage, level, "
    "duration_ms, summary, payload"
    ") VALUES ("
    ":request_id, :owner_uid, :product, :ts, :service, :stage, :level, "
    ":duration_ms, :summary, CAST(:payload AS jsonb)"
    ")"
)


@dataclass(slots=True)
class _StageEvent:
    ts: datetime
    stage: str
    level: str
    duration_ms: int | None
    summary: str | None
    payload_json: str | None


class TraceRecorder:
    """Per-request buffer of pricing-pipeline stage events.

    Construct one at the top of a pricing route, append stages as the
    pipeline progresses, and let
    :class:`~quantra_orchestrator.tracing.middleware.TraceFlushMiddleware`
    call :meth:`flush` after the response is sent. When ``enabled`` is
    ``False`` (``TRACE_CAPTURE`` off, or no ``app_rw`` engine) every
    method is a cheap no-op.
    """

    def __init__(
        self,
        *,
        request_id: str,
        owner_uid: str,
        rw_engine: AsyncEngine | None,
        enabled: bool,
        max_payload_bytes: int,
        product: str | None = None,
        service: str = SERVICE_ORCHESTRATOR,
    ) -> None:
        self._request_id = request_id
        self._owner_uid = owner_uid
        self._product = product
        self._rw_engine = rw_engine
        # Capture is only meaningful when both the gate is on AND we
        # have somewhere to write. A missing ``app_rw`` engine collapses
        # the recorder to a no-op exactly like the history hook.
        self._enabled = enabled and rw_engine is not None
        self._max_payload_bytes = max_payload_bytes
        self._service = service
        self._events: list[_StageEvent] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def request_id(self) -> str:
        return self._request_id

    def stage(
        self,
        stage: str,
        payload: dict[str, Any] | None = None,
        *,
        level: str = "info",
        duration_ms: float | None = None,
        summary: str | None = None,
    ) -> None:
        """Buffer one stage event. No I/O — never raises on the hot path.

        ``summary`` is the short human-readable one-liner the investigate
        screen renders for this stage (summaries). It is stored in a
        dedicated ``app.pricing_traces.summary`` column (not inside the
        JSON payload) so it survives the payload size-cap truncation and
        reads back as a clean top-level field.
        """

        if not self._enabled:
            return
        safe_level = level if level in _VALID_LEVELS else "info"
        self._events.append(
            _StageEvent(
                ts=datetime.now(UTC),
                stage=stage,
                level=safe_level,
                duration_ms=int(duration_ms) if duration_ms is not None else None,
                summary=summary if isinstance(summary, str) and summary else None,
                payload_json=self._encode_payload(payload),
            )
        )

    def _encode_payload(self, payload: dict[str, Any] | None) -> str | None:
        if payload is None:
            return None
        try:
            encoded = json.dumps(payload, default=str)
        except (TypeError, ValueError):
            # A non-serialisable payload must never break capture; store
            # a marker so the timeline still shows the stage happened.
            return json.dumps({TRUNCATION_MARKER: True, "reason": "unserializable"})
        size = len(encoded.encode("utf-8"))
        if size <= self._max_payload_bytes:
            return encoded
        return json.dumps(
            {
                TRUNCATION_MARKER: True,
                "original_size_bytes": size,
                "cap_bytes": self._max_payload_bytes,
                "preview": encoded[: self._max_payload_bytes],
            }
        )

    async def flush(self) -> None:
        """Persist every buffered stage. Best-effort; never raises.

        Inserts all stages for the request in one transaction. Any
        failure (engine missing, asyncpg connectivity, a stale grant,
        the engine disposed mid-request, …) is swallowed and logged at
        ``warning`` with the :data:`TRACE_WRITE_FAILED_CODE` token. The
        pricing response has already been sent by the time this runs, so
        a failure here is invisible to the caller.
        """

        if not self._enabled or not self._events or self._rw_engine is None:
            return
        try:
            async with self._rw_engine.begin() as conn:
                for event in self._events:
                    await conn.execute(
                        _INSERT_SQL,
                        {
                            "request_id": self._request_id,
                            "owner_uid": self._owner_uid,
                            "product": self._product,
                            "ts": event.ts,
                            "service": self._service,
                            "stage": event.stage,
                            "level": event.level,
                            "duration_ms": event.duration_ms,
                            "summary": event.summary,
                            "payload": event.payload_json,
                        },
                    )
        except Exception as exc:
            _log.warning(
                "orchestrator.pricing_trace.write_failed",
                code=TRACE_WRITE_FAILED_CODE,
                request_id=self._request_id,
                owner_uid=self._owner_uid,
                stage_count=len(self._events),
                exc_type=type(exc).__name__,
            )


__all__ = [
    "SERVICE_ORCHESTRATOR",
    "STAGE_ENGINE_REQUEST",
    "STAGE_ENGINE_RESPONSE",
    "STAGE_ERROR",
    "STAGE_HISTORY_WRITE",
    "STAGE_INPUT",
    "STAGE_LOAD_ENTITIES",
    "STAGE_MD_RESOLVE",
    "TRACE_WRITE_FAILED_CODE",
    "TRUNCATION_MARKER",
    "TraceRecorder",
]
