"""Unit tests for the shared resolved-input → FlatBuffers ``Pricing`` translator.

Production request assembly builds the engine's
``Pricing`` graph faithfully from the caller's resolved curves + MD-resolved
quote values, substituting quote ids server-side and honouring the
resolved entity ids. These tests pin the translator core in isolation:

* a resolved curve → ``TermStructure`` is deterministic;
* quote substitution writes the resolved value into the right helper field and
  drops the ``quote_id`` (never leaks to the engine, invariant #8);
* a missing quote / a point with no value source / an unknown helper kind each
  reject (no canonical fallback);
* the curve id placed in the graph is the resolved entity id, not ``CANONICAL_*``.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import flatbuffers
import pytest

from quantra_common.engine_client._generated.quantra.CdsQuote import CdsQuoteT
from quantra_common.engine_client._generated.quantra.CreditCurveSpec import (
    CreditCurveSpecT,
)
from quantra_common.engine_client._generated.quantra.enums.Calendar import Calendar
from quantra_common.engine_client._generated.quantra.enums.CdsQuoteType import (
    CdsQuoteType,
)
from quantra_common.engine_client._generated.quantra.enums.DayCounter import DayCounter
from quantra_common.engine_client._generated.quantra.enums.InflationCurveKind import (
    InflationCurveKind,
)
from quantra_common.engine_client._generated.quantra.enums.IrModelType import (
    IrModelType,
)
from quantra_common.engine_client._generated.quantra.enums.TimeUnit import TimeUnit
from quantra_common.engine_client._generated.quantra.enums.VolatilityType import (
    VolatilityType,
)
from quantra_common.engine_client._generated.quantra.enums.VolSurfaceShape import (
    VolSurfaceShape,
)
from quantra_common.engine_client._generated.quantra.IndexType import IndexType
from quantra_common.engine_client._generated.quantra.InflationPoint import (
    InflationPoint,
)
from quantra_common.engine_client._generated.quantra.ModelPayload import ModelPayload
from quantra_common.engine_client._generated.quantra.Point import Point
from quantra_common.engine_client._generated.quantra.Pricing import Pricing, PricingT
from quantra_common.engine_client._generated.quantra.QuoteKind import QuoteKind
from quantra_common.engine_client._generated.quantra.QuoteType import QuoteType
from quantra_common.engine_client._generated.quantra.SwaptionVolPayload import (
    SwaptionVolPayload,
)
from quantra_common.engine_client._generated.quantra.VolPayload import VolPayload
from quantra_orchestrator.pricing._translator import (
    DEFAULT_CDS_MODEL_ID,
    DEFAULT_EQUITY_MODEL_ID,
    DEFAULT_EQUITY_SPOT_QUOTE_ID,
    DEFAULT_EQUITY_UNDERLYING_ID,
    DEFAULT_FORWARDING_INDEX_ID,
    CreditCurveInvariantError,
    CreditCurveTranslationError,
    CurveRole,
    CurveTranslationError,
    IndexRegistrationError,
    InflationIndexTranslationError,
    QuoteResolutionError,
    ResolvedMarketData,
    SpotTranslationError,
    UnknownHelperKindError,
    UnknownVolSurfaceKindError,
    VolSurfaceTranslationError,
    _guard_credit_curve_invariant,
    build_pricing_from_resolved,
    resolved_credit_curve_id,
    resolved_curve_id,
    resolved_index_id,
    resolved_inflation_index_id,
    resolved_model_id,
    resolved_spot_quote_id,
    resolved_underlier_id,
    resolved_vol_surface_id,
    translate_credit_curve,
    translate_index,
    translate_inflation_curve,
    translate_inflation_index,
    translate_spot_quote,
)
from quantra_orchestrator.pricing.cds.models import (
    ResolvedCreditCurve,
)
from quantra_orchestrator.pricing.equity_options.models import (
    ResolvedSpotQuote,
)
from quantra_orchestrator.pricing.swap_ir.models import (
    ResolvedCurve,
    ResolvedIndex,
    ResolvedQuoteValue,
)
from quantra_orchestrator.pricing.swaps_inflation.models import (
    ResolvedCurve as InflationResolvedCurve,
)
from quantra_orchestrator.pricing.swaps_inflation.models import (
    ResolvedInflationIndex,
)
from quantra_orchestrator.pricing.swaption.models import (
    ResolvedSwaptionModel,
    ResolvedVolSurface,
)

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _deposit(*, quote_id: str | None = "USD.IRS.1Y", rate: float | None = None) -> dict[str, Any]:
    point: dict[str, Any] = {
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


def _bond(*, quote_id: str = "BOND.1") -> dict[str, Any]:
    return {
        "point_type": "BondHelper",
        "point": {
            "quote_id": quote_id,
            "settlement_days": 2,
            "face_amount": 100.0,
            "coupon_rate": 0.05,
            "day_counter": "Actual365Fixed",
            "business_day_convention": "ModifiedFollowing",
            "redemption": 100.0,
            "issue_date": "2025-01-15",
            "schedule": {
                "effective_date": "2025-01-15",
                "termination_date": "2030-01-15",
                "calendar": "TARGET",
                "frequency": "Semiannual",
                "convention": "ModifiedFollowing",
                "termination_date_convention": "ModifiedFollowing",
                "date_generation_rule": "Forward",
            },
        },
    }


def _future(*, quote_id: str = "FUT.1") -> dict[str, Any]:
    return {
        "point_type": "FutureHelper",
        "point": {
            "quote_id": quote_id,
            "future_start_date": "2025-03-19",
            "future_months": 3,
            "calendar": "TARGET",
            "business_day_convention": "ModifiedFollowing",
            "day_counter": "Actual365Fixed",
        },
    }


def _curve(
    curve_id: uuid.UUID | None = None,
    *,
    name: str = "USD-OIS",
    points: list[dict[str, Any]] | None = None,
    day_counter: str | None = "Actual360",
    body: dict[str, Any] | None = None,
) -> ResolvedCurve:
    return ResolvedCurve(
        id=curve_id,
        name=name,
        currency="USD",
        day_counter=day_counter,
        reference_date=date(2026, 5, 13),
        points=points if points is not None else [_deposit()],
        body=body if body is not None else {"interpolator": "LogLinear"},
    )


def _quote(canonical_id: str = "USD.IRS.1Y", value: float = 0.0425) -> ResolvedQuoteValue:
    return ResolvedQuoteValue(canonical_id=canonical_id, as_of=date(2026, 5, 13), value=value)


def _vol_surface(
    surface_id: uuid.UUID | None = None,
    *,
    name: str = "USD-ATM",
    kind: str = "SwaptionVolSpec",
    base: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> ResolvedVolSurface:
    if payload is None:
        payload = {
            "swap_index_id": "EUR_SWAP_6M",
            "base": base if base is not None else {"quote_id": "USD.SWPTN.ATM.5Y10Y.VOL"},
        }
    return ResolvedVolSurface(id=surface_id, name=name, kind=kind, payload=payload)


def _model(
    model_id: uuid.UUID | None = None,
    *,
    name: str = "HW-LATTICE",
    kind: str = "HullWhiteLattice",
    payload: dict[str, Any] | None = None,
) -> ResolvedSwaptionModel:
    return ResolvedSwaptionModel(
        id=model_id,
        name=name,
        kind=kind,
        payload=payload if payload is not None else {"hw_a": 0.05, "hw_sigma": 0.01},
    )


def _resolved(
    *,
    curves: list[ResolvedCurve],
    quotes: list[ResolvedQuoteValue] | None = None,
    vol_surfaces: list[ResolvedVolSurface] | None = None,
    models: list[ResolvedSwaptionModel] | None = None,
) -> ResolvedMarketData:
    return ResolvedMarketData(
        as_of="2026-05-13",
        curves=tuple(curves),
        quotes=tuple(quotes if quotes is not None else [_quote()]),
        vol_surfaces=tuple(vol_surfaces) if vol_surfaces is not None else (),
        models=tuple(models) if models is not None else (),
    )


def _pack(pricing: Any) -> bytes:
    builder = flatbuffers.Builder(1024)
    builder.Finish(pricing.Pack(builder))
    return bytes(builder.Output())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_resolved_curve_to_term_structure_is_deterministic() -> None:
    """Same resolved inputs → byte-identical packed ``Pricing`` graph."""

    curve_id = uuid.uuid4()
    first = build_pricing_from_resolved(_resolved(curves=[_curve(curve_id)], quotes=[_quote()]))
    second = build_pricing_from_resolved(_resolved(curves=[_curve(curve_id)], quotes=[_quote()]))
    assert _pack(first) == _pack(second)


def test_quote_substitution_writes_rate_and_drops_quote_id() -> None:
    """The resolved value lands in ``rate``; the ``quote_id`` never reaches the FB graph."""

    pricing = build_pricing_from_resolved(
        _resolved(
            curves=[_curve(uuid.uuid4(), points=[_deposit(quote_id="USD.IRS.1Y")])],
            quotes=[_quote("USD.IRS.1Y", 0.0425)],
        )
    )
    helper = pricing.rates.curves[0].points[0].point
    assert helper.rate == pytest.approx(0.0425)
    assert not helper.quoteId  # None / "" — substituted server-side


def test_inline_value_without_quote_id_is_carried_through() -> None:
    pricing = build_pricing_from_resolved(
        _resolved(
            curves=[_curve(uuid.uuid4(), points=[_deposit(quote_id=None, rate=0.051)])],
            quotes=[],
        )
    )
    assert pricing.rates.curves[0].points[0].point.rate == pytest.approx(0.051)


def test_bond_helper_substitutes_into_price_field() -> None:
    """A BondHelper with no inline ``rate`` writes the quote into ``price`` (portal parity)."""

    pricing = build_pricing_from_resolved(
        _resolved(
            curves=[_curve(uuid.uuid4(), points=[_bond(quote_id="BOND.1")])],
            quotes=[_quote("BOND.1", 99.5)],
        )
    )
    helper = pricing.rates.curves[0].points[0].point
    assert pricing.rates.curves[0].points[0].pointType == Point.BondHelper
    assert helper.price == pytest.approx(99.5)
    # Untouched: the regenerated presence-based bindings initialise unset
    # scalars to ``None`` (absent on the wire) rather than 0.0.
    assert helper.rate is None


def test_future_helper_substitutes_into_futures_price_field() -> None:
    pricing = build_pricing_from_resolved(
        _resolved(
            curves=[_curve(uuid.uuid4(), points=[_future(quote_id="FUT.1")])],
            quotes=[_quote("FUT.1", 99.0)],
        )
    )
    helper = pricing.rates.curves[0].points[0].point
    assert pricing.rates.curves[0].points[0].pointType == Point.FutureHelper
    assert helper.futuresPrice == pytest.approx(99.0)


def test_missing_quote_raises_quote_resolution_error() -> None:
    with pytest.raises(QuoteResolutionError) as excinfo:
        build_pricing_from_resolved(
            _resolved(
                curves=[_curve(uuid.uuid4(), points=[_deposit(quote_id="USD.IRS.1Y")])],
                quotes=[],
            )
        )
    assert "USD.IRS.1Y" in excinfo.value.missing_canonical_ids


def test_point_without_value_source_raises_curve_translation_error() -> None:
    with pytest.raises(CurveTranslationError):
        build_pricing_from_resolved(
            _resolved(
                curves=[_curve(uuid.uuid4(), points=[_deposit(quote_id=None, rate=None)])],
                quotes=[],
            )
        )


def test_unknown_helper_kind_rejects_with_no_fallback() -> None:
    bad = {"point_type": "WidgetHelper", "point": {"quote_id": "USD.IRS.1Y"}}
    with pytest.raises(UnknownHelperKindError) as excinfo:
        build_pricing_from_resolved(
            _resolved(curves=[_curve(uuid.uuid4(), points=[bad])], quotes=[_quote()])
        )
    # Subclass of CurveTranslationError so a base-only catch still maps to 422.
    assert isinstance(excinfo.value, CurveTranslationError)
    assert excinfo.value.details is not None
    assert excinfo.value.details[0]["helper_kind"] == "WidgetHelper"


def test_missing_point_type_rejects() -> None:
    """A flat point with no ``point_type`` is an unknown kind (no silent default)."""

    with pytest.raises(UnknownHelperKindError):
        build_pricing_from_resolved(
            _resolved(
                curves=[_curve(uuid.uuid4(), points=[{"quote_id": "USD.IRS.1Y"}])],
                quotes=[_quote()],
            )
        )


def test_missing_tenor_rejects() -> None:
    no_tenor = {
        "point_type": "DepositHelper",
        "point": {"quote_id": "USD.IRS.1Y", "calendar": "TARGET"},
    }
    with pytest.raises(CurveTranslationError):
        build_pricing_from_resolved(
            _resolved(curves=[_curve(uuid.uuid4(), points=[no_tenor])], quotes=[_quote()])
        )


def test_empty_curves_rejects() -> None:
    with pytest.raises(CurveTranslationError):
        build_pricing_from_resolved(ResolvedMarketData(as_of="2026-05-13", curves=(), quotes=()))


def test_curve_id_in_graph_is_resolved_entity_id() -> None:
    """The ``TermStructure.id`` is the resolved UUID, never the canonical string."""

    curve_id = uuid.uuid4()
    pricing = build_pricing_from_resolved(_resolved(curves=[_curve(curve_id)]))
    ts = pricing.rates.curves[0]
    assert ts.id == str(curve_id)
    assert ts.referenceDate == "2026-05-13"
    assert ts.id != "discount"


def test_resolved_curve_id_prefers_uuid_then_name() -> None:
    curve_id = uuid.uuid4()
    assert resolved_curve_id(_curve(curve_id)) == str(curve_id)
    assert resolved_curve_id(_curve(None, name="inline-curve")) == "inline-curve"


def test_unrecognised_convention_enum_falls_back_to_default() -> None:
    """Loose curve metadata: an unmapped calendar defaults, it does not reject."""

    point = {
        "point_type": "DepositHelper",
        "point": {
            "quote_id": "USD.IRS.1Y",
            "tenor": {"n": 1, "unit": "Years"},
            "calendar": "Atlantis",  # not an FB Calendar member
        },
    }
    pricing = build_pricing_from_resolved(
        _resolved(curves=[_curve(uuid.uuid4(), points=[point])], quotes=[_quote()])
    )
    assert pricing.rates.curves[0].points[0].point.calendar == Calendar.TARGET


def test_default_forwarding_index_is_emitted() -> None:
    pricing = build_pricing_from_resolved(_resolved(curves=[_curve(uuid.uuid4())]))
    assert len(pricing.rates.indices) == 1
    assert pricing.rates.indices[0].id == DEFAULT_FORWARDING_INDEX_ID


# ---------------------------------------------------------------------------
# Swaption vol surface + model translation
# ---------------------------------------------------------------------------


def test_no_volatility_block_when_no_surfaces_or_models() -> None:
    """A curves-only request (swap_ir) emits no ``VolatilityMarketData`` (unchanged)."""

    pricing = build_pricing_from_resolved(_resolved(curves=[_curve(uuid.uuid4())]))
    assert pricing.volatility is None
    assert pricing.options.swaptionPricingDetails is False


def test_vol_surface_builds_from_resolved_body_not_a_default() -> None:
    """The encoded surface is the resolved one: real id + the supplied vol level."""

    surface_id = uuid.uuid4()
    model_id = uuid.uuid4()
    pricing = build_pricing_from_resolved(
        _resolved(
            curves=[_curve(uuid.uuid4())],
            quotes=[_quote(), _quote("USD.SWPTN.ATM.5Y10Y.VOL", 0.65)],
            vol_surfaces=[_vol_surface(surface_id)],
            models=[_model(model_id)],
        )
    )
    volatility = pricing.volatility
    assert volatility is not None
    assert len(volatility.volSurfaces) == 1
    spec = volatility.volSurfaces[0]
    # Resolved entity id, not the canonical "swaption_vol" string.
    assert spec.id == str(surface_id)
    assert spec.id != "swaption_vol"
    assert spec.payloadType == VolPayload.SwaptionVolSpec
    swaption_vol = spec.payload
    assert swaption_vol.payloadType == SwaptionVolPayload.SwaptionVolConstantSpec
    assert swaption_vol.swapIndexId == "EUR_SWAP_6M"
    base = swaption_vol.payload.base
    # The supplied vol level lands in constantVol (not the canonical 0.20).
    assert base.constantVol == pytest.approx(0.65)
    assert base.referenceDate == "2026-05-13"
    # Swaption diagnostics flag rides with the surface (decode-surface parity).
    assert pricing.options.swaptionPricingDetails is True


def test_vol_surface_quote_substitution_drops_quote_id() -> None:
    """The resolved vol lands in ``constantVol``; ``quote_id`` never reaches the FB graph."""

    pricing = build_pricing_from_resolved(
        _resolved(
            curves=[_curve(uuid.uuid4())],
            quotes=[_quote(), _quote("USD.SWPTN.ATM.5Y10Y.VOL", 0.65)],
            vol_surfaces=[_vol_surface(base={"quote_id": "USD.SWPTN.ATM.5Y10Y.VOL"})],
            models=[_model()],
        )
    )
    base = pricing.volatility.volSurfaces[0].payload.payload.base
    assert base.constantVol == pytest.approx(0.65)
    assert not base.quoteId  # None / "" — substituted server-side (invariant #8)


def test_vol_surface_inline_constant_vol_carried_through() -> None:
    """An inline ``constant_vol`` (no quote_id) is carried through verbatim."""

    pricing = build_pricing_from_resolved(
        _resolved(
            curves=[_curve(uuid.uuid4())],
            quotes=[_quote()],
            vol_surfaces=[_vol_surface(base={"constant_vol": 0.18})],
            models=[_model()],
        )
    )
    base = pricing.volatility.volSurfaces[0].payload.payload.base
    assert base.constantVol == pytest.approx(0.18)


def test_vol_surface_missing_quote_raises_quote_resolution_error() -> None:
    """A surface ``quote_id`` with no resolved value → ``QuoteResolutionError``."""

    with pytest.raises(QuoteResolutionError) as excinfo:
        build_pricing_from_resolved(
            _resolved(
                curves=[_curve(uuid.uuid4())],
                quotes=[_quote()],  # curve quote only; surface quote unresolved
                vol_surfaces=[_vol_surface(base={"quote_id": "USD.SWPTN.ATM.5Y10Y.VOL"})],
                models=[_model()],
            )
        )
    assert "USD.SWPTN.ATM.5Y10Y.VOL" in excinfo.value.missing_canonical_ids


def test_vol_surface_missing_base_raises_surface_translation_error() -> None:
    """A SwaptionVolSpec surface with no ``base`` envelope rejects → surface 422."""

    with pytest.raises(VolSurfaceTranslationError):
        build_pricing_from_resolved(
            _resolved(
                curves=[_curve(uuid.uuid4())],
                quotes=[_quote()],
                vol_surfaces=[_vol_surface(payload={"swap_index_id": "EUR_SWAP_6M"})],
                models=[_model()],
            )
        )


def test_vol_surface_constant_path_accepts_nested_base_shape() -> None:
    """The constant path reads ``base`` nested under the inner payload (portal wire)."""

    payload = {
        "swap_index_id": "EUR_SWAP_6M",
        "payload_type": "SwaptionVolConstantSpec",
        "payload": {"base": {"constant_vol": 0.19}},
    }
    pricing = build_pricing_from_resolved(
        _resolved(
            curves=[_curve(uuid.uuid4())],
            quotes=[_quote()],
            vol_surfaces=[_vol_surface(payload=payload)],
            models=[_model()],
        )
    )
    swaption_vol = pricing.volatility.volSurfaces[0].payload
    assert swaption_vol.payloadType == SwaptionVolPayload.SwaptionVolConstantSpec
    assert swaption_vol.payload.base.constantVol == pytest.approx(0.19)


# ---------------------------------------------------------------------------
# ATM matrix swaption vol surface (expiry x tenor)
# ---------------------------------------------------------------------------


def _atm_matrix_surface(
    surface_id: uuid.UUID | None = None,
    *,
    name: str = "EUR-ATM",
    n_rows: int = 3,
    n_cols: int = 2,
    values: list[float] | None = None,
) -> ResolvedVolSurface:
    payload = {
        "swap_index_id": "EUR_SWAP_6M",
        "payload_type": "SwaptionVolAtmMatrixSpec",
        "payload": {
            "base": {
                "reference_date": "2026-05-13",
                "calendar": "TARGET",
                "business_day_convention": "ModifiedFollowing",
                "day_counter": "Actual365Fixed",
                "volatility_type": "Normal",
                "shape": "AtmMatrix2D",
            },
            "expiries": [
                {"n": 1, "unit": "Years"},
                {"n": 2, "unit": "Years"},
                {"n": 5, "unit": "Years"},
            ],
            "tenors": [{"n": 5, "unit": "Years"}, {"n": 10, "unit": "Years"}],
            "vols": {
                "n_rows": n_rows,
                "n_cols": n_cols,
                "values": values
                if values is not None
                else [0.0060, 0.0065, 0.0070, 0.0075, 0.0080, 0.0085],
            },
        },
    }
    return ResolvedVolSurface(id=surface_id, name=name, kind="SwaptionVolSpec", payload=payload)


def test_swaption_atm_matrix_surface_translates() -> None:
    """An ATM-matrix surface builds the matrix payload (was a hard 422 previously)."""

    surface_id = uuid.uuid4()
    pricing = build_pricing_from_resolved(
        _resolved(
            curves=[_curve(uuid.uuid4())],
            quotes=[_quote()],
            vol_surfaces=[_atm_matrix_surface(surface_id)],
            models=[_model()],
        )
    )
    spec = pricing.volatility.volSurfaces[0]
    assert spec.id == str(surface_id)
    assert spec.payloadType == VolPayload.SwaptionVolSpec
    swaption_vol = spec.payload
    assert swaption_vol.swapIndexId == "EUR_SWAP_6M"
    assert swaption_vol.payloadType == SwaptionVolPayload.SwaptionVolAtmMatrixSpec
    matrix = swaption_vol.payload
    assert matrix.base.shape == VolSurfaceShape.AtmMatrix2D
    assert matrix.base.volatilityType == VolatilityType.Normal
    assert matrix.base.referenceDate == "2026-05-13"
    assert len(matrix.expiries) == 3
    assert len(matrix.tenors) == 2
    assert (matrix.expiries[0].n, matrix.expiries[0].unit) == (1, TimeUnit.Years)
    assert (matrix.tenors[1].n, matrix.tenors[1].unit) == (10, TimeUnit.Years)
    assert matrix.vols.nRows == 3
    assert matrix.vols.nCols == 2
    assert list(matrix.vols.values) == pytest.approx(
        [0.0060, 0.0065, 0.0070, 0.0075, 0.0080, 0.0085]
    )
    # Invariant #8: inline values only — no quote-id vector on the matrix.
    assert not matrix.vols.quoteIds


def test_swaption_atm_matrix_surface_wire_roundtrip() -> None:
    """The matrix survives a ForceDefaults encode → decode round-trip (wire fidelity)."""

    pricing = build_pricing_from_resolved(
        _resolved(
            curves=[_curve(uuid.uuid4())],
            quotes=[_quote()],
            vol_surfaces=[_atm_matrix_surface()],
            models=[_model()],
        )
    )
    # Mirror the production encode (``vol_tools._call`` uses ForceDefaults(True)).
    builder = flatbuffers.Builder(2048)
    builder.ForceDefaults(True)
    builder.Finish(pricing.Pack(builder))
    decoded = PricingT.InitFromObj(Pricing.GetRootAs(bytearray(builder.Output()), 0))

    surfaces = decoded.volatility.volSurfaces
    assert len(surfaces) == 1
    swaption_vol = surfaces[0].payload
    assert swaption_vol.payloadType == SwaptionVolPayload.SwaptionVolAtmMatrixSpec
    matrix = swaption_vol.payload
    assert matrix.base.shape == VolSurfaceShape.AtmMatrix2D
    assert len(matrix.expiries) == 3
    assert len(matrix.tenors) == 2
    assert matrix.vols.nRows == 3
    assert matrix.vols.nCols == 2
    assert list(matrix.vols.values) == pytest.approx(
        [0.0060, 0.0065, 0.0070, 0.0075, 0.0080, 0.0085]
    )


def test_swaption_atm_matrix_grid_dimension_mismatch_rejects() -> None:
    """A ``vols`` grid whose n_rows*n_cols ≠ len(values) rejects → surface 422."""

    with pytest.raises(VolSurfaceTranslationError):
        build_pricing_from_resolved(
            _resolved(
                curves=[_curve(uuid.uuid4())],
                quotes=[_quote()],
                # 3x2 declared but only 4 values.
                vol_surfaces=[_atm_matrix_surface(values=[0.006, 0.0065, 0.007, 0.0075])],
                models=[_model()],
            )
        )


def test_swaption_atm_matrix_axis_grid_disagreement_rejects() -> None:
    """A grid shape that disagrees with the expiry/tenor axes rejects → surface 422."""

    with pytest.raises(VolSurfaceTranslationError):
        build_pricing_from_resolved(
            _resolved(
                curves=[_curve(uuid.uuid4())],
                quotes=[_quote()],
                # 3 expiries x 2 tenors on the axes, but the grid claims 2x2.
                vol_surfaces=[
                    _atm_matrix_surface(n_rows=2, n_cols=2, values=[0.006, 0.0065, 0.007, 0.0075])
                ],
                models=[_model()],
            )
        )


def test_vol_surface_base_without_value_source_rejects() -> None:
    """A base with neither a quote_id nor a constant_vol rejects (no default vol)."""

    with pytest.raises(VolSurfaceTranslationError):
        build_pricing_from_resolved(
            _resolved(
                curves=[_curve(uuid.uuid4())],
                quotes=[_quote()],
                vol_surfaces=[_vol_surface(base={"calendar": "TARGET"})],
                models=[_model()],
            )
        )


def test_unknown_vol_surface_kind_rejects_with_no_fallback() -> None:
    """An unmapped surface kind rejects rather than falling back.

    ``OptionletVolSpec`` (cap/floor) is not one the translator maps; ``BlackVolSpec``
    is now a mapped equity kind, so it is no longer the "unknown" example.
    """

    with pytest.raises(UnknownVolSurfaceKindError) as excinfo:
        build_pricing_from_resolved(
            _resolved(
                curves=[_curve(uuid.uuid4())],
                quotes=[_quote()],
                vol_surfaces=[_vol_surface(kind="OptionletVolSpec")],
                models=[_model()],
            )
        )
    # Subclass of VolSurfaceTranslationError so a base-only catch still 422s.
    assert isinstance(excinfo.value, VolSurfaceTranslationError)
    assert excinfo.value.details is not None
    assert excinfo.value.details[0]["vol_surface_kind"] == "OptionletVolSpec"


def test_swaption_model_builds_from_resolved_body_not_a_default() -> None:
    """The model is the resolved one: real id + the resolved model type."""

    model_id = uuid.uuid4()
    pricing = build_pricing_from_resolved(
        _resolved(
            curves=[_curve(uuid.uuid4())],
            quotes=[_quote(), _quote("USD.SWPTN.ATM.5Y10Y.VOL", 0.65)],
            vol_surfaces=[_vol_surface()],
            models=[_model(model_id, payload={"hw_a": 0.07, "hw_sigma": 0.012})],
        )
    )
    volatility = pricing.volatility
    assert volatility is not None
    assert len(volatility.models) == 1
    model = volatility.models[0]
    # Resolved entity id, not the canonical "black_model" string.
    assert model.id == str(model_id)
    assert model.id != "black_model"
    assert model.payloadType == ModelPayload.SwaptionModelSpec
    assert model.payload.modelType == IrModelType.HullWhiteLattice
    assert model.payload.hwA == pytest.approx(0.07)
    assert model.payload.hwSigma == pytest.approx(0.012)


def test_resolved_vol_surface_id_prefers_uuid_then_name() -> None:
    surface_id = uuid.uuid4()
    assert resolved_vol_surface_id(_vol_surface(surface_id)) == str(surface_id)
    assert (
        resolved_vol_surface_id(_vol_surface(None, name="inline-vol-surface"))
        == "inline-vol-surface"
    )


def test_resolved_model_id_prefers_uuid_then_name() -> None:
    model_id = uuid.uuid4()
    assert resolved_model_id(_model(model_id)) == str(model_id)
    assert resolved_model_id(_model(None, name="inline-swaption-model")) == "inline-swaption-model"


# ---------------------------------------------------------------------------
# Resolved index translation + role-tagged curve consumption
# ---------------------------------------------------------------------------


def _index(
    index_id: uuid.UUID | None = None,
    *,
    name: str = "USD-SOFR",
    kind: str = "OvernightIndex",
    body: dict[str, Any] | None = None,
) -> ResolvedIndex:
    return ResolvedIndex(
        id=index_id,
        name=name,
        kind=kind,
        currency="USD",
        calendar="UnitedStates",
        day_counter="Actual360",
        body=body if body is not None else {"tenor": {"n": 3, "unit": "Months"}, "fixing_days": 2},
    )


def test_translate_index_builds_faithful_indexdef() -> None:
    """A resolved index → a faithful ``IndexDef`` carrying the resolved id + body."""

    index_id = uuid.uuid4()
    idx = translate_index(_index(index_id))
    assert idx.id == str(index_id)
    assert idx.id != DEFAULT_FORWARDING_INDEX_ID
    assert idx.name == "USD-SOFR"
    assert idx.indexType == IndexType.Overnight
    assert idx.tenor.n == 3
    assert idx.tenor.unit == TimeUnit.Months
    assert idx.fixingDays == 2


def test_translate_index_defaults_ibor_for_unmapped_kind() -> None:
    """An unrecognised index kind defaults to Ibor (loose metadata — no reject)."""

    idx = translate_index(_index(uuid.uuid4(), kind="SomethingExotic"))
    assert idx.indexType == IndexType.Ibor


def test_resolved_index_id_prefers_uuid_then_name() -> None:
    index_id = uuid.uuid4()
    assert resolved_index_id(_index(index_id)) == str(index_id)
    assert resolved_index_id(_index(None, name="inline-index")) == "inline-index"


def test_build_pricing_registers_resolved_index_alongside_default() -> None:
    """A resolved index is registered *alongside* the default forwarding index.

    The engine's ``IndexRegistry`` (keyed by ``IndexDef.id``) resolves both the
    curve-helper refs (``SwapHelper.floatIndex`` / ``OISHelper.overnightIndex``,
    which default to :data:`DEFAULT_FORWARDING_INDEX_ID`) *and* the per-trade
    instrument ref (the resolved index id). Both must therefore be present:
    dropping the default to register only the resolved index orphans the curve
    helpers and the engine rejects the curve with ``Unknown index id:
    forwarding_index`` (a live bonds_floating failure this registration fixes).
    """

    index_id = uuid.uuid4()
    resolved = ResolvedMarketData(
        as_of="2026-05-13",
        curves=(_curve(uuid.uuid4()),),
        quotes=(_quote(),),
        index=_index(index_id),
    )
    pricing = build_pricing_from_resolved(resolved)
    index_ids = [idx.id for idx in pricing.rates.indices]
    # The resolved index reaches the registry (not the canonical EUR_6M) ...
    assert str(index_id) in index_ids
    # ... and the default forwarding index the curve helpers reference survives.
    assert DEFAULT_FORWARDING_INDEX_ID in index_ids


def _ois_point(
    years: int,
    *,
    overnight_index_id: str | None = "ESTR",
    quote_id: str | None = None,
    rate: float | None = None,
) -> dict[str, Any]:
    """An OISHelper point referencing an overnight index by id."""

    point: dict[str, Any] = {
        "tenor": {"n": years, "unit": "Years"},
        "settlement_days": 2,
        "calendar": "TARGET",
        "fixed_leg_frequency": "Annual",
    }
    if overnight_index_id is not None:
        point["overnight_index"] = {"id": overnight_index_id}
    if quote_id is not None:
        point["quote_id"] = quote_id
    if rate is not None:
        point["rate"] = rate
    return {"point_type": "OISHelper", "point": point}


def test_ois_helper_estr_ref_registers_estr_indexdef() -> None:
    """An OIS curve referencing ``overnight_index:{id:"ESTR"}`` registers an ESTR IndexDef.

    The fix: before this the ESTR id landed in the OISHelper struct but
    was never registered in ``rates.indices``, so the engine rejected the curve
    with ``NOT_FOUND: Unknown index id: ESTR``. The registry must now carry a
    faithful ESTR IndexDef (Overnight, TARGET, Actual360, EUR, 0 fixing days),
    with no duplicate ids.
    """

    curve = _curve(
        uuid.uuid4(),
        name="EUR-ESTR-OIS",
        points=[_ois_point(5, overnight_index_id="ESTR", rate=0.025)],
    )
    pricing = build_pricing_from_resolved(_resolved(curves=[curve], quotes=[]))
    by_id = {idx.id: idx for idx in pricing.rates.indices}
    # No duplicate ids in the registry.
    assert len(by_id) == len(pricing.rates.indices)
    assert "ESTR" in by_id
    estr = by_id["ESTR"]
    assert estr.indexType == IndexType.Overnight
    assert estr.calendar == Calendar.TARGET
    assert estr.dayCounter == DayCounter.Actual360
    assert estr.currency == "EUR"
    assert estr.fixingDays == 0
    # The default forwarding index the (absent) swap helpers would reference is
    # still emitted.
    assert DEFAULT_FORWARDING_INDEX_ID in by_id


def test_ois_helper_referencing_resolved_index_id_does_not_duplicate() -> None:
    """A helper ref matching the resolved index id reuses the resolved def (no dup, wins)."""

    index_id = uuid.uuid4()
    curve = _curve(
        uuid.uuid4(),
        name="EUR-OIS",
        points=[_ois_point(5, overnight_index_id=str(index_id), rate=0.025)],
    )
    resolved = ResolvedMarketData(
        as_of="2026-05-13",
        curves=(curve,),
        quotes=(),
        index=_index(index_id, name="EUR-ESTR", kind="OvernightIndex"),
    )
    pricing = build_pricing_from_resolved(resolved)
    by_id = {idx.id: idx for idx in pricing.rates.indices}
    assert len(by_id) == len(pricing.rates.indices)  # no duplicate ids
    # The resolved entity definition wins (its name, not the catalog "ESTR").
    assert by_id[str(index_id)].name == "EUR-ESTR"


def test_unknown_overnight_index_ref_raises_actionable_error() -> None:
    """An unknown referenced index id raises an actionable 422, not a silent pass-through.

    The translator does not invent a bogus IndexDef for an id it cannot resolve
    against the catalog (mis-pricing risk) nor let it reach the engine as an
    opaque ``NOT_FOUND``; it raises :class:`IndexRegistrationError` (a
    :class:`CurveTranslationError` subclass → the product's 422) naming the id.
    """

    curve = _curve(
        uuid.uuid4(),
        name="EUR-OIS",
        points=[_ois_point(5, overnight_index_id="WIBOR_ON", rate=0.025)],
    )
    with pytest.raises(IndexRegistrationError) as excinfo:
        build_pricing_from_resolved(_resolved(curves=[curve], quotes=[]))
    # Maps to ``<product>_curve_resolution_failed`` via a base-only catch.
    assert isinstance(excinfo.value, CurveTranslationError)
    assert excinfo.value.index_id == "WIBOR_ON"
    assert excinfo.value.details is not None
    assert excinfo.value.details[0]["unregistered_index_id"] == "WIBOR_ON"


def _swap_point(
    years: int,
    *,
    float_index_id: str | None = "EURIBOR_6M",
    quote_id: str | None = None,
    rate: float | None = None,
) -> dict[str, Any]:
    """A SwapHelper point referencing a term-IBOR forwarding index by id."""

    point: dict[str, Any] = {
        "tenor": {"n": years, "unit": "Years"},
        "calendar": "TARGET",
        "sw_fixed_leg_frequency": "Annual",
    }
    if float_index_id is not None:
        point["float_index"] = {"id": float_index_id}
    if quote_id is not None:
        point["quote_id"] = quote_id
    if rate is not None:
        point["rate"] = rate
    return {"point_type": "SwapHelper", "point": point}


def test_swap_helper_euribor6m_ref_registers_ibor_indexdef() -> None:
    """A curve whose SwapHelper carries ``float_index:{id:"EURIBOR_6M"}`` registers it.

    The live bug: loading / pricing a EUR curve (swaption, vol-surface sample,
    curve-preview) aborted with ``IndexRegistrationError`` — ``EURIBOR_6M`` is
    neither the resolved instrument index, the default forwarding index, nor an
    overnight index, so the translator could not emit an ``IndexDef`` for it.
    The fix registers a faithful term-IBOR def from the known-IBOR catalog with
    the standard EURIBOR conventions (Ibor, TARGET, Actual360, EUR, 6M tenor, 2
    fixing days), with no duplicate ids and the default forwarding index intact.
    """

    curve = _curve(
        uuid.uuid4(),
        name="EUR-6M-SWAP",
        points=[_swap_point(5, float_index_id="EURIBOR_6M", rate=0.025)],
    )
    pricing = build_pricing_from_resolved(_resolved(curves=[curve], quotes=[]))
    by_id = {idx.id: idx for idx in pricing.rates.indices}
    # No duplicate ids in the registry.
    assert len(by_id) == len(pricing.rates.indices)
    assert "EURIBOR_6M" in by_id
    euribor = by_id["EURIBOR_6M"]
    assert euribor.indexType == IndexType.Ibor
    assert euribor.calendar == Calendar.TARGET
    assert euribor.dayCounter == DayCounter.Actual360
    assert euribor.currency == "EUR"
    assert euribor.fixingDays == 2
    assert euribor.tenor.n == 6
    assert euribor.tenor.unit == TimeUnit.Months
    # The default forwarding index is still emitted alongside it.
    assert DEFAULT_FORWARDING_INDEX_ID in by_id


def test_swap_helper_usd_libor_ref_registers_ibor_indexdef() -> None:
    """The catalog is data-driven: a USD LIBOR helper ref also registers cleanly."""

    curve = _curve(
        uuid.uuid4(),
        name="USD-3M-SWAP",
        points=[_swap_point(5, float_index_id="USD_LIBOR_3M", rate=0.03)],
    )
    pricing = build_pricing_from_resolved(_resolved(curves=[curve], quotes=[]))
    by_id = {idx.id: idx for idx in pricing.rates.indices}
    assert "USD_LIBOR_3M" in by_id
    usd = by_id["USD_LIBOR_3M"]
    assert usd.indexType == IndexType.Ibor
    assert usd.currency == "USD"
    assert usd.tenor.n == 3
    assert usd.tenor.unit == TimeUnit.Months


def test_forwarding_index_id_reflects_resolved_index_else_default() -> None:
    index_id = uuid.uuid4()
    with_index = ResolvedMarketData(
        as_of="2026-05-13",
        curves=(_curve(uuid.uuid4()),),
        quotes=(_quote(),),
        index=_index(index_id),
    )
    assert with_index.forwarding_index_id == str(index_id)
    without_index = ResolvedMarketData(
        as_of="2026-05-13", curves=(_curve(uuid.uuid4()),), quotes=(_quote(),)
    )
    assert without_index.forwarding_index_id == DEFAULT_FORWARDING_INDEX_ID


def test_curve_id_for_role_returns_role_tagged_id_no_canonical_leak() -> None:
    """``curve_id_for_role`` reads the role map; ids are resolved, not CANONICAL_*."""

    discount_id = uuid.uuid4()
    forwarding_id = uuid.uuid4()
    resolved = ResolvedMarketData(
        as_of="2026-05-13",
        curves=(_curve(discount_id), _curve(forwarding_id, name="USD-FWD")),
        quotes=(_quote(),),
        curve_roles={
            CurveRole.DISCOUNT: str(discount_id),
            CurveRole.FORWARDING: str(forwarding_id),
        },
    )
    assert resolved.curve_id_for_role(CurveRole.DISCOUNT) == str(discount_id)
    assert resolved.curve_id_for_role(CurveRole.FORWARDING) == str(forwarding_id)
    assert resolved.curve_id_for_role(CurveRole.PROJECTION) is None
    assert resolved.curve_id_for_role(CurveRole.DISCOUNT) != "discount"


def test_two_resolved_curves_both_translate_into_rates() -> None:
    """Every resolved curve reaches ``rates.curves`` in order (no drop under role-split)."""

    discount_id = uuid.uuid4()
    forwarding_id = uuid.uuid4()
    resolved = ResolvedMarketData(
        as_of="2026-05-13",
        curves=(_curve(discount_id), _curve(forwarding_id, name="USD-FWD")),
        quotes=(_quote(),),
        curve_roles={
            CurveRole.DISCOUNT: str(discount_id),
            CurveRole.FORWARDING: str(forwarding_id),
        },
    )
    pricing = build_pricing_from_resolved(resolved)
    ids = [c.id for c in pricing.rates.curves]
    assert ids == [str(discount_id), str(forwarding_id)]


def test_include_coupon_pricer_adds_default_pricer() -> None:
    """The bonds-floating option adds the default coupon pricer under rates."""

    pricing = build_pricing_from_resolved(
        _resolved(curves=[_curve(uuid.uuid4())]), include_coupon_pricer=True
    )
    assert pricing.rates.couponPricers is not None
    assert len(pricing.rates.couponPricers) == 1
    assert pricing.options.bondPricingDetails is False
    pricing_bond = build_pricing_from_resolved(
        _resolved(curves=[_curve(uuid.uuid4())]),
        bond_pricing_details=True,
        include_coupon_pricer=True,
    )
    assert pricing_bond.options.bondPricingDetails is True


# ---------------------------------------------------------------------------
# Credit-curve translation (cds; the hot path)
# ---------------------------------------------------------------------------


def _credit_curve(
    credit_id: uuid.UUID | None = None,
    *,
    name: str = "ACME-SR",
    recovery_rate: float = 0.4,
    body: dict[str, Any] | None = None,
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


def test_flat_hazard_credit_curve_translates_to_flat_spec() -> None:
    """A flat-hazard body → ``flatHazardRate`` set, recovery honoured, no quotes.

    Faithful translator: not the canonical 2 % / 40 % fixture. The id honours the
    resolved entity and ``calendar`` is non-Null (defaults to TARGET).
    """

    credit_id = uuid.uuid4()
    spec = translate_credit_curve(
        _credit_curve(credit_id, recovery_rate=0.55, body={"flat_hazard_rate": 0.037}),
        default_reference_date="2026-05-13",
    )
    assert spec.id == str(credit_id)
    assert spec.id != "credit"  # no canonical-placeholder leak
    assert spec.flatHazardRate == pytest.approx(0.037)
    assert spec.flatHazardRate != pytest.approx(0.02)
    assert spec.recoveryRate == pytest.approx(0.55)
    assert spec.recoveryRate != pytest.approx(0.4)
    assert not spec.quotes  # flat path → no inline quotes
    assert spec.calendar == Calendar.TARGET  # non-Null
    assert spec.referenceDate == "2026-05-13"


def test_inline_par_spread_credit_curve_emits_quotes_with_zero_flat_hazard() -> None:
    """Inline par-spread points → ``quotes[]`` with ``flat_hazard_rate`` ABSENT.

        engine 0.2.0 credit-curve construction is presence-based
    : a PRESENT ``flat_hazard_rate`` (incl. a genuine 0)
        pins a flat-hazard curve and IGNORES the quotes; only an ABSENT
        ``flat_hazard_rate`` bootstraps from them. Since the request builder forces
        defaults onto the wire, the bootstrap path must leave ``flatHazardRate``
        None so it is omitted (a forced present 0 would zero out the curve). This
        pins the invariant on the natural translation path.
    """

    spec = translate_credit_curve(
        _credit_curve(
            uuid.uuid4(),
            recovery_rate=0.35,
            body={
                "points": [
                    {"tenor": "5Y", "quoted_par_spread": 0.012},
                    {"tenor": {"n": 10, "unit": "Years"}, "quoted_par_spread": 0.015},
                ]
            },
        ),
        default_reference_date="2026-05-13",
    )
    # HARD invariant: inline quotes ⇒ flat_hazard_rate ABSENT (None) so the
    # 0.2.0 engine bootstraps from the quotes instead of honouring a present 0.
    assert spec.flatHazardRate is None
    assert spec.recoveryRate == pytest.approx(0.35)
    assert spec.quotes is not None
    assert len(spec.quotes) == 2
    assert spec.quotes[0].quoteType == CdsQuoteType.ParSpread
    assert spec.quotes[0].quotedParSpread == pytest.approx(0.012)
    assert spec.quotes[0].tenor.n == 5
    assert spec.quotes[0].tenor.unit == TimeUnit.Years
    assert spec.quotes[1].quotedParSpread == pytest.approx(0.015)
    assert spec.quotes[1].tenor.n == 10


def test_inline_upfront_credit_curve_carries_running_coupon() -> None:
    """An upfront point → an ``Upfront`` quote carrying the running coupon."""

    spec = translate_credit_curve(
        _credit_curve(
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
        ),
        default_reference_date="2026-05-13",
    )
    assert spec.flatHazardRate is None  # bootstrap path omits flat hazard
    assert spec.quotes is not None
    quote = spec.quotes[0]
    assert quote.quoteType == CdsQuoteType.Upfront
    assert quote.quotedUpfront == pytest.approx(0.08)
    assert quote.runningCoupon == pytest.approx(0.01)


def test_credit_curve_quote_id_rejected_no_md_bypass() -> None:
    """A credit point referencing a vendor quote id rejects (credit bypasses MD)."""

    with pytest.raises(CreditCurveTranslationError):
        translate_credit_curve(
            _credit_curve(
                uuid.uuid4(),
                body={"points": [{"tenor": "5Y", "quote_id": "CDX.ACME.5Y"}]},
            ),
            default_reference_date="2026-05-13",
        )


def test_credit_curve_without_hazard_or_quotes_rejects() -> None:
    """A body with neither flat hazard nor inline quotes rejects (no fallback)."""

    with pytest.raises(CreditCurveTranslationError):
        translate_credit_curve(
            _credit_curve(uuid.uuid4(), body={"recovery_note": "nope"}),
            default_reference_date="2026-05-13",
        )


def test_credit_curve_invariant_guard_rejects_quotes_with_nonzero_flat_hazard() -> None:
    """The HARD guard rejects a spec pairing inline quotes with a non-zero flat hazard.

    Belt-and-braces: the translator never builds this pairing, but the
    guard is the assertion that a future refactor can't reintroduce the
    engine's silent flat-curve override.
    """

    spec = CreditCurveSpecT()
    spec.flatHazardRate = 0.02
    quote = CdsQuoteT()
    quote.quoteType = CdsQuoteType.ParSpread
    quote.quotedParSpread = 0.01
    spec.quotes = [quote]
    with pytest.raises(CreditCurveInvariantError):
        _guard_credit_curve_invariant(spec)


def test_resolved_credit_curve_id_prefers_uuid_then_name() -> None:
    credit_id = uuid.uuid4()
    assert resolved_credit_curve_id(_credit_curve(credit_id)) == str(credit_id)
    assert resolved_credit_curve_id(_credit_curve(None, name="inline-cc")) == "inline-cc"


def test_build_pricing_with_credit_curve_populates_credit_and_cds_model() -> None:
    """A resolved credit curve lands under ``credit.credit_curves`` + adds the cds model.

    The end-to-end translator path: ``pricing.credit`` carries the
    faithful credit curve and ``pricing.volatility.models`` carries the default
    MidPoint cds model the per-trade ``model`` ref points at.
    """

    credit_id = uuid.uuid4()
    resolved = ResolvedMarketData(
        as_of="2026-05-13",
        curves=(_curve(uuid.uuid4()),),
        quotes=(_quote(),),
        credit_curve=_credit_curve(credit_id, body={"flat_hazard_rate": 0.025}),
    )
    pricing = build_pricing_from_resolved(resolved)
    assert pricing.credit is not None
    assert pricing.credit.creditCurves is not None
    assert len(pricing.credit.creditCurves) == 1
    assert pricing.credit.creditCurves[0].id == str(credit_id)
    assert pricing.credit.creditCurves[0].flatHazardRate == pytest.approx(0.025)
    assert pricing.volatility is not None
    assert pricing.volatility.models is not None
    model_ids = [m.id for m in pricing.volatility.models]
    assert DEFAULT_CDS_MODEL_ID in model_ids


def test_build_pricing_without_credit_curve_omits_credit_block() -> None:
    """Non-cds products (no credit curve) get no ``credit`` block / cds model."""

    pricing = build_pricing_from_resolved(_resolved(curves=[_curve(uuid.uuid4())]))
    assert pricing.credit is None


# ---------------------------------------------------------------------------
# equity_options: BlackVol surface + spot + underlier
# ---------------------------------------------------------------------------

_EQ_VOL_QUOTE_ID = "AAPL.IMPLVOL.1Y"
_EQ_SPOT_QUOTE_ID = "AAPL.SPOT"


def _black_vol_surface(
    surface_id: uuid.UUID | None = None,
    *,
    name: str = "AAPL-vol",
    kind: str = "BlackVolSpec",
    base: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> ResolvedVolSurface:
    if payload is None:
        payload = {"base": base if base is not None else {"constant_vol": 0.27}}
    return ResolvedVolSurface(id=surface_id, name=name, kind=kind, payload=payload)


def _spot(*, value: float | None = 123.45, canonical_id: str | None = None) -> ResolvedSpotQuote:
    return ResolvedSpotQuote(canonical_id=canonical_id, value=value)


def _equity_resolved(
    *,
    curves: list[ResolvedCurve] | None = None,
    quotes: list[ResolvedQuoteValue] | None = None,
    vol_surfaces: list[ResolvedVolSurface] | None = None,
    spot: ResolvedSpotQuote | None = None,
    curve_roles: dict[CurveRole, str] | None = None,
) -> ResolvedMarketData:
    """Default equity bundle: discount + dividend curves + BlackVol surface + spot."""

    if curves is None:
        curves = [
            _curve(
                uuid.uuid4(),
                name="USD-DISC",
                points=[_deposit(quote_id=None, rate=0.04)],
            ),
            _curve(
                uuid.uuid4(),
                name="AAPL-DIV",
                points=[_deposit(quote_id=None, rate=0.01)],
            ),
        ]
    if quotes is None:
        quotes = []
    if vol_surfaces is None:
        vol_surfaces = [_black_vol_surface(uuid.uuid4())]
    if spot is None:
        spot = _spot()
    if curve_roles is None:
        curve_roles = {CurveRole.DISCOUNT: resolved_curve_id(curves[0])}
        if len(curves) > 1:
            curve_roles[CurveRole.DIVIDEND] = resolved_curve_id(curves[1])
    return ResolvedMarketData(
        as_of="2026-05-13",
        curves=tuple(curves),
        quotes=tuple(quotes),
        curve_roles=curve_roles,
        vol_surfaces=tuple(vol_surfaces),
        spot=spot,
    )


def test_black_vol_surface_builds_from_resolved_body_not_a_default() -> None:
    """The encoded surface is the resolved one: real id + the supplied vol."""

    surface_id = uuid.uuid4()
    pricing = build_pricing_from_resolved(
        _equity_resolved(vol_surfaces=[_black_vol_surface(surface_id, base={"constant_vol": 0.33})])
    )
    volatility = pricing.volatility
    assert volatility is not None
    assert len(volatility.volSurfaces) == 1
    spec = volatility.volSurfaces[0]
    assert spec.id == str(surface_id)
    assert spec.id != "equity_vol"  # not the canonical surface id
    assert spec.payloadType == VolPayload.BlackVolSpec
    base = spec.payload.base
    assert base.constantVol == pytest.approx(0.33)
    assert base.constantVol != pytest.approx(0.20)
    # An equity BlackVol surface must not flip the swaption diagnostics flag.
    assert pricing.options.swaptionPricingDetails is False


def test_black_vol_surface_quote_substitution_drops_quote_id() -> None:
    """The resolved vol lands in ``constantVol``; ``quote_id`` never reaches bytes."""

    pricing = build_pricing_from_resolved(
        _equity_resolved(
            quotes=[_quote(_EQ_VOL_QUOTE_ID, 0.41)],
            vol_surfaces=[_black_vol_surface(base={"quote_id": _EQ_VOL_QUOTE_ID})],
        )
    )
    base = pricing.volatility.volSurfaces[0].payload.base
    assert base.constantVol == pytest.approx(0.41)
    assert not base.quoteId  # None / "" — substituted server-side (invariant #8)


def test_black_vol_surface_inline_constant_vol_carried_through() -> None:
    pricing = build_pricing_from_resolved(
        _equity_resolved(vol_surfaces=[_black_vol_surface(base={"constant_vol": 0.18})])
    )
    assert pricing.volatility.volSurfaces[0].payload.base.constantVol == pytest.approx(0.18)


def test_black_vol_surface_missing_base_rejects() -> None:
    """A BlackVolSpec surface with no ``base`` envelope rejects → surface 422."""

    with pytest.raises(VolSurfaceTranslationError):
        build_pricing_from_resolved(
            _equity_resolved(vol_surfaces=[_black_vol_surface(payload={"term_vols": []})])
        )


def test_black_vol_surface_base_without_value_source_rejects() -> None:
    """A base with neither a quote_id nor a constant_vol rejects (no default vol)."""

    with pytest.raises(VolSurfaceTranslationError):
        build_pricing_from_resolved(
            _equity_resolved(vol_surfaces=[_black_vol_surface(base={"calendar": "TARGET"})])
        )


def test_black_vol_surface_missing_quote_raises_quote_resolution_error() -> None:
    with pytest.raises(QuoteResolutionError) as excinfo:
        build_pricing_from_resolved(
            _equity_resolved(
                quotes=[],
                vol_surfaces=[_black_vol_surface(base={"quote_id": _EQ_VOL_QUOTE_ID})],
            )
        )
    assert _EQ_VOL_QUOTE_ID in excinfo.value.missing_canonical_ids


def test_spot_quote_inline_value_carried_through() -> None:
    """An inline spot value lands in the QuoteSpec verbatim (Price / Curve kind)."""

    quote = translate_spot_quote(_spot(value=222.5), {}, missing_quotes=[])
    assert quote.value == pytest.approx(222.5)
    assert quote.kind == QuoteKind.Price
    assert quote.quoteType == QuoteType.Curve
    assert quote.id == DEFAULT_EQUITY_SPOT_QUOTE_ID


def test_spot_quote_resolves_canonical_id_and_honours_it() -> None:
    quote = translate_spot_quote(
        _spot(value=None, canonical_id=_EQ_SPOT_QUOTE_ID),
        {_EQ_SPOT_QUOTE_ID: 314.15},
        missing_quotes=[],
    )
    assert quote.id == _EQ_SPOT_QUOTE_ID  # resolved id, not the default
    assert quote.value == pytest.approx(314.15)


def test_spot_quote_missing_canonical_appends_to_missing_quotes() -> None:
    missing: list[str] = []
    translate_spot_quote(
        _spot(value=None, canonical_id=_EQ_SPOT_QUOTE_ID), {}, missing_quotes=missing
    )
    assert missing == [_EQ_SPOT_QUOTE_ID]


def test_spot_quote_without_value_source_raises_spot_translation_error() -> None:
    with pytest.raises(SpotTranslationError):
        translate_spot_quote(_spot(value=None, canonical_id=None), {}, missing_quotes=[])


def test_build_pricing_with_spot_populates_equity_bundle() -> None:
    """A resolved spot lands under ``quotes`` + adds the equity model + underlier."""

    discount_id = uuid.uuid4()
    dividend_id = uuid.uuid4()
    surface_id = uuid.uuid4()
    resolved = _equity_resolved(
        curves=[
            _curve(discount_id, name="USD-DISC", points=[_deposit(quote_id=None, rate=0.04)]),
            _curve(dividend_id, name="AAPL-DIV", points=[_deposit(quote_id=None, rate=0.01)]),
        ],
        vol_surfaces=[_black_vol_surface(surface_id)],
        spot=_spot(value=150.0),
    )
    pricing = build_pricing_from_resolved(resolved)
    # Spot under Pricing.quotes (faithful value).
    assert pricing.quotes is not None
    assert len(pricing.quotes) == 1
    assert pricing.quotes[0].value == pytest.approx(150.0)
    # Equity model under volatility.models at the default id (pure config).
    assert pricing.volatility is not None
    model_ids = [m.id for m in pricing.volatility.models]
    assert DEFAULT_EQUITY_MODEL_ID in model_ids
    # Equity underlier honours the resolved dividend curve id.
    assert pricing.equity is not None
    assert len(pricing.equity.equityUnderlyings) == 1
    underlying = pricing.equity.equityUnderlyings[0]
    assert underlying.id == DEFAULT_EQUITY_UNDERLYING_ID  # inline-only spot
    assert underlying.spotQuoteId == DEFAULT_EQUITY_SPOT_QUOTE_ID
    assert underlying.dividendYieldCurveId == str(dividend_id)
    assert underlying.dividendYieldCurveId != "dividend"


def test_build_pricing_without_spot_omits_equity_bundle() -> None:
    """Non-equity products (no spot) get no ``quotes`` / ``equity`` block."""

    pricing = build_pricing_from_resolved(_resolved(curves=[_curve(uuid.uuid4())]))
    assert not pricing.quotes
    assert pricing.equity is None


def test_resolved_spot_quote_id_prefers_canonical_then_default() -> None:
    assert resolved_spot_quote_id(_spot(value=None, canonical_id="X.SPOT")) == "X.SPOT"
    assert resolved_spot_quote_id(_spot(value=1.0)) == DEFAULT_EQUITY_SPOT_QUOTE_ID


def test_resolved_underlier_id_prefers_canonical_then_default() -> None:
    assert resolved_underlier_id(_spot(value=None, canonical_id="X.SPOT")) == "X.SPOT"
    assert resolved_underlier_id(_spot(value=1.0)) == DEFAULT_EQUITY_UNDERLYING_ID


# ---------------------------------------------------------------------------
# Inflation index + curve translation (swaps_inflation)
# ---------------------------------------------------------------------------


def _infl_index(
    index_uuid: uuid.UUID | None = None,
    *,
    index_id: str = "EUHICP",
    name: str = "EU HICP",
    body: dict[str, Any] | None = None,
) -> ResolvedInflationIndex:
    return ResolvedInflationIndex(
        id=index_uuid,
        name=name,
        index_id=index_id,
        currency="EUR",
        day_counter="Actual365Fixed",
        body=body
        if body is not None
        else {
            "family_name": "EU HICP",
            "frequency": "Monthly",
            "fixings": [
                {"date": "2024-10-01", "value": 100.0},
                {"date": "2024-11-01", "value": 100.2},
            ],
        },
    )


def _infl_point(
    *,
    quote_id: str | None = "EUR.HICP.5Y",
    quote_value: float | None = None,
    tenor: Any = None,
) -> dict[str, Any]:
    point: dict[str, Any] = {"tenor": tenor if tenor is not None else {"n": 5, "unit": "Years"}}
    if quote_id is not None:
        point["quote_id"] = quote_id
    if quote_value is not None:
        point["quote_value"] = quote_value
    return point


def _infl_curve(
    curve_id: uuid.UUID | None = None,
    *,
    name: str = "HICP_ZC",
    points: list[dict[str, Any]] | None = None,
    body: dict[str, Any] | None = None,
) -> InflationResolvedCurve:
    return InflationResolvedCurve(
        id=curve_id,
        name=name,
        role="inflation",
        currency="EUR",
        day_counter="Actual365Fixed",
        reference_date=date(2026, 5, 13),
        points=points if points is not None else [_infl_point()],
        body=body if body is not None else {"interpolator": "Linear"},
    )


def test_resolved_inflation_index_id_is_the_string_index_id() -> None:
    """The engine references the index by its string ``index_id``."""

    # Even when the index came from a row (has a UUID), the engine-facing id is
    # the string ``index_id`` (e.g. "EUHICP"), not the UUID.
    assert resolved_inflation_index_id(_infl_index(uuid.uuid4())) == "EUHICP"
    assert resolved_inflation_index_id(_infl_index(index_id="UKRPI")) == "UKRPI"


def test_inflation_index_builds_from_resolved_body_not_a_default() -> None:
    """The index spec carries the resolved id / family / fixings, not the canonical fixture."""

    spec = translate_inflation_index(_infl_index(), swap_kind="zero_coupon")
    assert spec.id == "EUHICP"
    assert spec.familyName == "EU HICP"
    assert spec.kind == InflationCurveKind.ZeroInflation
    # Fixings are consumed verbatim from the body — not the canonical
    # 100.0 / 100.2 / 100.4 EU-HICP fixture (which has three fixings).
    assert len(spec.fixings) == 2
    assert spec.fixings[0].date == "2024-10-01"
    assert spec.fixings[0].value == pytest.approx(100.0)
    assert spec.fixings[1].value == pytest.approx(100.2)


def test_inflation_index_kind_follows_swap_kind() -> None:
    """``swap_kind`` selects ZeroInflation (ZCIIS) vs YoYInflation (YYIIS)."""

    zc = translate_inflation_index(_infl_index(), swap_kind="zero_coupon")
    yoy = translate_inflation_index(_infl_index(), swap_kind="year_on_year")
    assert zc.kind == InflationCurveKind.ZeroInflation
    assert yoy.kind == InflationCurveKind.YoYInflation


def test_inflation_index_without_fixings_emits_empty_not_canonical() -> None:
    """An index body with no fixings is faithful-empty, never the canonical fixture."""

    spec = translate_inflation_index(
        _infl_index(body={"family_name": "EU HICP"}), swap_kind="zero_coupon"
    )
    assert spec.fixings == []
    assert spec.familyName == "EU HICP"


def test_inflation_index_malformed_fixings_rejects() -> None:
    """A non-list / value-less ``fixings`` block rejects (no canonical fallback)."""

    with pytest.raises(InflationIndexTranslationError):
        translate_inflation_index(
            _infl_index(body={"fixings": "not-a-list"}), swap_kind="zero_coupon"
        )
    with pytest.raises(InflationIndexTranslationError):
        translate_inflation_index(
            _infl_index(body={"fixings": [{"date": "2024-10-01"}]}),
            swap_kind="zero_coupon",
        )


def test_inflation_curve_zciis_substitutes_quote_value_and_drops_quote_id() -> None:
    """ZCIIS helper carries the resolved value in ``quoteValue``; no ``quoteId`` leak."""

    missing: list[str] = []
    spec = translate_inflation_curve(
        _infl_curve(),
        {"EUR.HICP.5Y": 0.025},
        swap_kind="zero_coupon",
        index_id="EUHICP",
        discount_curve_id="DISC",
        nominal_curve_id="DISC",
        default_reference_date="2026-05-13",
        missing_quotes=missing,
    )
    assert spec.kind == InflationCurveKind.ZeroInflation
    assert spec.indexId == "EUHICP"
    assert spec.discountCurveId == "DISC"
    assert len(spec.points) == 1
    wrapper = spec.points[0]
    assert wrapper.pointType == InflationPoint.ZeroCouponInflationSwapHelper
    helper = wrapper.point
    assert helper.quoteValue == pytest.approx(0.025)
    assert not helper.quoteId  # invariant #8 — engine never sees a quote id
    assert missing == []


def test_inflation_curve_yyiis_carries_nominal_curve_id() -> None:
    """YYIIS helper carries the nominal-curve link QuantLib's YoY bootstrap needs."""

    spec = translate_inflation_curve(
        _infl_curve(),
        {"EUR.HICP.5Y": 0.025},
        swap_kind="year_on_year",
        index_id="EUHICP",
        discount_curve_id="DISC",
        nominal_curve_id="DISC",
        default_reference_date="2026-05-13",
        missing_quotes=[],
    )
    assert spec.kind == InflationCurveKind.YoYInflation
    wrapper = spec.points[0]
    assert wrapper.pointType == InflationPoint.YearOnYearInflationSwapHelper
    assert wrapper.point.nominalCurveId == "DISC"


def test_inflation_curve_inline_value_carried_through() -> None:
    """An inline ``quote_value`` (no ``quote_id``) short-circuits MD substitution."""

    spec = translate_inflation_curve(
        _infl_curve(points=[_infl_point(quote_id=None, quote_value=0.031)]),
        {},
        swap_kind="zero_coupon",
        index_id="EUHICP",
        discount_curve_id="DISC",
        nominal_curve_id="DISC",
        default_reference_date="2026-05-13",
        missing_quotes=[],
    )
    assert spec.points[0].point.quoteValue == pytest.approx(0.031)


def test_inflation_curve_missing_quote_collects_into_missing_list() -> None:
    """An unresolved ``quote_id`` is collected (batched into one 422 by the caller)."""

    missing: list[str] = []
    translate_inflation_curve(
        _infl_curve(points=[_infl_point(quote_id="EUR.HICP.UNRESOLVED")]),
        {},
        swap_kind="zero_coupon",
        index_id="EUHICP",
        discount_curve_id="DISC",
        nominal_curve_id="DISC",
        default_reference_date="2026-05-13",
        missing_quotes=missing,
    )
    assert missing == ["EUR.HICP.UNRESOLVED"]


def test_inflation_curve_point_without_value_source_rejects() -> None:
    with pytest.raises(CurveTranslationError):
        translate_inflation_curve(
            _infl_curve(points=[_infl_point(quote_id=None)]),
            {},
            swap_kind="zero_coupon",
            index_id="EUHICP",
            discount_curve_id="DISC",
            nominal_curve_id="DISC",
            default_reference_date="2026-05-13",
            missing_quotes=[],
        )


def test_inflation_curve_missing_tenor_rejects() -> None:
    with pytest.raises(CurveTranslationError):
        translate_inflation_curve(
            _infl_curve(points=[{"quote_id": "EUR.HICP.5Y"}]),
            {"EUR.HICP.5Y": 0.025},
            swap_kind="zero_coupon",
            index_id="EUHICP",
            discount_curve_id="DISC",
            nominal_curve_id="DISC",
            default_reference_date="2026-05-13",
            missing_quotes=[],
        )


def _inflation_resolved(
    *,
    nominal: ResolvedCurve,
    inflation: InflationResolvedCurve,
    index: ResolvedInflationIndex,
    quotes: list[ResolvedQuoteValue],
) -> ResolvedMarketData:
    return ResolvedMarketData(
        as_of="2026-05-13",
        curves=(nominal,),
        quotes=tuple(quotes),
        curve_roles={
            CurveRole.NOMINAL: resolved_curve_id(nominal),
            CurveRole.INFLATION: resolved_curve_id(inflation),
        },
        inflation_curve=inflation,
        inflation_index=index,
    )


def test_build_pricing_emits_faithful_inflation_market_data() -> None:
    """End-to-end: the inflation bundle carries resolved ids + values, no CANONICAL_* leak."""

    nominal_id = uuid.uuid4()
    inflation_id = uuid.uuid4()
    nominal = _curve(nominal_id, name="DISC", points=[_deposit(quote_id="EUR.IRS.1Y")])
    inflation = _infl_curve(inflation_id, points=[_infl_point(quote_id="EUR.HICP.5Y")])
    resolved = _inflation_resolved(
        nominal=nominal,
        inflation=inflation,
        index=_infl_index(),
        quotes=[_quote("EUR.IRS.1Y", 0.031), _quote("EUR.HICP.5Y", 0.025)],
    )

    pricing = build_pricing_from_resolved(resolved, inflation_swap_kind="zero_coupon")

    # Nominal curve rides under rates.curves with the resolved id + rate.
    assert pricing.rates is not None
    assert pricing.rates.curves[0].id == str(nominal_id)
    assert pricing.rates.curves[0].id != "discount"
    assert pricing.rates.curves[0].points[0].point.rate == pytest.approx(0.031)

    # Inflation index + curve under Pricing.inflation, ids honoured.
    assert pricing.inflation is not None
    index_spec = pricing.inflation.inflationIndices[0]
    assert index_spec.id == "EUHICP"
    assert index_spec.id != "HICP_ZC"
    curve_spec = pricing.inflation.inflationCurves[0]
    assert curve_spec.id == str(inflation_id)
    assert curve_spec.id not in ("HICP_ZC", "discount")
    assert curve_spec.indexId == "EUHICP"
    assert curve_spec.discountCurveId == str(nominal_id)  # links to the nominal curve
    # Inflation helper carries the resolved value, not the canonical quote rate.
    helper = curve_spec.points[0].point
    assert helper.quoteValue == pytest.approx(0.025)
    assert not helper.quoteId


def test_build_pricing_inflation_quote_miss_raises_quote_resolution_error() -> None:
    """An unresolved inflation-curve quote surfaces the batched QuoteResolutionError."""

    nominal = _curve(name="DISC", points=[_deposit(quote_id="EUR.IRS.1Y")])
    inflation = _infl_curve(points=[_infl_point(quote_id="EUR.HICP.MISSING")])
    resolved = _inflation_resolved(
        nominal=nominal,
        inflation=inflation,
        index=_infl_index(),
        quotes=[_quote("EUR.IRS.1Y", 0.031)],
    )
    with pytest.raises(QuoteResolutionError) as excinfo:
        build_pricing_from_resolved(resolved, inflation_swap_kind="zero_coupon")
    assert "EUR.HICP.MISSING" in excinfo.value.missing_canonical_ids


def test_build_pricing_without_inflation_index_omits_inflation_block() -> None:
    """Non-inflation products (no inflation index) get no ``Pricing.inflation`` block."""

    pricing = build_pricing_from_resolved(_resolved(curves=[_curve(uuid.uuid4())]))
    assert pricing.inflation is None
