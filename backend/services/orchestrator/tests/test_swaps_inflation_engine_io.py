"""Unit tests for ``pricing/swaps_inflation/engine_io.py``.

The wire layer is a thin FB encoder + decoder around
``EngineClient.call``. These tests pin every contract a
downstream consumer (route handler / live engine) relies on:

* ``SWAP_INFLATION_ZERO_COUPON_RPC`` is exactly
  :attr:`EngineRpc.PRICE_ZERO_COUPON_INFLATION_SWAP` and
  ``SWAP_INFLATION_YEAR_ON_YEAR_RPC`` is exactly
  :attr:`EngineRpc.PRICE_YEAR_ON_YEAR_INFLATION_SWAP`. The plan
  text used a single ``PRICE_SWAP_INFLATION`` placeholder; the
  canonical enum has one RPC per swap kind. We honor the enum
  (settled — same precedent).
* The dispatch keys off ``shared_inputs["swap_kind"]`` —
  ``"zero_coupon"`` or absent → ZCIIS RPC; ``"year_on_year"`` →
  YYIIS RPC.
* ``build_swap_inflation_request`` produces a valid FlatBuffer
  for both RPCs with the per-trade overrides applied.
* ``build_swap_inflation_request`` builds
  a *faithful* ``Pricing`` graph from the route-captured
  :class:`ResolvedMarketData` — the supplied nominal discount curve
  under ``Pricing.rates`` (resolved id + substituted rate) and the
  supplied inflation curve + index under
  ``Pricing.inflation`` (resolved ids, helper ``quoteValue``
  substituted, index fixings verbatim), never the canonical
  EU-HICP fixture.
* ``decode_swap_inflation_response`` round-trips a synthesised
  response flatbuffer (one fixture per RPC) and rejects every
  malformed shape (empty, malformed root, count mismatch).
* ``price_swap_inflation_batch`` calls ``EngineClient.call``
  exactly once per batch with the right RPC + bytes and decodes
  the returned response.
* ``EngineClient.call`` exceptions bubble unchanged.
* ``EngineBatch.shared_inputs`` carries ALL keying fields per
  ``nominal_curve_id`` / ``inflation_curve_id`` /
  ``inflation_index_id`` / ``as_of`` / ``resolved_md_pin_id``
  (``None`` allowed for inline-only calls).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

import flatbuffers
import pytest

from quantra_common.engine_client import EngineClient, EngineRpc
from quantra_common.engine_client._generated.quantra.DepositHelper import DepositHelper
from quantra_common.engine_client._generated.quantra.enums.SwapType import SwapType
from quantra_common.engine_client._generated.quantra.InflationPoint import (
    InflationPoint,
)
from quantra_common.engine_client._generated.quantra.PriceYearOnYearInflationSwapRequest import (
    PriceYearOnYearInflationSwapRequest,
)
from quantra_common.engine_client._generated.quantra.PriceYearOnYearInflationSwapResponse import (
    PriceYearOnYearInflationSwapResponseT,
)
from quantra_common.engine_client._generated.quantra.PriceZeroCouponInflationSwapRequest import (
    PriceZeroCouponInflationSwapRequest,
    PriceZeroCouponInflationSwapRequestT,
)
from quantra_common.engine_client._generated.quantra.PriceZeroCouponInflationSwapResponse import (
    PriceZeroCouponInflationSwapResponseT,
)
from quantra_common.engine_client._generated.quantra.YearOnYearInflationSwapResponse import (
    YearOnYearInflationSwapResponseT,
)
from quantra_common.engine_client._generated.quantra.ZeroCouponInflationSwapHelper import (
    ZeroCouponInflationSwapHelper,
)
from quantra_common.engine_client._generated.quantra.ZeroCouponInflationSwapResponse import (
    ZeroCouponInflationSwapResponseT,
)
from quantra_orchestrator.pricing._translator import (
    CurveRole,
    ResolvedMarketData,
)
from quantra_orchestrator.pricing.concurrency import EngineBatch
from quantra_orchestrator.pricing.swaps_inflation.engine_io import (
    SWAP_INFLATION_YEAR_ON_YEAR_RPC,
    SWAP_INFLATION_ZERO_COUPON_RPC,
    build_swap_inflation_request,
    decode_swap_inflation_response,
    price_swap_inflation_batch,
)
from quantra_orchestrator.pricing.swaps_inflation.errors import (
    SwapInflationMissingTradeFieldsError,
)
from quantra_orchestrator.pricing.swaps_inflation.models import (
    InflationSwapResult,
    InflationSwapTrade,
    ResolvedCurve,
    ResolvedInflationIndex,
    ResolvedQuoteValue,
)


def _resolved_md(
    *, nominal_rate: float = 0.03, inflation_value: float = 0.025
) -> ResolvedMarketData:
    """A faithful ``ResolvedMarketData`` mirroring what the route captures.

    One nominal discount curve (rates DepositHelper point), one inflation curve
    (ZCIIS/YYIIS helper point, kind chosen by ``swap_kind`` at build time) and one
    inflation index with body fixings consumed verbatim. Both curve points
    carry ``quote_id``s the supplied quotes resolve.
    """

    nominal = ResolvedCurve(
        id=None,
        name="DISC",
        role="nominal",
        currency="EUR",
        day_counter="Actual365Fixed",
        reference_date=date(2025, 1, 15),
        points=[
            {
                "point_type": "DepositHelper",
                "tenor": {"n": 1, "unit": "Years"},
                "quote_id": "EUR.IRS.1Y",
            }
        ],
        body={"interpolator": "LogLinear"},
    )
    inflation = ResolvedCurve(
        id=None,
        name="HICP_ZC",
        role="inflation",
        currency="EUR",
        day_counter="Actual365Fixed",
        reference_date=date(2025, 1, 15),
        points=[{"tenor": {"n": 5, "unit": "Years"}, "quote_id": "EUR.HICP.5Y"}],
        body={"interpolator": "Linear"},
    )
    index = ResolvedInflationIndex(
        id=None,
        name="EU HICP",
        index_id="EUHICP",
        currency="EUR",
        day_counter="Actual365Fixed",
        body={
            "family_name": "EU HICP",
            "frequency": "Monthly",
            "fixings": [{"date": "2024-10-01", "value": 100.0}],
        },
    )
    quotes = [
        ResolvedQuoteValue(canonical_id="EUR.IRS.1Y", as_of=date(2025, 1, 15), value=nominal_rate),
        ResolvedQuoteValue(
            canonical_id="EUR.HICP.5Y", as_of=date(2025, 1, 15), value=inflation_value
        ),
    ]
    return ResolvedMarketData(
        as_of="2025-01-15",
        curves=(nominal,),
        quotes=tuple(quotes),
        curve_roles={CurveRole.NOMINAL: "DISC", CurveRole.INFLATION: "HICP_ZC"},
        inflation_curve=inflation,
        inflation_index=index,
    )


def _zciis_trade(name: str = "t-1", **swap_kwargs: object) -> InflationSwapTrade:
    body: dict[str, Any] = {
        "swap_kind": "zero_coupon",
        "swaps": [
            {
                "zero_coupon_inflation_swap": {
                    "swap_type": "Payer",
                    "notional": 1_000_000.0,
                    "start_date": "2025-01-15",
                    "maturity_date": "2030-01-15",
                    "fixed_rate": 0.0217,
                }
            }
        ],
    }
    body.update(swap_kwargs)
    return InflationSwapTrade(swap_id=None, name=name, swap=body)


def _yyiis_trade(name: str = "t-1") -> InflationSwapTrade:
    body: dict[str, Any] = {
        "swap_kind": "year_on_year",
        "swaps": [
            {
                "year_on_year_inflation_swap": {
                    "swap_type": "Receiver",
                    "notional": 1_000_000.0,
                    "fixed_schedule": {
                        "effective_date": "2025-01-15",
                        "termination_date": "2027-01-15",
                        "calendar": "TARGET",
                        "frequency": "Annual",
                    },
                    "yoy_schedule": {
                        "effective_date": "2025-01-15",
                        "termination_date": "2027-01-15",
                        "calendar": "TARGET",
                        "frequency": "Annual",
                    },
                    "fixed_rate": 0.0204,
                    "spread": 0.0002,
                }
            }
        ],
    }
    return InflationSwapTrade(swap_id=None, name=name, swap=body)


def _zciis_response_bytes(npvs: list[float]) -> bytes:
    response = PriceZeroCouponInflationSwapResponseT()
    response.swaps = []
    for npv in npvs:
        s = ZeroCouponInflationSwapResponseT()
        s.npv = npv
        s.fairRate = 0.0217
        s.fixedLegBps = 0.0
        s.fixedLegNpv = npv * 0.5
        s.inflationLegNpv = npv * 0.5
        response.swaps.append(s)
    builder = flatbuffers.Builder(256)
    builder.Finish(response.Pack(builder))
    return bytes(builder.Output())


def _yyiis_response_bytes(npvs: list[float]) -> bytes:
    response = PriceYearOnYearInflationSwapResponseT()
    response.swaps = []
    for npv in npvs:
        s = YearOnYearInflationSwapResponseT()
        s.npv = npv
        s.fairRate = 0.0204
        s.fairSpread = 0.0002
        s.fixedLegBps = 0.0
        s.yoyLegBps = 0.0
        s.fixedLegNpv = npv * 0.5
        s.yoyLegNpv = npv * 0.5
        response.swaps.append(s)
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
# RPC name pin
# ---------------------------------------------------------------------------


def test_rpc_pins_zero_coupon_to_canonical_enum() -> None:
    """Plan placeholder ``PRICE_SWAP_INFLATION`` resolves to the ZCIIS RPC.

    The canonical enum has one RPC per swap kind; we honor the
    enum (settled — same precedent). A future
    refactor that adds a wrapper RPC must update this pin explicitly.
    """

    assert SWAP_INFLATION_ZERO_COUPON_RPC is EngineRpc.PRICE_ZERO_COUPON_INFLATION_SWAP


def test_rpc_pins_year_on_year_to_canonical_enum() -> None:
    assert SWAP_INFLATION_YEAR_ON_YEAR_RPC is EngineRpc.PRICE_YEAR_ON_YEAR_INFLATION_SWAP


# ---------------------------------------------------------------------------
# build_swap_inflation_request — ZCIIS branch
# ---------------------------------------------------------------------------


def test_build_request_emits_zciis_flatbuffer_when_swap_kind_zero_coupon() -> None:
    trades = [_zciis_trade()]
    shared = {"as_of": "2025-01-15", "swap_kind": "zero_coupon"}

    raw = build_swap_inflation_request(trades, shared, resolved=_resolved_md())

    request = PriceZeroCouponInflationSwapRequest.GetRootAs(raw, 0)
    assert request.SwapsLength() == 1
    pricing = request.Pricing()
    assert pricing is not None
    assert pricing.AsOfDate() == b"2025-01-15"
    inflation = pricing.Inflation()
    assert inflation is not None
    assert inflation.InflationCurvesLength() == 1
    assert inflation.InflationIndicesLength() == 1


def test_build_request_emits_yyiis_flatbuffer_when_swap_kind_year_on_year() -> None:
    trades = [_yyiis_trade()]
    shared = {"as_of": "2025-01-15", "swap_kind": "year_on_year"}

    raw = build_swap_inflation_request(trades, shared, resolved=_resolved_md())

    request = PriceYearOnYearInflationSwapRequest.GetRootAs(raw, 0)
    assert request.SwapsLength() == 1
    pricing = request.Pricing()
    assert pricing is not None
    inflation = pricing.Inflation()
    assert inflation is not None
    assert inflation.InflationCurvesLength() == 1


def test_build_request_defaults_to_zciis_when_swap_kind_unset() -> None:
    """Missing ``swap_kind`` defaults to zero-coupon (the canonical / safer choice)."""

    raw = build_swap_inflation_request(
        [_zciis_trade()], {"as_of": "2025-01-15"}, resolved=_resolved_md()
    )
    request = PriceZeroCouponInflationSwapRequest.GetRootAs(raw, 0)
    assert request.SwapsLength() == 1


def test_build_zciis_request_carries_resolved_curves_index_and_values() -> None:
    """Faithful proof: the bytes carry the SUPPLIED ids + values.

    The nominal discount curve, inflation curve and index ids are the resolved
    entities (``DISC`` / ``HICP_ZC`` / ``EUHICP`` here), the inflation helper's
    ``quoteValue`` is the MD-resolved value, and no ``quoteId`` reaches the wire
    (invariant #8). None of the canonical ``discount`` flat-fixture leaks.
    """

    raw = build_swap_inflation_request(
        [_zciis_trade()],
        {"as_of": "2025-01-15", "swap_kind": "zero_coupon"},
        resolved=_resolved_md(nominal_rate=0.04, inflation_value=0.022),
    )
    # Read API: walks the offsets we assert directly. The object-API decode
    # (``InitFromPackedBuf``) is exercised separately in
    # ``test_build_zciis_request_decodes_via_object_api`` below; the vendored-bindings
    # ``ObservationLag()`` binding fix means both paths now decode cleanly.
    request = PriceZeroCouponInflationSwapRequest.GetRootAs(raw, 0)
    pricing = request.Pricing()
    # Nominal discount curve: resolved id (not the canonical "discount") + the
    # resolved rate substituted into the deposit helper.
    nominal = pricing.Rates().Curves(0)
    assert nominal.Id() == b"DISC"
    assert nominal.Id() != b"discount"
    deposit_tbl = nominal.Points(0).Point()
    deposit = DepositHelper()
    deposit.Init(deposit_tbl.Bytes, deposit_tbl.Pos)
    assert deposit.Rate() == pytest.approx(0.04)
    # Inflation curve + index: resolved ids, helper quoteValue substituted, no
    # quoteId leak (invariant #8), index linked to the resolved nominal curve.
    inflation = pricing.Inflation()
    inflation_curve = inflation.InflationCurves(0)
    assert inflation_curve.Id() == b"HICP_ZC"
    assert inflation_curve.IndexId() == b"EUHICP"
    assert inflation_curve.DiscountCurveId() == b"DISC"
    infl_point = inflation_curve.Points(0)
    assert infl_point.PointType() == InflationPoint.ZeroCouponInflationSwapHelper
    helper_tbl = infl_point.Point()
    helper = ZeroCouponInflationSwapHelper()
    helper.Init(helper_tbl.Bytes, helper_tbl.Pos)
    assert helper.QuoteValue() == pytest.approx(0.022)
    assert not helper.QuoteId()
    assert inflation.InflationIndices(0).Id() == b"EUHICP"
    # Per-trade references honour the resolved ids — no CANONICAL_* leak.
    price = request.Swaps(0)
    assert price.DiscountingCurve() == b"DISC"
    assert price.InflationCurve() == b"HICP_ZC"
    assert price.ZeroCouponInflationSwap().InflationIndexId() == b"EUHICP"


def test_build_zciis_request_decodes_via_object_api() -> None:
    """The object-API ``InitFromPackedBuf`` decodes the whole ZCIIS graph.

    ``InitFromPackedBuf`` eagerly unpacks every nested table, so it walks
    ``InflationIndexSpecT._UnPack`` → ``ObservationLag``. Before the
    binding fix that read accessor referenced ``Period`` without importing it
    and raised ``NameError`` on the unpack path; the earlier tests dodged it with
    the lazy read API. With the import injected the whole faithful ``Pricing``
    tree decodes as Python objects, including the index's observation /
    availability lags and verbatim fixings.
    """

    raw = build_swap_inflation_request(
        [_zciis_trade()],
        {"as_of": "2025-01-15", "swap_kind": "zero_coupon"},
        resolved=_resolved_md(nominal_rate=0.04, inflation_value=0.022),
    )

    obj = PriceZeroCouponInflationSwapRequestT.InitFromPackedBuf(raw, 0)
    inflation = obj.pricing.inflation
    # The index decodes through the previously-broken ObservationLag accessor.
    index = inflation.inflationIndices[0]
    assert index.id == b"EUHICP"
    assert index.observationLag is not None
    assert index.observationLag.n >= 0
    assert index.availabilityLag is not None
    # Verbatim fixings survive the object-API round-trip.
    assert index.fixings is not None
    assert index.fixings[0].value == pytest.approx(100.0)
    # The faithful nominal curve + substituted rate also decode as objects.
    nominal = next(c for c in obj.pricing.rates.curves if c.id == b"DISC")
    assert nominal.points[0].point.rate == pytest.approx(0.04)


def test_build_zciis_request_honors_per_trade_overrides() -> None:
    body = {
        "swap_kind": "zero_coupon",
        "swaps": [
            {
                "zero_coupon_inflation_swap": {
                    "swap_type": "Receiver",
                    "notional": 2_000_000.0,
                    "start_date": "2030-01-15",
                    "maturity_date": "2035-01-15",
                    "fixed_rate": 0.04,
                }
            }
        ],
    }
    trade = InflationSwapTrade(swap_id=None, name="custom", swap=body)
    raw = build_swap_inflation_request(
        [trade],
        {"as_of": "2025-01-15", "swap_kind": "zero_coupon"},
        resolved=_resolved_md(),
    )
    request = PriceZeroCouponInflationSwapRequest.GetRootAs(raw, 0)
    swap = request.Swaps(0)
    assert swap is not None
    inner = swap.ZeroCouponInflationSwap()
    assert inner is not None
    assert inner.SwapType() == SwapType.Receiver
    assert inner.Notional() == pytest.approx(2_000_000.0)
    assert inner.FixedRate() == pytest.approx(0.04)
    assert inner.StartDate() == b"2030-01-15"
    assert inner.MaturityDate() == b"2035-01-15"


# ---------------------------------------------------------------------------
# shared_inputs keying contract
# ---------------------------------------------------------------------------


def test_shared_inputs_carries_all_d80_keying_fields() -> None:
    """``shared_inputs`` always emits the FIVE keying fields.

    The reserved ``group_by_inflation_curve`` policy keys
    off ``inflation_curve_id``; ``nominal_curve_id`` /
    ``inflation_index_id`` / ``as_of`` / ``resolved_md_pin_id``
    are also always present so the contract is stable for future
    policies. ``None`` is allowed for inline-only calls; the
    keys must NEVER be missing.
    """

    shared: dict[str, Any] = {
        "nominal_curve_id": "00000000-0000-0000-0000-000000000001",
        "inflation_curve_id": "00000000-0000-0000-0000-000000000002",
        "inflation_index_id": "00000000-0000-0000-0000-000000000003",
        "as_of": "2025-01-15",
        "resolved_md_pin_id": None,
        "swap_kind": "zero_coupon",
    }
    batch: EngineBatch[InflationSwapTrade] = EngineBatch(
        trades=(_zciis_trade(),), shared_inputs=shared
    )
    for key in (
        "nominal_curve_id",
        "inflation_curve_id",
        "inflation_index_id",
        "as_of",
        "resolved_md_pin_id",
    ):
        assert key in batch.shared_inputs

    none_batch: EngineBatch[InflationSwapTrade] = EngineBatch(
        trades=(_zciis_trade(),),
        shared_inputs={
            "nominal_curve_id": None,
            "inflation_curve_id": None,
            "inflation_index_id": None,
            "as_of": "2025-01-15",
            "resolved_md_pin_id": None,
            "swap_kind": "zero_coupon",
        },
    )
    for key in (
        "nominal_curve_id",
        "inflation_curve_id",
        "inflation_index_id",
        "resolved_md_pin_id",
    ):
        assert none_batch.shared_inputs[key] is None


# ---------------------------------------------------------------------------
# decode_swap_inflation_response
# ---------------------------------------------------------------------------


def test_decode_zciis_response_round_trips() -> None:
    body = _zciis_response_bytes([12345.5])
    decoded = decode_swap_inflation_response(body, expected_count=1, swap_kind="zero_coupon")
    assert len(decoded) == 1
    assert isinstance(decoded[0], InflationSwapResult)
    assert decoded[0].npv == pytest.approx(12345.5)
    assert decoded[0].swap_kind == "zero_coupon"
    assert decoded[0].fair_rate == pytest.approx(0.0217)
    assert decoded[0].inflation_leg_npv == pytest.approx(12345.5 * 0.5)
    assert decoded[0].fair_spread is None
    assert decoded[0].yoy_leg_npv is None


def test_decode_yyiis_response_round_trips() -> None:
    body = _yyiis_response_bytes([6789.0])
    decoded = decode_swap_inflation_response(body, expected_count=1, swap_kind="year_on_year")
    assert len(decoded) == 1
    assert decoded[0].swap_kind == "year_on_year"
    assert decoded[0].npv == pytest.approx(6789.0)
    assert decoded[0].fair_rate == pytest.approx(0.0204)
    assert decoded[0].fair_spread == pytest.approx(0.0002)
    assert decoded[0].yoy_leg_npv == pytest.approx(6789.0 * 0.5)
    assert decoded[0].inflation_leg_npv is None


def test_decode_response_rejects_empty_body() -> None:
    with pytest.raises(ValueError, match="empty engine response"):
        decode_swap_inflation_response(b"", expected_count=1, swap_kind="zero_coupon")


def test_decode_zciis_response_rejects_count_mismatch() -> None:
    body = _zciis_response_bytes([1.0, 2.0])
    with pytest.raises(ValueError, match=r"returned 2 ZCIIS"):
        decode_swap_inflation_response(body, expected_count=3, swap_kind="zero_coupon")


def test_decode_yyiis_response_rejects_count_mismatch() -> None:
    body = _yyiis_response_bytes([1.0, 2.0])
    with pytest.raises(ValueError, match=r"returned 2 YYIIS"):
        decode_swap_inflation_response(body, expected_count=3, swap_kind="year_on_year")


# ---------------------------------------------------------------------------
# price_swap_inflation_batch — RPC dispatch + round-trip
# ---------------------------------------------------------------------------


async def test_price_batch_dispatches_zciis_to_zero_coupon_rpc() -> None:
    body = _zciis_response_bytes([42.0])
    engine = _FakeEngine(response=body)
    batch: EngineBatch[InflationSwapTrade] = EngineBatch(
        trades=(_zciis_trade(),),
        shared_inputs={
            "as_of": "2025-01-15",
            "swap_kind": "zero_coupon",
            "nominal_curve_id": None,
            "inflation_curve_id": None,
            "inflation_index_id": None,
            "resolved_md_pin_id": None,
        },
    )

    results: Sequence[InflationSwapResult] = await price_swap_inflation_batch(
        engine, batch, resolved=_resolved_md()
    )

    assert len(engine.calls) == 1
    rpc, _ = engine.calls[0]
    assert rpc is EngineRpc.PRICE_ZERO_COUPON_INFLATION_SWAP
    assert results[0].swap_kind == "zero_coupon"
    assert results[0].npv == pytest.approx(42.0)


async def test_price_batch_dispatches_yyiis_to_year_on_year_rpc() -> None:
    body = _yyiis_response_bytes([99.0])
    engine = _FakeEngine(response=body)
    batch: EngineBatch[InflationSwapTrade] = EngineBatch(
        trades=(_yyiis_trade(),),
        shared_inputs={
            "as_of": "2025-01-15",
            "swap_kind": "year_on_year",
            "nominal_curve_id": None,
            "inflation_curve_id": None,
            "inflation_index_id": None,
            "resolved_md_pin_id": None,
        },
    )

    results = await price_swap_inflation_batch(engine, batch, resolved=_resolved_md())

    assert len(engine.calls) == 1
    rpc, _ = engine.calls[0]
    assert rpc is EngineRpc.PRICE_YEAR_ON_YEAR_INFLATION_SWAP
    assert results[0].swap_kind == "year_on_year"
    assert results[0].npv == pytest.approx(99.0)


async def test_price_batch_propagates_engine_exceptions() -> None:
    engine = _FakeEngine()
    engine.set_exception(NotImplementedError("stub backend"))
    batch: EngineBatch[InflationSwapTrade] = EngineBatch(
        trades=(_zciis_trade(),),
        shared_inputs={"swap_kind": "zero_coupon"},
    )

    with pytest.raises(NotImplementedError, match="stub backend"):
        await price_swap_inflation_batch(engine, batch, resolved=_resolved_md())


# ---------------------------------------------------------------------------
# missing required trade fields → typed 422 (no fixture-default swap)
# ---------------------------------------------------------------------------


def test_build_zciis_request_rejects_missing_required_fields() -> None:
    """A missing notional/rate/date is a typed 422, never a fixture-default ZCIIS."""

    trade = InflationSwapTrade(
        swap_id=None,
        name="t-1",
        swap={"swap_kind": "zero_coupon", "swaps": [{"zero_coupon_inflation_swap": {}}]},
    )
    with pytest.raises(SwapInflationMissingTradeFieldsError) as exc_info:
        build_swap_inflation_request([trade], {"swap_kind": "zero_coupon"}, resolved=_resolved_md())
    exc = exc_info.value
    assert exc.status_code == 422
    assert exc.code == "swap_inflation_missing_trade_fields"
    missing = {d["field"] for d in exc.details or []}
    assert missing == {"notional", "fixed_rate", "start_date", "maturity_date"}


def test_build_yyiis_request_rejects_missing_required_fields() -> None:
    """YYIIS: missing schedule dates are reported with their block prefix."""

    trade = InflationSwapTrade(
        swap_id=None,
        name="t-1",
        swap={
            "swap_kind": "year_on_year",
            "swaps": [{"year_on_year_inflation_swap": {"notional": 1_000_000.0}}],
        },
    )
    with pytest.raises(SwapInflationMissingTradeFieldsError) as exc_info:
        build_swap_inflation_request(
            [trade], {"swap_kind": "year_on_year"}, resolved=_resolved_md()
        )
    exc = exc_info.value
    assert exc.status_code == 422
    assert exc.code == "swap_inflation_missing_trade_fields"
    missing = {d["field"] for d in exc.details or []}
    assert missing == {
        "fixed_rate",
        "fixed_schedule.effective_date",
        "fixed_schedule.termination_date",
        "yoy_schedule.effective_date",
        "yoy_schedule.termination_date",
    }
