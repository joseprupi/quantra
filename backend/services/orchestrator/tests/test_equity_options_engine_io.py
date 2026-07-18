"""Unit tests for ``pricing/equity_options/engine_io.py``.

The wire layer ships three callables (build / decode / price_batch).
These tests pin every contract a downstream consumer (route handler /
live engine) relies on:

* RPC bindings: ``EQUITY_OPTION_ENGINE_RPC`` resolves to the
  canonical :attr:`EngineRpc.PRICE_EQUITY_OPTION`.
* :func:`build_equity_option_request` produces a valid
  ``PriceEquityOptionRequest`` flatbuffer whose ``Pricing`` context is
  the caller's *resolved* discount + dividend curves + underlier spot +
  BlackVol surface: the encoded curve points carry the
  supplied / MD-resolved rate (not the canonical 0.03), the spot quote
  carries the supplied value (not the canonical 100.0), the BlackVol
  surface carries the supplied vol (not the canonical 0.20), and the
  per-trade discount / volatility / underlyingId references carry the
  resolved entity ids (no ``CANONICAL_*`` leak). The analytic
  Black-Scholes equity model survives as a pure-config default.
* a missing curve / spot quote → ``equity_option_quote_resolution_failed``
  ; an unmapped surface kind →
  ``equity_option_surface_resolution_failed``; an unmapped curve helper
  kind → ``equity_option_curve_resolution_failed``.
* :func:`build_equity_option_request` honors per-trade overrides
  (option type / strike / quantity / expiry / settlement) without
  breaking the pricing context, **including the nested
  ``trade.equity_option["option"]["..."]`` shape**.
* :func:`decode_equity_option_response` round-trips a synthesised
  ``PriceEquityOptionResponse`` flatbuffer and rejects malformed shapes.
* :func:`price_equity_option_batch` calls ``EngineClient.call`` exactly
  once per batch (assembled from the resolved bundle) and decodes the
  response; engine exceptions bubble unchanged.
* batching hook: ``EngineBatch.shared_inputs[equity_surface_id]`` is always
  present (``None`` allowed for inline-only surfaces).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date

import flatbuffers
import pytest

from quantra_common.engine_client import EngineClient, EngineRpc
from quantra_common.engine_client._generated.quantra.enums.EquitySettlementType import (
    EquitySettlementType,
)
from quantra_common.engine_client._generated.quantra.EquityOptionResponse import (
    EquityOptionResponseT,
)
from quantra_common.engine_client._generated.quantra.PriceEquityOptionRequest import (
    PriceEquityOptionRequest,
    PriceEquityOptionRequestT,
)
from quantra_common.engine_client._generated.quantra.PriceEquityOptionResponse import (
    PriceEquityOptionResponseT,
)
from quantra_common.engine_client._generated.quantra.VolPayload import VolPayload
from quantra_orchestrator.pricing._translator import (
    DEFAULT_EQUITY_MODEL_ID,
    DEFAULT_EQUITY_SPOT_QUOTE_ID,
    DEFAULT_EQUITY_UNDERLYING_ID,
    CurveRole,
    ResolvedMarketData,
    resolved_curve_id,
)
from quantra_orchestrator.pricing.concurrency import EngineBatch
from quantra_orchestrator.pricing.equity_options.engine_io import (
    EQUITY_OPTION_ENGINE_RPC,
    build_equity_option_request,
    decode_equity_option_response,
    price_equity_option_batch,
)
from quantra_orchestrator.pricing.equity_options.errors import (
    EquityOptionCurveResolutionFailedError,
    EquityOptionQuoteResolutionFailedError,
    EquityOptionSurfaceResolutionFailedError,
)
from quantra_orchestrator.pricing.equity_options.models import (
    EquityOptionResult,
    EquityOptionTrade,
    ResolvedCurve,
    ResolvedQuoteValue,
    ResolvedSpotQuote,
    ResolvedVolSurface,
)

_VOL_QUOTE_ID = "AAPL.IMPLVOL.1Y"
_SPOT_QUOTE_ID = "AAPL.SPOT"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _trade(
    name: str = "eq-1",
    *,
    body: dict[str, object] | None = None,
) -> EquityOptionTrade:
    return EquityOptionTrade(
        equity_option_id=None,
        name=name,
        equity_option=body if body is not None else {},
    )


def _deposit_point(*, quote_id: str | None = None, rate: float | None = 0.04) -> dict[str, object]:
    point: dict[str, object] = {
        "point_type": "DepositHelper",
        "tenor": {"n": 1, "unit": "Years"},
        "fixing_days": 2,
        "calendar": "TARGET",
        "business_day_convention": "ModifiedFollowing",
        "day_counter": "Actual365Fixed",
    }
    if quote_id is not None:
        point["quote_id"] = quote_id
    if rate is not None:
        point["rate"] = rate
    return point


def _resolved_curve(
    curve_id: uuid.UUID | None,
    *,
    role: str = "discount",
    name: str = "USD-OIS",
    points: list[dict[str, object]] | None = None,
) -> ResolvedCurve:
    return ResolvedCurve(
        id=curve_id,
        name=name,
        role=role,
        currency="USD",
        day_counter="Actual365Fixed",
        reference_date=date(2026, 5, 13),
        points=points if points is not None else [_deposit_point(rate=0.04)],
        body={"interpolator": "LogLinear"},
    )


def _quote(canonical_id: str = "USD.IRS.1Y", value: float = 0.0425) -> ResolvedQuoteValue:
    return ResolvedQuoteValue(canonical_id=canonical_id, as_of=date(2026, 5, 13), value=value)


def _vol_surface(
    surface_id: uuid.UUID | None = None,
    *,
    name: str = "AAPL-vol",
    kind: str = "BlackVolSpec",
    base: dict[str, object] | None = None,
    payload: dict[str, object] | None = None,
) -> ResolvedVolSurface:
    if payload is None:
        payload = {"base": base if base is not None else {"constant_vol": 0.27}}
    return ResolvedVolSurface(id=surface_id, name=name, kind=kind, payload=payload)


def _spot(*, value: float | None = 123.45, canonical_id: str | None = None) -> ResolvedSpotQuote:
    return ResolvedSpotQuote(canonical_id=canonical_id, value=value)


def _resolved(
    *,
    curves: list[ResolvedCurve] | None = None,
    quotes: list[ResolvedQuoteValue] | None = None,
    vol_surfaces: list[ResolvedVolSurface] | None = None,
    spot: ResolvedSpotQuote | None = None,
    curve_roles: dict[CurveRole, str] | None = None,
    as_of: str = "2026-05-13",
) -> ResolvedMarketData:
    """Default: discount + dividend curves + a BlackVol surface + an inline spot."""

    if curves is None:
        curves = [
            _resolved_curve(uuid.uuid4(), role="discount", name="USD-DISC"),
            _resolved_curve(
                uuid.uuid4(),
                role="dividend",
                name="AAPL-DIV",
                points=[_deposit_point(rate=0.01)],
            ),
        ]
    if quotes is None:
        quotes = []
    if vol_surfaces is None:
        vol_surfaces = [_vol_surface(uuid.uuid4())]
    if spot is None:
        spot = _spot()
    if curve_roles is None:
        curve_roles = {CurveRole.DISCOUNT: resolved_curve_id(curves[0])}
        if len(curves) > 1:
            curve_roles[CurveRole.DIVIDEND] = resolved_curve_id(curves[1])
    return ResolvedMarketData(
        as_of=as_of,
        curves=tuple(curves),
        quotes=tuple(quotes),
        curve_roles=curve_roles,
        vol_surfaces=tuple(vol_surfaces),
        spot=spot,
    )


def _response_bytes(
    npvs: Sequence[float],
    *,
    delta: float = 0.55,
    gamma: float = 0.04,
    vega: float = 12.5,
    theta: float = -8.0,
    rho: float = 4.5,
    implied_volatility: float = 0.20,
    used_spot: float = 100.0,
    used_strike: float = 100.0,
    used_settlement: int = EquitySettlementType.Physical,
) -> bytes:
    """Synthesise a ``PriceEquityOptionResponse`` flatbuffer with given NPVs."""

    response = PriceEquityOptionResponseT()
    response.options = []
    for npv in npvs:
        v = EquityOptionResponseT()
        v.tradeId = "eq"
        v.npv = npv
        v.delta = delta
        v.gamma = gamma
        v.vega = vega
        v.theta = theta
        v.rho = rho
        v.impliedVolatility = implied_volatility
        v.usedSpot = used_spot
        v.usedStrike = used_strike
        v.usedSettlement = used_settlement
        response.options.append(v)
    builder = flatbuffers.Builder(256)
    builder.Finish(response.Pack(builder))
    return bytes(builder.Output())


class _FakeEngine(EngineClient):
    """Captures one ``call(rpc, request_bytes)`` and returns canned bytes."""

    def __init__(self, response: bytes | None = None) -> None:
        self.calls: list[tuple[EngineRpc, bytes]] = []
        self._response = response
        self._exception: BaseException | None = None

    def set_exception(self, exc: BaseException) -> None:
        self._exception = exc

    async def call(self, rpc: EngineRpc, request_bytes: bytes) -> bytes:
        self.calls.append((rpc, request_bytes))
        if self._exception is not None:
            raise self._exception
        if self._response is None:
            msg = "_FakeEngine: no response configured"
            raise AssertionError(msg)
        return self._response

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# RPC enum binding
# ---------------------------------------------------------------------------


def test_equity_option_engine_rpc_is_price_equity_option() -> None:
    """Pin the canonical RPC name."""

    assert EQUITY_OPTION_ENGINE_RPC is EngineRpc.PRICE_EQUITY_OPTION
    assert EQUITY_OPTION_ENGINE_RPC.value == "PriceEquityOption"


# ---------------------------------------------------------------------------
# build_equity_option_request — faithful flatbuffer shape
# ---------------------------------------------------------------------------


def test_build_request_emits_a_parseable_flatbuffer() -> None:
    """Output is a valid ``PriceEquityOptionRequest`` flatbuffer, one option per trade.

    The ``as_of`` now comes from the resolved bundle, not ``shared_inputs``.
    """

    raw = build_equity_option_request([_trade("a"), _trade("b")], resolved=_resolved())
    request = PriceEquityOptionRequest.GetRootAs(raw, 0)
    assert request.OptionsLength() == 2
    pricing = request.Pricing()
    assert pricing is not None
    assert pricing.AsOfDate() == b"2026-05-13"


def test_build_request_carries_resolved_discount_and_dividend_curves() -> None:
    """``Pricing.rates.curves`` carries both resolved curves (real ids, not canonical)."""

    discount_id = uuid.uuid4()
    dividend_id = uuid.uuid4()
    resolved = _resolved(
        curves=[
            _resolved_curve(discount_id, role="discount", name="USD-DISC"),
            _resolved_curve(
                dividend_id,
                role="dividend",
                name="AAPL-DIV",
                points=[_deposit_point(rate=0.011)],
            ),
        ]
    )
    raw = build_equity_option_request([_trade()], resolved=resolved)
    request = PriceEquityOptionRequestT.InitFromPackedBuf(raw, 0)
    # The object API decodes string fields as bytes.
    ids = {c.id for c in request.pricing.rates.curves}
    assert ids == {str(discount_id).encode(), str(dividend_id).encode()}
    assert b"discount" not in ids
    assert b"dividend" not in ids
    # The dividend curve's supplied rate reaches the bytes faithfully.
    dividend = next(c for c in request.pricing.rates.curves if c.id == str(dividend_id).encode())
    assert dividend.points[0].point.rate == pytest.approx(0.011)


def test_build_request_carries_resolved_spot_value() -> None:
    """``Pricing.quotes`` carries the underlier spot with the supplied value."""

    raw = build_equity_option_request([_trade()], resolved=_resolved(spot=_spot(value=222.5)))
    request = PriceEquityOptionRequestT.InitFromPackedBuf(raw, 0)
    assert len(request.pricing.quotes) == 1
    quote = request.pricing.quotes[0]
    assert quote.value == pytest.approx(222.5)
    assert quote.value != pytest.approx(100.0)  # not the canonical default spot
    # Inline-only spot → the documented default spot-quote id.
    assert quote.id == DEFAULT_EQUITY_SPOT_QUOTE_ID.encode()


def test_build_request_resolves_spot_canonical_id_via_md() -> None:
    """A canonical-id spot resolves through the MD map and honours the resolved id."""

    raw = build_equity_option_request(
        [_trade()],
        resolved=_resolved(
            quotes=[_quote(_SPOT_QUOTE_ID, 314.15)],
            spot=_spot(value=None, canonical_id=_SPOT_QUOTE_ID),
        ),
    )
    request = PriceEquityOptionRequestT.InitFromPackedBuf(raw, 0)
    quote = request.pricing.quotes[0]
    assert quote.id == _SPOT_QUOTE_ID.encode()  # resolved id
    assert quote.value == pytest.approx(314.15)
    # The underlier id honours the resolved spot identity.
    underlying = request.pricing.equity.equityUnderlyings[0]
    assert underlying.id == _SPOT_QUOTE_ID.encode()
    assert underlying.id != DEFAULT_EQUITY_UNDERLYING_ID.encode()


def test_build_request_carries_resolved_black_vol_surface() -> None:
    """``Pricing.volatility.volSurfaces`` carries the resolved BlackVol surface + vol."""

    surface_id = uuid.uuid4()
    raw = build_equity_option_request(
        [_trade()],
        resolved=_resolved(vol_surfaces=[_vol_surface(surface_id, base={"constant_vol": 0.33})]),
    )
    request = PriceEquityOptionRequestT.InitFromPackedBuf(raw, 0)
    volatility = request.pricing.volatility
    assert volatility is not None
    assert len(volatility.volSurfaces) == 1
    surface = volatility.volSurfaces[0]
    assert surface.id == str(surface_id).encode()
    assert surface.id != b"equity_vol"  # not the canonical surface id
    assert surface.payloadType == VolPayload.BlackVolSpec
    assert surface.payload.base.constantVol == pytest.approx(0.33)
    assert surface.payload.base.constantVol != pytest.approx(0.20)
    # Equity request → swaption diagnostics flag stays False.
    assert request.pricing.options.swaptionPricingDetails is False


def test_build_request_vol_surface_quote_substitution_drops_quote_id() -> None:
    """The resolved vol lands in ``constantVol``; ``quote_id`` never reaches bytes."""

    raw = build_equity_option_request(
        [_trade()],
        resolved=_resolved(
            quotes=[_quote(_VOL_QUOTE_ID, 0.41)],
            vol_surfaces=[_vol_surface(base={"quote_id": _VOL_QUOTE_ID})],
        ),
    )
    request = PriceEquityOptionRequestT.InitFromPackedBuf(raw, 0)
    base = request.pricing.volatility.volSurfaces[0].payload.base
    assert base.constantVol == pytest.approx(0.41)
    assert not base.quoteId  # None / "" — substituted server-side (invariant #8)


def test_build_request_carries_default_equity_model() -> None:
    """``Pricing.volatility.models`` carries the default equity model (pure config)."""

    raw = build_equity_option_request([_trade()], resolved=_resolved())
    request = PriceEquityOptionRequestT.InitFromPackedBuf(raw, 0)
    model_ids = {m.id for m in request.pricing.volatility.models}
    assert DEFAULT_EQUITY_MODEL_ID.encode() in model_ids


def test_build_request_carries_resolved_equity_underlying() -> None:
    """``Pricing.equity.equityUnderlyings`` honours the resolved spot + dividend ids."""

    dividend_id = uuid.uuid4()
    resolved = _resolved(
        curves=[
            _resolved_curve(uuid.uuid4(), role="discount", name="USD-DISC"),
            _resolved_curve(dividend_id, role="dividend", name="AAPL-DIV"),
        ]
    )
    raw = build_equity_option_request([_trade()], resolved=resolved)
    request = PriceEquityOptionRequestT.InitFromPackedBuf(raw, 0)
    assert len(request.pricing.equity.equityUnderlyings) == 1
    underlying = request.pricing.equity.equityUnderlyings[0]
    # Inline-only spot → default underlier + spot-quote ids; dividend ref resolved.
    assert underlying.id == DEFAULT_EQUITY_UNDERLYING_ID.encode()
    assert underlying.spotQuoteId == DEFAULT_EQUITY_SPOT_QUOTE_ID.encode()
    assert underlying.dividendYieldCurveId == str(dividend_id).encode()
    assert underlying.dividendYieldCurveId != b"dividend"


def test_build_request_per_option_references_resolved_ids() -> None:
    """Every ``PriceEquityOption`` references the resolved bundle ids (no CANONICAL_*)."""

    discount_id = uuid.uuid4()
    surface_id = uuid.uuid4()
    resolved = _resolved(
        curves=[
            _resolved_curve(discount_id, role="discount", name="USD-DISC"),
            _resolved_curve(uuid.uuid4(), role="dividend", name="AAPL-DIV"),
        ],
        vol_surfaces=[_vol_surface(surface_id)],
    )
    raw = build_equity_option_request([_trade()], resolved=resolved)
    request = PriceEquityOptionRequest.GetRootAs(raw, 0)
    one = request.Options(0)
    assert one is not None
    assert one.DiscountingCurve() == str(discount_id).encode()
    assert one.Volatility() == str(surface_id).encode()
    assert one.Model() == DEFAULT_EQUITY_MODEL_ID.encode()
    option = one.Option()
    assert option is not None
    assert option.UnderlyingId() == DEFAULT_EQUITY_UNDERLYING_ID.encode()
    # None of the canonical curve / surface placeholders leak into the wire refs.
    for ref in (one.DiscountingCurve(), one.Volatility()):
        assert ref not in (b"discount", b"dividend", b"equity_vol")


def test_build_request_missing_curve_quote_raises_quote_resolution_failed() -> None:
    """A curve quote_id with no resolved value → ``equity_option_quote_resolution_failed``."""

    resolved = _resolved(
        curves=[
            _resolved_curve(
                uuid.uuid4(),
                role="discount",
                name="USD-DISC",
                points=[_deposit_point(quote_id="USD.IRS.1Y", rate=None)],
            ),
            _resolved_curve(uuid.uuid4(), role="dividend", name="AAPL-DIV"),
        ],
        quotes=[],
    )
    with pytest.raises(EquityOptionQuoteResolutionFailedError) as excinfo:
        build_equity_option_request([_trade()], resolved=resolved)
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "equity_option_quote_resolution_failed"


def test_build_request_missing_spot_quote_raises_quote_resolution_failed() -> None:
    """A spot canonical_id with no resolved value → quote-resolution 422."""

    resolved = _resolved(
        quotes=[],
        spot=_spot(value=None, canonical_id=_SPOT_QUOTE_ID),
    )
    with pytest.raises(EquityOptionQuoteResolutionFailedError) as excinfo:
        build_equity_option_request([_trade()], resolved=resolved)
    assert excinfo.value.status_code == 422
    assert _SPOT_QUOTE_ID in [d["canonical_id"] for d in (excinfo.value.details or [])]


def test_build_request_unknown_surface_kind_raises_surface_resolution_failed() -> None:
    """A non-equity surface kind → ``equity_option_surface_resolution_failed``."""

    resolved = _resolved(vol_surfaces=[_vol_surface(kind="OptionletVolSpec")])
    with pytest.raises(EquityOptionSurfaceResolutionFailedError) as excinfo:
        build_equity_option_request([_trade()], resolved=resolved)
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "equity_option_surface_resolution_failed"


def test_build_request_unknown_helper_kind_raises_curve_resolution_failed() -> None:
    """An unmapped curve ``point_type`` → ``equity_option_curve_resolution_failed``."""

    bad_point: dict[str, object] = {
        "point_type": "WidgetHelper",
        "point": {"quote_id": "USD.IRS.1Y"},
    }
    resolved = _resolved(
        curves=[
            _resolved_curve(uuid.uuid4(), role="discount", name="USD-DISC", points=[bad_point]),
            _resolved_curve(uuid.uuid4(), role="dividend", name="AAPL-DIV"),
        ]
    )
    with pytest.raises(EquityOptionCurveResolutionFailedError) as excinfo:
        build_equity_option_request([_trade()], resolved=resolved)
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "equity_option_curve_resolution_failed"


def test_build_request_honors_top_level_body_overrides() -> None:
    raw = build_equity_option_request(
        [
            _trade(
                body={
                    "trade_id": "demo-AAPL",
                    "option_type": "Put",
                    "strike": 95.5,
                    "quantity": 5.0,
                    "expiry_date": "2027-06-15",
                    "settlement": "Cash",
                }
            )
        ],
        resolved=_resolved(),
    )
    request = PriceEquityOptionRequest.GetRootAs(raw, 0)
    one = request.Options(0)
    assert one is not None
    option = one.Option()
    assert option is not None
    assert option.TradeId() == b"demo-AAPL"
    assert option.Quantity() == pytest.approx(5.0)
    assert option.Settlement() == EquitySettlementType.Cash


def test_build_request_honors_nested_option_body_shape() -> None:
    """The engine's own ``equity_option_request.json`` shape nests under ``option``."""

    raw = build_equity_option_request(
        [
            _trade(
                body={
                    "option": {
                        "trade_id": "demo-NESTED",
                        "option_type": "Call",
                        "strike": 110.0,
                        "quantity": 1.0,
                        "expiry_date": "2027-09-15",
                        "settlement": "Physical",
                    }
                }
            )
        ],
        resolved=_resolved(),
    )
    request = PriceEquityOptionRequest.GetRootAs(raw, 0)
    one = request.Options(0)
    assert one is not None
    option = one.Option()
    assert option is not None
    assert option.TradeId() == b"demo-NESTED"
    assert option.Settlement() == EquitySettlementType.Physical


def test_build_request_default_option_type_is_call() -> None:
    """Trades with no ``option_type`` default to ``Call``."""

    raw = build_equity_option_request([_trade()], resolved=_resolved())
    request = PriceEquityOptionRequest.GetRootAs(raw, 0)
    one = request.Options(0)
    assert one is not None
    option = one.Option()
    assert option is not None
    assert option.PayoffType() != 0  # PayoffType union discriminator set
    assert option.Quantity() == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# decode_equity_option_response — happy paths
# ---------------------------------------------------------------------------


def test_decode_response_round_trips_one_trade() -> None:
    body = _response_bytes([1234.5])
    decoded = decode_equity_option_response(body, expected_count=1)
    assert len(decoded) == 1
    result = decoded[0]
    assert isinstance(result, EquityOptionResult)
    assert result.npv == pytest.approx(1234.5)
    assert result.delta == pytest.approx(0.55)
    assert result.gamma == pytest.approx(0.04)
    assert result.vega == pytest.approx(12.5)
    assert result.theta == pytest.approx(-8.0)
    assert result.rho == pytest.approx(4.5)
    assert result.implied_volatility == pytest.approx(0.20)
    assert result.used_spot == pytest.approx(100.0)
    assert result.used_strike == pytest.approx(100.0)
    assert result.used_settlement == "Physical"
    assert result.extras["used_spot"] == pytest.approx(100.0)


def test_decode_response_preserves_input_order() -> None:
    body = _response_bytes([1.0, 2.0, 3.0])
    decoded = decode_equity_option_response(body, expected_count=3)
    assert [r.npv for r in decoded] == [
        pytest.approx(1.0),
        pytest.approx(2.0),
        pytest.approx(3.0),
    ]


def test_decode_response_cash_settlement_round_trips() -> None:
    body = _response_bytes([42.0], used_settlement=EquitySettlementType.Cash)
    decoded = decode_equity_option_response(body, expected_count=1)
    assert decoded[0].used_settlement == "Cash"


# ---------------------------------------------------------------------------
# decode_equity_option_response — malformed shapes
# ---------------------------------------------------------------------------


def test_decode_rejects_empty_body() -> None:
    with pytest.raises(ValueError, match="empty engine response"):
        decode_equity_option_response(b"", expected_count=1)


def test_decode_rejects_malformed_root() -> None:
    with pytest.raises(ValueError, match=r"malformed engine response|expected "):
        decode_equity_option_response(b"this-is-not-a-flatbuffer", expected_count=1)


def test_decode_rejects_count_mismatch() -> None:
    body = _response_bytes([1.0, 2.0])
    with pytest.raises(ValueError, match=r"returned 2 result\(s\), expected 3"):
        decode_equity_option_response(body, expected_count=3)


# ---------------------------------------------------------------------------
# price_equity_option_batch — the translator
# ---------------------------------------------------------------------------


async def test_price_batch_round_trip() -> None:
    body = _response_bytes([42.0])
    engine = _FakeEngine(response=body)
    surface_id = uuid.uuid4()
    batch: EngineBatch[EquityOptionTrade] = EngineBatch(
        trades=(_trade("a"),),
        shared_inputs={
            "as_of": "2026-05-13",
            "equity_surface_id": None,
            "discount_curve_id": None,
            "dividend_curve_id": None,
        },
    )

    results: Sequence[EquityOptionResult] = await price_equity_option_batch(
        engine, batch, resolved=_resolved(vol_surfaces=[_vol_surface(surface_id)])
    )

    assert len(engine.calls) == 1
    rpc, request_bytes = engine.calls[0]
    assert rpc is EngineRpc.PRICE_EQUITY_OPTION
    parsed = PriceEquityOptionRequestT.InitFromPackedBuf(request_bytes, 0)
    assert len(parsed.options) == 1
    # The bytes carry the resolved surface id (faithful, not canonical).
    assert parsed.pricing.volatility.volSurfaces[0].id == str(surface_id).encode()
    assert len(results) == 1
    assert results[0].npv == pytest.approx(42.0)


async def test_price_batch_propagates_engine_exceptions() -> None:
    engine = _FakeEngine()
    engine.set_exception(NotImplementedError("stub backend"))
    batch: EngineBatch[EquityOptionTrade] = EngineBatch(
        trades=(_trade(),),
        shared_inputs={"equity_surface_id": None},
    )

    with pytest.raises(NotImplementedError, match="stub backend"):
        await price_equity_option_batch(engine, batch, resolved=_resolved())
