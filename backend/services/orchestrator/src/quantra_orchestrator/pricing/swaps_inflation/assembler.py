"""Load + collapse the inflation-swap pricing request into an engine-ready shape.

Reads up to five ``app.*`` tables via the ``app_ro`` engine:

* ``app.swaps_inflation`` — the saved trade row (``swap_id`` path).
* ``app.curves`` — both the nominal discount curve and the
  inflation curve (one row per curve). The assembler enforces no
  ``kind`` invariant across the two curves; both are rates-shaped
 and the role lands in ``details`` on a 422.
* ``app.indices`` — the inflation index spec referenced by both
  the inflation curve and the swap body. The assembler enforces
  ``kind = 'Inflation'`` and raises
  :class:`SwapInflationIndexNotFoundError` on a wrong-kind row.
* ``app.snapshots`` — optional MD pin.

Writes nothing. Every read carries ``owner_uid = :owner_uid`` +
``deleted_at IS NULL`` so cross-tenant + soft-deleted rows are
invisible (north-star invariant #5).

The assembler's three jobs:

1. **Branch collapsing.** Either ``swap_id`` (by reference) or
   ``swap`` (inline) populates :class:`InflationSwapTrade`. Either
   ``curves`` / ``inflation_index`` from the request, or the saved
   swap's matching pricing-block refs, fill the resolved shapes.
   Cross-entity refs inside the saved swap's JSONB are soft.
2. **Three-bundle resolution.** Nominal discount curve, inflation
   curve, inflation index. Each bundle stage's failure surfaces as
   a distinct error code; the two curves share a single 422
   code with the role pinned in ``details``'s refinement.
3. **Snapshot pinning.** When ``snapshot_id`` is set, the
   :class:`ResolvedSnapshot` returned alongside the other outputs
   gives ``md_resolution`` a precomputed ``canonical_id → value``
   map. Quotes that aren't pinned fall through to the live MD
   service.

The output is everything the MD-resolution walker needs to do its
job; the assembler itself never touches the MD client or the
engine.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_orchestrator.pricing.swaps_inflation.errors import (
    SwapInflationCurveResolutionFailedError,
    SwapInflationIndexNotFoundError,
    SwapInflationIndexResolutionFailedError,
    SwapInflationInflationCurveNotFoundError,
    SwapInflationInvalidRequestError,
    SwapInflationNominalCurveNotFoundError,
    SwapInflationNotFoundError,
    SwapInflationSnapshotNotFoundError,
)
from quantra_orchestrator.pricing.swaps_inflation.models import (
    CurveRef,
    InflationIndexRef,
    InflationSwapPriceRequest,
    InflationSwapTrade,
    ResolvedCurve,
    ResolvedInflationIndex,
)

_PRICING_KEY = "pricing"
_CURVES_KEYS: tuple[str, ...] = ("curves",)
_INFLATION_INDEX_ID_KEYS: tuple[str, ...] = (
    "inflation_index_id",
    "inflationIndexId",
)
_INFLATION_INDEX_BLOCK_KEYS: tuple[str, ...] = (
    "inflation_index",
    "inflationIndex",
)
_INFLATION_BLOCK_KEYS: tuple[str, ...] = ("inflation",)
_CURVE_REF_ID_KEYS: tuple[str, ...] = ("curve_id", "curveId", "id")

_INFLATION_INDEX_KIND: str = "Inflation"


@dataclass(frozen=True, slots=True)
class ResolvedSnapshot:
    """A loaded ``app.snapshots`` row, projected for MD resolution."""

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

    ``trade`` packs the swap body. ``nominal_curve`` /
    ``inflation_curve`` are the resolved (no quote substitution
    yet) curves; ``inflation_index`` is the resolved index spec.
    ``nominal_curve_id`` / ``inflation_curve_id`` /
    ``inflation_index_id`` carry the logical bundle ids, echoed in
    the assembled request. ``swap_kind`` echoes the trade body's discriminator
    (``"zero_coupon"`` or ``"year_on_year"``) so ``engine_io`` can
    dispatch to the correct RPC. ``snapshot`` is the optional pin
    loaded from ``app.snapshots``.
    """

    trade: InflationSwapTrade
    nominal_curve: ResolvedCurve
    nominal_curve_id: UUID | None
    inflation_curve: ResolvedCurve
    inflation_curve_id: UUID | None
    inflation_index: ResolvedInflationIndex
    inflation_index_id: UUID | None
    swap_kind: str
    snapshot: ResolvedSnapshot | None


async def assemble(
    request: InflationSwapPriceRequest,
    *,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> AssemblerOutput:
    """Resolve the request into the inputs MD resolution + engine call need.

    Steps:

    1. Materialise the trade (load ``app.swaps_inflation`` or copy
       inline).
    2. Determine the source of curves (request override → saved
       swap's pricing.curves) and split into nominal / inflation
       by ``role`` or by position.
    3. Materialise each curve (load ``app.curves`` or copy inline).
    4. Materialise the inflation index (request override → saved
       swap's pricing.inflation block / pricing.inflation_index_id).
       Enforce ``kind = 'Inflation'``.
    5. Resolve the trade's ``swap_kind`` discriminator (request
       override → trade body → ``"zero_coupon"`` default).
    6. Optionally load the pinned ``app.snapshots`` row.
    """

    trade, swap_request = await _load_trade(
        request=request, owner_uid=owner_uid, ro_engine=ro_engine
    )

    nominal, nominal_id, inflation, inflation_id = await _resolve_curves(
        request=request,
        swap_request=swap_request,
        owner_uid=owner_uid,
        ro_engine=ro_engine,
    )

    index, index_id = await _resolve_inflation_index(
        request=request,
        swap_request=swap_request,
        owner_uid=owner_uid,
        ro_engine=ro_engine,
    )

    swap_kind = _resolve_swap_kind(request=request, swap_request=swap_request)

    snapshot = await _load_snapshot(
        snapshot_id=request.snapshot_id,
        owner_uid=owner_uid,
        ro_engine=ro_engine,
    )

    return AssemblerOutput(
        trade=trade,
        nominal_curve=nominal,
        nominal_curve_id=nominal_id,
        inflation_curve=inflation,
        inflation_curve_id=inflation_id,
        inflation_index=index,
        inflation_index_id=index_id,
        swap_kind=swap_kind,
        snapshot=snapshot,
    )


# ---------------------------------------------------------------------------
# Trade
# ---------------------------------------------------------------------------


async def _load_trade(
    *,
    request: InflationSwapPriceRequest,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> tuple[InflationSwapTrade, dict[str, Any]]:
    """Return ``(trade, raw_request_body)`` for the requested swap."""

    if request.swap is not None:
        return (
            InflationSwapTrade(
                swap_id=None,
                name=None,
                swap=dict(request.swap),
            ),
            dict(request.swap),
        )

    assert request.swap_id is not None  # nosec B101 - guaranteed by pydantic
    row = await _fetch_swap(
        owner_uid=owner_uid,
        swap_id=request.swap_id,
        ro_engine=ro_engine,
    )
    if row is None:
        raise SwapInflationNotFoundError(swap_id=request.swap_id)
    raw_request = row["request"] if isinstance(row.get("request"), dict) else {}
    trade = InflationSwapTrade(
        swap_id=request.swap_id,
        name=row.get("name"),
        swap=raw_request,
    )
    return trade, raw_request


async def _fetch_swap(
    *,
    owner_uid: str,
    swap_id: UUID,
    ro_engine: AsyncEngine,
) -> dict[str, Any] | None:
    """Load one live ``app.swaps_inflation`` row by ``(owner_uid, id)``."""

    sql = text(
        "SELECT id::text AS id, name, request "
        "FROM app.swaps_inflation "
        "WHERE id = :swap_id "
        "  AND owner_uid = :owner_uid "
        "  AND deleted_at IS NULL"
    )
    async with ro_engine.connect() as conn:
        result = await conn.execute(
            sql,
            {"swap_id": str(swap_id), "owner_uid": owner_uid},
        )
        row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Curves (nominal + inflation)
# ---------------------------------------------------------------------------


async def _resolve_curves(
    *,
    request: InflationSwapPriceRequest,
    swap_request: dict[str, Any],
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> tuple[ResolvedCurve, UUID | None, ResolvedCurve, UUID | None]:
    """Walk the request → saved-swap chain to produce two ResolvedCurves.

    Source priority for curves:

    1. ``request.curves`` (explicit override or inline-mode payload).
    2. ``swap.request.pricing.curves`` (per-curve refs or inline
       definitions baked into the saved swap).

    The first resolved curve is treated as the nominal discount
    curve; the second as the inflation curve. Curves may pin their
    role explicitly via the ``role`` field on the request body.

    Returns ``(nominal, nominal_id, inflation, inflation_id)``.
    """

    if request.curves is not None and len(request.curves) > 0:
        resolved = await _materialise_curve_refs(
            refs=request.curves, owner_uid=owner_uid, ro_engine=ro_engine
        )
    else:
        pricing = swap_request.get(_PRICING_KEY)
        if not isinstance(pricing, dict):
            raise SwapInflationCurveResolutionFailedError(
                reason=(
                    "Saved inflation swap is missing the ``pricing`` "
                    "block; cannot determine which curves to use. "
                    "Re-save the swap with a ``pricing.curves`` "
                    "list or pass an explicit ``curves`` override "
                    "in the pricing request."
                ),
                details=[{"role": "nominal"}],
            )
        inline_curves_raw = pricing.get(_CURVES_KEYS[0])
        if not isinstance(inline_curves_raw, list) or len(inline_curves_raw) == 0:
            raise SwapInflationCurveResolutionFailedError(
                reason=(
                    "Saved inflation swap has no ``pricing.curves`` "
                    "list; the orchestrator cannot infer which "
                    "curves to bootstrap. Re-save the swap or "
                    "pass an explicit ``curves`` override in the "
                    "pricing request."
                ),
                details=[{"role": "nominal"}],
            )
        refs = _refs_from_inline_curves(inline_curves_raw)
        resolved = await _materialise_curve_refs(
            refs=refs, owner_uid=owner_uid, ro_engine=ro_engine
        )

    nominal, inflation = _split_into_nominal_and_inflation(resolved)
    return (nominal, nominal.id, inflation, inflation.id)


def _split_into_nominal_and_inflation(
    resolved: list[ResolvedCurve],
) -> tuple[ResolvedCurve, ResolvedCurve]:
    """Bucket the resolved curves into ``(nominal, inflation)``.

    Honors an explicit ``role`` if pinned on at least one curve;
    otherwise treats the first as nominal and the second as
    inflation. Both roles must be populated — unlike equity options
    where the dividend curve has a flat-zero placeholder fallback,
    inflation swaps require both curves to be supplied (the
    inflation-leg projection has no canonical default).
    """

    by_role: dict[str, ResolvedCurve] = {}
    unrolled: list[ResolvedCurve] = []
    for curve in resolved:
        role = curve.role.lower()
        if role in {"nominal", "inflation"}:
            by_role[role] = curve
        else:
            unrolled.append(curve)

    if "nominal" in by_role:
        nominal = by_role["nominal"]
    elif unrolled:
        nominal = unrolled.pop(0).model_copy(update={"role": "nominal"})
    else:
        msg = (
            "Inflation-swap pricing requires a nominal discount curve; the resolved list was empty."
        )
        raise SwapInflationCurveResolutionFailedError(reason=msg, details=[{"role": "nominal"}])

    if "inflation" in by_role:
        inflation = by_role["inflation"]
    elif unrolled:
        inflation = unrolled.pop(0).model_copy(update={"role": "inflation"})
    else:
        msg = (
            "Inflation-swap pricing requires an inflation curve "
            "alongside the nominal discount curve; only one curve "
            "was supplied."
        )
        raise SwapInflationCurveResolutionFailedError(reason=msg, details=[{"role": "inflation"}])

    return nominal, inflation


def _refs_from_inline_curves(raw: list[Any]) -> list[CurveRef]:
    """Coerce a saved ``pricing.curves`` list into :class:`CurveRef` instances."""

    refs: list[CurveRef] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise SwapInflationCurveResolutionFailedError(
                reason=(f"Saved inflation swap ``pricing.curves[{index}]`` is not an object."),
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
            raise SwapInflationCurveResolutionFailedError(
                reason=(
                    f"Saved inflation swap "
                    f"``pricing.curves[{index}]`` is not a valid "
                    "CurveRef shape."
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
    """Collapse each :class:`CurveRef` into a :class:`ResolvedCurve`.

    Pure-ref entries hit ``app.curves`` via ``app_ro``. Inline
    entries skip the DB and project directly. Refs that fail to
    resolve raise the role-aware 404 (nominal vs. inflation) so
    operators get an actionable code without parsing strings.
    """

    resolved: list[ResolvedCurve] = []
    for index, ref in enumerate(refs):
        inferred_role = ref.role or ("nominal" if index == 0 else "inflation")
        if ref.id is not None and ref.points is None:
            row = await _fetch_curve(curve_id=ref.id, owner_uid=owner_uid, ro_engine=ro_engine)
            if row is None:
                if inferred_role.lower() == "inflation":
                    raise SwapInflationInflationCurveNotFoundError(curve_id=ref.id)
                raise SwapInflationNominalCurveNotFoundError(curve_id=ref.id)
            resolved.append(_resolved_curve_from_row(row, role=inferred_role))
            continue
        if ref.name is None or ref.points is None:
            raise SwapInflationCurveResolutionFailedError(
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
# Inflation index
# ---------------------------------------------------------------------------


async def _resolve_inflation_index(
    *,
    request: InflationSwapPriceRequest,
    swap_request: dict[str, Any],
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> tuple[ResolvedInflationIndex, UUID | None]:
    """Walk the request → saved-swap chain to produce a ResolvedInflationIndex.

    Source priority:

    1. ``request.inflation_index`` (explicit override / inline mode).
    2. ``swap.request.pricing.inflation_index`` block (mirrors the
       inline ref shape — ``{"id": "<uuid>"}`` or inline body).
    3. ``swap.request.pricing.inflation_index_id`` scalar (loaded
       via ``app.indices``).
    4. ``swap.request.pricing.inflation.inflation_indices[0]``
       (engine-canonical inline shape).
    """

    if request.inflation_index is not None:
        return await _materialise_index_ref(
            ref=request.inflation_index,
            owner_uid=owner_uid,
            ro_engine=ro_engine,
        )

    pricing = swap_request.get(_PRICING_KEY)
    if not isinstance(pricing, dict):
        raise SwapInflationIndexResolutionFailedError(
            reason=(
                "Saved inflation swap is missing the ``pricing`` "
                "block; cannot determine which inflation index "
                "to use."
            ),
        )

    block = _first_present(pricing, _INFLATION_INDEX_BLOCK_KEYS)
    if isinstance(block, dict):
        try:
            ref = InflationIndexRef.model_validate(block)
        except ValueError as exc:
            raise SwapInflationIndexResolutionFailedError(
                reason=(
                    "Saved inflation swap "
                    "``pricing.inflation_index`` is not a valid "
                    "InflationIndexRef shape."
                ),
                details=[{"error": str(exc)}],
            ) from exc
        return await _materialise_index_ref(ref=ref, owner_uid=owner_uid, ro_engine=ro_engine)

    scalar_id = _extract_uuid(pricing, _INFLATION_INDEX_ID_KEYS)
    if scalar_id is not None:
        return await _materialise_index_ref(
            ref=InflationIndexRef(id=scalar_id),
            owner_uid=owner_uid,
            ro_engine=ro_engine,
        )

    inflation_block = _first_present(pricing, _INFLATION_BLOCK_KEYS)
    if isinstance(inflation_block, dict):
        indices_raw = inflation_block.get("inflation_indices")
        if isinstance(indices_raw, list) and indices_raw:
            first = indices_raw[0]
            if isinstance(first, dict):
                return await _materialise_index_ref(
                    ref=_inline_index_ref_from_engine_shape(first),
                    owner_uid=owner_uid,
                    ro_engine=ro_engine,
                )

    raise SwapInflationIndexResolutionFailedError(
        reason=(
            "Saved inflation swap has no ``pricing.inflation_index`` "
            "block, no ``pricing.inflation_index_id`` scalar, and "
            "no ``pricing.inflation.inflation_indices[]`` list; "
            "the orchestrator cannot infer the inflation index."
        ),
    )


def _inline_index_ref_from_engine_shape(entry: dict[str, Any]) -> InflationIndexRef:
    """Coerce an engine-canonical ``inflation_indices[0]`` blob into a typed ref."""

    index_id_raw = entry.get("id") or entry.get("index_id")
    name_raw = entry.get("family_name") or entry.get("name")
    currency_raw = entry.get("currency")
    day_counter_raw = entry.get("day_counter")

    body = dict(entry)
    body.setdefault("id", index_id_raw)
    if name_raw is not None and "family_name" not in body:
        body["family_name"] = name_raw

    try:
        return InflationIndexRef(
            id=None,
            name=name_raw if isinstance(name_raw, str) else None,
            index_id=index_id_raw if isinstance(index_id_raw, str) else None,
            currency=currency_raw if isinstance(currency_raw, str) else None,
            day_counter=(day_counter_raw if isinstance(day_counter_raw, str) else None),
            body=body,
        )
    except ValueError as exc:
        raise SwapInflationIndexResolutionFailedError(
            reason=(
                "Saved inflation swap "
                "``pricing.inflation.inflation_indices[0]`` cannot be "
                "coerced into an InflationIndexRef."
            ),
            details=[{"error": str(exc)}],
        ) from exc


async def _materialise_index_ref(
    *,
    ref: InflationIndexRef,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> tuple[ResolvedInflationIndex, UUID | None]:
    """Collapse one :class:`InflationIndexRef` into a :class:`ResolvedInflationIndex`."""

    if ref.id is not None and ref.body is None:
        row = await _fetch_index(index_id=ref.id, owner_uid=owner_uid, ro_engine=ro_engine)
        if row is None:
            raise SwapInflationIndexNotFoundError(index_id=ref.id)
        actual_kind = str(row["kind"])
        if actual_kind != _INFLATION_INDEX_KIND:
            raise SwapInflationIndexNotFoundError(index_id=ref.id, actual_kind=actual_kind)
        return _resolved_index_from_row(row, ref=ref), ref.id

    if ref.name is None and ref.index_id is None:
        raise SwapInflationInvalidRequestError(
            reason=(
                "Inline inflation index is missing both ``name`` "
                "and ``index_id``; at least one is required."
            ),
        )
    body = dict(ref.body) if ref.body is not None else {}
    return (
        ResolvedInflationIndex(
            id=ref.id,
            name=ref.name or ref.index_id or "",
            index_id=ref.index_id or ref.name or "",
            currency=ref.currency,
            day_counter=ref.day_counter,
            body=body,
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
        "SELECT id::text AS id, name, kind, currency, day_counter, body "
        "FROM app.indices "
        "WHERE id = :index_id "
        "  AND owner_uid = :owner_uid "
        "  AND deleted_at IS NULL"
    )
    async with ro_engine.connect() as conn:
        result = await conn.execute(sql, {"index_id": str(index_id), "owner_uid": owner_uid})
        row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


def _resolved_index_from_row(
    row: dict[str, Any], *, ref: InflationIndexRef
) -> ResolvedInflationIndex:
    raw_body = row.get("body")
    body: dict[str, Any] = raw_body if isinstance(raw_body, dict) else {}
    name = str(row["name"])
    body_index_id_raw = body.get("id")
    body_index_id = (
        body_index_id_raw if isinstance(body_index_id_raw, str) and body_index_id_raw else None
    )
    index_id = ref.index_id or body_index_id or name
    return ResolvedInflationIndex(
        id=UUID(str(row["id"])),
        name=name,
        index_id=index_id,
        currency=row.get("currency"),
        day_counter=row.get("day_counter"),
        body=body,
    )


# ---------------------------------------------------------------------------
# Swap-kind discriminator
# ---------------------------------------------------------------------------


def _resolve_swap_kind(
    *,
    request: InflationSwapPriceRequest,
    swap_request: dict[str, Any],
) -> str:
    """Resolve the ``swap_kind`` discriminator for the engine RPC dispatch.

    Source priority:

    1. ``request.swap_kind`` (explicit override).
    2. Top-level ``swap_request.swap_kind``.
    3. Engine-canonical shape detection — if any nested key under
       ``swap_request.swaps[*]`` carries
       ``year_on_year_inflation_swap`` we treat the trade as YYIIS;
       similarly for ``zero_coupon_inflation_swap``.
    4. Default ``"zero_coupon"``.

    The endpoint dispatches to either
    :attr:`EngineRpc.PRICE_ZERO_COUPON_INFLATION_SWAP` or
    :attr:`EngineRpc.PRICE_YEAR_ON_YEAR_INFLATION_SWAP` based on
    this value (settled).
    """

    if request.swap_kind is not None:
        return request.swap_kind

    explicit = swap_request.get("swap_kind")
    if isinstance(explicit, str) and explicit:
        return explicit

    swaps_raw = swap_request.get("swaps")
    if isinstance(swaps_raw, list):
        for entry in swaps_raw:
            if not isinstance(entry, dict):
                continue
            if "year_on_year_inflation_swap" in entry:
                return "year_on_year"
            if "zero_coupon_inflation_swap" in entry:
                return "zero_coupon"

    return "zero_coupon"


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
        raise SwapInflationSnapshotNotFoundError(snapshot_id=snapshot_id)
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
