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
entry in the engine's ``rpc_service QuantraServer`` block — for an IBOR float
index. When the float index is registered as an OVERNIGHT index (``IndexDef.
index_type == Overnight``: SONIA, SOFR, ESTR, ...) the same ``/v1`` swap is
sent as an ``OisSwap`` on :attr:`EngineRpc.PRICE_OIS_SWAP`: engine >= 0.10.0
accepts an overnight index only through ``OvernightCoupons``, which
``VanillaSwap.floating_leg`` does not take (``docs/http-api.md`` § "Swap
legs"). See :func:`swap_ir_engine_rpc`.
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
from quantra_common.engine_client._generated.quantra.enums.PayReceive import PayReceive
from quantra_common.engine_client._generated.quantra.enums.RateAveragingType import (
    RateAveragingType,
)
from quantra_common.engine_client._generated.quantra.enums.SwapFlowKind import SwapFlowKind
from quantra_common.engine_client._generated.quantra.FixedCoupons import FixedCouponsT
from quantra_common.engine_client._generated.quantra.IborCoupons import IborCouponsT
from quantra_common.engine_client._generated.quantra.OisSwap import OisSwapT
from quantra_common.engine_client._generated.quantra.OvernightCoupons import OvernightCouponsT
from quantra_common.engine_client._generated.quantra.PriceOisSwap import PriceOisSwapT
from quantra_common.engine_client._generated.quantra.PriceOisSwapRequest import (
    PriceOisSwapRequestT,
)
from quantra_common.engine_client._generated.quantra.PriceOisSwapResponse import (
    PriceOisSwapResponse,
)
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
from quantra_common.engine_client._generated.quantra.SwapCoupons import SwapCoupons
from quantra_common.engine_client._generated.quantra.SwapLeg import SwapLegT
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
    forwarding_index_currency,
    forwarding_index_is_overnight,
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
# The RPC an overnight-index swap goes to (``OisSwap`` / ``OvernightCoupons``).
SWAP_IR_OIS_ENGINE_RPC: Final[EngineRpc] = EngineRpc.PRICE_OIS_SWAP


def swap_ir_engine_rpc(resolved: ResolvedMarketData) -> EngineRpc:
    """The engine RPC a swap on ``resolved``'s float index is priced through.

    ``PriceOisSwap`` when the float index is registered as an overnight index
    (:func:`forwarding_index_is_overnight`, the registered ``IndexDef``'s
    ``index_type``), else ``PriceVanillaSwap``.
    """

    if forwarding_index_is_overnight(resolved):
        return SWAP_IR_OIS_ENGINE_RPC
    return SWAP_IR_ENGINE_RPC


# A vanilla swap is exactly two ``SwapLeg``s: ``fixed_leg`` (``legs[0]``) and
# ``floating_leg`` (``legs[1]``) — the engine's ``VanillaSwap`` table order.
_VANILLA_SWAP_LEG_COUNT: Final[int] = 2

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

       An overnight float index (:func:`swap_ir_engine_rpc`) encodes the batch
       as ``PriceOisSwapRequest`` bytes instead (``OisSwap`` with an
       ``OvernightCoupons`` overnight leg), to be sent on ``PriceOisSwap``.

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
    overnight = forwarding_index_is_overnight(resolved)
    request: PriceVanillaSwapRequestT | PriceOisSwapRequestT = (
        PriceOisSwapRequestT() if overnight else PriceVanillaSwapRequestT()
    )
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
    leg_currency = forwarding_index_currency(resolved)
    float_frequency = float_leg_frequency(resolved)
    request.swaps = [
        _build_one_swap(
            trade,
            discount_curve_id=discount_curve_id,
            forwarding_curve_id=forwarding_curve_id,
            forwarding_index_id=forwarding_index_id,
            float_frequency=float_frequency,
            leg_currency=leg_currency,
            overnight=overnight,
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
    overnight: bool = False,
) -> list[IrSwapResult]:
    """Decode one batch of ``PriceVanillaSwapResponse`` bytes into typed results.

    ``overnight`` reads a ``PriceOisSwapResponse`` (the ``PriceOisSwap`` reply
    to an overnight-index swap) instead; its rows carry the same ``npv`` /
    ``fair_rate`` / ``fair_spread`` / ``legs`` (``legs[1]`` = the overnight
    leg), so the result shape is identical.

    Pulls NPV / fair_rate / fair_spread out of each ``VanillaSwapResponse``
    and the per-leg NPVs + per-period cashflows out of its ``legs[]``
    (engine >= 0.10.0: ``legs[0]`` = fixed leg, ``legs[1]`` = floating leg,
    each a ``SwapLegResult`` whose ``flows`` vector the engine fills when
    the request set ``include_flows``).
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
        response: PriceVanillaSwapResponse | PriceOisSwapResponse = (
            PriceOisSwapResponse.GetRootAs(response_bytes, 0)
            if overnight
            else PriceVanillaSwapResponse.GetRootAs(response_bytes, 0)
        )
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
        # Engine >= 0.10.0: one ``SwapLegResult`` per leg in request order
        # (``fixed_leg`` then ``floating_leg``); the per-leg ``*_npv`` /
        # ``*_bps`` / ``*_flows`` fields no longer exist.
        n_legs = swap_response.LegsLength()
        if n_legs != _VANILLA_SWAP_LEG_COUNT:
            msg = (
                "decode_swap_ir_response: engine returned "
                f"{n_legs} leg result(s) for swap {i}, expected {_VANILLA_SWAP_LEG_COUNT}."
            )
            raise ValueError(msg)
        fixed_leg = swap_response.Legs(0)
        floating_leg = swap_response.Legs(1)
        if fixed_leg is None or floating_leg is None:
            msg = f"decode_swap_ir_response: null leg result at index {i}."
            raise ValueError(msg)
        results.append(
            IrSwapResult(
                npv=float(swap_response.Npv()),
                leg_npvs=[
                    {"role": "fixed", "npv": float(fixed_leg.Npv())},
                    {"role": "floating", "npv": float(floating_leg.Npv())},
                ],
                extras={
                    # ``= null`` optionals since 0.10.0: absent when the leg's
                    # annuity is zero. Decode presence, never ``float(None)``.
                    "fair_rate": _decode_optional_float(swap_response.FairRate()),
                    "fair_spread": _decode_optional_float(swap_response.FairSpread()),
                },
                fixed_leg_flows=_decode_leg_flows(fixed_leg.FlowsLength, fixed_leg.Flows),
                floating_leg_flows=_decode_leg_flows(floating_leg.FlowsLength, floating_leg.Flows),
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
        request: PriceVanillaSwapRequestT | PriceOisSwapRequestT = (
            PriceVanillaSwapRequestT.InitFromPackedBuf(request_bytes, 0)
        )
        if _is_ois_request(request):
            request = PriceOisSwapRequestT.InitFromPackedBuf(request_bytes, 0)
    except Exception as exc:  # tracing is best-effort — never raise
        return {"__undecodable__": True, "exc_type": type(exc).__name__}
    return serialize_fb_object(request)


def _is_ois_request(request: PriceVanillaSwapRequestT | PriceOisSwapRequestT) -> bool:
    """Whether a buffer read as ``PriceVanillaSwapRequest`` is really an OIS one.

    ``PriceVanillaSwapRequest`` and ``PriceOisSwapRequest`` share their slot
    layout down to the two legs, and swap_ir sends ``OvernightCoupons`` (only
    ever on ``PriceOisSwap``) exactly when the float index is overnight, so
    the second leg's coupon kind tells the two roots apart.
    """

    for entry in getattr(request, "swaps", None) or []:
        swap = getattr(entry, "vanillaSwap", None)
        leg = getattr(swap, "floatingLeg", None) if swap is not None else None
        if leg is not None and leg.couponsType == SwapCoupons.OvernightCoupons:
            return True
    return False


def _decode_optional_float(raw: float | None) -> float | None:
    """Decode a ``= null`` FlatBuffers double (``None`` when absent on the wire)."""

    return float(raw) if raw is not None else None


_FLOW_KIND_LABELS: Final[dict[int, str]] = {
    SwapFlowKind.Coupon: "Coupon",
    SwapFlowKind.NotionalExchange: "NotionalExchange",
}


def _decode_flow_kind(raw: int | None) -> str | None:
    """Map the trailing ``SwapLegFlow.kind`` enum (engine >= 0.10.0) to its label."""

    if raw is None:
        return None
    return _FLOW_KIND_LABELS.get(int(raw), str(raw))


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

    # Engine 0.5.0 (#119) dropped ``has_cms_swap_rate`` — presence of
    # ``cms_swap_rate`` alone carries the flag and the accessor returns
    # ``None`` when absent (non-CMS coupons).
    cms_raw = flow.CmsSwapRate()
    has_cms = cms_raw is not None

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
        has_cms_swap_rate=has_cms,
        cms_swap_rate=float(cms_raw) if cms_raw is not None else 0.0,
        spread=float(flow.Spread()),
        rate=float(flow.Rate()),
        kind=_decode_flow_kind(flow.Kind()),
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
    rpc = swap_ir_engine_rpc(resolved)
    response_bytes = await engine.call(rpc, request_bytes)
    return decode_swap_ir_response(
        response_bytes,
        expected_count=len(batch.trades),
        overnight=rpc is SWAP_IR_OIS_ENGINE_RPC,
    )


def _build_one_swap(
    trade: IrSwapTrade,
    *,
    discount_curve_id: str,
    forwarding_curve_id: str,
    forwarding_index_id: str,
    float_frequency: int,
    leg_currency: str,
    overnight: bool = False,
) -> PriceVanillaSwapT | PriceOisSwapT:
    """Map one :class:`IrSwapTrade` to a ``PriceVanillaSwap`` FB graph.

    With ``overnight`` (the float index is registered as an overnight index)
    the same legs go into a ``PriceOisSwap`` instead: the floating leg carries
    ``OvernightCoupons`` (:func:`build_overnight_coupons`) and both legs state
    ``payment_calendar`` = the schedule calendar and ``payment_lag`` = 0
    (presence-required on ``/price-ois-swap``; the values the vanilla path's
    absent fields already meant), so only the coupon kind changes.

    Engine >= 0.10.0 ``SwapLeg`` model: each leg carries its side
    (``pay_receive``), ``currency``, notional, schedule, day count, payment
    convention, a ``coupons`` union (``FixedCoupons{rate}`` /
    ``IborCoupons{index, spread}``) and its own curves. The mapping from the
    orchestrator's ``/v1`` body (unchanged) follows the engine's converter
    (``tools/migrate_swap_requests/README.md`` § "What changes, field by
    field"):

    * ``swap_type`` ``Payer`` → fixed leg ``Pay`` / floating leg ``Receive``;
      ``Receiver`` the inverse.
    * ``currency`` = the forwarding index's ``IndexDef.currency`` on both legs
      (the engine 400s a floating leg whose currency differs from its index).
    * the request-level discounting curve → ``discounting_curve`` on BOTH
      legs; the request-level forwarding curve → ``forwarding_curve`` on the
      floating leg only (rejected on a fixed leg).
    * ``IborCoupons.fixing_days`` left absent = the index's own fixing days;
      ``initial_exchange`` / ``final_exchange`` absent = ``false``.

    The discount / forwarding curve references honour the resolved entity ids
    the translator placed in ``Pricing.rates.curves``, not ``CANONICAL_*``;
    the float-leg index references the resolved float index id
    (``forwarding_index_id``) — the resolved one when supplied, else the
    interim default forwarding index. ``float_frequency`` is the coupon
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
    fixed_side, float_side = pay_receive_sides(swap_type_label)

    fixed_leg = _build_fixed_leg(
        side=fixed_side,
        currency=leg_currency,
        notional=notional,
        rate=fixed_rate,
        schedule=_build_leg_schedule(effective_date, termination_date, Frequency.Annual),
        discount_curve_id=discount_curve_id,
    )
    floating_leg = _build_floating_leg(
        side=float_side,
        currency=leg_currency,
        notional=notional,
        index_id=forwarding_index_id,
        schedule=_build_leg_schedule(effective_date, termination_date, float_frequency),
        discount_curve_id=discount_curve_id,
        forwarding_curve_id=forwarding_curve_id,
        overnight=overnight,
    )

    if overnight:
        for leg in (fixed_leg, floating_leg):
            # Presence-required on every ``/price-ois-swap`` leg. The schedule
            # calendar and no lag == what the vanilla path's absent fields meant.
            leg.paymentCalendar = _LEG_CALENDAR
            leg.paymentLag = 0
        ois = OisSwapT()
        ois.fixedLeg = fixed_leg
        ois.overnightLeg = floating_leg
        price_ois = PriceOisSwapT()
        price_ois.oisSwap = ois
        return price_ois

    swap = VanillaSwapT()
    swap.fixedLeg = fixed_leg
    swap.floatingLeg = floating_leg
    price_swap = PriceVanillaSwapT()
    price_swap.vanillaSwap = swap
    return price_swap


# The calendar every swap_ir leg schedule is generated on (and, on an OIS, the
# payment calendar both legs state).
_LEG_CALENDAR: Final[int] = Calendar.TARGET


def _build_leg_schedule(effective_date: str, termination_date: str, frequency: int) -> ScheduleT:
    """The vanilla TARGET / ModifiedFollowing / Forward schedule both legs use."""

    schedule = ScheduleT()
    schedule.effectiveDate = effective_date
    schedule.terminationDate = termination_date
    schedule.calendar = _LEG_CALENDAR
    schedule.frequency = frequency
    schedule.convention = BusinessDayConvention.ModifiedFollowing
    schedule.terminationDateConvention = BusinessDayConvention.ModifiedFollowing
    schedule.dateGenerationRule = DateGenerationRule.Forward
    # Presence-required since engine 0.5.0 (#118); False == the engine-0.2.0
    # default this schedule always priced with.
    schedule.endOfMonth = False
    return schedule


def _build_fixed_leg(
    *,
    side: int,
    currency: str,
    notional: float,
    rate: float,
    schedule: ScheduleT,
    discount_curve_id: str,
) -> SwapLegT:
    """A ``SwapLeg`` carrying ``FixedCoupons{rate}`` (Thirty360, MF payments).

    No ``forwarding_curve``: the engine rejects one on a fixed leg (400).
    """

    coupons = FixedCouponsT()
    coupons.rate = rate

    leg = SwapLegT()
    leg.payReceive = side
    leg.currency = currency
    leg.notional = notional
    leg.schedule = schedule
    leg.dayCounter = DayCounter.Thirty360
    leg.paymentConvention = BusinessDayConvention.ModifiedFollowing
    leg.couponsType = SwapCoupons.FixedCoupons
    leg.coupons = coupons
    leg.discountingCurve = discount_curve_id
    return leg


def _build_floating_leg(
    *,
    side: int,
    currency: str,
    notional: float,
    index_id: str,
    schedule: ScheduleT,
    discount_curve_id: str,
    forwarding_curve_id: str,
    overnight: bool = False,
) -> SwapLegT:
    """A ``SwapLeg`` carrying ``IborCoupons{index, spread 0}`` (Actual360, MF).

    ``fixing_days`` is left absent (= the index's own fixing days, the 0.10.0
    contract); ``in_arrears`` stays ``false``.

    With ``overnight`` the leg carries ``OvernightCoupons`` instead
    (:func:`build_overnight_coupons`) and no ``day_counter``: the engine
    accepts but does not read it there (the index's own day count accrues).
    """

    leg = SwapLegT()
    leg.payReceive = side
    leg.currency = currency
    leg.notional = notional
    leg.schedule = schedule
    leg.paymentConvention = BusinessDayConvention.ModifiedFollowing
    if overnight:
        leg.couponsType = SwapCoupons.OvernightCoupons
        leg.coupons = build_overnight_coupons(index_id)
    else:
        coupons = IborCouponsT()
        coupons.index = build_index_ref(index_id)
        coupons.spread = 0.0
        leg.dayCounter = DayCounter.Actual360
        leg.couponsType = SwapCoupons.IborCoupons
        leg.coupons = coupons
    leg.discountingCurve = discount_curve_id
    leg.forwardingCurve = forwarding_curve_id
    return leg


def build_overnight_coupons(index_id: str) -> OvernightCouponsT:
    """``OvernightCoupons{index, spread 0}`` with QuantLib's plain-OIS conventions.

    Engine >= 0.10.0 (``docs/http-api.md`` § "Swap legs"): ``averaging_method``
    / ``lookback_days`` / ``lockout_days`` / ``apply_observation_shift`` are
    presence-required, ``telescopic_value_dates`` defaults to ``false``. The
    orchestrator's ``/v1`` swap body carries none of them, so they take the
    ``QuantLib::OvernightIndexedSwap`` defaults: daily compounding, no
    lookback, no lockout, no observation shift (stated explicitly on the wire).
    """

    coupons = OvernightCouponsT()
    coupons.index = build_index_ref(index_id)
    coupons.spread = 0.0
    coupons.averagingMethod = RateAveragingType.Compound
    coupons.lookbackDays = 0
    coupons.lockoutDays = 0
    coupons.applyObservationShift = False
    return coupons


def pay_receive_sides(swap_type_label: str) -> tuple[int, int]:
    """Map the ``/v1`` ``swap_type`` label to ``(fixed_leg, floating_leg)`` sides.

    Engine converter rule: ``Payer`` means the party pays the fixed leg
    (``Pay``) and receives the floating leg (``Receive``); ``Receiver`` is the
    inverse. Anything that is not ``"receiver"`` (case-insensitive) is Payer —
    the pre-0.10.0 ``SwapType`` default the orchestrator always applied.
    """

    if swap_type_label.lower() == "receiver":
        return PayReceive.Receive, PayReceive.Pay
    return PayReceive.Pay, PayReceive.Receive


__all__ = [
    "SWAP_IR_ENGINE_RPC",
    "SWAP_IR_OIS_ENGINE_RPC",
    "build_overnight_coupons",
    "build_swap_ir_request",
    "decode_swap_ir_request_wire",
    "decode_swap_ir_response",
    "pay_receive_sides",
    "price_swap_ir_batch",
    "swap_ir_engine_rpc",
]
