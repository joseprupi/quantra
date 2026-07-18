"""Engine wire-layer for the inflation-swap pricing path.

Three callables, structurally identical to the swap_ir / swaption /
bonds / cds / equity_options ``engine_io`` modules (the per-
product split):

* :func:`build_swap_inflation_request` — turn one
  :class:`EngineBatch[InflationSwapTrade]` into the FlatBuffers
  payload the engine expects on the
  :attr:`EngineRpc.PRICE_ZERO_COUPON_INFLATION_SWAP` *or*
  :attr:`EngineRpc.PRICE_YEAR_ON_YEAR_INFLATION_SWAP` RPC,
  branching on the ``swap_kind`` discriminator carried in
  ``shared_inputs``.
* :func:`decode_swap_inflation_response` — turn the engine's
  response bytes (a ``ZeroCouponInflationSwapResponse`` /
  ``YearOnYearInflationSwapResponse`` flatbuffer) into a
  ``Sequence[InflationSwapResult]`` (one per trade in the batch,
  in input order).
* :func:`price_swap_inflation_batch` — the per-product translator
  the concurrency seam's :func:`execute` runner injects via
  its ``price_batch=`` parameter.

RPC naming (settled): an earlier design referred to a single
``EngineRpc.PRICE_SWAP_INFLATION``
placeholder; the canonical enum
(:mod:`quantra_common.engine_client.rpcs`) has two RPCs — one per
swap kind. Following the precedent of
``PRICE_VANILLA_SWAP`` / ``PRICE_FIXED_RATE_BOND`` /
``PRICE_FLOATING_RATE_BOND`` / ``PRICE_EQUITY_OPTION``, this
module honors the enum and branches on ``swap_kind`` rather than
introducing a new wrapper RPC. The ``shared_inputs`` carries the
discriminator so :func:`execute` doesn't need a
per-RPC seam.

Trade-body keys honored (matching the engine's
``test_price_zero_coupon_inflation_swap_smoke`` / YYIIS reference
fixtures):

* Top-level ``zero_coupon_inflation_swap`` / ``year_on_year_inflation_swap``
  block; engine-canonical nested shape preferred but loose
  top-level keys also accepted as a forward-compat fallback.
* ``swap_type`` (``"Payer"`` / ``"Receiver"``, default Payer).
* ``notional`` (float, default 1,000,000).
* ZCIIS: ``start_date`` / ``maturity_date`` (default 5Y window
  starting at the canonical as-of date), ``fixed_rate``,
  ``fixed_calendar``, ``fixed_convention``, ``day_counter``,
  ``observation_lag``, ``observation_interpolation``,
  ``adjust_observation_dates``, ``inflation_calendar``,
  ``inflation_convention``, ``inflation_index_id``.
* YYIIS: ``fixed_schedule`` / ``yoy_schedule``
  (``effective_date`` / ``termination_date`` / ``frequency`` /
  ``calendar`` / ``convention`` /
  ``termination_date_convention`` / ``date_generation_rule``),
  ``fixed_rate``, ``fixed_day_counter``, ``yoy_day_counter``,
  ``observation_lag``, ``observation_interpolation``, ``spread``,
  ``payment_calendar``, ``payment_convention``,
  ``inflation_index_id``.
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
from quantra_common.engine_client._generated.quantra.enums.CPIInterpolationType import (
    CPIInterpolationType,
)
from quantra_common.engine_client._generated.quantra.enums.DateGenerationRule import (
    DateGenerationRule,
)
from quantra_common.engine_client._generated.quantra.enums.DayCounter import DayCounter
from quantra_common.engine_client._generated.quantra.enums.Frequency import Frequency
from quantra_common.engine_client._generated.quantra.enums.SwapType import SwapType
from quantra_common.engine_client._generated.quantra.enums.TimeUnit import TimeUnit
from quantra_common.engine_client._generated.quantra.Period import PeriodT
from quantra_common.engine_client._generated.quantra.PriceYearOnYearInflationSwap import (
    PriceYearOnYearInflationSwapT,
)
from quantra_common.engine_client._generated.quantra.PriceYearOnYearInflationSwapRequest import (
    PriceYearOnYearInflationSwapRequestT,
)
from quantra_common.engine_client._generated.quantra.PriceYearOnYearInflationSwapResponse import (
    PriceYearOnYearInflationSwapResponse,
)
from quantra_common.engine_client._generated.quantra.PriceZeroCouponInflationSwap import (
    PriceZeroCouponInflationSwapT,
)
from quantra_common.engine_client._generated.quantra.PriceZeroCouponInflationSwapRequest import (
    PriceZeroCouponInflationSwapRequestT,
)
from quantra_common.engine_client._generated.quantra.PriceZeroCouponInflationSwapResponse import (
    PriceZeroCouponInflationSwapResponse,
)
from quantra_common.engine_client._generated.quantra.Schedule import ScheduleT
from quantra_common.engine_client._generated.quantra.YearOnYearInflationSwap import (
    YearOnYearInflationSwapT,
)
from quantra_common.engine_client._generated.quantra.ZeroCouponInflationSwap import (
    ZeroCouponInflationSwapT,
)
from quantra_orchestrator.pricing._translator import (
    CurveRole,
    CurveTranslationError,
    InflationIndexTranslationError,
    QuoteResolutionError,
    ResolvedMarketData,
    build_pricing_from_resolved,
    resolved_curve_id,
    resolved_inflation_index_id,
)
from quantra_orchestrator.pricing.concurrency import EngineBatch
from quantra_orchestrator.pricing.swaps_inflation.errors import (
    SwapInflationCurveResolutionFailedError,
    SwapInflationIndexResolutionFailedError,
    SwapInflationMissingTradeFieldsError,
    SwapInflationQuoteResolutionFailedError,
)
from quantra_orchestrator.pricing.swaps_inflation.models import (
    InflationSwapResult,
    InflationSwapTrade,
)
from quantra_orchestrator.tracing.stages import serialize_fb_object

SWAP_INFLATION_ZERO_COUPON_RPC: Final[EngineRpc] = EngineRpc.PRICE_ZERO_COUPON_INFLATION_SWAP
"""Engine RPC for zero-coupon inflation-indexed swaps (ZCIIS)."""

SWAP_INFLATION_YEAR_ON_YEAR_RPC: Final[EngineRpc] = EngineRpc.PRICE_YEAR_ON_YEAR_INFLATION_SWAP
"""Engine RPC for year-on-year inflation-indexed swaps (YYIIS)."""

_DEFAULT_YYIIS_SPREAD: Final[float] = 0.0

# Economic fields with NO sensible market-convention default. Historically a
# missing key here silently priced a fixture inflation swap (1M notional,
# fixture fixed rates, hardcoded 2025→2030/2027 dates). Now a hard 422
# ``swap_inflation_missing_trade_fields`` per field; YYIIS schedule dates
# are reported with a ``fixed_schedule.`` / ``yoy_schedule.`` prefix.
_REQUIRED_ZCIIS_FIELDS: Final[tuple[str, ...]] = (
    "notional",
    "fixed_rate",
    "start_date",
    "maturity_date",
)
_REQUIRED_YYIIS_FIELDS: Final[tuple[str, ...]] = (
    "notional",
    "fixed_rate",
)
_REQUIRED_YYIIS_SCHEDULE_FIELDS: Final[tuple[str, ...]] = (
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
        raise SwapInflationMissingTradeFieldsError(product=product, missing_fields=missing)


def _require_yyiis_trade_fields(body: Mapping[str, Any]) -> None:
    """YYIIS variant: dates live in the two nested schedule blocks."""

    missing = [field for field in _REQUIRED_YYIIS_FIELDS if body.get(field) in (None, "")]
    for block in ("fixed_schedule", "yoy_schedule"):
        raw = body.get(block)
        schedule: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        missing.extend(
            f"{block}.{field}"
            for field in _REQUIRED_YYIIS_SCHEDULE_FIELDS
            if schedule.get(field) in (None, "")
        )
    if missing:
        raise SwapInflationMissingTradeFieldsError(
            product="swaps_inflation", missing_fields=missing
        )


_SWAP_KIND_ZERO_COUPON: Final[str] = "zero_coupon"
_SWAP_KIND_YEAR_ON_YEAR: Final[str] = "year_on_year"

_SWAP_TYPE_BY_NAME: Final[dict[str, int]] = {
    "Payer": SwapType.Payer,
    "Receiver": SwapType.Receiver,
}

_TIME_UNIT_BY_NAME: Final[dict[str, int]] = {
    "Days": TimeUnit.Days,
    "Weeks": TimeUnit.Weeks,
    "Months": TimeUnit.Months,
    "Years": TimeUnit.Years,
}

_FREQUENCY_BY_NAME: Final[dict[str, int]] = {
    "Once": Frequency.Once,
    "Annual": Frequency.Annual,
    "Semiannual": Frequency.Semiannual,
    "Quarterly": Frequency.Quarterly,
    "Monthly": Frequency.Monthly,
    "Weekly": Frequency.Weekly,
    "Daily": Frequency.Daily,
}

_CALENDAR_BY_NAME: Final[dict[str, int]] = {
    "TARGET": Calendar.TARGET,
    "NullCalendar": Calendar.NullCalendar,
    "UnitedStates": Calendar.UnitedStates,
}

_BDC_BY_NAME: Final[dict[str, int]] = {
    "Following": BusinessDayConvention.Following,
    "ModifiedFollowing": BusinessDayConvention.ModifiedFollowing,
    "Preceding": BusinessDayConvention.Preceding,
    "ModifiedPreceding": BusinessDayConvention.ModifiedPreceding,
    "Unadjusted": BusinessDayConvention.Unadjusted,
}

_DATE_GEN_BY_NAME: Final[dict[str, int]] = {
    "Forward": DateGenerationRule.Forward,
    "Backward": DateGenerationRule.Backward,
}

_DAY_COUNTER_BY_NAME: Final[dict[str, int]] = {
    "Actual360": DayCounter.Actual360,
    "Actual365Fixed": DayCounter.Actual365Fixed,
    "Thirty360": DayCounter.Thirty360,
}

_CPI_INTERP_BY_NAME: Final[dict[str, int]] = {
    "AsIndex": CPIInterpolationType.AsIndex,
    "Flat": CPIInterpolationType.Flat,
    "Linear": CPIInterpolationType.Linear,
}


def build_swap_inflation_request(
    trades: Sequence[InflationSwapTrade],
    shared_inputs: Mapping[str, Any],
    *,
    resolved: ResolvedMarketData,
) -> bytes:
    """Encode one batch of inflation-swap trades into FlatBuffers bytes.

    Branches on ``shared_inputs["swap_kind"]`` to pick between
    :class:`PriceZeroCouponInflationSwapRequestT` (ZCIIS) and
    :class:`PriceYearOnYearInflationSwapRequestT` (YYIIS).

    The ``Pricing`` context is a *faithful* encoding of the caller's
    resolved nominal + inflation curves and inflation index, built
    once per batch by
    :func:`~quantra_orchestrator.pricing._translator.build_pricing_from_resolved`
    — the nominal discount curve under ``rates.curves`` with the
    resolved quote values substituted in, the inflation curve
    + index under ``Pricing.inflation`` (the index body verbatim per
    the no-MD-fallback rule, the inflation-curve helpers quote-substituted), never the
    canonical EU-HICP fixture. The ``swap_kind`` discriminator stays
    in ``shared_inputs`` and is threaded in as the
    ``inflation_swap_kind`` argument; ``resolved`` reaches here by
    route-closure capture, not through the seam. Required economic
    fields (notional, rates, dates) must be present on each trade
    (422 ``swap_inflation_missing_trade_fields`` otherwise), and the
    per-trade discount / inflation curve + inflation index references
    honour the resolved entity ids. Neutral translator
    failures map onto the swaps_inflation 422 codes
    (``swap_inflation_quote_resolution_failed`` /
    ``swap_inflation_index_resolution_failed`` /
    ``swap_inflation_curve_resolution_failed``) so the catalog is
    unchanged.
    """

    swap_kind = _resolve_swap_kind(shared_inputs)
    try:
        pricing = build_pricing_from_resolved(resolved, inflation_swap_kind=swap_kind)
    except QuoteResolutionError as exc:
        raise SwapInflationQuoteResolutionFailedError(
            missing_canonical_ids=exc.missing_canonical_ids,
            as_of=resolved.as_of,
        ) from exc
    except InflationIndexTranslationError as exc:
        raise SwapInflationIndexResolutionFailedError(
            reason=exc.reason, details=exc.details
        ) from exc
    except CurveTranslationError as exc:
        raise SwapInflationCurveResolutionFailedError(
            reason=exc.reason, details=exc.details
        ) from exc

    discount_curve_id = _resolve_nominal_curve_ref(resolved)
    inflation_curve_id = _resolve_inflation_curve_ref(resolved)
    inflation_index_id = _resolve_inflation_index_ref(resolved)

    builder = flatbuffers.Builder(2048)
    # ForceDefaults: the engine requires every set field explicit on the wire;
    # zero-default enums/scalars (e.g. Frequency.Annual == 0) must not be omitted.
    builder.ForceDefaults(True)

    if swap_kind == _SWAP_KIND_YEAR_ON_YEAR:
        request = PriceYearOnYearInflationSwapRequestT()
        request.pricing = pricing
        request.swaps = [
            _build_one_yyiis(
                trade,
                discount_curve_id=discount_curve_id,
                inflation_curve_id=inflation_curve_id,
                inflation_index_id=inflation_index_id,
            )
            for trade in trades
        ]
        request.includeFlows = False
        builder.Finish(request.Pack(builder))
    else:
        request_zc = PriceZeroCouponInflationSwapRequestT()
        request_zc.pricing = pricing
        request_zc.swaps = [
            _build_one_zciis(
                trade,
                discount_curve_id=discount_curve_id,
                inflation_curve_id=inflation_curve_id,
                inflation_index_id=inflation_index_id,
            )
            for trade in trades
        ]
        request_zc.includeFlows = False
        builder.Finish(request_zc.Pack(builder))

    return bytes(builder.Output())


def _resolve_nominal_curve_ref(resolved: ResolvedMarketData) -> str:
    """Return the resolved nominal discount curve id by role.

    Reads the nominal reference id off the role map; falls back to the first
    translated curve so a role-less caller stays robust.
    ``build_pricing_from_resolved`` has already rejected the empty-curve case, so
    ``curves[0]`` is safe. Never ``CANONICAL_DISCOUNT_CURVE_ID``.
    """

    return resolved.curve_id_for_role(CurveRole.NOMINAL) or resolved_curve_id(resolved.curves[0])


def _resolve_inflation_curve_ref(resolved: ResolvedMarketData) -> str:
    """Return the resolved inflation curve id the per-trade ``inflationCurve`` ref points at.

    The inflation route always supplies exactly one resolved inflation curve, so
    the reference honours that resolved entity id, never
    ``CANONICAL_INFLATION_CURVE_ID``. An absent curve is defensively surfaced as
    the swaps_inflation curve-resolution 422.
    """

    if resolved.inflation_curve is None:
        raise SwapInflationCurveResolutionFailedError(
            reason=("No resolved inflation curve to reference in the inflation-swap request."),
            details=[{"role": "inflation", "resolved_inflation_curve": None}],
        )
    return resolved_curve_id(resolved.inflation_curve)


def _resolve_inflation_index_ref(resolved: ResolvedMarketData) -> str:
    """Return the resolved inflation index id the per-trade ``inflationIndexId`` ref points at.

    The inflation route always supplies exactly one resolved inflation index, so
    the reference honours its resolved ``index_id``, never
    ``CANONICAL_INFLATION_INDEX_ID``. An absent index is defensively surfaced as
    the swaps_inflation index-resolution 422.
    """

    if resolved.inflation_index is None:
        raise SwapInflationIndexResolutionFailedError(
            reason=("No resolved inflation index to reference in the inflation-swap request."),
            details=[{"resolved_inflation_index": None}],
        )
    return resolved_inflation_index_id(resolved.inflation_index)


def decode_swap_inflation_response(
    response_bytes: bytes,
    *,
    expected_count: int,
    swap_kind: str,
) -> list[InflationSwapResult]:
    """Decode the engine's inflation-swap response into typed results.

    Branches on ``swap_kind`` to pick between
    :class:`ZeroCouponInflationSwapResponse` (per-trade ``npv``,
    ``fair_rate``, ``fixed_leg_*`` / ``inflation_leg_npv``) and
    :class:`YearOnYearInflationSwapResponse` (adds ``fair_spread``,
    ``yoy_leg_*`` instead of ``inflation_leg_*``).
    """

    if not response_bytes:
        msg = "decode_swap_inflation_response: empty engine response."
        raise ValueError(msg)

    if swap_kind == _SWAP_KIND_YEAR_ON_YEAR:
        return _decode_yyiis(response_bytes, expected_count=expected_count)
    return _decode_zciis(response_bytes, expected_count=expected_count)


def decode_swap_inflation_request_wire(
    request_bytes: bytes, *, rpc: str | None = None
) -> dict[str, Any]:
    """Decode the EXACT transmitted inflation-swap request bytes to JSON.

    Inflation swaps ride two distinct request roots — ZCIIS
    (:class:`PriceZeroCouponInflationSwapRequestT`) and YYIIS
    (:class:`PriceYearOnYearInflationSwapRequestT`) — so the reader is picked
    from the captured RPC (``rpc``): the YYIIS reader only when the wire RPC is
    :attr:`SWAP_INFLATION_YEAR_ON_YEAR_RPC`, otherwise the ZCIIS reader (the
    default one-variant case). Renders the unpacked object JSON-safe via the
    shared :func:`serialize_fb_object`. Best-effort: a malformed buffer degrades
    to a marker dict, never raises. Mirrors ``decode_swap_ir_request_wire``.
    """

    reader = (
        PriceYearOnYearInflationSwapRequestT
        if rpc == SWAP_INFLATION_YEAR_ON_YEAR_RPC.value
        else PriceZeroCouponInflationSwapRequestT
    )
    try:
        request = reader.InitFromPackedBuf(request_bytes, 0)
    except Exception as exc:  # tracing is best-effort — never raise
        return {"__undecodable__": True, "exc_type": type(exc).__name__}
    return serialize_fb_object(request)


async def price_swap_inflation_batch(
    engine: EngineClient,
    batch: EngineBatch[InflationSwapTrade],
    *,
    resolved: ResolvedMarketData,
) -> Sequence[InflationSwapResult]:
    """The per-product translator injected into the ``execute`` runner.

    One ``EngineClient.call`` per batch, dispatched to the
    :data:`SWAP_INFLATION_ZERO_COUPON_RPC` or
    :data:`SWAP_INFLATION_YEAR_ON_YEAR_RPC` RPC name based on the
    ``swap_kind`` discriminator threaded through ``shared_inputs``
    (the canonical enum has one RPC per swap kind, same
    precedent). ``resolved`` is the route-captured
    :class:`ResolvedMarketData` the route's ``price_batch``
    lambda binds; it carries the faithful nominal + inflation curves,
    quotes and inflation index the request is assembled from.
    """

    swap_kind = _resolve_swap_kind(batch.shared_inputs)
    rpc = (
        SWAP_INFLATION_YEAR_ON_YEAR_RPC
        if swap_kind == _SWAP_KIND_YEAR_ON_YEAR
        else SWAP_INFLATION_ZERO_COUPON_RPC
    )

    request_bytes = build_swap_inflation_request(
        batch.trades, batch.shared_inputs, resolved=resolved
    )
    response_bytes = await engine.call(rpc, request_bytes)
    return decode_swap_inflation_response(
        response_bytes,
        expected_count=len(batch.trades),
        swap_kind=swap_kind,
    )


# ---------------------------------------------------------------------------
# Discriminator helpers
# ---------------------------------------------------------------------------


def _resolve_swap_kind(shared_inputs: Mapping[str, Any]) -> str:
    raw = shared_inputs.get("swap_kind")
    if isinstance(raw, str) and raw == _SWAP_KIND_YEAR_ON_YEAR:
        return _SWAP_KIND_YEAR_ON_YEAR
    return _SWAP_KIND_ZERO_COUPON


def _swap_body(trade: InflationSwapTrade, *, kind: str) -> Mapping[str, Any]:
    """Return the engine-canonical inflation-swap inner block.

    Accepts both the engine-canonical nested shape
    (``{"zero_coupon_inflation_swap": {...}}``) and a loose
    top-level shape (the body itself carries the swap fields).
    """

    body_root: Mapping[str, Any] = trade.swap or {}
    nested_key = (
        "year_on_year_inflation_swap"
        if kind == _SWAP_KIND_YEAR_ON_YEAR
        else "zero_coupon_inflation_swap"
    )

    swaps_list = body_root.get("swaps")
    if isinstance(swaps_list, list):
        for entry in swaps_list:
            if isinstance(entry, Mapping):
                inner = entry.get(nested_key)
                if isinstance(inner, Mapping):
                    return inner
                if kind == _SWAP_KIND_ZERO_COUPON and "zero_coupon_inflation_swap" in entry:
                    candidate = entry["zero_coupon_inflation_swap"]
                    if isinstance(candidate, Mapping):
                        return candidate
                if kind == _SWAP_KIND_YEAR_ON_YEAR and "year_on_year_inflation_swap" in entry:
                    candidate = entry["year_on_year_inflation_swap"]
                    if isinstance(candidate, Mapping):
                        return candidate

    nested = body_root.get(nested_key)
    if isinstance(nested, Mapping):
        return nested
    return body_root


# ---------------------------------------------------------------------------
# ZCIIS builder + decoder
# ---------------------------------------------------------------------------


def _build_one_zciis(
    trade: InflationSwapTrade,
    *,
    discount_curve_id: str,
    inflation_curve_id: str,
    inflation_index_id: str,
) -> PriceZeroCouponInflationSwapT:
    """Map one :class:`InflationSwapTrade` to a ``PriceZeroCouponInflationSwap``.

    The ``discountingCurve`` / ``inflationCurve`` / ``inflationIndexId``
    references honour the resolved entity ids the translator placed in the
    ``Pricing`` graph, never ``CANONICAL_*``.
    """

    body = _swap_body(trade, kind=_SWAP_KIND_ZERO_COUPON)
    _require_trade_fields(body, _REQUIRED_ZCIIS_FIELDS, product="swaps_inflation")

    swap = ZeroCouponInflationSwapT()
    swap.swapType = _enum_or_default(_SWAP_TYPE_BY_NAME, body.get("swap_type"), SwapType.Payer)
    swap.notional = float(body["notional"])
    swap.startDate = str(body["start_date"])
    swap.maturityDate = str(body["maturity_date"])
    swap.fixedCalendar = _enum_or_default(
        _CALENDAR_BY_NAME, body.get("fixed_calendar"), Calendar.TARGET
    )
    swap.fixedConvention = _enum_or_default(
        _BDC_BY_NAME,
        body.get("fixed_convention"),
        BusinessDayConvention.ModifiedFollowing,
    )
    swap.dayCounter = _enum_or_default(
        _DAY_COUNTER_BY_NAME, body.get("day_counter"), DayCounter.Actual365Fixed
    )
    swap.fixedRate = float(body["fixed_rate"])
    swap.inflationIndexId = inflation_index_id
    swap.observationLag = _build_period(
        body.get("observation_lag"), default_n=3, default_unit=TimeUnit.Months
    )
    swap.observationInterpolation = _enum_or_default(
        _CPI_INTERP_BY_NAME,
        body.get("observation_interpolation"),
        CPIInterpolationType.Linear,
    )
    swap.adjustObservationDates = bool(body.get("adjust_observation_dates", False))
    swap.inflationCalendar = _enum_or_default(
        _CALENDAR_BY_NAME, body.get("inflation_calendar"), Calendar.NullCalendar
    )
    swap.inflationConvention = _enum_or_default(
        _BDC_BY_NAME,
        body.get("inflation_convention"),
        BusinessDayConvention.Following,
    )

    price = PriceZeroCouponInflationSwapT()
    price.zeroCouponInflationSwap = swap
    price.discountingCurve = discount_curve_id
    price.inflationCurve = inflation_curve_id
    return price


def _decode_zciis(response_bytes: bytes, *, expected_count: int) -> list[InflationSwapResult]:
    try:
        response = PriceZeroCouponInflationSwapResponse.GetRootAs(response_bytes, 0)
        actual = response.SwapsLength()
    except Exception as exc:
        msg = f"decode_swap_inflation_response: malformed ZCIIS engine response ({exc})."
        raise ValueError(msg) from exc

    if actual != expected_count:
        msg = (
            "decode_swap_inflation_response: engine returned "
            f"{actual} ZCIIS result(s), expected {expected_count}."
        )
        raise ValueError(msg)

    results: list[InflationSwapResult] = []
    for i in range(actual):
        swap_response = response.Swaps(i)
        if swap_response is None:
            msg = f"decode_swap_inflation_response: null ZCIIS swap result at index {i}."
            raise ValueError(msg)
        npv = float(swap_response.Npv())
        fair_rate = float(swap_response.FairRate())
        fixed_leg_npv = float(swap_response.FixedLegNpv())
        inflation_leg_npv = float(swap_response.InflationLegNpv())
        fixed_leg_bps = float(swap_response.FixedLegBps())
        results.append(
            InflationSwapResult(
                npv=npv,
                fair_rate=fair_rate,
                fair_spread=None,
                fixed_leg_bps=fixed_leg_bps,
                fixed_leg_npv=fixed_leg_npv,
                inflation_leg_npv=inflation_leg_npv,
                yoy_leg_bps=None,
                yoy_leg_npv=None,
                swap_kind=_SWAP_KIND_ZERO_COUPON,
                extras={
                    "npv": npv,
                    "fair_rate": fair_rate,
                    "fixed_leg_bps": fixed_leg_bps,
                    "fixed_leg_npv": fixed_leg_npv,
                    "inflation_leg_npv": inflation_leg_npv,
                },
            )
        )
    return results


# ---------------------------------------------------------------------------
# YYIIS builder + decoder
# ---------------------------------------------------------------------------


def _build_one_yyiis(
    trade: InflationSwapTrade,
    *,
    discount_curve_id: str,
    inflation_curve_id: str,
    inflation_index_id: str,
) -> PriceYearOnYearInflationSwapT:
    """Map one :class:`InflationSwapTrade` to a ``PriceYearOnYearInflationSwap``.

    The ``discountingCurve`` / ``inflationCurve`` / ``inflationIndexId``
    references honour the resolved entity ids the translator placed in the
    ``Pricing`` graph, never ``CANONICAL_*``.
    """

    body = _swap_body(trade, kind=_SWAP_KIND_YEAR_ON_YEAR)
    _require_yyiis_trade_fields(body)

    swap = YearOnYearInflationSwapT()
    swap.swapType = _enum_or_default(_SWAP_TYPE_BY_NAME, body.get("swap_type"), SwapType.Payer)
    swap.notional = float(body["notional"])
    swap.fixedSchedule = _build_yyiis_schedule(body.get("fixed_schedule"), is_fixed=True)
    swap.fixedRate = float(body["fixed_rate"])
    swap.fixedDayCounter = _enum_or_default(
        _DAY_COUNTER_BY_NAME,
        body.get("fixed_day_counter"),
        DayCounter.Actual365Fixed,
    )
    swap.yoySchedule = _build_yyiis_schedule(body.get("yoy_schedule"), is_fixed=False)
    swap.inflationIndexId = inflation_index_id
    swap.observationLag = _build_period(
        body.get("observation_lag"), default_n=3, default_unit=TimeUnit.Months
    )
    swap.observationInterpolation = _enum_or_default(
        _CPI_INTERP_BY_NAME,
        body.get("observation_interpolation"),
        CPIInterpolationType.Linear,
    )
    swap.spread = float(body.get("spread", _DEFAULT_YYIIS_SPREAD))
    swap.yoyDayCounter = _enum_or_default(
        _DAY_COUNTER_BY_NAME,
        body.get("yoy_day_counter"),
        DayCounter.Actual365Fixed,
    )
    swap.paymentCalendar = _enum_or_default(
        _CALENDAR_BY_NAME, body.get("payment_calendar"), Calendar.TARGET
    )
    swap.paymentConvention = _enum_or_default(
        _BDC_BY_NAME,
        body.get("payment_convention"),
        BusinessDayConvention.ModifiedFollowing,
    )

    price = PriceYearOnYearInflationSwapT()
    price.yearOnYearInflationSwap = swap
    price.discountingCurve = discount_curve_id
    price.inflationCurve = inflation_curve_id
    return price


def _decode_yyiis(response_bytes: bytes, *, expected_count: int) -> list[InflationSwapResult]:
    try:
        response = PriceYearOnYearInflationSwapResponse.GetRootAs(response_bytes, 0)
        actual = response.SwapsLength()
    except Exception as exc:
        msg = f"decode_swap_inflation_response: malformed YYIIS engine response ({exc})."
        raise ValueError(msg) from exc

    if actual != expected_count:
        msg = (
            "decode_swap_inflation_response: engine returned "
            f"{actual} YYIIS result(s), expected {expected_count}."
        )
        raise ValueError(msg)

    results: list[InflationSwapResult] = []
    for i in range(actual):
        swap_response = response.Swaps(i)
        if swap_response is None:
            msg = f"decode_swap_inflation_response: null YYIIS swap result at index {i}."
            raise ValueError(msg)
        npv = float(swap_response.Npv())
        fair_rate = float(swap_response.FairRate())
        fair_spread = float(swap_response.FairSpread())
        fixed_leg_bps = float(swap_response.FixedLegBps())
        yoy_leg_bps = float(swap_response.YoyLegBps())
        fixed_leg_npv = float(swap_response.FixedLegNpv())
        yoy_leg_npv = float(swap_response.YoyLegNpv())
        results.append(
            InflationSwapResult(
                npv=npv,
                fair_rate=fair_rate,
                fair_spread=fair_spread,
                fixed_leg_bps=fixed_leg_bps,
                fixed_leg_npv=fixed_leg_npv,
                inflation_leg_npv=None,
                yoy_leg_bps=yoy_leg_bps,
                yoy_leg_npv=yoy_leg_npv,
                swap_kind=_SWAP_KIND_YEAR_ON_YEAR,
                extras={
                    "npv": npv,
                    "fair_rate": fair_rate,
                    "fair_spread": fair_spread,
                    "fixed_leg_bps": fixed_leg_bps,
                    "fixed_leg_npv": fixed_leg_npv,
                    "yoy_leg_bps": yoy_leg_bps,
                    "yoy_leg_npv": yoy_leg_npv,
                },
            )
        )
    return results


# ---------------------------------------------------------------------------
# Period / Schedule helpers
# ---------------------------------------------------------------------------


def _build_period(
    raw: Any,  # noqa: ANN401 -- runtime branch
    *,
    default_n: int,
    default_unit: int,
) -> PeriodT:
    period = PeriodT()
    if isinstance(raw, Mapping):
        n_raw = raw.get("n")
        period.n = int(n_raw) if isinstance(n_raw, (int, float)) else default_n
        period.unit = _enum_or_default(_TIME_UNIT_BY_NAME, raw.get("unit"), default_unit)
    else:
        period.n = default_n
        period.unit = default_unit
    return period


def _build_yyiis_schedule(
    raw: Any,  # noqa: ANN401 -- runtime branch
    *,
    is_fixed: bool,
) -> ScheduleT:
    schedule = ScheduleT()
    schedule.calendar = Calendar.TARGET
    schedule.frequency = Frequency.Annual
    schedule.convention = BusinessDayConvention.ModifiedFollowing
    schedule.terminationDateConvention = BusinessDayConvention.ModifiedFollowing
    schedule.dateGenerationRule = DateGenerationRule.Forward
    schedule.endOfMonth = False

    if isinstance(raw, Mapping):
        schedule.calendar = _enum_or_default(
            _CALENDAR_BY_NAME, raw.get("calendar"), schedule.calendar
        )
        eff = raw.get("effective_date")
        if isinstance(eff, str) and eff:
            schedule.effectiveDate = eff
        term = raw.get("termination_date")
        if isinstance(term, str) and term:
            schedule.terminationDate = term
        schedule.frequency = _enum_or_default(
            _FREQUENCY_BY_NAME, raw.get("frequency"), schedule.frequency
        )
        schedule.convention = _enum_or_default(
            _BDC_BY_NAME, raw.get("convention"), schedule.convention
        )
        schedule.terminationDateConvention = _enum_or_default(
            _BDC_BY_NAME,
            raw.get("termination_date_convention"),
            schedule.terminationDateConvention,
        )
        schedule.dateGenerationRule = _enum_or_default(
            _DATE_GEN_BY_NAME,
            raw.get("date_generation_rule"),
            schedule.dateGenerationRule,
        )
        eom = raw.get("end_of_month")
        if isinstance(eom, bool):
            schedule.endOfMonth = eom

    # YYIIS fixed and yoy legs share the same shape; the
    # ``is_fixed`` arg is reserved for future role-specific
    # defaults (e.g. yearly vs semiannual frequencies).
    _ = is_fixed
    return schedule


def _enum_or_default(
    by_name: Mapping[str, int],
    raw: Any,  # noqa: ANN401 -- runtime branch
    default: int,
) -> int:
    if isinstance(raw, str) and raw in by_name:
        return by_name[raw]
    return default


__all__ = [
    "SWAP_INFLATION_YEAR_ON_YEAR_RPC",
    "SWAP_INFLATION_ZERO_COUPON_RPC",
    "build_swap_inflation_request",
    "decode_swap_inflation_response",
    "price_swap_inflation_batch",
]
