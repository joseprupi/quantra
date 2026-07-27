"""Engine wire-layer for the bonds pricing paths (fixed + floating).

Six callables, structurally identical to the swap_ir / swaption
``engine_io`` modules (the per-product split) — one trio per
variant:

* :func:`build_fixed_bond_request` / :func:`build_floating_bond_request`
  — turn one :class:`EngineBatch[FixedBondTrade | FloatingBondTrade]`
  into the FlatBuffers payload the engine expects on the matching
  RPC.
* :func:`decode_fixed_bond_response` / :func:`decode_floating_bond_response`
  — turn the engine's response bytes (``PriceFixedRateBondResponse``
  / ``PriceFloatingRateBondResponse`` flatbuffer roots) into a typed
  result sequence (one per trade in the batch, in input order).
* :func:`price_fixed_bond_batch` / :func:`price_floating_bond_batch`
  — the per-product translators the concurrency seam's
  :func:`execute` runner injects via its ``price_batch=`` parameter.

A later revision flips this module from a JSON-over-bytes placeholder to
real FlatBuffers encoding via the vendored bindings. The
orchestrator's API surface (``FixedBondTrade.bond`` /
``FloatingBondTrade.bond`` body — loose JSONB) stays
intentionally schema-light; this module fills in the schema-level
detail (canonical curve + helper conventions + Yield block; for
floating bonds also a Black-Ibor coupon pricer) using sensible
defaults from :mod:`quantra_orchestrator.pricing._fb_helpers`.

Trade-body keys honored (both variants):

* ``face_amount`` (float, default 100) — bond face / notional.
* ``settlement_days`` (int, default 2).
* ``redemption`` (float, default 100) — final-redemption percent.
* ``issue_date`` (YYYY-MM-DD, **required**).
* ``effective_date`` / ``termination_date`` — schedule first / last
  cashflow dates, **required**. A missing/empty required economic
  field raises a 422 ``bond_missing_trade_fields`` — the wire layer
  never silently substitutes a fixture bond for the caller's trade.

Fixed-only:

* ``coupon_rate`` (float, **required**) — coupon rate.
  the canonical flat-body key for the fixed-rate bond coupon is
  ``coupon_rate`` (the descriptive name the portal sends).

Floating-only:

* ``spread`` (float, default 0.001) — leg spread.
* ``fixing_days`` (int, default 2).
* ``in_arrears`` (bool, default False).

The RPC names are :attr:`EngineRpc.PRICE_FIXED_RATE_BOND` /
:attr:`EngineRpc.PRICE_FLOATING_RATE_BOND` — the canonical entries
in the engine's ``rpc_service QuantraServer`` block (mapped
the plan-level placeholder names ``PRICE_BOND_FIXED`` /
``PRICE_BOND_FLOATING`` to these enums).
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
from quantra_common.engine_client._generated.quantra.enums.Frequency import Frequency
from quantra_common.engine_client._generated.quantra.PriceFixedRateBond import (
    PriceFixedRateBondT,
)
from quantra_common.engine_client._generated.quantra.PriceFixedRateBondRequest import (
    PriceFixedRateBondRequestT,
)
from quantra_common.engine_client._generated.quantra.PriceFixedRateBondResponse import (
    PriceFixedRateBondResponse,
)
from quantra_common.engine_client._generated.quantra.PriceFloatingRateBond import (
    PriceFloatingRateBondT,
)
from quantra_common.engine_client._generated.quantra.PriceFloatingRateBondRequest import (
    PriceFloatingRateBondRequestT,
)
from quantra_common.engine_client._generated.quantra.PriceFloatingRateBondResponse import (
    PriceFloatingRateBondResponse,
)
from quantra_common.engine_client._generated.quantra.Pricing import PricingT
from quantra_common.engine_client._generated.quantra.Schedule import ScheduleT
from quantra_common.engine_client.wire_compat import FixedRateBondT, FloatingRateBondT
from quantra_orchestrator.pricing._fb_helpers import (
    CANONICAL_COUPON_PRICER_ID,
    build_canonical_yield,
    build_index_ref,
)
from quantra_orchestrator.pricing._translator import (
    CurveRole,
    CurveTranslationError,
    QuoteResolutionError,
    ResolvedMarketData,
    build_pricing_from_resolved,
    float_leg_frequency,
    resolved_curve_id,
)
from quantra_orchestrator.pricing.bonds.errors import (
    BondCurveResolutionFailedError,
    BondMissingTradeFieldsError,
    BondQuoteResolutionFailedError,
)
from quantra_orchestrator.pricing.bonds.models import (
    FixedBondResult,
    FixedBondTrade,
    FloatingBondResult,
    FloatingBondTrade,
)
from quantra_orchestrator.pricing.concurrency import EngineBatch
from quantra_orchestrator.tracing.stages import serialize_fb_object

FIXED_BOND_ENGINE_RPC: Final[EngineRpc] = EngineRpc.PRICE_FIXED_RATE_BOND
FLOATING_BOND_ENGINE_RPC: Final[EngineRpc] = EngineRpc.PRICE_FLOATING_RATE_BOND

_DEFAULT_FACE: Final[float] = 100.0
_DEFAULT_SETTLEMENT_DAYS: Final[int] = 2
_DEFAULT_REDEMPTION: Final[float] = 100.0
_DEFAULT_SPREAD: Final[float] = 0.001
_DEFAULT_FIXING_DAYS: Final[int] = 2

# Economic fields with NO sensible market-convention default. Historically a
# missing key here silently priced a fixture bond (5 % coupon, 2024→2029
# schedule) — a silent default. Now a hard 422
# ``bond_missing_trade_fields`` per field.
_REQUIRED_FIXED_BOND_FIELDS: Final[tuple[str, ...]] = (
    "coupon_rate",
    "issue_date",
    "effective_date",
    "termination_date",
)
_REQUIRED_FLOATING_BOND_FIELDS: Final[tuple[str, ...]] = (
    "issue_date",
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
        raise BondMissingTradeFieldsError(product=product, missing_fields=missing)


def _pricing_from_resolved(
    resolved: ResolvedMarketData, *, include_coupon_pricer: bool = False
) -> PricingT:
    """Build the ``Pricing`` graph faithfully, mapping translator errors to bond 422s.

    The neutral translator failures are mapped onto the
    bonds error codes (``bond_quote_resolution_failed`` /
    ``bond_curve_resolution_failed``) so the catalog is unchanged. ``bonds``
    always asks for ``bond_pricing_details`` (the decode surface reports
    duration / convexity); floating additionally needs the coupon pricer.
    """

    try:
        return build_pricing_from_resolved(
            resolved,
            bond_pricing_details=True,
            include_coupon_pricer=include_coupon_pricer,
        )
    except QuoteResolutionError as exc:
        raise BondQuoteResolutionFailedError(
            missing_canonical_ids=exc.missing_canonical_ids,
            as_of=resolved.as_of,
        ) from exc
    except CurveTranslationError as exc:
        raise BondCurveResolutionFailedError(reason=exc.reason, details=exc.details) from exc


def _discount_curve_id(resolved: ResolvedMarketData) -> str:
    """Resolved discount-curve id by role, falling back to the first curve."""

    return resolved.curve_id_for_role(CurveRole.DISCOUNT) or resolved_curve_id(resolved.curves[0])


def build_fixed_bond_request(
    trades: Sequence[FixedBondTrade],
    *,
    resolved: ResolvedMarketData,
) -> bytes:
    """Encode one batch of fixed-rate bond trades as ``PriceFixedRateBondRequest``.

    The ``Pricing`` context is built faithfully from the caller's resolved
    discount curve + quotes, not a canonical fixture; the
    per-trade ``discountingCurve`` ref honours the resolved entity id by
    role. The resolved graph reaches here by route-closure capture.
    """

    builder = flatbuffers.Builder(2048)
    # ForceDefaults: the engine requires every set field explicit on the wire;
    # zero-default enums/scalars (e.g. Frequency.Annual == 0) must not be omitted.
    builder.ForceDefaults(True)
    request = PriceFixedRateBondRequestT()
    request.pricing = _pricing_from_resolved(resolved)
    discount_curve_id = _discount_curve_id(resolved)
    request.bonds = [
        _build_one_fixed_bond(trade, discount_curve_id=discount_curve_id) for trade in trades
    ]
    builder.Finish(request.Pack(builder))
    return bytes(builder.Output())


def build_floating_bond_request(
    trades: Sequence[FloatingBondTrade],
    *,
    resolved: ResolvedMarketData,
) -> bytes:
    """Encode one batch of floating-rate bond trades as ``PriceFloatingRateBondRequest``.

    Built faithfully from the caller's resolved discount + projection curves,
    the resolved projection index, and quotes. The pricing
    context additionally carries the default zero-vol Black-Ibor ``CouponPricer``
    the engine's floating-bond pricer requires (pure config, not a discarded
    resolved input). The per-trade ``discountingCurve`` / ``forwardingCurve`` /
    ``index`` refs honour the resolved discount / projection / index ids by
    role, never ``CANONICAL_*``.
    """

    builder = flatbuffers.Builder(2048)
    # ForceDefaults: the engine requires every set field explicit on the wire;
    # zero-default enums/scalars (e.g. Frequency.Annual == 0) must not be omitted.
    builder.ForceDefaults(True)
    request = PriceFloatingRateBondRequestT()
    request.pricing = _pricing_from_resolved(resolved, include_coupon_pricer=True)
    discount_curve_id = _discount_curve_id(resolved)
    projection_curve_id = resolved.curve_id_for_role(CurveRole.PROJECTION) or discount_curve_id
    forwarding_index_id = resolved.forwarding_index_id
    float_frequency = float_leg_frequency(resolved)
    request.bonds = [
        _build_one_floating_bond(
            trade,
            discount_curve_id=discount_curve_id,
            projection_curve_id=projection_curve_id,
            forwarding_index_id=forwarding_index_id,
            float_frequency=float_frequency,
        )
        for trade in trades
    ]
    builder.Finish(request.Pack(builder))
    return bytes(builder.Output())


def decode_fixed_bond_response(
    response_bytes: bytes,
    *,
    expected_count: int,
) -> list[FixedBondResult]:
    """Decode ``PriceFixedRateBondResponse`` bytes into typed results."""

    if not response_bytes:
        msg = "decode_fixed_bond_response: empty engine response."
        raise ValueError(msg)
    try:
        response = PriceFixedRateBondResponse.GetRootAs(response_bytes, 0)
        actual = response.BondsLength()
    except Exception as exc:
        msg = f"decode_fixed_bond_response: malformed engine response ({exc})."
        raise ValueError(msg) from exc

    if actual != expected_count:
        msg = (
            "decode_fixed_bond_response: engine returned "
            f"{actual} result(s), expected {expected_count}."
        )
        raise ValueError(msg)

    results: list[FixedBondResult] = []
    for i in range(actual):
        bond_response = response.Bonds(i)
        if bond_response is None:
            msg = f"decode_fixed_bond_response: null bond result at index {i}."
            raise ValueError(msg)
        results.append(
            FixedBondResult(
                npv=float(bond_response.Npv()),
                clean_price=float(bond_response.CleanPrice()),
                dirty_price=float(bond_response.DirtyPrice()),
                accrued=float(bond_response.AccruedAmount()),
                yield_to_maturity=float(bond_response.Yield()),
                extras={
                    "accrued_days": float(bond_response.AccruedDays()),
                    "macaulay_duration": float(bond_response.MacaulayDuration()),
                    "modified_duration": float(bond_response.ModifiedDuration()),
                    "convexity": float(bond_response.Convexity()),
                    "bps": float(bond_response.Bps()),
                },
            )
        )
    return results


def decode_floating_bond_response(
    response_bytes: bytes,
    *,
    expected_count: int,
) -> list[FloatingBondResult]:
    """Decode ``PriceFloatingRateBondResponse`` bytes into typed results."""

    if not response_bytes:
        msg = "decode_floating_bond_response: empty engine response."
        raise ValueError(msg)
    try:
        response = PriceFloatingRateBondResponse.GetRootAs(response_bytes, 0)
        actual = response.BondsLength()
    except Exception as exc:
        msg = f"decode_floating_bond_response: malformed engine response ({exc})."
        raise ValueError(msg) from exc

    if actual != expected_count:
        msg = (
            "decode_floating_bond_response: engine returned "
            f"{actual} result(s), expected {expected_count}."
        )
        raise ValueError(msg)

    results: list[FloatingBondResult] = []
    for i in range(actual):
        bond_response = response.Bonds(i)
        if bond_response is None:
            msg = f"decode_floating_bond_response: null bond result at index {i}."
            raise ValueError(msg)
        results.append(
            FloatingBondResult(
                npv=float(bond_response.Npv()),
                clean_price=float(bond_response.CleanPrice()),
                dirty_price=float(bond_response.DirtyPrice()),
                accrued=float(bond_response.AccruedAmount()),
                extras={
                    "yield": float(bond_response.Yield()),
                    "accrued_days": float(bond_response.AccruedDays()),
                    "macaulay_duration": float(bond_response.MacaulayDuration()),
                    "modified_duration": float(bond_response.ModifiedDuration()),
                    "convexity": float(bond_response.Convexity()),
                    "bps": float(bond_response.Bps()),
                },
            )
        )
    return results


def decode_fixed_bond_request_wire(request_bytes: bytes) -> dict[str, Any]:
    """Decode the EXACT transmitted fixed-rate bond request bytes to JSON.

    Reads the buffer with the generated FlatBuffers reader
    (``PriceFixedRateBondRequestT.InitFromPackedBuf``) so the result is provably
    derived from the wire bytes, then renders it JSON-safe via the shared
    :func:`serialize_fb_object`. Best-effort: a malformed buffer degrades to a
    marker dict, never raises. Mirrors ``decode_swap_ir_request_wire``.
    """

    try:
        request = PriceFixedRateBondRequestT.InitFromPackedBuf(request_bytes, 0)
    except Exception as exc:  # tracing is best-effort — never raise
        return {"__undecodable__": True, "exc_type": type(exc).__name__}
    return serialize_fb_object(request)


def decode_floating_bond_request_wire(request_bytes: bytes) -> dict[str, Any]:
    """Decode the EXACT transmitted floating-rate bond request bytes to JSON.

    Reads the buffer with the generated FlatBuffers reader
    (``PriceFloatingRateBondRequestT.InitFromPackedBuf``) so the result is
    provably derived from the wire bytes, then renders it JSON-safe via the
    shared :func:`serialize_fb_object`. Best-effort: a malformed buffer degrades
    to a marker dict, never raises. Mirrors ``decode_swap_ir_request_wire``.
    """

    try:
        request = PriceFloatingRateBondRequestT.InitFromPackedBuf(request_bytes, 0)
    except Exception as exc:  # tracing is best-effort — never raise
        return {"__undecodable__": True, "exc_type": type(exc).__name__}
    return serialize_fb_object(request)


async def price_fixed_bond_batch(
    engine: EngineClient,
    batch: EngineBatch[FixedBondTrade],
    *,
    resolved: ResolvedMarketData,
) -> Sequence[FixedBondResult]:
    """The per-product translator injected into the ``execute`` runner.

    ``resolved`` is the route-captured :class:`ResolvedMarketData` the
    route's ``price_batch`` lambda binds; it carries the faithful discount curve
    + quotes the request is assembled from.
    """

    request_bytes = build_fixed_bond_request(batch.trades, resolved=resolved)
    response_bytes = await engine.call(FIXED_BOND_ENGINE_RPC, request_bytes)
    return decode_fixed_bond_response(response_bytes, expected_count=len(batch.trades))


async def price_floating_bond_batch(
    engine: EngineClient,
    batch: EngineBatch[FloatingBondTrade],
    *,
    resolved: ResolvedMarketData,
) -> Sequence[FloatingBondResult]:
    """The per-product translator for the floating-rate bond endpoint.

    ``resolved`` is the route-captured :class:`ResolvedMarketData`
    carrying the faithful discount + projection curves, projection index, and
    quotes the request is assembled from.
    """

    request_bytes = build_floating_bond_request(batch.trades, resolved=resolved)
    response_bytes = await engine.call(FLOATING_BOND_ENGINE_RPC, request_bytes)
    return decode_floating_bond_response(response_bytes, expected_count=len(batch.trades))


def _build_one_fixed_bond(trade: FixedBondTrade, *, discount_curve_id: str) -> PriceFixedRateBondT:
    """Map one :class:`FixedBondTrade` to a ``PriceFixedRateBond`` FB graph.

    The ``discountingCurve`` ref honours the resolved discount-curve id the
    translator placed in ``Pricing.rates.curves``, not ``CANONICAL_*``.
    """

    body: Mapping[str, Any] = trade.bond or {}
    _require_trade_fields(body, _REQUIRED_FIXED_BOND_FIELDS, product="bonds_fixed")
    face_amount = float(body.get("face_amount", _DEFAULT_FACE))
    settlement_days = int(body.get("settlement_days", _DEFAULT_SETTLEMENT_DAYS))
    redemption = float(body.get("redemption", _DEFAULT_REDEMPTION))
    rate = float(body["coupon_rate"])
    issue_date = str(body["issue_date"])
    effective_date = str(body["effective_date"])
    termination_date = str(body["termination_date"])

    schedule = ScheduleT()
    schedule.effectiveDate = effective_date
    schedule.terminationDate = termination_date
    schedule.calendar = Calendar.TARGET
    schedule.frequency = Frequency.Annual
    schedule.convention = BusinessDayConvention.Unadjusted
    schedule.terminationDateConvention = BusinessDayConvention.Unadjusted
    schedule.dateGenerationRule = DateGenerationRule.Backward
    schedule.endOfMonth = False

    bond = FixedRateBondT()
    bond.settlementDays = settlement_days
    bond.faceAmount = face_amount
    bond.rate = rate
    bond.accrualDayCounter = DayCounter.ActualActual
    bond.paymentConvention = BusinessDayConvention.Unadjusted
    bond.redemption = redemption
    bond.issueDate = issue_date
    bond.schedule = schedule

    price_bond = PriceFixedRateBondT()
    price_bond.fixedRateBond = bond
    price_bond.discountingCurve = discount_curve_id
    price_bond.yield_ = build_canonical_yield()
    return price_bond


def _build_one_floating_bond(
    trade: FloatingBondTrade,
    *,
    discount_curve_id: str,
    projection_curve_id: str,
    forwarding_index_id: str,
    float_frequency: int,
) -> PriceFloatingRateBondT:
    """Map one :class:`FloatingBondTrade` to a ``PriceFloatingRateBond`` FB graph.

    The ``discountingCurve`` / ``forwardingCurve`` refs honour the resolved
    discount / projection curve ids and ``index`` references the
    resolved projection index id, not ``CANONICAL_*``. ``couponPricer`` keeps
    the default zero-vol Black-Ibor pricer id (pure config, not a resolved
    entity). ``float_frequency`` is the coupon frequency derived from that
    index's tenor (shared with swap_ir/swaption) — Quarterly for a 3M
    index, Semiannual for 6M, etc. — so the coupon schedule and the projection
    index agree; it is the module default when no index was resolved."""

    body: Mapping[str, Any] = trade.bond or {}
    _require_trade_fields(body, _REQUIRED_FLOATING_BOND_FIELDS, product="bonds_floating")
    face_amount = float(body.get("face_amount", _DEFAULT_FACE))
    settlement_days = int(body.get("settlement_days", _DEFAULT_SETTLEMENT_DAYS))
    redemption = float(body.get("redemption", _DEFAULT_REDEMPTION))
    spread = float(body.get("spread", _DEFAULT_SPREAD))
    fixing_days = int(body.get("fixing_days", _DEFAULT_FIXING_DAYS))
    in_arrears = bool(body.get("in_arrears", False))
    issue_date = str(body["issue_date"])
    effective_date = str(body["effective_date"])
    termination_date = str(body["termination_date"])

    schedule = ScheduleT()
    schedule.effectiveDate = effective_date
    schedule.terminationDate = termination_date
    schedule.calendar = Calendar.TARGET
    schedule.frequency = float_frequency
    schedule.convention = BusinessDayConvention.ModifiedFollowing
    schedule.terminationDateConvention = BusinessDayConvention.ModifiedFollowing
    schedule.dateGenerationRule = DateGenerationRule.Forward

    bond = FloatingRateBondT()
    bond.settlementDays = settlement_days
    bond.faceAmount = face_amount
    bond.schedule = schedule
    bond.index = build_index_ref(forwarding_index_id)
    bond.accrualDayCounter = DayCounter.Actual360
    bond.paymentConvention = BusinessDayConvention.ModifiedFollowing
    bond.fixingDays = fixing_days
    bond.spread = spread
    bond.inArrears = in_arrears
    bond.redemption = redemption
    bond.issueDate = issue_date

    price_bond = PriceFloatingRateBondT()
    price_bond.floatingRateBond = bond
    price_bond.discountingCurve = discount_curve_id
    price_bond.forwardingCurve = projection_curve_id
    price_bond.couponPricer = CANONICAL_COUPON_PRICER_ID
    price_bond.yield_ = build_canonical_yield()
    return price_bond


__all__ = [
    "FIXED_BOND_ENGINE_RPC",
    "FLOATING_BOND_ENGINE_RPC",
    "build_fixed_bond_request",
    "build_floating_bond_request",
    "decode_fixed_bond_response",
    "decode_floating_bond_response",
    "price_fixed_bond_batch",
    "price_floating_bond_batch",
]
