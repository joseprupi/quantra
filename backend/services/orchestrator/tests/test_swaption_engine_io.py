"""Unit tests for ``pricing/swaption/engine_io.py``.

The wire layer is a thin FB encoder + decoder around
``EngineClient.call``. These tests pin every contract a
downstream consumer (route handler / live engine) relies on:

* ``SWAPTION_ENGINE_RPC`` is exactly :attr:`EngineRpc.PRICE_SWAPTION`.
* ``build_swaption_request`` produces a valid ``PriceSwaptionRequest``
  flatbuffer whose ``Pricing`` context is the caller's *resolved* vol
  surface + swaption model: the encoded
  surface carries the supplied vol level (not the canonical 0.20) and
  the per-trade discount / forwarding / volatility / model references
  carry the resolved entity ids (no ``CANONICAL_*`` leak).
* a missing surface quote → ``swaption_quote_resolution_failed``;
  an unmapped surface kind → ``swaption_surface_resolution_failed``;
  an unmapped curve helper kind → ``swaption_curve_resolution_failed``.
* ``decode_swaption_response`` round-trips a synthesised
  ``PriceSwaptionResponse`` flatbuffer and rejects every malformed
  shape (empty, malformed root, count mismatch).
* ``price_swaption_batch`` calls ``EngineClient.call`` exactly once per
  batch with the right RPC + bytes (assembled from the resolved bundle)
  and decodes whatever the engine returns.
* ``EngineClient.call`` exceptions bubble unchanged (the route
  handler's :func:`map_engine_client_error` is responsible for
  translating them to the error envelope).
  keying field — independent of the FB encode but required for future
  grouping policies.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date

import flatbuffers
import pytest

from quantra_common.engine_client import EngineClient, EngineRpc
from quantra_common.engine_client._generated.quantra.enums.Frequency import Frequency
from quantra_common.engine_client._generated.quantra.enums.IrModelType import (
    IrModelType,
)
from quantra_common.engine_client._generated.quantra.enums.SwapType import SwapType
from quantra_common.engine_client._generated.quantra.ModelPayload import ModelPayload
from quantra_common.engine_client._generated.quantra.PriceSwaptionRequest import (
    PriceSwaptionRequest,
)
from quantra_common.engine_client._generated.quantra.PriceSwaptionResponse import (
    PriceSwaptionResponseT,
)
from quantra_common.engine_client._generated.quantra.SwaptionModelSpec import (
    SwaptionModelSpec,
)
from quantra_common.engine_client._generated.quantra.SwaptionResponse import (
    SwaptionResponseT,
)
from quantra_common.engine_client._generated.quantra.SwaptionVolConstantSpec import (
    SwaptionVolConstantSpec,
)
from quantra_common.engine_client._generated.quantra.SwaptionVolPayload import (
    SwaptionVolPayload,
)
from quantra_common.engine_client._generated.quantra.SwaptionVolSpec import (
    SwaptionVolSpec,
)
from quantra_common.engine_client._generated.quantra.VanillaSwap import (
    VanillaSwap as VanillaSwapTbl,
)
from quantra_common.engine_client._generated.quantra.VolPayload import VolPayload
from quantra_orchestrator.pricing._translator import (
    DEFAULT_FORWARDING_INDEX_ID,
    CurveRole,
    ResolvedMarketData,
    resolved_curve_id,
)
from quantra_orchestrator.pricing.concurrency import EngineBatch
from quantra_orchestrator.pricing.swap_ir.models import ResolvedIndex
from quantra_orchestrator.pricing.swaption.engine_io import (
    SWAPTION_ENGINE_RPC,
    build_swaption_request,
    decode_swaption_response,
    price_swaption_batch,
)
from quantra_orchestrator.pricing.swaption.errors import (
    SwaptionCurveResolutionFailedError,
    SwaptionMissingTradeFieldsError,
    SwaptionQuoteResolutionFailedError,
    SwaptionSurfaceResolutionFailedError,
)
from quantra_orchestrator.pricing.swaption.models import (
    ResolvedCurve,
    ResolvedQuoteValue,
    ResolvedSwaptionModel,
    ResolvedVolSurface,
    SwaptionResult,
    SwaptionTrade,
)


@pytest.fixture(autouse=True)
def _canonical_engine_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the canonical (0.5) wire layout for request-content assertions.

    The default ``ENGINE_WIRE_COMPAT=0.2`` packs the four slot-shifted tables
    (legs / bonds) in the legacy engine-0.2.0 layout (see
    ``quantra_common.engine_client.wire_compat``), which the canonical raw
    readers used below would misread. The legacy layout itself is byte-pinned
    by the golden-hex tests (which re-pin ``0.2`` explicitly) and by
    ``test_wire_compat.py``.
    """

    monkeypatch.setenv("ENGINE_WIRE_COMPAT", "0.5")


_VOL_QUOTE_ID = "USD.SWPTN.ATM.5Y10Y.VOL"


# ---------------------------------------------------------------------------
# Builders
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


def _vol_surface(
    surface_id: uuid.UUID | None = None,
    *,
    name: str = "USD-ATM",
    kind: str = "SwaptionVolSpec",
    base: dict[str, object] | None = None,
    payload: dict[str, object] | None = None,
) -> ResolvedVolSurface:
    if payload is None:
        payload = {
            "swap_index_id": "EUR_SWAP_6M",
            "base": base if base is not None else {"quote_id": _VOL_QUOTE_ID},
        }
    return ResolvedVolSurface(id=surface_id, name=name, kind=kind, payload=payload)


def _model(
    model_id: uuid.UUID | None = None,
    *,
    name: str = "HW-LATTICE",
    kind: str = "HullWhiteLattice",
    payload: dict[str, object] | None = None,
) -> ResolvedSwaptionModel:
    return ResolvedSwaptionModel(
        id=model_id,
        name=name,
        kind=kind,
        payload=payload if payload is not None else {"hw_a": 0.05, "hw_sigma": 0.01},
    )


def _resolved(
    *,
    curves: list[ResolvedCurve] | None = None,
    quotes: list[ResolvedQuoteValue] | None = None,
    vol_surfaces: list[ResolvedVolSurface] | None = None,
    models: list[ResolvedSwaptionModel] | None = None,
    as_of: str = "2026-05-13",
) -> ResolvedMarketData:
    """Default: one curve + one vol surface + one model, all quote-substituted."""

    if curves is None:
        curves = [_resolved_curve(uuid.uuid4())]
    if quotes is None:
        quotes = [_quote(), _quote(_VOL_QUOTE_ID, 0.65)]
    if vol_surfaces is None:
        vol_surfaces = [_vol_surface()]
    if models is None:
        models = [_model()]
    return ResolvedMarketData(
        as_of=as_of,
        curves=tuple(curves),
        quotes=tuple(quotes),
        vol_surfaces=tuple(vol_surfaces),
        models=tuple(models),
    )


def _trade(name: str = "t-1", **swaption_kwargs: object) -> SwaptionTrade:
    # Explicit economics — the wire layer no longer supplies silent
    # defaults (422 ``swaption_missing_trade_fields`` otherwise). Values
    # match the historical fixture defaults so golden bytes are unchanged.
    body: dict[str, object] = {
        "exercise_type": "European",
        "notional": 1_000_000.0,
        "strike": 0.035,
        "exercise_date": "2026-01-15",
        "effective_date": "2026-01-17",
        "termination_date": "2031-01-17",
    }
    body.update(swaption_kwargs)
    return SwaptionTrade(swaption_id=None, name=name, swaption=body)


def _decode_constant_surface(
    request: PriceSwaptionRequest,
) -> tuple[bytes | None, int, float, bytes | None]:
    """Return ``(surface_id, swaption_payload_type, constant_vol, base_quote_id)``."""

    volatility = request.Pricing().Volatility()
    assert volatility is not None
    surface = volatility.VolSurfaces(0)
    assert surface is not None
    assert surface.PayloadType() == VolPayload.SwaptionVolSpec
    sv_table = surface.Payload()
    sv = SwaptionVolSpec()
    sv.Init(sv_table.Bytes, sv_table.Pos)
    const_table = sv.Payload()
    const = SwaptionVolConstantSpec()
    const.Init(const_table.Bytes, const_table.Pos)
    base = const.Base()
    assert base is not None
    return surface.Id(), sv.PayloadType(), base.ConstantVol(), base.QuoteId()


def _make_response_bytes(npvs: list[float]) -> bytes:
    """Synthesise a ``PriceSwaptionResponse`` flatbuffer with given NPVs."""

    response = PriceSwaptionResponseT()
    response.swaptions = []
    for npv in npvs:
        s = SwaptionResponseT()
        s.npv = npv
        s.impliedVolatility = 0.20
        s.atmForward = 0.03
        s.annuity = 4.4e6
        s.delta = 0.5
        s.vega = npv * 0.01
        s.gamma = 0.0
        s.theta = 0.0
        s.dv01 = 0.0
        response.swaptions.append(s)
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


def test_swaption_engine_rpc_is_price_swaption() -> None:
    """The 07-plan's ``PRICE_SWAPTION`` placeholder matches the canonical enum.

    Pinned: a future enum rename (or a typo in a downstream
    refactor) won't silently retarget the orchestrator.
    """

    assert SWAPTION_ENGINE_RPC is EngineRpc.PRICE_SWAPTION
    assert str(SWAPTION_ENGINE_RPC) == "PriceSwaption"


def test_build_request_emits_a_parseable_flatbuffer() -> None:
    """Output is a valid ``PriceSwaptionRequest`` flatbuffer with one swaption per trade.

    The ``as_of`` now comes from the resolved bundle, not ``shared_inputs``.
    """

    trades = [_trade("a"), _trade("b")]

    raw = build_swaption_request(trades, resolved=_resolved())

    request = PriceSwaptionRequest.GetRootAs(raw, 0)
    assert request.SwaptionsLength() == 2
    pricing = request.Pricing()
    assert pricing is not None
    assert pricing.AsOfDate() == b"2026-05-13"


def test_build_request_carries_resolved_surface_and_model() -> None:
    """The surface + model are the *resolved* ones: real ids, supplied vol.

    The faithful translator replaces the canonical
    ``swaption_vol`` / flat-0.20 / ``black_model`` fixture: the encoded surface
    carries the resolved ``app.vol_surfaces`` UUID and the supplied vol level
    (0.65), and the model carries the resolved ``app.swaption_models`` UUID.
    """

    surface_id = uuid.uuid4()
    model_id = uuid.uuid4()
    resolved = _resolved(
        vol_surfaces=[_vol_surface(surface_id)],
        models=[_model(model_id)],
    )

    raw = build_swaption_request([_trade()], resolved=resolved)

    request = PriceSwaptionRequest.GetRootAs(raw, 0)
    volatility = request.Pricing().Volatility()
    assert volatility is not None
    assert volatility.VolSurfacesLength() == 1
    assert volatility.ModelsLength() == 1

    sid, payload_type, constant_vol, base_quote_id = _decode_constant_surface(request)
    # Resolved entity id, not the canonical "swaption_vol" string.
    assert sid == str(surface_id).encode()
    assert sid != b"swaption_vol"
    assert payload_type == SwaptionVolPayload.SwaptionVolConstantSpec
    # Supplied vol level reached the bytes (not the canonical 0.20), quote dropped.
    assert constant_vol == pytest.approx(0.65)
    assert not base_quote_id  # None / b"" — never leaks to the engine (invariant #8)

    model = volatility.Models(0)
    assert model is not None
    assert model.Id() == str(model_id).encode()
    assert model.Id() != b"black_model"
    assert model.PayloadType() == ModelPayload.SwaptionModelSpec
    model_table = model.Payload()
    swaption_model = SwaptionModelSpec()
    swaption_model.Init(model_table.Bytes, model_table.Pos)
    assert swaption_model.ModelType() == IrModelType.HullWhiteLattice


def test_build_request_substitutes_inline_constant_vol() -> None:
    """An inline ``constant_vol`` (no quote_id) is carried through verbatim."""

    resolved = _resolved(
        quotes=[_quote()],
        vol_surfaces=[_vol_surface(base={"constant_vol": 0.18})],
    )
    raw = build_swaption_request([_trade()], resolved=resolved)
    request = PriceSwaptionRequest.GetRootAs(raw, 0)
    _sid, _pt, constant_vol, _qid = _decode_constant_surface(request)
    assert constant_vol == pytest.approx(0.18)


def test_build_request_per_trade_refs_carry_resolved_ids() -> None:
    """Per-trade discount/forwarding/volatility/model refs equal the resolved ids."""

    discount_id = uuid.uuid4()
    forwarding_id = uuid.uuid4()
    surface_id = uuid.uuid4()
    model_id = uuid.uuid4()
    resolved = _resolved(
        curves=[
            _resolved_curve(discount_id),
            _resolved_curve(forwarding_id, name="USD-FWD"),
        ],
        vol_surfaces=[_vol_surface(surface_id)],
        models=[_model(model_id)],
    )

    raw = build_swaption_request([_trade()], resolved=resolved)
    request = PriceSwaptionRequest.GetRootAs(raw, 0)
    swaption = request.Swaptions(0)
    assert swaption is not None
    assert swaption.DiscountingCurve() == str(discount_id).encode()
    assert swaption.ForwardingCurve() == str(forwarding_id).encode()
    assert swaption.Volatility() == str(surface_id).encode()
    assert swaption.Model() == str(model_id).encode()
    # None of the canonical placeholders leak into the wire references.
    for ref in (
        swaption.DiscountingCurve(),
        swaption.ForwardingCurve(),
        swaption.Volatility(),
        swaption.Model(),
    ):
        assert ref not in (b"discount", b"swaption_vol", b"black_model")
    # The underlying float leg references the interim default forwarding index,
    # never a canonical curve / index id.
    underlying = swaption.Swaption().Underlying()
    vs = VanillaSwapTbl()
    vs.Init(underlying.Bytes, underlying.Pos)
    assert vs.FloatingLeg().Index().Id() == DEFAULT_FORWARDING_INDEX_ID.encode()


def test_build_request_missing_quote_raises_swaption_quote_resolution_failed() -> None:
    """A surface quote_id with no resolved value → ``swaption_quote_resolution_failed``."""

    resolved = _resolved(
        quotes=[_quote()],  # curve quote only; the surface quote is unresolved
        vol_surfaces=[_vol_surface(base={"quote_id": _VOL_QUOTE_ID})],
    )
    with pytest.raises(SwaptionQuoteResolutionFailedError) as excinfo:
        build_swaption_request([_trade()], resolved=resolved)
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "swaption_quote_resolution_failed"


def test_build_request_unknown_surface_kind_raises_surface_resolution_failed() -> None:
    """An unmapped surface kind → ``swaption_surface_resolution_failed`` (422).

    ``OptionletVolSpec`` (cap/floor) is not one the translator maps; ``BlackVolSpec``
    is now the mapped equity kind, so it is no longer the "unknown" example.
    """

    resolved = _resolved(vol_surfaces=[_vol_surface(kind="OptionletVolSpec")])
    with pytest.raises(SwaptionSurfaceResolutionFailedError) as excinfo:
        build_swaption_request([_trade()], resolved=resolved)
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "swaption_surface_resolution_failed"


def test_build_request_unknown_helper_kind_raises_curve_resolution_failed() -> None:
    """An unmapped curve ``point_type`` → ``swaption_curve_resolution_failed`` (422)."""

    bad_point: dict[str, object] = {
        "point_type": "WidgetHelper",
        "point": {"quote_id": "USD.IRS.1Y"},
    }
    resolved = _resolved(curves=[_resolved_curve(uuid.uuid4(), points=[bad_point])])
    with pytest.raises(SwaptionCurveResolutionFailedError) as excinfo:
        build_swaption_request([_trade()], resolved=resolved)
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "swaption_curve_resolution_failed"


def test_build_request_rejects_missing_required_fields() -> None:
    """A missing strike/notional/date is a typed 422, never a fixture-default swaption."""

    trade = SwaptionTrade(swaption_id=None, name="s-1", swaption={"exercise_type": "European"})
    with pytest.raises(SwaptionMissingTradeFieldsError) as exc_info:
        build_swaption_request([trade], resolved=_resolved())
    exc = exc_info.value
    assert exc.status_code == 422
    assert exc.code == "swaption_missing_trade_fields"
    missing = {d["field"] for d in exc.details or []}
    assert missing == {
        "notional",
        "strike",
        "exercise_date",
        "effective_date",
        "termination_date",
    }


def test_build_request_honors_per_trade_overrides() -> None:
    """``trade.swaption`` keys override per-leg defaults; faithful pricing stays."""

    trade = _trade(
        "custom",
        notional=2_500_000.0,
        strike=0.04,
        swap_type="Receiver",
        exercise_date="2030-06-15",
        effective_date="2030-06-17",
        termination_date="2035-06-17",
    )
    raw = build_swaption_request([trade], resolved=_resolved())
    request = PriceSwaptionRequest.GetRootAs(raw, 0)
    swaption = request.Swaptions(0)
    assert swaption is not None
    swap = swaption.Swaption()
    assert swap is not None
    assert swap.ExerciseDate() == b"2030-06-15"
    underlying = swap.Underlying()
    assert underlying is not None
    vs = VanillaSwapTbl()
    vs.Init(underlying.Bytes, underlying.Pos)
    assert vs.SwapType() == SwapType.Receiver
    fl = vs.FixedLeg()
    assert fl is not None
    assert fl.Notional() == pytest.approx(2_500_000.0)
    assert fl.Rate() == pytest.approx(0.04)


def _index_with_tenor(n: int, unit: str) -> ResolvedIndex:
    """A resolved underlying-swap float index at the given tenor (pure config)."""

    return ResolvedIndex(
        id=uuid.uuid4(),
        name="TEST-IBOR",
        kind="IborIndex",
        currency="USD",
        body={"tenor": {"n": n, "unit": unit}},
    )


def _underlying_float_frequency(resolved: ResolvedMarketData) -> int:
    """Encode a swaption with ``resolved`` and read back the float-leg frequency."""

    raw = build_swaption_request([_trade()], resolved=resolved)
    request = PriceSwaptionRequest.GetRootAs(raw, 0)
    underlying = request.Swaptions(0).Swaption().Underlying()
    vs = VanillaSwapTbl()
    vs.Init(underlying.Bytes, underlying.Pos)
    return int(vs.FloatingLeg().Schedule().Frequency())


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
def test_underlying_float_frequency_follows_index_tenor(n: int, unit: str, expected: int) -> None:
    """the underlying-swap float schedule frequency follows the index tenor.

    A 3M index must pay quarterly float coupons, a 6M index semi-annually, etc.
    — the same tenor→frequency derivation originally applied to swap_ir, now shared so
    the underlying float schedule and the projection index can never disagree.
    Previously the frequency was hardcoded ``Semiannual``.
    """

    resolved = _resolved()
    resolved = ResolvedMarketData(
        as_of=resolved.as_of,
        curves=resolved.curves,
        quotes=resolved.quotes,
        vol_surfaces=resolved.vol_surfaces,
        models=resolved.models,
        index=_index_with_tenor(n, unit),
    )
    assert _underlying_float_frequency(resolved) == expected


def test_underlying_float_frequency_defaults_semiannual_without_index() -> None:
    """No resolved index → the documented single flat-default stays Semiannual."""

    assert _underlying_float_frequency(_resolved()) == Frequency.Semiannual


def test_underlying_float_frequency_falls_back_on_uncleanly_divisible_tenor() -> None:
    """A tenor with no exact annual frequency (5M, the case) falls back to default."""

    base = _resolved()
    resolved = ResolvedMarketData(
        as_of=base.as_of,
        curves=base.curves,
        quotes=base.quotes,
        vol_surfaces=base.vol_surfaces,
        models=base.models,
        index=_index_with_tenor(5, "Months"),
    )
    assert _underlying_float_frequency(resolved) == Frequency.Semiannual


def test_decode_response_round_trips_a_one_trade_payload() -> None:
    body = _make_response_bytes([12345.5])
    decoded = decode_swaption_response(body, expected_count=1)
    assert len(decoded) == 1
    assert isinstance(decoded[0], SwaptionResult)
    assert decoded[0].npv == pytest.approx(12345.5)
    assert decoded[0].vega == pytest.approx(123.455)
    assert "implied_volatility" in decoded[0].extras


def test_decode_response_round_trips_multi_trade_payload_in_order() -> None:
    body = _make_response_bytes([1.0, 2.0, 3.0])
    decoded = decode_swaption_response(body, expected_count=3)
    assert [r.npv for r in decoded] == [
        pytest.approx(1.0),
        pytest.approx(2.0),
        pytest.approx(3.0),
    ]


def test_decode_response_rejects_empty_body() -> None:
    with pytest.raises(ValueError, match="empty engine response"):
        decode_swaption_response(b"", expected_count=1)


def test_decode_response_rejects_malformed_root() -> None:
    """A buffer that is not a ``PriceSwaptionResponse`` flatbuffer is rejected."""

    with pytest.raises(ValueError, match=r"malformed engine response|expected "):
        decode_swaption_response(b"this-is-not-a-flatbuffer", expected_count=1)


def test_decode_response_rejects_count_mismatch() -> None:
    body = _make_response_bytes([1.0, 2.0])
    with pytest.raises(ValueError, match="returned 2 result\\(s\\), expected 3"):
        decode_swaption_response(body, expected_count=3)


async def test_price_swaption_batch_round_trip() -> None:
    body = _make_response_bytes([42.0])
    engine = _FakeEngine(response=body)
    surface_id = uuid.uuid4()
    batch: EngineBatch[SwaptionTrade] = EngineBatch(
        trades=(_trade("a"),),
        shared_inputs={"as_of": "2026-05-13", "vol_surface_kind": "SwaptionVolSpec"},
    )

    results: Sequence[SwaptionResult] = await price_swaption_batch(
        engine, batch, resolved=_resolved(vol_surfaces=[_vol_surface(surface_id)])
    )

    assert len(engine.calls) == 1
    rpc, request_bytes = engine.calls[0]
    assert rpc is EngineRpc.PRICE_SWAPTION
    # The bytes EngineClient.call received decode into a faithful request:
    # the supplied vol is present (not the canonical 0.20) and the surface id is
    # the resolved one.
    parsed = PriceSwaptionRequest.GetRootAs(request_bytes, 0)
    assert parsed.SwaptionsLength() == 1
    sid, _pt, constant_vol, base_quote_id = _decode_constant_surface(parsed)
    assert sid == str(surface_id).encode()
    assert constant_vol == pytest.approx(0.65)
    assert not base_quote_id
    assert len(results) == 1
    assert results[0].npv == pytest.approx(42.0)


async def test_price_swaption_batch_propagates_engine_exceptions() -> None:
    engine = _FakeEngine()
    engine.set_exception(NotImplementedError("stub backend"))
    batch: EngineBatch[SwaptionTrade] = EngineBatch(trades=(_trade(),), shared_inputs={})

    with pytest.raises(NotImplementedError, match="stub backend"):
        await price_swaption_batch(engine, batch, resolved=_resolved())


# ---------------------------------------------------------------------------
# Bytes-unchanged regression (role-split is a pure refactor for the
# EUR-6M fixture). Golden bytes captured from the previous swaption request path.
# ---------------------------------------------------------------------------

# golden re-captured after ``build_swaption_request`` began forcing
# FlatBuffers defaults onto the wire (``builder.ForceDefaults(True)``). The
# added bytes are the previously-dropped zero-valued convention fields; the
# priced NPV is unchanged on both 0.1.1 and 0.2.0 (live A/B).
_SWAPTION_GOLDEN_HEX = (
    "140000000000000000000a0010000c00080007000a000000000000000800000064020000010000001400000000"
    "000e001800140010000c00080004000e000000140000003c000000640000008c000000c4000000240000003434"
    "3434343434342d343434342d343434342d343434342d3434343434343434343434340000000024000000333333"
    "33333333332d333333332d333333332d333333332d333333333333333333333333000000002400000032323232"
    "323232322d323232322d323232322d323232322d32323232323232323232323200000000240000003131313131"
    "3131312d313131312d313131312d313131312d3131313131313131313131310000120016001500140013000c00"
    "00000b000400120000001c000000000000013c0100000000000000000a0010000f00080004000a000000200000"
    "00b400000000000000140030002c0020001c0010000f000e000800070014000000000000000200000000000200"
    "000000000000000000000000140000000000000080842e41000000002400000016feffff040000001000000066"
    "6f7277617264696e675f696e6465780000000098ffffff000000000202020b0c00000018000000000000200a00"
    "0000323033312d30312d313700000a000000323032362d30312d3137000000000e0020001c0010000800070006"
    "000e0000000000020eec51b81e85eba13f0000000080842e41000000001800000014001800170010000c000b00"
    "0a000900080007001400000000000000020202000c00000018000000000000200a000000323033312d30312d31"
    "3700000a000000323032362d30312d313700000a000000323032362d30312d3135000000001600180014001000"
    "00000c000000080000000000040016000000200000002c0000007801000044030000500300000c000800070006"
    "00050004000c0000000001000008000c000800040008000000080000007c000000010000000400000076ffffff"
    "1c000000000000023800000000000e002400230014000c00080007000e00000000000000320000007b14ae47e1"
    "7a843f9a9999999999a93f00000000000000032400000034343434343434342d343434342d343434342d343434"
    "342d343434343434343434343434000000000100000004000000eeffffff18000000000000028400000000000a"
    "0012000c000b0004000a0000001400000000000001580000000000060008000400060000001800000014002000"
    "1c001b001a001900180017000c000400140000009a9999999999c93f0000000000000000000000010001022004"
    "0000000a000000323032362d30352d313300000b0000004555525f535741505f364d0024000000333333333333"
    "33332d333333332d333333332d333333332d33333333333333333333333300000a000c000800000004000a0000"
    "00080000004801000002000000a00000000400000078ffffff100000001c000000000004004c0000000a000000"
    "323032362d30352d31330000010000000400000068ffffff080000000000000160ffffff00010220020000000c"
    "0000006abc74931804a63fd0feffff00000008010000002400000032323232323232322d323232322d32323232"
    "2d323232322d323232323232323232323232000000001000140010000f000e000d000800040010000000100000"
    "001c00000000000400640000000a000000323032362d30352d31330000010000000c00000008000c000b000400"
    "0800000018000000000000011000180010000c0008000700060005001000000000010220020000000c000000c3"
    "f5285c8fc2a53f80ffffff00000008010000002400000031313131313131312d313131312d313131312d313131"
    "312d31313131313131313131313100000000010000001c000000180020001c001800170010000c000b000a0009"
    "0008000400180000001c00000000000220020000002000000000000000240000002c0000000300000045555200"
    "08000c00080007000800000000000005060000000700000045757269626f720010000000666f7277617264696e"
    "675f696e646578000000000a000000323032362d30352d313300000a000000323032362d30352d31330000"
)


def test_build_request_bytes_unchanged_pre_post_16c(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The EUR-6M fixture produces byte-identical engine bytes before and after the refactor.

    The role-split consumes curves by role; the role map points
    discount/forwarding at the same resolved ids the positional convention
    chose, and no resolved index is supplied so the default forwarding index is
    emitted unchanged. The vol surface / model translation is unchanged.
    """

    # The golden bytes are the LEGACY (engine-0.2.0) wire — the layout the
    # compose engine pin ships. Pin it explicitly so the autouse canonical
    # fixture above does not flip it.
    monkeypatch.setenv("ENGINE_WIRE_COMPAT", "0.2")

    discount_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    forwarding_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
    vol_id = uuid.UUID("33333333-3333-3333-3333-333333333333")
    model_id = uuid.UUID("44444444-4444-4444-4444-444444444444")
    discount = _resolved_curve(
        discount_id, name="USD-DISC", points=[_deposit_point(quote_id=None, rate=0.0425)]
    )
    forwarding = _resolved_curve(
        forwarding_id, name="USD-FWD", points=[_deposit_point(quote_id=None, rate=0.0430)]
    )
    vol = _vol_surface(vol_id, base={"constant_vol": 0.2})
    model = _model(model_id)
    resolved = ResolvedMarketData(
        as_of="2026-05-13",
        curves=(discount, forwarding),
        quotes=(),
        curve_roles={
            CurveRole.DISCOUNT: resolved_curve_id(discount),
            CurveRole.FORWARDING: resolved_curve_id(forwarding),
        },
        vol_surfaces=(vol,),
        models=(model,),
    )
    # Explicit economics matching the historical silent defaults — the wire
    # bytes are UNCHANGED (the encoder is untouched; only the require-check
    # was added), so the golden hex still holds.
    trade = SwaptionTrade(
        swaption_id=None,
        name="s-1",
        swaption={
            "notional": 1_000_000.0,
            "strike": 0.035,
            "exercise_date": "2026-01-15",
            "effective_date": "2026-01-17",
            "termination_date": "2031-01-17",
        },
    )
    raw = build_swaption_request([trade], resolved=resolved)
    assert raw.hex() == _SWAPTION_GOLDEN_HEX
