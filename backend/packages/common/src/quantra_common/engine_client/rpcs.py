"""Canonical list of pricing-engine RPC names.

Mirrors the ``rpc_service QuantraServer`` block in the engine's
``grpc/quantraserver.fbs``. Values are the exact gRPC method names that
the engine exposes; passing one of these to ``EngineClient.call`` lets
the wire layer construct the fully qualified path
(``quantra.QuantraServer/<value>``) without callers ever assembling
strings themselves.
"""

from __future__ import annotations

from enum import StrEnum


class EngineRpc(StrEnum):
    PRICE_FIXED_RATE_BOND = "PriceFixedRateBond"
    PRICE_FLOATING_RATE_BOND = "PriceFloatingRateBond"
    PRICE_VANILLA_SWAP = "PriceVanillaSwap"
    PRICE_ZERO_COUPON_INFLATION_SWAP = "PriceZeroCouponInflationSwap"
    PRICE_YEAR_ON_YEAR_INFLATION_SWAP = "PriceYearOnYearInflationSwap"
    PRICE_OIS_SWAP = "PriceOisSwap"
    PRICE_BASIS_SWAP = "PriceBasisSwap"
    PRICE_FRA = "PriceFRA"
    PRICE_CAP_FLOOR = "PriceCapFloor"
    PRICE_SWAPTION = "PriceSwaption"
    PRICE_CDS = "PriceCDS"
    BOOTSTRAP_CURVES = "BootstrapCurves"
    BOOTSTRAP_INFLATION_CURVES = "BootstrapInflationCurves"
    SAMPLE_VOL_SURFACES = "SampleVolSurfaces"
    CALENDAR_BUSINESS_DAYS = "CalendarBusinessDays"
    CALENDAR_HOLIDAYS = "CalendarHolidays"
    CALENDAR_ADVANCE = "CalendarAdvance"
    CALIBRATE_SWAPTION_MODEL = "CalibrateSwaptionModel"
    CALIBRATE_SWAPTION_VOL = "CalibrateSwaptionVol"
    PRICE_EQUITY_OPTION = "PriceEquityOption"
