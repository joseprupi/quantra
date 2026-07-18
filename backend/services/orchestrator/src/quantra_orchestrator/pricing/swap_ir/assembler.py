"""Load + collapse the IR-swap pricing request into an engine-ready shape.

Reads ``app.swaps_ir`` / ``app.curves`` / ``app.curve_sets`` /
``app.snapshots`` via the ``app_ro`` engine. Writes nothing.

The assembler's two jobs:

1. **Branch collapsing.** Either ``swap_id`` (by reference) or
   ``swap`` (inline) populates :class:`IrSwapTrade`. Either
   ``curves[]`` from the request, ``curve_refs[]`` from the saved
   swap's curve set, or the saved swap's own ``pricing.curves``
   array fills the :class:`ResolvedCurve` list. Cross-entity refs
   inside the saved swap's JSONB are soft: missing referents
   surface as ``swap_ir_curve_set_not_found`` /
   ``swap_ir_curve_resolution_failed`` rather than silently
   resolving to null.

2. **Snapshot pinning.** When ``snapshot_id`` is set, the
   :class:`ResolvedSnapshot` returned alongside the other outputs
   gives ``md_resolution`` a precomputed ``canonical_id → value``
   map. Quotes that aren't pinned fall through to the live MD
   service.

The output is everything the MD-resolution walker needs to do its
job; the assembler itself never touches the MD client or the engine.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_orchestrator.pricing._translator import CurveRole, resolved_curve_id
from quantra_orchestrator.pricing.swap_ir.errors import (
    SwapIrCurveResolutionFailedError,
    SwapIrCurveSetNotFoundError,
    SwapIrNotFoundError,
    SwapIrSnapshotNotFoundError,
)
from quantra_orchestrator.pricing.swap_ir.models import (
    CurveRef,
    IrSwapPriceRequest,
    IrSwapTrade,
    ResolvedCurve,
    ResolvedIndex,
)

# Default key inside the saved-swap JSONB where the portal stores the
# pinned curve set (mentions ``curve_sets.body.curve_refs[*].curve_id``
# as the soft-ref shape, so the swap-side pointer is by ``curve_set_id``
# at the same level as ``pricing``). Falls back to per-curve ``id`` refs
# under ``pricing.curves[*].id`` when no curve set is pinned.
_SWAP_PRICING_KEY = "pricing"
_SWAP_CURVE_SET_KEYS: tuple[str, ...] = ("curve_set_id", "curveSetId")
_SWAP_CURVES_KEYS: tuple[str, ...] = ("curves",)
_CURVE_SET_BODY_REFS_KEYS: tuple[str, ...] = ("curve_refs", "curveRefs")
_CURVE_REF_ID_KEYS: tuple[str, ...] = ("curve_id", "curveId", "id")
_SWAP_INDICES_KEYS: tuple[str, ...] = ("indices", "swap_indices", "swapIndices")
_SWAP_INDEX_KEYS: tuple[str, ...] = ("index",)
_INDEX_KIND_KEYS: tuple[str, ...] = ("kind", "index_type", "indexType")
_INDEX_NAME_KEYS: tuple[str, ...] = ("name", "family_name", "index_name")


@dataclass(frozen=True, slots=True)
class ResolvedSnapshot:
    """A loaded ``app.snapshots`` row, projected for MD resolution.

    ``pins`` is a ``canonical_id → (value, source)`` map built from
    the ``content`` JSONB. The MD resolution walker consults it
    first; live MD lookups only fire for canonical IDs the pin
    doesn't cover. Stored as a plain mapping (rather than a Pydantic
    model) because nothing leaves the orchestrator with this shape
    — it's an internal intermediate.
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

    ``trade`` packs the swap body. ``curves`` is the resolved (no
    quote substitution yet) curve list. ``curve_set_id`` carries the
    logical bundle id for the concurrency seam's ``shared_inputs``.
    ``index`` is the resolved float-leg index — ``None`` when the saved
    swap carries no inline forwarding index, in which case the translator emits
    the interim default Euribor-6M index. ``snapshot`` is the optional pin
    loaded from ``app.snapshots``.
    """

    trade: IrSwapTrade
    curves: list[ResolvedCurve]
    curve_set_id: UUID | None
    snapshot: ResolvedSnapshot | None
    index: ResolvedIndex | None = None

    def curve_roles(self) -> dict[CurveRole, str]:
        """Tag the resolved curves with their engine reference role.

        swap_ir's curve list is discount-first; the forwarding role reuses the
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
    request: IrSwapPriceRequest,
    *,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> AssemblerOutput:
    """Resolve the request into the inputs MD resolution + engine call need.

    Steps:

    1. Materialise the trade (load ``app.swaps_ir`` or copy inline).
    2. Determine the source of curves (request override, saved
       swap's pinned curve set, saved swap's pricing.curves list).
    3. Materialise each curve (load ``app.curves`` or copy inline).
    4. Optionally load the pinned ``app.snapshots`` row.

    Every read goes through ``app_ro``. Soft references
    that resolve to ``None`` raise the appropriate
    ``swap_ir_*_not_found`` / ``swap_ir_curve_resolution_failed``
    so the failure surfaces as a 4xx with a stable code rather
    than a 500.
    """

    trade, swap_request = await _load_trade(
        request=request, owner_uid=owner_uid, ro_engine=ro_engine
    )

    curves, curve_set_id = await _resolve_curves(
        request=request,
        swap_request=swap_request,
        owner_uid=owner_uid,
        ro_engine=ro_engine,
    )

    snapshot = await _load_snapshot(
        snapshot_id=request.snapshot_id,
        owner_uid=owner_uid,
        ro_engine=ro_engine,
    )

    index = _resolve_index(swap_request)

    return AssemblerOutput(
        trade=trade,
        curves=curves,
        curve_set_id=curve_set_id,
        snapshot=snapshot,
        index=index,
    )


def _resolve_index(swap_request: dict[str, Any]) -> ResolvedIndex | None:
    """Resolve the swap's float-leg forwarding index from the saved-swap body.

    Reads the inline forwarding-index the portal's fat ``pricing`` block carries
    (``pricing.indices[0]`` / ``pricing.index``). The index body is pure
    config (not MD-resolved); the translator builds a faithful ``IndexDef`` from
    it. Returns ``None`` when no inline index is present — the translator
    then emits the documented interim default Euribor-6M forwarding index, so a
    saved swap that predates the index field keeps pricing unchanged.
    """

    pricing = swap_request.get(_SWAP_PRICING_KEY)
    if not isinstance(pricing, dict):
        return None
    raw = _first_present(pricing, _SWAP_INDICES_KEYS)
    entry: Any = None
    if isinstance(raw, list) and raw:
        entry = raw[0]
    elif isinstance(raw, dict):
        entry = raw
    if entry is None:
        entry = _first_present(pricing, _SWAP_INDEX_KEYS)
    if not isinstance(entry, dict):
        return None
    return _resolved_index_from_inline(entry)


def _resolved_index_from_inline(entry: dict[str, Any]) -> ResolvedIndex:
    """Project one inline index dict into a :class:`ResolvedIndex`."""

    index_id = _extract_uuid(entry, _CURVE_REF_ID_KEYS)
    name = _first_str(entry, _INDEX_NAME_KEYS) or "forwarding-index"
    kind = _first_str(entry, _INDEX_KIND_KEYS) or "IborIndex"
    return ResolvedIndex(
        id=index_id,
        name=name,
        kind=kind,
        currency=_first_str(entry, ("currency",)),
        calendar=_first_str(entry, ("calendar",)),
        day_counter=_first_str(entry, ("day_counter", "dayCounter")),
        body=dict(entry),
    )


async def _load_trade(
    *,
    request: IrSwapPriceRequest,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> tuple[IrSwapTrade, dict[str, Any]]:
    """Return ``(trade, raw_request_body)`` for the requested swap.

    The raw request body is returned alongside the typed trade so
    the curve-resolution step can inspect the saved swap's
    ``pricing.curves`` / ``pricing.curve_set_id`` without re-loading.
    """

    if request.swap is not None:
        return (
            IrSwapTrade(swap_id=None, name=None, swap=dict(request.swap)),
            dict(request.swap),
        )

    assert request.swap_id is not None  # nosec B101 - guaranteed by pydantic
    row = await _fetch_swap(
        owner_uid=owner_uid,
        swap_id=request.swap_id,
        ro_engine=ro_engine,
    )
    if row is None:
        raise SwapIrNotFoundError(swap_id=request.swap_id)
    raw_request = row["request"] if isinstance(row.get("request"), dict) else {}
    trade = IrSwapTrade(
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
    """Load one live ``app.swaps_ir`` row by ``(owner_uid, id)`` or return ``None``.

    Soft-deleted rows are invisible (``deleted_at IS NULL``); cross-
    tenant rows are invisible (``owner_uid = :owner_uid``). The
    privilege envelope is the same one ``CrudRepository`` already
    enforces; we re-state the predicate here so the assembler stays
    a single, audit-friendly SQL site for the swap_ir read path.
    """

    sql = text(
        "SELECT id::text AS id, name, request "
        "FROM app.swaps_ir "
        "WHERE id = :swap_id "
        "  AND owner_uid = :owner_uid "
        "  AND deleted_at IS NULL"
    )
    async with ro_engine.connect() as conn:
        result = await conn.execute(sql, {"swap_id": str(swap_id), "owner_uid": owner_uid})
        row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


async def _resolve_curves(
    *,
    request: IrSwapPriceRequest,
    swap_request: dict[str, Any],
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> tuple[list[ResolvedCurve], UUID | None]:
    """Walk the request → saved-swap → curve-set chain to produce ResolvedCurves.

    Source priority:

    1. ``request.curves`` (explicit override or inline-mode payload).
    2. ``swap.request.pricing.curve_set_id`` → ``app.curve_sets`` row.
    3. ``swap.request.pricing.curves`` (per-curve refs or inline
       definitions baked into the saved swap).

    Returns ``(curves, curve_set_id)``; ``curve_set_id`` populates
    the ``shared_inputs`` so the future ``GroupByCurveSet``
    policy has a key to group by.
    """

    if request.curves is not None and len(request.curves) > 0:
        # Explicit override path — caller has already declared the
        # exact curve list. We materialise each ref and stop.
        return (
            await _materialise_refs(refs=request.curves, owner_uid=owner_uid, ro_engine=ro_engine),
            None,
        )

    pricing = swap_request.get(_SWAP_PRICING_KEY)
    if not isinstance(pricing, dict):
        raise SwapIrCurveResolutionFailedError(
            reason=(
                "Saved swap is missing the ``pricing`` block; cannot "
                "determine which curves to use. Re-save the swap with a "
                "``pricing.curves`` list or pass an explicit ``curves`` "
                "override in the pricing request."
            ),
            details=[{"swap_pricing": pricing}],
        )

    curve_set_id = _extract_uuid(pricing, _SWAP_CURVE_SET_KEYS)
    if curve_set_id is not None:
        refs = await _refs_from_curve_set(
            curve_set_id=curve_set_id,
            owner_uid=owner_uid,
            ro_engine=ro_engine,
        )
        return (
            await _materialise_refs(refs=refs, owner_uid=owner_uid, ro_engine=ro_engine),
            curve_set_id,
        )

    inline_curves_raw = pricing.get(_SWAP_CURVES_KEYS[0])
    if not isinstance(inline_curves_raw, list) or len(inline_curves_raw) == 0:
        raise SwapIrCurveResolutionFailedError(
            reason=(
                "Saved swap has no ``pricing.curve_set_id`` and no "
                "``pricing.curves`` list; the orchestrator cannot infer "
                "which curves to bootstrap. Re-save the swap with one of "
                "those fields populated or pass an explicit ``curves`` "
                "override in the pricing request."
            ),
        )
    refs = _refs_from_inline_curves(inline_curves_raw)
    return (
        await _materialise_refs(refs=refs, owner_uid=owner_uid, ro_engine=ro_engine),
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
            sql,
            {"curve_set_id": str(curve_set_id), "owner_uid": owner_uid},
        )
        row = result.mappings().one_or_none()
    if row is None:
        raise SwapIrCurveSetNotFoundError(curve_set_id=curve_set_id)
    raw_body = row.get("body")
    body: dict[str, Any] = raw_body if isinstance(raw_body, dict) else {}
    refs_raw = _first_present(body, _CURVE_SET_BODY_REFS_KEYS) or []
    if not isinstance(refs_raw, list):
        raise SwapIrCurveResolutionFailedError(
            reason=(f"Curve set {curve_set_id} has no ``curve_refs`` array in its body."),
            details=[{"curve_set_id": str(curve_set_id)}],
        )
    refs: list[CurveRef] = []
    for index, entry in enumerate(refs_raw):
        if not isinstance(entry, dict):
            raise SwapIrCurveResolutionFailedError(
                reason=(
                    f"Curve set {curve_set_id} has a non-object entry at "
                    f"index {index} of ``curve_refs``."
                ),
                details=[{"curve_set_id": str(curve_set_id), "index": index}],
            )
        curve_id = _extract_uuid(entry, _CURVE_REF_ID_KEYS)
        if curve_id is None:
            raise SwapIrCurveResolutionFailedError(
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
    """Coerce a saved ``pricing.curves`` list into :class:`CurveRef` instances.

    Each entry is either a ref (``{"id": "<uuid>"}``) or an inline
    curve definition. The router's pydantic layer would normally do
    this for request.curves, but ``request.curves`` is None on the
    "load from saved swap" path so we re-do the coercion here.
    Failures here surface as ``swap_ir_curve_resolution_failed``
    rather than ``validation_error`` because the offending data
    lives in the saved swap, not the inbound request.
    """

    refs: list[CurveRef] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise SwapIrCurveResolutionFailedError(
                reason=(f"Saved swap ``pricing.curves[{index}]`` is not an object."),
                details=[{"index": index}],
            )
        candidate_id = _extract_uuid(entry, _CURVE_REF_ID_KEYS)
        if candidate_id is not None and not _has_inline_curve_payload(entry):
            refs.append(CurveRef(id=candidate_id))
            continue
        # Inline definition — let pydantic validate the fields that
        # matter (name, points). Bubble the validation as a
        # resolution failure so the code stays product-scoped.
        try:
            refs.append(CurveRef.model_validate(entry))
        except ValueError as exc:
            raise SwapIrCurveResolutionFailedError(
                reason=(f"Saved swap ``pricing.curves[{index}]`` is not a valid CurveRef shape."),
                details=[{"index": index, "error": str(exc)}],
            ) from exc
    return refs


def _has_inline_curve_payload(entry: dict[str, Any]) -> bool:
    """Decide whether an entry that *also* has an ``id`` should be treated as inline.

    A saved row may carry both a stable ``id`` and an inline
    ``points`` / ``body`` (e.g. when the portal cloned a curve into
    the swap snapshot). In that case the inline copy wins because
    the original ``app.curves`` row may have moved on. The plain
    ``{"id": "..."}`` shape — no other keys — is the pure-ref case.
    """

    keys = set(entry.keys())
    return bool(keys - {"id", "curve_id", "curveId"})


async def _materialise_refs(
    *,
    refs: Iterable[CurveRef],
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> list[ResolvedCurve]:
    """Collapse each :class:`CurveRef` into a :class:`ResolvedCurve`.

    Pure-ref entries (``id`` set, no inline fields) hit ``app.curves``
    via ``app_ro``. Inline entries skip the DB and project directly.
    Refs that fail to resolve (row missing / soft-deleted / cross-
    tenant) raise ``swap_ir_curve_resolution_failed``.
    """

    resolved: list[ResolvedCurve] = []
    for index, ref in enumerate(refs):
        if ref.id is not None and ref.points is None:
            row = await _fetch_curve(curve_id=ref.id, owner_uid=owner_uid, ro_engine=ro_engine)
            if row is None:
                raise SwapIrCurveResolutionFailedError(
                    reason=(f"Curve {ref.id} is not owned by the caller or has been soft-deleted."),
                    details=[{"curve_id": str(ref.id), "index": index}],
                )
            resolved.append(_resolved_curve_from_row(row))
            continue
        # Inline (or hybrid id+inline) — use the inline values.
        if ref.name is None or ref.points is None:
            raise SwapIrCurveResolutionFailedError(
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


async def _load_snapshot(
    *,
    snapshot_id: UUID | None,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> ResolvedSnapshot | None:
    """Load and project an ``app.snapshots`` row when ``snapshot_id`` is set."""

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
        result = await conn.execute(
            sql,
            {"snapshot_id": str(snapshot_id), "owner_uid": owner_uid},
        )
        row = result.mappings().one_or_none()
    if row is None:
        raise SwapIrSnapshotNotFoundError(snapshot_id=snapshot_id)
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
    """Project an ``app.snapshots.content`` JSONB into a ``canonical_id → pin`` map.

    The shape isn't tightened yet (per migration 0004 the comment
    is literally "typically a {quote_id -> value or {quote_id,
    value}} shape"). We tolerate three observed shapes:

    * ``{"USD.IRS.10Y": 4.25}`` — bare value.
    * ``{"USD.IRS.10Y": {"value": 4.25, "source": "vendor_x"}}`` — value + metadata.
    * ``{"quotes": [{"canonical_id": "...", "value": ...}, ...]}`` — list-of-objects.

    Anything else is silently skipped (the missing pins fall
    through to the live MD path, which then either succeeds or
    surfaces ``quote_not_found``).
    """

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


def _first_str(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """Return the first key whose value is a non-empty string, else ``None``."""

    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


__all__ = [
    "AssemblerOutput",
    "ResolvedSnapshot",
    "assemble",
]
