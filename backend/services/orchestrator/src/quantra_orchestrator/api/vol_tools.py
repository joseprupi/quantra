"""IR-vol utility routes — vol-surface sampling + swaption calibration.

Three owner-agnostic ``/v1/`` endpoints that let the portal's IR Vol Sampler
and Swaption-calibration features run through the orchestrator instead of the
retired legacy cloud API:

* ``POST /v1/vol-surfaces/sample``      → engine ``SampleVolSurfaces``
* ``POST /v1/calibrate-swaption-vol``   → engine ``CalibrateSwaptionVol``
* ``POST /v1/calibrate-swaption-model`` → engine ``CalibrateSwaptionModel``

Unlike the pure-math calendar routes, these need a full engine ``Pricing``
graph (rates curves + a vol surface, plus an optional swaption model). Rather
than reimplement that assembly, the shared :func:`_resolve_pricing` reuses the
pricing pipeline's :func:`build_pricing_from_resolved` translator: it accepts
an inline ``pricing`` payload (curves with inline rates and/or ``quote_id``
points, a constant-vol swaption surface, an optional model), resolves every
``quote_id`` server-side through the MD client (invariant #8 — quote ids never
leave the client resolved), substitutes the resolved values and returns
the faithful ``PricingT``.

Vol-surface value source: the translator maps the constant-vol path
(``payload.base.constant_vol`` / ``payload.base.quote_id``), the ATM-matrix
path (``payload_type == 'SwaptionVolAtmMatrixSpec'`` with an inline
expiry x tenor grid) AND the SABR-calibration path
(``payload_type == 'SwaptionSabrCalibrateSpec'`` with an inline
expiry x tenor x strike-spread market-vol cube) — so the Vol Workbench can
sample a real term structure and the calibration endpoint can fit SABR
smiles, not just flat surfaces. Queries accept the portal's engine-native
shape (``expiry_grid`` / ``tenor_grid`` / ``strike_grid`` objects +
``output_mode`` / ``options`` / slice params); the engine REQUIRES a strike
grid on every query (an omitted one is unparseable). Auth is the standard
dev-bypass-aware dependency; engine failures map through the shared structured
envelope; a translation failure (untranslatable curve / surface, unresolved
quote) is a clean 422.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any, Protocol

import flatbuffers
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_common.auth.context import AuthContext
from quantra_common.engine_client import (
    CapturingEngineClient,
    EngineClient,
    EngineClientError,
    EngineRpc,
)
from quantra_common.engine_client._generated.quantra.CalibrateSwaptionModelRequest import (
    CalibrateSwaptionModelRequestT,
)
from quantra_common.engine_client._generated.quantra.CalibrateSwaptionModelResponse import (
    CalibrateSwaptionModelResponse,
    CalibrateSwaptionModelResponseT,
)
from quantra_common.engine_client._generated.quantra.CalibrateSwaptionVolRequest import (
    CalibrateSwaptionVolRequestT,
)
from quantra_common.engine_client._generated.quantra.CalibrateSwaptionVolResponse import (
    CalibrateSwaptionVolResponse,
    CalibrateSwaptionVolResponseT,
)
from quantra_common.engine_client._generated.quantra.DateGrid import DateGrid
from quantra_common.engine_client._generated.quantra.DateGridSpec import DateGridSpecT
from quantra_common.engine_client._generated.quantra.enums.BusinessDayConvention import (
    BusinessDayConvention,
)
from quantra_common.engine_client._generated.quantra.enums.Calendar import Calendar
from quantra_common.engine_client._generated.quantra.enums.DateGenerationRule import (
    DateGenerationRule,
)
from quantra_common.engine_client._generated.quantra.enums.DayCounter import DayCounter
from quantra_common.engine_client._generated.quantra.enums.Frequency import Frequency
from quantra_common.engine_client._generated.quantra.enums.SwaptionStrikeKind import (
    SwaptionStrikeKind,
)
from quantra_common.engine_client._generated.quantra.Error import ErrorT
from quantra_common.engine_client._generated.quantra.Period import PeriodT
from quantra_common.engine_client._generated.quantra.Pricing import PricingT
from quantra_common.engine_client._generated.quantra.QueryOptions import QueryOptionsT
from quantra_common.engine_client._generated.quantra.RangeGrid import RangeGridT
from quantra_common.engine_client._generated.quantra.SabrCalibrationDiagnostics import (
    SabrCalibrationDiagnosticsT,
)
from quantra_common.engine_client._generated.quantra.SampleVolSurfacesRequest import (
    SampleVolSurfacesRequestT,
)
from quantra_common.engine_client._generated.quantra.SampleVolSurfacesResponse import (
    SampleVolSurfacesResponse,
    SampleVolSurfacesResponseT,
)
from quantra_common.engine_client._generated.quantra.StrikeGrid import StrikeGridT
from quantra_common.engine_client._generated.quantra.SwapIndexDef import SwapIndexDefT
from quantra_common.engine_client._generated.quantra.SwapIndexFixedLegSpec import (
    SwapIndexFixedLegSpecT,
)
from quantra_common.engine_client._generated.quantra.SwapIndexFloatLegSpec import (
    SwapIndexFloatLegSpecT,
)
from quantra_common.engine_client._generated.quantra.SwapIndexKind import SwapIndexKind
from quantra_common.engine_client._generated.quantra.SwaptionHwCalibrationSpec import (
    SwaptionHwCalibrationSpecT,
)
from quantra_common.engine_client._generated.quantra.SwaptionVolDiagnostics import (
    SwaptionVolDiagnosticsT,
)
from quantra_common.engine_client._generated.quantra.TenorGrid import TenorGridT
from quantra_common.engine_client._generated.quantra.VolOutputMode import VolOutputMode
from quantra_common.engine_client._generated.quantra.VolQuerySpec import VolQuerySpecT
from quantra_common.engine_client._generated.quantra.VolSurfaceSample import (
    VolSurfaceSampleT,
)
from quantra_common.engine_client._generated.quantra.VolSurfaceType import (
    VolSurfaceType,
)
from quantra_common.md_client import MdClient, MdClientError
from quantra_orchestrator.auth.dependencies import get_auth_context
from quantra_orchestrator.engine import get_engine_client, map_engine_client_error
from quantra_orchestrator.md import get_md_client, map_md_client_error
from quantra_orchestrator.pricing._translator import (
    DEFAULT_SWAP_INDEX_ID,
    ResolvedMarketData,
    TranslationError,
    build_pricing_from_resolved,
)
from quantra_orchestrator.pricing.swap_ir.md_resolution import collect_quote_ids
from quantra_orchestrator.pricing.swap_ir.models import (
    ResolvedCurve,
    ResolvedQuoteValue,
)
from quantra_orchestrator.pricing.swaption.models import (
    ResolvedSwaptionModel,
    ResolvedVolSurface,
)
from quantra_orchestrator.settings import (
    OrchestratorSettings,
    get_orchestrator_settings,
)
from quantra_orchestrator.tracing import (
    TraceRecorder,
    elapsed_ms,
    record_engine_error,
    record_engine_request_wire,
    record_engine_response,
    record_error_stage,
    record_input,
    record_load_entities,
    serialize_fb_object,
    start_trace,
)

router = APIRouter(prefix="/v1", tags=["vol-tools"])

# ``product`` column tag for the trace of a vol-surface sample (the
# Investigate page is product-agnostic; this is just the label the timeline
# and a future recent-calls list carry).
_SAMPLE_PRODUCT: str = "vol_sample"


def _get_app_rw_engine_optional(request: Request) -> AsyncEngine | None:
    """Resolve the lifespan-owned ``app_rw`` engine without raising.

    Trace capture writes to ``app.pricing_traces`` via ``app_rw``. Mirrors the
    pricing routes' optional accessor: a missing engine returns ``None`` so
    :func:`start_trace` is a silent no-op rather than a 503 — tracing must never
    turn a successful sample into an error.
    """

    engine: AsyncEngine | None = getattr(request.app.state, "app_rw_engine", None)
    return engine


class _Packable(Protocol):
    """A generated FlatBuffers ``*T`` object able to pack itself into a builder."""

    def Pack(self, builder: flatbuffers.Builder) -> int: ...


_TIME_UNIT_BY_NAME: dict[str, int] = {
    "Days": 0,
    "Weeks": 7,
    "Months": 5,
    "Years": 8,
}
_STRIKE_AXIS_BY_NAME: dict[str, int] = {
    "Absolute": SwaptionStrikeKind.Absolute,
    # The portal names the absolute axis ``AbsoluteStrike``; the engine enum
    # member is ``Absolute``. Both map to the same wire value.
    "AbsoluteStrike": SwaptionStrikeKind.Absolute,
    "SpreadFromATM": SwaptionStrikeKind.SpreadFromATM,
}
_OUTPUT_MODE_BY_NAME: dict[str, int] = {
    "Cube": VolOutputMode.Cube,
    "SmileSlice": VolOutputMode.SmileSlice,
    "TermSlice": VolOutputMode.TermSlice,
    "ExpirySlice": VolOutputMode.ExpirySlice,
}


def _decode_str(value: object) -> object:
    return value.decode() if isinstance(value, (bytes, bytearray)) else value


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class Tenor(BaseModel):
    """A ``{n, unit}`` tenor (unit is a QuantLib TimeUnit name)."""

    n: int
    unit: str = "Years"


class PricingInput(BaseModel):
    """Inline ``Pricing`` payload the vol tools translate server-side.

    ``curves`` follow the pricing-path curve shape (inline rates and/or
    ``quote_id`` points); ``vol_surfaces`` carry a constant-vol swaption
    surface (``kind='SwaptionVolSpec'``, ``payload.base.constant_vol`` or
    ``payload.base.quote_id``); ``models`` are optional swaption models.
    """

    model_config = ConfigDict(extra="allow")

    as_of_date: str | None = None
    curves: list[dict[str, Any]] = Field(default_factory=list)
    vol_surfaces: list[dict[str, Any]] = Field(default_factory=list)
    models: list[dict[str, Any]] = Field(default_factory=list)
    # Swap-index definitions the vol surface / calibration reference by id (the
    # engine resolves a swaption surface's ``swap_index_id`` against these). Each:
    # ``{id, kind?, float_index_id, spot_days?, calendar?,
    #    fixed: {frequency?, day_counter?}, float: {tenor: {n, unit}}}``.
    swap_indices: list[dict[str, Any]] = Field(default_factory=list)


class VolSampleQuery(BaseModel):
    """One vol-surface sampling query.

    Accepts the portal's engine-native grid shape (``expiry_grid`` /
    ``tenor_grid`` / ``strike_grid`` objects + ``output_mode`` / ``options`` /
    slice params, all mirroring the engine's ``VolQuerySpec``) AND the legacy
    flat shape (``expiry_tenors`` / ``swap_tenors`` / ``strikes`` /
    ``strike_axis``) for back-compat. When both are present the rich shape wins.

    The engine requires a strike grid on every query — an omitted one makes the
    request unparseable — so the rich shape (which always carries ``strike_grid``)
    is what actually lets the Vol Workbench sample.
    """

    model_config = ConfigDict(extra="allow")

    vol_id: str = Field(description="Id of the vol surface to sample (matches a surface name/id).")
    surface_type: str = Field(default="Swaption")
    swap_index_id: str | None = None
    discounting_curve_id: str | None = None
    forwarding_curve_id: str | None = None

    # Rich (portal / engine-native) shape.
    expiry_grid: dict[str, Any] | None = None
    tenor_grid: dict[str, Any] | None = None
    strike_grid: dict[str, Any] | None = None
    output_mode: str | None = None
    options: dict[str, Any] | None = None
    slice_expiry_index: int | None = None
    slice_tenor_index: int | None = None
    slice_strike: float | None = None
    slice_strike_is_set: bool | None = None

    # Legacy flat shape.
    expiry_tenors: list[Tenor] = Field(default_factory=list)
    swap_tenors: list[Tenor] = Field(default_factory=list)
    strikes: list[float] = Field(default_factory=list)
    strike_axis: str = Field(default="Absolute")


class VolSampleRequest(BaseModel):
    pricing: PricingInput
    queries: list[VolSampleQuery] = Field(default_factory=list)
    include_diagnostics: bool = False


class CalibrateVolRequest(BaseModel):
    pricing: PricingInput
    vol_id: str
    discounting_curve_id: str
    forwarding_curve_id: str


class CalibrationSpec(BaseModel):
    swaption_vol_id: str
    discount_curve_id: str
    forwarding_curve_id: str
    swap_index_id: str | None = None
    expiries: list[Tenor] = Field(default_factory=list)
    tenors: list[Tenor] = Field(default_factory=list)
    calibrate_a: bool = True
    calibrate_sigma: bool = True
    a_init: float = 0.03
    sigma_init: float = 0.01
    max_iterations: int = 200
    function_evaluations: int = 1000
    end_criteria_eps: float = 1e-8


class CalibrateModelRequest(BaseModel):
    pricing: PricingInput
    model_id: str
    calibration: CalibrationSpec


_ENGINE_ERR_RESPONSES: dict[int | str, dict[str, str]] = {
    401: {"description": "Missing or invalid credentials."},
    422: {"description": "Untranslatable curve / surface, or unresolved quote id."},
    502: {"description": "Engine failure."},
    503: {"description": "MD service or engine unavailable."},
}


# ---------------------------------------------------------------------------
# Shared pricing assembly
# ---------------------------------------------------------------------------


def _parse_as_of(raw: str | None) -> date:
    if not raw:
        return date.today()
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid pricing.as_of_date: {raw!r}.",
        ) from None


def _as_resolved_curve(raw: dict[str, Any], index: int) -> ResolvedCurve:
    name = str(raw.get("name") or raw.get("id") or f"curve-{index}")
    reference_date = raw.get("reference_date")
    parsed_ref: date | None = None
    if isinstance(reference_date, str) and reference_date:
        try:
            parsed_ref = date.fromisoformat(reference_date[:10])
        except ValueError:
            parsed_ref = None
    return ResolvedCurve(
        name=name,
        currency=raw.get("currency"),
        day_counter=raw.get("day_counter"),
        helper_kind=raw.get("helper_kind"),
        reference_date=parsed_ref,
        points=list(raw.get("points") or []),
        body={k: v for k, v in raw.items() if k not in {"name", "id", "points"}},
    )


def _vol_surface_quote_ids(raw: dict[str, Any]) -> list[str]:
    """The single ``quote_id`` a constant-vol surface base may reference.

    The base envelope sits either at ``payload.base`` (legacy / flat shape) or
    one level deeper at ``payload.payload.base`` (the shape the portal emits,
    where the outer ``payload`` carries ``swap_index_id`` + ``payload_type``).
    Matrix surfaces carry inline grid values, not a base quote, so they yield
    nothing here.
    """

    payload = raw.get("payload")
    if not isinstance(payload, dict):
        return []
    base = payload.get("base")
    if not isinstance(base, dict):
        inner = payload.get("payload")
        base = inner.get("base") if isinstance(inner, dict) else None
    if not isinstance(base, dict):
        return []
    quote = base.get("quote_id") or base.get("quoteId")
    return [str(quote)] if isinstance(quote, str) and quote else []


async def _resolve_pricing(
    pricing_in: PricingInput,
    *,
    md_client: MdClient,
) -> PricingT:
    """Translate the inline ``pricing`` payload into a faithful ``PricingT``.

    Resolves every curve + vol-surface ``quote_id`` server-side through the MD
    client (invariant #8) and substitutes the values via the shared
    :func:`build_pricing_from_resolved`. Translation / resolution failures are
    clean 422s.
    """

    as_of = _parse_as_of(pricing_in.as_of_date)
    curves = [
        _as_resolved_curve(raw, i)
        for i, raw in enumerate(pricing_in.curves)
        if isinstance(raw, dict)
    ]
    if not curves:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="pricing.curves must contain at least one curve.",
        )
    vol_surfaces = [
        ResolvedVolSurface(
            name=str(raw.get("name") or raw.get("id") or f"vol-{i}"),
            kind=str(raw.get("kind") or "SwaptionVolSpec"),
            payload=dict(raw.get("payload") or {}),
        )
        for i, raw in enumerate(pricing_in.vol_surfaces)
        if isinstance(raw, dict)
    ]
    models = [
        ResolvedSwaptionModel(
            name=str(raw.get("name") or raw.get("id") or f"model-{i}"),
            kind=str(raw.get("kind") or "HullWhiteLattice"),
            payload=dict(raw.get("payload") or {}),
        )
        for i, raw in enumerate(pricing_in.models)
        if isinstance(raw, dict)
    ]

    quote_ids = list(collect_quote_ids(curves))
    for raw in pricing_in.vol_surfaces:
        if isinstance(raw, dict):
            quote_ids.extend(_vol_surface_quote_ids(raw))
    quote_ids = list(dict.fromkeys(quote_ids))
    resolved_quotes = await _resolve_quotes(quote_ids, as_of, md_client)

    resolved = ResolvedMarketData(
        as_of=as_of.isoformat(),
        curves=tuple(curves),
        quotes=tuple(resolved_quotes),
        vol_surfaces=tuple(vol_surfaces),
        models=tuple(models),
    )
    try:
        pricing = build_pricing_from_resolved(resolved)
    except TranslationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Pricing translation failed: {exc}",
        ) from exc

    # Swap indices the swaption surface / calibration reference by id. The shared
    # translator does not build these (they are utility-route specific), so they
    # are attached to the translated ``rates`` graph here.
    swap_indices = [
        _build_swap_index(raw) for raw in pricing_in.swap_indices if isinstance(raw, dict)
    ]
    if swap_indices and pricing.rates is not None:
        pricing.rates.swapIndices = swap_indices
    return pricing


def _enum_val(enum_cls: type, raw: object, default: int) -> int:
    """Resolve an enum member name / int to its value, falling back to ``default``."""

    if isinstance(raw, bool):
        return default
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        member = getattr(enum_cls, raw, None)
        if isinstance(member, int):
            return int(member)
    return default


def _build_swap_index(raw: dict[str, Any]) -> SwapIndexDefT:
    """Build one ``SwapIndexDef`` from an inline swap-index spec.

    The swaption vol surface / calibration reference a swap index by id; the
    engine resolves it against ``rates.swapIndices``. Sensible EUR conventions
    (Annual / Thirty360 fixed leg, 6M float tenor, TARGET) fill any gaps so the
    caller need only supply ``id`` + ``float_index_id`` in the common case.
    """

    fixed_raw = raw.get("fixed")
    fixed_raw = fixed_raw if isinstance(fixed_raw, dict) else {}
    float_raw = raw.get("float")
    float_raw = float_raw if isinstance(float_raw, dict) else {}
    tenor_raw = float_raw.get("tenor")
    tenor_raw = tenor_raw if isinstance(tenor_raw, dict) else {}

    fixed_leg = SwapIndexFixedLegSpecT()
    fixed_leg.fixedFrequency = _enum_val(Frequency, fixed_raw.get("frequency"), Frequency.Annual)
    fixed_leg.fixedDayCounter = _enum_val(
        DayCounter, fixed_raw.get("day_counter"), DayCounter.Thirty360
    )
    fixed_leg.fixedCalendar = _enum_val(
        Calendar, fixed_raw.get("calendar") or raw.get("calendar"), Calendar.TARGET
    )
    fixed_leg.fixedBdc = _enum_val(
        BusinessDayConvention,
        fixed_raw.get("business_day_convention"),
        BusinessDayConvention.ModifiedFollowing,
    )
    # Presence-required since engine 0.5.0 (#118); the defaults are the
    # engine-0.2.0 schema defaults these legs always priced with.
    fixed_leg.fixedTermBdc = _enum_val(
        BusinessDayConvention,
        fixed_raw.get("termination_date_convention"),
        BusinessDayConvention.ModifiedFollowing,
    )
    fixed_leg.fixedDateRule = _enum_val(
        DateGenerationRule,
        fixed_raw.get("date_generation_rule"),
        DateGenerationRule.Forward,
    )
    fixed_leg.fixedEom = bool(fixed_raw.get("end_of_month", False))

    float_leg = SwapIndexFloatLegSpecT()
    float_tenor = PeriodT()
    float_tenor.n = int(tenor_raw.get("n", 6))
    float_tenor.unit = _TIME_UNIT_BY_NAME.get(
        str(tenor_raw.get("unit", "Months")), _TIME_UNIT_BY_NAME["Months"]
    )
    float_leg.floatTenor = float_tenor
    float_leg.floatCalendar = _enum_val(
        Calendar, float_raw.get("calendar") or raw.get("calendar"), Calendar.TARGET
    )
    # Presence-required since engine 0.5.0 (#118); engine-0.2.0 schema defaults.
    float_leg.floatBdc = _enum_val(
        BusinessDayConvention,
        float_raw.get("business_day_convention"),
        BusinessDayConvention.ModifiedFollowing,
    )
    float_leg.floatTermBdc = _enum_val(
        BusinessDayConvention,
        float_raw.get("termination_date_convention"),
        BusinessDayConvention.ModifiedFollowing,
    )
    float_leg.floatDateRule = _enum_val(
        DateGenerationRule,
        float_raw.get("date_generation_rule"),
        DateGenerationRule.Forward,
    )
    float_leg.floatEom = bool(float_raw.get("end_of_month", False))

    spec = SwapIndexDefT()
    spec.id = str(raw.get("id") or DEFAULT_SWAP_INDEX_ID)
    spec.kind = _enum_val(SwapIndexKind, raw.get("kind"), SwapIndexKind.IborSwapIndex)
    spec.spotDays = int(raw.get("spot_days", 2))
    spec.calendar = _enum_val(Calendar, raw.get("calendar"), Calendar.TARGET)
    spec.businessDayConvention = _enum_val(
        BusinessDayConvention,
        raw.get("business_day_convention"),
        BusinessDayConvention.ModifiedFollowing,
    )
    # Presence-required since engine 0.5.0 (#118); False == engine-0.2.0 default.
    spec.endOfMonth = bool(raw.get("end_of_month", False))
    spec.fixedLeg = fixed_leg
    spec.floatIndexId = str(raw.get("float_index_id") or "forwarding_index")
    spec.floatLeg = float_leg
    return spec


async def _resolve_quotes(
    quote_ids: list[str], as_of: date, md_client: MdClient
) -> list[ResolvedQuoteValue]:
    if not quote_ids:
        return []
    try:
        resolved = await md_client.resolve_quotes(quote_ids, as_of)
    except MdClientError as exc:
        raise map_md_client_error(exc, canonical_id=quote_ids[0], as_of=as_of.isoformat()) from exc
    misses = [r.canonical_id for r in resolved if not r.found or r.value is None]
    if misses:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Could not resolve {len(misses)} quote id(s) at "
                f"{as_of.isoformat()}: " + ", ".join(sorted(misses)[:10])
            ),
        )
    return [
        ResolvedQuoteValue(
            canonical_id=r.canonical_id,
            as_of=as_of,
            value=float(r.value),
            source=getattr(r, "source", None),
        )
        for r in resolved
        if r.value is not None
    ]


def _tenor_grid(tenors: list[Tenor]) -> DateGridSpecT:
    """Build a ``DateGridSpec`` (TenorGrid of Periods) from ``{n, unit}`` tenors."""

    grid = TenorGridT()
    grid.tenors = []
    for t in tenors:
        period = PeriodT()
        period.n = int(t.n)
        period.unit = _TIME_UNIT_BY_NAME.get(t.unit, _TIME_UNIT_BY_NAME["Years"])
        grid.tenors.append(period)
    spec = DateGridSpecT()
    spec.gridType = DateGrid.TenorGrid
    spec.grid = grid
    return spec


def _periods(tenors: list[Tenor]) -> list[PeriodT]:
    out: list[PeriodT] = []
    for t in tenors:
        period = PeriodT()
        period.n = int(t.n)
        period.unit = _TIME_UNIT_BY_NAME.get(t.unit, _TIME_UNIT_BY_NAME["Years"])
        out.append(period)
    return out


def _period_from_obj(raw: dict[str, Any]) -> PeriodT:
    period = PeriodT()
    period.n = int(raw.get("n", 0) or 0)
    period.unit = _TIME_UNIT_BY_NAME.get(str(raw.get("unit", "Years")), _TIME_UNIT_BY_NAME["Years"])
    return period


def _date_grid_spec_from_obj(obj: dict[str, Any]) -> DateGridSpecT:
    """Build a ``DateGridSpec`` from the portal's ``{grid_type, grid}`` shape.

    Supports both a ``TenorGrid`` (a list of ``{n, unit}`` tenors) and a
    ``RangeGrid`` (start/end + step), mirroring the engine's ``DateGridSpec``
    union.
    """

    grid_type = str(obj.get("grid_type") or obj.get("gridType") or "TenorGrid")
    grid_obj = obj.get("grid")
    body: dict[str, Any] = grid_obj if isinstance(grid_obj, dict) else {}
    spec = DateGridSpecT()
    if grid_type == "RangeGrid":
        rng = RangeGridT()
        start = body.get("start_date") or body.get("startDate")
        end = body.get("end_date") or body.get("endDate")
        rng.startDate = str(start) if isinstance(start, str) and start else None
        rng.endDate = str(end) if isinstance(end, str) and end else None
        step_n = body.get("step_number", body.get("stepNumber", 1))
        rng.stepNumber = int(step_n) if isinstance(step_n, (int, float)) else 1
        rng.stepTimeUnit = _TIME_UNIT_BY_NAME.get(
            str(body.get("step_time_unit") or body.get("stepTimeUnit") or "Months"),
            _TIME_UNIT_BY_NAME["Months"],
        )
        rng.businessDaysOnly = bool(
            body.get("business_days_only", body.get("businessDaysOnly", False))
        )
        rng.calendar = _enum_val(Calendar, body.get("calendar"), Calendar.TARGET)
        rng.businessDayConvention = _enum_val(
            BusinessDayConvention,
            body.get("business_day_convention"),
            BusinessDayConvention.ModifiedFollowing,
        )
        spec.gridType = DateGrid.RangeGrid
        spec.grid = rng
        return spec

    grid = TenorGridT()
    tenors_obj = body.get("tenors")
    tenors_raw: list[Any] = tenors_obj if isinstance(tenors_obj, list) else []
    grid.tenors = [_period_from_obj(t) for t in tenors_raw if isinstance(t, dict)]
    grid.calendar = _enum_val(Calendar, body.get("calendar"), Calendar.TARGET)
    grid.businessDayConvention = _enum_val(
        BusinessDayConvention,
        body.get("business_day_convention"),
        BusinessDayConvention.ModifiedFollowing,
    )
    spec.gridType = DateGrid.TenorGrid
    spec.grid = grid
    return spec


def _strike_grid_from_obj(obj: dict[str, Any]) -> StrikeGridT:
    """Build a ``StrikeGrid`` from the portal's ``{axis, strikes}`` shape."""

    grid = StrikeGridT()
    grid.axis = _STRIKE_AXIS_BY_NAME.get(
        str(obj.get("axis") or "Absolute"), SwaptionStrikeKind.Absolute
    )
    strikes_obj = obj.get("strikes")
    strikes_raw: list[Any] = strikes_obj if isinstance(strikes_obj, list) else []
    grid.strikes = [
        float(s) for s in strikes_raw if isinstance(s, (int, float)) and not isinstance(s, bool)
    ]
    return grid


def _query_options_from_obj(obj: dict[str, Any]) -> QueryOptionsT:
    """Build ``QueryOptions`` from the portal's ``{allow_extrapolation, max_points}``."""

    opt = QueryOptionsT()
    max_points = obj.get("max_points", obj.get("maxPoints"))
    if isinstance(max_points, (int, float)) and not isinstance(max_points, bool):
        opt.maxPoints = int(max_points)
    allow = obj.get("allow_extrapolation", obj.get("allowExtrapolation"))
    if isinstance(allow, bool):
        opt.allowExtrapolation = allow
    strict = obj.get("strict")
    if isinstance(strict, bool):
        opt.strict = strict
    opt.calendar = _enum_val(Calendar, obj.get("calendar"), opt.calendar)
    opt.businessDayConvention = _enum_val(
        BusinessDayConvention,
        obj.get("business_day_convention"),
        opt.businessDayConvention,
    )
    return opt


def _apply_query_grids(spec: VolQuerySpecT, q: VolSampleQuery) -> None:
    """Populate the expiry / tenor / strike grids (rich shape wins over legacy).

    The engine requires a strike grid on every query — an omitted one makes the
    request unparseable — so the rich ``{axis, strikes}`` shape (which the portal
    always sends) is what actually lets the Vol Workbench sample.
    """

    if q.expiry_grid is not None:
        spec.expiryGrid = _date_grid_spec_from_obj(q.expiry_grid)
    elif q.expiry_tenors:
        spec.expiryGrid = _tenor_grid(q.expiry_tenors)

    if q.tenor_grid is not None:
        spec.tenorGrid = _date_grid_spec_from_obj(q.tenor_grid)
    elif q.swap_tenors:
        spec.tenorGrid = _tenor_grid(q.swap_tenors)

    if q.strike_grid is not None:
        spec.strikeGrid = _strike_grid_from_obj(q.strike_grid)
    elif q.strikes:
        strike_grid = StrikeGridT()
        strike_grid.axis = _STRIKE_AXIS_BY_NAME.get(q.strike_axis, SwaptionStrikeKind.Absolute)
        strike_grid.strikes = list(q.strikes)
        spec.strikeGrid = strike_grid


def _apply_query_slices(spec: VolQuerySpecT, q: VolSampleQuery) -> None:
    """Populate the optional slice controls (SmileSlice / TermSlice / ExpirySlice)."""

    if q.slice_expiry_index is not None:
        spec.sliceExpiryIndex = int(q.slice_expiry_index)
    if q.slice_tenor_index is not None:
        spec.sliceTenorIndex = int(q.slice_tenor_index)
    if q.slice_strike is not None:
        spec.sliceStrike = float(q.slice_strike)
    if q.slice_strike_is_set is not None:
        spec.sliceStrikeIsSet = bool(q.slice_strike_is_set)


def _build_vol_query_spec(q: VolSampleQuery) -> VolQuerySpecT:
    """Translate one :class:`VolSampleQuery` into an engine ``VolQuerySpec``.

    Accepts both the rich portal shape (``expiry_grid`` / ``tenor_grid`` /
    ``strike_grid`` objects + ``output_mode`` / ``options`` / slice params) and
    the legacy flat shape (``expiry_tenors`` / ``swap_tenors`` / ``strikes``),
    the rich shape winning where both are present.
    """

    spec = VolQuerySpecT()
    spec.volId = q.vol_id
    spec.surfaceType = getattr(VolSurfaceType, q.surface_type, VolSurfaceType.Swaption)
    _apply_query_grids(spec, q)
    _apply_query_slices(spec, q)
    if q.options is not None:
        spec.options = _query_options_from_obj(q.options)
    if q.output_mode is not None:
        spec.outputMode = _OUTPUT_MODE_BY_NAME.get(q.output_mode, VolOutputMode.Cube)
    spec.swapIndexId = q.swap_index_id or DEFAULT_SWAP_INDEX_ID
    if q.discounting_curve_id:
        spec.discountingCurveId = q.discounting_curve_id
    if q.forwarding_curve_id:
        spec.forwardingCurveId = q.forwarding_curve_id
    return spec


async def _call(engine_client: EngineClient, rpc: EngineRpc, request: _Packable) -> bytes:
    builder = flatbuffers.Builder(2048)
    # ForceDefaults: the engine requires every set field explicit on the wire;
    # zero-default enums/scalars (e.g. Frequency.Annual == 0) must not be omitted.
    builder.ForceDefaults(True)
    builder.Finish(request.Pack(builder))
    try:
        return await engine_client.call(rpc, bytes(builder.Output()))
    except EngineClientError as exc:
        raise map_engine_client_error(exc) from exc


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _decode_sample_request_wire(request_bytes: bytes) -> dict[str, Any]:
    """Decode the EXACT ``SampleVolSurfaces`` request bytes into a JSON view.

    Reads the transmitted buffer with the generated FlatBuffers reader so the
    result is provably derived from the bytes on the wire (not a re-pack), then
    renders it JSON-safe via the shared :func:`serialize_fb_object`. Best-effort:
    a malformed buffer degrades to a marker dict, never raises — tracing must
    never disturb a sample.
    """

    try:
        request = SampleVolSurfacesRequestT.InitFromPackedBuf(request_bytes, 0)
    except Exception as exc:  # tracing is best-effort — never raise
        return {"__undecodable__": True, "exc_type": type(exc).__name__}
    return serialize_fb_object(request)


def _record_sample_input(trace: TraceRecorder, payload: VolSampleRequest) -> None:
    """Emit the ``input`` stage for a vol sample: as_of + queries + shape."""

    pricing = payload.pricing
    record_input(
        trace,
        product=_SAMPLE_PRODUCT,
        params={
            "as_of": pricing.as_of_date,
            "query_count": len(payload.queries),
            "vol_ids": [q.vol_id for q in payload.queries],
            "curve_count": len(pricing.curves),
            "surface_count": len(pricing.vol_surfaces),
            "include_diagnostics": payload.include_diagnostics,
        },
    )


def _record_sample_resolve(
    trace: TraceRecorder, payload: VolSampleRequest, stage_started: float
) -> None:
    """Emit the ``load_entities`` stage: the resolved curves / surfaces / indices.

    ``_resolve_pricing`` translates the inline curves + vol surface(s) and
    resolves every ``quote_id`` server-side (invariant #8) in one step, so this
    single stage summarizes what that resolve/translate produced rather than
    splitting it into separate load/md_resolve stages the way a saved-entity
    product route does.
    """

    pricing = payload.pricing
    curve_names = [
        str(c.get("name") or c.get("id") or f"curve-{i}")
        for i, c in enumerate(pricing.curves)
        if isinstance(c, dict)
    ]
    surface_names = [
        str(s.get("name") or s.get("id") or f"vol-{i}")
        for i, s in enumerate(pricing.vol_surfaces)
        if isinstance(s, dict)
    ]
    swap_index_ids = [
        str(s.get("id")) for s in pricing.swap_indices if isinstance(s, dict) and s.get("id")
    ]
    record_load_entities(
        trace,
        {
            "curve_names": curve_names,
            "vol_surface_names": surface_names,
            "swap_index_ids": swap_index_ids,
        },
        duration_ms=elapsed_ms(stage_started),
    )


def _record_sample_engine_request(
    trace: TraceRecorder,
    payload: VolSampleRequest,
    capturing_engine: CapturingEngineClient,
) -> None:
    """Emit the ``engine_request`` stage with the wire view for a vol sample.

    Records both the orchestrator's small assembled-inputs summary AND the exact
    ``SampleVolSurfaces`` bytes transmitted to the engine (base64 +
    decoded-from-those-bytes JSON via :func:`_decode_sample_request_wire`).
    ``capturing_engine.last_request`` is ``None`` on a pre-send failure, recorded
    as ``sent: false``. Gated on ``trace.enabled`` inside the shared helper.
    """

    assembled_request = {
        "as_of": payload.pricing.as_of_date,
        "query_count": len(payload.queries),
        "vol_ids": [q.vol_id for q in payload.queries],
        "include_diagnostics": payload.include_diagnostics,
    }
    record_engine_request_wire(
        trace,
        assembled_request=assembled_request,
        capturing_engine=capturing_engine,
        decode=_decode_sample_request_wire,
    )


def _summarize_sample_response(
    results: list[dict[str, Any]], diagnostics: list[dict[str, Any] | None]
) -> dict[str, Any]:
    """Build the ``engine_response`` payload for a vol sample (per-surface points)."""

    return {
        "result_count": len(results),
        "diagnostics_count": len(diagnostics),
        "results": [
            {
                "vol_id": r.get("vol_id"),
                "reference_date": r.get("reference_date"),
                "n_expiries": r.get("n_expiries"),
                "n_tenors": r.get("n_tenors"),
                "n_strikes": r.get("n_strikes"),
                "n_vols": len(r.get("vols") or []),
                "error": r.get("error"),
            }
            for r in results
        ],
    }


@router.post(
    "/vol-surfaces/sample",
    summary="Sample a vol surface over an expiry / tenor / strike grid",
    responses=_ENGINE_ERR_RESPONSES,
)
async def sample_vol_surfaces(
    payload: VolSampleRequest,
    http_request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    rw_engine: AsyncEngine | None = Depends(_get_app_rw_engine_optional),
    md_client: MdClient = Depends(get_md_client),
    engine_client: EngineClient = Depends(get_engine_client),
    settings: OrchestratorSettings = Depends(get_orchestrator_settings),
) -> dict[str, Any]:
    # in-app pricing trace. Buffers the orchestrator's per-stage view of
    # this sample keyed by request_id, flushed best-effort post-response by
    # ``TraceFlushMiddleware`` so the portal's Investigate page can show the
    # timeline. A no-op when ``TRACE_CAPTURE`` is off or no ``app_rw`` engine is
    # configured — capture never turns a successful sample into an error.
    trace = start_trace(
        http_request,
        owner_uid=ctx.uid,
        rw_engine=rw_engine,
        settings=settings,
        product=_SAMPLE_PRODUCT,
    )
    _record_sample_input(trace, payload)

    try:
        stage_started = time.monotonic()
        pricing = await _resolve_pricing(payload.pricing, md_client=md_client)
        _record_sample_resolve(trace, payload, stage_started)

        queries = [
            _build_vol_query_spec(q) for q in (payload.queries or [VolSampleQuery(vol_id="")])
        ]

        request = SampleVolSurfacesRequestT()
        request.pricing = pricing
        request.queries = queries
        request.includeDiagnostics = payload.include_diagnostics

        # wrap the engine so the EXACT FlatBuffers bytes handed to gRPC
        # are captured at the send boundary for the ``engine_request`` stage.
        # Inlined (vs ``_call``) so the RAW engine error is captured on the
        # ``engine_response`` stage before it is mapped to the envelope.
        capturing_engine = CapturingEngineClient(engine_client)
        builder = flatbuffers.Builder(2048)
        # ForceDefaults: the engine requires every set field explicit on the wire;
        # zero-default enums/scalars (e.g. Frequency.Annual == 0) must not be omitted.
        builder.ForceDefaults(True)
        builder.Finish(request.Pack(builder))
        engine_started = time.monotonic()
        try:
            response_bytes = await capturing_engine.call(
                EngineRpc.SAMPLE_VOL_SURFACES, bytes(builder.Output())
            )
        except EngineClientError as exc:
            # The request bytes WERE put on the wire; record the engine_request
            # stage + the engine's REAL error text on ``engine_response`` before
            # mapping to the envelope (the outer handler records ``error``).
            _record_sample_engine_request(trace, payload, capturing_engine)
            record_engine_error(trace, exc, duration_ms=elapsed_ms(engine_started))
            raise map_engine_client_error(exc) from exc
        _record_sample_engine_request(trace, payload, capturing_engine)

        decoded = SampleVolSurfacesResponseT.InitFromObj(
            SampleVolSurfacesResponse.GetRootAs(bytearray(response_bytes), 0)
        )
        results = [_decode_vol_sample(s) for s in (decoded.results or [])]
        diagnostics = [_decode_diagnostics(d) for d in (decoded.diagnostics or [])]
        record_engine_response(
            trace,
            _summarize_sample_response(results, diagnostics),
            duration_ms=elapsed_ms(engine_started),
        )
        return {"results": results, "diagnostics": diagnostics}
    except HTTPException as exc:
        # close the timeline on the same failure the client got
        # (translation 422, unresolved-quote 422, or engine 502).
        record_error_stage(trace, exc)
        raise


def _decode_vol_sample(sample: VolSurfaceSampleT) -> dict[str, Any]:
    return {
        "vol_id": _decode_str(sample.volId),
        "reference_date": _decode_str(sample.referenceDate),
        "n_expiries": int(sample.nExpiries),
        "n_tenors": int(sample.nTenors),
        "n_strikes": int(sample.nStrikes),
        "expiries": [_decode_str(e) for e in (sample.expiries or [])],
        "tenors": [{"n": int(p.n), "unit": int(p.unit)} for p in (sample.tenors or [])],
        "strikes": [float(s) for s in (sample.strikes or [])],
        "vols": [float(v) for v in (sample.vols or [])],
        "atm_levels": [float(a) for a in (sample.atmLevels or [])],
        "error": _decode_error(sample.error),
    }


def _decode_error(error: ErrorT | None) -> dict[str, Any] | None:
    if error is None:
        return None
    return {"error_message": _decode_str(getattr(error, "errorMessage", None))}


@router.post(
    "/calibrate-swaption-vol",
    summary="Calibrate (build) a swaption vol surface and return diagnostics",
    responses=_ENGINE_ERR_RESPONSES,
)
async def calibrate_swaption_vol(
    payload: CalibrateVolRequest,
    _ctx: AuthContext = Depends(get_auth_context),
    md_client: MdClient = Depends(get_md_client),
    engine_client: EngineClient = Depends(get_engine_client),
) -> dict[str, Any]:
    pricing = await _resolve_pricing(payload.pricing, md_client=md_client)

    request = CalibrateSwaptionVolRequestT()
    request.pricing = pricing
    request.volId = payload.vol_id
    request.discountingCurveId = payload.discounting_curve_id
    request.forwardingCurveId = payload.forwarding_curve_id

    response_bytes = await _call(engine_client, EngineRpc.CALIBRATE_SWAPTION_VOL, request)
    decoded = CalibrateSwaptionVolResponseT.InitFromObj(
        CalibrateSwaptionVolResponse.GetRootAs(bytearray(response_bytes), 0)
    )
    return {
        "vol_id": _decode_str(decoded.volId),
        "diagnostics": _decode_diagnostics(decoded.diagnostics),
    }


def _float_list(values: object) -> list[float]:
    """Render an optional FB float vector (list OR numpy array) as a plain list.

    Avoids ``values or []``, which raises on a multi-element numpy array (the
    generated ``InitFromObj`` decodes vectors via ``AsNumpy`` when numpy is
    importable).
    """

    if values is None:
        return []
    return [float(v) for v in values]  # type: ignore[attr-defined]


def _int_list(values: object) -> list[int]:
    """Integer analogue of :func:`_float_list`."""

    if values is None:
        return []
    return [int(v) for v in values]  # type: ignore[attr-defined]


def _decode_diagnostics(
    diag: SwaptionVolDiagnosticsT | None,
) -> dict[str, Any] | None:
    if diag is None:
        return None
    return {
        "vol_id": _decode_str(diag.volId),
        "kind": int(diag.kind),
        "n_expiries": int(diag.nExpiries),
        "n_tenors": int(diag.nTenors),
        "expiries": [{"n": int(p.n), "unit": int(p.unit)} for p in (diag.expiries or [])],
        "tenors": [{"n": int(p.n), "unit": int(p.unit)} for p in (diag.tenors or [])],
        "forward_per_node": _float_list(diag.forwardPerNode),
        "atm_vol_per_node": _float_list(diag.atmVolPerNode),
        "time_to_expiry_per_node": _float_list(diag.timeToExpiryPerNode),
        "alpha_per_node": _float_list(diag.alphaPerNode),
        "beta_per_node": _float_list(diag.betaPerNode),
        "rho_per_node": _float_list(diag.rhoPerNode),
        "nu_per_node": _float_list(diag.nuPerNode),
        "calibration": _decode_calibration(diag.calibration),
        "warnings": [_decode_str(w) for w in (diag.warnings or [])],
    }


def _decode_calibration(
    cal: SabrCalibrationDiagnosticsT | None,
) -> dict[str, Any] | None:
    """Decode the SABR calibration sub-block (present only for SabrCalibrate)."""

    if cal is None:
        return None
    return {
        "per_node_rmse": _float_list(cal.perNodeRmse),
        "per_node_max_abs_error": _float_list(cal.perNodeMaxAbsError),
        "overall_rmse": float(cal.overallRmse),
        "converged": bool(cal.converged),
        "iterations_per_node": _int_list(cal.iterationsPerNode),
        "strikes": _float_list(cal.strikes),
        "per_strike_fit_error": _float_list(cal.perStrikeFitError),
    }


@router.post(
    "/calibrate-swaption-model",
    summary="Calibrate a Hull-White swaption model to a vol surface",
    responses=_ENGINE_ERR_RESPONSES,
)
async def calibrate_swaption_model(
    payload: CalibrateModelRequest,
    _ctx: AuthContext = Depends(get_auth_context),
    md_client: MdClient = Depends(get_md_client),
    engine_client: EngineClient = Depends(get_engine_client),
) -> dict[str, Any]:
    pricing = await _resolve_pricing(payload.pricing, md_client=md_client)
    cal = payload.calibration

    spec = SwaptionHwCalibrationSpecT()
    spec.swaptionVolId = cal.swaption_vol_id
    spec.discountCurveId = cal.discount_curve_id
    spec.forwardingCurveId = cal.forwarding_curve_id
    spec.swapIndexId = cal.swap_index_id or DEFAULT_SWAP_INDEX_ID
    spec.expiries = _periods(cal.expiries)
    spec.tenors = _periods(cal.tenors)
    spec.calibrateA = cal.calibrate_a
    spec.calibrateSigma = cal.calibrate_sigma
    spec.aInit = cal.a_init
    spec.sigmaInit = cal.sigma_init
    spec.maxIterations = cal.max_iterations
    spec.functionEvaluations = cal.function_evaluations
    spec.endCriteriaEps = cal.end_criteria_eps

    request = CalibrateSwaptionModelRequestT()
    request.pricing = pricing
    request.modelId = payload.model_id
    request.calibration = spec

    response_bytes = await _call(engine_client, EngineRpc.CALIBRATE_SWAPTION_MODEL, request)
    decoded = CalibrateSwaptionModelResponseT.InitFromObj(
        CalibrateSwaptionModelResponse.GetRootAs(bytearray(response_bytes), 0)
    )
    return {
        "model_id": _decode_str(decoded.modelId),
        "hw_a": float(decoded.hwA),
        "hw_sigma": float(decoded.hwSigma),
        "rmse": float(decoded.rmse),
        "num_helpers": int(decoded.numHelpers),
        "grid_rows": int(decoded.gridRows),
        "grid_cols": int(decoded.gridCols),
        "grid_points": int(decoded.gridPoints),
    }


__all__ = ["router"]
