"""Unit tests for ``pricing/cds/engine_io.py`` (the faithful request path).

The wire layer ships three callables (build / decode / price_batch).
These tests pin every contract a downstream consumer (route handler /
live engine) relies on:

* RPC bindings: ``CDS_ENGINE_RPC`` resolves to the canonical
  :attr:`EngineRpc.PRICE_CDS`.
* :func:`build_cds_request` produces a valid ``PriceCDSRequest``
  flatbuffer that round-trips through the FB parser.
* :func:`build_cds_request` builds the market-data context
  **faithfully** from the route-captured :class:`ResolvedMarketData`
  : the discount curve carries the resolved entity id +
  the supplied quote value (not the canonical flat-3 % fixture), and the
  credit curve carries the supplied hazard inputs (flat-hazard or inline
  par-spread bootstrap) + recovery rate (not the canonical 2 % / 40 %).
* :func:`build_cds_request` honours the inline par-spread bootstrap
  invariant: when it emits inline quotes it pins ``flat_hazard_rate == 0``
  so the engine bootstraps the supplied term structure
  rather than silently overriding it with a flat curve.
* Each ``PriceCDS`` references the resolved discount / credit-curve ids
 + the default MidPoint model id — never ``CANONICAL_*``.
* :func:`build_cds_request` honours per-trade overrides (notional,
  running coupon, schedule dates, &c.), the nested ``cds.cds["cds"]``
  shape, and the as_of date defaults.
* A discount-curve quote with no resolved value → ``cds_quote_resolution_failed``;
  a missing credit curve → ``cds_credit_curve_resolution_failed``.
* :func:`decode_cds_response` round-trips a synthesised
  ``PriceCDSResponse`` and rejects malformed shapes.
* :func:`price_cds_batch` calls ``EngineClient.call`` exactly once per
  batch with the right RPC + bytes and decodes whatever the engine returns.
* :func:`price_cds_batch` propagates engine exceptions unchanged.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date

import flatbuffers
import pytest

from quantra_common.engine_client import EngineClient, EngineRpc
from quantra_common.engine_client._generated.quantra.CDSValues import CDSValuesT
from quantra_common.engine_client._generated.quantra.DepositHelper import DepositHelper
from quantra_common.engine_client._generated.quantra.enums.CdsQuoteType import (
    CdsQuoteType,
)
from quantra_common.engine_client._generated.quantra.enums.ProtectionSide import (
    ProtectionSide,
)
from quantra_common.engine_client._generated.quantra.enums.TimeUnit import TimeUnit
from quantra_common.engine_client._generated.quantra.Error import ErrorT
from quantra_common.engine_client._generated.quantra.PriceCDSRequest import (
    PriceCDSRequest,
)
from quantra_common.engine_client._generated.quantra.PriceCDSResponse import (
    PriceCDSResponseT,
)
from quantra_orchestrator.pricing._translator import (
    CurveRole,
    ResolvedMarketData,
    resolved_curve_id,
)
from quantra_orchestrator.pricing.cds.engine_io import (
    CDS_ENGINE_RPC,
    build_cds_request,
    decode_cds_response,
    price_cds_batch,
)
from quantra_orchestrator.pricing.cds.errors import (
    CdsCreditCurveResolutionFailedError,
    CdsMissingTradeFieldsError,
    CdsQuoteResolutionFailedError,
)
from quantra_orchestrator.pricing.cds.models import (
    CdsResult,
    CdsTrade,
    ResolvedCreditCurve,
    ResolvedCurve,
    ResolvedQuoteValue,
)
from quantra_orchestrator.pricing.concurrency import EngineBatch


def _cds_trade(name: str = "cds-1", **cds_kwargs: object) -> CdsTrade:
    # Explicit economics — the wire layer no longer supplies silent
    # defaults (422 ``cds_missing_trade_fields`` otherwise). Values match
    # the historical fixture defaults so byte assertions are unchanged.
    body: dict[str, object] = {
        "notional": 10_000_000.0,
        "running_coupon": 0.01,
        "schedule": {
            "effective_date": "2025-01-15",
            "termination_date": "2030-01-15",
        },
    }
    body.update(cds_kwargs)
    return CdsTrade(cds_id=None, name=name, cds=body)


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


def _resolved_discount(
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


def _resolved_credit(
    credit_id: uuid.UUID | None,
    *,
    name: str = "ACME-SR",
    recovery_rate: float = 0.4,
    body: dict[str, object] | None = None,
) -> ResolvedCreditCurve:
    return ResolvedCreditCurve(
        id=credit_id,
        name=name,
        reference_entity="ACME",
        currency="USD",
        seniority="Senior Unsecured",
        source="flat",
        recovery_rate=recovery_rate,
        body=body if body is not None else {"flat_hazard_rate": 0.03},
    )


def _quote(canonical_id: str = "USD.IRS.1Y", value: float = 0.0425) -> ResolvedQuoteValue:
    return ResolvedQuoteValue(canonical_id=canonical_id, as_of=date(2026, 5, 13), value=value)


def _resolved(
    *,
    discount: ResolvedCurve | None = None,
    credit: ResolvedCreditCurve | None = None,
    quotes: list[ResolvedQuoteValue] | None = None,
) -> ResolvedMarketData:
    if discount is None:
        discount = _resolved_discount(uuid.uuid4())
    if credit is None:
        credit = _resolved_credit(uuid.uuid4())
    return ResolvedMarketData(
        as_of="2026-05-13",
        curves=(discount,),
        quotes=tuple(quotes if quotes is not None else [_quote()]),
        curve_roles={CurveRole.DISCOUNT: resolved_curve_id(discount)},
        credit_curve=credit,
    )


def _cds_response_bytes(npvs: list[float]) -> bytes:
    """Synthesise a ``PriceCDSResponse`` flatbuffer with given NPVs."""

    response = PriceCDSResponseT()
    response.cdsList = []
    for npv in npvs:
        v = CDSValuesT()
        v.npv = npv
        v.fairSpread = 0.0125
        v.fairUpfront = 0.005
        v.defaultLegNpv = npv * 0.4
        v.premiumLegNpv = npv * 0.6
        response.cdsList.append(v)
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


def test_cds_engine_rpc_is_price_cds() -> None:
    """``CDS_ENGINE_RPC`` is exactly :attr:`EngineRpc.PRICE_CDS`."""

    assert CDS_ENGINE_RPC is EngineRpc.PRICE_CDS
    assert CDS_ENGINE_RPC.value == "PriceCDS"


# ---------------------------------------------------------------------------
# build_cds_request — flatbuffer shape
# ---------------------------------------------------------------------------


def test_build_cds_request_emits_a_parseable_flatbuffer() -> None:
    trades = [_cds_trade("a"), _cds_trade("b")]
    raw = build_cds_request(trades, {"as_of": "2026-05-13"}, resolved=_resolved())
    request = PriceCDSRequest.GetRootAs(raw, 0)
    assert request.CdsListLength() == 2
    pricing = request.Pricing()
    assert pricing is not None
    assert pricing.AsOfDate() == b"2026-05-13"


def test_build_cds_request_carries_resolved_discount_curve_and_rate() -> None:
    """The discount curve is the resolved one: real id + the supplied rate.

    The faithful translator replaces the canonical ``discount`` / flat-3 %
    fixture: the encoded curve carries the resolved ``app.curves`` UUID and the
    deposit helper carries the supplied quote value (not 0.03), quote_id dropped.
    """

    curve_id = uuid.uuid4()
    discount = _resolved_discount(curve_id, points=[_deposit_point(quote_id="USD.IRS.1Y")])
    resolved = _resolved(discount=discount, quotes=[_quote("USD.IRS.1Y", 0.0411)])
    raw = build_cds_request([_cds_trade()], {"as_of": "2026-05-13"}, resolved=resolved)
    request = PriceCDSRequest.GetRootAs(raw, 0)
    rates = request.Pricing().Rates()
    assert rates.CurvesLength() == 1
    curve = rates.Curves(0)
    assert curve.Id() == str(curve_id).encode()
    assert curve.Id() != b"discount"
    table = curve.Points(0).Point()
    deposit = DepositHelper()
    deposit.Init(table.Bytes, table.Pos)
    assert deposit.Rate() == pytest.approx(0.0411)
    assert not deposit.QuoteId()  # never leaks to the engine


def test_build_cds_request_carries_faithful_flat_hazard_credit_curve() -> None:
    """The flat-hazard credit curve carries the supplied hazard + recovery.

    Faithful translator replaces the canonical 2 % / 40 % ``credit`` fixture: the
    encoded credit curve carries the resolved ``app.credit_curves`` UUID, the
    supplied flat hazard rate, the supplied recovery rate, and **no** inline
    quotes (flat path).
    """

    credit_id = uuid.uuid4()
    credit = _resolved_credit(credit_id, recovery_rate=0.55, body={"flat_hazard_rate": 0.037})
    raw = build_cds_request(
        [_cds_trade()], {"as_of": "2026-05-13"}, resolved=_resolved(credit=credit)
    )
    request = PriceCDSRequest.GetRootAs(raw, 0)
    credit_md = request.Pricing().Credit()
    assert credit_md is not None
    assert credit_md.CreditCurvesLength() == 1
    curve = credit_md.CreditCurves(0)
    assert curve.Id() == str(credit_id).encode()
    assert curve.Id() != b"credit"
    assert curve.FlatHazardRate() == pytest.approx(0.037)
    assert curve.FlatHazardRate() != pytest.approx(0.02)  # not the canonical fixture
    assert curve.RecoveryRate() == pytest.approx(0.55)
    assert curve.RecoveryRate() != pytest.approx(0.4)
    assert curve.QuotesLength() == 0


def test_build_cds_request_bootstraps_inline_par_spread_credit_curve() -> None:
    """Inline par-spread points → ``quotes[]`` with ``flat_hazard_rate`` ABSENT.

        engine 0.2.0 credit-curve construction is presence-based
    : a PRESENT ``flat_hazard_rate`` (incl. a forced 0)
        pins a flat-hazard curve and IGNORES the quotes → a zero-hazard curve →
        protection leg silently 0. The bootstrap path therefore leaves the field
        ABSENT on the wire (reader returns ``None``) so the quotes drive the
        bootstrap. This doubles as the hermetic wire-presence assertion.
    """

    credit_id = uuid.uuid4()
    credit = _resolved_credit(
        credit_id,
        recovery_rate=0.35,
        body={
            "points": [
                {"tenor": "5Y", "quoted_par_spread": 0.012},
                {"tenor": "10Y", "quoted_par_spread": 0.015},
            ]
        },
    )
    raw = build_cds_request(
        [_cds_trade()], {"as_of": "2026-05-13"}, resolved=_resolved(credit=credit)
    )
    request = PriceCDSRequest.GetRootAs(raw, 0)
    curve = request.Pricing().Credit().CreditCurves(0)
    # inline quotes ⇒ flat_hazard_rate ABSENT on the wire (reader → None).
    assert curve.FlatHazardRate() is None
    assert curve.RecoveryRate() == pytest.approx(0.35)
    assert curve.QuotesLength() == 2
    first = curve.Quotes(0)
    assert first.QuoteType() == CdsQuoteType.ParSpread
    assert first.QuotedParSpread() == pytest.approx(0.012)
    assert first.Tenor().N() == 5
    assert first.Tenor().Unit() == TimeUnit.Years
    second = curve.Quotes(1)
    assert second.QuotedParSpread() == pytest.approx(0.015)
    assert second.Tenor().N() == 10


def test_build_cds_request_bootstraps_inline_upfront_credit_curve() -> None:
    """Inline upfront points → ``Upfront`` quotes carrying running coupon."""

    credit = _resolved_credit(
        uuid.uuid4(),
        body={
            "points": [
                {
                    "tenor": "5Y",
                    "quote_type": "Upfront",
                    "quoted_upfront": 0.08,
                    "running_coupon": 0.01,
                }
            ]
        },
    )
    raw = build_cds_request(
        [_cds_trade()], {"as_of": "2026-05-13"}, resolved=_resolved(credit=credit)
    )
    request = PriceCDSRequest.GetRootAs(raw, 0)
    curve = request.Pricing().Credit().CreditCurves(0)
    assert curve.FlatHazardRate() is None  # bootstrap path omits flat hazard
    quote = curve.Quotes(0)
    assert quote.QuoteType() == CdsQuoteType.Upfront
    assert quote.QuotedUpfront() == pytest.approx(0.08)
    assert quote.RunningCoupon() == pytest.approx(0.01)


def test_build_cds_request_carries_cds_model() -> None:
    """``Pricing.volatility.models`` carries the default MidPoint CDS model spec."""

    raw = build_cds_request([_cds_trade()], {"as_of": "2026-05-13"}, resolved=_resolved())
    request = PriceCDSRequest.GetRootAs(raw, 0)
    volatility = request.Pricing().Volatility()
    assert volatility is not None
    assert volatility.ModelsLength() >= 1
    found = any(
        volatility.Models(i).Id() == b"cds_midpoint" for i in range(volatility.ModelsLength())
    )
    assert found, "default CDS model spec missing from request"


def test_build_cds_request_per_cds_references_resolved_ids() -> None:
    """Every ``PriceCDS`` honours the resolved discount + credit ids + model id.

    No ``CANONICAL_*`` leak: the discount / credit refs are the resolved entity
    UUIDs (not ``discount`` / ``credit``); the model ref is the default config id.
    """

    curve_id = uuid.uuid4()
    credit_id = uuid.uuid4()
    resolved = _resolved(
        discount=_resolved_discount(curve_id),
        credit=_resolved_credit(credit_id),
    )
    raw = build_cds_request([_cds_trade()], {"as_of": "2026-05-13"}, resolved=resolved)
    request = PriceCDSRequest.GetRootAs(raw, 0)
    one = request.CdsList(0)
    assert one is not None
    assert one.DiscountingCurve() == str(curve_id).encode()
    assert one.CreditCurveId() == str(credit_id).encode()
    assert one.Model() == b"cds_midpoint"
    # No canonical-placeholder leak.
    assert one.DiscountingCurve() != b"discount"
    assert one.CreditCurveId() != b"credit"


def test_build_cds_request_honors_top_level_body_overrides() -> None:
    trade = _cds_trade(
        side="Seller",
        notional=5_000_000.0,
        running_coupon=0.02,
        upfront=12345.0,
        schedule={
            "effective_date": "2026-03-20",
            "termination_date": "2031-03-20",
        },
    )
    raw = build_cds_request([trade], {"as_of": "2026-03-19"}, resolved=_resolved())
    request = PriceCDSRequest.GetRootAs(raw, 0)
    one = request.CdsList(0)
    assert one is not None
    cds = one.Cds()
    assert cds is not None
    assert cds.Side() == ProtectionSide.Seller
    assert cds.Notional() == pytest.approx(5_000_000.0)
    assert cds.RunningCoupon() == pytest.approx(0.02)
    assert cds.Upfront() == pytest.approx(12345.0)
    sched = cds.Schedule()
    assert sched is not None
    assert sched.EffectiveDate() == b"2026-03-20"
    assert sched.TerminationDate() == b"2031-03-20"


def test_build_cds_request_honors_nested_cds_body_shape() -> None:
    """The engine's own ``cds_request.json`` shape nests the CDS body under ``cds``."""

    trade = _cds_trade(
        cds={
            "side": "Seller",
            "notional": 7_500_000.0,
            "running_coupon": 0.015,
            "schedule": {
                "effective_date": "2027-01-15",
                "termination_date": "2032-01-15",
            },
        },
    )
    raw = build_cds_request([trade], {"as_of": "2027-01-14"}, resolved=_resolved())
    request = PriceCDSRequest.GetRootAs(raw, 0)
    one = request.CdsList(0)
    assert one is not None
    cds = one.Cds()
    assert cds is not None
    assert cds.Side() == ProtectionSide.Seller
    assert cds.Notional() == pytest.approx(7_500_000.0)
    sched = cds.Schedule()
    assert sched is not None
    assert sched.EffectiveDate() == b"2027-01-15"


def test_build_cds_request_defaults_dates_to_as_of() -> None:
    """``protection_start`` / ``trade_date`` default to ``as_of``; ``upfront_date`` does not.

    A plain running-spread CDS (no upfront) must leave ``upfront_date`` absent so
    the engine builds the running-spread ``CreditDefaultSwap`` constructor. The
    prior behavior — defaulting ``upfront_date`` to ``as_of`` — forced the engine
    into its upfront-variant constructor, whose ``fairUpfront()`` the live engine
    cannot evaluate ("fair upfront not available"), zeroing every diagnostic
    (root cause).
    """

    raw = build_cds_request([_cds_trade()], {"as_of": "2026-05-13"}, resolved=_resolved())
    request = PriceCDSRequest.GetRootAs(raw, 0)
    one = request.CdsList(0)
    assert one is not None
    cds = one.Cds()
    assert cds is not None
    assert cds.ProtectionStart() == b"2026-05-13"
    assert cds.TradeDate() == b"2026-05-13"
    # No upfront leg → no fabricated upfront date (fix).
    assert cds.UpfrontDate() is None
    # no upfront leg → ``upfront`` ABSENT on the wire (reader → None). Under
    # ForceDefaults a present 0 would land here and, because engine 0.2.0 selects
    # the upfront-bearing constructor on PRESENCE (not value != 0), silently flip
    # a plain running-spread CDS onto the upfront path. Absence keeps the plain
    # constructor (byte-equivalent to what 0.1.1 effectively priced).
    assert cds.Upfront() is None


def test_build_cds_request_upfront_date_present_when_upfront_set() -> None:
    """A non-zero ``upfront`` brings back an ``upfront_date`` (defaults to ``as_of``).

    When the trade genuinely has an upfront leg the engine's upfront-variant
    constructor is the correct one; the orchestrator supplies the settlement
    date it needs. (The live engine's unconditional ``fairUpfront()`` on this
    path is escalated engine-side.)
    """

    trade = _cds_trade(upfront=0.05)
    raw = build_cds_request([trade], {"as_of": "2026-05-13"}, resolved=_resolved())
    request = PriceCDSRequest.GetRootAs(raw, 0)
    one = request.CdsList(0)
    assert one is not None
    cds = one.Cds()
    assert cds is not None
    assert cds.Upfront() == pytest.approx(0.05)
    assert cds.UpfrontDate() == b"2026-05-13"
    # An explicit body ``upfront_date`` is honored verbatim even with no upfront.
    explicit = _cds_trade(
        cds={
            "upfront_date": "2026-06-01",
            "notional": 10_000_000.0,
            "running_coupon": 0.01,
            "schedule": {
                "effective_date": "2025-01-15",
                "termination_date": "2030-01-15",
            },
        }
    )
    raw2 = build_cds_request([explicit], {"as_of": "2026-05-13"}, resolved=_resolved())
    cds2 = PriceCDSRequest.GetRootAs(raw2, 0).CdsList(0).Cds()
    assert cds2 is not None
    assert cds2.UpfrontDate() == b"2026-06-01"


# ---------------------------------------------------------------------------
# build_cds_request — faithful-translation failures (no canonical fallback)
# ---------------------------------------------------------------------------


def test_build_cds_request_rejects_missing_required_fields() -> None:
    """A missing notional/coupon/schedule date is a typed 422, never a fixture-default CDS.

    The schedule dates are reported with a ``schedule.`` prefix so the
    client knows the fix belongs in the nested block.
    """

    trade = CdsTrade(cds_id=None, name="cds-1", cds={"side": "Buyer"})
    with pytest.raises(CdsMissingTradeFieldsError) as exc_info:
        build_cds_request([trade], {"as_of": "2026-05-13"}, resolved=_resolved())
    exc = exc_info.value
    assert exc.status_code == 422
    assert exc.code == "cds_missing_trade_fields"
    missing = {d["field"] for d in exc.details or []}
    assert missing == {
        "notional",
        "running_coupon",
        "schedule.effective_date",
        "schedule.termination_date",
    }


def test_build_cds_request_unresolved_quote_raises_cds_422() -> None:
    """A discount-curve quote with no resolved value → ``cds_quote_resolution_failed``.

    The translator never substitutes a default for an unresolved quote;
    it surfaces the product's existing 422 instead of a canonical fixture.
    """

    discount = _resolved_discount(uuid.uuid4(), points=[_deposit_point(quote_id="USD.IRS.1Y")])
    resolved = _resolved(discount=discount, quotes=[])  # quote map empty
    with pytest.raises(CdsQuoteResolutionFailedError):
        build_cds_request([_cds_trade()], {"as_of": "2026-05-13"}, resolved=resolved)


def test_build_cds_request_missing_credit_curve_raises_cds_422() -> None:
    """No resolved credit curve → ``cds_credit_curve_resolution_failed``.

    Guards the all-zero-diagnostics root cause: a missing credit curve must fail
    fast, never reach the engine as a canonical / empty placeholder.
    """

    resolved = ResolvedMarketData(
        as_of="2026-05-13",
        curves=(_resolved_discount(uuid.uuid4()),),
        quotes=(_quote(),),
        curve_roles={},
        credit_curve=None,
    )
    with pytest.raises(CdsCreditCurveResolutionFailedError):
        build_cds_request([_cds_trade()], {"as_of": "2026-05-13"}, resolved=resolved)


# ---------------------------------------------------------------------------
# decode_cds_response — happy paths
# ---------------------------------------------------------------------------


def test_decode_cds_response_round_trips_one_trade() -> None:
    body = _cds_response_bytes([10_000.5])
    decoded = decode_cds_response(body, expected_count=1)
    assert len(decoded) == 1
    result = decoded[0]
    assert isinstance(result, CdsResult)
    assert result.npv == pytest.approx(10_000.5)
    assert result.fair_spread == pytest.approx(0.0125)
    assert result.fair_upfront == pytest.approx(0.005)
    assert result.protection_leg_npv == pytest.approx(10_000.5 * 0.4)
    assert result.premium_leg_npv == pytest.approx(10_000.5 * 0.6)
    assert result.extras["default_leg_npv"] == pytest.approx(10_000.5 * 0.4)


def test_decode_cds_response_preserves_input_order() -> None:
    body = _cds_response_bytes([1.0, 2.0, 3.0])
    decoded = decode_cds_response(body, expected_count=3)
    assert [r.npv for r in decoded] == [
        pytest.approx(1.0),
        pytest.approx(2.0),
        pytest.approx(3.0),
    ]


# ---------------------------------------------------------------------------
# decode_cds_response — malformed shapes
# ---------------------------------------------------------------------------


def test_decode_cds_rejects_empty_body() -> None:
    with pytest.raises(ValueError, match="empty engine response"):
        decode_cds_response(b"", expected_count=1)


def test_decode_cds_rejects_malformed_root() -> None:
    with pytest.raises(ValueError, match=r"malformed engine response|expected "):
        decode_cds_response(b"this-is-not-a-flatbuffer", expected_count=1)


def test_decode_cds_rejects_count_mismatch() -> None:
    body = _cds_response_bytes([1.0, 2.0])
    with pytest.raises(ValueError, match=r"returned 2 result\(s\), expected 3"):
        decode_cds_response(body, expected_count=3)


# ---------------------------------------------------------------------------
# decode_cds_response — per-trade engine error (never silently all-zero)
# ---------------------------------------------------------------------------


def _cds_response_with_error(message: str) -> bytes:
    """Synthesise a ``PriceCDSResponse`` whose single result carries an error.

        Mirrors what the engine emits on a per-trade pricing failure: every numeric
        field defaults to 0.0 and only ``error`` is populated
    .
    """

    response = PriceCDSResponseT()
    v = CDSValuesT()
    err = ErrorT()
    err.errorMessage = message
    v.error = err
    response.cdsList = [v]
    builder = flatbuffers.Builder(256)
    builder.Finish(response.Pack(builder))
    return bytes(builder.Output())


def test_decode_cds_surfaces_per_trade_engine_error() -> None:
    """An engine per-trade error is raised, never returned as an all-zero result.

    This is the silent-collapse guard: before the fix the decoder ignored
    the ``CDSValues.error`` field and returned the engine's all-zero numeric
    defaults verbatim as a *success*, masking an engine pricing failure as a
    zero-valued price. The decoder must instead raise so the route maps it to a
    502 ``engine_upstream_error`` carrying the engine's message.
    """

    body = _cds_response_with_error("CDS pricing error: fair upfront not available")
    with pytest.raises(ValueError, match=r"per-trade pricing error.*fair upfront"):
        decode_cds_response(body, expected_count=1)


# ---------------------------------------------------------------------------
# price_cds_batch — the translator
# ---------------------------------------------------------------------------


async def test_price_cds_batch_round_trip() -> None:
    body = _cds_response_bytes([42.0])
    engine = _FakeEngine(response=body)
    batch: EngineBatch[CdsTrade] = EngineBatch(
        trades=(_cds_trade("a"),),
        shared_inputs={
            "as_of": "2026-05-13",
            "credit_curve_id": None,
            "discount_curve_id": None,
        },
    )

    results: Sequence[CdsResult] = await price_cds_batch(engine, batch, resolved=_resolved())

    assert len(engine.calls) == 1
    rpc, request_bytes = engine.calls[0]
    assert rpc is EngineRpc.PRICE_CDS
    parsed = PriceCDSRequest.GetRootAs(request_bytes, 0)
    assert parsed.CdsListLength() == 1
    assert len(results) == 1
    assert results[0].npv == pytest.approx(42.0)


async def test_price_cds_batch_propagates_engine_exceptions() -> None:
    engine = _FakeEngine()
    engine.set_exception(NotImplementedError("stub backend"))
    batch: EngineBatch[CdsTrade] = EngineBatch(
        trades=(_cds_trade(),),
        shared_inputs={"as_of": "2026-05-13"},
    )

    with pytest.raises(NotImplementedError, match="stub backend"):
        await price_cds_batch(engine, batch, resolved=_resolved())
