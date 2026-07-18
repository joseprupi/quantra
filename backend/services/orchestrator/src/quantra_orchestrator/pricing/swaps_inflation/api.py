"""``POST /v1/price/swaps/inflation`` — the inflation-swap pricing endpoint.

Sixth
per-product endpoint following the swap_ir / swaption / bonds /
cds / equity_options vertical:

1. Auth (``Depends(get_auth_context)``) — unauthenticated path
   on failure.
2. Assemble (``app_ro``) — load swap / nominal-discount curve /
   inflation curve / inflation index / snapshot from up to five
   ``app.*`` tables. The inflation index ``kind =
   'Inflation'`` invariant is enforced here.
3. MD resolve — substitute every quote ID for its resolved value
   in BOTH curves (inflation curves are rates-shaped and DO
   flow through the MD walker; the inflation index body is
   consumed verbatim and does NOT). Snapshot pin first, live MD
   second — issued as a single batched
   :meth:`MdClient.resolve_quotes` call so cache + correlated
   logs are amortized across the whole bundle.
4. Fan out via the concurrency seam
   (``execute(policy, engine, [trade], price_batch=price_swap_inflation_batch,
   shared_inputs={...})``). One trade today; the path through
   ``execute`` is wired so a portfolio endpoint is a one-line
   change. ``shared_inputs`` carries ``swap_kind``, the RPC
   discriminator the wire builder branches on.
5. Decode + respond with the typed
   :class:`InflationSwapPriceResponse`.

Engine RPC dispatch (settled): the canonical enum
exposes one RPC per swap kind
(:attr:`EngineRpc.PRICE_ZERO_COUPON_INFLATION_SWAP` /
:attr:`EngineRpc.PRICE_YEAR_ON_YEAR_INFLATION_SWAP`); an earlier
design used a single ``PRICE_SWAP_INFLATION`` placeholder. This
module honors the canonical enum and
threads the discriminator through ``shared_inputs["swap_kind"]``
 so :func:`execute` doesn't need a per-RPC seam.

Failure surfaces:

* Auth: 401 ``unauthenticated``.
* DB engine missing: 503 ``storage_unavailable``.
* MD client missing: 503 ``md_client_unavailable``.
* Engine client missing: 503 ``engine_client_unavailable``.
* Saved swap not visible: 404 ``swap_inflation_not_found``.
* Nominal curve not visible: 404
  ``swap_inflation_nominal_curve_not_found``.
* Inflation curve not visible: 404
  ``swap_inflation_inflation_curve_not_found``.
* Inflation index not visible / wrong kind: 404
  ``swap_inflation_index_not_found``.
* Snapshot not visible: 404 ``swap_inflation_snapshot_not_found``.
* Curve resolution: 422 ``swap_inflation_curve_resolution_failed``
  (same code for nominal + inflation; role in
  ``details``).
* Index resolution: 422 ``swap_inflation_index_resolution_failed``.
* Quote resolution: 422 ``swap_inflation_quote_resolution_failed`` /
  502 ``md_unreachable`` / 502 ``md_upstream_error``.
* Shape past pydantic: 400 ``swap_inflation_invalid_request``.
* Engine: 502 ``engine_unavailable`` / 502 ``engine_unreachable``
  / 504 ``engine_timeout`` / 400 ``engine_invalid_request`` /
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
from quantra_orchestrator.pricing._translator import (
    CurveRole,
    ResolvedMarketData,
    resolved_curve_id,
)
from quantra_orchestrator.pricing.concurrency import (
    EngineBatch,
    execute,
    resolve_policy,
)
from quantra_orchestrator.pricing.history import record_pricing_call
from quantra_orchestrator.pricing.swaps_inflation.assembler import (
    AssemblerOutput,
    assemble,
)
from quantra_orchestrator.pricing.swaps_inflation.engine_io import (
    decode_swap_inflation_request_wire,
    price_swap_inflation_batch,
)
from quantra_orchestrator.pricing.swaps_inflation.md_resolution import (
    collect_quote_ids,
)
from quantra_orchestrator.pricing.swaps_inflation.md_resolution import (
    resolve as md_resolve,
)
from quantra_orchestrator.pricing.swaps_inflation.models import (
    AssembledInflationSwapRequest,
    InflationSwapPriceRequest,
    InflationSwapPriceResponse,
    InflationSwapResult,
    ResolvedQuoteValue,
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

_PRODUCT_KIND: str = "swaps_inflation"


def _get_app_rw_engine_optional(request: Request) -> AsyncEngine | None:
    engine: AsyncEngine | None = getattr(request.app.state, "app_rw_engine", None)
    return engine


router = APIRouter(prefix="/v1/price", tags=["pricing:swaps_inflation"])

_log = structlog.get_logger(__name__)

_COMMON_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"description": "Engine rejected the request as invalid."},
    401: {"description": "Missing or invalid credentials."},
    422: {"description": ("Curve, index, quote, or shape resolution failed (see ``code``).")},
    502: {"description": ("Engine or MD service unreachable / returned an error.")},
    503: {"description": ("Persistent storage, MD client, or engine client not configured.")},
    504: {"description": "Engine timed out."},
}


@router.post(
    "/swaps/inflation",
    status_code=status.HTTP_200_OK,
    response_model=InflationSwapPriceResponse,
    summary="Price one inflation swap (saved by id or inline).",
    description=(
        'Accepts either ``{"swap_id": "...", "as_of": '
        '"..."}`` to price a saved ``app.swaps_inflation`` row '
        "or an inline payload with explicit ``swap`` body, "
        "``curves`` (nominal + inflation), and "
        "``inflation_index`` overrides. The orchestrator resolves "
        "every referenced curve / quote / index server-side "
        "and forwards a fully self-contained request to the "
        "pricing engine. Both ZCIIS and YYIIS variants are "
        "supported via the ``swap_kind`` discriminator."
    ),
    responses={
        **_COMMON_RESPONSES,
        404: {
            "description": (
                "``swap_id`` / referenced ``curve_id`` (nominal "
                "or inflation) / ``inflation_index_id`` / "
                "``snapshot_id`` not visible to the caller."
            )
        },
    },
)
async def price_swap_inflation(
    payload: InflationSwapPriceRequest,
    http_request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    ro_engine: AsyncEngine = Depends(get_app_ro_engine),
    rw_engine: AsyncEngine | None = Depends(_get_app_rw_engine_optional),
    md_client: MdClient = Depends(get_md_client),
    engine: EngineClient = Depends(get_engine_client),
    settings: OrchestratorSettings = Depends(get_orchestrator_settings),
) -> InflationSwapPriceResponse:
    """End-to-end inflation-swap pricing route. See module docstring for failures."""

    started_ms = time.monotonic()
    request_payload = payload.model_dump(mode="json")

    # in-app pricing trace (best-effort, off the hot path).
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
            "swap_id": str(payload.swap_id) if payload.swap_id is not None else None,
            "as_of": payload.as_of.isoformat(),
            "snapshot_id": (str(payload.snapshot_id) if payload.snapshot_id is not None else None),
            "inline_swap": payload.swap is not None,
            "inline_curve_count": len(payload.curves) if payload.curves else 0,
        },
    )

    try:
        stage_started = time.monotonic()
        assembled = await assemble(request=payload, owner_uid=ctx.uid, ro_engine=ro_engine)
        record_load_entities(
            trace,
            {
                "nominal_curve_id": (
                    str(assembled.nominal_curve_id)
                    if assembled.nominal_curve_id is not None
                    else None
                ),
                "inflation_curve_id": (
                    str(assembled.inflation_curve_id)
                    if assembled.inflation_curve_id is not None
                    else None
                ),
                "inflation_index_id": (
                    str(assembled.inflation_index_id)
                    if assembled.inflation_index_id is not None
                    else None
                ),
                "curve_names": [
                    assembled.nominal_curve.name,
                    assembled.inflation_curve.name,
                ],
                "swap_kind": assembled.swap_kind,
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
            nominal_curve=assembled.nominal_curve,
            inflation_curve=assembled.inflation_curve,
            as_of=payload.as_of,
            snapshot=assembled.snapshot,
            md_client=md_client,
            snapshot_version=(assembled.snapshot.version_etag if assembled.snapshot else None),
        )
        record_md_resolve(
            trace,
            requested_canonical_ids=collect_quote_ids(
                [assembled.nominal_curve, assembled.inflation_curve]
            ),
            resolved_quotes=resolved_quotes,
            duration_ms=elapsed_ms(stage_started),
        )

        assembled_request = _build_assembled_request(
            request=payload,
            assembled=assembled,
            resolved_quotes=resolved_quotes,
        )

        # the faithful request is assembled from the
        # resolved nominal + inflation curves + quotes + inflation index the
        # stages above produced. The bundle is captured once at route scope and
        # passed into ``price_swap_inflation_batch`` by the ``price_batch``
        # lambda's closure, keeping the ``shared_inputs`` seam free
        # of large resolved graphs. The nominal curve rides in ``curves`` (it feeds
        # ``rates.curves``); the inflation curve is kept separate (its helper points
        # are inflation-swap helpers). Both ids are tagged by role and the
        # ZCIIS-vs-YYIIS discriminator stays in ``shared_inputs["swap_kind"]``
        # , threaded to the translator by the engine_io.
        resolved_market_data = ResolvedMarketData(
            as_of=payload.as_of.isoformat(),
            curves=(assembled.nominal_curve,),
            quotes=tuple(resolved_quotes),
            curve_roles={
                CurveRole.NOMINAL: resolved_curve_id(assembled.nominal_curve),
                CurveRole.INFLATION: resolved_curve_id(assembled.inflation_curve),
            },
            inflation_curve=assembled.inflation_curve,
            inflation_index=assembled.inflation_index,
        )

        policy_cls = resolve_policy(settings.concurrency_policy_swaps_inflation)
        # ``swap_kind`` is the one shared input the inflation wire builder
        # reads (the ZCIIS-vs-YYIIS RPC discriminator).
        shared_inputs: dict[str, Any] = {
            "swap_kind": assembled.swap_kind,
        }

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
                price_batch=lambda eng, batch: price_swap_inflation_batch(
                    eng, _coerce_batch(batch), resolved=resolved_market_data
                ),
                shared_inputs=shared_inputs,
            )
        except HTTPException:
            # Pre-send failure — no bytes on the wire → sent: false.
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
        record_error_stage(trace, exc)
        history_id = await record_pricing_call(
            rw_engine=rw_engine,
            owner_uid=ctx.uid,
            product_kind=_PRODUCT_KIND,
            product_id=payload.swap_id,
            as_of=payload.as_of,
            request_payload=request_payload,
            response_payload=failure_envelope(exc),
        )
        record_history_write(trace, history_id, outcome="failure_row")
        raise

    duration_ms = (time.monotonic() - started_ms) * 1000.0
    _log.info(
        "orchestrator.pricing.swaps_inflation.success",
        uid=ctx.uid,
        swap_id=str(payload.swap_id) if payload.swap_id is not None else None,
        nominal_curve_id=(
            str(assembled.nominal_curve_id) if assembled.nominal_curve_id is not None else None
        ),
        inflation_curve_id=(
            str(assembled.inflation_curve_id) if assembled.inflation_curve_id is not None else None
        ),
        inflation_index_id=(
            str(assembled.inflation_index_id) if assembled.inflation_index_id is not None else None
        ),
        swap_kind=assembled.swap_kind,
        as_of=payload.as_of.isoformat(),
        snapshot_id=(str(payload.snapshot_id) if payload.snapshot_id is not None else None),
        resolved_quote_count=len(resolved_quotes),
        duration_ms=round(duration_ms, 3),
    )

    result = results[0]
    record_engine_response(
        trace, result.model_dump(mode="json"), duration_ms=elapsed_ms(engine_started)
    )
    history_id = await record_pricing_call(
        rw_engine=rw_engine,
        owner_uid=ctx.uid,
        product_kind=_PRODUCT_KIND,
        product_id=payload.swap_id,
        as_of=payload.as_of,
        request_payload=request_payload,
        response_payload=result.model_dump(mode="json"),
    )
    record_history_write(trace, history_id, outcome="success_row")

    return InflationSwapPriceResponse(
        pricing_history_id=str(history_id) if history_id is not None else None,
        assembled_request=assembled_request,
        result=result,
    )


def _record_engine_request_stage(
    trace: TraceRecorder,
    assembled_request: AssembledInflationSwapRequest,
    capturing_engine: CapturingEngineClient,
) -> None:
    """Emit the ``engine_request`` stage with both views (assembled + wire).

    Inflation swaps ride one of two request roots (ZCIIS / YYIIS), so the wire
    decoder is bound to the captured RPC on the :class:`CapturingEngineClient`
    (``None`` when the request never reached the send boundary). Delegates to
    the shared :func:`record_engine_request_wire` for the gate + ``sent: false``
    pre-send handling.
    """

    captured = capturing_engine.last_request
    rpc = captured.rpc if captured is not None else None
    record_engine_request_wire(
        trace,
        assembled_request=assembled_request.model_dump(mode="json"),
        capturing_engine=capturing_engine,
        decode=lambda request_bytes: decode_swap_inflation_request_wire(request_bytes, rpc=rpc),
    )


def _coerce_batch(batch: object) -> EngineBatch[Any]:
    """Cast the runner-supplied batch into the typed shape the batch translator wants."""

    if not isinstance(batch, EngineBatch):
        msg = f"swaps_inflation.price_batch received a non-EngineBatch: {type(batch).__name__}"
        raise TypeError(msg)
    return batch


def _build_assembled_request(
    *,
    request: InflationSwapPriceRequest,
    assembled: AssemblerOutput,
    resolved_quotes: list[ResolvedQuoteValue],
) -> AssembledInflationSwapRequest:
    return AssembledInflationSwapRequest(
        as_of=request.as_of,
        snapshot_id=request.snapshot_id,
        nominal_curve_id=assembled.nominal_curve_id,
        inflation_curve_id=assembled.inflation_curve_id,
        inflation_index_id=assembled.inflation_index_id,
        trade=assembled.trade,
        nominal_curve=assembled.nominal_curve,
        inflation_curve=assembled.inflation_curve,
        inflation_index=assembled.inflation_index,
        resolved_quotes=resolved_quotes,
    )


def _attach_assembled_details(
    exc: HTTPException, assembled_request: AssembledInflationSwapRequest
) -> None:
    """Stash the assembled request on the HTTPException for error ``details``."""

    detail_entry: dict[str, Any] = {"assembled_request": assembled_request.model_dump(mode="json")}
    existing = getattr(exc, "details", None)
    if isinstance(existing, list):
        exc.details = [*existing, detail_entry]  # type: ignore[attr-defined]
    else:
        exc.details = [detail_entry]  # type: ignore[attr-defined]


def _on_engine_failure(
    trace: TraceRecorder,
    exc: Exception,
    engine_started: float,
    assembled_request: AssembledInflationSwapRequest,
    ctx: AuthContext,
    payload: InflationSwapPriceRequest,
    assembled: AssemblerOutput,
    started_ms: float,
) -> HTTPException:
    """Record + map + log one engine-side failure; return the mapped error."""

    record_engine_error(trace, exc, duration_ms=elapsed_ms(engine_started))
    mapped = map_engine_client_error(exc)
    _attach_assembled_details(mapped, assembled_request)
    _log_failure(ctx=ctx, payload=payload, assembled=assembled, started_ms=started_ms, exc=exc)
    return mapped


def _log_failure(
    *,
    ctx: AuthContext,
    payload: InflationSwapPriceRequest,
    assembled: AssemblerOutput,
    started_ms: float,
    exc: BaseException,
) -> None:
    duration_ms = (time.monotonic() - started_ms) * 1000.0
    _log.warning(
        "orchestrator.pricing.swaps_inflation.failure",
        uid=ctx.uid,
        swap_id=str(payload.swap_id) if payload.swap_id is not None else None,
        nominal_curve_id=(
            str(assembled.nominal_curve_id) if assembled.nominal_curve_id is not None else None
        ),
        inflation_curve_id=(
            str(assembled.inflation_curve_id) if assembled.inflation_curve_id is not None else None
        ),
        inflation_index_id=(
            str(assembled.inflation_index_id) if assembled.inflation_index_id is not None else None
        ),
        swap_kind=assembled.swap_kind,
        as_of=payload.as_of.isoformat(),
        snapshot_id=(str(payload.snapshot_id) if payload.snapshot_id is not None else None),
        duration_ms=round(duration_ms, 3),
        exc_type=type(exc).__name__,
    )


__all__ = [
    "InflationSwapPriceRequest",
    "InflationSwapPriceResponse",
    "InflationSwapResult",
    "router",
]
