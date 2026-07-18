"""Load + collapse the swaption pricing request into an engine-ready shape.

Reads five ``app.*`` tables via the ``app_ro`` engine:

* ``app.swaptions`` — the saved trade row (``swaption_id`` path).
* ``app.curve_sets`` + ``app.curves`` — discount + projection curves.
* ``app.vol_surfaces`` — the swaption vol surface (saved or inline).
* ``app.swaption_models`` — short-rate / SABR model config (saved or inline).
* ``app.snapshots`` — optional MD pin.

Writes nothing. Every read carries ``owner_uid =:owner_uid`` +
``deleted_at IS NULL`` so cross-tenant + soft-deleted rows are
invisible (north-star invariant #5).

The assembler's three jobs:

1. **Branch collapsing.** Either ``swaption_id`` (by reference) or
   ``swaption`` (inline) populates :class:`SwaptionTrade`. Either
   ``curves[]`` / ``vol_surface`` / ``swaption_model`` from the
   request, or the saved swaption's matching pricing-block refs,
   fill the resolved shapes. Cross-entity refs inside the saved
   swaption's JSONB are soft: missing referents surface as
   ``swaption_*_not_found`` / ``swaption_*_resolution_failed``
   rather than silently resolving to null.

2. **Five-table fan-out.** Unlike IR swaps (three tables) the
   swaption endpoint fans out to five ``app.*`` tables. Each load
   is an independent SQL site to keep the audit-friendly "one SQL
   per concern" structure intact.

3. **Snapshot pinning.** When ``snapshot_id`` is set, the
   :class:`ResolvedSnapshot` returned alongside the other outputs
   gives ``md_resolution`` a precomputed ``canonical_id → value``
   map. Quotes that aren't pinned fall through to the live MD
   service.

The output is everything the MD-resolution walker needs to do its
job; the assembler itself never touches the MD client or the engine.

This module deliberately duplicates structure from
``pricing/swap_ir/assembler.py`` — factoring a shared walker before
the per-product shapes stop moving would couple the products
prematurely. A shared module is a candidate future refactor.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_orchestrator.pricing._translator import CurveRole, resolved_curve_id
from quantra_orchestrator.pricing.swaption.errors import (
    SwaptionCurveResolutionFailedError,
    SwaptionCurveSetNotFoundError,
    SwaptionIndexNotFoundError,
    SwaptionIndexResolutionFailedError,
    SwaptionInvalidRequestError,
    SwaptionModelNotFoundError,
    SwaptionNotFoundError,
    SwaptionSnapshotNotFoundError,
    SwaptionSurfaceResolutionFailedError,
    SwaptionVolSurfaceNotFoundError,
)
from quantra_orchestrator.pricing.swaption.models import (
    CurveRef,
    IndexRef,
    ResolvedCurve,
    ResolvedIndex,
    ResolvedSwaptionModel,
    ResolvedVolSurface,
    SwaptionModelRef,
    SwaptionPriceRequest,
    SwaptionTrade,
    VolSurfaceRef,
)

_SWAP_PRICING_KEY = "pricing"
_SWAP_CURVE_SET_KEYS: tuple[str, ...] = ("curve_set_id", "curveSetId")
_SWAP_CURVES_KEYS: tuple[str, ...] = ("curves",)
_SWAP_VOL_SURFACES_KEYS: tuple[str, ...] = ("vol_surfaces", "volSurfaces")
_SWAP_VOL_SURFACE_ID_KEYS: tuple[str, ...] = ("vol_surface_id", "volSurfaceId")
_SWAP_MODELS_KEYS: tuple[str, ...] = ("models", "swaption_models", "swaptionModels")
_SWAP_MODEL_ID_KEYS: tuple[str, ...] = ("swaption_model_id", "swaptionModelId", "model_id")
_CURVE_SET_BODY_REFS_KEYS: tuple[str, ...] = ("curve_refs", "curveRefs")
_CURVE_REF_ID_KEYS: tuple[str, ...] = ("curve_id", "curveId", "id")
_VOL_SURFACE_REF_ID_KEYS: tuple[str, ...] = ("vol_surface_id", "volSurfaceId", "id")
_MODEL_REF_ID_KEYS: tuple[str, ...] = ("swaption_model_id", "model_id", "swaptionModelId", "id")


@dataclass(frozen=True, slots=True)
class ResolvedSnapshot:
    """A loaded ``app.snapshots`` row, projected for MD resolution.

    Same shape (and same projection logic) as the swap_ir-side
    ``ResolvedSnapshot``; the MD-resolution walker consults
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

    ``trade`` packs the swaption body. ``curves`` is the resolved
    (no quote substitution yet) curve list. ``vol_surface`` is the
    resolved surface; ``swaption_model`` is the resolved model
    config. ``curve_set_id`` / ``vol_surface_id`` /
    ``swaption_model_id`` carry the logical bundle ids for the
    concurrency seam's ``shared_inputs``. ``snapshot`` is the
    optional pin loaded from ``app.snapshots``.
    """

    trade: SwaptionTrade
    curves: list[ResolvedCurve]
    curve_set_id: UUID | None
    vol_surface: ResolvedVolSurface
    vol_surface_id: UUID | None
    swaption_model: ResolvedSwaptionModel
    swaption_model_id: UUID | None
    snapshot: ResolvedSnapshot | None
    # the resolved underlying-swap float index (``None`` when the
    # request omitted it, which preserves exactly today's default-forwarding
    # + Semiannual behaviour). When present it flows into
    # ``ResolvedMarketData.index`` so the underlying float leg's projection
    # index and coupon frequency follow its tenor.
    index: ResolvedIndex | None = None

    def curve_roles(self) -> dict[CurveRole, str]:
        """Tag the resolved curves with their engine reference role.

        swaption's curve list is discount-first; the forwarding role reuses the
        second curve when present, else the discount curve (the EUR single-curve
        case). This is the role-assignment site the translator / engine_io
        consume by role, replacing the older positional convention.
        """

        if not self.curves:
            return {}
        forwarding = self.curves[1] if len(self.curves) > 1 else self.curves[0]
        return {
            CurveRole.DISCOUNT: resolved_curve_id(self.curves[0]),
            CurveRole.FORWARDING: resolved_curve_id(forwarding),
        }


async def assemble(
    request: SwaptionPriceRequest,
    *,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> AssemblerOutput:
    """Resolve the request into the inputs MD resolution + engine call need.

    Steps:

    1. Materialise the trade (load ``app.swaptions`` or copy inline).
    2. Determine the source of curves (request override → saved
       swaption's pinned curve set → saved swaption's pricing.curves).
    3. Materialise each curve (load ``app.curves`` or copy inline).
    4. Materialise the vol surface (request override → saved
       swaption's pricing.vol_surface_id / pricing.vol_surfaces[0]).
    5. Materialise the swaption model (request override → saved
       swaption's pricing.swaption_model_id / pricing.models[0]).
    6. Optionally load the pinned ``app.snapshots`` row.

    Every read goes through ``app_ro``. Soft references
    that resolve to ``None`` raise the appropriate
    ``swaption_*_not_found`` / ``swaption_*_resolution_failed`` so
    the failure surfaces as a 4xx with a stable code rather
    than a 500.
    """

    trade, swaption_request = await _load_trade(
        request=request, owner_uid=owner_uid, ro_engine=ro_engine
    )

    curves, curve_set_id = await _resolve_curves(
        request=request,
        swaption_request=swaption_request,
        owner_uid=owner_uid,
        ro_engine=ro_engine,
    )

    vol_surface, vol_surface_id = await _resolve_vol_surface(
        request=request,
        swaption_request=swaption_request,
        owner_uid=owner_uid,
        ro_engine=ro_engine,
    )

    swaption_model, swaption_model_id = await _resolve_swaption_model(
        request=request,
        swaption_request=swaption_request,
        owner_uid=owner_uid,
        ro_engine=ro_engine,
    )

    snapshot = await _load_snapshot(
        snapshot_id=request.snapshot_id,
        owner_uid=owner_uid,
        ro_engine=ro_engine,
    )

    index = await _resolve_index(request=request, owner_uid=owner_uid, ro_engine=ro_engine)

    return AssemblerOutput(
        trade=trade,
        curves=curves,
        curve_set_id=curve_set_id,
        vol_surface=vol_surface,
        vol_surface_id=vol_surface_id,
        swaption_model=swaption_model,
        swaption_model_id=swaption_model_id,
        snapshot=snapshot,
        index=index,
    )


# ---------------------------------------------------------------------------
# Trade
# ---------------------------------------------------------------------------


async def _load_trade(
    *,
    request: SwaptionPriceRequest,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> tuple[SwaptionTrade, dict[str, Any]]:
    """Return ``(trade, raw_request_body)`` for the requested swaption.

    The raw request body is returned alongside the typed trade so
    the curve / vol-surface / model resolution steps can inspect
    the saved swaption's pricing block without re-loading.
    """

    if request.swaption is not None:
        return (
            SwaptionTrade(swaption_id=None, name=None, swaption=dict(request.swaption)),
            dict(request.swaption),
        )

    assert request.swaption_id is not None  # nosec B101 - guaranteed by pydantic
    row = await _fetch_swaption(
        owner_uid=owner_uid,
        swaption_id=request.swaption_id,
        ro_engine=ro_engine,
    )
    if row is None:
        raise SwaptionNotFoundError(swaption_id=request.swaption_id)
    raw_request = row["request"] if isinstance(row.get("request"), dict) else {}
    trade = SwaptionTrade(
        swaption_id=request.swaption_id,
        name=row.get("name"),
        swaption=raw_request,
    )
    return trade, raw_request


async def _fetch_swaption(
    *,
    owner_uid: str,
    swaption_id: UUID,
    ro_engine: AsyncEngine,
) -> dict[str, Any] | None:
    """Load one live ``app.swaptions`` row by ``(owner_uid, id)``."""

    sql = text(
        "SELECT id::text AS id, name, request "
        "FROM app.swaptions "
        "WHERE id = :swaption_id "
        "  AND owner_uid = :owner_uid "
        "  AND deleted_at IS NULL"
    )
    async with ro_engine.connect() as conn:
        result = await conn.execute(sql, {"swaption_id": str(swaption_id), "owner_uid": owner_uid})
        row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Curves (same source-priority chain as swap_ir)
# ---------------------------------------------------------------------------


async def _resolve_curves(
    *,
    request: SwaptionPriceRequest,
    swaption_request: dict[str, Any],
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> tuple[list[ResolvedCurve], UUID | None]:
    """Walk the request → saved-swaption → curve-set chain to produce ResolvedCurves.

    Source priority:

    1. ``request.curves`` (explicit override or inline-mode payload).
    2. ``swaption.request.pricing.curve_set_id`` → ``app.curve_sets`` row.
    3. ``swaption.request.pricing.curves`` (per-curve refs or inline
       definitions baked into the saved swaption).

    Returns ``(curves, curve_set_id)``.
    """

    if request.curves is not None and len(request.curves) > 0:
        return (
            await _materialise_curve_refs(
                refs=request.curves, owner_uid=owner_uid, ro_engine=ro_engine
            ),
            None,
        )

    pricing = swaption_request.get(_SWAP_PRICING_KEY)
    if not isinstance(pricing, dict):
        raise SwaptionCurveResolutionFailedError(
            reason=(
                "Saved swaption is missing the ``pricing`` block; cannot "
                "determine which curves to use. Re-save the swaption with "
                "a ``pricing.curves`` list or pass an explicit ``curves`` "
                "override in the pricing request."
            ),
            details=[{"swaption_pricing": pricing}],
        )

    curve_set_id = _extract_uuid(pricing, _SWAP_CURVE_SET_KEYS)
    if curve_set_id is not None:
        refs = await _refs_from_curve_set(
            curve_set_id=curve_set_id,
            owner_uid=owner_uid,
            ro_engine=ro_engine,
        )
        return (
            await _materialise_curve_refs(refs=refs, owner_uid=owner_uid, ro_engine=ro_engine),
            curve_set_id,
        )

    inline_curves_raw = pricing.get(_SWAP_CURVES_KEYS[0])
    if not isinstance(inline_curves_raw, list) or len(inline_curves_raw) == 0:
        raise SwaptionCurveResolutionFailedError(
            reason=(
                "Saved swaption has no ``pricing.curve_set_id`` and no "
                "``pricing.curves`` list; the orchestrator cannot infer "
                "which curves to bootstrap. Re-save the swaption with one "
                "of those fields populated or pass an explicit ``curves`` "
                "override in the pricing request."
            ),
        )
    refs = _refs_from_inline_curves(inline_curves_raw)
    return (
        await _materialise_curve_refs(refs=refs, owner_uid=owner_uid, ro_engine=ro_engine),
        None,
    )


async def _refs_from_curve_set(
    *,
    curve_set_id: UUID,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> list[CurveRef]:
    """Load an ``app.curve_sets`` row and project its ``body.curve_refs``."""

    sql = text(
        "SELECT id::text AS id, name, body "
        "FROM app.curve_sets "
        "WHERE id = :curve_set_id "
        "  AND owner_uid = :owner_uid "
        "  AND deleted_at IS NULL"
    )
    async with ro_engine.connect() as conn:
        result = await conn.execute(
            sql, {"curve_set_id": str(curve_set_id), "owner_uid": owner_uid}
        )
        row = result.mappings().one_or_none()
    if row is None:
        raise SwaptionCurveSetNotFoundError(curve_set_id=curve_set_id)
    raw_body = row.get("body")
    body: dict[str, Any] = raw_body if isinstance(raw_body, dict) else {}
    refs_raw = _first_present(body, _CURVE_SET_BODY_REFS_KEYS) or []
    if not isinstance(refs_raw, list):
        raise SwaptionCurveResolutionFailedError(
            reason=(f"Curve set {curve_set_id} has no ``curve_refs`` array in its body."),
            details=[{"curve_set_id": str(curve_set_id)}],
        )
    refs: list[CurveRef] = []
    for index, entry in enumerate(refs_raw):
        if not isinstance(entry, dict):
            raise SwaptionCurveResolutionFailedError(
                reason=(
                    f"Curve set {curve_set_id} has a non-object entry at "
                    f"index {index} of ``curve_refs``."
                ),
                details=[{"curve_set_id": str(curve_set_id), "index": index}],
            )
        curve_id = _extract_uuid(entry, _CURVE_REF_ID_KEYS)
        if curve_id is None:
            raise SwaptionCurveResolutionFailedError(
                reason=(
                    f"Curve set {curve_set_id} entry {index} has no valid "
                    "curve id; soft-ref shape is "
                    '``{"curve_id": "<uuid>"}`` per D9.'
                ),
                details=[{"curve_set_id": str(curve_set_id), "index": index}],
            )
        refs.append(CurveRef(id=curve_id))
    return refs


def _refs_from_inline_curves(raw: list[Any]) -> list[CurveRef]:
    """Coerce a saved ``pricing.curves`` list into :class:`CurveRef` instances."""

    refs: list[CurveRef] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise SwaptionCurveResolutionFailedError(
                reason=(f"Saved swaption ``pricing.curves[{index}]`` is not an object."),
                details=[{"index": index}],
            )
        candidate_id = _extract_uuid(entry, _CURVE_REF_ID_KEYS)
        if candidate_id is not None and not _has_inline_curve_payload(entry):
            refs.append(CurveRef(id=candidate_id))
            continue
        try:
            refs.append(CurveRef.model_validate(entry))
        except ValueError as exc:
            raise SwaptionCurveResolutionFailedError(
                reason=(
                    f"Saved swaption ``pricing.curves[{index}]`` is not a valid CurveRef shape."
                ),
                details=[{"index": index, "error": str(exc)}],
            ) from exc
    return refs


def _has_inline_curve_payload(entry: dict[str, Any]) -> bool:
    keys = set(entry.keys())
    return bool(keys - {"id", "curve_id", "curveId"})


async def _materialise_curve_refs(
    *,
    refs: Iterable[CurveRef],
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> list[ResolvedCurve]:
    """Collapse each :class:`CurveRef` into a :class:`ResolvedCurve`."""

    resolved: list[ResolvedCurve] = []
    for index, ref in enumerate(refs):
        if ref.id is not None and ref.points is None:
            row = await _fetch_curve(curve_id=ref.id, owner_uid=owner_uid, ro_engine=ro_engine)
            if row is None:
                raise SwaptionCurveResolutionFailedError(
                    reason=(f"Curve {ref.id} is not owned by the caller or has been soft-deleted."),
                    details=[{"curve_id": str(ref.id), "index": index}],
                )
            resolved.append(_resolved_curve_from_row(row))
            continue
        if ref.name is None or ref.points is None:
            raise SwaptionCurveResolutionFailedError(
                reason=(f"Inline curve at index {index} is missing ``name`` or ``points``."),
                details=[{"index": index}],
            )
        resolved.append(
            ResolvedCurve(
                id=ref.id,
                name=ref.name,
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


def _resolved_curve_from_row(row: dict[str, Any]) -> ResolvedCurve:
    raw_points = row.get("points")
    points: list[dict[str, Any]] = (
        [pt for pt in raw_points if isinstance(pt, dict)] if isinstance(raw_points, list) else []
    )
    raw_body = row.get("body")
    body: dict[str, Any] = raw_body if isinstance(raw_body, dict) else {}
    return ResolvedCurve(
        id=UUID(str(row["id"])),
        name=str(row["name"]),
        currency=row.get("currency"),
        day_counter=row.get("day_counter"),
        helper_kind=row.get("helper_kind"),
        reference_date=row.get("reference_date"),
        points=points,
        body=body,
    )


# ---------------------------------------------------------------------------
# Vol surface
# ---------------------------------------------------------------------------


async def _resolve_vol_surface(
    *,
    request: SwaptionPriceRequest,
    swaption_request: dict[str, Any],
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> tuple[ResolvedVolSurface, UUID | None]:
    """Walk the request → saved-swaption chain to produce a ResolvedVolSurface.

    Source priority:

    1. ``request.vol_surface`` (explicit override or inline-mode payload).
    2. ``swaption.request.pricing.vol_surface_id`` →
       ``app.vol_surfaces`` row.
    3. ``swaption.request.pricing.vol_surfaces[0]`` (the first entry of
       the list the portal saves; it carries either an ``id`` or
       inline ``payload``).

    Returns ``(vol_surface, vol_surface_id)``.
    """

    if request.vol_surface is not None:
        return await _materialise_vol_surface_ref(
            ref=request.vol_surface, owner_uid=owner_uid, ro_engine=ro_engine
        )

    pricing = swaption_request.get(_SWAP_PRICING_KEY)
    if not isinstance(pricing, dict):
        raise SwaptionSurfaceResolutionFailedError(
            reason=(
                "Saved swaption is missing the ``pricing`` block; cannot "
                "determine which vol surface to use. Re-save the swaption "
                "or pass an explicit ``vol_surface`` override in the "
                "pricing request."
            ),
            details=[{"swaption_pricing": pricing}],
        )

    vol_surface_id = _extract_uuid(pricing, _SWAP_VOL_SURFACE_ID_KEYS)
    if vol_surface_id is not None:
        return await _materialise_vol_surface_ref(
            ref=VolSurfaceRef(id=vol_surface_id),
            owner_uid=owner_uid,
            ro_engine=ro_engine,
        )

    inline_list = _first_present(pricing, _SWAP_VOL_SURFACES_KEYS)
    if not isinstance(inline_list, list) or len(inline_list) == 0:
        raise SwaptionSurfaceResolutionFailedError(
            reason=(
                "Saved swaption has no ``pricing.vol_surface_id`` and no "
                "``pricing.vol_surfaces`` list; cannot infer the vol "
                "surface."
            ),
        )
    first = inline_list[0]
    if not isinstance(first, dict):
        raise SwaptionSurfaceResolutionFailedError(
            reason=("Saved swaption ``pricing.vol_surfaces[0]`` is not an object."),
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
    """A vol-surface entry that carries more than just an id is inline-ish."""

    keys = set(entry.keys())
    return bool(keys - {"id", "vol_surface_id", "volSurfaceId", "name"})


def _inline_vol_surface_ref(entry: dict[str, Any]) -> VolSurfaceRef:
    """Coerce a saved ``pricing.vol_surfaces[0]`` inline blob into a typed ref.

    The portal's saved shape is the full ``VolSurfaceSpec`` (payload
    fields at the top level); the orchestrator-side reified shape
    nests them under ``payload``. We accept both: an entry that
    already has a ``payload`` key passes through; an entry that
    carries the portal's flat shape gets re-shaped into the typed
    form so the resolver downstream sees one canonical layout.
    """

    if "payload" in entry and isinstance(entry["payload"], dict):
        try:
            return VolSurfaceRef.model_validate(entry)
        except ValueError as exc:
            raise SwaptionSurfaceResolutionFailedError(
                reason=(
                    "Saved swaption ``pricing.vol_surfaces[0]`` is not a valid VolSurfaceRef shape."
                ),
                details=[{"error": str(exc)}],
            ) from exc

    kind_raw = entry.get("kind") or entry.get("payload_type")
    if not isinstance(kind_raw, str) or not kind_raw:
        raise SwaptionSurfaceResolutionFailedError(
            reason=(
                "Saved swaption ``pricing.vol_surfaces[0]`` is missing "
                "``kind`` (or ``payload_type``); cannot determine the "
                "engine bootstrap shape."
            ),
        )
    name_raw = entry.get("name")
    payload = {
        key: value
        for key, value in entry.items()
        if key not in {"id", "vol_surface_id", "volSurfaceId", "name", "kind", "payload_type"}
    }
    try:
        return VolSurfaceRef(
            id=None,
            name=name_raw if isinstance(name_raw, str) else None,
            kind=kind_raw,
            payload=payload,
        )
    except ValueError as exc:
        raise SwaptionSurfaceResolutionFailedError(
            reason=(
                "Saved swaption ``pricing.vol_surfaces[0]`` cannot be coerced into a VolSurfaceRef."
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
            raise SwaptionVolSurfaceNotFoundError(vol_surface_id=ref.id)
        return (_resolved_vol_surface_from_row(row), ref.id)

    if ref.kind is None or ref.payload is None:
        raise SwaptionSurfaceResolutionFailedError(
            reason="Inline vol surface is missing ``kind`` or ``payload``.",
        )
    return (
        ResolvedVolSurface(
            id=ref.id,
            name=ref.name or "inline-vol-surface",
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
# Swaption model
# ---------------------------------------------------------------------------


async def _resolve_swaption_model(
    *,
    request: SwaptionPriceRequest,
    swaption_request: dict[str, Any],
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> tuple[ResolvedSwaptionModel, UUID | None]:
    """Walk the request → saved-swaption chain to produce a ResolvedSwaptionModel.

    Source priority:

    1. ``request.swaption_model`` (explicit override or inline-mode payload).
    2. ``swaption.request.pricing.swaption_model_id`` →
       ``app.swaption_models`` row.
    3. ``swaption.request.pricing.models[0]`` (the first entry of the
       list the portal saves; carries either an ``id`` or inline
       calibration knobs).

    The model is **not** MD-resolved (its body is pure config) but
    we still load it so the endpoint fails fast on a stale reference
    rather than surfacing a confusing engine-side error.
    """

    if request.swaption_model is not None:
        return await _materialise_model_ref(
            ref=request.swaption_model, owner_uid=owner_uid, ro_engine=ro_engine
        )

    pricing = swaption_request.get(_SWAP_PRICING_KEY)
    if not isinstance(pricing, dict):
        raise SwaptionInvalidRequestError(
            reason=(
                "Saved swaption is missing the ``pricing`` block; cannot "
                "determine which swaption model to use. Re-save the "
                "swaption or pass an explicit ``swaption_model`` "
                "override in the pricing request."
            ),
            details=[{"swaption_pricing": pricing}],
        )

    model_id = _extract_uuid(pricing, _SWAP_MODEL_ID_KEYS)
    if model_id is not None:
        return await _materialise_model_ref(
            ref=SwaptionModelRef(id=model_id),
            owner_uid=owner_uid,
            ro_engine=ro_engine,
        )

    inline_list = _first_present(pricing, _SWAP_MODELS_KEYS)
    if not isinstance(inline_list, list) or len(inline_list) == 0:
        raise SwaptionInvalidRequestError(
            reason=(
                "Saved swaption has no ``pricing.swaption_model_id`` and "
                "no ``pricing.models`` list; cannot infer the swaption "
                "model."
            ),
        )
    first = inline_list[0]
    if not isinstance(first, dict):
        raise SwaptionInvalidRequestError(
            reason=("Saved swaption ``pricing.models[0]`` is not an object."),
            details=[{"entry": first}],
        )
    saved_id = _extract_uuid(first, _MODEL_REF_ID_KEYS)
    if saved_id is not None and not _has_inline_model_payload(first):
        return await _materialise_model_ref(
            ref=SwaptionModelRef(id=saved_id),
            owner_uid=owner_uid,
            ro_engine=ro_engine,
        )
    return await _materialise_model_ref(
        ref=_inline_model_ref(first), owner_uid=owner_uid, ro_engine=ro_engine
    )


def _has_inline_model_payload(entry: dict[str, Any]) -> bool:
    keys = set(entry.keys())
    return bool(keys - {"id", "swaption_model_id", "model_id", "swaptionModelId", "name"})


def _inline_model_ref(entry: dict[str, Any]) -> SwaptionModelRef:
    """Coerce a saved ``pricing.models[0]`` blob into a typed ref.

    Saved-row shape variants (none are tightened in the migration):

    * Already-typed form: ``{"kind": "...", "payload": {...}}``.
    * Portal's flat form: ``{"kind": "HullWhiteLattice", "hw_a": 0.05,
      "hw_sigma": 0.01, ...}`` — the calibration knobs live at the
      top level. We re-shape into the typed form below so the
      resolver downstream sees one canonical layout.
    """

    if "payload" in entry and isinstance(entry["payload"], dict):
        try:
            return SwaptionModelRef.model_validate(entry)
        except ValueError as exc:
            raise SwaptionInvalidRequestError(
                reason=(
                    "Saved swaption ``pricing.models[0]`` is not a valid SwaptionModelRef shape."
                ),
                details=[{"error": str(exc)}],
            ) from exc

    kind_raw = entry.get("kind")
    if not isinstance(kind_raw, str) or not kind_raw:
        raise SwaptionInvalidRequestError(
            reason=(
                "Saved swaption ``pricing.models[0]`` is missing ``kind`` "
                "(e.g. ``HullWhiteLattice``)."
            ),
        )
    name_raw = entry.get("name")
    payload = {
        key: value
        for key, value in entry.items()
        if key not in {"id", "swaption_model_id", "model_id", "swaptionModelId", "name", "kind"}
    }
    try:
        return SwaptionModelRef(
            id=None,
            name=name_raw if isinstance(name_raw, str) else None,
            kind=kind_raw,
            payload=payload,
        )
    except ValueError as exc:
        raise SwaptionInvalidRequestError(
            reason=(
                "Saved swaption ``pricing.models[0]`` cannot be coerced into a SwaptionModelRef."
            ),
            details=[{"error": str(exc)}],
        ) from exc


async def _materialise_model_ref(
    *,
    ref: SwaptionModelRef,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> tuple[ResolvedSwaptionModel, UUID | None]:
    """Collapse one :class:`SwaptionModelRef` into a :class:`ResolvedSwaptionModel`."""

    if ref.id is not None and ref.payload is None:
        row = await _fetch_swaption_model(
            swaption_model_id=ref.id, owner_uid=owner_uid, ro_engine=ro_engine
        )
        if row is None:
            raise SwaptionModelNotFoundError(swaption_model_id=ref.id)
        return (_resolved_model_from_row(row), ref.id)

    if ref.kind is None or ref.payload is None:
        raise SwaptionInvalidRequestError(
            reason=("Inline swaption model is missing ``kind`` or ``payload``."),
        )
    return (
        ResolvedSwaptionModel(
            id=ref.id,
            name=ref.name or "inline-swaption-model",
            kind=ref.kind,
            payload=dict(ref.payload),
        ),
        ref.id,
    )


async def _fetch_swaption_model(
    *,
    swaption_model_id: UUID,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> dict[str, Any] | None:
    sql = text(
        "SELECT id::text AS id, name, kind, payload "
        "FROM app.swaption_models "
        "WHERE id = :swaption_model_id "
        "  AND owner_uid = :owner_uid "
        "  AND deleted_at IS NULL"
    )
    async with ro_engine.connect() as conn:
        result = await conn.execute(
            sql,
            {
                "swaption_model_id": str(swaption_model_id),
                "owner_uid": owner_uid,
            },
        )
        row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


def _resolved_model_from_row(row: dict[str, Any]) -> ResolvedSwaptionModel:
    raw_payload = row.get("payload")
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
    return ResolvedSwaptionModel(
        id=UUID(str(row["id"])),
        name=str(row["name"]),
        kind=str(row["kind"]),
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Underlying-swap float index (mirrors bonds/assembler.py::_resolve_index)
# ---------------------------------------------------------------------------


async def _resolve_index(
    *,
    request: SwaptionPriceRequest,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> ResolvedIndex | None:
    """Resolve the underlying swap's float-leg index from ``request.index``.

    Fully optional + back-compatible: when ``request.index`` is ``None`` this
    returns ``None`` so ``ResolvedMarketData.index`` stays ``None`` and the
    underlying float leg keeps exactly today's behaviour (the interim default
    forwarding index + Semiannual coupon frequency). When supplied it collapses
    the ref-or-inline :class:`IndexRef` into a :class:`ResolvedIndex` the same
    way swap_ir / bonds do; the translator then builds a faithful ``IndexDef``
    and ``float_leg_frequency`` follows its tenor.

    Unlike bonds this never reads the trade body: the swaption's saved
    underlying-swap index is not surfaced today, so reading it here would
    silently change existing swaption prices. The request field is the only
    source (codes on a bad ref / malformed inline spec).
    """

    if request.index is None:
        return None
    return await _materialise_index_ref(ref=request.index, owner_uid=owner_uid, ro_engine=ro_engine)


async def _materialise_index_ref(
    *,
    ref: IndexRef,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> ResolvedIndex:
    """Collapse one :class:`IndexRef` into a :class:`ResolvedIndex`."""

    if ref.id is not None and ref.body is None:
        row = await _fetch_index(index_id=ref.id, owner_uid=owner_uid, ro_engine=ro_engine)
        if row is None:
            raise SwaptionIndexNotFoundError(index_id=ref.id)
        return _resolved_index_from_row(row)

    if ref.kind is None or ref.body is None:
        raise SwaptionIndexResolutionFailedError(
            reason="Inline index is missing ``kind`` or ``body``.",
            details=[{"role": "index"}],
        )
    return ResolvedIndex(
        id=ref.id,
        name=ref.name or "inline-index",
        kind=ref.kind,
        currency=ref.currency,
        calendar=ref.calendar,
        day_counter=ref.day_counter,
        body=dict(ref.body),
    )


async def _fetch_index(
    *,
    index_id: UUID,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> dict[str, Any] | None:
    """Load one live ``app.indices`` row by ``(owner_uid, id)`` or return ``None``.

    Soft-deleted rows are invisible (``deleted_at IS NULL``); cross-tenant
    rows never match (``owner_uid`` predicate) — north-star #5.
    """

    sql = text(
        "SELECT id::text AS id, name, kind, currency, calendar, "
        "       day_counter, body "
        "FROM app.indices "
        "WHERE id = :index_id "
        "  AND owner_uid = :owner_uid "
        "  AND deleted_at IS NULL"
    )
    async with ro_engine.connect() as conn:
        result = await conn.execute(sql, {"index_id": str(index_id), "owner_uid": owner_uid})
        row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


def _resolved_index_from_row(row: dict[str, Any]) -> ResolvedIndex:
    raw_body = row.get("body")
    body: dict[str, Any] = raw_body if isinstance(raw_body, dict) else {}
    return ResolvedIndex(
        id=UUID(str(row["id"])),
        name=str(row["name"]),
        kind=str(row["kind"]),
        currency=row.get("currency"),
        calendar=row.get("calendar"),
        day_counter=row.get("day_counter"),
        body=body,
    )


# ---------------------------------------------------------------------------
# Snapshot (identical to swap_ir; duplicated per the "Key product-
# specific additions" — do not factor a shared module until 08+ lands)
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
        raise SwaptionSnapshotNotFoundError(snapshot_id=snapshot_id)
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
