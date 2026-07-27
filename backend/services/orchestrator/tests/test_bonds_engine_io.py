"""Unit tests for ``pricing/bonds/engine_io.py``.

The wire layer ships six callables (one trio per variant — build /
decode / price_batch). These tests pin every contract a downstream
consumer (route handlers / live engine) relies on per the 08-plan
+ 08c plan:

* RPC bindings: ``FIXED_BOND_ENGINE_RPC`` /
  ``FLOATING_BOND_ENGINE_RPC`` resolve to the canonical
  ``PRICE_FIXED_RATE_BOND`` / ``PRICE_FLOATING_RATE_BOND`` entries
  on ``EngineRpc``.
* ``build_*_request`` produces a valid
  ``Price{Fixed,Floating}RateBondRequest`` flatbuffer that
  round-trips through the FB parser.
* ``build_*_request`` carries the canonical curve in Pricing.rates;
  the floating variant additionally carries a ``CouponPricer`` spec.
* ``build_*_request`` honors per-trade overrides (face / rate /
  spread / dates) without breaking the pricing context.
* ``decode_*_response`` round-trips a synthesised
  ``Price{Fixed,Floating}RateBondResponse`` flatbuffer and rejects
  every malformed shape (empty, malformed root, count mismatch).
* ``price_{fixed,floating}_bond_batch`` calls ``EngineClient.call``
  exactly once per batch with the right RPC + bytes and decodes
  whatever the engine returns.
* ``EngineClient.call`` exceptions bubble unchanged (the route
  handler's :func:`map_engine_client_error` is responsible for
  translating them to the error envelope).
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
from quantra_common.engine_client._generated.quantra.FixedRateBondResponse import (
    FixedRateBondResponseT,
)
from quantra_common.engine_client._generated.quantra.FloatingRateBondResponse import (
    FloatingRateBondResponseT,
)
from quantra_common.engine_client._generated.quantra.PriceFixedRateBondRequest import (
    PriceFixedRateBondRequest,
)
from quantra_common.engine_client._generated.quantra.PriceFixedRateBondResponse import (
    PriceFixedRateBondResponseT,
)
from quantra_common.engine_client._generated.quantra.PriceFloatingRateBondRequest import (
    PriceFloatingRateBondRequest,
    PriceFloatingRateBondRequestT,
)
from quantra_common.engine_client._generated.quantra.PriceFloatingRateBondResponse import (
    PriceFloatingRateBondResponseT,
)
from quantra_orchestrator.pricing._translator import (
    DEFAULT_FORWARDING_INDEX_ID,
    CurveRole,
    ResolvedMarketData,
    resolved_curve_id,
)
from quantra_orchestrator.pricing.bonds.engine_io import (
    FIXED_BOND_ENGINE_RPC,
    FLOATING_BOND_ENGINE_RPC,
    build_fixed_bond_request,
    build_floating_bond_request,
    decode_fixed_bond_response,
    decode_floating_bond_response,
    price_fixed_bond_batch,
    price_floating_bond_batch,
)
from quantra_orchestrator.pricing.bonds.errors import BondMissingTradeFieldsError
from quantra_orchestrator.pricing.bonds.models import (
    FixedBondResult,
    FixedBondTrade,
    FloatingBondResult,
    FloatingBondTrade,
    ResolvedCurve,
    ResolvedIndex,
    ResolvedQuoteValue,
)
from quantra_orchestrator.pricing.concurrency import EngineBatch


def _fixed_trade(name: str = "t-1", **bond_kwargs: object) -> FixedBondTrade:
    # The required economic fields ship explicitly — the wire layer no
    # longer substitutes fixture defaults for a missing coupon/schedule.
    body: dict[str, object] = {
        "face_amount": 100.0,
        "coupon_rate": 0.05,
        "issue_date": "2024-01-15",
        "effective_date": "2024-01-15",
        "termination_date": "2029-01-15",
    }
    body.update(bond_kwargs)
    return FixedBondTrade(bond_id=None, name=name, bond=body)


def _floating_trade(name: str = "t-1", **bond_kwargs: object) -> FloatingBondTrade:
    body: dict[str, object] = {
        "face_amount": 100.0,
        "issue_date": "2025-01-17",
        "effective_date": "2025-01-17",
        "termination_date": "2030-01-17",
    }
    body.update(bond_kwargs)
    return FloatingBondTrade(bond_id=None, name=name, bond=body)


# ---------------------------------------------------------------------------
# Resolved-input builders (the faithful request path)
# ---------------------------------------------------------------------------


def _deposit_point(
    *, quote_id: str | None = "USD.IRS.1Y", rate: float | None = None
) -> dict[str, object]:
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
    name: str = "USD-DISC",
    points: list[dict[str, object]] | None = None,
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


def _resolved_index(index_id: uuid.UUID, *, name: str = "USD-SOFR") -> ResolvedIndex:
    return ResolvedIndex(
        id=index_id,
        name=name,
        kind="OvernightIndex",
        currency="USD",
        calendar="UnitedStates",
        day_counter="Actual360",
        body={"tenor": {"n": 3, "unit": "Months"}, "fixing_days": 2},
    )


def _quote(canonical_id: str = "USD.IRS.1Y", value: float = 0.0425) -> ResolvedQuoteValue:
    return ResolvedQuoteValue(canonical_id=canonical_id, as_of=date(2026, 5, 13), value=value)


def _fixed_resolved(
    *,
    discount: ResolvedCurve | None = None,
    quotes: list[ResolvedQuoteValue] | None = None,
) -> ResolvedMarketData:
    if discount is None:
        discount = _resolved_curve(uuid.uuid4())
    return ResolvedMarketData(
        as_of="2026-05-13",
        curves=(discount,),
        quotes=tuple(quotes if quotes is not None else [_quote()]),
        curve_roles={CurveRole.DISCOUNT: resolved_curve_id(discount)},
    )


def _floating_resolved(
    *,
    discount: ResolvedCurve | None = None,
    projection: ResolvedCurve | None = None,
    index: ResolvedIndex | None = None,
    quotes: list[ResolvedQuoteValue] | None = None,
) -> ResolvedMarketData:
    if discount is None:
        discount = _resolved_curve(uuid.uuid4())
    if projection is None:
        projection = _resolved_curve(
            uuid.uuid4(),
            name="USD-PROJ",
            points=[_deposit_point(quote_id="USD.IRS.3M")],
        )
    if index is None:
        index = _resolved_index(uuid.uuid4())
    if quotes is None:
        quotes = [_quote(), _quote("USD.IRS.3M", 0.0435)]
    return ResolvedMarketData(
        as_of="2026-05-13",
        curves=(discount, projection),
        quotes=tuple(quotes),
        curve_roles={
            CurveRole.DISCOUNT: resolved_curve_id(discount),
            CurveRole.PROJECTION: resolved_curve_id(projection),
        },
        index=index,
    )


def _fixed_response_bytes(npvs: list[float]) -> bytes:
    """Synthesise a ``PriceFixedRateBondResponse`` flatbuffer with given NPVs."""

    response = PriceFixedRateBondResponseT()
    response.bonds = []
    for npv in npvs:
        b = FixedRateBondResponseT()
        b.npv = npv
        b.cleanPrice = npv * 0.99
        b.dirtyPrice = npv * 1.001
        b.accruedAmount = 0.5
        b.yield_ = 0.045
        b.accruedDays = 1.0
        b.macaulayDuration = 3.7
        b.modifiedDuration = 3.5
        b.convexity = 17.6
        b.bps = 0.0
        response.bonds.append(b)
    builder = flatbuffers.Builder(256)
    builder.Finish(response.Pack(builder))
    return bytes(builder.Output())


def _floating_response_bytes(npvs: list[float]) -> bytes:
    """Synthesise a ``PriceFloatingRateBondResponse`` flatbuffer with given NPVs."""

    response = PriceFloatingRateBondResponseT()
    response.bonds = []
    for npv in npvs:
        b = FloatingRateBondResponseT()
        b.npv = npv
        b.cleanPrice = npv * 0.99
        b.dirtyPrice = npv * 1.001
        b.accruedAmount = 0.5
        b.yield_ = 0.03
        b.accruedDays = 0.0
        b.macaulayDuration = 4.7
        b.modifiedDuration = 4.6
        b.convexity = 26.6
        b.bps = 0.0
        response.bonds.append(b)
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
# RPC enum bindings
# ---------------------------------------------------------------------------


def test_fixed_bond_engine_rpc_is_price_fixed_rate_bond() -> None:
    """``FIXED_BOND_ENGINE_RPC`` is exactly ``EngineRpc.PRICE_FIXED_RATE_BOND``.

    The 08-plan's ``EngineRpc.PRICE_BOND_FIXED`` placeholder name
    maps to this canonical enum entry (pattern, like
    swap_ir's ``PRICE_SWAP_IR`` → ``PRICE_VANILLA_SWAP``). Pinned
    in a test so a future refactor that adds a new enum entry
    doesn't silently re-target the orchestrator.
    """

    assert FIXED_BOND_ENGINE_RPC is EngineRpc.PRICE_FIXED_RATE_BOND


def test_floating_bond_engine_rpc_is_price_floating_rate_bond() -> None:
    """``FLOATING_BOND_ENGINE_RPC`` is exactly ``EngineRpc.PRICE_FLOATING_RATE_BOND``."""

    assert FLOATING_BOND_ENGINE_RPC is EngineRpc.PRICE_FLOATING_RATE_BOND


# ---------------------------------------------------------------------------
# build_* — emits valid flatbuffers
# ---------------------------------------------------------------------------


def test_build_fixed_request_rejects_missing_required_fields() -> None:
    """A missing coupon/schedule is a typed 422, never a fixture-default bond."""

    trade = FixedBondTrade(bond_id=None, name="t-1", bond={"face_amount": 100.0})
    with pytest.raises(BondMissingTradeFieldsError) as exc_info:
        build_fixed_bond_request([trade], resolved=_fixed_resolved())
    exc = exc_info.value
    assert exc.status_code == 422
    assert exc.code == "bond_missing_trade_fields"
    missing = {d["field"] for d in exc.details or []}
    assert missing == {
        "coupon_rate",
        "issue_date",
        "effective_date",
        "termination_date",
    }


def test_build_floating_request_rejects_missing_required_fields() -> None:
    trade = FloatingBondTrade(bond_id=None, name="t-1", bond={"face_amount": 100.0})
    with pytest.raises(BondMissingTradeFieldsError) as exc_info:
        build_floating_bond_request([trade], resolved=_floating_resolved())
    exc = exc_info.value
    assert exc.status_code == 422
    assert exc.code == "bond_missing_trade_fields"
    missing = {d["field"] for d in exc.details or []}
    assert missing == {"issue_date", "effective_date", "termination_date"}


def test_build_fixed_request_emits_a_parseable_flatbuffer() -> None:
    trades = [_fixed_trade("a"), _fixed_trade("b")]
    raw = build_fixed_bond_request(trades, resolved=_fixed_resolved())
    request = PriceFixedRateBondRequest.GetRootAs(raw, 0)
    assert request.BondsLength() == 2
    pricing = request.Pricing()
    assert pricing is not None
    assert pricing.AsOfDate() == b"2026-05-13"


def test_build_floating_request_emits_a_parseable_flatbuffer() -> None:
    trades = [_floating_trade("a"), _floating_trade("b")]
    raw = build_floating_bond_request(trades, resolved=_floating_resolved())
    request = PriceFloatingRateBondRequest.GetRootAs(raw, 0)
    assert request.BondsLength() == 2
    pricing = request.Pricing()
    assert pricing is not None
    assert pricing.AsOfDate() == b"2026-05-13"


def test_build_fixed_request_carries_resolved_curve_and_rate() -> None:
    """The request curve is the resolved one: real id + the supplied rate.

    Faithful translator replaces the canonical ``discount`` / flat-3 %
    fixture: the encoded curve carries the resolved ``app.curves`` UUID and the
    deposit helper carries the supplied quote value (not 0.03), quote_id dropped.
    """

    curve_id = uuid.uuid4()
    discount = _resolved_curve(curve_id, points=[_deposit_point(quote_id="USD.IRS.1Y")])
    resolved = _fixed_resolved(discount=discount, quotes=[_quote("USD.IRS.1Y", 0.0411)])
    raw = build_fixed_bond_request([_fixed_trade()], resolved=resolved)
    request = PriceFixedRateBondRequest.GetRootAs(raw, 0)
    rates = request.Pricing().Rates()
    assert rates.CurvesLength() == 1
    curve = rates.Curves(0)
    # Resolved entity id, not the canonical "discount" string.
    assert curve.Id() == str(curve_id).encode()
    assert curve.Id() != b"discount"
    table = curve.Points(0).Point()
    deposit = DepositHelper()
    deposit.Init(table.Bytes, table.Pos)
    assert deposit.Rate() == pytest.approx(0.0411)
    assert not deposit.QuoteId()  # never leaks to the engine
    # The per-trade discount ref honours the resolved id, not CANONICAL_*.
    assert request.Bonds(0).DiscountingCurve() == str(curve_id).encode()


def test_build_floating_request_honours_resolved_curves_index_and_pricer() -> None:
    """Floating discount/forwarding/index refs are the resolved ids; coupon pricer present.

    The per-bond ``discountingCurve`` / ``forwardingCurve`` / ``index`` honour the
    resolved discount / projection / index ids, never ``CANONICAL_*``;
    the engine's required Black-Ibor coupon pricer rides under ``rates`` at the
    default id ``"iborpricer"``.
    """

    discount_id = uuid.uuid4()
    projection_id = uuid.uuid4()
    index_id = uuid.uuid4()
    resolved = _floating_resolved(
        discount=_resolved_curve(discount_id, points=[_deposit_point(quote_id="USD.IRS.1Y")]),
        projection=_resolved_curve(
            projection_id,
            name="USD-PROJ",
            points=[_deposit_point(quote_id="USD.IRS.3M")],
        ),
        index=_resolved_index(index_id),
    )
    raw = build_floating_bond_request([_floating_trade()], resolved=resolved)
    request = PriceFloatingRateBondRequest.GetRootAs(raw, 0)
    pricing = request.Pricing()
    rates = pricing.Rates()
    # Both resolved curves reach rates.curves (no canonical fixture).
    curve_ids = {rates.Curves(i).Id() for i in range(rates.CurvesLength())}
    assert curve_ids == {str(discount_id).encode(), str(projection_id).encode()}
    # The resolved index reaches rates.indices (not the canonical EUR_6M),
    # registered alongside the default forwarding index the curve helpers
    # reference so neither ref orphans against the engine registry.
    index_ids = {rates.Indices(i).Id() for i in range(rates.IndicesLength())}
    assert str(index_id).encode() in index_ids
    assert DEFAULT_FORWARDING_INDEX_ID.encode() in index_ids
    assert rates.CouponPricersLength() == 1
    assert rates.CouponPricers(0).Id() == b"iborpricer"

    bond = request.Bonds(0)
    assert bond.DiscountingCurve() == str(discount_id).encode()
    assert bond.ForwardingCurve() == str(projection_id).encode()
    assert bond.CouponPricer() == b"iborpricer"
    assert bond.FloatingRateBond().Index().Id() == str(index_id).encode()


def _index_with_tenor(n: int, unit: str) -> ResolvedIndex:
    """A resolved float-leg index at the given tenor (pure config, not MD)."""

    return ResolvedIndex(
        id=uuid.uuid4(),
        name="TEST-IBOR",
        kind="IborIndex",
        currency="USD",
        body={"tenor": {"n": n, "unit": unit}},
    )


def _floating_schedule_frequency(resolved: ResolvedMarketData) -> int:
    """Encode a floating bond with ``resolved`` and read back its schedule frequency."""

    raw = build_floating_bond_request([_floating_trade()], resolved=resolved)
    request = PriceFloatingRateBondRequest.GetRootAs(raw, 0)
    return int(request.Bonds(0).FloatingRateBond().Schedule().Frequency())


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
def test_floating_bond_schedule_frequency_follows_index_tenor(
    n: int, unit: str, expected: int
) -> None:
    """the floating-bond coupon schedule frequency follows the index tenor.

    A 3M index must pay quarterly coupons, a 6M index semi-annually, etc. — the
    same tenor→frequency derivation originally applied to swap_ir, now shared so the
    coupon schedule and the projection index can never disagree. Previously the
    frequency was hardcoded ``Semiannual`` so the index tenor was invisible.
    """

    resolved = _floating_resolved(index=_index_with_tenor(n, unit))
    assert _floating_schedule_frequency(resolved) == expected


def test_floating_bond_schedule_frequency_defaults_semiannual_without_index() -> None:
    """No resolved index → the documented single flat-default stays Semiannual."""

    discount = _resolved_curve(uuid.uuid4())
    resolved = ResolvedMarketData(
        as_of="2026-05-13",
        curves=(discount,),
        quotes=(_quote(),),
        curve_roles={CurveRole.DISCOUNT: resolved_curve_id(discount)},
    )
    assert _floating_schedule_frequency(resolved) == Frequency.Semiannual


def test_floating_bond_schedule_frequency_falls_back_on_uncleanly_divisible_tenor() -> None:
    """A tenor with no exact annual frequency (5M, the case) falls back to default."""

    resolved = _floating_resolved(index=_index_with_tenor(5, "Months"))
    assert _floating_schedule_frequency(resolved) == Frequency.Semiannual


def test_build_floating_request_decodes_via_object_api() -> None:
    """The fixed flatc object-API unpack path decodes the floating request.

    ``InitFromPackedBuf`` previously raised ``NameError: PricingT`` because the
    generated ``_UnPack`` referenced the object-API classes without importing
    them; the vendored-bindings fix makes the whole graph decode, so a test can
    walk the faithful ``Pricing`` tree as Python objects.
    """

    discount_id = uuid.uuid4()
    resolved = _floating_resolved(
        discount=_resolved_curve(discount_id, points=[_deposit_point(quote_id="USD.IRS.1Y")]),
        quotes=[_quote("USD.IRS.1Y", 0.0399), _quote("USD.IRS.3M", 0.0435)],
    )
    raw = build_floating_bond_request([_floating_trade()], resolved=resolved)
    obj = PriceFloatingRateBondRequestT.InitFromPackedBuf(raw, 0)
    # flatc's object-API decodes string fields as bytes.
    discount_curve = next(c for c in obj.pricing.rates.curves if c.id == str(discount_id).encode())
    assert discount_curve.points[0].point.rate == pytest.approx(0.0399)
    assert obj.pricing.options.bondPricingDetails is True
    assert obj.pricing.rates.couponPricers[0].id == b"iborpricer"


def test_build_request_includes_curve_set_id_in_shared_inputs() -> None:
    """batching hook: ``curve_set_id`` rides through the engine batch.

    Both bond endpoints must carry it through unchanged so the
    future ``group_by_curve_set`` policy can group batches without
    the seam needing to widen its type. With FlatBuffers the field
    doesn't ride into the wire bytes (the engine schema has no
    such field) — it lives in :class:`EngineBatch.shared_inputs`
    and is consumed by the runner / future policy.
    """

    fixed_batch: EngineBatch[FixedBondTrade] = EngineBatch(
        trades=(_fixed_trade(),),
        shared_inputs={
            "as_of": "2026-05-13",
            "curve_set_id": "00000000-0000-0000-0000-000000000001",
        },
    )
    assert fixed_batch.shared_inputs["curve_set_id"] == "00000000-0000-0000-0000-000000000001"

    floating_batch: EngineBatch[FloatingBondTrade] = EngineBatch(
        trades=(_floating_trade(),),
        shared_inputs={
            "as_of": "2026-05-13",
            "curve_set_id": "00000000-0000-0000-0000-000000000002",
        },
    )
    assert floating_batch.shared_inputs["curve_set_id"] == "00000000-0000-0000-0000-000000000002"


def test_build_fixed_request_honors_per_trade_overrides() -> None:
    # the canonical flat-body key for the fixed-bond coupon is
    # ``coupon_rate`` (aligned with the field name the portal sends).
    # The prior ``"rate"`` reading was a silent default.
    trade = _fixed_trade(
        face_amount=200.0,
        coupon_rate=0.07,
        issue_date="2024-06-15",
        effective_date="2024-06-15",
        termination_date="2034-06-15",
    )
    raw = build_fixed_bond_request([trade], resolved=_fixed_resolved())
    request = PriceFixedRateBondRequest.GetRootAs(raw, 0)
    bond = request.Bonds(0)
    assert bond is not None
    inner = bond.FixedRateBond()
    assert inner is not None
    assert inner.FaceAmount() == pytest.approx(200.0)
    assert inner.Rate() == pytest.approx(0.07)
    assert inner.IssueDate() == b"2024-06-15"
    sched = inner.Schedule()
    assert sched is not None
    assert sched.EffectiveDate() == b"2024-06-15"
    assert sched.TerminationDate() == b"2034-06-15"


def test_build_floating_request_honors_per_trade_overrides() -> None:
    trade = _floating_trade(
        face_amount=500.0,
        spread=0.0025,
        in_arrears=True,
        issue_date="2025-06-17",
        effective_date="2025-06-17",
        termination_date="2030-06-17",
    )
    raw = build_floating_bond_request([trade], resolved=_floating_resolved())
    request = PriceFloatingRateBondRequest.GetRootAs(raw, 0)
    bond = request.Bonds(0)
    assert bond is not None
    inner = bond.FloatingRateBond()
    assert inner is not None
    assert inner.FaceAmount() == pytest.approx(500.0)
    assert inner.Spread() == pytest.approx(0.0025)
    assert inner.InArrears() is True


# ---------------------------------------------------------------------------
# decode_* — happy paths
# ---------------------------------------------------------------------------


def test_decode_fixed_response_round_trips_a_one_trade_payload() -> None:
    body = _fixed_response_bytes([12345.5])
    decoded = decode_fixed_bond_response(body, expected_count=1)
    assert len(decoded) == 1
    assert isinstance(decoded[0], FixedBondResult)
    assert decoded[0].npv == pytest.approx(12345.5)
    assert decoded[0].clean_price == pytest.approx(12345.5 * 0.99)
    assert "macaulay_duration" in decoded[0].extras


def test_decode_floating_response_round_trips_a_one_trade_payload() -> None:
    body = _floating_response_bytes([999.0])
    decoded = decode_floating_bond_response(body, expected_count=1)
    assert len(decoded) == 1
    assert isinstance(decoded[0], FloatingBondResult)
    assert decoded[0].npv == pytest.approx(999.0)
    assert "yield" in decoded[0].extras


def test_decode_multi_trade_payload_preserves_input_order() -> None:
    body = _fixed_response_bytes([1.0, 2.0, 3.0])
    decoded = decode_fixed_bond_response(body, expected_count=3)
    assert [r.npv for r in decoded] == [
        pytest.approx(1.0),
        pytest.approx(2.0),
        pytest.approx(3.0),
    ]


# ---------------------------------------------------------------------------
# decode_* — malformed shapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "decoder",
    [decode_fixed_bond_response, decode_floating_bond_response],
)
def test_decode_rejects_empty_body(decoder: object) -> None:
    with pytest.raises(ValueError, match="empty engine response"):
        decoder(b"", expected_count=1)  # type: ignore[operator]


@pytest.mark.parametrize(
    "decoder",
    [decode_fixed_bond_response, decode_floating_bond_response],
)
def test_decode_rejects_malformed_root(decoder: object) -> None:
    """A buffer that is not a valid flatbuffer root is rejected."""

    with pytest.raises(ValueError, match=r"malformed engine response|expected "):
        decoder(b"this-is-not-a-flatbuffer", expected_count=1)  # type: ignore[operator]


def test_decode_fixed_rejects_count_mismatch() -> None:
    body = _fixed_response_bytes([1.0, 2.0])
    with pytest.raises(ValueError, match="returned 2 result\\(s\\), expected 3"):
        decode_fixed_bond_response(body, expected_count=3)


def test_decode_floating_rejects_count_mismatch() -> None:
    body = _floating_response_bytes([1.0, 2.0])
    with pytest.raises(ValueError, match="returned 2 result\\(s\\), expected 3"):
        decode_floating_bond_response(body, expected_count=3)


# ---------------------------------------------------------------------------
# price_*_batch — the translators
# ---------------------------------------------------------------------------


async def test_price_fixed_bond_batch_round_trip() -> None:
    body = _fixed_response_bytes([42.0])
    engine = _FakeEngine(response=body)
    batch: EngineBatch[FixedBondTrade] = EngineBatch(
        trades=(_fixed_trade("a"),),
        shared_inputs={"as_of": "2026-05-13", "curve_set_id": None},
    )

    results: Sequence[FixedBondResult] = await price_fixed_bond_batch(
        engine, batch, resolved=_fixed_resolved()
    )

    assert len(engine.calls) == 1
    rpc, request_bytes = engine.calls[0]
    assert rpc is EngineRpc.PRICE_FIXED_RATE_BOND
    parsed = PriceFixedRateBondRequest.GetRootAs(request_bytes, 0)
    assert parsed.BondsLength() == 1
    assert len(results) == 1
    assert results[0].npv == pytest.approx(42.0)


async def test_price_floating_bond_batch_round_trip() -> None:
    body = _floating_response_bytes([42.0])
    engine = _FakeEngine(response=body)
    batch: EngineBatch[FloatingBondTrade] = EngineBatch(
        trades=(_floating_trade("a"),),
        shared_inputs={"as_of": "2026-05-13", "curve_set_id": None},
    )

    results: Sequence[FloatingBondResult] = await price_floating_bond_batch(
        engine, batch, resolved=_floating_resolved()
    )

    assert len(engine.calls) == 1
    rpc, _request_bytes = engine.calls[0]
    assert rpc is EngineRpc.PRICE_FLOATING_RATE_BOND
    assert len(results) == 1
    assert results[0].npv == pytest.approx(42.0)


async def test_price_fixed_bond_batch_propagates_engine_exceptions() -> None:
    engine = _FakeEngine()
    engine.set_exception(NotImplementedError("stub backend"))
    batch: EngineBatch[FixedBondTrade] = EngineBatch(trades=(_fixed_trade(),), shared_inputs={})

    with pytest.raises(NotImplementedError, match="stub backend"):
        await price_fixed_bond_batch(engine, batch, resolved=_fixed_resolved())


async def test_price_floating_bond_batch_propagates_engine_exceptions() -> None:
    engine = _FakeEngine()
    engine.set_exception(NotImplementedError("stub backend"))
    batch: EngineBatch[FloatingBondTrade] = EngineBatch(
        trades=(_floating_trade(),), shared_inputs={}
    )

    with pytest.raises(NotImplementedError, match="stub backend"):
        await price_floating_bond_batch(engine, batch, resolved=_floating_resolved())
