"""``POST /v1/price/equity-option`` — the equity-option pricing endpoint.

Fifth
per-product endpoint following the swap_ir / swaption / bonds /
cds vertical:

1. Auth (``Depends(get_auth_context)``) — unauthenticated path
   on failure.
2. Assemble (``app_ro``) — load equity option / discount + dividend
   curves / equity Black-vol surface / snapshot from up to four
   ``app.*`` tables. The vol-surface ``kind = 'BlackVolSpec'``
   invariant is enforced here.
3. MD resolve — substitute every quote ID for its resolved value
   (curves + the spot canonical id; the spot's inline value branch
   short-circuits the lookup but the canonical id is still echoed).
   Snapshot pin first, live MD second. Equity options
   does NOT bypass MD — every leaf with a canonical id
   flows through the MD walker.
4. Fan out via the concurrency seam
   (``execute(policy, engine, [trade], price_batch=price_equity_option_batch,
   shared_inputs={})``). One trade today; the path through
   ``execute`` is wired so a portfolio endpoint is a one-line
   change.
5. Decode + respond with the typed
   :class:`EquityOptionPriceResponse`.

Failure surfaces:

* Auth: 401 ``unauthenticated``.
* DB engine missing: 503 ``storage_unavailable``.
* MD client missing: 503 ``md_client_unavailable``.
* Engine client missing: 503 ``engine_client_unavailable``.
* Saved option not visible: 404 ``equity_option_not_found``.
* Discount / dividend curve not visible: 404
  ``equity_option_discount_curve_not_found`` (role pinned in
  ``details`` refinement).
* Vol surface not visible: 404
  ``equity_option_vol_surface_not_found``.
* Vol surface kind wrong: 422
  ``equity_option_vol_surface_wrong_kind``.
* Snapshot not visible: 404 ``equity_option_snapshot_not_found``.
* Curve resolution: 422 ``equity_option_curve_resolution_failed``
  (same code for discount + dividend; role
  in ``details``).
* Surface resolution: 422
  ``equity_option_surface_resolution_failed``.
* Spot resolution: 422 ``equity_option_spot_resolution_failed``.
* Quote resolution: 422
  ``equity_option_quote_resolution_failed`` /
  502 ``md_unreachable`` / 502 ``md_upstream_error``.
* Shape past pydantic: 400 ``equity_option_invalid_request``.
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
from quantra_orchestrator.pricing.equity_options.assembler import (
    AssemblerOutput,
    assemble,
)
from quantra_orchestrator.pricing.equity_options.engine_io import (
    decode_equity_option_request_wire,
    price_equity_option_batch,
)
from quantra_orchestrator.pricing.equity_options.md_resolution import (
    collect_quote_ids,
)
from quantra_orchestrator.pricing.equity_options.md_resolution import (
    resolve as md_resolve,
)
from quantra_orchestrator.pricing.equity_options.models import (
    AssembledEquityOptionRequest,
    EquityOptionPriceRequest,
    EquityOptionPriceResponse,
    EquityOptionResult,
    ResolvedQuoteValue,
)
from quantra_orchestrator.pricing.history import record_pricing_call
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

_PRODUCT_KIND: str = "equity_options"


def _get_app_rw_engine_optional(request: Request) -> AsyncEngine | None:
    engine: AsyncEngine | None = getattr(request.app.state, "app_rw_engine", None)
    return engine


router = APIRouter(prefix="/v1/price", tags=["pricing:equity_options"])

_log = structlog.get_logger(__name__)

_COMMON_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"description": "Engine rejected the request as invalid."},
    401: {"description": "Missing or invalid credentials."},
    422: {
        "description": ("Curve, surface, spot, quote, or shape resolution failed (see ``code``).")
    },
    502: {"description": ("Engine or MD service unreachable / returned an error.")},
    503: {"description": ("Persistent storage, MD client, or engine client not configured.")},
    504: {"description": "Engine timed out."},
}


@router.post(
    "/equity-option",
    status_code=status.HTTP_200_OK,
    response_model=EquityOptionPriceResponse,
    summary="Price one equity option (saved by id or inline).",
    description=(
        'Accepts either ``{"equity_option_id": "...", '
        '"as_of": "..."}`` to price a saved '
        "``app.equity_options`` row or an inline "
        '``{"equity_option": {...}, "curves": [...], '
        '"vol_surface": {...}, "spot": {...}, '
        '"as_of": "..."}`` payload. The orchestrator resolves '
        "the referenced discount + dividend curves, equity Black-"
        "vol surface and underlier spot server-side and "
        "forwards a fully self-contained request to the pricing "
        "engine. Every market-data reference (including the spot "
        "quote) resolves through the market-data service — "
        "equity options never bypass it."
    ),
    responses={
        **_COMMON_RESPONSES,
        404: {
            "description": (
                "``equity_option_id`` / referenced ``vol_surface_id`` "
                "/ ``curve_id`` / ``snapshot_id`` not visible to the "
                "caller."
            )
        },
    },
)
async def price_equity_option(
    payload: EquityOptionPriceRequest,
    http_request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    ro_engine: AsyncEngine = Depends(get_app_ro_engine),
    rw_engine: AsyncEngine | None = Depends(_get_app_rw_engine_optional),
    md_client: MdClient = Depends(get_md_client),
    engine: EngineClient = Depends(get_engine_client),
    settings: OrchestratorSettings = Depends(get_orchestrator_settings),
) -> EquityOptionPriceResponse:
    """End-to-end equity-option pricing route. See module docstring for failures."""

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
            "equity_option_id": (
                str(payload.equity_option_id) if payload.equity_option_id is not None else None
            ),
            "as_of": payload.as_of.isoformat(),
            "snapshot_id": (str(payload.snapshot_id) if payload.snapshot_id is not None else None),
            "inline_equity_option": payload.equity_option is not None,
            "inline_curve_count": len(payload.curves) if payload.curves else 0,
        },
    )

    try:
        stage_started = time.monotonic()
        assembled = await assemble(request=payload, owner_uid=ctx.uid, ro_engine=ro_engine)
        record_load_entities(
            trace,
            {
                "discount_curve_id": (
                    str(assembled.discount_curve_id)
                    if assembled.discount_curve_id is not None
                    else None
                ),
                "dividend_curve_id": (
                    str(assembled.dividend_curve_id)
                    if assembled.dividend_curve_id is not None
                    else None
                ),
                "equity_surface_id": (
                    str(assembled.equity_surface_id)
                    if assembled.equity_surface_id is not None
                    else None
                ),
                "curve_names": [
                    assembled.discount_curve.name,
                    assembled.dividend_curve.name,
                ],
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
            curves=[assembled.discount_curve, assembled.dividend_curve],
            vol_surface=assembled.vol_surface,
            spot=assembled.spot,
            as_of=payload.as_of,
            snapshot=assembled.snapshot,
            md_client=md_client,
            snapshot_version=(assembled.snapshot.version_etag if assembled.snapshot else None),
        )
        record_md_resolve(
            trace,
            requested_canonical_ids=collect_quote_ids(
                [assembled.discount_curve, assembled.dividend_curve],
                assembled.vol_surface,
                assembled.spot,
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
        # resolved discount + dividend curves + quotes + BlackVol surface + spot
        # the stages above produced. The bundle is captured once at route scope
        # and passed into ``price_equity_option_batch`` by the ``price_batch``
        # lambda's closure, keeping the ``shared_inputs`` seam free
        # of large resolved graphs. The discount / dividend curve ids are tagged
        # by role so the translator + engine_io read each reference slot by
        # role; ``spot`` carries the underlier spot (inline value or resolved
        # canonical id).
        resolved_market_data = ResolvedMarketData(
            as_of=payload.as_of.isoformat(),
            curves=(assembled.discount_curve, assembled.dividend_curve),
            quotes=tuple(resolved_quotes),
            curve_roles={
                CurveRole.DISCOUNT: resolved_curve_id(assembled.discount_curve),
                CurveRole.DIVIDEND: resolved_curve_id(assembled.dividend_curve),
            },
            vol_surfaces=(assembled.vol_surface,),
            spot=assembled.spot,
        )

        policy_cls = resolve_policy(settings.concurrency_policy_equity_options)

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
                price_batch=lambda eng, batch: price_equity_option_batch(
                    eng, _coerce_batch(batch), resolved=resolved_market_data
                ),
                shared_inputs={},
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
            product_id=payload.equity_option_id,
            as_of=payload.as_of,
            request_payload=request_payload,
            response_payload=failure_envelope(exc),
        )
        record_history_write(trace, history_id, outcome="failure_row")
        raise

    duration_ms = (time.monotonic() - started_ms) * 1000.0
    _log.info(
        "orchestrator.pricing.equity_options.success",
        uid=ctx.uid,
        equity_option_id=(
            str(payload.equity_option_id) if payload.equity_option_id is not None else None
        ),
        equity_surface_id=(
            str(assembled.equity_surface_id) if assembled.equity_surface_id is not None else None
        ),
        discount_curve_id=(
            str(assembled.discount_curve_id) if assembled.discount_curve_id is not None else None
        ),
        dividend_curve_id=(
            str(assembled.dividend_curve_id) if assembled.dividend_curve_id is not None else None
        ),
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
        product_id=payload.equity_option_id,
        as_of=payload.as_of,
        request_payload=request_payload,
        response_payload=result.model_dump(mode="json"),
    )
    record_history_write(trace, history_id, outcome="success_row")

    return EquityOptionPriceResponse(
        pricing_history_id=str(history_id) if history_id is not None else None,
        assembled_request=assembled_request,
        result=result,
    )


def _record_engine_request_stage(
    trace: TraceRecorder,
    assembled_request: AssembledEquityOptionRequest,
    capturing_engine: CapturingEngineClient,
) -> None:
    """Emit the ``engine_request`` stage with both views (assembled + wire).

    Reads the exact transmitted bytes off the :class:`CapturingEngineClient`
    and decodes them with :func:`decode_equity_option_request_wire`. Delegates
    to the shared :func:`record_engine_request_wire` for the gate + ``sent:
    false`` pre-send handling.
    """

    record_engine_request_wire(
        trace,
        assembled_request=assembled_request.model_dump(mode="json"),
        capturing_engine=capturing_engine,
        decode=decode_equity_option_request_wire,
    )


def _coerce_batch(batch: object) -> EngineBatch[Any]:
    """Cast the runner-supplied batch into the typed shape the batch translator wants."""

    if not isinstance(batch, EngineBatch):
        msg = f"equity_options.price_batch received a non-EngineBatch: {type(batch).__name__}"
        raise TypeError(msg)
    return batch


def _build_assembled_request(
    *,
    request: EquityOptionPriceRequest,
    assembled: AssemblerOutput,
    resolved_quotes: list[ResolvedQuoteValue],
) -> AssembledEquityOptionRequest:
    return AssembledEquityOptionRequest(
        as_of=request.as_of,
        snapshot_id=request.snapshot_id,
        discount_curve_id=assembled.discount_curve_id,
        dividend_curve_id=assembled.dividend_curve_id,
        equity_surface_id=assembled.equity_surface_id,
        trade=assembled.trade,
        discount_curve=assembled.discount_curve,
        dividend_curve=assembled.dividend_curve,
        vol_surface=assembled.vol_surface,
        spot=assembled.spot,
        resolved_quotes=resolved_quotes,
    )


def _attach_assembled_details(
    exc: HTTPException, assembled_request: AssembledEquityOptionRequest
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
    assembled_request: AssembledEquityOptionRequest,
    ctx: AuthContext,
    payload: EquityOptionPriceRequest,
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
    payload: EquityOptionPriceRequest,
    assembled: AssemblerOutput,
    started_ms: float,
    exc: BaseException,
) -> None:
    duration_ms = (time.monotonic() - started_ms) * 1000.0
    _log.warning(
        "orchestrator.pricing.equity_options.failure",
        uid=ctx.uid,
        equity_option_id=(
            str(payload.equity_option_id) if payload.equity_option_id is not None else None
        ),
        equity_surface_id=(
            str(assembled.equity_surface_id) if assembled.equity_surface_id is not None else None
        ),
        discount_curve_id=(
            str(assembled.discount_curve_id) if assembled.discount_curve_id is not None else None
        ),
        dividend_curve_id=(
            str(assembled.dividend_curve_id) if assembled.dividend_curve_id is not None else None
        ),
        as_of=payload.as_of.isoformat(),
        snapshot_id=(str(payload.snapshot_id) if payload.snapshot_id is not None else None),
        duration_ms=round(duration_ms, 3),
        exc_type=type(exc).__name__,
    )


__all__ = [
    "EquityOptionPriceRequest",
    "EquityOptionPriceResponse",
    "EquityOptionResult",
    "router",
]
