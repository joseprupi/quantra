"""``POST /v1/price/bonds/{fixed,floating}`` — the bonds pricing endpoints.

Two routes from one package.
The fixed and floating handlers share every helper (envelope
attachment, structured-log shape, batch-coerce wrapper) and diverge
in exactly four places per route: the assembler entry point, the
``md_resolve`` curve list (single curve for fixed; deduplicated
discount + projection for floating), the engine RPC translator, and
the assembled-request model constructor.

End-to-end vertical (per-route):

1. Auth (``Depends(get_auth_context)``) — unauthenticated path on
   failure.
2. Assemble (``app_ro``) — load bond / curve(s) / index (floating) /
   snapshot from up to four ``app.*`` tables.
3. MD resolve — substitute every quote ID for its resolved value
   (snapshot pin first, live MD second). For floating bonds the
   walker traverses both the discount and projection curves
   (deduplicated by ``app.curves.id`` so ``use_same_curve`` is a
   single walk). Server-side.
4. Fan out via the concurrency seam
   (``execute(policy, engine, [trade], price_batch=price_{fixed,floating}_bond_batch)``).
   One trade today; the path through ``execute`` is wired so a
   portfolio endpoint is a one-line change.
5. Decode + respond with the typed
   :class:`{Fixed,Floating}BondPriceResponse`.

Failure surfaces (shared between fixed + floating except where noted):

* Auth: 401 ``unauthenticated``.
* DB engine missing: 503 ``storage_unavailable`` (data layer 503).
* MD client missing: 503 ``md_client_unavailable``.
* Engine client missing: 503 ``engine_client_unavailable``.
* Saved bond not visible: 404 ``bond_fixed_not_found`` /
  ``bond_floating_not_found``.
* Discount curve not visible: 404 ``bond_discount_curve_not_found``.
* Projection curve not visible (floating-only): 404
  ``bond_projection_curve_not_found``.
* Index not visible (floating-only): 404 ``bond_index_not_found``.
* Snapshot not visible: 404 ``bond_snapshot_not_found``.
* Curve resolution: 422 ``bond_curve_resolution_failed`` (the error-code convention; one
  code shared between the discount-curve and projection-curve
  resolution stages — ``details`` carries which role failed and why).
* Quote resolution: 422 ``bond_quote_resolution_failed`` /
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
    EngineDateCoherenceError,
    get_engine_client,
    map_engine_client_error,
)
from quantra_orchestrator.md import get_md_client
from quantra_orchestrator.pricing._translator import (
    CurveRole,
    ResolvedMarketData,
    resolved_curve_id,
)
from quantra_orchestrator.pricing.bonds.assembler import (
    FixedAssemblerOutput,
    FloatingAssemblerOutput,
    assemble_fixed,
    assemble_floating,
    collect_curves,
)
from quantra_orchestrator.pricing.bonds.engine_io import (
    decode_fixed_bond_request_wire,
    decode_floating_bond_request_wire,
    price_fixed_bond_batch,
    price_floating_bond_batch,
)
from quantra_orchestrator.pricing.bonds.md_resolution import collect_quote_ids
from quantra_orchestrator.pricing.bonds.md_resolution import resolve as md_resolve
from quantra_orchestrator.pricing.bonds.models import (
    AssembledFixedBondRequest,
    AssembledFloatingBondRequest,
    FixedBondPriceRequest,
    FixedBondPriceResponse,
    FixedBondResult,
    FloatingBondPriceRequest,
    FloatingBondPriceResponse,
    FloatingBondResult,
    ResolvedQuoteValue,
)
from quantra_orchestrator.pricing.concurrency import (
    EngineBatch,
    execute,
    resolve_policy,
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

_PRODUCT_KIND_FIXED: str = "bonds_fixed"
_PRODUCT_KIND_FLOATING: str = "bonds_floating"


def _get_app_rw_engine_optional(request: Request) -> AsyncEngine | None:
    engine: AsyncEngine | None = getattr(request.app.state, "app_rw_engine", None)
    return engine


router = APIRouter(prefix="/v1/price/bonds", tags=["pricing:bonds"])

_log = structlog.get_logger(__name__)

_COMMON_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"description": "Engine rejected the request as invalid."},
    401: {"description": "Missing or invalid credentials."},
    422: {"description": ("Curve, quote, or shape resolution failed (see ``code``).")},
    502: {"description": ("Engine or MD service unreachable / returned an error.")},
    503: {"description": ("Persistent storage, MD client, or engine client not configured.")},
    504: {"description": "Engine timed out."},
}


# ---------------------------------------------------------------------------
# Fixed-rate bond route
# ---------------------------------------------------------------------------


@router.post(
    "/fixed",
    status_code=status.HTTP_200_OK,
    response_model=FixedBondPriceResponse,
    summary="Price one fixed-rate bond (saved by id or inline).",
    description=(
        'Accepts either ``{"bond_id": "...", "as_of": "..."}`` to '
        'price a saved ``app.bonds_fixed`` row or an inline ``{"bond": '
        '{...}, "curves": [...], "as_of": "..."}`` payload. The '
        "orchestrator resolves the referenced discount curve and every "
        "quote ID server-side and forwards a fully self-contained "
        "request to the pricing engine. If no engine backend is "
        "configured the call fails with an ``engine_unavailable`` "
        "error envelope that includes the assembled request in "
        "``details`` so the resolution path is verifiable."
    ),
    responses={
        **_COMMON_RESPONSES,
        404: {
            "description": (
                "``bond_id`` / referenced ``discount_curve_id`` / "
                "``snapshot_id`` not visible to the caller."
            )
        },
    },
)
async def price_bond_fixed(
    payload: FixedBondPriceRequest,
    http_request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    ro_engine: AsyncEngine = Depends(get_app_ro_engine),
    rw_engine: AsyncEngine | None = Depends(_get_app_rw_engine_optional),
    md_client: MdClient = Depends(get_md_client),
    engine: EngineClient = Depends(get_engine_client),
    settings: OrchestratorSettings = Depends(get_orchestrator_settings),
) -> FixedBondPriceResponse:
    """End-to-end fixed-rate pricing route. See module docstring for failures."""

    started_ms = time.monotonic()
    request_payload = payload.model_dump(mode="json")

    # in-app pricing trace (best-effort, off the hot path).
    trace = start_trace(
        http_request,
        owner_uid=ctx.uid,
        rw_engine=rw_engine,
        settings=settings,
        product=_PRODUCT_KIND_FIXED,
    )
    record_input(
        trace,
        product=_PRODUCT_KIND_FIXED,
        params={
            "bond_id": str(payload.bond_id) if payload.bond_id is not None else None,
            "as_of": payload.as_of.isoformat(),
            "snapshot_id": (str(payload.snapshot_id) if payload.snapshot_id is not None else None),
            "inline_bond": payload.bond is not None,
            "inline_curve_count": len(payload.curves) if payload.curves else 0,
        },
    )

    try:
        stage_started = time.monotonic()
        assembled = await assemble_fixed(request=payload, owner_uid=ctx.uid, ro_engine=ro_engine)
        record_load_entities(
            trace,
            {
                "curve_set_id": (
                    str(assembled.curve_set_id) if assembled.curve_set_id is not None else None
                ),
                "discount_curve_id": (
                    str(assembled.discount_curve_id)
                    if assembled.discount_curve_id is not None
                    else None
                ),
                "discount_curve_name": assembled.discount_curve.name,
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
            curves=[assembled.discount_curve],
            as_of=payload.as_of,
            snapshot=assembled.snapshot,
            md_client=md_client,
            snapshot_version=(assembled.snapshot.version_etag if assembled.snapshot else None),
        )
        record_md_resolve(
            trace,
            requested_canonical_ids=collect_quote_ids([assembled.discount_curve]),
            resolved_quotes=resolved_quotes,
            duration_ms=elapsed_ms(stage_started),
        )

        assembled_request = _build_fixed_assembled_request(
            request=payload, assembled=assembled, resolved_quotes=resolved_quotes
        )

        # faithful request assembled from the resolved
        # discount curve + quotes, captured at route scope and passed into
        # ``price_fixed_bond_batch`` by the ``price_batch`` lambda's closure.
        # The discount curve is tagged with its role so the per-trade
        # ``discountingCurve`` ref honours the resolved entity id.
        resolved_market_data = ResolvedMarketData(
            as_of=payload.as_of.isoformat(),
            curves=(assembled.discount_curve,),
            quotes=tuple(resolved_quotes),
            curve_roles={CurveRole.DISCOUNT: resolved_curve_id(assembled.discount_curve)},
        )

        policy_cls = resolve_policy(settings.concurrency_policy_bonds_fixed)

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
                price_batch=lambda eng, batch: price_fixed_bond_batch(
                    eng, _coerce_batch(batch), resolved=resolved_market_data
                ),
                shared_inputs={},
            )
        except HTTPException:
            # Pre-send failure — no bytes on the wire → sent: false.
            _record_fixed_engine_request_stage(trace, assembled_request, capturing_engine)
            raise
        except Exception as exc:
            # Any engine-side failure (transport, NotImplementedError, decode
            # errors, ...) gets the same structured surface, so the caller
            # never sees the orchestrator's generic 500 handler when the
            # engine round-trip is the failing link.
            _record_fixed_engine_request_stage(trace, assembled_request, capturing_engine)
            raise _on_engine_failure_fixed(
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
        _record_fixed_engine_request_stage(trace, assembled_request, capturing_engine)

        if not results:
            msg = "engine returned no results for one-trade batch"
            mapped = map_engine_client_error(RuntimeError(msg))
            _attach_assembled_details_fixed(mapped, assembled_request)
            raise mapped
    except HTTPException as exc:
        record_error_stage(trace, exc)
        history_id = await record_pricing_call(
            rw_engine=rw_engine,
            owner_uid=ctx.uid,
            product_kind=_PRODUCT_KIND_FIXED,
            product_id=payload.bond_id,
            as_of=payload.as_of,
            request_payload=request_payload,
            response_payload=failure_envelope(exc),
        )
        record_history_write(trace, history_id, outcome="failure_row")
        raise

    duration_ms = (time.monotonic() - started_ms) * 1000.0
    _log.info(
        "orchestrator.pricing.bonds_fixed.success",
        uid=ctx.uid,
        bond_id=str(payload.bond_id) if payload.bond_id is not None else None,
        curve_set_id=(str(assembled.curve_set_id) if assembled.curve_set_id is not None else None),
        discount_curve_id=(
            str(assembled.discount_curve_id) if assembled.discount_curve_id is not None else None
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
        product_kind=_PRODUCT_KIND_FIXED,
        product_id=payload.bond_id,
        as_of=payload.as_of,
        request_payload=request_payload,
        response_payload=result.model_dump(mode="json"),
    )
    record_history_write(trace, history_id, outcome="success_row")

    return FixedBondPriceResponse(
        pricing_history_id=str(history_id) if history_id is not None else None,
        assembled_request=assembled_request,
        result=result,
    )


# ---------------------------------------------------------------------------
# Floating-rate bond route
# ---------------------------------------------------------------------------


@router.post(
    "/floating",
    status_code=status.HTTP_200_OK,
    response_model=FloatingBondPriceResponse,
    summary="Price one floating-rate bond (saved by id or inline).",
    description=(
        'Accepts either ``{"bond_id": "...", "as_of": "..."}`` to '
        "price a saved ``app.bonds_floating`` row or an inline payload "
        "with ``bond`` + ``curves`` (≥ 1, role-tagged or position-"
        "ordered) + ``index``. The orchestrator resolves the discount "
        "curve, the projection curve, the projection index, and every "
        "quote ID server-side. If no engine backend is configured the "
        "call fails with an ``engine_unavailable`` error envelope that "
        "includes the assembled request in ``details``."
    ),
    responses={
        **_COMMON_RESPONSES,
        404: {
            "description": (
                "``bond_id`` / referenced discount / projection curve / "
                "index / snapshot not visible to the caller."
            )
        },
    },
)
async def price_bond_floating(
    # pipeline: discount + projection + index resolution plus the pricing-trace
    # per-stage trace instrumentation. The stage recording itself is
    # already factored into shared helpers; the remaining length is
    # irreducible orchestration, not duplication.
    payload: FloatingBondPriceRequest,
    http_request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    ro_engine: AsyncEngine = Depends(get_app_ro_engine),
    rw_engine: AsyncEngine | None = Depends(_get_app_rw_engine_optional),
    md_client: MdClient = Depends(get_md_client),
    engine: EngineClient = Depends(get_engine_client),
    settings: OrchestratorSettings = Depends(get_orchestrator_settings),
) -> FloatingBondPriceResponse:
    """End-to-end floating-rate pricing route. See module docstring for failures."""

    started_ms = time.monotonic()
    request_payload = payload.model_dump(mode="json")

    # in-app pricing trace (best-effort, off the hot path).
    trace = start_trace(
        http_request,
        owner_uid=ctx.uid,
        rw_engine=rw_engine,
        settings=settings,
        product=_PRODUCT_KIND_FLOATING,
    )
    record_input(
        trace,
        product=_PRODUCT_KIND_FLOATING,
        params={
            "bond_id": str(payload.bond_id) if payload.bond_id is not None else None,
            "as_of": payload.as_of.isoformat(),
            "snapshot_id": (str(payload.snapshot_id) if payload.snapshot_id is not None else None),
            "inline_bond": payload.bond is not None,
            "inline_curve_count": len(payload.curves) if payload.curves else 0,
        },
    )

    try:
        stage_started = time.monotonic()
        assembled = await assemble_floating(request=payload, owner_uid=ctx.uid, ro_engine=ro_engine)
        # Deduplicate by ``app.curves.id`` so a ``use_same_curve`` bond
        # only walks each point list once. ``collect_curves`` preserves
        # input order so the resolved-quotes echo stays stable.
        md_curves = collect_curves(assembled.discount_curve, assembled.projection_curve)
        record_load_entities(
            trace,
            {
                "curve_set_id": (
                    str(assembled.curve_set_id) if assembled.curve_set_id is not None else None
                ),
                "discount_curve_id": (
                    str(assembled.discount_curve_id)
                    if assembled.discount_curve_id is not None
                    else None
                ),
                "projection_curve_id": (
                    str(assembled.projection_curve_id)
                    if assembled.projection_curve_id is not None
                    else None
                ),
                "index_id": (str(assembled.index_id) if assembled.index_id is not None else None),
                "curve_names": [c.name for c in md_curves],
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
            curves=md_curves,
            as_of=payload.as_of,
            snapshot=assembled.snapshot,
            md_client=md_client,
            snapshot_version=(assembled.snapshot.version_etag if assembled.snapshot else None),
        )
        record_md_resolve(
            trace,
            requested_canonical_ids=collect_quote_ids(md_curves),
            resolved_quotes=resolved_quotes,
            duration_ms=elapsed_ms(stage_started),
        )

        assembled_request = _build_floating_assembled_request(
            request=payload, assembled=assembled, resolved_quotes=resolved_quotes
        )

        # faithful request assembled from the resolved
        # discount + projection curves (deduplicated for ``use_same_curve``), the
        # resolved projection index, and quotes. The curves are tagged with their
        # roles so the per-trade ``discountingCurve`` / ``forwardingCurve``
        # / ``index`` refs honour the resolved entity ids.
        resolved_market_data = ResolvedMarketData(
            as_of=payload.as_of.isoformat(),
            curves=tuple(md_curves),
            quotes=tuple(resolved_quotes),
            curve_roles={
                CurveRole.DISCOUNT: resolved_curve_id(assembled.discount_curve),
                CurveRole.PROJECTION: resolved_curve_id(assembled.projection_curve),
            },
            index=assembled.index,
        )

        policy_cls = resolve_policy(settings.concurrency_policy_bonds_floating)

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
                price_batch=lambda eng, batch: price_floating_bond_batch(
                    eng, _coerce_batch(batch), resolved=resolved_market_data
                ),
                shared_inputs={},
            )
        except HTTPException:
            # Pre-send failure — no bytes on the wire → sent: false.
            _record_floating_engine_request_stage(trace, assembled_request, capturing_engine)
            raise
        except Exception as exc:
            # Any engine-side failure (transport, NotImplementedError, decode
            # errors, ...) gets the same structured surface, so the caller
            # never sees the orchestrator's generic 500 handler when the
            # engine round-trip is the failing link.
            _record_floating_engine_request_stage(trace, assembled_request, capturing_engine)
            raise _on_engine_failure_floating(
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
        _record_floating_engine_request_stage(trace, assembled_request, capturing_engine)

        if not results:
            msg = "engine returned no results for one-trade batch"
            mapped = map_engine_client_error(RuntimeError(msg))
            _attach_assembled_details_floating(mapped, assembled_request)
            raise mapped
    except HTTPException as exc:
        record_error_stage(trace, exc)
        history_id = await record_pricing_call(
            rw_engine=rw_engine,
            owner_uid=ctx.uid,
            product_kind=_PRODUCT_KIND_FLOATING,
            product_id=payload.bond_id,
            as_of=payload.as_of,
            request_payload=request_payload,
            response_payload=failure_envelope(exc),
        )
        record_history_write(trace, history_id, outcome="failure_row")
        raise

    duration_ms = (time.monotonic() - started_ms) * 1000.0
    _log.info(
        "orchestrator.pricing.bonds_floating.success",
        uid=ctx.uid,
        bond_id=str(payload.bond_id) if payload.bond_id is not None else None,
        curve_set_id=(str(assembled.curve_set_id) if assembled.curve_set_id is not None else None),
        discount_curve_id=(
            str(assembled.discount_curve_id) if assembled.discount_curve_id is not None else None
        ),
        projection_curve_id=(
            str(assembled.projection_curve_id)
            if assembled.projection_curve_id is not None
            else None
        ),
        index_id=(str(assembled.index_id) if assembled.index_id is not None else None),
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
        product_kind=_PRODUCT_KIND_FLOATING,
        product_id=payload.bond_id,
        as_of=payload.as_of,
        request_payload=request_payload,
        response_payload=result.model_dump(mode="json"),
    )
    record_history_write(trace, history_id, outcome="success_row")

    return FloatingBondPriceResponse(
        pricing_history_id=str(history_id) if history_id is not None else None,
        assembled_request=assembled_request,
        result=result,
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _record_fixed_engine_request_stage(
    trace: TraceRecorder,
    assembled_request: AssembledFixedBondRequest,
    capturing_engine: CapturingEngineClient,
) -> None:
    """Emit the ``engine_request`` stage (assembled + wire) for fixed bonds.

    Reads the exact transmitted bytes off the :class:`CapturingEngineClient`
    and decodes them with :func:`decode_fixed_bond_request_wire`. Delegates to
    the shared :func:`record_engine_request_wire` for the gate + ``sent: false``
    pre-send handling.
    """

    record_engine_request_wire(
        trace,
        assembled_request=assembled_request.model_dump(mode="json"),
        capturing_engine=capturing_engine,
        decode=decode_fixed_bond_request_wire,
    )


def _record_floating_engine_request_stage(
    trace: TraceRecorder,
    assembled_request: AssembledFloatingBondRequest,
    capturing_engine: CapturingEngineClient,
) -> None:
    """Emit the ``engine_request`` stage (assembled + wire) for floating bonds.

    Reads the exact transmitted bytes off the :class:`CapturingEngineClient`
    and decodes them with :func:`decode_floating_bond_request_wire`. Delegates
    to the shared :func:`record_engine_request_wire` for the gate + ``sent:
    false`` pre-send handling.
    """

    record_engine_request_wire(
        trace,
        assembled_request=assembled_request.model_dump(mode="json"),
        capturing_engine=capturing_engine,
        decode=decode_floating_bond_request_wire,
    )


def _coerce_batch(batch: object) -> EngineBatch[Any]:
    """Cast the runner-supplied batch into the typed shape the batch translator wants.

    The ``execute`` runner is generic over ``T``; the lambdas
    above close over the actual ``{Fixed,Floating}BondTrade`` type.
    Re-asserting the shape here keeps mypy happy without disabling
    strictness in the route module.
    """

    if not isinstance(batch, EngineBatch):
        msg = f"bonds.price_batch received a non-EngineBatch: {type(batch).__name__}"
        raise TypeError(msg)
    return batch


def _build_fixed_assembled_request(
    *,
    request: FixedBondPriceRequest,
    assembled: FixedAssemblerOutput,
    resolved_quotes: list[ResolvedQuoteValue],
) -> AssembledFixedBondRequest:
    return AssembledFixedBondRequest(
        as_of=request.as_of,
        snapshot_id=request.snapshot_id,
        curve_set_id=assembled.curve_set_id,
        discount_curve_id=assembled.discount_curve_id,
        trade=assembled.trade,
        discount_curve=assembled.discount_curve,
        resolved_quotes=resolved_quotes,
    )


def _build_floating_assembled_request(
    *,
    request: FloatingBondPriceRequest,
    assembled: FloatingAssemblerOutput,
    resolved_quotes: list[ResolvedQuoteValue],
) -> AssembledFloatingBondRequest:
    return AssembledFloatingBondRequest(
        as_of=request.as_of,
        snapshot_id=request.snapshot_id,
        curve_set_id=assembled.curve_set_id,
        discount_curve_id=assembled.discount_curve_id,
        projection_curve_id=assembled.projection_curve_id,
        index_id=assembled.index_id,
        trade=assembled.trade,
        discount_curve=assembled.discount_curve,
        projection_curve=assembled.projection_curve,
        index=assembled.index,
        resolved_quotes=resolved_quotes,
    )


def _attach_assembled_details_fixed(
    exc: HTTPException, assembled_request: AssembledFixedBondRequest
) -> None:
    """Stash the assembled fixed-bond request on the HTTPException for error ``details``.

    Mirrors the swap_ir / swaption pattern. The global handler
    reads ``getattr(exc, 'details', None)`` and forwards it into the
    envelope. Appends rather than replaces so any pre-populated
    details (none today) are preserved.
    """

    _attach_assembled_details(exc, assembled_request.model_dump(mode="json"))


def _attach_assembled_details_floating(
    exc: HTTPException, assembled_request: AssembledFloatingBondRequest
) -> None:
    """Stash the assembled floating-bond request on the HTTPException."""

    _attach_assembled_details(exc, assembled_request.model_dump(mode="json"))


def _attach_assembled_details(exc: HTTPException, dumped_request: dict[str, Any]) -> None:
    detail_entry: dict[str, Any] = {"assembled_request": dumped_request}
    existing = getattr(exc, "details", None)
    if isinstance(existing, list):
        exc.details = [*existing, detail_entry]  # type: ignore[attr-defined]
    else:
        exc.details = [detail_entry]  # type: ignore[attr-defined]


def _on_engine_failure_fixed(
    trace: TraceRecorder,
    exc: Exception,
    engine_started: float,
    assembled_request: AssembledFixedBondRequest,
    ctx: AuthContext,
    payload: FixedBondPriceRequest,
    assembled: FixedAssemblerOutput,
    started_ms: float,
) -> HTTPException:
    """Record + map + log one fixed-bond engine-side failure; return the error."""

    record_engine_error(trace, exc, duration_ms=elapsed_ms(engine_started))
    mapped = map_engine_client_error(exc)
    # A "negative time" abort is the cross-source As-Of/date mismatch (the
    # default As-Of can predate the auto-rolled curve date). The boundary
    # mapper produced the generic typed 422; this route HAS the resolved
    # curve, so upgrade message + details to name the as-of + the curve's
    # reference date (actionable error-quality contract).
    if isinstance(mapped, EngineDateCoherenceError):
        mapped.add_date_context(as_of=payload.as_of.isoformat(), curves=[assembled.discount_curve])
    _attach_assembled_details_fixed(mapped, assembled_request)
    _log_failure_fixed(
        ctx=ctx, payload=payload, assembled=assembled, started_ms=started_ms, exc=exc
    )
    return mapped


def _on_engine_failure_floating(
    trace: TraceRecorder,
    exc: Exception,
    engine_started: float,
    assembled_request: AssembledFloatingBondRequest,
    ctx: AuthContext,
    payload: FloatingBondPriceRequest,
    assembled: FloatingAssemblerOutput,
    started_ms: float,
) -> HTTPException:
    """Record + map + log one floating-bond engine-side failure; return the error."""

    record_engine_error(trace, exc, duration_ms=elapsed_ms(engine_started))
    mapped = map_engine_client_error(exc)
    # Same date-context upgrade as the fixed path; the floating request
    # carries a projection curve alongside the discount curve, so both are
    # named (either can be the later-dated one).
    if isinstance(mapped, EngineDateCoherenceError):
        mapped.add_date_context(
            as_of=payload.as_of.isoformat(),
            curves=[assembled.discount_curve, assembled.projection_curve],
        )
    _attach_assembled_details_floating(mapped, assembled_request)
    _log_failure_floating(
        ctx=ctx, payload=payload, assembled=assembled, started_ms=started_ms, exc=exc
    )
    return mapped


def _log_failure_fixed(
    *,
    ctx: AuthContext,
    payload: FixedBondPriceRequest,
    assembled: FixedAssemblerOutput,
    started_ms: float,
    exc: BaseException,
) -> None:
    duration_ms = (time.monotonic() - started_ms) * 1000.0
    _log.warning(
        "orchestrator.pricing.bonds_fixed.failure",
        uid=ctx.uid,
        bond_id=str(payload.bond_id) if payload.bond_id is not None else None,
        curve_set_id=(str(assembled.curve_set_id) if assembled.curve_set_id is not None else None),
        discount_curve_id=(
            str(assembled.discount_curve_id) if assembled.discount_curve_id is not None else None
        ),
        as_of=payload.as_of.isoformat(),
        snapshot_id=(str(payload.snapshot_id) if payload.snapshot_id is not None else None),
        duration_ms=round(duration_ms, 3),
        exc_type=type(exc).__name__,
    )


def _log_failure_floating(
    *,
    ctx: AuthContext,
    payload: FloatingBondPriceRequest,
    assembled: FloatingAssemblerOutput,
    started_ms: float,
    exc: BaseException,
) -> None:
    duration_ms = (time.monotonic() - started_ms) * 1000.0
    _log.warning(
        "orchestrator.pricing.bonds_floating.failure",
        uid=ctx.uid,
        bond_id=str(payload.bond_id) if payload.bond_id is not None else None,
        curve_set_id=(str(assembled.curve_set_id) if assembled.curve_set_id is not None else None),
        discount_curve_id=(
            str(assembled.discount_curve_id) if assembled.discount_curve_id is not None else None
        ),
        projection_curve_id=(
            str(assembled.projection_curve_id)
            if assembled.projection_curve_id is not None
            else None
        ),
        index_id=(str(assembled.index_id) if assembled.index_id is not None else None),
        as_of=payload.as_of.isoformat(),
        snapshot_id=(str(payload.snapshot_id) if payload.snapshot_id is not None else None),
        duration_ms=round(duration_ms, 3),
        exc_type=type(exc).__name__,
    )


__all__ = [
    "FixedBondPriceRequest",
    "FixedBondPriceResponse",
    "FixedBondResult",
    "FloatingBondPriceRequest",
    "FloatingBondPriceResponse",
    "FloatingBondResult",
    "router",
]
