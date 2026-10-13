"""``POST /v1/price/fx-forward`` — the FX-forward pricing endpoint (engine >= 0.9.0).

Same vertical as ``/v1/price/swap/ir``: auth → assemble (``app_ro``) → MD
resolve (curve quotes + FX spot quotes, server-side) → fan out via the
concurrency seam → decode → history row + trace. Failure surface:

* 401 ``unauthenticated``; 503 ``storage_unavailable`` /
  ``md_client_unavailable`` / ``engine_client_unavailable``.
* 404 ``fx_forward_not_found`` / ``fx_forward_snapshot_not_found``.
* 422 ``fx_forward_curve_resolution_failed`` /
  ``fx_forward_fx_spot_resolution_failed`` /
  ``fx_forward_index_resolution_failed`` /
  ``fx_forward_quote_resolution_failed`` /
  ``fx_forward_missing_trade_fields``.
* Engine: 502 / 504 / 400 via :func:`map_engine_client_error`, with the
  assembled request echoed in ``details``.
"""

from __future__ import annotations

import time
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_common.auth.context import AuthContext
from quantra_common.engine_client import CapturingEngineClient, EngineClient
from quantra_common.md_client import MdClient
from quantra_orchestrator.auth.dependencies import get_auth_context
from quantra_orchestrator.data.engines import get_app_ro_engine
from quantra_orchestrator.engine import get_engine_client, map_engine_client_error
from quantra_orchestrator.md import get_md_client
from quantra_orchestrator.pricing._translator import ResolvedMarketData
from quantra_orchestrator.pricing.concurrency import EngineBatch, execute, resolve_policy
from quantra_orchestrator.pricing.fx_forward.assembler import assemble
from quantra_orchestrator.pricing.fx_forward.engine_io import (
    decode_fx_forward_request_wire,
    price_fx_forward_batch,
)
from quantra_orchestrator.pricing.fx_forward.md_resolution import collect_quote_ids
from quantra_orchestrator.pricing.fx_forward.md_resolution import resolve as md_resolve
from quantra_orchestrator.pricing.fx_forward.models import (
    AssembledFxForwardRequest,
    FxForwardPriceRequest,
    FxForwardPriceResponse,
    FxForwardResult,
)
from quantra_orchestrator.pricing.history import record_pricing_call
from quantra_orchestrator.settings import OrchestratorSettings, get_orchestrator_settings
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

_PRODUCT_KIND: str = "fx_forwards"

router = APIRouter(prefix="/v1/price/fx-forward", tags=["pricing:fx-forward"])
_log = structlog.get_logger(__name__)


def _get_app_rw_engine_optional(request: Request) -> AsyncEngine | None:
    engine: AsyncEngine | None = getattr(request.app.state, "app_rw_engine", None)
    return engine


@router.post(
    "",
    status_code=status.HTTP_200_OK,
    response_model=FxForwardPriceResponse,
    summary="Price one FX forward (saved by id or inline).",
    description=(
        'Accepts ``{"fx_forward_id": "...", "as_of": "..."}`` to price a saved '
        '``app.fx_forwards`` row, or an inline ``{"fx_forward": {...}, "curves": '
        '[...], "fx_spots": [...], "indices": [...], "as_of": "..."}`` payload. '
        "Curves / FX spots / indices are refs into ``app.*`` or inline; every "
        "``quote_id`` (curve points and FX spots) is resolved server-side at "
        "``as_of``. The trade names its spot (``fx_spot``) and one discounting curve "
        "per currency (``base_discounting_curve`` / ``quote_discounting_curve``) by "
        "resolved id or name; ``reporting_currency`` defaults to the quote currency."
    ),
    responses={
        400: {"description": "Engine rejected the request as invalid."},
        401: {"description": "Missing or invalid credentials."},
        404: {"description": "``fx_forward_id`` / ``snapshot_id`` not visible to the caller."},
        422: {"description": "Curve / FX-spot / index / quote resolution or trade-field failure."},
        502: {"description": "Engine or MD service unreachable / returned an error."},
        503: {"description": "Storage, MD client, or engine client not configured."},
        504: {"description": "Engine timed out."},
    },
)
async def price_fx_forward(
    payload: FxForwardPriceRequest,
    http_request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    ro_engine: AsyncEngine = Depends(get_app_ro_engine),
    rw_engine: AsyncEngine | None = Depends(_get_app_rw_engine_optional),
    md_client: MdClient = Depends(get_md_client),
    engine: EngineClient = Depends(get_engine_client),
    settings: OrchestratorSettings = Depends(get_orchestrator_settings),
) -> FxForwardPriceResponse:
    started_ms = time.monotonic()
    request_payload = payload.model_dump(mode="json")
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
        record_load_entities(
            trace,
            {
                "curve_ids": [str(c.id) for c in assembled.curves if c.id is not None],
                "curve_names": [c.name for c in assembled.curves],
                "fx_spot_ids": [str(s.id) for s in assembled.fx_spots if s.id is not None],
                "fx_spot_names": [s.name for s in assembled.fx_spots],
                "index_names": [i.name for i in assembled.indices],
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
            fx_spots=assembled.fx_spots,
            as_of=payload.as_of,
            snapshot=assembled.snapshot,
            md_client=md_client,
            snapshot_version=(assembled.snapshot.version_etag if assembled.snapshot else None),
        )
        record_md_resolve(
            trace,
            requested_canonical_ids=collect_quote_ids(assembled.curves, assembled.fx_spots),
            resolved_quotes=resolved_quotes,
            duration_ms=elapsed_ms(stage_started),
        )

        assembled_request = AssembledFxForwardRequest(
            as_of=payload.as_of,
            snapshot_id=payload.snapshot_id,
            trade=assembled.trade,
            curves=assembled.curves,
            fx_spots=assembled.fx_spots,
            indices=assembled.indices,
            resolved_quotes=resolved_quotes,
        )
        resolved_market_data = ResolvedMarketData(
            as_of=payload.as_of.isoformat(),
            curves=tuple(assembled.curves),
            quotes=tuple(resolved_quotes),
            indices=tuple(assembled.indices),
            fx_spots=tuple(assembled.fx_spots),
        )

        policy_cls = resolve_policy(settings.concurrency_policy_swap_ir)
        capturing_engine = CapturingEngineClient(engine)
        engine_started = time.monotonic()
        try:
            results = await execute(
                policy_cls(),
                capturing_engine,
                trades=[assembled.trade],
                price_batch=lambda eng, batch: price_fx_forward_batch(
                    eng, _coerce_batch(batch), resolved=resolved_market_data
                ),
                shared_inputs={},
            )
        except HTTPException:
            _record_engine_request_stage(trace, assembled_request, capturing_engine)
            raise
        except Exception as exc:
            _record_engine_request_stage(trace, assembled_request, capturing_engine)
            record_engine_error(trace, exc, duration_ms=elapsed_ms(engine_started))
            mapped = map_engine_client_error(exc)
            _attach_assembled_details(mapped, assembled_request)
            _log.warning(
                "orchestrator.pricing.fx_forward.failure",
                uid=ctx.uid,
                fx_forward_id=(
                    str(payload.fx_forward_id) if payload.fx_forward_id is not None else None
                ),
                as_of=payload.as_of.isoformat(),
                duration_ms=round((time.monotonic() - started_ms) * 1000.0, 3),
                exc_type=type(exc).__name__,
            )
            raise mapped from exc

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
            product_id=payload.fx_forward_id,
            as_of=payload.as_of,
            request_payload=request_payload,
            response_payload=failure_envelope(exc),
        )
        record_history_write(trace, history_id, outcome="failure_row")
        raise

    result = results[0]
    _log.info(
        "orchestrator.pricing.fx_forward.success",
        uid=ctx.uid,
        fx_forward_id=str(payload.fx_forward_id) if payload.fx_forward_id is not None else None,
        as_of=payload.as_of.isoformat(),
        resolved_quote_count=len(resolved_quotes),
        npv=result.npv,
        duration_ms=round((time.monotonic() - started_ms) * 1000.0, 3),
    )
    record_engine_response(
        trace, result.model_dump(mode="json"), duration_ms=elapsed_ms(engine_started)
    )
    history_id = await record_pricing_call(
        rw_engine=rw_engine,
        owner_uid=ctx.uid,
        product_kind=_PRODUCT_KIND,
        product_id=payload.fx_forward_id,
        as_of=payload.as_of,
        request_payload=request_payload,
        response_payload=result.model_dump(mode="json"),
    )
    record_history_write(trace, history_id, outcome="success_row")
    return FxForwardPriceResponse(
        pricing_history_id=str(history_id) if history_id is not None else None,
        assembled_request=assembled_request,
        result=result,
    )


def _coerce_batch(batch: object) -> EngineBatch[Any]:
    if not isinstance(batch, EngineBatch):
        msg = f"price_fx_forward_batch received a non-EngineBatch: {type(batch).__name__}"
        raise TypeError(msg)
    return batch


def _record_engine_request_stage(
    trace: TraceRecorder,
    assembled_request: AssembledFxForwardRequest,
    capturing_engine: CapturingEngineClient,
) -> None:
    record_engine_request_wire(
        trace,
        assembled_request=assembled_request.model_dump(mode="json"),
        capturing_engine=capturing_engine,
        decode=decode_fx_forward_request_wire,
    )


def _attach_assembled_details(
    exc: HTTPException, assembled_request: AssembledFxForwardRequest
) -> None:
    detail_entry: dict[str, Any] = {
        "assembled_request": assembled_request.model_dump(mode="json"),
    }
    existing = getattr(exc, "details", None)
    if isinstance(existing, list):
        exc.details = [*existing, detail_entry]  # type: ignore[attr-defined]
    else:
        exc.details = [detail_entry]  # type: ignore[attr-defined]


def _record_input(trace: TraceRecorder, payload: FxForwardPriceRequest) -> None:
    params: dict[str, Any] = {
        "fx_forward_id": (
            str(payload.fx_forward_id) if payload.fx_forward_id is not None else None
        ),
        "as_of": payload.as_of.isoformat(),
        "snapshot_id": (str(payload.snapshot_id) if payload.snapshot_id is not None else None),
        "inline_fx_forward": payload.fx_forward is not None,
        "inline_curve_count": len(payload.curves) if payload.curves else 0,
        "inline_fx_spot_count": len(payload.fx_spots) if payload.fx_spots else 0,
    }
    body = payload.fx_forward or {}
    for key in (
        "base_currency",
        "quote_currency",
        "direction",
        "base_notional",
        "notional",
        "forward_rate",
        "maturity_date",
        "reporting_currency",
    ):
        value = body.get(key)
        if value is not None:
            params[key] = value
    record_input(trace, product=_PRODUCT_KIND, params=params)


__all__ = ["FxForwardPriceRequest", "FxForwardPriceResponse", "FxForwardResult", "router"]
