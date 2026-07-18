"""``POST /v1/price/swaption`` — the swaption pricing endpoint.

End-to-end vertical, structurally identical to the swap_ir route
(per-product split):

1. Auth (``Depends(get_auth_context)``) — unauthenticated path on
   failure.
2. Assemble (``app_ro``) — load swaption / curve set / curves / vol
   surface / swaption model / snapshot from five ``app.*`` tables.
3. MD resolve — substitute every quote ID for its resolved value
   (snapshot pin first, live MD second). The walker traverses both
   curve points and the vol-surface body grid. Server-side.
4. Fan out via the concurrency seam
   (``execute(policy, engine, [trade], price_batch=price_swaption_batch)``).
   One trade today; the path through ``execute`` is wired so the
   wave-two portfolio endpoint is a one-line change.
5. Decode + respond with the typed :class:`SwaptionPriceResponse`.

Failure surfaces:

* Auth: 401 ``unauthenticated``.
* DB engine missing: 503 ``storage_unavailable`` (data layer 503).
* MD client missing: 503 ``md_client_unavailable``.
* Engine client missing: 503 ``engine_client_unavailable``.
* Saved swaption / curve set / vol surface / swaption model /
  snapshot not visible: 404 ``swaption_not_found`` /
  ``swaption_curve_set_not_found`` /
  ``swaption_vol_surface_not_found`` /
  ``swaption_model_not_found`` /
  ``swaption_snapshot_not_found``.
* Curve resolution: 422 ``swaption_curve_resolution_failed``.
* Surface resolution: 422 ``swaption_surface_resolution_failed``.
* Quote resolution: 422 ``swaption_quote_resolution_failed`` /
  502 ``md_unreachable`` / 502 ``md_upstream_error``.
* Shape past pydantic: 400 ``swaption_invalid_request``.
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
from quantra_orchestrator.pricing.swaption.assembler import (
    AssemblerOutput,
    assemble,
)
from quantra_orchestrator.pricing.swaption.engine_io import (
    decode_swaption_request_wire,
    price_swaption_batch,
)
from quantra_orchestrator.pricing.swaption.md_resolution import collect_quote_ids
from quantra_orchestrator.pricing.swaption.md_resolution import resolve as md_resolve
from quantra_orchestrator.pricing.swaption.models import (
    AssembledSwaptionRequest,
    SwaptionPriceRequest,
    SwaptionPriceResponse,
    SwaptionResult,
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

_PRODUCT_KIND: str = "swaptions"


def _get_app_rw_engine_optional(request: Request) -> AsyncEngine | None:
    engine: AsyncEngine | None = getattr(request.app.state, "app_rw_engine", None)
    return engine


router = APIRouter(prefix="/v1/price/swaption", tags=["pricing:swaption"])

_log = structlog.get_logger(__name__)


@router.post(
    "",
    status_code=status.HTTP_200_OK,
    response_model=SwaptionPriceResponse,
    summary="Price one swaption (saved by id or inline).",
    description=(
        'Accepts either ``{"swaption_id": "...", "as_of": "..."}`` '
        "to price a saved ``app.swaptions`` row or an inline "
        '``{"swaption": {...}, "curves": [...], "vol_surface": '
        '{...}, "swaption_model": {...}, "as_of": "..."}`` payload. '
        "The orchestrator resolves every referenced curve / vol-"
        "surface quote / swaption-model config server-side and "
        "forwards a fully self-contained request to the pricing "
        "engine. If no engine backend is configured the call fails "
        "with an ``engine_unavailable`` error envelope that includes "
        "the assembled request in ``details`` so the resolution path "
        "is verifiable."
    ),
    responses={
        400: {"description": "Engine rejected the request as invalid."},
        401: {"description": "Missing or invalid credentials."},
        404: {
            "description": (
                "``swaption_id`` / referenced ``curve_set_id`` / "
                "``vol_surface_id`` / ``swaption_model_id`` / "
                "``snapshot_id`` not visible to the caller."
            )
        },
        422: {"description": ("Curve, surface, or quote resolution failed (see ``code``).")},
        502: {"description": ("Engine or MD service unreachable / returned an error.")},
        503: {"description": ("Persistent storage, MD client, or engine client not configured.")},
        504: {"description": "Engine timed out."},
    },
)
async def price_swaption(
    payload: SwaptionPriceRequest,
    http_request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    ro_engine: AsyncEngine = Depends(get_app_ro_engine),
    rw_engine: AsyncEngine | None = Depends(_get_app_rw_engine_optional),
    md_client: MdClient = Depends(get_md_client),
    engine: EngineClient = Depends(get_engine_client),
    settings: OrchestratorSettings = Depends(get_orchestrator_settings),
) -> SwaptionPriceResponse:
    """End-to-end pricing route. See module docstring for the failure table."""

    started_ms = time.monotonic()
    request_payload = payload.model_dump(mode="json")

    # in-app pricing trace (best-effort, off the hot path, gated
    # by ``TRACE_CAPTURE``). Keyed by request_id + owner_uid.
    trace = start_trace(
        http_request,
        owner_uid=ctx.uid,
        rw_engine=rw_engine,
        settings=settings,
        product=_PRODUCT_KIND,
    )
    record_input(
        trace,
        product=_PRODUCT_KIND,
        params={
            "swaption_id": (str(payload.swaption_id) if payload.swaption_id is not None else None),
            "as_of": payload.as_of.isoformat(),
            "snapshot_id": (str(payload.snapshot_id) if payload.snapshot_id is not None else None),
            "inline_swaption": payload.swaption is not None,
            "inline_curve_count": len(payload.curves) if payload.curves else 0,
        },
    )

    try:
        stage_started = time.monotonic()
        assembled = await assemble(request=payload, owner_uid=ctx.uid, ro_engine=ro_engine)
        record_load_entities(
            trace,
            {
                "curve_set_id": (
                    str(assembled.curve_set_id) if assembled.curve_set_id is not None else None
                ),
                "curve_ids": [str(c.id) for c in assembled.curves if c.id is not None],
                "curve_names": [c.name for c in assembled.curves],
                "vol_surface_id": (
                    str(assembled.vol_surface_id) if assembled.vol_surface_id is not None else None
                ),
                "swaption_model_id": (
                    str(assembled.swaption_model_id)
                    if assembled.swaption_model_id is not None
                    else None
                ),
                "snapshot": (
                    {"id": str(assembled.snapshot.id), "name": assembled.snapshot.name}
                    if assembled.snapshot is not None
                    else None
                ),
            },
            duration_ms=elapsed_ms(stage_started),
        )

        stage_started = time.monotonic()
        resolved_quotes = await md_resolve(
            curves=assembled.curves,
            vol_surface=assembled.vol_surface,
            as_of=payload.as_of,
            snapshot=assembled.snapshot,
            md_client=md_client,
            snapshot_version=(assembled.snapshot.version_etag if assembled.snapshot else None),
        )
        record_md_resolve(
            trace,
            requested_canonical_ids=collect_quote_ids(assembled.curves, assembled.vol_surface),
            resolved_quotes=resolved_quotes,
            duration_ms=elapsed_ms(stage_started),
        )

        assembled_request = _build_assembled_request_model(
            request=payload, assembled=assembled, resolved_quotes=resolved_quotes
        )

        # the faithful request is assembled from the
        # resolved curves + quotes + vol surface + swaption model the stages
        # above produced. The bundle is captured once at route scope and passed
        # into ``price_swaption_batch`` by the ``price_batch`` lambda's closure
        # , keeping the ``shared_inputs`` seam free of large
        # resolved graphs. One bundle per request is correct for
        # ``OneTradePerCall`` and the reserved ``group_by_<shared-bundle>``
        # policies; see the route-scope note in ``_translator``.
        resolved_market_data = ResolvedMarketData(
            as_of=payload.as_of.isoformat(),
            curves=tuple(assembled.curves),
            quotes=tuple(resolved_quotes),
            curve_roles=assembled.curve_roles(),
            # the resolved underlying-swap float index (``None`` when the
            # request omitted it → the interim default forwarding index +
            # Semiannual, exactly today's behaviour). When present the engine_io
            # already reads it via ``resolved.forwarding_index_id`` +
            # ``float_leg_frequency(resolved)`` so the underlying float leg's
            # projection index and coupon frequency follow its tenor.
            index=assembled.index,
            vol_surfaces=(assembled.vol_surface,),
            models=(assembled.swaption_model,),
        )

        # Batching seam — even with one trade we go through ``execute`` so the
        # path through the runner is wired. Wave-two portfolio support is
        # a single change to ``trades=[...]``.
        policy_cls = resolve_policy(settings.concurrency_policy_swaption)

        # wrap the engine so the EXACT FlatBuffers bytes handed to the
        # gRPC channel are captured at the send boundary; the route reads them
        # back after the call to record the ``engine_request`` wire view.
        capturing_engine = CapturingEngineClient(engine)

        engine_started = time.monotonic()
        try:
            results = await execute(
                policy_cls(),
                capturing_engine,
                trades=[assembled.trade],
                price_batch=lambda eng, batch: price_swaption_batch(
                    eng, _coerce_batch(batch), resolved=resolved_market_data
                ),
                shared_inputs={},
            )
        except HTTPException:
            # MD-resolution + assembler raised structured-error HTTPExceptions
            # already; let them propagate verbatim. Record the engine_request
            # stage first (no bytes sent on a pre-send failure → sent: false).
            _record_engine_request_stage(trace, assembled_request, capturing_engine)
            raise
        except Exception as exc:
            # Any engine-side failure (transport, NotImplementedError, decode
            # errors, ...) gets the same structured surface, so the caller
            # never sees the orchestrator's generic 500 handler when the
            # engine round-trip is the failing link.
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
        # + decoded view + assembled-inputs superset) before engine_response.
        _record_engine_request_stage(trace, assembled_request, capturing_engine)

        if not results:
            msg = "engine returned no results for one-trade batch"
            mapped = map_engine_client_error(RuntimeError(msg))
            _attach_assembled_details(mapped, assembled_request)
            raise mapped
    except HTTPException as exc:
        # close the trace timeline on the same envelope the
        # client got (covers 404 / 422 / MD surfaces too).
        record_error_stage(trace, exc)
        history_id = await record_pricing_call(
            rw_engine=rw_engine,
            owner_uid=ctx.uid,
            product_kind=_PRODUCT_KIND,
            product_id=payload.swaption_id,
            as_of=payload.as_of,
            request_payload=request_payload,
            response_payload=failure_envelope(exc),
        )
        record_history_write(trace, history_id, outcome="failure_row")
        raise

    duration_ms = (time.monotonic() - started_ms) * 1000.0
    _log.info(
        "orchestrator.pricing.swaption.success",
        uid=ctx.uid,
        swaption_id=(str(payload.swaption_id) if payload.swaption_id is not None else None),
        curve_set_id=(str(assembled.curve_set_id) if assembled.curve_set_id is not None else None),
        vol_surface_id=(
            str(assembled.vol_surface_id) if assembled.vol_surface_id is not None else None
        ),
        swaption_model_id=(
            str(assembled.swaption_model_id) if assembled.swaption_model_id is not None else None
        ),
        as_of=payload.as_of.isoformat(),
        snapshot_id=(str(payload.snapshot_id) if payload.snapshot_id is not None else None),
        resolved_quote_count=len(resolved_quotes),
        duration_ms=round(duration_ms, 3),
    )

    result = results[0]
    # engine_response stage on success (npv + legs + extras).
    record_engine_response(
        trace,
        {"npv": result.npv, "leg_npvs": result.leg_npvs, "extras": result.extras},
        duration_ms=elapsed_ms(engine_started),
    )
    history_id = await record_pricing_call(
        rw_engine=rw_engine,
        owner_uid=ctx.uid,
        product_kind=_PRODUCT_KIND,
        product_id=payload.swaption_id,
        as_of=payload.as_of,
        request_payload=request_payload,
        response_payload=result.model_dump(mode="json"),
    )
    record_history_write(trace, history_id, outcome="success_row")

    return SwaptionPriceResponse(
        pricing_history_id=str(history_id) if history_id is not None else None,
        assembled_request=assembled_request,
        result=result,
    )


def _record_engine_request_stage(
    trace: TraceRecorder,
    assembled_request: AssembledSwaptionRequest,
    capturing_engine: CapturingEngineClient,
) -> None:
    """Emit the ``engine_request`` stage with both views (assembled + wire).

    Reads the exact transmitted bytes off the :class:`CapturingEngineClient`
    (``None`` when the request never reached the send boundary) and decodes
    them with :func:`decode_swaption_request_wire`. Delegates to the shared
    :func:`record_engine_request_wire`, which handles the ``trace.enabled``
    gate and the ``sent: false`` pre-send case.
    """

    record_engine_request_wire(
        trace,
        assembled_request=assembled_request.model_dump(mode="json"),
        capturing_engine=capturing_engine,
        decode=decode_swaption_request_wire,
    )


def _coerce_batch(batch: object) -> EngineBatch[Any]:
    """Cast the runner-supplied batch into the typed shape ``price_swaption_batch`` wants.

    The ``execute`` runner is generic over ``T``; the lambda
    above closes over the actual ``SwaptionTrade`` type. Re-asserting
    the shape here keeps mypy happy without disabling strictness in
    the route module.
    """

    if not isinstance(batch, EngineBatch):
        msg = f"price_swaption_batch received a non-EngineBatch: {type(batch).__name__}"
        raise TypeError(msg)
    return batch


def _build_assembled_request_model(
    *,
    request: SwaptionPriceRequest,
    assembled: AssemblerOutput,
    resolved_quotes: list[Any],
) -> AssembledSwaptionRequest:
    return AssembledSwaptionRequest(
        as_of=request.as_of,
        snapshot_id=request.snapshot_id,
        curve_set_id=assembled.curve_set_id,
        vol_surface_id=assembled.vol_surface_id,
        swaption_model_id=assembled.swaption_model_id,
        trade=assembled.trade,
        curves=assembled.curves,
        vol_surface=assembled.vol_surface,
        swaption_model=assembled.swaption_model,
        resolved_quotes=resolved_quotes,
    )


def _attach_assembled_details(
    exc: HTTPException, assembled_request: AssembledSwaptionRequest
) -> None:
    """Stash the assembled request on the HTTPException so error ``details`` carries it.

    Mirrors the swap_ir-side pattern: the global handler reads
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


def _on_engine_failure(
    trace: TraceRecorder,
    exc: Exception,
    engine_started: float,
    assembled_request: AssembledSwaptionRequest,
    ctx: AuthContext,
    payload: SwaptionPriceRequest,
    assembled: AssemblerOutput,
    started_ms: float,
) -> HTTPException:
    """Record + map + log one engine-side failure; return the mapped error.

    Mirrors the swap_ir pilot: the trace capture of the engine's
    REAL error text, the mapping, the assembled-request ``details``
    attachment, and the structured failure log stay identical across
    both engine ``except`` arms by construction.
    """

    record_engine_error(trace, exc, duration_ms=elapsed_ms(engine_started))
    mapped = map_engine_client_error(exc)
    _attach_assembled_details(mapped, assembled_request)
    _log_failure(ctx=ctx, payload=payload, assembled=assembled, started_ms=started_ms, exc=exc)
    return mapped


def _log_failure(
    *,
    ctx: AuthContext,
    payload: SwaptionPriceRequest,
    assembled: AssemblerOutput,
    started_ms: float,
    exc: BaseException,
) -> None:
    duration_ms = (time.monotonic() - started_ms) * 1000.0
    _log.warning(
        "orchestrator.pricing.swaption.failure",
        uid=ctx.uid,
        swaption_id=(str(payload.swaption_id) if payload.swaption_id is not None else None),
        curve_set_id=(str(assembled.curve_set_id) if assembled.curve_set_id is not None else None),
        vol_surface_id=(
            str(assembled.vol_surface_id) if assembled.vol_surface_id is not None else None
        ),
        swaption_model_id=(
            str(assembled.swaption_model_id) if assembled.swaption_model_id is not None else None
        ),
        as_of=payload.as_of.isoformat(),
        snapshot_id=(str(payload.snapshot_id) if payload.snapshot_id is not None else None),
        duration_ms=round(duration_ms, 3),
        exc_type=type(exc).__name__,
    )


__all__ = [
    "SwaptionPriceRequest",
    "SwaptionPriceResponse",
    "SwaptionResult",
    "router",
]
