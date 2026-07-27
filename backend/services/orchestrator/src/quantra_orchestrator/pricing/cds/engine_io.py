"""Engine wire-layer for the CDS pricing path.

Three callables, structurally identical to the swap_ir / swaption /
bonds ``engine_io`` modules (the per-product split):

* :func:`build_cds_request` — turn one :class:`EngineBatch[CdsTrade]`
  into the FlatBuffers payload the engine expects on the
  :attr:`EngineRpc.PRICE_CDS` RPC.
* :func:`decode_cds_response` — turn the engine's
  ``PriceCDSResponse`` flatbuffer into a typed
  :class:`CdsResult` sequence (one per trade in the batch, in
  input order).
* :func:`price_cds_batch` — the per-product translator the
  concurrency seam's :func:`execute` runner injects via its
  ``price_batch=`` parameter.

The orchestrator's API surface (``CdsTrade.cds`` body — loose
JSONB) stays intentionally schema-light; this module
fills in the per-trade CDS schedule + side / coupon / convention
detail. The market-data context (discount curve + credit curve +
CDS model) is built **faithfully** from the route-captured
:class:`ResolvedMarketData`: the discount curve
+ quotes are translated by
:func:`quantra_orchestrator.pricing._translator.build_pricing_from_resolved`
and the credit curve by
:func:`quantra_orchestrator.pricing._translator.translate_credit_curve`
(flat-hazard or inline par-spread bootstrap — never the canonical
2 % / 40 % fixture). The per-trade discount / credit-curve refs honour
the resolved entity ids; the cds model ref points at the
default MidPoint model id (pure config).

Trade-body keys honored (top-level on ``trade.cds`` *or* nested
under ``trade.cds["cds"]``; the latter matches the engine's own
``cds_request.json`` example shape):

* ``side`` (``"Buyer"`` / ``"Seller"``, default ``"Buyer"``).
* ``notional`` (float, default 10_000_000.0).
* ``running_coupon`` (float, default 0.01 = 100bps).
* ``upfront`` (float, default 0.0).
* ``day_counter`` (string enum name, default ``"Actual360"``).
* ``business_day_convention`` (string enum name, default
  ``"Following"``).
* ``protection_start`` / ``upfront_date`` / ``trade_date``
  (YYYY-MM-DD; default to the ``as_of`` carried in
  ``shared_inputs`` so the schedule lands on the same date).
* ``cash_settlement_days`` (int, default 3).
* ``settles_accrual`` / ``pays_at_default_time`` /
  ``rebates_accrual`` (bool, defaults match the engine's CDS
  defaults).
* ``last_period_day_counter`` (string enum, default
  ``"Actual360"``).

Schedule keys honored under ``trade.cds["schedule"]`` (matches the
engine's ``cds_request.json``):

* ``effective_date`` (YYYY-MM-DD, default 2025-01-15).
* ``termination_date`` (YYYY-MM-DD, default 2030-01-15).

The RPC name is :attr:`EngineRpc.PRICE_CDS` — the canonical entry
in the engine's ``rpc_service QuantraServer`` block. Already
present in :mod:`quantra_common.engine_client.rpcs`;
no D# mismatch needs to be proposed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

import flatbuffers

from quantra_common.engine_client import EngineClient, EngineRpc
from quantra_common.engine_client._generated.quantra.CDS import CDST
from quantra_common.engine_client._generated.quantra.enums.BusinessDayConvention import (
    BusinessDayConvention,
)
from quantra_common.engine_client._generated.quantra.enums.Calendar import Calendar
from quantra_common.engine_client._generated.quantra.enums.DateGenerationRule import (
    DateGenerationRule,
)
from quantra_common.engine_client._generated.quantra.enums.DayCounter import DayCounter
from quantra_common.engine_client._generated.quantra.enums.Frequency import Frequency
from quantra_common.engine_client._generated.quantra.enums.ProtectionSide import (
    ProtectionSide,
)
from quantra_common.engine_client._generated.quantra.PriceCDS import PriceCDST
from quantra_common.engine_client._generated.quantra.PriceCDSRequest import (
    PriceCDSRequestT,
)
from quantra_common.engine_client._generated.quantra.PriceCDSResponse import (
    PriceCDSResponse,
)
from quantra_common.engine_client._generated.quantra.Pricing import PricingT
from quantra_common.engine_client._generated.quantra.Schedule import ScheduleT
from quantra_orchestrator.pricing._translator import (
    DEFAULT_CDS_MODEL_ID,
    CreditCurveTranslationError,
    CurveRole,
    CurveTranslationError,
    QuoteResolutionError,
    ResolvedMarketData,
    build_pricing_from_resolved,
    resolved_credit_curve_id,
    resolved_curve_id,
)
from quantra_orchestrator.pricing.cds.errors import (
    CdsCreditCurveResolutionFailedError,
    CdsCurveResolutionFailedError,
    CdsMissingTradeFieldsError,
    CdsQuoteResolutionFailedError,
)
from quantra_orchestrator.pricing.cds.models import CdsResult, CdsTrade
from quantra_orchestrator.pricing.concurrency import EngineBatch
from quantra_orchestrator.tracing.stages import serialize_fb_object

CDS_ENGINE_RPC: Final[EngineRpc] = EngineRpc.PRICE_CDS

_DEFAULT_SIDE: Final[str] = "Buyer"
# Economic fields with NO sensible market-convention default. Historically a
# missing key here silently priced a fixture CDS (10M notional, 1 %
# running coupon, a 2025→2030 schedule). Now a hard 422
# ``cds_missing_trade_fields`` per field; the schedule dates are reported
# with a ``schedule.`` prefix.
_REQUIRED_CDS_FIELDS: Final[tuple[str, ...]] = (
    "notional",
    "running_coupon",
)
_REQUIRED_CDS_SCHEDULE_FIELDS: Final[tuple[str, ...]] = (
    "effective_date",
    "termination_date",
)
_DEFAULT_UPFRONT: Final[float] = 0.0
_DEFAULT_CASH_SETTLEMENT_DAYS: Final[int] = 3

# Enum lookup tables. We accept the engine's string enum names on the
# wire (matches the JSON shape ``cds_request.json`` documents) and
# fall back to a safe default if the body carries an unrecognised
# value. The strict-validation pass happens server-side; the
# orchestrator's job here is the loose-to-typed coercion.
_SIDE_BY_NAME: Final[dict[str, int]] = {
    "Buyer": ProtectionSide.Buyer,
    "Seller": ProtectionSide.Seller,
}

_DAY_COUNTER_BY_NAME: Final[dict[str, int]] = {
    "Actual360": DayCounter.Actual360,
    "Actual365Fixed": DayCounter.Actual365Fixed,
    "Actual365NoLeap": DayCounter.Actual365NoLeap,
    "ActualActual": DayCounter.ActualActual,
    "ActualActualISMA": DayCounter.ActualActualISMA,
    "ActualActualBond": DayCounter.ActualActualBond,
    "ActualActualISDA": DayCounter.ActualActualISDA,
    "ActualActualHistorical": DayCounter.ActualActualHistorical,
    "Thirty360": DayCounter.Thirty360,
}

_BDC_BY_NAME: Final[dict[str, int]] = {
    "Following": BusinessDayConvention.Following,
    "HalfMonthModifiedFollowing": BusinessDayConvention.HalfMonthModifiedFollowing,
    "ModifiedFollowing": BusinessDayConvention.ModifiedFollowing,
    "ModifiedPreceding": BusinessDayConvention.ModifiedPreceding,
    "Nearest": BusinessDayConvention.Nearest,
    "Preceding": BusinessDayConvention.Preceding,
    "Unadjusted": BusinessDayConvention.Unadjusted,
}


def _pricing_from_resolved(resolved: ResolvedMarketData) -> PricingT:
    """Build the ``Pricing`` graph faithfully, mapping translator errors to cds 422s.

    The neutral translator failures are mapped onto the cds
    error codes so the catalog is unchanged: a quote miss →
    ``cds_quote_resolution_failed``; a credit-curve translation failure →
    ``cds_credit_curve_resolution_failed`` (the distinct credit bundle
    stage); a rates-curve translation failure →
    ``cds_curve_resolution_failed``.
    """

    try:
        return build_pricing_from_resolved(resolved)
    except QuoteResolutionError as exc:
        raise CdsQuoteResolutionFailedError(
            missing_canonical_ids=exc.missing_canonical_ids,
            as_of=resolved.as_of,
        ) from exc
    except CreditCurveTranslationError as exc:
        raise CdsCreditCurveResolutionFailedError(reason=exc.reason, details=exc.details) from exc
    except CurveTranslationError as exc:
        raise CdsCurveResolutionFailedError(reason=exc.reason, details=exc.details) from exc


def build_cds_request(
    trades: Sequence[CdsTrade],
    shared_inputs: Mapping[str, Any],
    *,
    resolved: ResolvedMarketData,
) -> bytes:
    """Encode one batch of CDS trades as ``PriceCDSRequest``.

    The ``Pricing`` context is built faithfully from the caller's resolved
    discount curve + quotes + credit curve, not a
    canonical fixture; the resolved graph reaches here by route-closure capture
    , not the ``shared_inputs`` channel. Every trade in the batch
    references the resolved discount-curve id (by role), the resolved
    credit-curve id, and the default MidPoint cds model id; the engine looks
    them up in :class:`Pricing.rates.curves`,
    :class:`Pricing.credit.credit_curves` and
    :class:`Pricing.volatility.models` respectively.
    """

    if resolved.credit_curve is None:
        # The cds route always resolves a credit curve; guard so a programming
        # error surfaces as the typed 422 rather than an engine-side all-zero
        # price (root cause).
        raise CdsCreditCurveResolutionFailedError(
            reason="No resolved credit curve to translate into the cds request.",
            details=[{"role": "credit"}],
        )

    discount_curve_id = resolved.curve_id_for_role(CurveRole.DISCOUNT) or (
        resolved_curve_id(resolved.curves[0]) if resolved.curves else ""
    )
    credit_curve_id = resolved_credit_curve_id(resolved.credit_curve)

    builder = flatbuffers.Builder(2048)
    # ForceDefaults: the engine requires every set field explicit on the wire;
    # zero-default enums/scalars (e.g. Frequency.Annual == 0) must not be omitted.
    builder.ForceDefaults(True)
    request = PriceCDSRequestT()
    request.pricing = _pricing_from_resolved(resolved)
    request.cdsList = [
        _build_one_cds(
            trade,
            shared_inputs,
            discount_curve_id=discount_curve_id,
            credit_curve_id=credit_curve_id,
        )
        for trade in trades
    ]
    builder.Finish(request.Pack(builder))
    return bytes(builder.Output())


def decode_cds_response(
    response_bytes: bytes,
    *,
    expected_count: int,
) -> list[CdsResult]:
    """Decode ``PriceCDSResponse`` bytes into typed :class:`CdsResult` instances.

    Each :class:`CDSValues` flatbuffer carries the per-trade
    diagnostics ``cds_response.fbs`` documents: ``npv``,
    ``fair_spread`` (par spread in decimal), ``fair_upfront``,
    ``default_leg_npv`` (also surfaced as ``protection_leg_npv`` in
    the orchestrator's typed result), and ``premium_leg_npv``. The
    engine populates every field on a non-error response; we
    propagate them verbatim plus a thin ``extras`` carry for any
    future enrichment.
    """

    if not response_bytes:
        msg = "decode_cds_response: empty engine response."
        raise ValueError(msg)
    try:
        response = PriceCDSResponse.GetRootAs(response_bytes, 0)
        actual = response.CdsListLength()
    except Exception as exc:
        msg = f"decode_cds_response: malformed engine response ({exc})."
        raise ValueError(msg) from exc

    if actual != expected_count:
        msg = f"decode_cds_response: engine returned {actual} result(s), expected {expected_count}."
        raise ValueError(msg)

    results: list[CdsResult] = []
    for i in range(actual):
        cds_response = response.CdsList(i)
        if cds_response is None:
            msg = f"decode_cds_response: null CDS result at index {i}."
            raise ValueError(msg)
        # The engine reports a per-trade pricing failure on the ``CDSValues``
        # ``error`` field while leaving every numeric field at its 0.0
        # default. Surfacing that as a raised error
        # (→ the route's engine-error mapping → 502 ``engine_upstream_error``
        # with the assembled-request detail) is the correct behavior:
        # the prior decoder ignored the field and returned the all-zero result
        # verbatim as a *success*, which is exactly how an earlier defect
        # masked an engine error as an all-zero price. Never silently propagate an engine error
        # as a zero-valued result.
        engine_error = cds_response.Error()
        if engine_error is not None:
            raw_msg = engine_error.ErrorMessage()
            error_text = (
                raw_msg.decode() if isinstance(raw_msg, (bytes, bytearray)) else str(raw_msg)
            )
            msg = (
                "decode_cds_response: engine returned a per-trade pricing "
                f"error at index {i}: {error_text}"
            )
            raise ValueError(msg)
        default_leg_npv = float(cds_response.DefaultLegNpv())
        # ``CDSValues.fair_spread`` is presence-optional (absent when QuantLib
        # cannot express a fair spread, e.g. a zero-running-coupon CDS) — the
        # accessor returns ``None`` for an absent field; keep it None-safe.
        fair_spread_raw = cds_response.FairSpread()
        results.append(
            CdsResult(
                npv=float(cds_response.Npv()),
                fair_spread=float(fair_spread_raw) if fair_spread_raw is not None else None,
                fair_upfront=float(cds_response.FairUpfront()),
                protection_leg_npv=default_leg_npv,
                premium_leg_npv=float(cds_response.PremiumLegNpv()),
                extras={
                    # Pinned alongside the typed alias so callers that
                    # prefer the engine's own naming (``default_leg_npv``)
                    # don't need an extra rename layer.
                    "default_leg_npv": default_leg_npv,
                },
            )
        )
    return results


def decode_cds_request_wire(request_bytes: bytes) -> dict[str, Any]:
    """Decode the EXACT transmitted CDS request bytes into readable JSON.

    Reads the buffer with the generated FlatBuffers reader
    (``PriceCDSRequestT.InitFromPackedBuf``) so the result is provably derived
    from the bytes that went on the wire, then renders it JSON-safe via the
    shared :func:`serialize_fb_object`. Best-effort: a malformed buffer degrades
    to a marker dict, never raises. Mirrors ``decode_swap_ir_request_wire``.
    """

    try:
        request = PriceCDSRequestT.InitFromPackedBuf(request_bytes, 0)
    except Exception as exc:  # tracing is best-effort — never raise
        return {"__undecodable__": True, "exc_type": type(exc).__name__}
    return serialize_fb_object(request)


async def price_cds_batch(
    engine: EngineClient,
    batch: EngineBatch[CdsTrade],
    *,
    resolved: ResolvedMarketData,
) -> Sequence[CdsResult]:
    """The per-product translator injected into the ``execute`` runner.

    ``resolved`` is the route-captured :class:`ResolvedMarketData` the
    route's ``price_batch`` lambda binds; it carries the faithful discount curve
    + quotes + credit curve the request is assembled from.
    """

    request_bytes = build_cds_request(batch.trades, batch.shared_inputs, resolved=resolved)
    response_bytes = await engine.call(CDS_ENGINE_RPC, request_bytes)
    return decode_cds_response(response_bytes, expected_count=len(batch.trades))


def _require_cds_trade_fields(body: Mapping[str, Any]) -> None:
    """Raise the typed 422 when a required economic field is absent/empty.

    The CDS schedule dates live in a nested ``schedule`` block; they are
    reported with a ``schedule.`` prefix so the client knows where the
    fix belongs.
    """

    missing = [field for field in _REQUIRED_CDS_FIELDS if body.get(field) in (None, "")]
    schedule_raw = body.get("schedule")
    schedule: Mapping[str, Any] = schedule_raw if isinstance(schedule_raw, Mapping) else {}
    missing.extend(
        f"schedule.{field}"
        for field in _REQUIRED_CDS_SCHEDULE_FIELDS
        if schedule.get(field) in (None, "")
    )
    if missing:
        raise CdsMissingTradeFieldsError(product="cds", missing_fields=missing)


def _build_cds_schedule(schedule_body_raw: Any) -> ScheduleT:  # noqa: ANN401 -- dynamic
    """Build a canonical CDS :class:`Schedule` from an optional body block.

    The orchestrator picks ISDA-standard CDS conventions (TARGET,
    Quarterly, TwentiethIMM, Following / Unadjusted) by default;
    only the per-trade dates flow through from the body. the portal cutover
    may widen the surface but the engine's parity fixture lives at
    these conventions today.
    """

    schedule_body: Mapping[str, Any] = (
        schedule_body_raw if isinstance(schedule_body_raw, Mapping) else {}
    )
    effective_date = str(schedule_body["effective_date"])
    termination_date = str(schedule_body["termination_date"])

    schedule = ScheduleT()
    schedule.effectiveDate = effective_date
    schedule.terminationDate = termination_date
    schedule.calendar = Calendar.TARGET
    schedule.frequency = Frequency.Quarterly
    schedule.convention = BusinessDayConvention.Following
    schedule.terminationDateConvention = BusinessDayConvention.Unadjusted
    schedule.dateGenerationRule = DateGenerationRule.TwentiethIMM
    schedule.endOfMonth = False
    return schedule


def _build_one_cds(
    trade: CdsTrade,
    shared_inputs: Mapping[str, Any],
    *,
    discount_curve_id: str,
    credit_curve_id: str,
) -> PriceCDST:
    """Map one :class:`CdsTrade` to a :class:`PriceCDS` FB graph.

    The trade body is loose ``dict[str, Any]``; we look up
    each engine-side key with a sensible default. Both the top-level
    shape (``trade.cds["side"]``) and the engine-canonical nested
    shape (``trade.cds["cds"]["side"]``) are accepted — the saved
    CDS body that the portal eventually persists may settle on
    either after the portal cutover.

    The ``discountingCurve`` / ``creditCurveId`` refs honour the resolved
    discount / credit-curve ids the translator placed in
    ``Pricing.rates.curves`` / ``Pricing.credit.credit_curves``,
    never ``CANONICAL_*``. ``model`` points at the default MidPoint cds model id
    (pure engine config).
    """

    body_root: Mapping[str, Any] = trade.cds or {}
    nested_cds = body_root.get("cds")
    body: Mapping[str, Any] = nested_cds if isinstance(nested_cds, Mapping) else body_root
    _require_cds_trade_fields(body)

    as_of = str(shared_inputs["as_of"])

    side = _SIDE_BY_NAME.get(str(body.get("side", _DEFAULT_SIDE)), ProtectionSide.Buyer)
    notional = float(body["notional"])
    running_coupon = float(body["running_coupon"])
    upfront = float(body.get("upfront", _DEFAULT_UPFRONT))
    day_counter = _DAY_COUNTER_BY_NAME.get(
        str(body.get("day_counter", "Actual360")), DayCounter.Actual360
    )
    bdc = _BDC_BY_NAME.get(
        str(body.get("business_day_convention", "Following")),
        BusinessDayConvention.Following,
    )
    last_period_dc = _DAY_COUNTER_BY_NAME.get(
        str(body.get("last_period_day_counter", "Actual360")),
        DayCounter.Actual360,
    )
    settles_accrual = bool(body.get("settles_accrual", True))
    pays_at_default_time = bool(body.get("pays_at_default_time", True))
    rebates_accrual = bool(body.get("rebates_accrual", True))
    cash_settlement_days = int(body.get("cash_settlement_days", _DEFAULT_CASH_SETTLEMENT_DAYS))
    protection_start = str(body.get("protection_start", as_of))
    trade_date = str(body.get("trade_date", as_of))
    # Upfront settlement date — only emit one when the trade actually carries an
    # upfront leg. The engine picks its CDS constructor on
    # ``upfront != 0 || upfront_date present``; the
    # upfront-variant ``CreditDefaultSwap`` then trips
    # ``CreditDefaultSwap::fairUpfront()`` in the live engine (which the engine
    # calls unconditionally) → "fair upfront not
    # available" → the result is caught and zeroed (root cause). A plain
    # running-spread CDS (no upfront, no explicit date) must leave the date
    # absent so the engine builds the running-spread constructor the live engine
    # prices correctly. Defaulting it to ``as_of`` (the prior behavior) forced
    # every CDS down the broken upfront path. (The engine's unconditional
    # fairUpfront call on the upfront path is escalated engine-side.)
    upfront_date_raw = body.get("upfront_date")
    if upfront_date_raw is not None:
        upfront_date: str | None = str(upfront_date_raw)
    elif upfront != 0.0:
        upfront_date = as_of
    else:
        upfront_date = None

    schedule = _build_cds_schedule(body.get("schedule"))

    # The engine selects the upfront-bearing CreditDefaultSwap
    # constructor on the *presence* of ``upfront``
    # (``trade.upfront.has_value()``), not on ``upfront != 0``. Because the
    # request builder now forces defaults onto the wire, an unconditional
    # ``cds.upfront = 0.0`` would land as a present 0 and silently flip a plain
    # running-spread CDS onto the upfront path (mis-pricing fairUpfront
    # abort). So emit ``upfront`` ONLY for a genuine upfront leg — the SAME gate
    # the upfront_date logic above uses (``upfront != 0`` OR an explicit
    # upfront_date). Absent ⇒ the plain constructor, byte-for-byte the shape
    # 0.1.1 effectively priced (verified: identical NPV on both engines).
    has_upfront_leg = upfront != 0.0 or upfront_date_raw is not None

    cds = CDST()
    cds.side = side
    cds.notional = notional
    cds.runningCoupon = running_coupon
    cds.schedule = schedule
    cds.upfront = upfront if has_upfront_leg else None
    cds.dayCounter = day_counter
    cds.businessDayConvention = bdc
    cds.settlesAccrual = settles_accrual
    cds.paysAtDefaultTime = pays_at_default_time
    cds.rebatesAccrual = rebates_accrual
    cds.protectionStart = protection_start
    cds.upfrontDate = upfront_date
    cds.lastPeriodDayCounter = last_period_dc
    cds.tradeDate = trade_date
    cds.cashSettlementDays = cash_settlement_days

    price_cds = PriceCDST()
    price_cds.cds = cds
    price_cds.discountingCurve = discount_curve_id
    price_cds.creditCurveId = credit_curve_id
    price_cds.model = DEFAULT_CDS_MODEL_ID
    return price_cds


__all__ = [
    "CDS_ENGINE_RPC",
    "build_cds_request",
    "decode_cds_response",
    "price_cds_batch",
]
