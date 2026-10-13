"""Unit tests for ``pricing/fx_forward/engine_io.py``.

* ``FX_FORWARD_ENGINE_RPC`` is the engine's ``PriceFxForward`` RPC.
* ``build_fx_forward_request`` encodes a parseable ``PriceFxForwardRequest``
  whose ``Pricing`` carries the curves + ``fx.spots`` and whose
  ``fx_forwards[0]`` carries the trade mapped field-for-field (engine
  ``docs/http-api.md`` § "FX forward" / Request), curve / spot refs resolved
  to the engine-facing ids (UUID text or inline name), the direction enum
  and the reporting-currency default.
* Typed 422s: missing trade fields, a curve / spot ref that names nothing,
  a third reporting currency, an ambiguous spot.
* ``decode_fx_forward_response`` round-trips a synthesised response and
  rejects empty / malformed / count-mismatched bodies.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import flatbuffers
import pytest

from quantra_common.engine_client import EngineRpc
from quantra_common.engine_client._generated.quantra.enums.FxDirection import FxDirection
from quantra_common.engine_client._generated.quantra.FxForwardResponse import (
    FxForwardResponseT,
)
from quantra_common.engine_client._generated.quantra.PriceFxForwardRequest import (
    PriceFxForwardRequest,
)
from quantra_common.engine_client._generated.quantra.PriceFxForwardResponse import (
    PriceFxForwardResponseT,
)
from quantra_orchestrator.pricing._translator import ResolvedMarketData
from quantra_orchestrator.pricing.fx_forward.engine_io import (
    FX_FORWARD_ENGINE_RPC,
    build_fx_forward_request,
    decode_fx_forward_request_wire,
    decode_fx_forward_response,
)
from quantra_orchestrator.pricing.fx_forward.errors import (
    FxForwardCurveResolutionFailedError,
    FxForwardFxSpotResolutionFailedError,
    FxForwardMissingTradeFieldsError,
)
from quantra_orchestrator.pricing.fx_forward.models import (
    FxForwardTrade,
    ResolvedCurve,
    ResolvedFxSpot,
)

EUR_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USD_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
SPOT_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


def _deposit(rate: float) -> dict[str, Any]:
    return {
        "point_type": "DepositHelper",
        "point": {
            "tenor": {"n": 1, "unit": "Years"},
            "rate": rate,
            "fixing_days": 2,
            "calendar": "TARGET",
            "business_day_convention": "ModifiedFollowing",
            "day_counter": "Actual360",
        },
    }


def _curve(curve_id: uuid.UUID | None, name: str, rate: float) -> ResolvedCurve:
    return ResolvedCurve(
        id=curve_id,
        name=name,
        reference_date=date(2025, 1, 15),
        points=[_deposit(rate)],
        body={"interpolator": "LogLinear", "bootstrap_trait": "Discount"},
    )


def _spot(spot_id: uuid.UUID | None = SPOT_ID, name: str = "EURUSD") -> ResolvedFxSpot:
    return ResolvedFxSpot(
        id=spot_id,
        name=name,
        base_currency="EUR",
        quote_currency="USD",
        rate=1.085,
        spot_days=2,
        settlement_calendars=["TARGET", "UnitedStatesSettlement"],
        business_day_convention="ModifiedFollowing",
    )


def _market(*, spots: tuple[ResolvedFxSpot, ...] = (_spot(),)) -> ResolvedMarketData:
    return ResolvedMarketData(
        as_of="2025-01-15",
        curves=(_curve(EUR_ID, "EUR-DISC", 0.03), _curve(USD_ID, "USD-DISC", 0.05)),
        quotes=(),
        fx_spots=spots,
    )


def _trade(**overrides: Any) -> FxForwardTrade:
    body: dict[str, Any] = {
        "base_currency": "EUR",
        "quote_currency": "USD",
        "direction": "BuyBase",
        "base_notional": 10_000_000.0,
        "forward_rate": 1.095,
        "maturity_date": "2025-07-17",
        "fx_spot": str(SPOT_ID),
        "base_discounting_curve": str(EUR_ID),
        "quote_discounting_curve": "USD-DISC",
        "reporting_currency": "USD",
    }
    body.update(overrides)
    return FxForwardTrade(fx_forward=body)


def test_rpc_is_price_fx_forward() -> None:
    assert FX_FORWARD_ENGINE_RPC is EngineRpc.PRICE_FX_FORWARD
    assert str(FX_FORWARD_ENGINE_RPC) == "PriceFxForward"


def test_build_request_maps_the_trade_field_for_field() -> None:
    raw = build_fx_forward_request([_trade()], resolved=_market())
    request = PriceFxForwardRequest.GetRootAs(raw, 0)
    assert request.FxForwardsLength() == 1
    row = request.FxForwards(0)
    fwd = row.FxForward()
    assert fwd.BaseCurrency() == b"EUR"
    assert fwd.QuoteCurrency() == b"USD"
    assert fwd.BaseNotional() == pytest.approx(10_000_000.0)
    assert fwd.ForwardRate() == pytest.approx(1.095)
    assert fwd.MaturityDate() == b"2025-07-17"
    assert fwd.Direction() == FxDirection.BuyBase
    assert row.FxSpot() == str(SPOT_ID).encode()
    assert row.BaseDiscountingCurve() == str(EUR_ID).encode()
    # A curve named by its inline name resolves to its engine id (the UUID).
    assert row.QuoteDiscountingCurve() == str(USD_ID).encode()
    assert row.ReportingCurrency() == b"USD"
    pricing = request.Pricing()
    assert pricing.Rates().CurvesLength() == 2
    assert pricing.Fx().SpotsLength() == 1
    assert pricing.Fx().Spots(0).Id() == str(SPOT_ID).encode()
    # The wire decoder (trace) reads the same bytes back.
    decoded = decode_fx_forward_request_wire(raw)
    assert decoded["fx_forwards"][0]["fx_forward"]["direction"] == FxDirection.BuyBase


def test_build_request_defaults_reporting_to_quote_and_spot_when_single() -> None:
    body = _trade().fx_forward
    body.pop("reporting_currency")
    body.pop("fx_spot")
    body["direction"] = "SellBase"
    body["notional"] = body.pop("base_notional")
    raw = build_fx_forward_request([FxForwardTrade(fx_forward=body)], resolved=_market())
    row = PriceFxForwardRequest.GetRootAs(raw, 0).FxForwards(0)
    assert row.ReportingCurrency() == b"USD"
    assert row.FxSpot() == str(SPOT_ID).encode()
    assert row.FxForward().Direction() == FxDirection.SellBase
    assert row.FxForward().BaseNotional() == pytest.approx(10_000_000.0)


def test_build_request_missing_fields_422() -> None:
    with pytest.raises(FxForwardMissingTradeFieldsError) as exc_info:
        build_fx_forward_request([FxForwardTrade(fx_forward={})], resolved=_market())
    missing = {d["field"] for d in exc_info.value.details}
    assert {"base_currency", "forward_rate", "maturity_date", "base_notional"} <= missing


@pytest.mark.parametrize(
    "overrides",
    [
        {"direction": "Long"},
        {"forward_rate": -1.0},
        {"maturity_date": "17/07/2025"},
        {"reporting_currency": "GBP"},
    ],
)
def test_build_request_invalid_fields_422(overrides: dict[str, Any]) -> None:
    with pytest.raises(FxForwardMissingTradeFieldsError):
        build_fx_forward_request([_trade(**overrides)], resolved=_market())


def test_build_request_unknown_curve_and_spot_refs_422() -> None:
    with pytest.raises(FxForwardCurveResolutionFailedError):
        build_fx_forward_request([_trade(base_discounting_curve="nope")], resolved=_market())
    with pytest.raises(FxForwardFxSpotResolutionFailedError):
        build_fx_forward_request([_trade(fx_spot="nope")], resolved=_market())
    two_spots = _market(spots=(_spot(), _spot(spot_id=None, name="OTHER")))
    body = _trade().fx_forward
    body.pop("fx_spot")
    with pytest.raises(FxForwardMissingTradeFieldsError, match="fx_spot"):
        build_fx_forward_request([FxForwardTrade(fx_forward=body)], resolved=two_spots)


def _response_bytes(rows: list[dict[str, Any]]) -> bytes:
    response = PriceFxForwardResponseT()
    response.fxForwards = []
    for row in rows:
        r = FxForwardResponseT()
        for key, value in row.items():
            setattr(r, key, value)
        response.fxForwards.append(r)
    builder = flatbuffers.Builder(256)
    builder.Finish(response.Pack(builder))
    return bytes(builder.Output())


def test_decode_response_round_trip() -> None:
    raw = _response_bytes(
        [
            {
                "npv": 30201.2,
                "fairForwardRate": 1.0981,
                "forwardPoints": 0.0131,
                "baseLegPv": 9_861_310.0,
                "quoteLegPv": -10_667_900.0,
                "spotDate": "2025-01-17",
                "maturityDate": "2025-07-17",
                "fxSpotUsed": 1.085,
            }
        ]
    )
    [result] = decode_fx_forward_response(raw, expected_count=1, reporting_currencies=["USD"])
    assert result.npv == pytest.approx(30201.2)
    assert result.reporting_currency == "USD"
    assert result.fair_forward_rate == pytest.approx(1.0981)
    assert result.forward_points == pytest.approx(0.0131)
    assert result.base_leg_pv == pytest.approx(9_861_310.0)
    assert result.quote_leg_pv == pytest.approx(-10_667_900.0)
    assert result.fx_spot_used == pytest.approx(1.085)
    assert result.spot_date == "2025-01-17"
    assert result.maturity_date == "2025-07-17"


def test_decode_response_rejects_bad_shapes() -> None:
    with pytest.raises(ValueError, match="empty"):
        decode_fx_forward_response(b"", expected_count=1, reporting_currencies=["USD"])
    with pytest.raises(ValueError, match="malformed"):
        decode_fx_forward_response(b"\x01\x02", expected_count=1, reporting_currencies=["USD"])
    with pytest.raises(ValueError, match="expected 2"):
        decode_fx_forward_response(
            _response_bytes([{"npv": 1.0}]), expected_count=2, reporting_currencies=["USD", "USD"]
        )
