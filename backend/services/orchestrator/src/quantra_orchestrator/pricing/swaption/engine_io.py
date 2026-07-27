"""Engine wire-layer for the swaption pricing path.

Three callables, structurally identical to the swap_ir-side
``engine_io`` (the per-product split):

* :func:`build_swaption_request` — turn one
  :class:`EngineBatch[SwaptionTrade]` into the FlatBuffers payload
  the engine expects on the ``PriceSwaption`` RPC
  (``EngineRpc.PRICE_SWAPTION``).
* :func:`decode_swaption_response` — turn the engine's response
  bytes (a ``PriceSwaptionResponse`` flatbuffer root) into a
  ``Sequence[SwaptionResult]`` (one per trade in the batch, in
  input order).
* :func:`price_swaption_batch` — the per-product translator the
  concurrency seam's :func:`execute` runner injects via its
  ``price_batch=`` parameter. Composes the two callables above
  around a single :class:`EngineClient.call`.

This module does real FlatBuffers encoding via the vendored
bindings; the ``Pricing`` context is a faithful encoding
of the caller's resolved curves / vol surface / swaption model built
by :mod:`quantra_orchestrator.pricing._translator` — faithful curves
+ a swaption vol surface carrying the resolved vol level +
the resolved swaption model, not a canonical Black-vol / Black-model
fixture. The per-trade discount / forwarding curve, ``volatility``
and ``model`` references honour the resolved entity ids. The
orchestrator's API surface (``SwaptionTrade.swaption`` body / loose
vol-surface / loose model JSON) stays intentionally loose;
this module fills in the per-leg schedule detail using sensible
defaults. Trade-body keys honored:

* ``notional`` (float, default 1,000,000) — applied to both legs.
* ``strike`` (float, default 0.035) — fixed-leg coupon rate.
* ``swap_type`` (``"Payer"`` / ``"Receiver"``, default Payer).
* ``exercise_date`` (YYYY-MM-DD, default 2026-01-15) — European
  exercise.
* ``effective_date`` / ``termination_date`` — underlying swap
  schedule. Default to a 5Y swap starting 2026-01-17.

Anything else in ``trade.swaption`` is ignored (forward-compatible
— adding a new key never breaks an older orchestrator).

The RPC name is :attr:`EngineRpc.PRICE_SWAPTION` — the canonical
entry in the engine's ``rpc_service QuantraServer`` block. Pinned
in a unit test so a future enum rename trips the suite rather
than silently retargeting the orchestrator.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

import flatbuffers

from quantra_common.engine_client import EngineClient, EngineRpc
from quantra_common.engine_client._generated.quantra.enums.BusinessDayConvention import (
    BusinessDayConvention,
)
from quantra_common.engine_client._generated.quantra.enums.Calendar import Calendar
from quantra_common.engine_client._generated.quantra.enums.DateGenerationRule import (
    DateGenerationRule,
)
from quantra_common.engine_client._generated.quantra.enums.DayCounter import DayCounter
from quantra_common.engine_client._generated.quantra.enums.ExerciseType import (
    ExerciseType,
)
from quantra_common.engine_client._generated.quantra.enums.Frequency import Frequency
from quantra_common.engine_client._generated.quantra.enums.SettlementType import (
    SettlementType,
)
from quantra_common.engine_client._generated.quantra.enums.SwapType import SwapType
from quantra_common.engine_client._generated.quantra.PriceSwaption import (
    PriceSwaptionT,
)
from quantra_common.engine_client._generated.quantra.PriceSwaptionRequest import (
    PriceSwaptionRequestT,
)
from quantra_common.engine_client._generated.quantra.PriceSwaptionResponse import (
    PriceSwaptionResponse,
)
from quantra_common.engine_client._generated.quantra.Schedule import ScheduleT
from quantra_common.engine_client._generated.quantra.Swaption import SwaptionT
from quantra_common.engine_client._generated.quantra.SwaptionUnderlying import (
    SwaptionUnderlying,
)
from quantra_common.engine_client._generated.quantra.VanillaSwap import VanillaSwapT
from quantra_common.engine_client.wire_compat import SwapFixedLegT, SwapFloatingLegT
from quantra_orchestrator.pricing._fb_helpers import build_index_ref
from quantra_orchestrator.pricing._translator import (
    CurveRole,
    CurveTranslationError,
    QuoteResolutionError,
    ResolvedMarketData,
    VolSurfaceTranslationError,
    build_pricing_from_resolved,
    float_leg_frequency,
    resolved_curve_id,
    resolved_model_id,
    resolved_vol_surface_id,
)
from quantra_orchestrator.pricing.concurrency import EngineBatch
from quantra_orchestrator.pricing.swaption.errors import (
    SwaptionCurveResolutionFailedError,
    SwaptionMissingTradeFieldsError,
    SwaptionQuoteResolutionFailedError,
    SwaptionSurfaceResolutionFailedError,
)
from quantra_orchestrator.pricing.swaption.models import (
    SwaptionResult,
    SwaptionTrade,
)
from quantra_orchestrator.tracing.stages import serialize_fb_object

SWAPTION_ENGINE_RPC: Final[EngineRpc] = EngineRpc.PRICE_SWAPTION

# Economic fields with NO sensible market-convention default. Historically a
# missing key here silently priced a fixture swaption (3.5 % strike, 1M
# notional, hardcoded exercise/schedule dates). Now a hard 422
# ``swaption_missing_trade_fields`` per field.
_REQUIRED_SWAPTION_FIELDS: Final[tuple[str, ...]] = (
    "notional",
    "strike",
    "exercise_date",
    "effective_date",
    "termination_date",
)


def _require_trade_fields(
    body: Mapping[str, Any],
    required: tuple[str, ...],
    *,
    product: str,
) -> None:
    """Raise the typed 422 when a required economic field is absent/empty."""

    missing = [field for field in required if body.get(field) in (None, "")]
    if missing:
        raise SwaptionMissingTradeFieldsError(product=product, missing_fields=missing)


def build_swaption_request(
    trades: Sequence[SwaptionTrade],
    *,
    resolved: ResolvedMarketData,
) -> bytes:
    """Encode one batch of swaption trades as ``PriceSwaptionRequest`` bytes.

       The surrounding ``Pricing`` context (curves, vol surface, swaption model,
       index, options) is built once per batch from the caller's *resolved* market
       data via
       :func:`~quantra_orchestrator.pricing._translator.build_pricing_from_resolved`
    — faithful curves with the resolved quote values substituted
       in and a swaption vol surface carrying the resolved vol level, not a
       canonical fixture. Each trade's ``swaption`` body still overrides the
       per-trade levers (notional / strike / swap type / exercise & schedule
       dates) and the per-trade discount / forwarding curve, ``volatility`` and
       ``model`` references honour the resolved entity ids.

       The resolved graph reaches here by route-closure capture, not through
       the concurrency seam.
       Neutral translator failures are mapped onto the swaption 422 codes
       (``swaption_quote_resolution_failed`` / ``swaption_surface_resolution_failed``
       / ``swaption_curve_resolution_failed``) so the catalog is unchanged.
       Returns a finished FlatBuffers buffer ready for :meth:`EngineClient.call`.
    """

    builder = flatbuffers.Builder(2048)
    # ForceDefaults: the engine requires every set field explicit on the wire;
    # zero-default enums/scalars (e.g. Frequency.Annual == 0) must not be omitted.
    builder.ForceDefaults(True)
    request = PriceSwaptionRequestT()
    try:
        request.pricing = build_pricing_from_resolved(resolved)
    except QuoteResolutionError as exc:
        raise SwaptionQuoteResolutionFailedError(
            missing_canonical_ids=exc.missing_canonical_ids,
            as_of=resolved.as_of,
        ) from exc
    except VolSurfaceTranslationError as exc:
        raise SwaptionSurfaceResolutionFailedError(reason=exc.reason, details=exc.details) from exc
    except CurveTranslationError as exc:
        raise SwaptionCurveResolutionFailedError(reason=exc.reason, details=exc.details) from exc

    discount_curve_id, forwarding_curve_id = _resolve_curve_refs(resolved)
    vol_surface_id, model_id = _resolve_vol_and_model_refs(resolved)
    forwarding_index_id = resolved.forwarding_index_id
    float_frequency = float_leg_frequency(resolved)
    request.swaptions = [
        _build_one_swaption(
            trade,
            discount_curve_id=discount_curve_id,
            forwarding_curve_id=forwarding_curve_id,
            forwarding_index_id=forwarding_index_id,
            float_frequency=float_frequency,
            vol_surface_id=vol_surface_id,
            model_id=model_id,
        )
        for trade in trades
    ]
    request.includeDiagnostics = False
    builder.Finish(request.Pack(builder))
    return bytes(builder.Output())


def _resolve_curve_refs(resolved: ResolvedMarketData) -> tuple[str, str]:
    """Return ``(discount_curve_id, forwarding_curve_id)`` by role.

    The assembler tags the resolved curves with their role; this reads the
    discount + forwarding reference ids off :class:`ResolvedMarketData`'s role
    map, replacing the older positional convention. The forwarding role falls back
    to the discount curve (the EUR single-curve case reuses one curve for both).
    A role-less caller falls back to the first translated curve so the path
    stays robust; ``build_pricing_from_resolved`` has already rejected the
    empty-curve case, so ``curves[0]`` is safe.
    """

    curves = resolved.curves
    discount_curve_id = resolved.curve_id_for_role(CurveRole.DISCOUNT) or (
        resolved_curve_id(curves[0])
    )
    forwarding_curve_id = resolved.curve_id_for_role(CurveRole.FORWARDING) or (
        resolved_curve_id(curves[1]) if len(curves) > 1 else discount_curve_id
    )
    return discount_curve_id, forwarding_curve_id


def _resolve_vol_and_model_refs(resolved: ResolvedMarketData) -> tuple[str, str]:
    """Return ``(vol_surface_id, model_id)`` from the resolved surface + model.

    The route always supplies exactly one resolved vol surface and one resolved
    swaption model (both are required fields on the assembler output), so the
    per-trade ``volatility`` / ``model`` references honour those resolved entity
    ids — the same strings the translator placed on the emitted specs, never
    ``CANONICAL_*``. An absent surface / model is defensively surfaced as the
    swaption surface-resolution 422 rather than an opaque ``IndexError``.
    """

    if not resolved.vol_surfaces:
        raise SwaptionSurfaceResolutionFailedError(
            reason="No resolved vol surface to reference in the swaption request.",
            details=[{"resolved_vol_surface_count": 0}],
        )
    if not resolved.models:
        raise SwaptionSurfaceResolutionFailedError(
            reason="No resolved swaption model to reference in the swaption request.",
            details=[{"resolved_model_count": 0}],
        )
    return (
        resolved_vol_surface_id(resolved.vol_surfaces[0]),
        resolved_model_id(resolved.models[0]),
    )


def decode_swaption_response(
    response_bytes: bytes,
    *,
    expected_count: int,
) -> list[SwaptionResult]:
    """Decode one batch of ``PriceSwaptionResponse`` bytes into typed results.

    Pulls NPV / Greeks / implied-vol / atm_forward out of each
    :class:`SwaptionResponse`. Rejects a count mismatch (engine
    returned a different number of swaptions than the orchestrator
    sent) with a ``ValueError`` — the route handler treats this as
    a generic engine-upstream failure via
    :func:`map_engine_client_error`.
    """

    if not response_bytes:
        msg = "decode_swaption_response: empty engine response."
        raise ValueError(msg)
    try:
        response = PriceSwaptionResponse.GetRootAs(response_bytes, 0)
        actual = response.SwaptionsLength()
    except Exception as exc:
        msg = f"decode_swaption_response: malformed engine response ({exc})."
        raise ValueError(msg) from exc

    if actual != expected_count:
        msg = (
            "decode_swaption_response: engine returned "
            f"{actual} result(s), expected {expected_count}."
        )
        raise ValueError(msg)

    results: list[SwaptionResult] = []
    for i in range(actual):
        swaption_response = response.Swaptions(i)
        if swaption_response is None:
            msg = f"decode_swaption_response: null swaption result at index {i}."
            raise ValueError(msg)
        results.append(
            SwaptionResult(
                npv=float(swaption_response.Npv()),
                vega=float(swaption_response.Vega()),
                delta=float(swaption_response.Delta()),
                extras={
                    "implied_volatility": float(swaption_response.ImpliedVolatility()),
                    "atm_forward": float(swaption_response.AtmForward()),
                    "annuity": float(swaption_response.Annuity()),
                    "gamma": float(swaption_response.Gamma()),
                    "theta": float(swaption_response.Theta()),
                    "dv01": float(swaption_response.Dv01()),
                },
            )
        )
    return results


def decode_swaption_request_wire(request_bytes: bytes) -> dict[str, Any]:
    """Decode the EXACT transmitted swaption request bytes into readable JSON.

    Reads the buffer with the generated FlatBuffers reader
    (``PriceSwaptionRequestT.InitFromPackedBuf``) so the result is provably
    derived from the bytes that went on the wire, then renders it JSON-safe via
    the shared :func:`serialize_fb_object`. Best-effort: a malformed buffer
    degrades to a marker dict, never raises — tracing must never disturb a
    price. Mirrors ``decode_swap_ir_request_wire``.
    """

    try:
        request = PriceSwaptionRequestT.InitFromPackedBuf(request_bytes, 0)
    except Exception as exc:  # tracing is best-effort — never raise
        return {"__undecodable__": True, "exc_type": type(exc).__name__}
    return serialize_fb_object(request)


async def price_swaption_batch(
    engine: EngineClient,
    batch: EngineBatch[SwaptionTrade],
    *,
    resolved: ResolvedMarketData,
) -> Sequence[SwaptionResult]:
    """The per-product translator injected into the ``execute`` runner.

    One ``EngineClient.call`` per batch; the runner fans out one
    batch per :class:`ConcurrencyPolicy.plan` entry. Today's
    ``OneTradePerCall`` policy emits one batch per trade. ``resolved`` is the
    route-captured :class:`ResolvedMarketData` the route's
    ``price_batch`` lambda binds; it carries the faithful curves / quotes /
    vol surface / model the request is assembled from.
    """

    request_bytes = build_swaption_request(batch.trades, resolved=resolved)
    response_bytes = await engine.call(SWAPTION_ENGINE_RPC, request_bytes)
    return decode_swaption_response(response_bytes, expected_count=len(batch.trades))


def _build_swap_schedule(
    *,
    effective_date: str,
    termination_date: str,
    frequency: int,
) -> ScheduleT:
    """Vanilla schedule shared by underlying-swap fixed / floating legs."""

    sched = ScheduleT()
    sched.effectiveDate = effective_date
    sched.terminationDate = termination_date
    sched.calendar = Calendar.TARGET
    sched.frequency = frequency
    sched.convention = BusinessDayConvention.ModifiedFollowing
    sched.terminationDateConvention = BusinessDayConvention.ModifiedFollowing
    sched.dateGenerationRule = DateGenerationRule.Forward
    return sched


def _build_underlying_swap(
    *,
    notional: float,
    strike: float,
    swap_type_value: int,
    effective_date: str,
    termination_date: str,
    forwarding_index_id: str,
    float_frequency: int,
) -> VanillaSwapT:
    """Construct the underlying ``VanillaSwap`` for a swaption trade.

    The float leg references the resolved float index id — the resolved
    one when supplied, else the interim default forwarding index — and its
    schedule frequency follows that index's tenor (``float_frequency``,
    shared with swap_ir/bonds): Quarterly for a 3M index, Semiannual for 6M,
    etc., so the float schedule and the projection index agree; it is the
    module default when no index was resolved.
    """

    fixed_leg = SwapFixedLegT()
    fixed_leg.notional = notional
    fixed_leg.schedule = _build_swap_schedule(
        effective_date=effective_date,
        termination_date=termination_date,
        frequency=Frequency.Annual,
    )
    fixed_leg.rate = strike
    fixed_leg.dayCounter = DayCounter.Thirty360
    fixed_leg.paymentConvention = BusinessDayConvention.ModifiedFollowing

    float_leg = SwapFloatingLegT()
    float_leg.notional = notional
    float_leg.schedule = _build_swap_schedule(
        effective_date=effective_date,
        termination_date=termination_date,
        frequency=float_frequency,
    )
    float_leg.index = build_index_ref(forwarding_index_id)
    float_leg.dayCounter = DayCounter.Actual360
    float_leg.spread = 0.0
    float_leg.paymentConvention = BusinessDayConvention.ModifiedFollowing

    underlying = VanillaSwapT()
    underlying.swapType = swap_type_value
    underlying.fixedLeg = fixed_leg
    underlying.floatingLeg = float_leg
    return underlying


def _build_one_swaption(
    trade: SwaptionTrade,
    *,
    discount_curve_id: str,
    forwarding_curve_id: str,
    forwarding_index_id: str,
    float_frequency: int,
    vol_surface_id: str,
    model_id: str,
) -> PriceSwaptionT:
    """Map one :class:`SwaptionTrade` to a ``PriceSwaption`` FB graph.

    The discount / forwarding curve, ``volatility`` and ``model`` references
    honour the resolved entity ids the translator placed in the ``Pricing``
    graph, not ``CANONICAL_*``.
    """

    body: Mapping[str, Any] = trade.swaption or {}
    _require_trade_fields(body, _REQUIRED_SWAPTION_FIELDS, product="swaption")
    notional = float(body["notional"])
    strike = float(body["strike"])
    exercise_date = str(body["exercise_date"])
    effective_date = str(body["effective_date"])
    termination_date = str(body["termination_date"])
    swap_type_label = str(body.get("swap_type", "Payer"))
    swap_type_value = SwapType.Receiver if swap_type_label.lower() == "receiver" else SwapType.Payer

    underlying = _build_underlying_swap(
        notional=notional,
        strike=strike,
        swap_type_value=swap_type_value,
        effective_date=effective_date,
        termination_date=termination_date,
        forwarding_index_id=forwarding_index_id,
        float_frequency=float_frequency,
    )

    swaption = SwaptionT()
    swaption.exerciseType = ExerciseType.European
    swaption.settlementType = SettlementType.Physical
    swaption.exerciseDate = exercise_date
    swaption.underlyingType = SwaptionUnderlying.VanillaSwap
    swaption.underlying = underlying

    price_swaption = PriceSwaptionT()
    price_swaption.swaption = swaption
    price_swaption.discountingCurve = discount_curve_id
    price_swaption.forwardingCurve = forwarding_curve_id
    price_swaption.volatility = vol_surface_id
    price_swaption.model = model_id
    return price_swaption


__all__ = [
    "SWAPTION_ENGINE_RPC",
    "build_swaption_request",
    "decode_swaption_response",
    "price_swaption_batch",
]
