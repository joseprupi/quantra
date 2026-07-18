"""``POST /v1/curve-preview`` — server-side curve bootstrap preview.

Replaces the portal CurveBuilder's last legacy path: the preview used to
client-side-resolve quote ids and POST to the retired ``:8081`` HTTP engine
via the Vite proxy. This endpoint runs the same flow on the real stack:

1. Accepts the portal's legacy ``{pricing, queries}`` request shape
   (curves with inline rates and/or ``quote_id`` points, unresolved —
   invariant #8: quote ids never leave the client resolved).
2. Resolves every ``quote_id`` through the MD client at ``as_of_date``.
3. Translates each curve with the shared :func:`translate_curve` (the same
   code the pricing pipeline uses) and calls the engine's
   ``BootstrapCurves`` RPC.
4. Returns the legacy JSON response shape (``results[].grid_dates`` /
   ``series[].measure`` / ``values`` / ``pillar_dates``) so the portal's
   ``CurveChart`` renders unchanged.

v1 scope (documented, deliberate): the query's ``measures`` are honored;
``grid`` / ``zero`` / ``fwd`` sub-specs ride on engine defaults. Indices
are not forwarded (the deposit/swap/OIS helper set the builder emits does
not require explicit index definitions engine-side).

Inflation arm (inflation half): when ``pricing.inflation_curves`` is
present the request is dispatched to the inflation bootstrap path instead of
the nominal-rates one. It resolves the inflation curve's helper ``quote_id``s
(plus any nominal discount curve's) through the *same* MD walker (invariant
#8), translates the resolved inflation index (body verbatim) + inflation
curve (helper values quote-substituted) with the shared pricing-path
:func:`translate_inflation_index` / :func:`translate_inflation_curve`, and
calls the engine's ``BootstrapInflationCurves`` RPC. The response reuses the
same legacy JSON shape (``results[].grid_dates`` / ``series`` /
``pillar_dates``) so the portal's inflation ``CurveChart`` renders unchanged;
the ``series[].measure`` names are the inflation set (``ZeroRate`` / ``YoYRate``).
The ZCIIS-vs-YYIIS kind is read off the inflation curve spec's ``kind`` field.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

import flatbuffers
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from quantra_common.auth.context import AuthContext
from quantra_common.engine_client import EngineClient, EngineClientError
from quantra_common.engine_client._generated.quantra.BootstrapCurvesRequest import (
    BootstrapCurvesRequestT,
)
from quantra_common.engine_client._generated.quantra.BootstrapCurvesResponse import (
    BootstrapCurvesResponse,
    BootstrapCurvesResponseT,
)
from quantra_common.engine_client._generated.quantra.BootstrapInflationCurvesRequest import (
    BootstrapInflationCurvesRequestT,
)
from quantra_common.engine_client._generated.quantra.BootstrapInflationCurvesResponse import (
    BootstrapInflationCurvesResponse,
    BootstrapInflationCurvesResponseT,
)
from quantra_common.engine_client._generated.quantra.CurveQuerySpec import CurveQuerySpecT
from quantra_common.engine_client._generated.quantra.DateGrid import DateGrid
from quantra_common.engine_client._generated.quantra.DateGridSpec import DateGridSpecT
from quantra_common.engine_client._generated.quantra.enums.InflationCurveMeasure import (
    InflationCurveMeasure,
)
from quantra_common.engine_client._generated.quantra.InflationCurveQuerySpec import (
    InflationCurveQuerySpecT,
)
from quantra_common.engine_client._generated.quantra.InflationMarketData import (
    InflationMarketDataT,
)
from quantra_common.engine_client._generated.quantra.Period import PeriodT
from quantra_common.engine_client._generated.quantra.Pricing import PricingT
from quantra_common.engine_client._generated.quantra.RatesMarketData import RatesMarketDataT
from quantra_common.engine_client._generated.quantra.TenorGrid import TenorGridT
from quantra_common.engine_client.rpcs import EngineRpc
from quantra_common.md_client import MdClient, MdClientError
from quantra_orchestrator.auth.dependencies import get_auth_context
from quantra_orchestrator.engine import get_engine_client, map_engine_client_error
from quantra_orchestrator.md import get_md_client, map_md_client_error
from quantra_orchestrator.pricing._translator import (
    _KNOWN_IBOR_INDICES,
    _KNOWN_OVERNIGHT_INDICES,
    CurveTranslationError,
    IndexRegistrationError,
    InflationIndexTranslationError,
    _build_default_forwarding_index,
    _build_known_ibor_index,
    _build_known_overnight_index,
    _collect_helper_index_ids,
    translate_curve,
    translate_inflation_curve,
    translate_inflation_index,
)
from quantra_orchestrator.pricing.swap_ir.md_resolution import collect_quote_ids
from quantra_orchestrator.pricing.swap_ir.models import ResolvedCurve

router = APIRouter(prefix="/v1/curve-preview", tags=["curves:preview"])

_MEASURE_BY_NAME: dict[str, int] = {"DF": 0, "ZERO": 1, "FWD": 2}
_NAME_BY_MEASURE: dict[int, str] = {v: k for k, v in _MEASURE_BY_NAME.items()}
# Inflation series measures (mirrors the engine ``InflationCurveMeasure`` enum).
_INFLATION_MEASURE_BY_NAME: dict[str, int] = {
    "ZeroRate": InflationCurveMeasure.ZeroRate,
    "YoYRate": InflationCurveMeasure.YoYRate,
}
_INFLATION_NAME_BY_MEASURE: dict[int, str] = {v: k for k, v in _INFLATION_MEASURE_BY_NAME.items()}
# The inflation curve spec's ``kind`` → the pricing-path swap-kind discriminator.
_SWAP_KIND_ZERO_COUPON = "zero_coupon"
_SWAP_KIND_YEAR_ON_YEAR = "year_on_year"
_TIME_UNIT_BY_NAME: dict[str, int] = {"Days": 0, "Months": 5, "Weeks": 7, "Years": 8}
# Standard sampling ladder used when the query carries no (usable) grid.
_DEFAULT_TENORS: list[tuple[int, str]] = [
    (1, "Months"),
    (3, "Months"),
    (6, "Months"),
    (1, "Years"),
    (2, "Years"),
    (5, "Years"),
    (10, "Years"),
    (20, "Years"),
    (30, "Years"),
]


def _tenor_grid(raw_grid: dict[str, Any] | None) -> DateGridSpecT:
    """Build the REQUIRED ``CurveQuerySpec.grid`` (FB verifier rejects None).

    Maps the portal's ``{grid_type: 'TenorGrid', grid: {tenors: [{n, unit}]}}``
    shape; anything else falls back to the standard ladder. Calendar and
    business-day convention ride on the schema defaults.
    """

    tenors: list[tuple[int, str]] = []
    if raw_grid and raw_grid.get("grid_type") == "TenorGrid":
        for item in (raw_grid.get("grid") or {}).get("tenors") or []:
            if isinstance(item, dict) and isinstance(item.get("n"), int | float):
                unit = str(item.get("unit") or "Years")
                if unit in _TIME_UNIT_BY_NAME:
                    tenors.append((int(item["n"]), unit))
    if not tenors:
        tenors = _DEFAULT_TENORS
    grid = TenorGridT()
    grid.tenors = []
    for n, unit in tenors:
        period = PeriodT()
        period.n = n
        period.unit = _TIME_UNIT_BY_NAME[unit]
        grid.tenors.append(period)
    spec = DateGridSpecT()
    spec.gridType = DateGrid.TenorGrid
    spec.grid = grid
    return spec


class CurvePreviewRequest(BaseModel):
    """The portal's legacy bootstrap-curves request, accepted loosely."""

    model_config = ConfigDict(extra="allow")

    pricing: dict[str, Any]
    queries: list[dict[str, Any]] = Field(default_factory=list)


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
        reference_date=parsed_ref,
        points=list(raw.get("points") or []),
        body={
            key: raw[key]
            for key in ("interpolator", "bootstrap_trait", "bootstrapTrait", "role")
            if key in raw
        },
    )


def _decode(value: object) -> object:
    return value.decode() if isinstance(value, (bytes, bytearray)) else value


def _build_preview_indices(curves: list[ResolvedCurve]) -> list[Any]:
    """Register every index the curve helpers reference (mirrors the pricing translator).

    Same resolution order as the pricing path's ``_build_rates_indices``
    (minus the resolved instrument index, which a preview never has): the
    default forwarding index the swap helpers bootstrap against is always
    emitted; OIS-helper overnight refs come from the known-overnight catalog;
    SwapHelper term-IBOR refs (e.g. ``EURIBOR_6M``) come from the known-IBOR
    catalog (previously missing here — every IBOR SwapHelper preview 422ed
    while the same curve PRICED fine); an unknown ref surfaces an actionable
    422 instead of an opaque engine NOT_FOUND.
    """

    default_index = _build_default_forwarding_index()
    indices_by_id = {default_index.id: default_index}
    try:
        for index_id in _collect_helper_index_ids(curves):
            if index_id in indices_by_id:
                continue
            known_on = _KNOWN_OVERNIGHT_INDICES.get(index_id)
            if known_on is not None:
                indices_by_id[index_id] = _build_known_overnight_index(index_id, known_on)
                continue
            known_ibor = _KNOWN_IBOR_INDICES.get(index_id)
            if known_ibor is not None:
                indices_by_id[index_id] = _build_known_ibor_index(index_id, known_ibor)
                continue
            raise IndexRegistrationError(
                index_id,
                sorted(
                    set(indices_by_id) | set(_KNOWN_OVERNIGHT_INDICES) | set(_KNOWN_IBOR_INDICES)
                ),
            )
    except IndexRegistrationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    return list(indices_by_id.values())


@router.post(
    "",
    summary="Bootstrap curves for preview (resolves quote ids server-side)",
    responses={
        401: {"description": "Missing or invalid credentials."},
        422: {"description": "Unresolvable quote ids or untranslatable curve points."},
        502: {"description": "Engine failure."},
        503: {"description": "MD service or engine unavailable."},
    },
)
async def preview_curves(
    payload: CurvePreviewRequest,
    ctx: AuthContext = Depends(get_auth_context),
    md_client: MdClient = Depends(get_md_client),
    engine_client: EngineClient = Depends(get_engine_client),
) -> dict[str, Any]:
    pricing_in = payload.pricing or {}
    as_of_raw = str(pricing_in.get("as_of_date") or "")
    try:
        as_of = date.fromisoformat(as_of_raw[:10]) if as_of_raw else date.today()
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid pricing.as_of_date: {as_of_raw!r}.",
        ) from None

    # Inflation arm (inflation half): a request carrying ``inflation_curves``
    # bootstraps an inflation term structure, not a nominal-rates one.
    if pricing_in.get("inflation_curves"):
        return await _preview_inflation_curves(payload, pricing_in, as_of, md_client, engine_client)

    curves = [
        _as_resolved_curve(raw, i)
        for i, raw in enumerate(pricing_in.get("curves") or [])
        if isinstance(raw, dict)
    ]
    if not curves:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="pricing.curves must contain at least one curve.",
        )

    # 1. Resolve quote ids via MD (invariant #8: server-side only).
    quote_ids = collect_quote_ids(curves)
    quote_map = await _resolve_quote_map(quote_ids, as_of, md_client, label="quote")

    # 2. Translate curves with the shared pricing-path translator.
    missing_quotes: list[str] = []
    try:
        term_structures = [
            translate_curve(
                curve,
                quote_map,
                default_reference_date=as_of.isoformat(),
                missing_quotes=missing_quotes,
            )
            for curve in curves
        ]
    except CurveTranslationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Curve translation failed: {exc.reason}",
        ) from exc
    if missing_quotes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Unresolved quote id(s): " + ", ".join(sorted(set(missing_quotes))[:10]),
        )

    # 3. Assemble + call the engine.
    pricing = PricingT()
    pricing.asOfDate = as_of.isoformat()
    rates = RatesMarketDataT()
    rates.curves = term_structures
    rates.indices = _build_preview_indices(curves)
    pricing.rates = rates

    queries: list[CurveQuerySpecT] = []
    for raw_query in payload.queries or [{}]:
        spec = CurveQuerySpecT()
        spec.curveId = str(raw_query.get("curve_id") or curves[0].name)
        measures = raw_query.get("measures") or ["DF", "ZERO", "FWD"]
        spec.measures = [
            _MEASURE_BY_NAME[m] for m in measures if isinstance(m, str) and m in _MEASURE_BY_NAME
        ] or [0, 1, 2]
        spec.grid = _tenor_grid(raw_query.get("grid"))
        queries.append(spec)

    request = BootstrapCurvesRequestT()
    request.pricing = pricing
    request.queries = queries
    builder = flatbuffers.Builder(2048)
    # ForceDefaults: the engine requires every set field explicit on the wire;
    # zero-default enums/scalars (e.g. Frequency.Annual == 0) must not be omitted.
    builder.ForceDefaults(True)
    builder.Finish(request.Pack(builder))

    try:
        response_bytes = await engine_client.call(
            EngineRpc.BOOTSTRAP_CURVES, bytes(builder.Output())
        )
    except EngineClientError as exc:
        raise map_engine_client_error(exc) from exc

    # 4. Decode → the legacy JSON shape CurveChart consumes.
    return _decode_response(response_bytes)


def _decode_response(response_bytes: bytes) -> dict[str, Any]:
    decoded = BootstrapCurvesResponseT.InitFromObj(
        BootstrapCurvesResponse.GetRootAs(bytearray(response_bytes), 0)
    )
    results: list[dict[str, Any]] = []
    for result in decoded.results or []:
        error = result.error
        results.append(
            {
                "id": _decode(result.id),
                "reference_date": _decode(result.referenceDate),
                "grid_dates": [_decode(d) for d in (result.gridDates or [])],
                "pillar_dates": [_decode(d) for d in (result.pillarDates or [])],
                "series": [
                    {
                        "measure": _NAME_BY_MEASURE.get(int(series.measure), "DF"),
                        "values": [float(v) for v in (series.values or [])],
                    }
                    for series in (result.series or [])
                ],
                "error": (
                    {
                        "error_message": _decode(getattr(error, "errorMessage", None)),
                    }
                    if error is not None
                    else None
                ),
            }
        )
    return {"results": results}


async def _resolve_quote_map(
    quote_ids: list[str],
    as_of: date,
    md_client: MdClient,
    *,
    label: str,
) -> dict[str, float]:
    """Resolve every ``quote_id`` to a value via the MD walker (invariant #8).

    Shared by the nominal and inflation arms: a single batched
    ``resolve_quotes`` call, a batched 422 on any miss, and the MD
    client's MD error envelope bubbled on transport failure.
    """

    if not quote_ids:
        return {}
    try:
        resolved = await md_client.resolve_quotes(quote_ids, as_of)
    except MdClientError as exc:
        raise map_md_client_error(exc, canonical_id=quote_ids[0], as_of=as_of.isoformat()) from exc
    misses = [r.canonical_id for r in resolved if not r.found or r.value is None]
    if misses:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Could not resolve {len(misses)} {label} id(s) at "
                f"{as_of.isoformat()}: " + ", ".join(sorted(misses)[:10])
            ),
        )
    return {r.canonical_id: float(r.value) for r in resolved if r.value is not None}


# ---------------------------------------------------------------------------
# Inflation arm (inflation half)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ResolvedInflationIndex:
    """Lightweight ``ResolvedInflationIndexLike`` for :func:`translate_inflation_index`.

    The inflation index bypasses MD: its body (family / conventions /
    historical CPI fixings) is consumed verbatim. Built from the portal's
    inflation-index spec — accepted flat (``buildInflationIndexSpec`` shape) or
    nested under a ``body`` key.
    """

    index_id: str
    name: str
    currency: str | None
    day_counter: str | None
    body: Mapping[str, Any]
    id: UUID | None = None


@dataclass(frozen=True)
class _ResolvedInflationCurve:
    """Lightweight ``ResolvedCurveLike`` for :func:`translate_inflation_curve`.

    Unlike the nominal :class:`ResolvedCurve` (whose body keeps only a handful
    of keys) the inflation translator reads calendar / BDC / interpolator /
    bootstrap-accuracy / allow-extrapolation off the body, so the whole spec
    dict rides through as the body.
    """

    name: str
    day_counter: str | None
    reference_date: date | None
    points: list[Mapping[str, Any]]
    body: Mapping[str, Any]
    id: UUID | None = None


def _as_resolved_inflation_index(raw: Mapping[str, Any]) -> _ResolvedInflationIndex:
    nested = raw.get("body")
    body: Mapping[str, Any] = nested if isinstance(nested, Mapping) else raw
    index_id = str(
        raw.get("index_id") or raw.get("id") or body.get("index_id") or body.get("id") or ""
    )
    name = str(raw.get("name") or body.get("family_name") or body.get("familyName") or index_id)
    currency = raw.get("currency") or body.get("currency")
    day_counter = raw.get("day_counter") or body.get("day_counter")
    return _ResolvedInflationIndex(
        index_id=index_id,
        name=name,
        currency=currency if isinstance(currency, str) else None,
        day_counter=day_counter if isinstance(day_counter, str) else None,
        body=body,
    )


def _as_resolved_inflation_curve(raw: Mapping[str, Any], index: int) -> _ResolvedInflationCurve:
    name = str(raw.get("id") or raw.get("name") or f"inflation-curve-{index}")
    reference_date = raw.get("reference_date")
    parsed_ref: date | None = None
    if isinstance(reference_date, str) and reference_date:
        try:
            parsed_ref = date.fromisoformat(reference_date[:10])
        except ValueError:
            parsed_ref = None
    points = [p for p in (raw.get("points") or []) if isinstance(p, Mapping)]
    return _ResolvedInflationCurve(
        name=name,
        day_counter=raw.get("day_counter") if isinstance(raw.get("day_counter"), str) else None,
        reference_date=parsed_ref,
        points=points,
        body=raw,
    )


def _swap_kind_of(raw_curve: Mapping[str, Any]) -> str:
    kind = str(raw_curve.get("kind") or "").strip()
    return _SWAP_KIND_YEAR_ON_YEAR if kind == "YoYInflation" else _SWAP_KIND_ZERO_COUPON


async def _preview_inflation_curves(
    payload: CurvePreviewRequest,
    pricing_in: dict[str, Any],
    as_of: date,
    md_client: MdClient,
    engine_client: EngineClient,
) -> dict[str, Any]:
    raw_inflation = [c for c in (pricing_in.get("inflation_curves") or []) if isinstance(c, dict)]
    if not raw_inflation:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="pricing.inflation_curves must contain at least one curve.",
        )
    raw_indices = [i for i in (pricing_in.get("inflation_indices") or []) if isinstance(i, dict)]
    if not raw_indices:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="pricing.inflation_indices must contain the inflation index.",
        )

    # Optional nominal discount curve(s): a ZCIIS zero curve bootstraps without
    # one, but a YYIIS curve (and a ZCIIS whose helpers discount past the front
    # node) needs the nominal term structure. They are resolved through
    # the same MD walker as the inflation helpers.
    nominal_curves = [
        _as_resolved_curve(raw, i)
        for i, raw in enumerate(pricing_in.get("curves") or [])
        if isinstance(raw, dict)
    ]
    inflation_curves = [_as_resolved_inflation_curve(raw, i) for i, raw in enumerate(raw_inflation)]
    swap_kind = _swap_kind_of(raw_inflation[0])
    resolved_index = _as_resolved_inflation_index(raw_indices[0])
    if not resolved_index.index_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="pricing.inflation_indices[0] is missing an ``id`` / ``index_id``.",
        )

    # 1. Resolve every quote id across the nominal + inflation curves in ONE
    #    batched MD call (invariant #8: server-side only). Both curve shapes
    #    expose ``.points``, which is all the walker reads.
    curves_for_quotes: list[Any] = [*nominal_curves, *inflation_curves]
    quote_ids = collect_quote_ids(curves_for_quotes)
    quote_map = await _resolve_quote_map(quote_ids, as_of, md_client, label="inflation quote")

    # 2. Translate nominal curves (if any) + the inflation index + curve with the
    #    shared pricing-path translators.
    missing_quotes: list[str] = []
    nominal_id = nominal_curves[0].name if nominal_curves else None
    index_id = str(raw_inflation[0].get("index_id") or resolved_index.index_id)
    discount_curve_id = str(raw_inflation[0].get("discount_curve_id") or nominal_id or "")
    try:
        term_structures = [
            translate_curve(
                curve,
                quote_map,
                default_reference_date=as_of.isoformat(),
                missing_quotes=missing_quotes,
            )
            for curve in nominal_curves
        ]
        inflation_index_spec = translate_inflation_index(resolved_index, swap_kind=swap_kind)
        inflation_curve_specs = [
            translate_inflation_curve(
                curve,
                quote_map,
                swap_kind=swap_kind,
                index_id=index_id,
                discount_curve_id=discount_curve_id,
                nominal_curve_id=discount_curve_id,
                default_reference_date=as_of.isoformat(),
                missing_quotes=missing_quotes,
            )
            for curve in inflation_curves
        ]
    except (CurveTranslationError, InflationIndexTranslationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Inflation curve translation failed: {exc.reason}",
        ) from exc
    if missing_quotes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Unresolved quote id(s): " + ", ".join(sorted(set(missing_quotes))[:10]),
        )

    # 3. Assemble the ``Pricing`` graph + call the engine.
    pricing = PricingT()
    pricing.asOfDate = as_of.isoformat()
    rates = RatesMarketDataT()
    rates.curves = term_structures
    rates.indices = _build_preview_indices(nominal_curves) if nominal_curves else []
    pricing.rates = rates
    inflation = InflationMarketDataT()
    inflation.inflationIndices = [inflation_index_spec]
    inflation.inflationCurves = inflation_curve_specs
    pricing.inflation = inflation

    request = BootstrapInflationCurvesRequestT()
    request.pricing = pricing
    request.queries = _build_inflation_queries(
        payload.queries, default_curve_id=inflation_curves[0].name, swap_kind=swap_kind
    )
    builder = flatbuffers.Builder(2048)
    # ForceDefaults: the engine requires every set field explicit on the wire;
    # zero-default enums/scalars (e.g. Frequency.Annual == 0) must not be omitted.
    builder.ForceDefaults(True)
    builder.Finish(request.Pack(builder))

    try:
        response_bytes = await engine_client.call(
            EngineRpc.BOOTSTRAP_INFLATION_CURVES, bytes(builder.Output())
        )
    except EngineClientError as exc:
        raise map_engine_client_error(exc) from exc

    return _decode_inflation_response(response_bytes)


def _build_inflation_queries(
    raw_queries: list[dict[str, Any]],
    *,
    default_curve_id: str,
    swap_kind: str,
) -> list[InflationCurveQuerySpecT]:
    """Map the portal's inflation ``queries`` to ``InflationCurveQuerySpec``s.

    ``measures`` default to the swap-kind's natural series (``ZeroRate`` for
    ZCIIS, ``YoYRate`` for YYIIS); the grid reuses the shared ``_tenor_grid``.
    """

    default_measure = "YoYRate" if swap_kind == _SWAP_KIND_YEAR_ON_YEAR else "ZeroRate"
    queries: list[InflationCurveQuerySpecT] = []
    for raw_query in raw_queries or [{}]:
        spec = InflationCurveQuerySpecT()
        spec.curveId = str(raw_query.get("curve_id") or default_curve_id)
        measures = raw_query.get("measures") or [default_measure]
        spec.measures = [
            _INFLATION_MEASURE_BY_NAME[m]
            for m in measures
            if isinstance(m, str) and m in _INFLATION_MEASURE_BY_NAME
        ] or [_INFLATION_MEASURE_BY_NAME[default_measure]]
        spec.grid = _tenor_grid(raw_query.get("grid"))
        queries.append(spec)
    return queries


def _decode_inflation_response(response_bytes: bytes) -> dict[str, Any]:
    decoded = BootstrapInflationCurvesResponseT.InitFromObj(
        BootstrapInflationCurvesResponse.GetRootAs(bytearray(response_bytes), 0)
    )
    results: list[dict[str, Any]] = []
    for result in decoded.results or []:
        error = result.error
        results.append(
            {
                "id": _decode(result.id),
                "reference_date": _decode(result.referenceDate),
                "grid_dates": [_decode(d) for d in (result.gridDates or [])],
                "pillar_dates": [_decode(d) for d in (result.pillarDates or [])],
                "series": [
                    {
                        "measure": _INFLATION_NAME_BY_MEASURE.get(int(series.measure), "ZeroRate"),
                        "values": [float(v) for v in (series.values or [])],
                    }
                    for series in (result.series or [])
                ],
                "error": (
                    {
                        "error_message": _decode(getattr(error, "errorMessage", None)),
                    }
                    if error is not None
                    else None
                ),
            }
        )
    return {"results": results}


__all__ = ["router"]
