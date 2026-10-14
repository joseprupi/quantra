"""Overnight float index → ``OvernightCoupons`` (swap_ir + swaption underlying).

Engine >= 0.10.0 rejects ``IborCoupons`` on an overnight index ("floating_leg.
coupons is IborCoupons but its index 'SONIA' is an overnight index: use
OvernightCoupons") and accepts ``OvernightCoupons`` only on
``OisSwap.overnight_leg`` (``docs/http-api.md@v0.11.0`` § "Swap legs"). So a
``/v1/price/swap/ir`` swap whose float index is REGISTERED as overnight
(``IndexDef.index_type == Overnight`` — never the name) goes out as an
``OisSwap`` on ``PriceOisSwap``, and a swaption on such an index takes an
``OisSwap`` underlying. IBOR indices keep the ``VanillaSwap`` /
``IborCoupons`` wire byte-for-byte (the existing goldens in
``test_swap_ir_engine_io.py`` / ``test_swaption_engine_io.py``).

The golden hex below pins the overnight wire; the decode-back assertions pin
the mapping (what each byte means), so the golden is not opaque.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date

import flatbuffers
import pytest

from quantra_common.engine_client import EngineClient, EngineRpc
from quantra_common.engine_client._generated.quantra.enums.Calendar import Calendar
from quantra_common.engine_client._generated.quantra.enums.PayReceive import PayReceive
from quantra_common.engine_client._generated.quantra.enums.RateAveragingType import (
    RateAveragingType,
)
from quantra_common.engine_client._generated.quantra.FixedCoupons import FixedCoupons
from quantra_common.engine_client._generated.quantra.IborCoupons import IborCoupons
from quantra_common.engine_client._generated.quantra.IndexType import IndexType
from quantra_common.engine_client._generated.quantra.OisSwap import OisSwap
from quantra_common.engine_client._generated.quantra.OisSwapResponse import OisSwapResponseT
from quantra_common.engine_client._generated.quantra.OvernightCoupons import OvernightCoupons
from quantra_common.engine_client._generated.quantra.PriceOisSwapRequest import (
    PriceOisSwapRequest,
)
from quantra_common.engine_client._generated.quantra.PriceOisSwapResponse import (
    PriceOisSwapResponseT,
)
from quantra_common.engine_client._generated.quantra.PriceSwaptionRequest import (
    PriceSwaptionRequest,
)
from quantra_common.engine_client._generated.quantra.PriceVanillaSwapRequest import (
    PriceVanillaSwapRequest,
)
from quantra_common.engine_client._generated.quantra.SwapCoupons import SwapCoupons
from quantra_common.engine_client._generated.quantra.SwapLeg import SwapLeg
from quantra_common.engine_client._generated.quantra.SwapLegResult import SwapLegResultT
from quantra_common.engine_client._generated.quantra.SwaptionUnderlying import (
    SwaptionUnderlying,
)
from quantra_common.engine_client._generated.quantra.VanillaSwap import VanillaSwap
from quantra_orchestrator.pricing._translator import (
    CurveRole,
    ResolvedMarketData,
    build_pricing_from_resolved,
    forwarding_index_is_overnight,
    resolved_curve_id,
)
from quantra_orchestrator.pricing.concurrency import EngineBatch
from quantra_orchestrator.pricing.swap_ir.engine_io import (
    SWAP_IR_ENGINE_RPC,
    SWAP_IR_OIS_ENGINE_RPC,
    build_swap_ir_request,
    decode_swap_ir_request_wire,
    decode_swap_ir_response,
    price_swap_ir_batch,
    swap_ir_engine_rpc,
)
from quantra_orchestrator.pricing.swap_ir.models import (
    IrSwapTrade,
    ResolvedCurve,
    ResolvedIndex,
)
from quantra_orchestrator.pricing.swaption.engine_io import build_swaption_request
from quantra_orchestrator.pricing.swaption.models import (
    ResolvedSwaptionModel,
    ResolvedVolSurface,
    SwaptionTrade,
)

_DISCOUNT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_FORWARDING_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_VOL_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
_MODEL_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")

# SwapLeg vtable offsets (generated reader): a non-zero table offset means the
# field is physically present on the wire (vs the reader's default for absent).
_LEG_OFFSET_DAY_COUNTER = 14
_LEG_OFFSET_PAYMENT_CALENDAR = 18
_LEG_OFFSET_PAYMENT_LAG = 20
# OvernightCoupons vtable offsets: averaging_method / lookback_days /
# lockout_days / apply_observation_shift (presence-required on the engine).
_ON_OFFSETS_PRESENCE_REQUIRED = (8, 10, 12, 14)


def _deposit_point(rate: float) -> dict[str, object]:
    return {
        "point_type": "DepositHelper",
        "point": {
            "tenor": {"n": 1, "unit": "Years"},
            "rate": rate,
            "fixing_days": 0,
            "calendar": "UnitedKingdom",
            "business_day_convention": "ModifiedFollowing",
            "day_counter": "Actual365Fixed",
        },
    }


def _curve(curve_id: uuid.UUID, name: str, rate: float) -> ResolvedCurve:
    return ResolvedCurve(
        id=curve_id,
        name=name,
        currency="GBP",
        day_counter="Actual365Fixed",
        reference_date=date(2026, 5, 13),
        points=[_deposit_point(rate)],
        body={"interpolator": "LogLinear"},
    )


def _sonia(kind: str = "Overnight", name: str = "SONIA") -> ResolvedIndex:
    """The seeded SONIA index (``scripts/seed_demo_entities.py`` shape), inline."""

    return ResolvedIndex(
        id=None,
        name=name,
        kind=kind,
        currency="GBP",
        calendar="UnitedKingdom",
        day_counter="Actual365Fixed",
        body={
            "id": name,
            "name": name,
            "index_type": kind,
            "currency": "GBP",
            "tenor_number": 0,
            "tenor_time_unit": "Days",
            "fixing_days": 0,
            "calendar": "UnitedKingdom",
            "day_counter": "Actual365Fixed",
        },
    )


def _resolved(index: ResolvedIndex | None, **extra: object) -> ResolvedMarketData:
    discount = _curve(_DISCOUNT_ID, "GBP-DISC", 0.0425)
    forwarding = _curve(_FORWARDING_ID, "GBP-FWD", 0.0430)
    return ResolvedMarketData(
        as_of="2026-05-13",
        curves=(discount, forwarding),
        quotes=(),
        curve_roles={
            CurveRole.DISCOUNT: resolved_curve_id(discount),
            CurveRole.FORWARDING: resolved_curve_id(forwarding),
        },
        index=index,
        **extra,  # type: ignore[arg-type]
    )


def _swap_trade() -> IrSwapTrade:
    return IrSwapTrade(
        swap_id=None,
        name="t-1",
        swap={
            "notional": 1_000_000.0,
            "fixed_rate": 0.035,
            "swap_type": "Payer",
            "effective_date": "2026-06-15",
            "termination_date": "2031-06-15",
        },
    )


def _fixed_coupons(leg: SwapLeg) -> FixedCoupons:
    assert leg.CouponsType() == SwapCoupons.FixedCoupons
    table = leg.Coupons()
    coupons = FixedCoupons()
    coupons.Init(table.Bytes, table.Pos)
    return coupons


def _overnight_coupons(leg: SwapLeg) -> OvernightCoupons:
    assert leg.CouponsType() == SwapCoupons.OvernightCoupons
    table = leg.Coupons()
    coupons = OvernightCoupons()
    coupons.Init(table.Bytes, table.Pos)
    return coupons


def _ibor_coupons(leg: SwapLeg) -> IborCoupons:
    assert leg.CouponsType() == SwapCoupons.IborCoupons
    table = leg.Coupons()
    coupons = IborCoupons()
    coupons.Init(table.Bytes, table.Pos)
    return coupons


def _assert_overnight_coupons(leg: SwapLeg, index_id: bytes) -> None:
    """The engine-0.11.0 ``OvernightCoupons`` contract with QuantLib's OIS defaults."""

    coupons = _overnight_coupons(leg)
    assert coupons.Index().Id() == index_id
    assert coupons.Spread() == pytest.approx(0.0)
    for offset in _ON_OFFSETS_PRESENCE_REQUIRED:
        assert coupons._tab.Offset(offset) != 0, f"OvernightCoupons slot {offset} absent"
    assert coupons.AveragingMethod() == RateAveragingType.Compound
    assert coupons.LookbackDays() == 0
    assert coupons.LockoutDays() == 0
    assert coupons.ApplyObservationShift() is False
    assert coupons.TelescopicValueDates() is False
    # Not read by the engine with OvernightCoupons (the index's day count
    # accrues): the orchestrator does not send one.
    assert leg._tab.Offset(_LEG_OFFSET_DAY_COUNTER) == 0


# ---------------------------------------------------------------------------
# The predicate: index_type decides, never the name
# ---------------------------------------------------------------------------


def test_predicate_reads_the_registered_index_type() -> None:
    assert forwarding_index_is_overnight(_resolved(_sonia())) is True
    assert forwarding_index_is_overnight(_resolved(_sonia(kind="OvernightIndex"))) is True
    # The name alone does not make an index overnight ...
    assert forwarding_index_is_overnight(_resolved(_sonia(kind="IborIndex"))) is False
    # ... and an overnight index under any name is overnight.
    assert forwarding_index_is_overnight(_resolved(_sonia(name="MY_ON_RATE"))) is True
    # No resolved index = the interim default Euribor 6M forwarding index (IBOR).
    assert forwarding_index_is_overnight(_resolved(None)) is False


def test_predicate_agrees_with_the_registered_index_def() -> None:
    """The predicate and ``rates.indices`` read the same ``IndexDef``."""

    resolved = _resolved(_sonia())
    pricing = build_pricing_from_resolved(resolved)
    registered = {idx.id: idx for idx in pricing.rates.indices}
    assert registered["SONIA"].indexType == IndexType.Overnight


# ---------------------------------------------------------------------------
# swap_ir
# ---------------------------------------------------------------------------

_SWAP_IR_SONIA_GOLDEN_HEX = (
    "140000000000000000000a0010000c00080007000a000000000000010800000054020000010000000400000006"
    "ffffff0c00000008000e000800040008000000280000005801000000001e00340033002c00200000001c000000"
    "1b001a00140013000c00080004001e000000300000005800000090000000000000030000000000002002bc0000"
    "000000000080842e4100000000e4000000000000012400000032323232323232322d323232322d323232322d32"
    "3232322d323232323232323232323232000000002400000031313131313131312d313131312d313131312d3131"
    "31312d3131313131313131313131310000120022001c00140013000c0008000700060012000000000000000000"
    "0000000000000000000000000000000000000c0000000000060008000400060000000400000005000000534f4e"
    "494100000038ffffff000000000202020b0c00000018000000000000200a000000323033312d30362d31350000"
    "0a000000323032362d30362d3135000003000000474250001c0030002f0028001c000000180017001600150010"
    "000f00080004001c0000002c0000005800000000000001000000000020020e680000000000000080842e410000"
    "000090000000000000002400000031313131313131312d313131312d313131312d313131312d31313131313131"
    "3131313131000006000c00040006000000ec51b81e85eba13f14001800170010000c000b000a00090008000700"
    "1400000000000000020202000c00000018000000000000200a000000323033312d30362d313500000a00000032"
    "3032362d30362d31350000030000004742500000001600140010000c0000000800000000000000000004001600"
    "00001c0000002c00000048020000540200000c000a0009000800070006000c0000000000000000000a000c0008"
    "00000004000a000000080000004801000002000000a00000000400000078ffffff100000001c00000000000401"
    "4c0000000a000000323032362d30352d31330000010000000400000068ffffff080000000000000160ffffff00"
    "010224000000000c0000006abc74931804a63f80feffff00000008010000002400000032323232323232322d32"
    "3232322d323232322d323232322d323232323232323232323232000000001000140010000f000e000d00080004"
    "0010000000100000001c00000000000401640000000a000000323032362d30352d31330000010000000c000000"
    "08000c000b0004000800000018000000000000011000180010000c000800070006000500100000000001022400"
    "0000000c000000c3f5285c8fc2a53f30ffffff00000008010000002400000031313131313131312d313131312d"
    "313131312d313131312d31313131313131313131313100000000020000006c00000004000000b4ffffff1c0000"
    "00000102240000000018000000000000011c0000002400000003000000474250009cffffff0000000000000000"
    "05000000534f4e494100000005000000534f4e4941000000180020001c001800170010000c000b000a00090008"
    "000400180000001c00000000000220020000002000000000000000240000002c00000003000000455552000800"
    "0c00080007000800000000000005060000000700000045757269626f720010000000666f7277617264696e675f"
    "696e646578000000000a000000323032362d30352d313300000a000000323032362d30352d31330000"
)


def test_swap_ir_sonia_goes_out_as_an_ois_swap_golden() -> None:
    resolved = _resolved(_sonia())
    assert swap_ir_engine_rpc(resolved) is SWAP_IR_OIS_ENGINE_RPC
    assert SWAP_IR_OIS_ENGINE_RPC is EngineRpc.PRICE_OIS_SWAP

    raw = build_swap_ir_request([_swap_trade()], resolved=resolved)
    assert raw.hex() == _SWAP_IR_SONIA_GOLDEN_HEX

    request = PriceOisSwapRequest.GetRootAs(raw, 0)
    assert request.IncludeFlows() is True
    assert request.SwapsLength() == 1
    ois = request.Swaps(0).OisSwap()
    fixed_leg, overnight_leg = ois.FixedLeg(), ois.OvernightLeg()
    assert fixed_leg.PayReceive() == PayReceive.Pay
    assert overnight_leg.PayReceive() == PayReceive.Receive
    assert fixed_leg.Currency() == b"GBP"
    assert overnight_leg.Currency() == b"GBP"
    assert fixed_leg.Notional() == pytest.approx(1_000_000.0)
    assert overnight_leg.Notional() == pytest.approx(1_000_000.0)
    assert _fixed_coupons(fixed_leg).Rate() == pytest.approx(0.035)
    _assert_overnight_coupons(overnight_leg, b"SONIA")
    # payment_calendar / payment_lag: presence-required on /price-ois-swap;
    # the schedule calendar and no lag (what the vanilla wire's absent meant).
    for leg in (fixed_leg, overnight_leg):
        assert leg._tab.Offset(_LEG_OFFSET_PAYMENT_CALENDAR) != 0
        assert leg._tab.Offset(_LEG_OFFSET_PAYMENT_LAG) != 0
        assert leg.PaymentCalendar() == Calendar.TARGET
        assert leg.PaymentLag() == 0
        assert leg.Schedule().Calendar() == Calendar.TARGET
        assert leg.DiscountingCurve() == str(_DISCOUNT_ID).encode()
    assert overnight_leg.ForwardingCurve() == str(_FORWARDING_ID).encode()
    assert fixed_leg.ForwardingCurve() is None


def test_swap_ir_ibor_index_keeps_the_vanilla_wire() -> None:
    """An IBOR-typed index named "SONIA" is still IborCoupons on a VanillaSwap."""

    resolved = _resolved(_sonia(kind="IborIndex"))
    assert swap_ir_engine_rpc(resolved) is SWAP_IR_ENGINE_RPC
    raw = build_swap_ir_request([_swap_trade()], resolved=resolved)
    request = PriceVanillaSwapRequest.GetRootAs(raw, 0)
    vs = request.Swaps(0).VanillaSwap()
    assert _ibor_coupons(vs.FloatingLeg()).Index().Id() == b"SONIA"
    assert vs.FloatingLeg()._tab.Offset(_LEG_OFFSET_PAYMENT_LAG) == 0


def test_swap_ir_wire_decoder_names_the_ois_root() -> None:
    """The trace's wire view decodes OIS bytes under the ``PriceOisSwap`` names."""

    ois = decode_swap_ir_request_wire(
        build_swap_ir_request([_swap_trade()], resolved=_resolved(_sonia()))
    )
    entry = ois["swaps"][0]
    assert "ois_swap" in entry
    assert "overnight_leg" in entry["ois_swap"]

    vanilla = decode_swap_ir_request_wire(
        build_swap_ir_request([_swap_trade()], resolved=_resolved(None))
    )
    assert "vanilla_swap" in vanilla["swaps"][0]


def _ois_response_bytes(npv: float) -> bytes:
    row = OisSwapResponseT()
    row.npv = npv
    row.fairRate = 0.041
    row.fairSpread = -0.006
    legs = []
    for leg_npv in (-npv, 2 * npv):
        leg = SwapLegResultT()
        leg.npv = leg_npv
        leg.npvReporting = leg_npv
        leg.flows = []
        legs.append(leg)
    row.legs = legs
    response = PriceOisSwapResponseT()
    response.swaps = [row]
    builder = flatbuffers.Builder(256)
    builder.Finish(response.Pack(builder))
    return bytes(builder.Output())


def test_decode_ois_response() -> None:
    [result] = decode_swap_ir_response(_ois_response_bytes(123.5), expected_count=1, overnight=True)
    assert result.npv == pytest.approx(123.5)
    assert result.extras == {"fair_rate": pytest.approx(0.041), "fair_spread": -0.006}
    assert result.leg_npvs == [
        {"role": "fixed", "npv": pytest.approx(-123.5)},
        {"role": "floating", "npv": pytest.approx(247.0)},
    ]


class _FakeEngine(EngineClient):
    def __init__(self, response: bytes) -> None:
        self.calls: list[tuple[EngineRpc, bytes]] = []
        self._response = response

    async def call(self, rpc: EngineRpc, request_bytes: bytes) -> bytes:
        self.calls.append((rpc, request_bytes))
        return self._response

    async def close(self) -> None:
        return None


async def test_price_batch_sends_sonia_on_price_ois_swap() -> None:
    engine = _FakeEngine(_ois_response_bytes(42.0))
    batch: EngineBatch[IrSwapTrade] = EngineBatch(trades=(_swap_trade(),), shared_inputs={})
    results: Sequence[object] = await price_swap_ir_batch(
        engine, batch, resolved=_resolved(_sonia())
    )
    assert [rpc for rpc, _ in engine.calls] == [EngineRpc.PRICE_OIS_SWAP]
    assert results[0].npv == pytest.approx(42.0)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# swaption underlying
# ---------------------------------------------------------------------------


def _swaption_resolved(index: ResolvedIndex | None) -> ResolvedMarketData:
    vol = ResolvedVolSurface(
        id=_VOL_ID,
        name="GBP-VOL",
        kind="SwaptionVolSpec",
        payload={"swap_index_id": "EUR_SWAP_6M", "base": {"constant_vol": 0.2}},
    )
    model = ResolvedSwaptionModel(
        id=_MODEL_ID,
        name="HW-LATTICE",
        kind="HullWhiteLattice",
        payload={"hw_a": 0.05, "hw_sigma": 0.01},
    )
    return _resolved(index, vol_surfaces=(vol,), models=(model,))


def _swaption_trade() -> SwaptionTrade:
    return SwaptionTrade(
        swaption_id=None,
        name="s-1",
        swaption={
            "notional": 1_000_000.0,
            "strike": 0.035,
            "exercise_date": "2027-05-13",
            "effective_date": "2027-05-17",
            "termination_date": "2032-05-17",
        },
    )


_SWAPTION_SONIA_GOLDEN_HEX = (
    "140000000000000000000a0010000c00080007000a0000000000000008000000dc020000010000001000000000"
    "000a0010000c00080004000a0000000c000000340000006c0000002400000034343434343434342d343434342d"
    "343434342d343434342d343434343434343434343434000000002400000033333333333333332d333333332d33"
    "3333332d333333332d3333333333333333333333330000120014001300120011000c0000000b00040012000000"
    "1800000000000002180200000000000008000e000800040008000000280000004801000000001e002c002b0024"
    "00180000001400000013000000000012000c00080004001e00000028000000500000008800000000000302b400"
    "00000000000080842e4100000000dc000000000000012400000032323232323232322d323232322d323232322d"
    "323232322d323232323232323232323232000000002400000031313131313131312d313131312d313131312d31"
    "3131312d3131313131313131313131310000120020001c00140013000c00080007000600120000000000000000"
    "0000000000000000000000000000000000000004000000c2fdffff0400000005000000534f4e494100000040ff"
    "ffff000000000202020b0c00000018000000000000200a000000323033322d30352d313700000a000000323032"
    "372d30352d3137000003000000474250001c002800270020001400000010000f000e00000000000d0008000400"
    "1c00000024000000500000000001020e680000000000000080842e410000000090000000000000002400000031"
    "313131313131312d313131312d313131312d313131312d313131313131313131313131000006000c0004000600"
    "0000ec51b81e85eba13f14001800170010000c000b000a000900080007001400000000000000020202000c0000"
    "0018000000000000200a000000323033322d30352d313700000a000000323032372d30352d3137000003000000"
    "474250000a000000323032372d30352d313300000000160018001400100000000c000000080000000000040016"
    "000000200000002c0000007801000094030000a00300000c00080007000600050004000c000000000100000800"
    "0c000800040008000000080000007c000000010000000400000076ffffff1c000000000000023800000000000e"
    "002400230014000c00080007000e00000000000000320000007b14ae47e17a843f9a9999999999a93f00000000"
    "000000032400000034343434343434342d343434342d343434342d343434342d34343434343434343434343400"
    "0000000100000004000000eeffffff18000000000000028400000000000a0012000c000b0004000a0000001400"
    "0000000000015800000000000600080004000600000018000000140020001c001b001a001900180017000c0004"
    "00140000009a9999999999c93f00000000000000000000000100010220040000000a000000323032362d30352d"
    "313300000b0000004555525f535741505f364d002400000033333333333333332d333333332d333333332d3333"
    "33332d33333333333333333333333300000a000c000800000004000a000000080000004801000002000000a000"
    "00000400000078ffffff100000001c000000000004014c0000000a000000323032362d30352d31330000010000"
    "000400000068ffffff080000000000000160ffffff00010224000000000c0000006abc74931804a63f80feffff"
    "00000008010000002400000032323232323232322d323232322d323232322d323232322d323232323232323232"
    "323232000000001000140010000f000e000d000800040010000000100000001c00000000000401640000000a00"
    "0000323032362d30352d31330000010000000c00000008000c000b000400080000001800000000000001100018"
    "0010000c0008000700060005001000000000010224000000000c000000c3f5285c8fc2a53f30ffffff00000008"
    "010000002400000031313131313131312d313131312d313131312d313131312d31313131313131313131313100"
    "000000020000006c00000004000000b4ffffff1c000000000102240000000018000000000000011c0000002400"
    "000003000000474250009cffffff000000000000000005000000534f4e494100000005000000534f4e49410000"
    "00180020001c001800170010000c000b000a00090008000400180000001c000000000002200200000020000000"
    "00000000240000002c000000030000004555520008000c00080007000800000000000005060000000700000045"
    "757269626f720010000000666f7277617264696e675f696e646578000000000a000000323032362d30352d3133"
    "00000a000000323032362d30352d31330000"
)


def test_swaption_on_sonia_takes_an_ois_underlying_golden() -> None:
    raw = build_swaption_request([_swaption_trade()], resolved=_swaption_resolved(_sonia()))
    assert raw.hex() == _SWAPTION_SONIA_GOLDEN_HEX

    swaption = PriceSwaptionRequest.GetRootAs(raw, 0).Swaptions(0).Swaption()
    assert swaption.UnderlyingType() == SwaptionUnderlying.OisSwap
    table = swaption.Underlying()
    ois = OisSwap()
    ois.Init(table.Bytes, table.Pos)
    fixed_leg, overnight_leg = ois.FixedLeg(), ois.OvernightLeg()
    assert fixed_leg.PayReceive() == PayReceive.Pay
    assert overnight_leg.PayReceive() == PayReceive.Receive
    assert overnight_leg.Currency() == b"GBP"
    assert _fixed_coupons(fixed_leg).Rate() == pytest.approx(0.035)
    _assert_overnight_coupons(overnight_leg, b"SONIA")
    # The swaption underlying needs no payment calendar / lag (absent = the
    # schedule calendar, no lag), as on the vanilla underlying.
    for leg in (fixed_leg, overnight_leg):
        assert leg._tab.Offset(_LEG_OFFSET_PAYMENT_CALENDAR) == 0
        assert leg._tab.Offset(_LEG_OFFSET_PAYMENT_LAG) == 0
        assert leg.DiscountingCurve() == str(_DISCOUNT_ID).encode()
    assert overnight_leg.ForwardingCurve() == str(_FORWARDING_ID).encode()


def test_swaption_on_ibor_keeps_the_vanilla_underlying() -> None:
    raw = build_swaption_request(
        [_swaption_trade()], resolved=_swaption_resolved(_sonia(kind="IborIndex"))
    )
    swaption = PriceSwaptionRequest.GetRootAs(raw, 0).Swaptions(0).Swaption()
    assert swaption.UnderlyingType() == SwaptionUnderlying.VanillaSwap
    table = swaption.Underlying()
    vs = VanillaSwap()
    vs.Init(table.Bytes, table.Pos)
    assert _ibor_coupons(vs.FloatingLeg()).Index().Id() == b"SONIA"
