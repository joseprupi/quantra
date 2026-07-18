"""Load + collapse the equity-option pricing request into an engine-ready shape.

Reads up to four ``app.*`` tables via the ``app_ro`` engine:

* ``app.equity_options`` — the saved trade row (``equity_option_id``
  path).
* ``app.curves`` — discount and dividend curves (one row per
  curve).
* ``app.vol_surfaces`` — the equity Black-vol surface; the
  assembler enforces ``kind = 'BlackVolSpec'`` and raises
  :class:`EquityOptionVolSurfaceWrongKindError` when the saved row
  is a swaption / optionlet surface.
* ``app.snapshots`` — optional MD pin.

Writes nothing. Every read carries ``owner_uid = :owner_uid`` +
``deleted_at IS NULL`` so cross-tenant + soft-deleted rows are
invisible (north-star invariant #5).

The assembler's three jobs:

1. **Branch collapsing.** Either ``equity_option_id`` (by
   reference) or ``equity_option`` (inline) populates
   :class:`EquityOptionTrade`. Either ``curves`` /
   ``vol_surface`` / ``spot`` from the request, or the saved
   option's matching pricing-block refs, fill the resolved shapes.
   Cross-entity refs inside the saved option's JSONB are soft
   .
2. **Three-bundle resolution.** Discount curve (rates),
   dividend-yield curve (rates, second role), equity vol surface
   (BlackVolSpec), spot quote (inline value or canonical id).
   Each bundle stage's failure surfaces as a distinct error code
   .
3. **Snapshot pinning.** When ``snapshot_id`` is set, the
   :class:`ResolvedSnapshot` returned alongside the other outputs
   gives ``md_resolution`` a precomputed ``canonical_id → value``
   map. Quotes that aren't pinned fall through to the live MD
   service.

The output is everything the MD-resolution walker needs to do its
job; the assembler itself never touches the MD client or the
engine.

This module deliberately duplicates structure from
``pricing/swaption/assembler.py`` — a shared ``pricing/_resolved/``
module factor-out is a candidate future refactor.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_orchestrator.pricing.equity_options.errors import (
    EquityOptionCurveResolutionFailedError,
    EquityOptionDiscountCurveNotFoundError,
    EquityOptionInvalidRequestError,
    EquityOptionNotFoundError,
    EquityOptionSnapshotNotFoundError,
    EquityOptionSpotResolutionFailedError,
    EquityOptionSurfaceResolutionFailedError,
    EquityOptionVolSurfaceNotFoundError,
    EquityOptionVolSurfaceWrongKindError,
)
from quantra_orchestrator.pricing.equity_options.models import (
    CurveRef,
    EquityOptionPriceRequest,
    EquityOptionTrade,
    ResolvedCurve,
    ResolvedSpotQuote,
    ResolvedVolSurface,
    VolSurfaceRef,
)

_PRICING_KEY = "pricing"
_CURVES_KEYS: tuple[str, ...] = ("curves",)
_VOL_SURFACES_KEYS: tuple[str, ...] = ("vol_surfaces", "volSurfaces")
_VOL_SURFACE_ID_KEYS: tuple[str, ...] = ("vol_surface_id", "volSurfaceId")
_SPOT_KEY = "spot"
_SPOT_QUOTE_ID_KEYS: tuple[str, ...] = ("canonical_id", "quote_id", "spot_quote_id")
_SPOT_VALUE_KEYS: tuple[str, ...] = ("value", "spot")
_CURVE_REF_ID_KEYS: tuple[str, ...] = ("curve_id", "curveId", "id")
_VOL_SURFACE_REF_ID_KEYS: tuple[str, ...] = ("vol_surface_id", "volSurfaceId", "id")

_BLACK_VOL_KIND: str = "BlackVolSpec"


@dataclass(frozen=True, slots=True)
class ResolvedSnapshot:
    """A loaded ``app.snapshots`` row, projected for MD resolution.

    Same shape (and same projection logic) as the swaption-side
    ``ResolvedSnapshot``. The MD-resolution walker consults
    ``pins`` first and only fires live MD calls for canonical IDs
    the pin doesn't cover.
    """

    id: UUID
    name: str
    pins: dict[str, dict[str, Any]] = field(default_factory=dict)
    # soft pin to ``md.snapshots.version_etag``. Read
    # from the orchestrator-owned ``app.snapshots.content.md_version_etag``
    # JSONB key; ``None`` when the user-side snapshot was not pinned to a
    # vendor etag (today's data). When set, threaded into
    # ``MdClient.resolve_quotes(snapshot_version=...)`` so the cache
    # invalidates as soon as the trigger advances the etag.
    version_etag: str | None = None


@dataclass(frozen=True, slots=True)
class AssemblerOutput:
    """All the pieces the route handler needs to call the MD walker + engine.

    ``trade`` packs the option body. ``discount_curve`` /
    ``dividend_curve`` are the resolved (no quote substitution
    yet) curves; ``vol_surface`` is the resolved BlackVolSpec
    surface; ``spot`` is the resolved underlier spot reference.
    ``discount_curve_id`` / ``dividend_curve_id`` /
    ``equity_surface_id`` carry the logical bundle ids, echoed in
    the assembled request. ``snapshot`` is the optional pin loaded
    from ``app.snapshots``.
    """

    trade: EquityOptionTrade
    discount_curve: ResolvedCurve
    discount_curve_id: UUID | None
    dividend_curve: ResolvedCurve
    dividend_curve_id: UUID | None
    vol_surface: ResolvedVolSurface
    equity_surface_id: UUID | None
    spot: ResolvedSpotQuote
    snapshot: ResolvedSnapshot | None


async def assemble(
    request: EquityOptionPriceRequest,
    *,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> AssemblerOutput:
    """Resolve the request into the inputs MD resolution + engine call need.

    Steps:

    1. Materialise the trade (load ``app.equity_options`` or copy
       inline).
    2. Determine the source of curves (request override → saved
       option's pricing.curves) and split into discount /
       dividend by ``role`` or by position.
    3. Materialise each curve (load ``app.curves`` or copy
       inline).
    4. Materialise the vol surface (request override → saved
       option's pricing.vol_surface_id / pricing.vol_surfaces[0]),
       enforcing ``kind = 'BlackVolSpec'``.
    5. Materialise the spot reference (request override → saved
       option's pricing.spot).
    6. Optionally load the pinned ``app.snapshots`` row.

    Every read goes through ``app_ro``. Soft references
    that resolve to ``None`` raise the appropriate typed
    HTTPException so the failure surfaces as a 4xx with a stable
    code rather than a 500.
    """

    trade, option_request = await _load_trade(
        request=request, owner_uid=owner_uid, ro_engine=ro_engine
    )

    discount, discount_id, dividend, dividend_id = await _resolve_curves(
        request=request,
        option_request=option_request,
        owner_uid=owner_uid,
        ro_engine=ro_engine,
    )

    vol_surface, equity_surface_id = await _resolve_vol_surface(
        request=request,
        option_request=option_request,
        owner_uid=owner_uid,
        ro_engine=ro_engine,
    )

    spot = _resolve_spot(request=request, option_request=option_request)

    snapshot = await _load_snapshot(
        snapshot_id=request.snapshot_id,
        owner_uid=owner_uid,
        ro_engine=ro_engine,
    )

    return AssemblerOutput(
        trade=trade,
        discount_curve=discount,
        discount_curve_id=discount_id,
        dividend_curve=dividend,
        dividend_curve_id=dividend_id,
        vol_surface=vol_surface,
        equity_surface_id=equity_surface_id,
        spot=spot,
        snapshot=snapshot,
    )


# ---------------------------------------------------------------------------
# Trade
# ---------------------------------------------------------------------------


async def _load_trade(
    *,
    request: EquityOptionPriceRequest,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> tuple[EquityOptionTrade, dict[str, Any]]:
    """Return ``(trade, raw_request_body)`` for the requested option."""

    if request.equity_option is not None:
        return (
            EquityOptionTrade(
                equity_option_id=None,
                name=None,
                equity_option=dict(request.equity_option),
            ),
            dict(request.equity_option),
        )

    assert request.equity_option_id is not None  # nosec B101 - guaranteed by pydantic
    row = await _fetch_equity_option(
        owner_uid=owner_uid,
        equity_option_id=request.equity_option_id,
        ro_engine=ro_engine,
    )
    if row is None:
        raise EquityOptionNotFoundError(equity_option_id=request.equity_option_id)
    raw_request = row["request"] if isinstance(row.get("request"), dict) else {}
    trade = EquityOptionTrade(
        equity_option_id=request.equity_option_id,
        name=row.get("name"),
        equity_option=raw_request,
    )
    return trade, raw_request


async def _fetch_equity_option(
    *,
    owner_uid: str,
    equity_option_id: UUID,
    ro_engine: AsyncEngine,
) -> dict[str, Any] | None:
    """Load one live ``app.equity_options`` row by ``(owner_uid, id)``."""

    sql = text(
        "SELECT id::text AS id, name, request "
        "FROM app.equity_options "
        "WHERE id = :equity_option_id "
        "  AND owner_uid = :owner_uid "
        "  AND deleted_at IS NULL"
    )
    async with ro_engine.connect() as conn:
        result = await conn.execute(
            sql,
            {
                "equity_option_id": str(equity_option_id),
                "owner_uid": owner_uid,
            },
        )
        row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Curves (discount + dividend)
# ---------------------------------------------------------------------------


async def _resolve_curves(
    *,
    request: EquityOptionPriceRequest,
    option_request: dict[str, Any],
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> tuple[ResolvedCurve, UUID | None, ResolvedCurve, UUID | None]:
    """Walk the request → saved-option chain to produce two ResolvedCurves.

    Source priority for curves:

    1. ``request.curves`` (explicit override or inline-mode
       payload).
    2. ``option.request.pricing.curves`` (per-curve refs or inline
       definitions baked into the saved option).

    The first resolved curve is treated as the discount curve;
    the second (if present) as the dividend curve. Curves may
    pin their role explicitly via the ``role`` field on the
    request body. If the dividend curve isn't supplied, the
    assembler synthesises a flat-zero placeholder so downstream
    code (engine_io) has a consistent two-curve shape — the
    engine's :class:`BlackVolSpec` falls back to its
    ``flat_dividend_rate`` when the placeholder is selected by
    id.

    Returns ``(discount, discount_id, dividend, dividend_id)``.
    """

    if request.curves is not None and len(request.curves) > 0:
        resolved = await _materialise_curve_refs(
            refs=request.curves, owner_uid=owner_uid, ro_engine=ro_engine
        )
    else:
        pricing = option_request.get(_PRICING_KEY)
        if not isinstance(pricing, dict):
            raise EquityOptionCurveResolutionFailedError(
                reason=(
                    "Saved equity option is missing the ``pricing`` "
                    "block; cannot determine which curves to use. "
                    "Re-save the option with a ``pricing.curves`` "
                    "list or pass an explicit ``curves`` override "
                    "in the pricing request."
                ),
                details=[{"role": "discount"}],
            )
        inline_curves_raw = pricing.get(_CURVES_KEYS[0])
        if not isinstance(inline_curves_raw, list) or len(inline_curves_raw) == 0:
            raise EquityOptionCurveResolutionFailedError(
                reason=(
                    "Saved equity option has no ``pricing.curves`` "
                    "list; the orchestrator cannot infer which "
                    "discount curve to bootstrap. Re-save the "
                    "option or pass an explicit ``curves`` "
                    "override in the pricing request."
                ),
                details=[{"role": "discount"}],
            )
        refs = _refs_from_inline_curves(inline_curves_raw)
        resolved = await _materialise_curve_refs(
            refs=refs, owner_uid=owner_uid, ro_engine=ro_engine
        )

    discount, dividend = _split_into_discount_and_dividend(resolved)
    return (discount, discount.id, dividend, dividend.id)


def _split_into_discount_and_dividend(
    resolved: list[ResolvedCurve],
) -> tuple[ResolvedCurve, ResolvedCurve]:
    """Bucket the resolved curves into ``(discount, dividend)``.

    Honors an explicit ``role`` if pinned on at least one curve;
    otherwise treats the first as discount and the second (when
    present) as dividend. When no dividend curve is supplied a
    flat-zero placeholder is synthesised; the engine itself falls
    back to ``flat_dividend_rate`` for a placeholder body.
    """

    by_role: dict[str, ResolvedCurve] = {}
    unrolled: list[ResolvedCurve] = []
    for curve in resolved:
        if curve.role.lower() in {"discount", "dividend"}:
            by_role[curve.role.lower()] = curve
        else:
            unrolled.append(curve)

    if "discount" in by_role:
        discount = by_role["discount"]
    elif unrolled:
        discount = unrolled.pop(0).model_copy(update={"role": "discount"})
    else:
        msg = "Equity-options pricing requires at least one curve; the resolved list was empty."
        raise EquityOptionCurveResolutionFailedError(reason=msg, details=[{"role": "discount"}])

    if "dividend" in by_role:
        dividend = by_role["dividend"]
    elif unrolled:
        dividend = unrolled.pop(0).model_copy(update={"role": "dividend"})
    else:
        dividend = ResolvedCurve(
            id=None,
            name="dividend-flat-zero",
            role="dividend",
            currency=discount.currency,
            day_counter=discount.day_counter,
            helper_kind=discount.helper_kind,
            reference_date=discount.reference_date,
            points=[],
            body={"placeholder": "flat_zero"},
        )

    return discount, dividend


def _refs_from_inline_curves(raw: list[Any]) -> list[CurveRef]:
    """Coerce a saved ``pricing.curves`` list into :class:`CurveRef` instances."""

    refs: list[CurveRef] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise EquityOptionCurveResolutionFailedError(
                reason=(f"Saved equity option ``pricing.curves[{index}]`` is not an object."),
                details=[{"index": index}],
            )
        candidate_id = _extract_uuid(entry, _CURVE_REF_ID_KEYS)
        if candidate_id is not None and not _has_inline_curve_payload(entry):
            role_raw = entry.get("role")
            role = role_raw if isinstance(role_raw, str) and role_raw else None
            refs.append(CurveRef(id=candidate_id, role=role))
            continue
        try:
            refs.append(CurveRef.model_validate(entry))
        except ValueError as exc:
            raise EquityOptionCurveResolutionFailedError(
                reason=(
                    f"Saved equity option ``pricing.curves[{index}]`` "
                    "is not a valid CurveRef shape."
                ),
                details=[{"index": index, "error": str(exc)}],
            ) from exc
    return refs


def _has_inline_curve_payload(entry: dict[str, Any]) -> bool:
    keys = set(entry.keys())
    return bool(keys - {"id", "curve_id", "curveId", "role"})


async def _materialise_curve_refs(
    *,
    refs: Iterable[CurveRef],
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> list[ResolvedCurve]:
    """Collapse each :class:`CurveRef` into a :class:`ResolvedCurve`."""

    resolved: list[ResolvedCurve] = []
    for index, ref in enumerate(refs):
        inferred_role = ref.role or ("discount" if index == 0 else "dividend")
        if ref.id is not None and ref.points is None:
            row = await _fetch_curve(curve_id=ref.id, owner_uid=owner_uid, ro_engine=ro_engine)
            if row is None:
                raise EquityOptionDiscountCurveNotFoundError(curve_id=ref.id, role=inferred_role)
            resolved.append(_resolved_curve_from_row(row, role=inferred_role))
            continue
        if ref.name is None or ref.points is None:
            raise EquityOptionCurveResolutionFailedError(
                reason=(f"Inline curve at index {index} is missing ``name`` or ``points``."),
                details=[{"index": index, "role": inferred_role}],
            )
        resolved.append(
            ResolvedCurve(
                id=ref.id,
                name=ref.name,
                role=inferred_role,
                currency=ref.currency,
                day_counter=ref.day_counter,
                helper_kind=ref.helper_kind,
                reference_date=ref.reference_date,
                points=list(ref.points),
                body=dict(ref.body) if ref.body is not None else {},
            )
        )
    return resolved


async def _fetch_curve(
    *,
    curve_id: UUID,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> dict[str, Any] | None:
    sql = text(
        "SELECT id::text AS id, name, currency, day_counter, helper_kind, "
        "       reference_date, points, body "
        "FROM app.curves "
        "WHERE id = :curve_id "
        "  AND owner_uid = :owner_uid "
        "  AND deleted_at IS NULL"
    )
    async with ro_engine.connect() as conn:
        result = await conn.execute(sql, {"curve_id": str(curve_id), "owner_uid": owner_uid})
        row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


def _resolved_curve_from_row(row: dict[str, Any], *, role: str) -> ResolvedCurve:
    raw_points = row.get("points")
    points: list[dict[str, Any]] = (
        [pt for pt in raw_points if isinstance(pt, dict)] if isinstance(raw_points, list) else []
    )
    raw_body = row.get("body")
    body: dict[str, Any] = raw_body if isinstance(raw_body, dict) else {}
    return ResolvedCurve(
        id=UUID(str(row["id"])),
        name=str(row["name"]),
        role=role,
        currency=row.get("currency"),
        day_counter=row.get("day_counter"),
        helper_kind=row.get("helper_kind"),
        reference_date=row.get("reference_date"),
        points=points,
        body=body,
    )


# ---------------------------------------------------------------------------
# Vol surface (BlackVolSpec only)
# ---------------------------------------------------------------------------


async def _resolve_vol_surface(
    *,
    request: EquityOptionPriceRequest,
    option_request: dict[str, Any],
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> tuple[ResolvedVolSurface, UUID | None]:
    """Walk the request → saved-option chain to produce a ResolvedVolSurface."""

    if request.vol_surface is not None:
        return await _materialise_vol_surface_ref(
            ref=request.vol_surface,
            owner_uid=owner_uid,
            ro_engine=ro_engine,
        )

    pricing = option_request.get(_PRICING_KEY)
    if not isinstance(pricing, dict):
        raise EquityOptionSurfaceResolutionFailedError(
            reason=(
                "Saved equity option is missing the ``pricing`` "
                "block; cannot determine which vol surface to use."
            ),
        )

    vol_surface_id = _extract_uuid(pricing, _VOL_SURFACE_ID_KEYS)
    if vol_surface_id is not None:
        return await _materialise_vol_surface_ref(
            ref=VolSurfaceRef(id=vol_surface_id),
            owner_uid=owner_uid,
            ro_engine=ro_engine,
        )

    inline_list = _first_present(pricing, _VOL_SURFACES_KEYS)
    if not isinstance(inline_list, list) or len(inline_list) == 0:
        raise EquityOptionSurfaceResolutionFailedError(
            reason=(
                "Saved equity option has no ``pricing.vol_surface_id`` "
                "and no ``pricing.vol_surfaces`` list; cannot infer "
                "the vol surface."
            ),
        )
    first = inline_list[0]
    if not isinstance(first, dict):
        raise EquityOptionSurfaceResolutionFailedError(
            reason=("Saved equity option ``pricing.vol_surfaces[0]`` is not an object."),
            details=[{"entry": first}],
        )
    saved_id = _extract_uuid(first, _VOL_SURFACE_REF_ID_KEYS)
    if saved_id is not None and not _has_inline_surface_payload(first):
        return await _materialise_vol_surface_ref(
            ref=VolSurfaceRef(id=saved_id),
            owner_uid=owner_uid,
            ro_engine=ro_engine,
        )
    return await _materialise_vol_surface_ref(
        ref=_inline_vol_surface_ref(first),
        owner_uid=owner_uid,
        ro_engine=ro_engine,
    )


def _has_inline_surface_payload(entry: dict[str, Any]) -> bool:
    keys = set(entry.keys())
    return bool(keys - {"id", "vol_surface_id", "volSurfaceId", "name"})


def _inline_vol_surface_ref(entry: dict[str, Any]) -> VolSurfaceRef:
    """Coerce a saved ``pricing.vol_surfaces[0]`` blob into a typed ref."""

    if "payload" in entry and isinstance(entry["payload"], dict):
        try:
            return VolSurfaceRef.model_validate(entry)
        except ValueError as exc:
            raise EquityOptionSurfaceResolutionFailedError(
                reason=(
                    "Saved equity option ``pricing.vol_surfaces[0]`` "
                    "is not a valid VolSurfaceRef shape."
                ),
                details=[{"error": str(exc)}],
            ) from exc

    kind_raw = entry.get("kind") or entry.get("payload_type")
    if not isinstance(kind_raw, str) or not kind_raw:
        raise EquityOptionSurfaceResolutionFailedError(
            reason=(
                "Saved equity option ``pricing.vol_surfaces[0]`` is "
                "missing ``kind``; cannot determine the engine "
                "bootstrap shape."
            ),
        )
    name_raw = entry.get("name")
    payload = {
        key: value
        for key, value in entry.items()
        if key
        not in {
            "id",
            "vol_surface_id",
            "volSurfaceId",
            "name",
            "kind",
            "payload_type",
        }
    }
    try:
        return VolSurfaceRef(
            id=None,
            name=name_raw if isinstance(name_raw, str) else None,
            kind=kind_raw,
            payload=payload,
        )
    except ValueError as exc:
        raise EquityOptionSurfaceResolutionFailedError(
            reason=(
                "Saved equity option ``pricing.vol_surfaces[0]`` "
                "cannot be coerced into a VolSurfaceRef."
            ),
            details=[{"error": str(exc)}],
        ) from exc


async def _materialise_vol_surface_ref(
    *,
    ref: VolSurfaceRef,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> tuple[ResolvedVolSurface, UUID | None]:
    """Collapse one :class:`VolSurfaceRef` into a :class:`ResolvedVolSurface`."""

    if ref.id is not None and ref.payload is None:
        row = await _fetch_vol_surface(
            vol_surface_id=ref.id, owner_uid=owner_uid, ro_engine=ro_engine
        )
        if row is None:
            raise EquityOptionVolSurfaceNotFoundError(vol_surface_id=ref.id)
        actual_kind = str(row["kind"])
        if actual_kind != _BLACK_VOL_KIND:
            raise EquityOptionVolSurfaceWrongKindError(
                vol_surface_id=ref.id, actual_kind=actual_kind
            )
        return (_resolved_vol_surface_from_row(row), ref.id)

    if ref.kind is None or ref.payload is None:
        raise EquityOptionSurfaceResolutionFailedError(
            reason=("Inline equity vol surface is missing ``kind`` or ``payload``."),
        )
    if ref.kind != _BLACK_VOL_KIND:
        raise EquityOptionInvalidRequestError(
            reason=(
                f"Inline vol surface kind={ref.kind!r}; equity options require kind='BlackVolSpec'."
            ),
            details=[{"actual_kind": ref.kind, "expected_kind": _BLACK_VOL_KIND}],
        )
    return (
        ResolvedVolSurface(
            id=ref.id,
            name=ref.name or "inline-equity-vol-surface",
            kind=ref.kind,
            payload=dict(ref.payload),
        ),
        ref.id,
    )


async def _fetch_vol_surface(
    *,
    vol_surface_id: UUID,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> dict[str, Any] | None:
    sql = text(
        "SELECT id::text AS id, name, kind, payload "
        "FROM app.vol_surfaces "
        "WHERE id = :vol_surface_id "
        "  AND owner_uid = :owner_uid "
        "  AND deleted_at IS NULL"
    )
    async with ro_engine.connect() as conn:
        result = await conn.execute(
            sql,
            {"vol_surface_id": str(vol_surface_id), "owner_uid": owner_uid},
        )
        row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


def _resolved_vol_surface_from_row(row: dict[str, Any]) -> ResolvedVolSurface:
    raw_payload = row.get("payload")
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
    return ResolvedVolSurface(
        id=UUID(str(row["id"])),
        name=str(row["name"]),
        kind=str(row["kind"]),
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Spot quote
# ---------------------------------------------------------------------------


def _resolve_spot(
    *,
    request: EquityOptionPriceRequest,
    option_request: dict[str, Any],
) -> ResolvedSpotQuote:
    """Walk the request → saved-option chain to produce a ResolvedSpotQuote.

    The spot is the only equity-options market-data leaf that is
    not loaded from a vendor-style table; it's either a literal
    inline value or a canonical id passed straight through to
    :mod:`equity_options.md_resolution`.
    """

    if request.spot is not None:
        return ResolvedSpotQuote(
            canonical_id=request.spot.canonical_id,
            value=request.spot.value,
        )

    pricing = option_request.get(_PRICING_KEY)
    spot_raw: Any = None
    if isinstance(pricing, dict):
        spot_raw = pricing.get(_SPOT_KEY)
    if spot_raw is None:
        spot_raw = option_request.get(_SPOT_KEY)
    if isinstance(spot_raw, (int, float)):
        return ResolvedSpotQuote(canonical_id=None, value=float(spot_raw))
    if isinstance(spot_raw, dict):
        canonical_id = _first_str(spot_raw, _SPOT_QUOTE_ID_KEYS)
        value_raw = _first_present(spot_raw, _SPOT_VALUE_KEYS)
        value = float(value_raw) if isinstance(value_raw, (int, float)) else None
        if canonical_id is None and value is None:
            raise EquityOptionSpotResolutionFailedError(
                reason=(
                    "Saved equity option ``pricing.spot`` carries "
                    "neither a canonical id nor an inline value."
                ),
            )
        try:
            return ResolvedSpotQuote(canonical_id=canonical_id, value=value)
        except ValueError as exc:
            raise EquityOptionSpotResolutionFailedError(
                reason=(
                    "Saved equity option ``pricing.spot`` cannot be coerced into a SpotQuoteRef."
                ),
                details=[{"error": str(exc)}],
            ) from exc

    raise EquityOptionSpotResolutionFailedError(
        reason=(
            "Saved equity option has no ``pricing.spot`` or "
            "top-level ``spot`` block; cannot determine the "
            "underlier spot."
        ),
    )


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


async def _load_snapshot(
    *,
    snapshot_id: UUID | None,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> ResolvedSnapshot | None:
    if snapshot_id is None:
        return None
    sql = text(
        "SELECT id::text AS id, name, content "
        "FROM app.snapshots "
        "WHERE id = :snapshot_id "
        "  AND owner_uid = :owner_uid "
        "  AND deleted_at IS NULL"
    )
    async with ro_engine.connect() as conn:
        result = await conn.execute(sql, {"snapshot_id": str(snapshot_id), "owner_uid": owner_uid})
        row = result.mappings().one_or_none()
    if row is None:
        raise EquityOptionSnapshotNotFoundError(snapshot_id=snapshot_id)
    raw_content = row.get("content")
    content: dict[str, Any] = raw_content if isinstance(raw_content, dict) else {}
    pins = _pins_from_content(content)
    version_etag = _version_etag_from_content(content)
    return ResolvedSnapshot(
        id=UUID(str(row["id"])),
        name=str(row["name"]),
        pins=pins,
        version_etag=version_etag,
    )


# Reserved top-level keys in ``app.snapshots.content`` that are NOT
# quote pins. ``_pins_from_content`` must skip these or they would be
# misread as canonical IDs (risk note). Add to this tuple when
# new reserved keys are introduced.
_RESERVED_CONTENT_KEYS: tuple[str, ...] = ("quotes", "md_version_etag")


def _version_etag_from_content(content: dict[str, Any]) -> str | None:
    """Read the optional ``md_version_etag`` soft pin from snapshot content."""

    value = content.get("md_version_etag")
    if isinstance(value, str) and value:
        return value
    return None


def _pins_from_content(content: dict[str, Any]) -> dict[str, dict[str, Any]]:
    pins: dict[str, dict[str, Any]] = {}
    list_entries = content.get("quotes")
    if isinstance(list_entries, list):
        for entry in list_entries:
            if not isinstance(entry, dict):
                continue
            cid = entry.get("canonical_id")
            value = entry.get("value")
            if isinstance(cid, str) and isinstance(value, (int, float)):
                pins[cid] = {
                    "value": float(value),
                    "source": entry.get("source"),
                }
        return pins
    for key, raw in content.items():
        if not isinstance(key, str) or key in _RESERVED_CONTENT_KEYS:
            continue
        if isinstance(raw, (int, float)):
            pins[key] = {"value": float(raw), "source": None}
        elif isinstance(raw, dict):
            value = raw.get("value")
            if isinstance(value, (int, float)):
                pins[key] = {
                    "value": float(value),
                    "source": raw.get("source"),
                }
    return pins


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_uuid(payload: dict[str, Any], keys: tuple[str, ...]) -> UUID | None:
    for key in keys:
        candidate = payload.get(key)
        if isinstance(candidate, str) and candidate:
            try:
                return UUID(candidate)
            except ValueError:
                continue
    return None


def _first_str(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _first_present(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:  # noqa: ANN401 -- dynamic
    for key in keys:
        if key in payload:
            return payload[key]
    return None


__all__ = [
    "AssemblerOutput",
    "ResolvedSnapshot",
    "assemble",
]
