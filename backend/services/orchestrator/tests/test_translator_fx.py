"""FX market data + FX-implied curve helpers in the shared translator (engine >= 0.9.0).

Pins:

* ``FxSwapHelper`` / ``CrossCcyBasisHelper`` curve points translate into the
  FB ``Point`` union with every engine-required field, the value read from the
  kind-specific inline field (``fx_points`` / ``spread``) or substituted from a
  resolved ``quote_id`` (invariant #8: no ``quoteId`` on the wire).
* ``Pricing.fx.spots[]`` is emitted from ``ResolvedMarketData.fx_spots`` with
  the MD quote substituted, and is ABSENT when no spot is resolved (every
  pre-FX product's wire unchanged — the golden-hex suites prove the bytes).
* Explicit ``ResolvedMarketData.indices`` register verbatim and beat the
  catalogs; the basis helper's two index refs are collected for registration.
* Engine-rejection pre-flight: pair / rate-xor-quote / calendars / convention
  surface as :class:`FxSpotTranslationError`; a basis helper with both or
  neither discount curve as :class:`CurveTranslationError`.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import flatbuffers
import pytest

from quantra_common.engine_client._generated.quantra.CrossCcyBasisHelper import (
    CrossCcyBasisHelper,
)
from quantra_common.engine_client._generated.quantra.enums.BusinessDayConvention import (
    BusinessDayConvention,
)
from quantra_common.engine_client._generated.quantra.enums.Calendar import Calendar
from quantra_common.engine_client._generated.quantra.enums.Frequency import Frequency
from quantra_common.engine_client._generated.quantra.enums.TimeUnit import TimeUnit
from quantra_common.engine_client._generated.quantra.FxSwapHelper import FxSwapHelper
from quantra_common.engine_client._generated.quantra.Point import Point
from quantra_common.engine_client._generated.quantra.Pricing import Pricing, PricingT
from quantra_orchestrator.pricing._translator import (
    CurveTranslationError,
    FxSpotTranslationError,
    QuoteResolutionError,
    ResolvedMarketData,
    _collect_helper_index_ids,
    build_pricing_from_resolved,
    translate_fx_spot,
)
from quantra_orchestrator.pricing.fx_forward.models import ResolvedFxSpot
from quantra_orchestrator.pricing.swap_ir.models import (
    ResolvedCurve,
    ResolvedIndex,
    ResolvedQuoteValue,
)

AS_OF = "2025-01-15"


def _ois(rate: float, n: int, unit: str) -> dict[str, Any]:
    return {
        "point_type": "OISHelper",
        "point": {
            "rate": rate,
            "settlement_days": 2,
            "overnight_index": {"id": "USD_SOFR"},
            "tenor": {"n": n, "unit": unit},
            "calendar": "UnitedStatesGovernmentBond",
            "fixed_leg_convention": "ModifiedFollowing",
            "fixed_leg_frequency": "Annual",
            "payment_lag": 2,
            "averaging_method": "Compound",
            "lookback_days": 0,
            "lockout_days": 0,
            "apply_observation_shift": False,
        },
    }


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


def _fx_swap(
    points: float | None, n: int, unit: str, *, quote_id: str | None = None
) -> dict[str, Any]:
    inner: dict[str, Any] = {
        "tenor": {"n": n, "unit": unit},
        "fx_spot": "EURUSD_SPOT",
        "end_of_month": False,
        "collateral_currency": "USD",
        "collateral_curve": {"id": "USD_SOFR_OIS"},
    }
    if points is not None:
        inner["fx_points"] = points
    if quote_id is not None:
        inner["quote_id"] = quote_id
    return {"point_type": "FxSwapHelper", "point": inner}


def _xccy(spread: float, **overrides: Any) -> dict[str, Any]:
    inner: dict[str, Any] = {
        "spread": spread,
        "tenor": {"n": 5, "unit": "Years"},
        "fx_spot": "EURUSD_SPOT",
        "base_index": {"id": "EUR_ESTR"},
        "base_forwarding_curve": {"id": "EUR_ESTR_OIS"},
        "quote_index": {"id": "USD_SOFR"},
        "quote_forwarding_curve": {"id": "USD_SOFR_OIS"},
        "quote_discount_curve": {"id": "USD_SOFR_OIS"},
        "spread_on_base_leg": True,
        "payment_frequency": "Quarterly",
        "payment_lag": 0,
        "calendar": "TARGET",
        "business_day_convention": "ModifiedFollowing",
        "end_of_month": False,
        "mtm_reset": False,
    }
    inner.update(overrides)
    return {"point_type": "CrossCcyBasisHelper", "point": inner}


def _curve(name: str, points: list[dict[str, Any]]) -> ResolvedCurve:
    return ResolvedCurve(
        name=name,
        day_counter="Actual365Fixed",
        reference_date=date(2025, 1, 15),
        points=points,
        body={"interpolator": "LogLinear", "bootstrap_trait": "Discount"},
    )


def _index(name: str, currency: str, calendar: str) -> ResolvedIndex:
    return ResolvedIndex(
        name=name,
        kind="Overnight",
        currency=currency,
        calendar=calendar,
        day_counter="Actual360",
        body={
            "name": name.rsplit("_", maxsplit=1)[-1],
            "tenor": {"n": 0, "unit": "Days"},
            "fixing_days": 0,
            "business_day_convention": "ModifiedFollowing",
            "end_of_month": True,
        },
    )


def _spot(**overrides: Any) -> ResolvedFxSpot:
    base: dict[str, Any] = {
        "name": "EURUSD_SPOT",
        "base_currency": "EUR",
        "quote_currency": "USD",
        "rate": 1.085,
        "spot_days": 2,
        "settlement_calendars": ["TARGET", "UnitedStatesSettlement"],
        "business_day_convention": "ModifiedFollowing",
    }
    base.update(overrides)
    return ResolvedFxSpot(**base)


def _resolved(
    curves: list[ResolvedCurve],
    *,
    fx_spots: tuple[ResolvedFxSpot, ...] = (),
    indices: tuple[ResolvedIndex, ...] = (),
    quotes: tuple[ResolvedQuoteValue, ...] = (),
) -> ResolvedMarketData:
    return ResolvedMarketData(
        as_of=AS_OF, curves=tuple(curves), quotes=quotes, indices=indices, fx_spots=fx_spots
    )


def _pack(pricing: PricingT) -> Pricing:
    builder = flatbuffers.Builder(4096)
    builder.ForceDefaults(True)
    builder.Finish(pricing.Pack(builder))
    return Pricing.GetRootAs(bytes(builder.Output()), 0)


def _curve_by_id(pricing: Pricing, curve_id: str) -> Any:
    rates = pricing.Rates()
    assert rates is not None
    for i in range(rates.CurvesLength()):
        ts = rates.Curves(i)
        if ts is not None and ts.Id() == curve_id.encode():
            return ts
    raise AssertionError(f"curve {curve_id} not on the wire")


def _collateral_market() -> ResolvedMarketData:
    usd = _curve("USD_SOFR_OIS", [_ois(0.0533, 1, "Months"), _ois(0.0495, 1, "Years")])
    eur = _curve(
        "EUR_USD_COLLATERAL",
        [_fx_swap(0.00052, 1, "Weeks"), _fx_swap(0.0131, 6, "Months"), _xccy(-0.0017)],
    )
    return _resolved(
        [eur, usd],
        fx_spots=(_spot(),),
        indices=(
            _index("EUR_ESTR", "EUR", "TARGET"),
            _index("USD_SOFR", "USD", "UnitedStatesGovernmentBond"),
        ),
    )


def test_fx_helpers_translate_with_every_engine_field() -> None:
    pricing = _pack(build_pricing_from_resolved(_collateral_market()))
    ts = _curve_by_id(pricing, "EUR_USD_COLLATERAL")
    assert ts.PointsLength() == 3

    fx_point = ts.Points(0)
    assert fx_point.PointType() == Point.FxSwapHelper
    helper = FxSwapHelper()
    helper.Init(fx_point.Point().Bytes, fx_point.Point().Pos)
    assert helper.FxPoints() == pytest.approx(0.00052)
    assert helper.QuoteId() is None
    assert helper.FxSpot() == b"EURUSD_SPOT"
    assert helper.EndOfMonth() is False
    assert helper.CollateralCurrency() == b"USD"
    assert helper.CollateralCurve().Id() == b"USD_SOFR_OIS"
    assert (helper.Tenor().N(), helper.Tenor().Unit()) == (1, TimeUnit.Weeks)

    basis_point = ts.Points(2)
    assert basis_point.PointType() == Point.CrossCcyBasisHelper
    basis = CrossCcyBasisHelper()
    basis.Init(basis_point.Point().Bytes, basis_point.Point().Pos)
    assert basis.Spread() == pytest.approx(-0.0017)
    assert basis.QuoteId() is None
    assert basis.BaseIndex().Id() == b"EUR_ESTR"
    assert basis.QuoteIndex().Id() == b"USD_SOFR"
    assert basis.BaseForwardingCurve().Id() == b"EUR_ESTR_OIS"
    assert basis.QuoteForwardingCurve().Id() == b"USD_SOFR_OIS"
    assert basis.BaseDiscountCurve() is None
    assert basis.QuoteDiscountCurve().Id() == b"USD_SOFR_OIS"
    assert basis.SpreadOnBaseLeg() is True
    assert basis.PaymentFrequency() == Frequency.Quarterly
    assert basis.PaymentLag() == 0
    assert basis.Calendar() == Calendar.TARGET
    assert basis.BusinessDayConvention() == BusinessDayConvention.ModifiedFollowing
    assert basis.EndOfMonth() is False
    assert basis.MtmReset() is False


def test_pricing_fx_spots_emitted_and_absent_without_spots() -> None:
    with_fx = _pack(build_pricing_from_resolved(_collateral_market()))
    fx = with_fx.Fx()
    assert fx is not None
    assert fx.SpotsLength() == 1
    spot = fx.Spots(0)
    assert spot.Id() == b"EURUSD_SPOT"
    assert spot.BaseCurrency() == b"EUR"
    assert spot.QuoteCurrency() == b"USD"
    assert spot.Rate() == pytest.approx(1.085)
    assert spot.QuoteId() is None
    assert spot.SpotDays() == 2
    assert [spot.SettlementCalendars(i) for i in range(spot.SettlementCalendarsLength())] == [
        Calendar.TARGET,
        Calendar.UnitedStatesSettlement,
    ]
    assert spot.BusinessDayConvention() == BusinessDayConvention.ModifiedFollowing

    plain = _pack(build_pricing_from_resolved(_resolved([_curve("USD", [_deposit(0.05)])])))
    assert plain.Fx() is None


def test_explicit_indices_register_verbatim_and_basis_refs_are_collected() -> None:
    market = _collateral_market()
    assert _collect_helper_index_ids(market.curves) == ["EUR_ESTR", "USD_SOFR"]
    pricing = _pack(build_pricing_from_resolved(market))
    rates = pricing.Rates()
    assert rates is not None
    by_id = {rates.Indices(i).Id(): rates.Indices(i) for i in range(rates.IndicesLength())}
    assert {b"EUR_ESTR", b"USD_SOFR"} <= set(by_id)
    sofr = by_id[b"USD_SOFR"]
    # The explicit def wins over the catalog guess (SOFR calendar in the catalog
    # is UnitedStates; the explicit one carries the government-bond calendar).
    assert sofr.Calendar() == Calendar.UnitedStatesGovernmentBond
    assert sofr.Currency() == b"USD"
    assert sofr.EndOfMonth() is True
    assert sofr.FixingDays() == 0


def test_fx_quote_ids_substituted_and_missing_batched() -> None:
    usd = _curve("USD_SOFR_OIS", [_deposit(0.05)])
    eur = _curve("EUR_USD_COLLATERAL", [_fx_swap(None, 6, "Months", quote_id="EUR.FX.PTS.6M")])
    spot = _spot(rate=None, quote_id="EUR.FX.ECB.EURUSD.1D.SPOT")
    quotes = (
        ResolvedQuoteValue(canonical_id="EUR.FX.PTS.6M", as_of=date(2025, 1, 15), value=0.0131),
        ResolvedQuoteValue(
            canonical_id="EUR.FX.ECB.EURUSD.1D.SPOT", as_of=date(2025, 1, 15), value=1.085
        ),
    )
    indices = (_index("USD_SOFR", "USD", "UnitedStatesGovernmentBond"),)
    pricing = _pack(
        build_pricing_from_resolved(
            _resolved([eur, usd], fx_spots=(spot,), indices=indices, quotes=quotes)
        )
    )
    assert pricing.Fx().Spots(0).Rate() == pytest.approx(1.085)
    assert pricing.Fx().Spots(0).QuoteId() is None
    ts = _curve_by_id(pricing, "EUR_USD_COLLATERAL")
    helper = FxSwapHelper()
    helper.Init(ts.Points(0).Point().Bytes, ts.Points(0).Point().Pos)
    assert helper.FxPoints() == pytest.approx(0.0131)
    assert helper.QuoteId() is None

    with pytest.raises(QuoteResolutionError) as exc_info:
        build_pricing_from_resolved(_resolved([eur, usd], fx_spots=(spot,), indices=indices))
    assert exc_info.value.missing_canonical_ids == [
        "EUR.FX.PTS.6M",
        "EUR.FX.ECB.EURUSD.1D.SPOT",
    ]


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"quote_currency": "EUR"}, "quote_currency"),
        ({"base_currency": "EURUSD"}, "base_currency"),
        ({"rate": None, "quote_id": None}, "rate"),
        ({"rate": 1.1, "quote_id": "X"}, "rate"),
        ({"rate": -1.0}, "rate"),
        ({"spot_days": -1}, "spot_days"),
        ({"settlement_calendars": []}, "settlement_calendars"),
        ({"settlement_calendars": ["Mars"]}, "settlement_calendars"),
        ({"business_day_convention": "Sideways"}, "business_day_convention"),
    ],
)
def test_fx_spot_rejections_name_the_field(overrides: dict[str, Any], field: str) -> None:
    with pytest.raises(FxSpotTranslationError) as exc_info:
        translate_fx_spot(_spot(**overrides), {}, missing_quotes=[])
    assert exc_info.value.details is not None
    assert exc_info.value.details[0]["field"] == field


def test_basis_helper_requires_exactly_one_discount_curve() -> None:
    usd = _curve("USD_SOFR_OIS", [_deposit(0.05)])
    both = _curve("EUR", [_xccy(-0.001, base_discount_curve={"id": "EUR_ESTR_OIS"})])
    neither = _curve("EUR2", [_xccy(-0.001, quote_discount_curve=None)])
    indices = (
        _index("EUR_ESTR", "EUR", "TARGET"),
        _index("USD_SOFR", "USD", "UnitedStatesGovernmentBond"),
    )
    for bad in (both, neither):
        with pytest.raises(CurveTranslationError, match="exactly one of"):
            build_pricing_from_resolved(_resolved([bad, usd], fx_spots=(_spot(),), indices=indices))


def test_fx_swap_helper_without_value_source_rejects_naming_fx_points() -> None:
    usd = _curve("USD_SOFR_OIS", [_deposit(0.05)])
    bad = _curve("EUR", [_fx_swap(None, 6, "Months")])
    with pytest.raises(CurveTranslationError, match="fx_points"):
        build_pricing_from_resolved(_resolved([bad, usd], fx_spots=(_spot(),)))
