"""Engine wire-layer for the FX-forward pricing path (engine >= 0.9.0).

* :func:`build_fx_forward_request` — one :class:`EngineBatch[FxForwardTrade]`
  → ``PriceFxForwardRequest`` bytes. The ``Pricing`` context comes from the
  shared :func:`build_pricing_from_resolved` (curves with quote substitution,
  explicit indices, ``Pricing.fx.spots[]`` from the resolved FX spots).
* :func:`decode_fx_forward_response` — ``PriceFxForwardResponse`` bytes →
  one :class:`FxForwardResult` per trade (``docs/http-api.md`` § "FX forward"
  / Response: no field is optional, a ``0.0`` is a computed zero; the two
  dates are read presence-aware).
* :func:`price_fx_forward_batch` — the ``price_batch=`` translator.

Trade-body mapping (``/v1`` → engine ``PriceFxForward``):

===========================  ==========================================
``/v1`` trade key            engine field
===========================  ==========================================
``base_currency``            ``fx_forward.base_currency``
``quote_currency``           ``fx_forward.quote_currency``
``base_notional``|``notional`` ``fx_forward.base_notional``
``forward_rate``             ``fx_forward.forward_rate``
``maturity_date``            ``fx_forward.maturity_date``
``direction``                ``fx_forward.direction`` (``BuyBase``/``SellBase``)
``fx_spot``                  ``fx_spot`` (resolved spot id; the only spot when omitted)
``base_discounting_curve``   ``base_discounting_curve`` (resolved curve id)
``quote_discounting_curve``  ``quote_discounting_curve`` (resolved curve id)
``reporting_currency``       ``reporting_currency`` (default: the quote ccy)
===========================  ==========================================
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from math import isfinite
from typing import Any, Final

import flatbuffers

from quantra_common.engine_client import EngineClient, EngineRpc
from quantra_common.engine_client._generated.quantra.enums.FxDirection import FxDirection
from quantra_common.engine_client._generated.quantra.FxForward import FxForwardT
from quantra_common.engine_client._generated.quantra.PriceFxForward import PriceFxForwardT
from quantra_common.engine_client._generated.quantra.PriceFxForwardRequest import (
    PriceFxForwardRequestT,
)
from quantra_common.engine_client._generated.quantra.PriceFxForwardResponse import (
    PriceFxForwardResponse,
)
from quantra_orchestrator.pricing._translator import (
    CurveTranslationError,
    FxSpotTranslationError,
    QuoteResolutionError,
    ResolvedMarketData,
    build_pricing_from_resolved,
    resolved_curve_id,
    resolved_fx_spot_id,
)
from quantra_orchestrator.pricing.concurrency import EngineBatch
from quantra_orchestrator.pricing.fx_forward.errors import (
    FxForwardCurveResolutionFailedError,
    FxForwardFxSpotResolutionFailedError,
    FxForwardMissingTradeFieldsError,
    FxForwardQuoteResolutionFailedError,
)
from quantra_orchestrator.pricing.fx_forward.models import (
    FxForwardResult,
    FxForwardTrade,
)
from quantra_orchestrator.tracing.stages import serialize_fb_object

FX_FORWARD_ENGINE_RPC: Final[EngineRpc] = EngineRpc.PRICE_FX_FORWARD

_DIRECTIONS: Final[dict[str, int]] = {
    "buybase": FxDirection.BuyBase,
    "sellbase": FxDirection.SellBase,
}
_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "base_currency",
    "quote_currency",
    "direction",
    "forward_rate",
    "maturity_date",
    "base_discounting_curve",
    "quote_discounting_curve",
)


def _positive_float(body: Mapping[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = body.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            f = float(value)
            return f if isfinite(f) and f > 0 else None
    return None


def _ref_id(raw: object) -> str | None:
    candidate = raw.get("id") if isinstance(raw, Mapping) else raw
    return candidate if isinstance(candidate, str) and candidate else None


def _match_curve(resolved: ResolvedMarketData, raw: object, field: str) -> str:
    """Map the trade's curve ref (id / name) onto a resolved curve's engine id."""

    wanted = _ref_id(raw)
    if wanted is None:
        raise FxForwardMissingTradeFieldsError(missing_fields=[field])
    for curve in resolved.curves:
        if resolved_curve_id(curve) == wanted or curve.name == wanted:
            return resolved_curve_id(curve)
    raise FxForwardCurveResolutionFailedError(
        reason=(
            f"FX forward ``{field}`` {wanted!r} names no resolved curve "
            f"(resolved: {', '.join(resolved_curve_id(c) for c in resolved.curves)})."
        ),
        details=[{"field": field, "value": wanted}],
    )


def _match_spot(resolved: ResolvedMarketData, raw: object) -> str:
    wanted = _ref_id(raw)
    if wanted is None:
        if len(resolved.fx_spots) == 1:
            return resolved_fx_spot_id(resolved.fx_spots[0])
        raise FxForwardMissingTradeFieldsError(
            missing_fields=["fx_spot"],
            reason=(
                "FX forward ``fx_spot`` is required when the request resolves "
                f"{len(resolved.fx_spots)} FX spots."
            ),
        )
    for spot in resolved.fx_spots:
        if resolved_fx_spot_id(spot) == wanted or spot.name == wanted:
            return resolved_fx_spot_id(spot)
    raise FxForwardFxSpotResolutionFailedError(
        reason=(
            f"FX forward ``fx_spot`` {wanted!r} names no resolved FX spot "
            f"(resolved: {', '.join(resolved_fx_spot_id(s) for s in resolved.fx_spots)})."
        ),
        details=[{"field": "fx_spot", "value": wanted}],
    )


def _build_one(trade: FxForwardTrade, resolved: ResolvedMarketData) -> PriceFxForwardT:
    body: Mapping[str, Any] = trade.fx_forward or {}
    missing = [f for f in _REQUIRED_FIELDS if body.get(f) in (None, "")]
    if _positive_float(body, ("base_notional", "notional")) is None:
        missing.append("base_notional")
    if missing:
        raise FxForwardMissingTradeFieldsError(missing_fields=missing)

    direction = _DIRECTIONS.get(str(body["direction"]).lower())
    if direction is None:
        raise FxForwardMissingTradeFieldsError(
            missing_fields=["direction"],
            reason=(
                f"FX forward ``direction`` must be BuyBase or SellBase (got {body['direction']!r})."
            ),
        )
    forward_rate = _positive_float(body, ("forward_rate",))
    if forward_rate is None:
        raise FxForwardMissingTradeFieldsError(
            missing_fields=["forward_rate"],
            reason="FX forward ``forward_rate`` must be a positive finite number.",
        )
    maturity = body["maturity_date"]
    maturity_str = maturity.isoformat() if isinstance(maturity, date) else str(maturity)
    try:
        date.fromisoformat(maturity_str[:10])
    except ValueError:
        raise FxForwardMissingTradeFieldsError(
            missing_fields=["maturity_date"],
            reason=f"FX forward ``maturity_date`` must be YYYY-MM-DD (got {maturity!r}).",
        ) from None

    base_ccy = str(body["base_currency"])
    quote_ccy = str(body["quote_currency"])
    reporting = str(body.get("reporting_currency") or quote_ccy)
    if reporting not in (base_ccy, quote_ccy):
        raise FxForwardMissingTradeFieldsError(
            missing_fields=["reporting_currency"],
            reason=(
                f"FX forward ``reporting_currency`` must be {base_ccy} or {quote_ccy} "
                f"(got {reporting!r})."
            ),
        )

    fwd = FxForwardT()
    fwd.baseCurrency = base_ccy
    fwd.quoteCurrency = quote_ccy
    fwd.baseNotional = _positive_float(body, ("base_notional", "notional"))
    fwd.forwardRate = forward_rate
    fwd.maturityDate = maturity_str[:10]
    fwd.direction = direction

    price = PriceFxForwardT()
    price.fxForward = fwd
    price.fxSpot = _match_spot(resolved, body.get("fx_spot"))
    price.baseDiscountingCurve = _match_curve(
        resolved, body.get("base_discounting_curve"), "base_discounting_curve"
    )
    price.quoteDiscountingCurve = _match_curve(
        resolved, body.get("quote_discounting_curve"), "quote_discounting_curve"
    )
    price.reportingCurrency = reporting
    return price


def build_fx_forward_request(
    trades: Sequence[FxForwardTrade], *, resolved: ResolvedMarketData
) -> bytes:
    """Encode one batch of FX forwards as ``PriceFxForwardRequest`` bytes."""

    builder = flatbuffers.Builder(2048)
    # ForceDefaults: the engine requires every set field explicit on the wire
    # (``FxDirection.BuyBase == 0``, ``spot_days == 0``, ``end_of_month == false``
    # must all land).
    builder.ForceDefaults(True)
    request = PriceFxForwardRequestT()
    try:
        request.pricing = build_pricing_from_resolved(resolved)
    except QuoteResolutionError as exc:
        raise FxForwardQuoteResolutionFailedError(
            missing_canonical_ids=exc.missing_canonical_ids, as_of=resolved.as_of
        ) from exc
    except FxSpotTranslationError as exc:
        raise FxForwardFxSpotResolutionFailedError(reason=exc.reason, details=exc.details) from exc
    except CurveTranslationError as exc:
        raise FxForwardCurveResolutionFailedError(reason=exc.reason, details=exc.details) from exc
    request.fxForwards = [_build_one(trade, resolved) for trade in trades]
    builder.Finish(request.Pack(builder))
    return bytes(builder.Output())


def _decode_str(raw: bytes | None) -> str | None:
    return raw.decode("utf-8") if raw is not None else None


def decode_fx_forward_response(
    response_bytes: bytes, *, expected_count: int, reporting_currencies: Sequence[str]
) -> list[FxForwardResult]:
    """Decode ``PriceFxForwardResponse`` bytes into typed results (request order)."""

    if not response_bytes:
        msg = "decode_fx_forward_response: empty engine response."
        raise ValueError(msg)
    try:
        response = PriceFxForwardResponse.GetRootAs(response_bytes, 0)
        actual = response.FxForwardsLength()
    except Exception as exc:
        msg = f"decode_fx_forward_response: malformed engine response ({exc})."
        raise ValueError(msg) from exc
    if actual != expected_count:
        msg = (
            "decode_fx_forward_response: engine returned "
            f"{actual} result(s), expected {expected_count}."
        )
        raise ValueError(msg)
    results: list[FxForwardResult] = []
    for i in range(actual):
        row = response.FxForwards(i)
        if row is None:
            msg = f"decode_fx_forward_response: null result at index {i}."
            raise ValueError(msg)
        results.append(
            FxForwardResult(
                npv=float(row.Npv()),
                reporting_currency=reporting_currencies[i] if i < len(reporting_currencies) else "",
                fair_forward_rate=float(row.FairForwardRate()),
                forward_points=float(row.ForwardPoints()),
                base_leg_pv=float(row.BaseLegPv()),
                quote_leg_pv=float(row.QuoteLegPv()),
                fx_spot_used=float(row.FxSpotUsed()),
                spot_date=_decode_str(row.SpotDate()),
                maturity_date=_decode_str(row.MaturityDate()),
            )
        )
    return results


def decode_fx_forward_request_wire(request_bytes: bytes) -> dict[str, Any]:
    """Decode the EXACT transmitted request bytes into a readable JSON view (trace)."""

    try:
        request = PriceFxForwardRequestT.InitFromPackedBuf(request_bytes, 0)
    except Exception as exc:  # tracing is best-effort — never raise
        return {"__undecodable__": True, "exc_type": type(exc).__name__}
    return serialize_fb_object(request)


def reporting_currency_of(trade: FxForwardTrade) -> str:
    body = trade.fx_forward or {}
    return str(body.get("reporting_currency") or body.get("quote_currency") or "")


async def price_fx_forward_batch(
    engine: EngineClient, batch: EngineBatch[FxForwardTrade], *, resolved: ResolvedMarketData
) -> Sequence[FxForwardResult]:
    request_bytes = build_fx_forward_request(batch.trades, resolved=resolved)
    response_bytes = await engine.call(FX_FORWARD_ENGINE_RPC, request_bytes)
    return decode_fx_forward_response(
        response_bytes,
        expected_count=len(batch.trades),
        reporting_currencies=[reporting_currency_of(t) for t in batch.trades],
    )


__all__ = [
    "FX_FORWARD_ENGINE_RPC",
    "build_fx_forward_request",
    "decode_fx_forward_request_wire",
    "decode_fx_forward_response",
    "price_fx_forward_batch",
]
