"""Engine wire-layer for the IR-swap pricing path.

Three callables:

* :func:`build_swap_ir_request` — turn one :class:`EngineBatch[IrSwapTrade]`
  into the FlatBuffers-encoded payload the engine expects on the
  ``PriceVanillaSwap`` RPC.
* :func:`decode_swap_ir_response` — turn the engine's response bytes
  (a ``PriceVanillaSwapResponse`` flatbuffer root) into a
  ``Sequence[IrSwapResult]`` (one per trade in the batch, in input
  order).
* :func:`price_swap_ir_batch` — the per-product translator the
  concurrency seam's :func:`execute` runner injects via its
  ``price_batch=`` parameter. Composes the two callables above
  around a single :class:`EngineClient.call`.

This module does real FlatBuffers encoding via the vendored
bindings; the ``Pricing`` context is a faithful encoding of the
caller's resolved curves / quotes
built by :mod:`quantra_orchestrator.pricing._translator`. The
orchestrator's API surface (D9-soft-shape ``IrSwapTrade.swap`` /
``CurveRef``) stays intentionally loose; this module fills in the
per-leg schedule detail using sensible defaults. Trade-body keys
that ARE honored:

* ``notional`` (float, default 1,000,000) — applied to both legs.
* ``fixed_rate`` (float, default 0.035) — fixed-leg coupon rate.
* ``swap_type`` (``"Payer"`` / ``"Receiver"``, default Payer).
* ``effective_date`` (YYYY-MM-DD, default a 5Y window starting
  two business days after the canonical as-of date).
* ``termination_date`` (YYYY-MM-DD, default 5Y after effective).

Anything else in ``trade.swap`` is ignored (forward-compatible —
adding a new key never breaks an older orchestrator). the portal's
frontend cutover is the right time to widen this surface.

The RPC name is :attr:`EngineRpc.PRICE_VANILLA_SWAP` — the canonical
entry in the engine's ``rpc_service QuantraServer`` block.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date
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
from quantra_common.engine_client._generated.quantra.enums.Frequency import Frequency
from quantra_common.engine_client._generated.quantra.enums.SwapType import SwapType
from quantra_common.engine_client._generated.quantra.PriceVanillaSwap import (
    PriceVanillaSwapT,
)
from quantra_common.engine_client._generated.quantra.PriceVanillaSwapRequest import (
    PriceVanillaSwapRequestT,
)
from quantra_common.engine_client._generated.quantra.PriceVanillaSwapResponse import (
    PriceVanillaSwapResponse,
)
from quantra_common.engine_client._generated.quantra.Schedule import ScheduleT
from quantra_common.engine_client._generated.quantra.SwapFixedLeg import SwapFixedLegT
from quantra_common.engine_client._generated.quantra.SwapFloatingLeg import (
    SwapFloatingLegT,
)
from quantra_common.engine_client._generated.quantra.SwapLegFlow import SwapLegFlow
from quantra_common.engine_client._generated.quantra.VanillaSwap import VanillaSwapT
from quantra_orchestrator.engine.errors import EngineMissingFixingError
from quantra_orchestrator.pricing._fb_helpers import build_index_ref
from quantra_orchestrator.pricing._translator import (
    CurveRole,
    CurveTranslationError,
    QuoteResolutionError,
    ResolvedMarketData,
    build_pricing_from_resolved,
    float_leg_frequency,
    resolved_curve_id,
)
from quantra_orchestrator.pricing.concurrency import EngineBatch
from quantra_orchestrator.pricing.swap_ir.errors import (
    SwapIrCurveResolutionFailedError,
    SwapIrMissingTradeFieldsError,
    SwapIrQuoteResolutionFailedError,
)
from quantra_orchestrator.pricing.swap_ir.models import (
    IrSwapResult,
    IrSwapTrade,
    SwapFlow,
)
from quantra_orchestrator.tracing.stages import serialize_fb_object

SWAP_IR_ENGINE_RPC: Final[EngineRpc] = EngineRpc.PRICE_VANILLA_SWAP

# Economic fields with NO sensible market-convention default. Historically a
# missing key here silently priced a fixture swap (3.5 % fixed rate, 1M
# notional, a 2025→2030 schedule). Now a hard 422
# ``swap_ir_missing_trade_fields`` per field.
_REQUIRED_SWAP_FIELDS: Final[tuple[str, ...]] = (
    "notional",
    "fixed_rate",
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
        raise SwapIrMissingTradeFieldsError(product=product, missing_fields=missing)


def build_swap_ir_request(
    trades: Sequence[IrSwapTrade],
    *,
    resolved: ResolvedMarketData,
) -> bytes:
    """Encode one batch of IR-swap trades as ``PriceVanillaSwapRequest`` bytes.

       The surrounding ``Pricing`` context (curves, index, options) is built
       once per batch from the caller's *resolved* market data via
       :func:`~quantra_orchestrator.pricing._translator.build_pricing_from_resolved`
    — faithful curves with the resolved quote values
       substituted in, not a canonical fixture. Each trade's ``swap``
       dict still overrides the per-trade levers (notional, fixed rate, swap
       type, dates) and the per-trade discount / forwarding curve references
       honour the resolved entity ids.

       The resolved graph reaches here by route-closure capture, not through
       the concurrency seam.
       Neutral translator failures are mapped onto the swap_ir 422 codes
       (``swap_ir_quote_resolution_failed`` / ``swap_ir_curve_resolution_failed``)
       so the catalog is unchanged. The return is a fully-finished
       FlatBuffers buffer ready for ``EngineClient.call``.
    """

    # Pre-flight (O47b): a floating leg whose first accrual period has already
    # STARTED as of the pricing date needs the HISTORICAL fixing for that
    # period. The orchestrator supplies no fixings today, so the engine would
    # abort inside QuantLib with a ``Missing <index> fixing for <date>`` — and
    # the current engine build masks that on the wire as a bare
    # ``ABORTED (QuantLib error)`` (see ``translate_aio_rpc_error``), leaving
    # the caller an opaque 502. Detect the unambiguous seasoned case here and
    # raise the actionable ``missing_required_fixing`` (422) BEFORE the round
    # trip so the caller learns WHICH fixing (index + first-started period) it
    # must supply. Bounded + safe: only fires when a period is strictly in the
    # past (effective < as_of), which can never be curve-forecast — so it never
    # blocks a swap the engine could actually price.
    _preflight_required_fixing(trades, resolved)

    builder = flatbuffers.Builder(2048)
    # ForceDefaults: the engine requires every set field explicit on the wire;
    # zero-default enums/scalars (e.g. Frequency.Annual == 0) must not be omitted.
    builder.ForceDefaults(True)
    request = PriceVanillaSwapRequestT()
    # Ask the engine for per-period cashflows on both legs. The engine
    # gates ``fixed_leg_flows`` / ``floating_leg_flows`` on this bool
    # (default false); the orchestrator now decodes them in
    # ``decode_swap_ir_response`` and surfaces them on ``IrSwapResult``.
    request.includeFlows = True
    try:
        request.pricing = build_pricing_from_resolved(resolved)
    except QuoteResolutionError as exc:
        raise SwapIrQuoteResolutionFailedError(
            missing_canonical_ids=exc.missing_canonical_ids,
            as_of=resolved.as_of,
        ) from exc
    except CurveTranslationError as exc:
        raise SwapIrCurveResolutionFailedError(reason=exc.reason, details=exc.details) from exc

    discount_curve_id, forwarding_curve_id = _resolve_curve_refs(resolved)
    forwarding_index_id = resolved.forwarding_index_id
    float_frequency = float_leg_frequency(resolved)
    request.swaps = [
        _build_one_swap(
            trade,
            discount_curve_id=discount_curve_id,
            forwarding_curve_id=forwarding_curve_id,
            forwarding_index_id=forwarding_index_id,
            float_frequency=float_frequency,
        )
        for trade in trades
    ]
    builder.Finish(request.Pack(builder))
    return bytes(builder.Output())


def _parse_date(value: object) -> date | None:
    """Parse a ``YYYY-MM-DD`` string into a ``date``; ``None`` if unparseable.

    Loose by design: a malformed / missing date simply skips the
    pre-flight rather than raising — the engine remains the backstop.
    """

    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _forwarding_index_label(resolved: ResolvedMarketData) -> str:
    """Human label for the floating index in the missing-fixing error.

    Prefers the resolved index name; falls back to the interim default forward
    index family (Euribor 6M) the request encodes when none was resolved.
    """

    index = resolved.index
    name = getattr(index, "name", None) if index is not None else None
    return str(name) if name else "the floating-rate index (default Euribor 6M)"


def _preflight_required_fixing(trades: Sequence[IrSwapTrade], resolved: ResolvedMarketData) -> None:
    """Raise ``missing_required_fixing`` when a trade needs an unavailable past fixing.

    A floating coupon whose accrual period starts strictly before the pricing
    ``as_of`` has a fixing date in the past; that fixing can only come from
    history (never the curve), and the orchestrator supplies none today — so the
    engine is guaranteed to abort. Detect it up front and name the index + the
    first already-started period so the caller can act (O47b(A)). Only the
    unambiguous ``effective < as_of`` case is flagged; spot-/forward-starting
    swaps fall through to the engine unchanged.
    """

    as_of = _parse_date(resolved.as_of)
    if as_of is None:
        return
    for trade in trades:
        body: Mapping[str, Any] = trade.swap or {}
        # ``effective_date`` is a required trade field (validated in
        # ``_build_one_swap``); an absent date here simply falls through —
        # the 422 for the missing field fires at build time.
        if "effective_date" not in body:
            continue
        effective = _parse_date(body.get("effective_date"))
        if effective is None or effective >= as_of:
            continue
        index_label = _forwarding_index_label(resolved)
        first_start = effective.isoformat()
        raise EngineMissingFixingError(
            index=index_label,
            fixing_date=first_start,
            engine_detail=(
                "pre-flight: floating leg effective date "
                f"{first_start} precedes as_of {as_of.isoformat()}; the "
                "already-started coupon period requires a historical fixing "
                f"for {index_label} that was not supplied. Supply the fixing "
                "(or pin a snapshot carrying it) to price this seasoned swap."
            ),
        )


def _resolve_curve_refs(resolved: ResolvedMarketData) -> tuple[str, str]:
    """Return ``(discount_curve_id, forwarding_curve_id)`` by role.

    The assembler tags the resolved curves with their role; this reads the
    discount + forwarding reference ids off :class:`ResolvedMarketData`'s role
    map, replacing the older positional convention. The forwarding role falls back
    to the discount curve (the EUR single-curve case reuses one curve for both).
    A role-less caller (no ``curve_roles``) falls back to the first translated
    curve so the path stays robust; ``build_pricing_from_resolved`` has already
    rejected the empty-curve case, so ``curves[0]`` is safe.
    """

    curves = resolved.curves
    discount_curve_id = resolved.curve_id_for_role(CurveRole.DISCOUNT) or (
        resolved_curve_id(curves[0])
    )
    forwarding_curve_id = resolved.curve_id_for_role(CurveRole.FORWARDING) or (
        resolved_curve_id(curves[1]) if len(curves) > 1 else discount_curve_id
    )
    return discount_curve_id, forwarding_curve_id


def decode_swap_ir_response(
    response_bytes: bytes,
    *,
    expected_count: int,
) -> list[IrSwapResult]:
    """Decode one batch of ``PriceVanillaSwapResponse`` bytes into typed results.

    Pulls NPV / fair_rate / per-leg NPVs out of each
    ``VanillaSwapResponse``, plus the per-period cashflows from
    ``FixedLegFlows`` / ``FloatingLegFlows`` (each a ``[SwapLegFlow]``
    vector the engine emits when the request set ``include_flows``).
    A response built without flows decodes to empty flow lists, so the
    path is back-compatible. Rejects a count mismatch (engine returned
    a different number of swaps than the orchestrator sent) with a
    ``ValueError`` — the route handler treats this as a generic
    engine-upstream failure via :func:`map_engine_client_error`.
    """

    if not response_bytes:
        msg = "decode_swap_ir_response: empty engine response."
        raise ValueError(msg)
    try:
        response = PriceVanillaSwapResponse.GetRootAs(response_bytes, 0)
        actual = response.SwapsLength()
    except Exception as exc:
        msg = f"decode_swap_ir_response: malformed engine response ({exc})."
        raise ValueError(msg) from exc

    if actual != expected_count:
        msg = (
            "decode_swap_ir_response: engine returned "
            f"{actual} result(s), expected {expected_count}."
        )
        raise ValueError(msg)

    results: list[IrSwapResult] = []
    for i in range(actual):
        swap_response = response.Swaps(i)
        if swap_response is None:
            msg = f"decode_swap_ir_response: null swap result at index {i}."
            raise ValueError(msg)
        results.append(
            IrSwapResult(
                npv=float(swap_response.Npv()),
                leg_npvs=[
                    {"role": "fixed", "npv": float(swap_response.FixedLegNpv())},
                    {"role": "floating", "npv": float(swap_response.FloatingLegNpv())},
                ],
                extras={
                    "fair_rate": float(swap_response.FairRate()),
                    "fair_spread": float(swap_response.FairSpread()),
                    "fixed_leg_bps": float(swap_response.FixedLegBps()),
                    "floating_leg_bps": float(swap_response.FloatingLegBps()),
                },
                fixed_leg_flows=_decode_leg_flows(
                    swap_response.FixedLegFlowsLength,
                    swap_response.FixedLegFlows,
                ),
                floating_leg_flows=_decode_leg_flows(
                    swap_response.FloatingLegFlowsLength,
                    swap_response.FloatingLegFlows,
                ),
            )
        )
    return results


def decode_swap_ir_request_wire(request_bytes: bytes) -> dict[str, Any]:
    """Decode the EXACT transmitted request bytes into a readable JSON view.

    Reads the buffer with the generated FlatBuffers *reader*
    (``PriceVanillaSwapRequestT.InitFromPackedBuf`` -> ``Init`` walks the packed
    table) so the result is provably derived from the bytes that went on the
    wire — not a parallel re-pack of a separate object. The unpacked object is
    then rendered to a JSON-safe dict by the shared :func:`serialize_fb_object`,
    yielding the engine-native ``pricing`` (incl. the ``rates.indices`` registry
    where ESTR lives) / ``swaps`` / ``include_flows`` shape.

    Best-effort: a malformed buffer degrades to a marker dict, never raises —
    tracing must never disturb a price. This is the swap_ir wire decoder; other
    products add their own one-liner against their request root type and reuse
    :func:`serialize_fb_object` unchanged.
    """

    try:
        request = PriceVanillaSwapRequestT.InitFromPackedBuf(request_bytes, 0)
    except Exception as exc:  # tracing is best-effort — never raise
        return {"__undecodable__": True, "exc_type": type(exc).__name__}
    return serialize_fb_object(request)


def _decode_str(raw: bytes | None) -> str | None:
    """Decode a FlatBuffers string field (``bytes``/``None``) to ``str``/``None``."""

    if raw is None:
        return None
    return raw.decode("utf-8")


def _decode_one_flow(flow: SwapLegFlow) -> SwapFlow:
    """Map one engine ``SwapLegFlow`` table into the typed :class:`SwapFlow`.

    Mirrors the FlatBuffers field names one-for-one. String fields come back
    as ``bytes`` (or ``None`` when absent) and are decoded to ``str``; the
    numeric fields carry their schema defaults when unset so an empty coupon
    never raises.
    """

    return SwapFlow(
        payment_date=_decode_str(flow.PaymentDate()),
        accrual_start_date=_decode_str(flow.AccrualStartDate()),
        accrual_end_date=_decode_str(flow.AccrualEndDate()),
        amount=float(flow.Amount()),
        accrual_year_fraction=float(flow.AccrualYearFraction()),
        gearing=float(flow.Gearing()),
        discount=float(flow.Discount()),
        present_value=float(flow.PresentValue()),
        fixing_date=_decode_str(flow.FixingDate()),
        index_fixing=float(flow.IndexFixing()),
        has_cms_swap_rate=bool(flow.HasCmsSwapRate()),
        cms_swap_rate=float(flow.CmsSwapRate()),
        spread=float(flow.Spread()),
        rate=float(flow.Rate()),
    )


def _decode_leg_flows(
    length_fn: Callable[[], int],
    flow_at: Callable[[int], SwapLegFlow | None],
) -> list[SwapFlow]:
    """Decode one leg's ``[SwapLegFlow]`` vector into typed :class:`SwapFlow`.

    ``length_fn`` / ``flow_at`` are the generated accessor pair
    (``FixedLegFlowsLength`` / ``FixedLegFlows`` or the floating equivalents).
    Returns an empty list when the engine omitted flows (e.g. a response built
    without ``include_flows``), so the path stays safe / back-compatible. A
    ``None`` entry inside a non-empty vector is skipped defensively.
    """

    return [_decode_one_flow(flow) for j in range(length_fn()) if (flow := flow_at(j)) is not None]


async def price_swap_ir_batch(
    engine: EngineClient,
    batch: EngineBatch[IrSwapTrade],
    *,
    resolved: ResolvedMarketData,
) -> Sequence[IrSwapResult]:
    """The per-product translator injected into the ``execute`` runner.

    One ``EngineClient.call`` per batch; the runner fans out one
    batch per :class:`ConcurrencyPolicy.plan` entry. Today's
    ``OneTradePerCall`` policy emits one batch per trade. ``resolved`` is the
    route-captured :class:`ResolvedMarketData` the route's
    ``price_batch`` lambda binds; it carries the faithful curves / quotes the
    request is assembled from.
    """

    request_bytes = build_swap_ir_request(batch.trades, resolved=resolved)
    response_bytes = await engine.call(SWAP_IR_ENGINE_RPC, request_bytes)
    return decode_swap_ir_response(response_bytes, expected_count=len(batch.trades))


def _build_one_swap(
    trade: IrSwapTrade,
    *,
    discount_curve_id: str,
    forwarding_curve_id: str,
    forwarding_index_id: str,
    float_frequency: int,
) -> PriceVanillaSwapT:
    """Map one :class:`IrSwapTrade` to a ``PriceVanillaSwap`` FB graph.

    The discount / forwarding curve references honour the resolved entity ids
    the translator placed in ``Pricing.rates.curves``, not
    ``CANONICAL_*``; the float-leg index references the resolved float index
    id (``forwarding_index_id``) — the resolved one when supplied, else
    the interim default forwarding index. ``float_frequency`` is the coupon
    frequency derived from that index's tenor — Quarterly for a 3M index,
    Semiannual for 6M, etc. — so the float schedule and the projection index
    agree; it is the module default when no index was resolved.
    """

    body: Mapping[str, Any] = trade.swap or {}
    _require_trade_fields(body, _REQUIRED_SWAP_FIELDS, product="swap_ir")
    notional = float(body["notional"])
    fixed_rate = float(body["fixed_rate"])
    effective_date = str(body["effective_date"])
    termination_date = str(body["termination_date"])
    swap_type_label = str(body.get("swap_type", "Payer"))
    swap_type_value = SwapType.Receiver if swap_type_label.lower() == "receiver" else SwapType.Payer

    fixed_schedule = ScheduleT()
    fixed_schedule.effectiveDate = effective_date
    fixed_schedule.terminationDate = termination_date
    fixed_schedule.calendar = Calendar.TARGET
    fixed_schedule.frequency = Frequency.Annual
    fixed_schedule.convention = BusinessDayConvention.ModifiedFollowing
    fixed_schedule.terminationDateConvention = BusinessDayConvention.ModifiedFollowing
    fixed_schedule.dateGenerationRule = DateGenerationRule.Forward

    fixed_leg = SwapFixedLegT()
    fixed_leg.notional = notional
    fixed_leg.schedule = fixed_schedule
    fixed_leg.rate = fixed_rate
    fixed_leg.dayCounter = DayCounter.Thirty360
    fixed_leg.paymentConvention = BusinessDayConvention.ModifiedFollowing

    float_schedule = ScheduleT()
    float_schedule.effectiveDate = effective_date
    float_schedule.terminationDate = termination_date
    float_schedule.calendar = Calendar.TARGET
    float_schedule.frequency = float_frequency
    float_schedule.convention = BusinessDayConvention.ModifiedFollowing
    float_schedule.terminationDateConvention = BusinessDayConvention.ModifiedFollowing
    float_schedule.dateGenerationRule = DateGenerationRule.Forward

    float_leg = SwapFloatingLegT()
    float_leg.notional = notional
    float_leg.schedule = float_schedule
    float_leg.index = build_index_ref(forwarding_index_id)
    float_leg.dayCounter = DayCounter.Actual360
    float_leg.spread = 0.0
    float_leg.paymentConvention = BusinessDayConvention.ModifiedFollowing

    swap = VanillaSwapT()
    swap.swapType = swap_type_value
    swap.fixedLeg = fixed_leg
    swap.floatingLeg = float_leg

    price_swap = PriceVanillaSwapT()
    price_swap.vanillaSwap = swap
    price_swap.discountingCurve = discount_curve_id
    price_swap.forwardingCurve = forwarding_curve_id
    return price_swap


__all__ = [
    "SWAP_IR_ENGINE_RPC",
    "build_swap_ir_request",
    "decode_swap_ir_request_wire",
    "decode_swap_ir_response",
    "price_swap_ir_batch",
]
