"""Engine wire-layer for the equity-options pricing path.

Three callables, structurally identical to the swap_ir / swaption /
bonds / cds ``engine_io`` modules (the per-product split):

* :func:`build_equity_option_request` — turn one
  :class:`EngineBatch[EquityOptionTrade]` into the FlatBuffers
  payload the engine expects on the
  :attr:`EngineRpc.PRICE_EQUITY_OPTION` RPC.
* :func:`decode_equity_option_response` — turn the engine's
  ``PriceEquityOptionResponse`` flatbuffer into a typed
  :class:`EquityOptionResult` sequence (one per trade in the
  batch, in input order).
* :func:`price_equity_option_batch` — the per-product translator
  the concurrency seam's :func:`execute` runner injects via
  its ``price_batch=`` parameter.

The ``Pricing`` context was flipped from a canonical
fixture to a faithful encoding of the caller's resolved discount + dividend
curves, underlier spot and BlackVol surface built by
:mod:`quantra_orchestrator.pricing._translator` — faithful curves with the
resolved quote values substituted in, the supplied spot, and the
BlackVol surface carrying the resolved vol level, not the canonical flat
fixture. The per-trade discount curve, ``volatility``, ``model`` and
``underlyingId`` references honour the resolved entity ids; the
analytic Black-Scholes equity model survives as a pure-config default.
The orchestrator's API surface (``EquityOptionTrade.equity_option`` body —
loose JSONB) stays intentionally schema-light; this module fills
in the per-option payoff / exercise detail using sensible defaults.

Trade-body keys honored — accepted at the top level of
``trade.equity_option`` and (matching the engine's own
``equity_option_request.json`` shape) under nested
``trade.equity_option["option"]``:

* ``trade_id`` (string, default derived from ``EquityOptionTrade.name``).
* ``option_type`` (``"Call"`` / ``"Put"``, default ``"Call"``).
* ``strike`` (float, default :data:`_DEFAULT_STRIKE`).
* ``quantity`` (float, default 1.0).
* ``expiry_date`` (YYYY-MM-DD, default
  :data:`_DEFAULT_EXPIRY_DATE`) — used for the European exercise.
* ``settlement`` (``"Physical"`` / ``"Cash"``, default
  ``"Physical"``).

The RPC name is :attr:`EngineRpc.PRICE_EQUITY_OPTION` — already
present in :mod:`quantra_common.engine_client.rpcs`;
no D# mismatch needs to be proposed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

import flatbuffers

from quantra_common.engine_client import EngineClient, EngineRpc
from quantra_common.engine_client._generated.quantra.enums.EquityOptionType import (
    EquityOptionType,
)
from quantra_common.engine_client._generated.quantra.enums.EquitySettlementType import (
    EquitySettlementType,
)
from quantra_common.engine_client._generated.quantra.EquityEuropeanExercise import (
    EquityEuropeanExerciseT,
)
from quantra_common.engine_client._generated.quantra.EquityExercise import (
    EquityExercise,
)
from quantra_common.engine_client._generated.quantra.EquityOption import EquityOptionT
from quantra_common.engine_client._generated.quantra.EquityPayoff import EquityPayoff
from quantra_common.engine_client._generated.quantra.EquityPlainVanillaPayoff import (
    EquityPlainVanillaPayoffT,
)
from quantra_common.engine_client._generated.quantra.PriceEquityOption import (
    PriceEquityOptionT,
)
from quantra_common.engine_client._generated.quantra.PriceEquityOptionRequest import (
    PriceEquityOptionRequestT,
)
from quantra_common.engine_client._generated.quantra.PriceEquityOptionResponse import (
    PriceEquityOptionResponse,
)
from quantra_orchestrator.pricing._translator import (
    DEFAULT_EQUITY_MODEL_ID,
    CurveRole,
    CurveTranslationError,
    QuoteResolutionError,
    ResolvedMarketData,
    SpotTranslationError,
    VolSurfaceTranslationError,
    build_pricing_from_resolved,
    resolved_curve_id,
    resolved_underlier_id,
    resolved_vol_surface_id,
)
from quantra_orchestrator.pricing.concurrency import EngineBatch
from quantra_orchestrator.pricing.equity_options.errors import (
    EquityOptionCurveResolutionFailedError,
    EquityOptionQuoteResolutionFailedError,
    EquityOptionSpotResolutionFailedError,
    EquityOptionSurfaceResolutionFailedError,
)
from quantra_orchestrator.pricing.equity_options.models import (
    EquityOptionResult,
    EquityOptionTrade,
)
from quantra_orchestrator.tracing.stages import serialize_fb_object

EQUITY_OPTION_ENGINE_RPC: Final[EngineRpc] = EngineRpc.PRICE_EQUITY_OPTION

_DEFAULT_TRADE_ID_PREFIX: Final[str] = "equity-option"
_DEFAULT_STRIKE: Final[float] = 100.0
_DEFAULT_QUANTITY: Final[float] = 1.0
_DEFAULT_EXPIRY_DATE: Final[str] = "2026-01-15"

_OPTION_TYPE_BY_NAME: Final[dict[str, int]] = {
    "Call": EquityOptionType.Call,
    "Put": EquityOptionType.Put,
}

_SETTLEMENT_BY_NAME: Final[dict[str, int]] = {
    "Physical": EquitySettlementType.Physical,
    "Cash": EquitySettlementType.Cash,
}

_SETTLEMENT_NAME_BY_VALUE: Final[dict[int, str]] = {
    EquitySettlementType.Physical: "Physical",
    EquitySettlementType.Cash: "Cash",
}


def build_equity_option_request(
    trades: Sequence[EquityOptionTrade],
    *,
    resolved: ResolvedMarketData,
) -> bytes:
    """Encode one batch of equity-option trades as ``PriceEquityOptionRequest`` bytes.

       The surrounding ``Pricing`` context (discount + dividend curves, the underlier
       spot quote, the BlackVol surface, the equity model and the underlier spec) is
       built once per batch from the caller's *resolved* market data via
       :func:`~quantra_orchestrator.pricing._translator.build_pricing_from_resolved`
    — faithful curves with the resolved quote values substituted in
       , the supplied spot, and the BlackVol surface carrying the resolved vol
       level, not the canonical fixture. Each trade's ``equity_option`` body still
       overrides the per-trade levers (option type / strike / quantity / expiry /
       settlement) and the per-trade discount curve, ``volatility``, ``model`` and
       ``underlyingId`` references honour the resolved entity ids.

       The resolved graph reaches here by route-closure capture, not through
       the concurrency seam.
       Neutral translator failures are mapped onto the equity-option 422 codes
       (``equity_option_quote_resolution_failed`` /
       ``equity_option_surface_resolution_failed`` /
       ``equity_option_spot_resolution_failed`` /
       ``equity_option_curve_resolution_failed``) so the catalog is unchanged.
    """

    builder = flatbuffers.Builder(2048)
    # ForceDefaults: the engine requires every set field explicit on the wire;
    # zero-default enums/scalars (e.g. Frequency.Annual == 0) must not be omitted.
    builder.ForceDefaults(True)
    request = PriceEquityOptionRequestT()
    try:
        request.pricing = build_pricing_from_resolved(resolved)
    except QuoteResolutionError as exc:
        raise EquityOptionQuoteResolutionFailedError(
            missing_canonical_ids=exc.missing_canonical_ids,
            as_of=resolved.as_of,
        ) from exc
    except SpotTranslationError as exc:
        raise EquityOptionSpotResolutionFailedError(reason=exc.reason, details=exc.details) from exc
    except VolSurfaceTranslationError as exc:
        raise EquityOptionSurfaceResolutionFailedError(
            reason=exc.reason, details=exc.details
        ) from exc
    except CurveTranslationError as exc:
        raise EquityOptionCurveResolutionFailedError(
            reason=exc.reason, details=exc.details
        ) from exc

    discount_curve_id = _resolve_discount_curve_ref(resolved)
    vol_surface_id = _resolve_vol_surface_ref(resolved)
    underlier_id = _resolve_underlier_ref(resolved)
    request.options = [
        _build_one_equity_option(
            trade,
            discount_curve_id=discount_curve_id,
            vol_surface_id=vol_surface_id,
            model_id=DEFAULT_EQUITY_MODEL_ID,
            underlier_id=underlier_id,
        )
        for trade in trades
    ]
    builder.Finish(request.Pack(builder))
    return bytes(builder.Output())


def _resolve_discount_curve_ref(resolved: ResolvedMarketData) -> str:
    """Return the resolved discount curve id by role.

    Reads the discount reference id off the role map; falls back to the first
    translated curve so a role-less caller stays robust.
    ``build_pricing_from_resolved`` has already rejected the empty-curve case, so
    ``curves[0]`` is safe. Never ``CANONICAL_DISCOUNT_CURVE_ID``.
    """

    return resolved.curve_id_for_role(CurveRole.DISCOUNT) or resolved_curve_id(resolved.curves[0])


def _resolve_vol_surface_ref(resolved: ResolvedMarketData) -> str:
    """Return the resolved BlackVol surface id the per-trade ``volatility`` ref points at.

    The equity route always supplies exactly one resolved vol surface (a required
    field on the assembler output), so the reference honours that resolved entity
    id, never ``CANONICAL_EQUITY_VOL_SURFACE_ID``. An absent surface is
    defensively surfaced as the equity surface-resolution 422 rather than an
    opaque ``IndexError``.
    """

    if not resolved.vol_surfaces:
        raise EquityOptionSurfaceResolutionFailedError(
            reason=("No resolved vol surface to reference in the equity-option request."),
            details=[{"resolved_vol_surface_count": 0}],
        )
    return resolved_vol_surface_id(resolved.vol_surfaces[0])


def _resolve_underlier_ref(resolved: ResolvedMarketData) -> str:
    """Return the resolved underlier id the per-trade ``underlyingId`` ref points at.

    The equity route always supplies exactly one resolved spot, so the reference
    honours the resolved underlier id (the spot's market identity),
    never ``CANONICAL_EQUITY_UNDERLYING_ID``. An absent spot is defensively
    surfaced as the equity spot-resolution 422.
    """

    if resolved.spot is None:
        raise EquityOptionSpotResolutionFailedError(
            reason="No resolved spot to reference in the equity-option request.",
            details=[{"resolved_spot": None}],
        )
    return resolved_underlier_id(resolved.spot)


def decode_equity_option_response(
    response_bytes: bytes,
    *,
    expected_count: int,
) -> list[EquityOptionResult]:
    """Decode ``PriceEquityOptionResponse`` bytes into typed results.

    Each :class:`EquityOptionResponse` flatbuffer carries the
    per-trade diagnostics ``equity_option_response.fbs`` documents:
    NPV, the full Greek surface (delta / gamma / vega / theta /
    rho), implied volatility, used spot, used strike, used
    settlement. We propagate them verbatim plus a thin ``extras``
    carry that mirrors the engine's wire naming so a future fix
    can surface without a decoder change.
    """

    if not response_bytes:
        msg = "decode_equity_option_response: empty engine response."
        raise ValueError(msg)
    try:
        response = PriceEquityOptionResponse.GetRootAs(response_bytes, 0)
        actual = response.OptionsLength()
    except Exception as exc:
        msg = f"decode_equity_option_response: malformed engine response ({exc})."
        raise ValueError(msg) from exc

    if actual != expected_count:
        msg = (
            "decode_equity_option_response: engine returned "
            f"{actual} result(s), expected {expected_count}."
        )
        raise ValueError(msg)

    results: list[EquityOptionResult] = []
    for i in range(actual):
        option_response = response.Options(i)
        if option_response is None:
            msg = f"decode_equity_option_response: null equity option result at index {i}."
            raise ValueError(msg)
        used_settlement = int(option_response.UsedSettlement())
        results.append(
            EquityOptionResult(
                npv=float(option_response.Npv()),
                delta=float(option_response.Delta()),
                gamma=float(option_response.Gamma()),
                vega=float(option_response.Vega()),
                theta=float(option_response.Theta()),
                rho=float(option_response.Rho()),
                implied_volatility=float(option_response.ImpliedVolatility()),
                used_spot=float(option_response.UsedSpot()),
                used_strike=float(option_response.UsedStrike()),
                used_settlement=_SETTLEMENT_NAME_BY_VALUE.get(used_settlement, "Physical"),
                extras={
                    # Engine wire-naming pin — if the engine
                    # ever emits a non-zero diagnostic that the
                    # typed mirror happens to coerce to 0.0 we
                    # still see the raw value here.
                    "implied_volatility": float(option_response.ImpliedVolatility()),
                    "used_spot": float(option_response.UsedSpot()),
                    "used_strike": float(option_response.UsedStrike()),
                    "used_settlement": used_settlement,
                },
            )
        )
    return results


def decode_equity_option_request_wire(request_bytes: bytes) -> dict[str, Any]:
    """Decode the EXACT transmitted equity-option request bytes to JSON.

    Reads the buffer with the generated FlatBuffers reader
    (``PriceEquityOptionRequestT.InitFromPackedBuf``) so the result is provably
    derived from the wire bytes, then renders it JSON-safe via the shared
    :func:`serialize_fb_object`. Best-effort: a malformed buffer degrades to a
    marker dict, never raises. Mirrors ``decode_swap_ir_request_wire``.
    """

    try:
        request = PriceEquityOptionRequestT.InitFromPackedBuf(request_bytes, 0)
    except Exception as exc:  # tracing is best-effort — never raise
        return {"__undecodable__": True, "exc_type": type(exc).__name__}
    return serialize_fb_object(request)


async def price_equity_option_batch(
    engine: EngineClient,
    batch: EngineBatch[EquityOptionTrade],
    *,
    resolved: ResolvedMarketData,
) -> Sequence[EquityOptionResult]:
    """The per-product translator injected into the ``execute`` runner.

    One ``EngineClient.call`` per batch. ``resolved`` is the route-captured
    :class:`ResolvedMarketData` the route's ``price_batch`` lambda binds;
    it carries the faithful curves / quotes / spot / vol surface the request is
    assembled from.
    """

    request_bytes = build_equity_option_request(batch.trades, resolved=resolved)
    response_bytes = await engine.call(EQUITY_OPTION_ENGINE_RPC, request_bytes)
    return decode_equity_option_response(response_bytes, expected_count=len(batch.trades))


def _build_one_equity_option(
    trade: EquityOptionTrade,
    *,
    discount_curve_id: str,
    vol_surface_id: str,
    model_id: str,
    underlier_id: str,
) -> PriceEquityOptionT:
    """Map one :class:`EquityOptionTrade` to a ``PriceEquityOption`` FB graph.

    Both the top-level body shape (``trade.equity_option["strike"]``)
    and the engine-canonical nested shape
    (``trade.equity_option["option"]["strike"]``) are accepted —
    the latter matches the engine's own
    ``equity_option_request.json`` example shape. The ``underlyingId``,
    ``discountingCurve``, ``volatility`` and ``model`` references honour the
    resolved entity ids the translator placed in the ``Pricing`` graph,
    not ``CANONICAL_*``.
    """

    body_root: Mapping[str, Any] = trade.equity_option or {}
    nested_option = body_root.get("option")
    body: Mapping[str, Any] = nested_option if isinstance(nested_option, Mapping) else body_root

    trade_id_raw = body.get("trade_id") or trade.name
    trade_id = str(trade_id_raw) if trade_id_raw else _default_trade_id(trade)
    option_type = _OPTION_TYPE_BY_NAME.get(
        str(body.get("option_type", "Call")), EquityOptionType.Call
    )
    strike = float(body.get("strike", _DEFAULT_STRIKE))
    quantity = float(body.get("quantity", _DEFAULT_QUANTITY))
    expiry_date = str(body.get("expiry_date", _DEFAULT_EXPIRY_DATE))
    settlement = _SETTLEMENT_BY_NAME.get(
        str(body.get("settlement", "Physical")),
        EquitySettlementType.Physical,
    )

    payoff = EquityPlainVanillaPayoffT()
    payoff.optionType = option_type
    payoff.strike = strike

    exercise = EquityEuropeanExerciseT()
    exercise.expiryDate = expiry_date

    option = EquityOptionT()
    option.tradeId = trade_id
    option.underlyingId = underlier_id
    option.quantity = quantity
    option.settlement = settlement
    option.payoffType = EquityPayoff.EquityPlainVanillaPayoff
    option.payoff = payoff
    option.exerciseType = EquityExercise.EquityEuropeanExercise
    option.exercise = exercise

    price_option = PriceEquityOptionT()
    price_option.option = option
    price_option.discountingCurve = discount_curve_id
    price_option.volatility = vol_surface_id
    price_option.model = model_id
    return price_option


def _default_trade_id(trade: EquityOptionTrade) -> str:
    """Fallback ``trade_id`` for trades that don't carry one explicitly."""

    if trade.equity_option_id is not None:
        return f"{_DEFAULT_TRADE_ID_PREFIX}-{trade.equity_option_id}"
    return f"{_DEFAULT_TRADE_ID_PREFIX}-{id(trade):x}"


__all__ = [
    "EQUITY_OPTION_ENGINE_RPC",
    "build_equity_option_request",
    "decode_equity_option_response",
    "price_equity_option_batch",
]
