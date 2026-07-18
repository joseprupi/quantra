"""Load + collapse the bonds pricing request into an engine-ready shape.

Reads ``app.bonds_{fixed,floating}`` / ``app.curves`` / ``app.curve_sets``
/ ``app.indices`` / ``app.snapshots`` via the ``app_ro`` engine.
Writes nothing.

Two public callables, one shared internal SQL site per ``app.*`` table:

* :func:`assemble_fixed` — resolves the request shape into a
  :class:`FixedAssemblerOutput`; the floating-only ``app.indices`` /
  projection-curve sites are never touched.
* :func:`assemble_floating` — resolves into a
  :class:`FloatingAssemblerOutput` that additionally carries a
  projection curve + index.

The assembler and md_resolution modules are shared between the two
variants on purpose: two endpoints, one module — divergent code paths
would be a smell that one product should be split off. Every helper
that isn't strictly variant-specific is shared (curve load,
snapshot load, ``_extract_uuid`` /
``_first_present``) and by making the two ``assemble_*`` entry points
the only places where the fixed / floating divergence shows up.

The assembler's three jobs:

1. **Branch collapsing.** Either ``bond_id`` (by reference) or
   ``bond`` (inline) populates the variant's typed trade. Either
   ``curves[]`` from the request, the saved bond's pricing-block
   refs, or the bond's own inline curve list fills the
   :class:`ResolvedCurve` slot(s). Cross-entity refs inside the
   saved bond's JSONB are soft: missing referents surface as
   ``bond_{fixed,floating}_not_found`` /
   ``bond_*_curve_not_found`` / ``bond_curve_resolution_failed``
   rather than silently resolving to null.

2. **Floating curve roles.** The floating variant needs two curves
   with distinct roles (discount + projection). The assembler resolves
   role markers from the saved bond's pricing block (``discount_curve_id``
   / ``forecast_curve_id`` per the portal's saved shape) and falls back
   to the request-side ``curves`` override (which, when present, must
   carry both roles via ``role`` markers on each :class:`CurveRef` —
   see :func:`_floating_role_for_ref`).

3. **Snapshot pinning.** When ``snapshot_id`` is set, the
   :class:`ResolvedSnapshot` returned alongside the other outputs
   gives :mod:`md_resolution` a precomputed ``canonical_id → value``
   map. Quotes that aren't pinned fall through to the live MD
   service.

This module deliberately duplicates structure from
``pricing/swap_ir/assembler.py`` for the discount-curve / snapshot
slices — a shared walker is a candidate future refactor now that
several products carry the same shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_orchestrator.pricing.bonds.errors import (
    BondCurveResolutionFailedError,
    BondDiscountCurveNotFoundError,
    BondFixedNotFoundError,
    BondFloatingNotFoundError,
    BondIndexNotFoundError,
    BondProjectionCurveNotFoundError,
    BondSnapshotNotFoundError,
)
from quantra_orchestrator.pricing.bonds.models import (
    CurveRef,
    FixedBondPriceRequest,
    FixedBondTrade,
    FloatingBondPriceRequest,
    FloatingBondTrade,
    IndexRef,
    ResolvedCurve,
    ResolvedIndex,
)

_BOND_PRICING_KEY = "pricing"
_BOND_CURVE_SET_KEYS: tuple[str, ...] = ("curve_set_id", "curveSetId")
_BOND_CURVES_KEYS: tuple[str, ...] = ("curves",)
_BOND_DISCOUNT_CURVE_ID_KEYS: tuple[str, ...] = (
    "discount_curve_id",
    "discountCurveId",
)
_BOND_PROJECTION_CURVE_ID_KEYS: tuple[str, ...] = (
    "forecast_curve_id",
    "forecastCurveId",
    "projection_curve_id",
    "projectionCurveId",
)
_BOND_INDEX_ID_KEYS: tuple[str, ...] = (
    "index_id",
    "indexId",
    "index_ref_id",
    "indexRefId",
)
_CURVE_SET_BODY_REFS_KEYS: tuple[str, ...] = ("curve_refs", "curveRefs")
_CURVE_REF_ID_KEYS: tuple[str, ...] = ("curve_id", "curveId", "id")
_CURVE_REF_ROLE_KEYS: tuple[str, ...] = ("role",)

_ROLE_DISCOUNT = "discount"
_ROLE_PROJECTION = "projection"

# Two distinct refs are needed to map onto (discount, projection) when
# no explicit ``role`` markers are present. A single unroled entry can
# only fill both roles when ``use_same_curve`` is set on the saved
# bond; otherwise the assembler falls back to the next source.
_MIN_REFS_FOR_DISTINCT_ROLES: Final[int] = 2


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedSnapshot:
    """A loaded ``app.snapshots`` row, projected for MD resolution.

    Same shape (and same projection logic) as the swap_ir / swaption-
    side ``ResolvedSnapshot``; the MD-resolution walker consults
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
class FixedAssemblerOutput:
    """Everything the fixed-bond route needs to call MD walker + engine.

    ``trade`` packs the bond body. ``discount_curve`` is the single
    resolved curve. ``curve_set_id`` / ``discount_curve_id`` carry
    the logical bundle ids for the ``shared_inputs`` (the future
    ``group_by_curve_set`` policy keys off ``curve_set_id``; the
    discount-curve id is surfaced for log correlation + the assembled-
    request echo). ``snapshot`` is the optional MD pin.
    """

    trade: FixedBondTrade
    discount_curve: ResolvedCurve
    curve_set_id: UUID | None
    discount_curve_id: UUID | None
    snapshot: ResolvedSnapshot | None


@dataclass(frozen=True, slots=True)
class FloatingAssemblerOutput:
    """Everything the floating-bond route needs to call MD walker + engine.

    Adds the projection curve + index alongside the discount curve.
    ``projection_curve_id`` / ``index_id`` surface for the assembled-
    request echo and for any future policy that groups by projection
    curve or index.
    """

    trade: FloatingBondTrade
    discount_curve: ResolvedCurve
    projection_curve: ResolvedCurve
    index: ResolvedIndex
    curve_set_id: UUID | None
    discount_curve_id: UUID | None
    projection_curve_id: UUID | None
    index_id: UUID | None
    snapshot: ResolvedSnapshot | None


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def assemble_fixed(
    request: FixedBondPriceRequest,
    *,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> FixedAssemblerOutput:
    """Resolve a fixed-rate bond pricing request.

    Steps:

    1. Materialise the trade (load ``app.bonds_fixed`` or copy inline).
    2. Resolve the single discount curve via the request → saved-bond
       chain (request override → ``pricing.discount_curve_id`` →
       ``pricing.curve_set_id`` → ``pricing.curves[0]``).
    3. Optionally load the pinned ``app.snapshots`` row.

    Every read goes through ``app_ro``. Soft references
    that resolve to ``None`` raise the appropriate
    ``bond_fixed_not_found`` / ``bond_discount_curve_not_found`` /
    ``bond_curve_resolution_failed`` / ``bond_snapshot_not_found``.
    """

    trade, bond_request = await _load_fixed_trade(
        request=request, owner_uid=owner_uid, ro_engine=ro_engine
    )

    discount_curve, curve_set_id, discount_curve_id = await _resolve_fixed_curve(
        request=request,
        bond_request=bond_request,
        owner_uid=owner_uid,
        ro_engine=ro_engine,
    )

    snapshot = await _load_snapshot(
        snapshot_id=request.snapshot_id,
        owner_uid=owner_uid,
        ro_engine=ro_engine,
    )

    return FixedAssemblerOutput(
        trade=trade,
        discount_curve=discount_curve,
        curve_set_id=curve_set_id,
        discount_curve_id=discount_curve_id,
        snapshot=snapshot,
    )


async def assemble_floating(
    request: FloatingBondPriceRequest,
    *,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> FloatingAssemblerOutput:
    """Resolve a floating-rate bond pricing request.

    Steps:

    1. Materialise the trade (load ``app.bonds_floating`` or copy
       inline).
    2. Resolve the two curves (discount + projection) with explicit
       role markers per :func:`_resolve_floating_curves`.
    3. Resolve the projection index (request override → saved-bond
       ``index_id`` / inline ``index``).
    4. Optionally load the pinned ``app.snapshots`` row.

    Every read goes through ``app_ro``. Soft references
    surface the per-bundle 404s (``bond_floating_not_found`` /
    ``bond_discount_curve_not_found`` /
    ``bond_projection_curve_not_found`` / ``bond_index_not_found`` /
    ``bond_snapshot_not_found``) and the shared 422
    ``bond_curve_resolution_failed``.
    """

    trade, bond_request = await _load_floating_trade(
        request=request, owner_uid=owner_uid, ro_engine=ro_engine
    )

    (
        discount_curve,
        projection_curve,
        curve_set_id,
        discount_curve_id,
        projection_curve_id,
    ) = await _resolve_floating_curves(
        request=request,
        bond_request=bond_request,
        owner_uid=owner_uid,
        ro_engine=ro_engine,
    )

    index, index_id = await _resolve_index(
        request=request,
        bond_request=bond_request,
        owner_uid=owner_uid,
        ro_engine=ro_engine,
    )

    snapshot = await _load_snapshot(
        snapshot_id=request.snapshot_id,
        owner_uid=owner_uid,
        ro_engine=ro_engine,
    )

    return FloatingAssemblerOutput(
        trade=trade,
        discount_curve=discount_curve,
        projection_curve=projection_curve,
        index=index,
        curve_set_id=curve_set_id,
        discount_curve_id=discount_curve_id,
        projection_curve_id=projection_curve_id,
        index_id=index_id,
        snapshot=snapshot,
    )


# ---------------------------------------------------------------------------
# Trade loaders (one per variant — minimal divergence: table name + code)
# ---------------------------------------------------------------------------


async def _load_fixed_trade(
    *,
    request: FixedBondPriceRequest,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> tuple[FixedBondTrade, dict[str, Any]]:
    """Return ``(trade, raw_request_body)`` for the requested fixed bond.

    The raw request body is returned alongside the typed trade so the
    curve-resolution step can inspect the saved bond's
    ``pricing.discount_curve_id`` / ``pricing.curve_set_id`` /
    ``pricing.curves`` without re-loading.
    """

    if request.bond is not None:
        return (
            FixedBondTrade(bond_id=None, name=None, bond=dict(request.bond)),
            dict(request.bond),
        )

    assert request.bond_id is not None  # nosec B101 - guaranteed by pydantic
    row = await _fetch_bond_row(
        owner_uid=owner_uid,
        bond_id=request.bond_id,
        ro_engine=ro_engine,
        table="bonds_fixed",
    )
    if row is None:
        raise BondFixedNotFoundError(bond_id=request.bond_id)
    raw_request = row["request"] if isinstance(row.get("request"), dict) else {}
    trade = FixedBondTrade(
        bond_id=request.bond_id,
        name=row.get("name"),
        bond=raw_request,
    )
    return trade, raw_request


async def _load_floating_trade(
    *,
    request: FloatingBondPriceRequest,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> tuple[FloatingBondTrade, dict[str, Any]]:
    """Return ``(trade, raw_request_body)`` for the requested floating bond."""

    if request.bond is not None:
        return (
            FloatingBondTrade(bond_id=None, name=None, bond=dict(request.bond)),
            dict(request.bond),
        )

    assert request.bond_id is not None  # nosec B101 - guaranteed by pydantic
    row = await _fetch_bond_row(
        owner_uid=owner_uid,
        bond_id=request.bond_id,
        ro_engine=ro_engine,
        table="bonds_floating",
    )
    if row is None:
        raise BondFloatingNotFoundError(bond_id=request.bond_id)
    raw_request = row["request"] if isinstance(row.get("request"), dict) else {}
    trade = FloatingBondTrade(
        bond_id=request.bond_id,
        name=row.get("name"),
        bond=raw_request,
    )
    return trade, raw_request


async def _fetch_bond_row(
    *,
    owner_uid: str,
    bond_id: UUID,
    ro_engine: AsyncEngine,
    table: str,
) -> dict[str, Any] | None:
    """Load one live ``app.bonds_{fixed,floating}`` row by ``(owner_uid, id)``.

    ``table`` is from a fixed in-module allow-list (the two callers
    pass either ``"bonds_fixed"`` or ``"bonds_floating"``); we
    f-string it into the SQL because SQLAlchemy's ``text(...)`` only
    parameterises values, not identifiers. The allow-list keeps
    SQL injection out of scope.
    """

    assert table in {"bonds_fixed", "bonds_floating"}, (  # nosec B101 - allow-list
        f"unexpected bond table: {table!r}"
    )
    sql = text(
        f"SELECT id::text AS id, name, request "  # noqa: S608 -- allow-listed table name
        f"FROM app.{table} "
        f"WHERE id = :bond_id "
        f"  AND owner_uid = :owner_uid "
        f"  AND deleted_at IS NULL"
    )
    async with ro_engine.connect() as conn:
        result = await conn.execute(sql, {"bond_id": str(bond_id), "owner_uid": owner_uid})
        row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Fixed-bond curve resolution
# ---------------------------------------------------------------------------


async def _resolve_fixed_curve(
    *,
    request: FixedBondPriceRequest,
    bond_request: dict[str, Any],
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> tuple[ResolvedCurve, UUID | None, UUID | None]:
    """Resolve the single discount curve for a fixed-rate bond.

    Source priority:

    1. ``request.curves[0]`` (explicit override or inline-mode payload).
       The fixed route consumes exactly one curve; a multi-entry list
       collapses to the first entry with the rest ignored (the future
       portfolio plan will revisit this; today the single-bond shape
       is the only consumer).
    2. ``bond.pricing.discount_curve_id`` → ``app.curves`` row.
    3. ``bond.pricing.curve_set_id`` → ``app.curve_sets`` row; the
       first ``curve_refs[]`` entry's curve becomes the discount curve.
    4. ``bond.pricing.curves[0]`` (per-curve refs or inline definitions
       baked into the saved bond).

    Returns ``(discount_curve, curve_set_id, discount_curve_id)``.
    """

    if request.curves is not None and len(request.curves) > 0:
        ref = request.curves[0]
        resolved = await _materialise_single_ref(
            ref=ref,
            role=_ROLE_DISCOUNT,
            owner_uid=owner_uid,
            ro_engine=ro_engine,
        )
        return resolved, None, ref.id

    pricing = bond_request.get(_BOND_PRICING_KEY)
    if not isinstance(pricing, dict):
        raise BondCurveResolutionFailedError(
            reason=(
                "Saved bond is missing the ``pricing`` block; cannot "
                "determine which discount curve to use. Re-save the "
                "bond with a ``pricing.discount_curve_id`` field or "
                "pass an explicit ``curves`` override in the pricing "
                "request."
            ),
            details=[{"bond_pricing": pricing, "role": _ROLE_DISCOUNT}],
        )

    discount_id = _extract_uuid(pricing, _BOND_DISCOUNT_CURVE_ID_KEYS)
    if discount_id is not None:
        row = await _fetch_curve(curve_id=discount_id, owner_uid=owner_uid, ro_engine=ro_engine)
        if row is None:
            raise BondDiscountCurveNotFoundError(curve_id=discount_id)
        return _resolved_curve_from_row(row), None, discount_id

    curve_set_id = _extract_uuid(pricing, _BOND_CURVE_SET_KEYS)
    if curve_set_id is not None:
        refs = await _refs_from_curve_set(
            curve_set_id=curve_set_id,
            owner_uid=owner_uid,
            ro_engine=ro_engine,
        )
        if not refs:
            raise BondCurveResolutionFailedError(
                reason=(
                    f"Curve set {curve_set_id} has an empty "
                    "``curve_refs`` list; cannot pick a discount curve."
                ),
                details=[{"curve_set_id": str(curve_set_id)}],
            )
        first = refs[0]
        resolved = await _materialise_single_ref(
            ref=first,
            role=_ROLE_DISCOUNT,
            owner_uid=owner_uid,
            ro_engine=ro_engine,
        )
        return resolved, curve_set_id, first.id

    inline_curves_raw = pricing.get(_BOND_CURVES_KEYS[0])
    if not isinstance(inline_curves_raw, list) or len(inline_curves_raw) == 0:
        raise BondCurveResolutionFailedError(
            reason=(
                "Saved bond has no ``pricing.discount_curve_id``, no "
                "``pricing.curve_set_id`` and no ``pricing.curves`` "
                "list; the orchestrator cannot infer which discount "
                "curve to use."
            ),
            details=[{"role": _ROLE_DISCOUNT}],
        )
    refs = _refs_from_inline_curves(inline_curves_raw)
    if not refs:
        raise BondCurveResolutionFailedError(
            reason=(
                "Saved bond ``pricing.curves`` collapsed to an empty "
                "ref list; cannot pick a discount curve."
            ),
            details=[{"role": _ROLE_DISCOUNT}],
        )
    first = refs[0]
    resolved = await _materialise_single_ref(
        ref=first,
        role=_ROLE_DISCOUNT,
        owner_uid=owner_uid,
        ro_engine=ro_engine,
    )
    return resolved, None, first.id


# ---------------------------------------------------------------------------
# Floating-bond curve resolution (discount + projection)
# ---------------------------------------------------------------------------


async def _resolve_floating_curves(
    *,
    request: FloatingBondPriceRequest,
    bond_request: dict[str, Any],
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> tuple[ResolvedCurve, ResolvedCurve, UUID | None, UUID | None, UUID | None]:
    """Resolve the discount + projection curves for a floating-rate bond.

    Source priority is per-role; both must end up resolved or the
    422 ``bond_curve_resolution_failed`` fires.

    Per-role chain:

    1. ``request.curves[]`` (explicit override). When set, each entry's
       ``body.role`` discriminates ``discount`` from ``projection``;
       if both roles aren't covered the call fails. A degenerate
       single-entry list works when ``bond.use_same_curve`` is true
       (the same curve plays both roles).
    2. ``bond.pricing.discount_curve_id`` /
       ``bond.pricing.forecast_curve_id`` — the portal's saved shape.
    3. ``bond.pricing.curve_set_id`` → ``app.curve_sets`` row; the
       set's ``curve_refs[]`` entries are walked looking for ``role``
       markers, falling back to "first entry = discount, second =
       projection" if no role markers are present.

    Returns ``(discount_curve, projection_curve, curve_set_id,
    discount_curve_id, projection_curve_id)``. The two curves may
    reference the same ``app.curves`` row (``use_same_curve`` on the
    portal side); the engine-side payload still labels them
    separately.
    """

    use_same_curve = _coerce_bool(
        bond_request.get("use_same_curve") or bond_request.get("useSameCurve")
    )
    discount_ref, projection_ref, curve_set_id = await _gather_floating_role_refs(
        request=request,
        bond_request=bond_request,
        owner_uid=owner_uid,
        ro_engine=ro_engine,
        use_same_curve=use_same_curve,
    )

    if discount_ref is None:
        raise BondCurveResolutionFailedError(
            reason=(
                "Floating bond is missing the discount curve "
                "(``pricing.discount_curve_id`` or a ``role=discount`` "
                "entry in the curves override)."
            ),
            details=[{"role": _ROLE_DISCOUNT}],
        )
    if projection_ref is None:
        raise BondCurveResolutionFailedError(
            reason=(
                "Floating bond is missing the projection curve "
                "(``pricing.forecast_curve_id`` / "
                "``pricing.projection_curve_id`` or a ``role=projection`` "
                "entry in the curves override)."
            ),
            details=[{"role": _ROLE_PROJECTION}],
        )

    discount_curve = await _materialise_single_ref(
        ref=discount_ref,
        role=_ROLE_DISCOUNT,
        owner_uid=owner_uid,
        ro_engine=ro_engine,
    )
    projection_curve, projection_id = await _materialise_projection(
        discount_ref=discount_ref,
        projection_ref=projection_ref,
        discount_curve=discount_curve,
        use_same_curve=use_same_curve,
        owner_uid=owner_uid,
        ro_engine=ro_engine,
    )
    return (
        discount_curve,
        projection_curve,
        curve_set_id,
        discount_ref.id,
        projection_id,
    )


async def _gather_floating_role_refs(
    *,
    request: FloatingBondPriceRequest,
    bond_request: dict[str, Any],
    owner_uid: str,
    ro_engine: AsyncEngine,
    use_same_curve: bool,
) -> tuple[CurveRef | None, CurveRef | None, UUID | None]:
    """Walk the four-source chain to populate (discount_ref, projection_ref).

    Sources (in priority order): request override, saved-bond
    pricing block direct fields, saved-bond curve_set_id, saved-bond
    pricing.curves[]. Returns ``(discount_ref, projection_ref,
    curve_set_id)``; either ref may be ``None`` when no source covers
    it (the caller raises ``bond_curve_resolution_failed`` for that).
    """

    discount_ref: CurveRef | None = None
    projection_ref: CurveRef | None = None
    curve_set_id: UUID | None = None

    if request.curves is not None and len(request.curves) > 0:
        discount_ref, projection_ref = _split_floating_request_overrides(
            refs=request.curves, use_same_curve=use_same_curve
        )

    if discount_ref is not None and projection_ref is not None:
        return discount_ref, projection_ref, curve_set_id

    pricing = bond_request.get(_BOND_PRICING_KEY)
    if not isinstance(pricing, dict):
        raise BondCurveResolutionFailedError(
            reason=(
                "Saved floating bond is missing the ``pricing`` block; "
                "cannot determine which curves to use. Re-save the "
                "bond with ``pricing.discount_curve_id`` + "
                "``pricing.forecast_curve_id`` or pass explicit "
                "``curves`` overrides in the pricing request."
            ),
            details=[{"bond_pricing": pricing}],
        )

    discount_id_raw = _extract_uuid(pricing, _BOND_DISCOUNT_CURVE_ID_KEYS)
    projection_id_raw = _extract_uuid(pricing, _BOND_PROJECTION_CURVE_ID_KEYS)
    if discount_ref is None and discount_id_raw is not None:
        discount_ref = CurveRef(id=discount_id_raw)
    if projection_ref is None and projection_id_raw is not None:
        projection_ref = CurveRef(id=projection_id_raw)

    # ``use_same_curve`` shorthand: if the saved bond carries only
    # one role's id (typically discount) plus the flag, the other
    # role uses the same row. Fills in the missing slot symmetrically
    # so a future bond that carries only ``forecast_curve_id`` +
    # ``use_same_curve=true`` also works.
    if use_same_curve:
        if discount_ref is None and projection_ref is not None:
            discount_ref = projection_ref
        elif projection_ref is None and discount_ref is not None:
            projection_ref = discount_ref

    if discount_ref is None or projection_ref is None:
        curve_set_id = _extract_uuid(pricing, _BOND_CURVE_SET_KEYS)
        if curve_set_id is not None:
            set_refs = await _refs_from_curve_set(
                curve_set_id=curve_set_id,
                owner_uid=owner_uid,
                ro_engine=ro_engine,
            )
            set_disc, set_proj = _split_curve_set_refs(
                refs=set_refs,
                use_same_curve=use_same_curve,
                curve_set_id=curve_set_id,
            )
            discount_ref = discount_ref or set_disc
            projection_ref = projection_ref or set_proj

    if discount_ref is None or projection_ref is None:
        inline_curves_raw = pricing.get(_BOND_CURVES_KEYS[0])
        if isinstance(inline_curves_raw, list) and inline_curves_raw:
            inline_refs = _refs_from_inline_curves(inline_curves_raw)
            inline_disc, inline_proj = _split_floating_request_overrides(
                refs=inline_refs, use_same_curve=use_same_curve
            )
            discount_ref = discount_ref or inline_disc
            projection_ref = projection_ref or inline_proj

    return discount_ref, projection_ref, curve_set_id


async def _materialise_projection(
    *,
    discount_ref: CurveRef,
    projection_ref: CurveRef,
    discount_curve: ResolvedCurve,
    use_same_curve: bool,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> tuple[ResolvedCurve, UUID | None]:
    """Materialise the projection curve, reusing the discount when it can.

    Two short-circuit cases skip the DB round-trip:

    * ``use_same_curve`` shorthand: caller (or saved bond) wants the
      same ``app.curves`` row to play both roles. The
      ``projection_ref.id`` may be ``None`` (the shorthand was the
      only signal); we still report a non-null projection_curve_id
      so the assembled-request echo is honest about the shape.
    * Refs pointing at the same row: reuse the materialised
      ``discount_curve`` rather than re-fetching the same row.

    Otherwise we materialise normally.
    """

    if use_same_curve and discount_ref.id is not None and projection_ref.id is None:
        return discount_curve, discount_ref.id
    if discount_ref.id is not None and projection_ref.id == discount_ref.id:
        return discount_curve, projection_ref.id
    projection_curve = await _materialise_single_ref(
        ref=projection_ref,
        role=_ROLE_PROJECTION,
        owner_uid=owner_uid,
        ro_engine=ro_engine,
    )
    return projection_curve, projection_ref.id


def _split_floating_request_overrides(
    *,
    refs: list[CurveRef],
    use_same_curve: bool,
) -> tuple[CurveRef | None, CurveRef | None]:
    """Map a flat curves list onto (discount, projection) roles.

    Priorities:

    * If any entry has ``body.role == "discount"`` / ``"projection"``,
      use those.
    * Otherwise, if exactly one entry is present and ``use_same_curve``
      is True, that entry plays both roles.
    * Otherwise, the first two entries map to (discount, projection)
      in declaration order.

    Returns ``(discount_ref, projection_ref)``. Either may be
    ``None`` if the override is incomplete — the caller falls back
    to the saved-bond chain.
    """

    discount: CurveRef | None = None
    projection: CurveRef | None = None
    unroled: list[CurveRef] = []
    for ref in refs:
        role = _ref_role(ref)
        if role == _ROLE_DISCOUNT and discount is None:
            discount = ref
        elif role == _ROLE_PROJECTION and projection is None:
            projection = ref
        else:
            unroled.append(ref)

    if discount is not None and projection is not None:
        return discount, projection
    return _fill_missing_roles(
        discount=discount,
        projection=projection,
        unroled=unroled,
        use_same_curve=use_same_curve,
    )


def _fill_missing_roles(
    *,
    discount: CurveRef | None,
    projection: CurveRef | None,
    unroled: list[CurveRef],
    use_same_curve: bool,
) -> tuple[CurveRef | None, CurveRef | None]:
    """Fill in missing discount/projection slots from unroled entries.

    Three sub-cases:

    * ``use_same_curve`` + one unroled entry → that entry fills any
      empty slots.
    * Two+ unroled entries → first/second by position fill any empty
      slots in (discount, projection) order.
    * Exactly one unroled entry + exactly one empty slot → entry
      fills that slot.

    Any other shape leaves slots ``None`` and the caller falls back
    to the next source.
    """

    if len(unroled) == 1 and use_same_curve:
        sole = unroled[0]
        return (discount or sole, projection or sole)
    if len(unroled) >= _MIN_REFS_FOR_DISTINCT_ROLES:
        return (discount or unroled[0], projection or unroled[1])
    if len(unroled) == 1:
        if discount is None and projection is not None:
            return unroled[0], projection
        if projection is None and discount is not None:
            return discount, unroled[0]
    return discount, projection


def _split_curve_set_refs(
    *,
    refs: list[CurveRef],
    use_same_curve: bool,
    curve_set_id: UUID,
) -> tuple[CurveRef | None, CurveRef | None]:
    """Map ``app.curve_sets`` refs onto floating-bond curve roles.

    Same role-marker → "single curve if use_same_curve" → "first two
    by position" chain as :func:`_split_floating_request_overrides`,
    plus a curve-set-aware error when the role markers leave both
    slots empty.
    """

    discount, projection = _split_floating_request_overrides(
        refs=refs, use_same_curve=use_same_curve
    )
    if discount is None and projection is None and refs:
        raise BondCurveResolutionFailedError(
            reason=(
                f"Curve set {curve_set_id} has no ``role`` markers on "
                "its ``curve_refs`` entries and only one entry is "
                "present; cannot infer discount + projection roles. "
                "Re-save the bond with explicit "
                "``pricing.discount_curve_id`` + "
                "``pricing.forecast_curve_id`` or tag the curve-set "
                "entries with ``role``."
            ),
            details=[{"curve_set_id": str(curve_set_id)}],
        )
    return discount, projection


def _ref_role(ref: CurveRef) -> str | None:
    """Return the role marker stored on a :class:`CurveRef`'s body, if any."""

    if ref.body is None:
        return None
    raw = _first_present(ref.body, _CURVE_REF_ROLE_KEYS)
    if isinstance(raw, str) and raw:
        return raw
    return None


# ---------------------------------------------------------------------------
# Curve loaders (shared between variants)
# ---------------------------------------------------------------------------


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
            sql,
            {"curve_set_id": str(curve_set_id), "owner_uid": owner_uid},
        )
        row = result.mappings().one_or_none()
    if row is None:
        raise BondCurveResolutionFailedError(
            reason=f"Curve set {curve_set_id} is not visible to the caller.",
            details=[{"curve_set_id": str(curve_set_id)}],
        )
    raw_body = row.get("body")
    body: dict[str, Any] = raw_body if isinstance(raw_body, dict) else {}
    refs_raw = _first_present(body, _CURVE_SET_BODY_REFS_KEYS) or []
    if not isinstance(refs_raw, list):
        raise BondCurveResolutionFailedError(
            reason=(f"Curve set {curve_set_id} has no ``curve_refs`` array in its body."),
            details=[{"curve_set_id": str(curve_set_id)}],
        )
    refs: list[CurveRef] = []
    for index, entry in enumerate(refs_raw):
        if not isinstance(entry, dict):
            raise BondCurveResolutionFailedError(
                reason=(
                    f"Curve set {curve_set_id} has a non-object entry "
                    f"at index {index} of ``curve_refs``."
                ),
                details=[{"curve_set_id": str(curve_set_id), "index": index}],
            )
        curve_id = _extract_uuid(entry, _CURVE_REF_ID_KEYS)
        if curve_id is None:
            raise BondCurveResolutionFailedError(
                reason=(
                    f"Curve set {curve_set_id} entry {index} has no "
                    "valid curve id; soft-ref shape is "
                    '``{"curve_id": "<uuid>"}`` per D9.'
                ),
                details=[{"curve_set_id": str(curve_set_id), "index": index}],
            )
        role_raw = _first_present(entry, _CURVE_REF_ROLE_KEYS)
        role_body: dict[str, Any] | None = None
        if isinstance(role_raw, str) and role_raw:
            role_body = {"role": role_raw}
        refs.append(CurveRef(id=curve_id, body=role_body))
    return refs


def _refs_from_inline_curves(raw: list[Any]) -> list[CurveRef]:
    """Coerce a saved ``pricing.curves`` list into :class:`CurveRef` instances.

    Each entry is either a ref (``{"id": "<uuid>"}``) or an inline
    curve definition. Failures here surface as
    ``bond_curve_resolution_failed`` rather than ``validation_error``
    because the offending data lives in the saved bond, not the
    inbound request.
    """

    refs: list[CurveRef] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise BondCurveResolutionFailedError(
                reason=(f"Saved bond ``pricing.curves[{index}]`` is not an object."),
                details=[{"index": index}],
            )
        candidate_id = _extract_uuid(entry, _CURVE_REF_ID_KEYS)
        if candidate_id is not None and not _has_inline_curve_payload(entry):
            role_raw = _first_present(entry, _CURVE_REF_ROLE_KEYS)
            role_body: dict[str, Any] | None = None
            if isinstance(role_raw, str) and role_raw:
                role_body = {"role": role_raw}
            refs.append(CurveRef(id=candidate_id, body=role_body))
            continue
        try:
            refs.append(CurveRef.model_validate(entry))
        except ValueError as exc:
            raise BondCurveResolutionFailedError(
                reason=(f"Saved bond ``pricing.curves[{index}]`` is not a valid CurveRef shape."),
                details=[{"index": index, "error": str(exc)}],
            ) from exc
    return refs


def _has_inline_curve_payload(entry: dict[str, Any]) -> bool:
    """Decide whether an entry that *also* has an ``id`` should be inline."""

    keys = set(entry.keys())
    return bool(keys - {"id", "curve_id", "curveId", "role"})


async def _materialise_single_ref(
    *,
    ref: CurveRef,
    role: str,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> ResolvedCurve:
    """Collapse one :class:`CurveRef` into a :class:`ResolvedCurve`.

    ``role`` is one of ``"discount"`` / ``"projection"`` and is used
    in the per-role 404 codes — pure-ref entries that fail to resolve
    raise ``bond_discount_curve_not_found`` /
    ``bond_projection_curve_not_found``; inline entries that are
    incomplete raise the shared ``bond_curve_resolution_failed``
    422 with the role in ``details``.
    """

    if ref.id is not None and ref.points is None:
        row = await _fetch_curve(curve_id=ref.id, owner_uid=owner_uid, ro_engine=ro_engine)
        if row is None:
            if role == _ROLE_DISCOUNT:
                raise BondDiscountCurveNotFoundError(curve_id=ref.id)
            raise BondProjectionCurveNotFoundError(curve_id=ref.id)
        return _resolved_curve_from_row(row)

    if ref.name is None or ref.points is None:
        raise BondCurveResolutionFailedError(
            reason=(f"Inline {role} curve is missing ``name`` or ``points``."),
            details=[{"role": role}],
        )
    return ResolvedCurve(
        id=ref.id,
        name=ref.name,
        currency=ref.currency,
        day_counter=ref.day_counter,
        helper_kind=ref.helper_kind,
        reference_date=ref.reference_date,
        points=list(ref.points),
        body=dict(ref.body) if ref.body is not None else {},
    )


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
# Index resolution (floating-only)
# ---------------------------------------------------------------------------


async def _resolve_index(
    *,
    request: FloatingBondPriceRequest,
    bond_request: dict[str, Any],
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> tuple[ResolvedIndex, UUID | None]:
    """Resolve the projection index for a floating-rate bond.

    Source priority:

    1. ``request.index`` (explicit override or inline-mode payload).
    2. ``bond.pricing.index_id`` / ``bond.index_id`` (a few key
       variants — the portal's saved shape is in flux).

    Returns ``(index, index_id)``; ``index_id`` is ``None`` for
    purely-inline payloads.
    """

    if request.index is not None:
        return await _materialise_index_ref(
            ref=request.index, owner_uid=owner_uid, ro_engine=ro_engine
        )

    pricing = bond_request.get(_BOND_PRICING_KEY)
    if isinstance(pricing, dict):
        index_id = _extract_uuid(pricing, _BOND_INDEX_ID_KEYS)
        if index_id is not None:
            return await _materialise_index_ref(
                ref=IndexRef(id=index_id),
                owner_uid=owner_uid,
                ro_engine=ro_engine,
            )

    top_index_id = _extract_uuid(bond_request, _BOND_INDEX_ID_KEYS)
    if top_index_id is not None:
        return await _materialise_index_ref(
            ref=IndexRef(id=top_index_id),
            owner_uid=owner_uid,
            ro_engine=ro_engine,
        )

    raise BondCurveResolutionFailedError(
        reason=(
            "Floating bond is missing the projection index "
            "(``pricing.index_id`` / ``index_id``) and no inline "
            "``index`` override was supplied."
        ),
        details=[{"role": "index"}],
    )


async def _materialise_index_ref(
    *,
    ref: IndexRef,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> tuple[ResolvedIndex, UUID | None]:
    """Collapse one :class:`IndexRef` into a :class:`ResolvedIndex`."""

    if ref.id is not None and ref.body is None:
        row = await _fetch_index(index_id=ref.id, owner_uid=owner_uid, ro_engine=ro_engine)
        if row is None:
            raise BondIndexNotFoundError(index_id=ref.id)
        return _resolved_index_from_row(row), ref.id

    if ref.kind is None or ref.body is None:
        raise BondCurveResolutionFailedError(
            reason="Inline index is missing ``kind`` or ``body``.",
            details=[{"role": "index"}],
        )
    return (
        ResolvedIndex(
            id=ref.id,
            name=ref.name or "inline-index",
            kind=ref.kind,
            currency=ref.currency,
            calendar=ref.calendar,
            day_counter=ref.day_counter,
            body=dict(ref.body),
        ),
        ref.id,
    )


async def _fetch_index(
    *,
    index_id: UUID,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> dict[str, Any] | None:
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
# Snapshot (identical to swap_ir / swaption; deliberately duplicated —
# a shared module is a candidate future refactor)
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
        raise BondSnapshotNotFoundError(snapshot_id=snapshot_id)
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
    """Return a parsed UUID from the first matching key, or ``None``."""

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


def _coerce_bool(value: Any) -> bool:  # noqa: ANN401 -- runtime branch
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"true", "1", "yes"}
    return False


def collect_curves(
    *curves: ResolvedCurve | None,
) -> list[ResolvedCurve]:
    """Return the non-``None`` curves in the given order, de-duplicated by ``id``.

    Used by the MD-resolution walker so a floating bond with
    ``use_same_curve == True`` (same row in both roles) only walks
    each point list once.
    """

    seen_ids: set[UUID] = set()
    out: list[ResolvedCurve] = []
    for curve in curves:
        if curve is None:
            continue
        if curve.id is not None and curve.id in seen_ids:
            continue
        if curve.id is not None:
            seen_ids.add(curve.id)
        out.append(curve)
    return out


__all__ = [
    "FixedAssemblerOutput",
    "FloatingAssemblerOutput",
    "ResolvedSnapshot",
    "assemble_fixed",
    "assemble_floating",
    "collect_curves",
]
