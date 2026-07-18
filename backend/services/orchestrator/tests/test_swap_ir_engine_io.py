"""Unit tests for ``pricing/swap_ir/engine_io.py``.

The wire layer is a thin FB encoder + decoder around
``EngineClient.call``. These tests pin every contract a
downstream consumer (route handler / live engine) relies on:

* ``SWAP_IR_ENGINE_RPC`` is exactly :attr:`EngineRpc.PRICE_VANILLA_SWAP`
  — the plan's placeholder ``PRICE_SWAP_IR`` resolves here;
  the engine has no separate IR-swap RPC.
* ``build_swap_ir_request`` produces a valid
  ``PriceVanillaSwapRequest`` flatbuffer with the per-trade
  overrides applied (notional / fixed_rate / swap_type / dates).
* ``build_swap_ir_request`` always lands its canonical curve +
  index in ``Pricing.rates`` regardless of how loose the trade
  bodies are.
* ``decode_swap_ir_response`` round-trips a synthesised
  ``PriceVanillaSwapResponse`` flatbuffer and rejects every
  malformed shape (empty, malformed root, count mismatch).
* ``price_swap_ir_batch`` calls ``EngineClient.call`` exactly once
  per batch with the right RPC + bytes and decodes whatever the
  engine returns.
* ``EngineClient.call`` exceptions bubble unchanged (the route
  handler's :func:`map_engine_client_error` is responsible for
  translating them to the error envelope).
* ``EngineBatch.shared_inputs`` carries the ``curve_set_id``
  keying field — the FB encode is independent of that
  field's value (the canonical curve is always emitted), but the
  field travels in the request graph regardless.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date

import flatbuffers
import pytest

from quantra_common.engine_client import EngineClient, EngineRpc
from quantra_common.engine_client._generated.quantra.DepositHelper import DepositHelper
from quantra_common.engine_client._generated.quantra.enums.Frequency import Frequency
from quantra_common.engine_client._generated.quantra.enums.SwapType import SwapType
from quantra_common.engine_client._generated.quantra.Point import Point
from quantra_common.engine_client._generated.quantra.PriceVanillaSwapRequest import (
    PriceVanillaSwapRequest,
)
from quantra_common.engine_client._generated.quantra.PriceVanillaSwapResponse import (
    PriceVanillaSwapResponseT,
)
from quantra_common.engine_client._generated.quantra.SwapLegFlow import SwapLegFlowT
from quantra_common.engine_client._generated.quantra.VanillaSwapResponse import (
    VanillaSwapResponseT,
)
from quantra_orchestrator.engine.errors import EngineMissingFixingError
from quantra_orchestrator.pricing._translator import (
    DEFAULT_FORWARDING_INDEX_ID,
    CurveRole,
    ResolvedMarketData,
    resolved_curve_id,
)
from quantra_orchestrator.pricing.concurrency import EngineBatch
from quantra_orchestrator.pricing.swap_ir.engine_io import (
    SWAP_IR_ENGINE_RPC,
    build_swap_ir_request,
    decode_swap_ir_response,
    price_swap_ir_batch,
)
from quantra_orchestrator.pricing.swap_ir.errors import (
    SwapIrCurveResolutionFailedError,
    SwapIrMissingTradeFieldsError,
    SwapIrQuoteResolutionFailedError,
)
from quantra_orchestrator.pricing.swap_ir.models import (
    IrSwapResult,
    IrSwapTrade,
    ResolvedCurve,
    ResolvedIndex,
    ResolvedQuoteValue,
    SwapFlow,
)


def _deposit_point(
    *, quote_id: str | None = "USD.IRS.1Y", rate: float | None = None
) -> dict[str, object]:
    """A wrapped DepositHelper curve point (quote-substituted or inline)."""

    point: dict[str, object] = {
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
    return {"point_type": "DepositHelper", "point": point}


def _resolved_curve(
    curve_id: uuid.UUID,
    *,
    points: list[dict[str, object]] | None = None,
    name: str = "USD-OIS",
) -> ResolvedCurve:
    return ResolvedCurve(
        id=curve_id,
        name=name,
        currency="USD",
        day_counter="Actual360",
        reference_date=date(2026, 5, 13),
        points=points if points is not None else [_deposit_point()],
        body={"interpolator": "LogLinear"},
    )


def _quote(canonical_id: str = "USD.IRS.1Y", value: float = 0.0425) -> ResolvedQuoteValue:
    return ResolvedQuoteValue(canonical_id=canonical_id, as_of=date(2026, 5, 13), value=value)


def _resolved(
    *,
    curves: list[ResolvedCurve] | None = None,
    quotes: list[ResolvedQuoteValue] | None = None,
    as_of: str = "2026-05-13",
) -> ResolvedMarketData:
    """Default: one resolved curve with one quote-substituted deposit helper."""

    if curves is None:
        curves = [_resolved_curve(uuid.uuid4())]
    if quotes is None:
        quotes = [_quote()]
    return ResolvedMarketData(as_of=as_of, curves=tuple(curves), quotes=tuple(quotes))


def _trade(name: str = "t-1", **swap_kwargs: object) -> IrSwapTrade:
    # Explicit economics — the wire layer no longer supplies silent
    # defaults (422 ``swap_ir_missing_trade_fields`` otherwise). Values
    # match the historical fixture defaults so golden bytes are unchanged.
    body: dict[str, object] = {
        "notional": 1_000_000.0,
        "fixed_rate": 0.035,
        # Forward-start relative to the fixture as_of (2026-05-13) so the
        # O47b seasoned-swap pre-flight does not fire.
        "effective_date": "2026-06-15",
        "termination_date": "2031-06-15",
    }
    body.update(swap_kwargs)
    return IrSwapTrade(swap_id=None, name=name, swap=body)


def _make_response_bytes(npvs: list[float]) -> bytes:
    """Synthesize a ``PriceVanillaSwapResponse`` flatbuffer with given NPVs.

    Used by the decode unit tests so they don't depend on a live
    engine — pack a ``PriceVanillaSwapResponseT`` with one
    ``VanillaSwapResponse`` per NPV and finish the buffer.
    """

    response = PriceVanillaSwapResponseT()
    response.swaps = []
    for npv in npvs:
        s = VanillaSwapResponseT()
        s.npv = npv
        s.fairRate = 0.03
        s.fairSpread = 0.0
        s.fixedLegBps = 0.0
        s.floatingLegBps = 0.0
        s.fixedLegNpv = npv * 0.5
        s.floatingLegNpv = npv * 0.5
        response.swaps.append(s)
    builder = flatbuffers.Builder(256)
    builder.Finish(response.Pack(builder))
    return bytes(builder.Output())


def _flow(**fields: object) -> SwapLegFlowT:
    """Build one ``SwapLegFlowT`` from keyword overrides of its FB fields."""

    flow = SwapLegFlowT()
    for key, value in fields.items():
        setattr(flow, key, value)
    return flow


def _make_response_bytes_with_flows() -> bytes:
    """Synthesize a one-swap ``PriceVanillaSwapResponse`` carrying per-leg flows.

    Two fixed-leg coupons + two floating-leg coupons, mirroring what a live
    engine emits when the request set ``include_flows``. Used to pin the
    decode -> :class:`SwapFlow` mapping without a live engine.
    """

    s = VanillaSwapResponseT()
    s.npv = 1000.0
    s.fairRate = 0.025
    s.fixedLegNpv = -500.0
    s.floatingLegNpv = 1500.0
    s.fixedLegFlows = [
        _flow(
            paymentDate="2027-02-11",
            accrualStartDate="2026-02-11",
            accrualEndDate="2027-02-11",
            amount=-250000.0,
            accrualYearFraction=1.0,
            discount=0.98,
            presentValue=-245000.0,
            rate=0.025,
        ),
        _flow(
            paymentDate="2028-02-11",
            accrualStartDate="2027-02-11",
            accrualEndDate="2028-02-11",
            amount=-250000.0,
            discount=0.96,
            presentValue=-240000.0,
            rate=0.025,
        ),
    ]
    s.floatingLegFlows = [
        _flow(
            paymentDate="2026-08-11",
            accrualStartDate="2026-02-11",
            accrualEndDate="2026-08-11",
            amount=130000.0,
            accrualYearFraction=0.5,
            discount=0.99,
            presentValue=128700.0,
            fixingDate="2026-02-09",
            indexFixing=0.026,
            rate=0.026,
        ),
        _flow(
            paymentDate="2027-02-11",
            accrualStartDate="2026-08-11",
            accrualEndDate="2027-02-11",
            amount=135000.0,
            discount=0.98,
            presentValue=132300.0,
            fixingDate="2026-08-09",
            indexFixing=0.027,
            rate=0.027,
        ),
    ]
    response = PriceVanillaSwapResponseT()
    response.swaps = [s]
    builder = flatbuffers.Builder(1024)
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


def test_swap_ir_engine_rpc_is_vanilla_swap() -> None:
    """The plan's placeholder ``PRICE_SWAP_IR`` resolves to ``PRICE_VANILLA_SWAP``.

    Engine's RPC enum has no separate IR-swap entry; the vanilla
    swap RPC handles the product. Pinned in a test so a future
    refactor that adds a new enum entry doesn't silently re-target
    the orchestrator.
    """

    assert SWAP_IR_ENGINE_RPC is EngineRpc.PRICE_VANILLA_SWAP


def test_build_request_emits_a_parseable_flatbuffer() -> None:
    """Output is a valid ``PriceVanillaSwapRequest`` flatbuffer.

    Round-trips through :class:`PriceVanillaSwapRequest.GetRootAs`. The
    ``as_of`` now comes from the resolved bundle, not ``shared_inputs``.
    """

    trades = [_trade("a"), _trade("b")]

    raw = build_swap_ir_request(trades, resolved=_resolved())

    request = PriceVanillaSwapRequest.GetRootAs(raw, 0)
    assert request.SwapsLength() == 2
    pricing = request.Pricing()
    assert pricing is not None
    assert pricing.AsOfDate() == b"2026-05-13"


def test_build_request_carries_the_resolved_curve_in_pricing() -> None:
    """The request curve is the *resolved* one: real id, supplied rate.

    The faithful translator replaces the old canonical
    ``discount`` / flat-3 % fixture: the encoded curve carries the resolved
    ``app.curves`` UUID as its id and the deposit helper carries the supplied
    quote value (0.0425), with the ``quote_id`` dropped server-side.
    """

    curve_id = uuid.uuid4()
    resolved = _resolved(
        curves=[_resolved_curve(curve_id, points=[_deposit_point(quote_id="USD.IRS.1Y")])],
        quotes=[_quote("USD.IRS.1Y", 0.0425)],
    )

    raw = build_swap_ir_request([_trade()], resolved=resolved)

    request = PriceVanillaSwapRequest.GetRootAs(raw, 0)
    pricing = request.Pricing()
    assert pricing is not None
    rates = pricing.Rates()
    assert rates is not None
    assert rates.CurvesLength() == 1
    curve = rates.Curves(0)
    assert curve is not None
    # Resolved entity id, not the canonical "discount" string.
    assert curve.Id() == str(curve_id).encode()
    assert curve.ReferenceDate() == b"2026-05-13"

    # Decode the helper point via the read API and assert the supplied rate
    # reached the bytes (not the canonical 0.03) and the quote id is gone.
    assert curve.PointsLength() == 1
    wrapper = curve.Points(0)
    assert wrapper.PointType() == Point.DepositHelper
    table = wrapper.Point()
    deposit = DepositHelper()
    deposit.Init(table.Bytes, table.Pos)
    assert deposit.Rate() == pytest.approx(0.0425)
    assert not deposit.QuoteId()  # None / b"" — never leaks to the engine


def test_build_request_substitutes_inline_rate_without_a_quote_id() -> None:
    """An inline ``rate`` (no quote_id) is carried through verbatim."""

    curve_id = uuid.uuid4()
    resolved = _resolved(
        curves=[_resolved_curve(curve_id, points=[_deposit_point(quote_id=None, rate=0.051)])],
        quotes=[],
    )

    raw = build_swap_ir_request([_trade()], resolved=resolved)

    request = PriceVanillaSwapRequest.GetRootAs(raw, 0)
    curve = request.Pricing().Rates().Curves(0)
    table = curve.Points(0).Point()
    deposit = DepositHelper()
    deposit.Init(table.Bytes, table.Pos)
    assert deposit.Rate() == pytest.approx(0.051)


def test_build_request_missing_quote_raises_swap_ir_quote_resolution_failed() -> None:
    """A quote_id with no resolved value → ``swap_ir_quote_resolution_failed`` (422)."""

    resolved = _resolved(
        curves=[_resolved_curve(uuid.uuid4(), points=[_deposit_point(quote_id="USD.IRS.1Y")])],
        quotes=[],  # nothing resolves
    )
    with pytest.raises(SwapIrQuoteResolutionFailedError) as excinfo:
        build_swap_ir_request([_trade()], resolved=resolved)
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "swap_ir_quote_resolution_failed"


def test_build_request_seasoned_swap_raises_missing_required_fixing() -> None:
    """A swap whose EXPLICIT effective precedes as_of → ``missing_required_fixing``.

    O47b(A): the floating leg's first accrual period has already started, so it
    needs a historical fixing the orchestrator supplies none of — the engine
    would abort with an (opaque, on this build) ``ABORTED (QuantLib error)``.
    The pre-flight detects this and raises the actionable 422 naming the index
    + the first already-started period, BEFORE the engine round-trip.
    """

    resolved = _resolved(as_of="2025-01-15")
    trade = _trade(effective_date="2024-07-15", termination_date="2029-07-15")
    with pytest.raises(EngineMissingFixingError) as excinfo:
        build_swap_ir_request([trade], resolved=resolved)
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "missing_required_fixing"
    entry = excinfo.value.details[0]
    assert entry["fixing_date"] == "2024-07-15"
    assert "2025-01-15" in entry["engine_detail"]
    assert "2024-07-15" in excinfo.value.detail


def test_build_request_forward_start_swap_does_not_preflight_fixing() -> None:
    """An explicit effective ON/AFTER as_of is a forward/spot start → no pre-flight.

    Being conservative: only a strictly-past period is flagged, so a swap the
    engine could actually price is never blocked.
    """

    resolved = _resolved(as_of="2025-01-15")
    trade = _trade(effective_date="2025-06-15", termination_date="2030-06-15")
    # Builds fine (no EngineMissingFixingError); returns a parseable buffer.
    raw = build_swap_ir_request([trade], resolved=resolved)
    assert PriceVanillaSwapRequest.GetRootAs(raw, 0).SwapsLength() == 1


def test_build_request_rejects_missing_required_fields() -> None:
    """A missing rate/notional/schedule is a typed 422, never a fixture-default swap."""

    trade = IrSwapTrade(swap_id=None, name="t-1", swap={"swap_type": "Payer"})
    with pytest.raises(SwapIrMissingTradeFieldsError) as exc_info:
        build_swap_ir_request([trade], resolved=_resolved())
    exc = exc_info.value
    assert exc.status_code == 422
    assert exc.code == "swap_ir_missing_trade_fields"
    missing = {d["field"] for d in exc.details or []}
    assert missing == {"notional", "fixed_rate", "effective_date", "termination_date"}


def test_build_request_unknown_helper_kind_raises_curve_resolution_failed() -> None:
    """An unmapped ``point_type`` rejects → ``swap_ir_curve_resolution_failed`` (422)."""

    bad_point: dict[str, object] = {
        "point_type": "WidgetHelper",
        "point": {"quote_id": "USD.IRS.1Y"},
    }
    resolved = _resolved(
        curves=[_resolved_curve(uuid.uuid4(), points=[bad_point])],
        quotes=[_quote()],
    )
    with pytest.raises(SwapIrCurveResolutionFailedError) as excinfo:
        build_swap_ir_request([_trade()], resolved=resolved)
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "swap_ir_curve_resolution_failed"


def test_build_request_per_trade_refs_carry_resolved_ids() -> None:
    """Per-trade discount/forwarding refs equal the resolved curve ids."""

    discount_id = uuid.uuid4()
    forwarding_id = uuid.uuid4()
    resolved = _resolved(
        curves=[
            _resolved_curve(discount_id, points=[_deposit_point(quote_id="USD.IRS.1Y")]),
            _resolved_curve(
                forwarding_id,
                name="USD-FWD",
                points=[_deposit_point(quote_id="USD.IRS.1Y")],
            ),
        ],
        quotes=[_quote("USD.IRS.1Y", 0.0425)],
    )

    raw = build_swap_ir_request([_trade()], resolved=resolved)
    request = PriceVanillaSwapRequest.GetRootAs(raw, 0)
    swap = request.Swaps(0)
    assert swap.DiscountingCurve() == str(discount_id).encode()
    assert swap.ForwardingCurve() == str(forwarding_id).encode()
    # The float leg references the interim default forwarding index, never a
    # canonical curve id.
    assert swap.VanillaSwap().FloatingLeg().Index().Id() == DEFAULT_FORWARDING_INDEX_ID.encode()


def _resolved_index(n: int, unit: str = "Months") -> ResolvedIndex:
    """A resolved float-leg index at the given tenor (pure config, not MD)."""

    return ResolvedIndex(
        id=None,
        name="TEST-IBOR",
        kind="IborIndex",
        currency="EUR",
        body={"tenor": {"n": n, "unit": unit}},
    )


def _float_frequency(resolved: ResolvedMarketData) -> int:
    """Encode a swap with ``resolved`` and read back its float-leg frequency."""

    raw = build_swap_ir_request([_trade()], resolved=resolved)
    request = PriceVanillaSwapRequest.GetRootAs(raw, 0)
    return int(request.Swaps(0).VanillaSwap().FloatingLeg().Schedule().Frequency())


@pytest.mark.parametrize(
    ("n", "unit", "expected"),
    [
        (3, "Months", Frequency.Quarterly),
        (6, "Months", Frequency.Semiannual),
        (1, "Months", Frequency.Monthly),
        (1, "Years", Frequency.Annual),
        (12, "Months", Frequency.Annual),
    ],
)
def test_float_leg_frequency_follows_index_tenor(n: int, unit: str, expected: int) -> None:
    """the float coupon schedule frequency is derived from the index tenor.

    A 3M index must pay quarterly float coupons, a 6M index semi-annually, etc.
    Previously the frequency was hardcoded ``Semiannual`` so the index tenor was
    invisible to the engine (a 3M index still priced with semi-annual float
    coupons — wrong on any multi-curve setup).
    """

    resolved = ResolvedMarketData(
        as_of="2026-05-13",
        curves=(_resolved_curve(uuid.uuid4()),),
        quotes=(_quote(),),
        index=_resolved_index(n, unit),
    )
    assert _float_frequency(resolved) == expected


def test_float_leg_frequency_defaults_semiannual_without_index() -> None:
    """No resolved index → the documented single flat-default stays Semiannual.

    The no-index fallback must price exactly as before this fix, so
    the frequency is unchanged when no float index is supplied.
    """

    assert _float_frequency(_resolved()) == Frequency.Semiannual


def test_float_leg_frequency_falls_back_on_uncleanly_divisible_tenor() -> None:
    """A tenor with no exact annual frequency (5M) falls back to the default."""

    resolved = ResolvedMarketData(
        as_of="2026-05-13",
        curves=(_resolved_curve(uuid.uuid4()),),
        quotes=(_quote(),),
        index=_resolved_index(5, "Months"),
    )
    assert _float_frequency(resolved) == Frequency.Semiannual


def test_build_request_includes_curve_set_id_in_shared_inputs() -> None:
    """batching hook: ``curve_set_id`` rides through the engine batch.

    The reserved ``group_by_curve_set`` policy keys off this
    field. The swap_ir route always emits ``curve_set_id``
    (``None`` for inline-only calls) so future policy adoption
    requires zero changes to ``engine_io.py``. The FB encoder
    doesn't (yet) thread the id into the wire request — it lives
    in :class:`EngineBatch.shared_inputs` and is consumed by the
    runner / future policy. Pinning the contract here means a
    regression that drops the field from `shared_inputs` is
    caught at the seam, not in production.
    """

    shared = {
        "as_of": "2026-05-13",
        "curve_set_id": "00000000-0000-0000-0000-000000000001",
    }
    batch: EngineBatch[IrSwapTrade] = EngineBatch(
        trades=(_trade(),),
        shared_inputs=shared,
    )
    assert batch.shared_inputs["curve_set_id"] == "00000000-0000-0000-0000-000000000001"

    none_batch: EngineBatch[IrSwapTrade] = EngineBatch(
        trades=(_trade(),),
        shared_inputs={"as_of": "2026-05-13", "curve_set_id": None},
    )
    assert "curve_set_id" in none_batch.shared_inputs
    assert none_batch.shared_inputs["curve_set_id"] is None


def test_build_request_honors_per_trade_overrides() -> None:
    """``trade.swap`` keys override per-leg defaults; canonical pricing stays."""

    trade = _trade(
        "custom",
        notional=2_000_000.0,
        fixed_rate=0.04,
        swap_type="Receiver",
        effective_date="2030-01-15",
        termination_date="2035-01-15",
    )
    raw = build_swap_ir_request([trade], resolved=_resolved())
    request = PriceVanillaSwapRequest.GetRootAs(raw, 0)
    swap = request.Swaps(0)
    assert swap is not None
    vs = swap.VanillaSwap()
    assert vs is not None
    assert vs.SwapType() == SwapType.Receiver
    fl = vs.FixedLeg()
    assert fl is not None
    assert fl.Notional() == pytest.approx(2_000_000.0)
    assert fl.Rate() == pytest.approx(0.04)
    fl_sched = fl.Schedule()
    assert fl_sched is not None
    assert fl_sched.EffectiveDate() == b"2030-01-15"
    assert fl_sched.TerminationDate() == b"2035-01-15"


def test_decode_response_round_trips_a_one_trade_payload() -> None:
    body = _make_response_bytes([12345.5])
    decoded = decode_swap_ir_response(body, expected_count=1)
    assert len(decoded) == 1
    assert isinstance(decoded[0], IrSwapResult)
    assert decoded[0].npv == pytest.approx(12345.5)
    # Per-leg roles always present and ordered fixed-then-floating.
    assert [entry["role"] for entry in decoded[0].leg_npvs] == ["fixed", "floating"]
    assert "fair_rate" in decoded[0].extras


def test_decode_response_round_trips_multi_trade_payload_in_order() -> None:
    body = _make_response_bytes([1.0, 2.0, 3.0])
    decoded = decode_swap_ir_response(body, expected_count=3)
    assert [r.npv for r in decoded] == [pytest.approx(1.0), pytest.approx(2.0), pytest.approx(3.0)]


def test_decode_response_rejects_empty_body() -> None:
    with pytest.raises(ValueError, match="empty engine response"):
        decode_swap_ir_response(b"", expected_count=1)


def test_decode_response_rejects_malformed_root() -> None:
    """A buffer that is not a ``PriceVanillaSwapResponse`` flatbuffer is rejected."""

    with pytest.raises(ValueError, match=r"malformed engine response|expected "):
        decode_swap_ir_response(b"this-is-not-a-flatbuffer", expected_count=1)


def test_decode_response_rejects_count_mismatch() -> None:
    body = _make_response_bytes([1.0, 2.0])
    with pytest.raises(ValueError, match="returned 2 result\\(s\\), expected 3"):
        decode_swap_ir_response(body, expected_count=3)


async def test_price_swap_ir_batch_round_trip() -> None:
    body = _make_response_bytes([42.0])
    engine = _FakeEngine(response=body)
    batch: EngineBatch[IrSwapTrade] = EngineBatch(
        trades=(_trade("a"),),
        shared_inputs={"as_of": "2026-05-13"},
    )

    results: Sequence[IrSwapResult] = await price_swap_ir_batch(engine, batch, resolved=_resolved())

    assert len(engine.calls) == 1
    rpc, request_bytes = engine.calls[0]
    assert rpc is EngineRpc.PRICE_VANILLA_SWAP
    # The bytes round-trip into a valid PriceVanillaSwapRequest.
    parsed = PriceVanillaSwapRequest.GetRootAs(request_bytes, 0)
    assert parsed.SwapsLength() == 1
    assert len(results) == 1
    assert results[0].npv == pytest.approx(42.0)


async def test_price_swap_ir_batch_propagates_engine_exceptions() -> None:
    engine = _FakeEngine()
    engine.set_exception(NotImplementedError("stub backend"))
    batch: EngineBatch[IrSwapTrade] = EngineBatch(trades=(_trade(),), shared_inputs={})

    with pytest.raises(NotImplementedError, match="stub backend"):
        await price_swap_ir_batch(engine, batch, resolved=_resolved())


# ---------------------------------------------------------------------------
# Bytes-unchanged regression (role-split + index resolution is a pure
# refactor for the EUR-6M fixture). Golden bytes captured from the previous
# swap_ir request path; any drift fails here.
# ---------------------------------------------------------------------------

# golden re-captured after ``build_swap_ir_request`` began forcing
# FlatBuffers defaults onto the wire (``builder.ForceDefaults(True)``) so engine
# 0.2.0's presence-based schema no longer reads a zero-default enum (e.g.
# ``Schedule.frequency == Frequency.Annual == 0``) as missing. The added bytes
# are exactly the previously-dropped zero-valued fields; the priced NPV is
# unchanged on both 0.1.1 and 0.2.0 (verified by the live A/B). See
# ``test_swap_ir_wire_forces_schedule_conventions`` for the presence assertions.
_SWAP_IR_GOLDEN_HEX = (
    "1000000000000a0010000c00080007000a0000000000000108000000d0010000010000001000000000000a0010"
    "000c00080004000a0000000c00000034000000640000002400000032323232323232322d323232322d32323232"
    "2d323232322d323232323232323232323232000000002400000031313131313131312d313131312d313131312d"
    "313131312d31313131313131313131313100000a0010000f00080004000a00000020000000bc00000000000000"
    "140032002c0020001c0010000f000e000800070014000000000000000200000000000200000000000000000000"
    "0000001c0000000000000080842e41000000002c0000000000060008000400060000000400000010000000666f"
    "7277617264696e675f696e6465780000000098ffffff000000000202020b0c00000018000000000000200a0000"
    "00323033312d30362d313500000a000000323032362d30362d3135000000000e0020001c001000080007000600"
    "0e0000000000020eec51b81e85eba13f0000000080842e41000000001800000014001800170010000c000b000a"
    "000900080007001400000000000000020202000c00000018000000000000200a000000323033312d30362d3135"
    "00000a000000323032362d30362d3135000000001600140010000c000000080000000000000000000400160000"
    "001c0000002c000000f8010000040200000c000a0009000800070006000c0000000000000000000a000c000800"
    "000004000a000000080000004801000002000000a00000000400000078ffffff100000001c000000000004004c"
    "0000000a000000323032362d30352d31330000010000000400000068ffffff080000000000000160ffffff0001"
    "0220020000000c0000006abc74931804a63fd0feffff00000008010000002400000032323232323232322d3232"
    "32322d323232322d323232322d323232323232323232323232000000001000140010000f000e000d0008000400"
    "10000000100000001c00000000000400640000000a000000323032362d30352d31330000010000000c00000008"
    "000c000b0004000800000018000000000000011000180010000c00080007000600050010000000000102200200"
    "00000c000000c3f5285c8fc2a53f80ffffff00000008010000002400000031313131313131312d313131312d31"
    "3131312d313131312d31313131313131313131313100000000010000001c000000180020001c00180017001000"
    "0c000b000a00090008000400180000001c00000000000220020000002000000000000000240000002c00000003"
    "0000004555520008000c00080007000800000000000005060000000700000045757269626f720010000000666f"
    "7277617264696e675f696e646578000000000a000000323032362d30352d313300000a000000323032362d3035"
    "2d31330000"
)


def test_build_request_bytes_unchanged_pre_post_16c() -> None:
    """The EUR-6M fixture produces stable engine bytes (now with include_flows).

    The role-split (curves consumed by role) + the resolved-index path are
    a pure refactor here: the role map points discount/forwarding at the same
    resolved ids the positional convention chose, and no resolved index is
    supplied so the interim default forwarding index is emitted unchanged. The
    golden bytes were re-captured when ``build_swap_ir_request`` began setting
    ``include_flows = True`` (the only intended drift vs. the pre-flows golden).
    """

    discount_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    forwarding_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
    discount = _resolved_curve(
        discount_id, name="USD-DISC", points=[_deposit_point(quote_id=None, rate=0.0425)]
    )
    forwarding = _resolved_curve(
        forwarding_id, name="USD-FWD", points=[_deposit_point(quote_id=None, rate=0.0430)]
    )
    resolved = ResolvedMarketData(
        as_of="2026-05-13",
        curves=(discount, forwarding),
        quotes=(),
        curve_roles={
            CurveRole.DISCOUNT: resolved_curve_id(discount),
            CurveRole.FORWARDING: resolved_curve_id(forwarding),
        },
    )
    trade = IrSwapTrade(
        swap_id=None,
        name="t-1",
        swap={
            "notional": 1_000_000.0,
            "fixed_rate": 0.035,
            "effective_date": "2026-06-15",
            "termination_date": "2031-06-15",
        },
    )
    raw = build_swap_ir_request([trade], resolved=resolved)
    assert raw.hex() == _SWAP_IR_GOLDEN_HEX


# Schedule vtable field offsets (from the generated ``Schedule`` reader): a
# non-zero table offset means the field is physically PRESENT on the wire, as
# opposed to the reader's default-0 return for an absent field.
_SCHED_OFFSET_FREQUENCY = 10
_SCHED_OFFSET_CONVENTION = 12
_SCHED_OFFSET_TERMINATION_CONVENTION = 14
_SCHED_OFFSET_DATE_GENERATION_RULE = 16


def test_swap_ir_wire_forces_schedule_conventions() -> None:
    """the encoded wire physically carries every zero-default schedule enum.

    Engine 0.2.0 rejects an OMITTED ``Schedule.frequency`` /``convention`` /
    ``termination_date_convention`` /``date_generation_rule`` (presence-based
    wire). ``Frequency.Annual == 0`` and ``BusinessDayConvention`` /
    ``DateGenerationRule`` members the swap uses are also 0, so without
    ``builder.ForceDefaults(True)`` FlatBuffers would drop them. Assert directly
    on the vtable that each is present (table offset!= 0) — the reader's plain
    accessor returns 0 for BOTH absent and present-zero, so only the offset
    distinguishes them.

    NOTE (task): a hermetic test cannot prove the wire *contract* — it only
    proves the orchestrator emits these bytes. That the engine reads them as the
    intended conventions (identical NPV on 0.1.1 and 0.2.0) is proven by the
    live A/B, not here.
    """

    discount_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    discount = _resolved_curve(
        discount_id, name="USD-DISC", points=[_deposit_point(quote_id=None, rate=0.0425)]
    )
    resolved = ResolvedMarketData(
        as_of="2026-05-13",
        curves=(discount,),
        quotes=(),
        curve_roles={CurveRole.DISCOUNT: resolved_curve_id(discount)},
    )
    trade = IrSwapTrade(
        swap_id=None,
        name="t-1",
        swap={
            "notional": 1_000_000.0,
            "fixed_rate": 0.035,
            "effective_date": "2026-06-15",
            "termination_date": "2031-06-15",
        },
    )
    raw = build_swap_ir_request([trade], resolved=resolved)

    request = PriceVanillaSwapRequest.GetRootAs(raw, 0)
    swap = request.Swaps(0)
    assert swap is not None
    for leg in (swap.VanillaSwap().FixedLeg(), swap.VanillaSwap().FloatingLeg()):
        schedule = leg.Schedule()
        tab = schedule._tab  # vtable presence check is the whole point
        assert tab.Offset(_SCHED_OFFSET_FREQUENCY) != 0, "frequency missing on wire"
        assert tab.Offset(_SCHED_OFFSET_CONVENTION) != 0, "convention missing on wire"
        assert tab.Offset(_SCHED_OFFSET_TERMINATION_CONVENTION) != 0, (
            "termination_date_convention missing on wire"
        )
        assert tab.Offset(_SCHED_OFFSET_DATE_GENERATION_RULE) != 0, (
            "date_generation_rule missing on wire"
        )
    # And the values decode to the intended conventions (Annual fixed leg).
    assert swap.VanillaSwap().FixedLeg().Schedule().Frequency() == Frequency.Annual


# ---------------------------------------------------------------------------
# Per-period cashflows (flows): request flag + decode mapping
# ---------------------------------------------------------------------------


def test_build_request_sets_include_flows() -> None:
    """``build_swap_ir_request`` flips ``include_flows`` on so the engine emits flows."""

    raw = build_swap_ir_request([_trade()], resolved=_resolved())
    request = PriceVanillaSwapRequest.GetRootAs(raw, 0)
    assert request.IncludeFlows() is True


def test_decode_response_maps_per_leg_flows() -> None:
    """``decode_swap_ir_response`` maps both flow vectors into typed ``SwapFlow``s."""

    decoded = decode_swap_ir_response(_make_response_bytes_with_flows(), expected_count=1)
    assert len(decoded) == 1
    result = decoded[0]

    assert len(result.fixed_leg_flows) == 2
    assert len(result.floating_leg_flows) == 2

    first_fixed = result.fixed_leg_flows[0]
    assert isinstance(first_fixed, SwapFlow)
    assert first_fixed.payment_date == "2027-02-11"
    assert first_fixed.accrual_start_date == "2026-02-11"
    assert first_fixed.accrual_end_date == "2027-02-11"
    assert first_fixed.amount == pytest.approx(-250000.0)
    assert first_fixed.discount == pytest.approx(0.98)
    assert first_fixed.present_value == pytest.approx(-245000.0)
    assert first_fixed.rate == pytest.approx(0.025)

    first_float = result.floating_leg_flows[0]
    assert first_float.payment_date == "2026-08-11"
    assert first_float.fixing_date == "2026-02-09"
    assert first_float.index_fixing == pytest.approx(0.026)
    assert first_float.present_value == pytest.approx(128700.0)
    # Existing headline fields stay intact alongside the flows.
    assert result.npv == pytest.approx(1000.0)
    assert [e["role"] for e in result.leg_npvs] == ["fixed", "floating"]


def test_decode_response_without_flows_yields_empty_lists() -> None:
    """A response built without flows decodes to empty flow lists (back-compat)."""

    decoded = decode_swap_ir_response(_make_response_bytes([12345.5]), expected_count=1)
    assert decoded[0].fixed_leg_flows == []
    assert decoded[0].floating_leg_flows == []


def test_ir_swap_result_serializes_flows() -> None:
    """``IrSwapResult`` round-trips flows through JSON serialization."""

    result = decode_swap_ir_response(_make_response_bytes_with_flows(), expected_count=1)[0]
    dumped = result.model_dump(mode="json")
    assert len(dumped["fixed_leg_flows"]) == 2
    assert len(dumped["floating_leg_flows"]) == 2
    assert dumped["fixed_leg_flows"][0]["payment_date"] == "2027-02-11"
    assert dumped["floating_leg_flows"][0]["fixing_date"] == "2026-02-09"


async def test_price_swap_ir_batch_returns_flows() -> None:
    """The full batch path surfaces non-empty per-leg flows from the engine."""

    engine = _FakeEngine(response=_make_response_bytes_with_flows())
    batch: EngineBatch[IrSwapTrade] = EngineBatch(
        trades=(_trade("a"),),
        shared_inputs={"as_of": "2026-05-13"},
    )
    results = await price_swap_ir_batch(engine, batch, resolved=_resolved())
    assert len(results) == 1
    assert len(results[0].fixed_leg_flows) == 2
    assert len(results[0].floating_leg_flows) == 2
    # The request the batch sent asked for flows.
    _, request_bytes = engine.calls[0]
    assert PriceVanillaSwapRequest.GetRootAs(request_bytes, 0).IncludeFlows() is True
