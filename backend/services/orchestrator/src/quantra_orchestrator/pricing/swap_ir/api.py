"""``POST /v1/price/swap/ir`` — the IR-swap pricing endpoint.

End-to-end vertical:

1. Auth (``Depends(get_auth_context)``) — unauthenticated path on
   failure.
2. Assemble (``app_ro``) — load swap / curve set / curves / snapshot.
3. MD resolve — substitute every quote ID for its resolved value
   (snapshot pin first, live MD second). Server-side.
4. Fan out via the concurrency seam
   (``execute(policy, engine, [trade], price_batch=price_swap_ir_batch)``).
   One trade today; the path through ``execute`` is wired so the
   wave-two portfolio endpoint is a one-line change.
5. Decode + respond with the typed :class:`IrSwapPriceResponse`.

Failure surfaces:

* Auth: 401 ``unauthenticated``.
* DB engine missing: 503 ``storage_unavailable`` (data layer 503).
* MD client missing: 503 ``md_client_unavailable``.
* Engine client missing: 503 ``engine_client_unavailable``.
* Saved swap / curve set / snapshot not visible: 404
  ``swap_ir_not_found`` / ``swap_ir_curve_set_not_found`` /
  ``swap_ir_snapshot_not_found``.
* Curve resolution: 422 ``swap_ir_curve_resolution_failed``.
* Quote resolution: 422 ``swap_ir_quote_resolution_failed`` /
  502 ``md_unreachable`` / 502 ``md_upstream_error``.
* Engine: 502 ``engine_unavailable`` / 502 ``engine_unreachable`` /
  504 ``engine_timeout`` / 400 ``engine_invalid_request`` /
  502 ``engine_upstream_error``. The engine-failure surface
  carries the assembled-request shape in the envelope
  ``details`` so operators can verify the orchestrator completed
  every preceding stage before the engine returned.
"""

from __future__ import annotations

import time
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_common.auth.context import AuthContext
from quantra_common.engine_client import (
    CapturingEngineClient,
    EngineClient,
)
from quantra_common.md_client import MdClient
from quantra_orchestrator.auth.dependencies import get_auth_context
from quantra_orchestrator.data.engines import get_app_ro_engine
from quantra_orchestrator.engine import (
    get_engine_client,
    map_engine_client_error,
)
from quantra_orchestrator.md import get_md_client
from quantra_orchestrator.pricing._translator import ResolvedMarketData
from quantra_orchestrator.pricing.concurrency import (
    EngineBatch,
    execute,
    resolve_policy,
)
from quantra_orchestrator.pricing.history import record_pricing_call
from quantra_orchestrator.pricing.swap_ir.assembler import (
    AssemblerOutput,
    assemble,
)
from quantra_orchestrator.pricing.swap_ir.engine_io import (
    decode_swap_ir_request_wire,
    price_swap_ir_batch,
)
from quantra_orchestrator.pricing.swap_ir.md_resolution import (
    collect_quote_ids,
)
from quantra_orchestrator.pricing.swap_ir.md_resolution import (
    resolve as md_resolve,
)
from quantra_orchestrator.pricing.swap_ir.models import (
    AssembledIrSwapRequest,
    IrSwapPriceRequest,
    IrSwapPriceResponse,
    IrSwapResult,
)
from quantra_orchestrator.settings import (
    OrchestratorSettings,
    get_orchestrator_settings,
)
from quantra_orchestrator.tracing import (
    TraceRecorder,
    elapsed_ms,
    failure_envelope,
    record_engine_error,
    record_engine_request_wire,
    record_engine_response,
    record_error_stage,
    record_history_write,
    record_input,
    record_load_entities,
    record_md_resolve,
    start_trace,
)

# ``product_kind`` value pinned by the migration's CHECK list (see
# ``record_pricing_call`` / ``VALID_PRODUCT_KINDS``).
_PRODUCT_KIND: str = "swaps_ir"


def _get_app_rw_engine_optional(request: Request) -> AsyncEngine | None:
    """Resolve the lifespan-owned ``app_rw`` engine without raising.

    The data layer's :func:`get_app_ro_engine` / :func:`get_app_rw_engine`
    raise a 503 ``storage_unavailable`` when the engine is missing
    because the CRUD surface can't serve traffic without a backing
    store. The pricing-history hook is different: by design a
    missing ``app_rw`` engine MUST be a non-blocking, fire-and-forget
    failure (history-write failure can't turn a successful price into
    a 500). Returning ``None`` here lets :func:`record_pricing_call`
    short-circuit gracefully.
    """

    engine: AsyncEngine | None = getattr(request.app.state, "app_rw_engine", None)
    return engine


router = APIRouter(prefix="/v1/price/swap/ir", tags=["pricing:swap:ir"])

_log = structlog.get_logger(__name__)


@router.post(
    "",
    status_code=status.HTTP_200_OK,
    response_model=IrSwapPriceResponse,
    summary="Price one IR swap (saved by id or inline).",
    description=(
        'Accepts either ``{"swap_id": "...", "as_of": "..."}`` to '
        'price a saved ``app.swaps_ir`` row or an inline ``{"swap": '
        '{...}, "curves": [...], "as_of": "..."}`` payload. The '
        "orchestrator resolves every referenced curve / quote server-"
        "side and forwards a fully self-contained request to the "
        "pricing engine. If no engine backend is configured the call "
        "fails with an ``engine_unavailable`` error envelope that "
        "includes the assembled request in ``details`` so the "
        "resolution path is verifiable."
    ),
    responses={
        400: {"description": "Engine rejected the request as invalid."},
        401: {"description": "Missing or invalid credentials."},
        404: {
            "description": (
                "``swap_id`` / referenced ``curve_set_id`` / ``snapshot_id`` "
                "not visible to the caller."
            )
        },
        422: {"description": ("Curve resolution or quote resolution failed (see ``code``).")},
        502: {"description": ("Engine or MD service unreachable / returned an error.")},
        503: {"description": ("Persistent storage, MD client, or engine client not configured.")},
        504: {"description": "Engine timed out."},
    },
)
async def price_swap_ir(
    payload: IrSwapPriceRequest,
    http_request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    ro_engine: AsyncEngine = Depends(get_app_ro_engine),
    rw_engine: AsyncEngine | None = Depends(_get_app_rw_engine_optional),
    md_client: MdClient = Depends(get_md_client),
    engine: EngineClient = Depends(get_engine_client),
    settings: OrchestratorSettings = Depends(get_orchestrator_settings),
) -> IrSwapPriceResponse:
    """End-to-end pricing route. See module docstring for the failure table."""

    started_ms = time.monotonic()

    # Pre-computed once and reused on both success and failure paths
    # so the persisted ``app.pricing_history.request`` is exactly the
    # post-validation body the route received (the pricing-history design: ``request``
    # is the post-validation body, never the raw bytes).
    request_payload = payload.model_dump(mode="json")

    # in-app pricing trace. The recorder buffers the
    # orchestrator's per-stage view of this call keyed by request_id and
    # is flushed best-effort (post-response, error-swallowing) by
    # ``TraceFlushMiddleware``. Capture is a no-op when ``TRACE_CAPTURE``
    # is off or no ``app_rw`` engine is configured.
    trace = start_trace(
        http_request,
        owner_uid=ctx.uid,
        rw_engine=rw_engine,
        settings=settings,
        product=_PRODUCT_KIND,
    )
    _record_input(trace, payload)

    try:
        stage_started = time.monotonic()
        assembled = await assemble(request=payload, owner_uid=ctx.uid, ro_engine=ro_engine)
        _record_load_entities(trace, assembled, stage_started)

        stage_started = time.monotonic()
        resolved_quotes = await md_resolve(
            curves=assembled.curves,
            as_of=payload.as_of,
            snapshot=assembled.snapshot,
            md_client=md_client,
            snapshot_version=(assembled.snapshot.version_etag if assembled.snapshot else None),
        )
        _record_md_resolve(trace, assembled, resolved_quotes, stage_started)

        assembled_request = _build_assembled_request_model(
            request=payload, assembled=assembled, resolved_quotes=resolved_quotes
        )

        # the faithful request is assembled from the
        # resolved curves + quotes the stages above produced. The bundle is
        # captured once at route scope and passed into ``price_swap_ir_batch``
        # by the ``price_batch`` lambda's closure, keeping the batching-contract
        # ``shared_inputs`` seam free of large resolved graphs. One bundle per
        # request is correct for ``OneTradePerCall`` and the reserved
        # ``group_by_<shared-bundle>`` policies; see the route-scope note
        # in ``_translator``.
        resolved_market_data = ResolvedMarketData(
            as_of=payload.as_of.isoformat(),
            curves=tuple(assembled.curves),
            quotes=tuple(resolved_quotes),
            curve_roles=assembled.curve_roles(),
            index=assembled.index,
        )

        # Batching seam — even with one trade we go through ``execute`` so the
        # path through the runner is wired. Portfolio support is a single
        # change to ``trades=[...]``.
        policy_cls = resolve_policy(settings.concurrency_policy_swap_ir)

        # wrap the engine so the EXACT FlatBuffers bytes handed to the
        # gRPC channel are captured at the send boundary (not re-packed
        # elsewhere). The wrapper holds the bytes on an instance attribute so
        # they survive the ``execute`` ``asyncio.gather`` fan-out; the route
        # reads them back after the call to record the ``engine_request`` stage.
        capturing_engine = CapturingEngineClient(engine)

        engine_started = time.monotonic()
        try:
            results = await execute(
                policy_cls(),
                capturing_engine,
                trades=[assembled.trade],
                price_batch=lambda eng, batch: price_swap_ir_batch(
                    eng, _coerce_batch(batch), resolved=resolved_market_data
                ),
                shared_inputs={},
            )
        except HTTPException:
            # MD-resolution + assembler raised structured-error HTTPExceptions
            # already; let them propagate verbatim. Record the engine_request
            # stage first (no bytes were sent on a pre-send failure, so the
            # wire view is marked ``sent: false``).
            _record_engine_request_stage(trace, assembled_request, capturing_engine)
            raise
        except Exception as exc:
            # Any engine-side failure (transport, NotImplementedError, decode
            # errors, ...) gets the same structured surface, so the caller
            # never sees the orchestrator's generic 500 handler when the
            # engine round-trip is the failing link.
            # The trace still captures the engine's REAL error text (not the
            # mapped error token) so an investigator sees what the engine
            # actually said (e.g. an ESTR negative-time bootstrap failure).
            _record_engine_request_stage(trace, assembled_request, capturing_engine)
            raise _on_engine_failure(
                trace,
                exc,
                engine_started,
                assembled_request,
                ctx,
                payload,
                assembled,
                started_ms,
            ) from exc

        # success path: record the engine_request stage (exact wire bytes
        # + decoded view + the assembled-inputs superset) before engine_response.
        _record_engine_request_stage(trace, assembled_request, capturing_engine)

        if not results:
            # Engine returned zero results for a one-trade batch — shape
            # bug. Treat as upstream error so the client gets a structured-error
            # envelope rather than a generic 500.
            msg = "engine returned no results for one-trade batch"
            mapped = map_engine_client_error(RuntimeError(msg))
            _attach_assembled_details(mapped, assembled_request)
            raise mapped
    except HTTPException as exc:
        # record the envelope the caller will see as the
        # ``error`` stage so the trace timeline ends on the same failure
        # the client got (covers every non-engine surface too: 404s,
        # 422s, MD errors — each already an HTTPException here).
        record_error_stage(trace, exc)
        # The ``app.pricing_history`` failure hook: every failed
        # pricing call lands one immutable row carrying the structured-error
        # envelope under ``response.error`` so the audit trail
        # reflects what the client actually saw. The recording is
        # fire-and-forget — ``record_pricing_call`` swallows its own
        # errors so a history-write failure can never replace the
        # original error surface.
        history_id = await record_pricing_call(
            rw_engine=rw_engine,
            owner_uid=ctx.uid,
            product_kind=_PRODUCT_KIND,
            product_id=payload.swap_id,
            as_of=payload.as_of,
            request_payload=request_payload,
            response_payload=failure_envelope(exc),
        )
        _record_history_write(trace, history_id, outcome="failure_row")
        raise

    duration_ms = (time.monotonic() - started_ms) * 1000.0
    _log.info(
        "orchestrator.pricing.swap_ir.success",
        uid=ctx.uid,
        swap_id=str(payload.swap_id) if payload.swap_id is not None else None,
        curve_set_id=(str(assembled.curve_set_id) if assembled.curve_set_id is not None else None),
        as_of=payload.as_of.isoformat(),
        snapshot_id=(str(payload.snapshot_id) if payload.snapshot_id is not None else None),
        resolved_quote_count=len(resolved_quotes),
        duration_ms=round(duration_ms, 3),
    )

    # engine_response stage on the success path: npv + per-leg
    # NPVs + extras (fair rate, etc.). This is the orchestrator's view
    # of what the engine returned for the one-trade batch.
    result = results[0]
    _record_engine_response(trace, result, engine_started)

    # The ``app.pricing_history`` success hook. ``response`` is
    # the decoded engine response for one trade (matches the read
    # surface ``GET /v1/pricing-history/{id}`` returns). The history
    # write is fire-and-forget / the pricing-history design — when the row
    # cannot be inserted, ``record_pricing_call`` returns ``None``
    # and the route still surfaces the pricing response with
    # ``pricing_history_id=None``.
    history_id = await record_pricing_call(
        rw_engine=rw_engine,
        owner_uid=ctx.uid,
        product_kind=_PRODUCT_KIND,
        product_id=payload.swap_id,
        as_of=payload.as_of,
        request_payload=request_payload,
        response_payload=result.model_dump(mode="json"),
    )
    # history_write stage captures the persistence outcome. A
    # ``None`` id means the immutable write failed (e.g. the existing
    # pricing_history IntegrityError); that surfaces here at ``warn``
    # so an investigator sees the audit-write problem in the timeline.
    _record_history_write(trace, history_id, outcome="success_row")

    return IrSwapPriceResponse(
        pricing_history_id=str(history_id) if history_id is not None else None,
        assembled_request=assembled_request,
        result=result,
    )


def _coerce_batch(batch: object) -> EngineBatch[Any]:
    """Cast the runner-supplied batch into the typed shape ``price_swap_ir_batch`` wants.

    The ``execute`` runner is generic over ``T``; the lambda
    above closes over the actual ``IrSwapTrade`` type. Re-asserting
    the shape here keeps mypy happy without disabling strictness in
    the route module.
    """

    if not isinstance(batch, EngineBatch):
        msg = f"price_swap_ir_batch received a non-EngineBatch: {type(batch).__name__}"
        raise TypeError(msg)
    return batch


def _record_engine_request_stage(
    trace: TraceRecorder,
    assembled_request: AssembledIrSwapRequest,
    capturing_engine: CapturingEngineClient,
) -> None:
    """Emit the ``engine_request`` stage with both views (assembled + wire).

    Records the orchestrator's assembled-inputs superset AND the exact bytes
    transmitted to the engine (base64 + decoded-from-those-bytes JSON via
    :func:`decode_swap_ir_request_wire`). Reads the captured buffer off the
    :class:`CapturingEngineClient` instance (``None`` when the request never
    reached the send boundary). Gated on ``trace.enabled`` so the (cheap)
    payload build + base64 is skipped entirely when capture is off.
    """

    record_engine_request_wire(
        trace,
        assembled_request=assembled_request.model_dump(mode="json"),
        capturing_engine=capturing_engine,
        decode=decode_swap_ir_request_wire,
    )


def _build_assembled_request_model(
    *,
    request: IrSwapPriceRequest,
    assembled: AssemblerOutput,
    resolved_quotes: list[Any],
) -> AssembledIrSwapRequest:
    return AssembledIrSwapRequest(
        as_of=request.as_of,
        snapshot_id=request.snapshot_id,
        curve_set_id=assembled.curve_set_id,
        trade=assembled.trade,
        curves=assembled.curves,
        resolved_quotes=resolved_quotes,
    )


def _attach_assembled_details(
    exc: HTTPException, assembled_request: AssembledIrSwapRequest
) -> None:
    """Stash the assembled request on the HTTPException so error ``details`` carries it.

    The global handler in ``quantra_orchestrator.errors`` reads
    ``getattr(exc, 'details', None)`` and forwards it into the
    envelope. We append rather than replace because some engine-
    family exceptions may eventually carry pre-populated details
    (none do today).
    """

    detail_entry: dict[str, Any] = {
        "assembled_request": assembled_request.model_dump(mode="json"),
    }
    existing = getattr(exc, "details", None)
    if isinstance(existing, list):
        exc.details = [*existing, detail_entry]  # type: ignore[attr-defined]
    else:
        exc.details = [detail_entry]  # type: ignore[attr-defined]


def _record_input(trace: TraceRecorder, payload: IrSwapPriceRequest) -> None:
    """Emit the ``input`` stage: product + key trade params + as_of.

    The inline swap body's economics (notional / side / fixed rate /
    schedule) are surfaced into the params when present so the shared
    ``input``-stage summary reads like a trade ticket
    ("Priced swaps_ir: 10,000,000 Payer @ 2.5%, … as_of …") rather than
    a bare product tag. The shared helper picks these keys up generically.
    """

    params: dict[str, Any] = {
        "swap_id": str(payload.swap_id) if payload.swap_id is not None else None,
        "as_of": payload.as_of.isoformat(),
        "snapshot_id": (str(payload.snapshot_id) if payload.snapshot_id is not None else None),
        "inline_swap": payload.swap is not None,
        "inline_curve_count": len(payload.curves) if payload.curves else 0,
    }
    body = payload.swap or {}
    for key in ("notional", "fixed_rate", "swap_type", "effective_date", "termination_date"):
        value = body.get(key)
        if value is not None:
            params[key] = value
    record_input(trace, product=_PRODUCT_KIND, params=params)


def _record_load_entities(
    trace: TraceRecorder, assembled: AssemblerOutput, stage_started: float
) -> None:
    """Emit the ``load_entities`` stage: which curve/swap/snapshot ids loaded."""

    record_load_entities(
        trace,
        {
            "curve_set_id": (
                str(assembled.curve_set_id) if assembled.curve_set_id is not None else None
            ),
            "curve_ids": [str(c.id) for c in assembled.curves if c.id is not None],
            "curve_names": [c.name for c in assembled.curves],
            "snapshot": (
                {"id": str(assembled.snapshot.id), "name": assembled.snapshot.name}
                if assembled.snapshot is not None
                else None
            ),
            "index": assembled.index.name if assembled.index is not None else None,
        },
        duration_ms=elapsed_ms(stage_started),
    )


def _on_engine_failure(
    trace: TraceRecorder,
    exc: Exception,
    engine_started: float,
    assembled_request: AssembledIrSwapRequest,
    ctx: AuthContext,
    payload: IrSwapPriceRequest,
    assembled: AssemblerOutput,
    started_ms: float,
) -> HTTPException:
    """Record + map + log one engine-side failure; return the mapped error.

    Shared by both engine-failure ``except`` arms so the trace capture,
    the mapping, the assembled-request ``details`` attachment, and
    the structured failure log stay identical by construction.
    """

    _record_engine_error(trace, exc, engine_started)
    mapped = map_engine_client_error(exc)
    _attach_assembled_details(mapped, assembled_request)
    _log_failure(ctx=ctx, payload=payload, assembled=assembled, started_ms=started_ms, exc=exc)
    return mapped


def _record_engine_error(trace: TraceRecorder, exc: BaseException, engine_started: float) -> None:
    """Emit the ``engine_response`` stage with the engine's REAL error text."""

    record_engine_error(trace, exc, duration_ms=elapsed_ms(engine_started))


def _record_engine_response(
    trace: TraceRecorder, result: IrSwapResult, engine_started: float
) -> None:
    """Emit the ``engine_response`` stage on success: npv + legs + extras + flows."""

    # The per-period flows make this payload much larger than the headline
    # NPV view (a 30Y swap can carry ~90 coupons). The trace recorder
    # JSON-encodes the payload and truncates past ``trace_max_payload_bytes``
    # with a ``__truncated__`` marker (graceful), so an oversized flow set
    # never breaks capture. The pricing API response itself is the
    # separately-returned ``IrSwapResult`` and is NEVER capped.
    record_engine_response(
        trace,
        {
            "npv": result.npv,
            "leg_npvs": result.leg_npvs,
            "extras": result.extras,
            "fixed_leg_flows": [f.model_dump(mode="json") for f in result.fixed_leg_flows],
            "floating_leg_flows": [f.model_dump(mode="json") for f in result.floating_leg_flows],
        },
        duration_ms=elapsed_ms(engine_started),
    )


def _record_history_write(trace: TraceRecorder, history_id: object | None, *, outcome: str) -> None:
    """Emit the ``history_write`` stage: persistence success or failure reason."""

    record_history_write(trace, history_id, outcome=outcome)


def _record_md_resolve(
    trace: TraceRecorder,
    assembled: AssemblerOutput,
    resolved_quotes: list[Any],
    stage_started: float,
) -> None:
    """Emit the ``md_resolve`` stage: requested ids, resolved values, source."""

    record_md_resolve(
        trace,
        requested_canonical_ids=collect_quote_ids(assembled.curves),
        resolved_quotes=resolved_quotes,
        duration_ms=elapsed_ms(stage_started),
    )


def _log_failure(
    *,
    ctx: AuthContext,
    payload: IrSwapPriceRequest,
    assembled: AssemblerOutput,
    started_ms: float,
    exc: BaseException,
) -> None:
    duration_ms = (time.monotonic() - started_ms) * 1000.0
    _log.warning(
        "orchestrator.pricing.swap_ir.failure",
        uid=ctx.uid,
        swap_id=str(payload.swap_id) if payload.swap_id is not None else None,
        curve_set_id=(str(assembled.curve_set_id) if assembled.curve_set_id is not None else None),
        as_of=payload.as_of.isoformat(),
        snapshot_id=(str(payload.snapshot_id) if payload.snapshot_id is not None else None),
        duration_ms=round(duration_ms, 3),
        exc_type=type(exc).__name__,
    )


# Re-exported for OpenAPI consumers + tests.
__all__ = [
    "IrSwapPriceRequest",
    "IrSwapPriceResponse",
    "IrSwapResult",
    "router",
]
