"""Load + collapse the FX-forward pricing request into an engine-ready shape.

Reads ``app.fx_forwards`` / ``app.curves`` / ``app.fx_spots`` / ``app.indices``
/ ``app.snapshots`` via ``app_ro``. Writes nothing. Same two jobs as the
swap_ir assembler: branch collapsing (ref vs inline) and snapshot pinning.

Source priority for each market-data list (curves / fx_spots / indices):

1. the request-level list (explicit override or inline-mode payload);
2. the saved trade's ``pricing.<list>`` (ref-or-inline entries).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_orchestrator.pricing.fx_forward.errors import (
    FxForwardCurveResolutionFailedError,
    FxForwardFxSpotResolutionFailedError,
    FxForwardIndexResolutionFailedError,
    FxForwardNotFoundError,
    FxForwardSnapshotNotFoundError,
)
from quantra_orchestrator.pricing.fx_forward.models import (
    CurveRef,
    FxForwardPriceRequest,
    FxForwardTrade,
    FxSpotRef,
    IndexRef,
    ResolvedCurve,
    ResolvedFxSpot,
    ResolvedIndex,
)
from quantra_orchestrator.pricing.swap_ir.assembler import ResolvedSnapshot

_PRICING_KEY = "pricing"
_CURVES_KEY = "curves"
_FX_SPOTS_KEY = "fx_spots"
_INDICES_KEY = "indices"
_REF_ID_KEYS: tuple[str, ...] = ("id", "curve_id", "curveId")


@dataclass(frozen=True, slots=True)
class AssemblerOutput:
    trade: FxForwardTrade
    curves: list[ResolvedCurve]
    fx_spots: list[ResolvedFxSpot]
    indices: list[ResolvedIndex] = field(default_factory=list)
    snapshot: ResolvedSnapshot | None = None


async def assemble(
    request: FxForwardPriceRequest,
    *,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> AssemblerOutput:
    trade, raw_request = await _load_trade(
        request=request, owner_uid=owner_uid, ro_engine=ro_engine
    )
    pricing = raw_request.get(_PRICING_KEY)
    pricing_block: dict[str, Any] = pricing if isinstance(pricing, dict) else {}

    curve_refs = request.curves or _curve_refs_from_saved(pricing_block)
    if not curve_refs:
        raise FxForwardCurveResolutionFailedError(
            reason=(
                "No curves: pass a non-empty ``curves`` list or save the FX forward with "
                "``pricing.curves`` (the base and quote discounting curves)."
            ),
        )
    spot_refs = request.fx_spots or _spot_refs_from_saved(pricing_block)
    if not spot_refs:
        raise FxForwardFxSpotResolutionFailedError(
            reason=(
                "No FX spot: pass a non-empty ``fx_spots`` list or save the FX forward with "
                "``pricing.fx_spots``."
            ),
        )
    index_refs = request.indices or _index_refs_from_saved(pricing_block)

    curves = await _materialise_curves(curve_refs, owner_uid=owner_uid, ro_engine=ro_engine)
    fx_spots = await _materialise_spots(spot_refs, owner_uid=owner_uid, ro_engine=ro_engine)
    indices = await _materialise_indices(index_refs, owner_uid=owner_uid, ro_engine=ro_engine)
    snapshot = await _load_snapshot(
        snapshot_id=request.snapshot_id, owner_uid=owner_uid, ro_engine=ro_engine
    )
    return AssemblerOutput(
        trade=trade, curves=curves, fx_spots=fx_spots, indices=indices, snapshot=snapshot
    )


# ---------------------------------------------------------------------------
# Trade
# ---------------------------------------------------------------------------


async def _load_trade(
    *, request: FxForwardPriceRequest, owner_uid: str, ro_engine: AsyncEngine
) -> tuple[FxForwardTrade, dict[str, Any]]:
    if request.fx_forward is not None:
        body = dict(request.fx_forward)
        return FxForwardTrade(fx_forward_id=None, name=None, fx_forward=body), body
    assert request.fx_forward_id is not None  # nosec B101 - guaranteed by pydantic
    sql = text(
        "SELECT id::text AS id, name, request FROM app.fx_forwards "
        "WHERE id = :fx_forward_id AND owner_uid = :owner_uid AND deleted_at IS NULL"
    )
    async with ro_engine.connect() as conn:
        result = await conn.execute(
            sql, {"fx_forward_id": str(request.fx_forward_id), "owner_uid": owner_uid}
        )
        row = result.mappings().one_or_none()
    if row is None:
        raise FxForwardNotFoundError(fx_forward_id=request.fx_forward_id)
    raw = row["request"] if isinstance(row.get("request"), dict) else {}
    return (
        FxForwardTrade(fx_forward_id=request.fx_forward_id, name=row.get("name"), fx_forward=raw),
        raw,
    )


# ---------------------------------------------------------------------------
# Saved-trade ``pricing`` lists → refs
# ---------------------------------------------------------------------------


def _extract_uuid(entry: dict[str, Any]) -> UUID | None:
    for key in _REF_ID_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value:
            try:
                return UUID(value)
            except ValueError:
                continue
        if isinstance(value, UUID):
            return value
    return None


def _curve_refs_from_saved(pricing: dict[str, Any]) -> list[CurveRef]:
    raw = pricing.get(_CURVES_KEY)
    if not isinstance(raw, list):
        return []
    refs: list[CurveRef] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        curve_id = _extract_uuid(entry)
        points = entry.get("points")
        if curve_id is not None and not isinstance(points, list):
            refs.append(CurveRef(id=curve_id))
            continue
        refs.append(
            CurveRef(
                id=curve_id,
                name=entry.get("name"),
                currency=entry.get("currency"),
                day_counter=entry.get("day_counter"),
                helper_kind=entry.get("helper_kind"),
                reference_date=entry.get("reference_date"),
                points=points if isinstance(points, list) else None,
                body=entry.get("body")
                if isinstance(entry.get("body"), dict)
                else {
                    k: entry[k]
                    for k in ("interpolator", "bootstrap_trait", "bootstrapTrait")
                    if k in entry
                },
            )
        )
    return refs


def _spot_refs_from_saved(pricing: dict[str, Any]) -> list[FxSpotRef]:
    raw = pricing.get(_FX_SPOTS_KEY)
    if not isinstance(raw, list):
        return []
    refs: list[FxSpotRef] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        spot_id = _extract_uuid(entry)
        if spot_id is not None and "base_currency" not in entry:
            refs.append(FxSpotRef(id=spot_id))
            continue
        refs.append(
            FxSpotRef(
                id=spot_id,
                name=entry.get("name") or (entry.get("id") if spot_id is None else None),
                base_currency=entry.get("base_currency"),
                quote_currency=entry.get("quote_currency"),
                rate=entry.get("rate"),
                quote_id=entry.get("quote_id"),
                spot_days=entry.get("spot_days"),
                settlement_calendars=entry.get("settlement_calendars"),
                business_day_convention=entry.get("business_day_convention"),
            )
        )
    return refs


def _index_refs_from_saved(pricing: dict[str, Any]) -> list[IndexRef]:
    raw = pricing.get(_INDICES_KEY)
    if not isinstance(raw, list):
        return []
    refs: list[IndexRef] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        index_id = _extract_uuid(entry)
        if index_id is not None and "body" not in entry:
            refs.append(IndexRef(id=index_id))
            continue
        refs.append(
            IndexRef(
                id=index_id,
                name=entry.get("name") or (entry.get("id") if index_id is None else None),
                kind=entry.get("kind") or entry.get("index_type"),
                currency=entry.get("currency"),
                calendar=entry.get("calendar"),
                day_counter=entry.get("day_counter"),
                body=entry.get("body") if isinstance(entry.get("body"), dict) else dict(entry),
            )
        )
    return refs


# ---------------------------------------------------------------------------
# Materialisation (ref → resolved)
# ---------------------------------------------------------------------------


async def _materialise_curves(
    refs: Iterable[CurveRef], *, owner_uid: str, ro_engine: AsyncEngine
) -> list[ResolvedCurve]:
    resolved: list[ResolvedCurve] = []
    for index, ref in enumerate(refs):
        if ref.id is not None and ref.points is None:
            row = await _fetch_row(
                "SELECT id::text AS id, name, currency, day_counter, helper_kind, "
                "       reference_date, points, body FROM app.curves "
                "WHERE id = :row_id AND owner_uid = :owner_uid AND deleted_at IS NULL",
                row_id=ref.id,
                owner_uid=owner_uid,
                ro_engine=ro_engine,
            )
            if row is None:
                raise FxForwardCurveResolutionFailedError(
                    reason=f"Curve {ref.id} is not owned by the caller or has been soft-deleted.",
                    details=[{"curve_id": str(ref.id), "index": index}],
                )
            raw_points = row.get("points")
            raw_body = row.get("body")
            resolved.append(
                ResolvedCurve(
                    id=UUID(str(row["id"])),
                    name=str(row["name"]),
                    currency=row.get("currency"),
                    day_counter=row.get("day_counter"),
                    helper_kind=row.get("helper_kind"),
                    reference_date=row.get("reference_date"),
                    points=[p for p in raw_points if isinstance(p, dict)]
                    if isinstance(raw_points, list)
                    else [],
                    body=raw_body if isinstance(raw_body, dict) else {},
                )
            )
            continue
        if ref.name is None or ref.points is None:
            raise FxForwardCurveResolutionFailedError(
                reason=f"Inline curve at index {index} is missing ``name`` or ``points``.",
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


async def _materialise_spots(
    refs: Iterable[FxSpotRef], *, owner_uid: str, ro_engine: AsyncEngine
) -> list[ResolvedFxSpot]:
    resolved: list[ResolvedFxSpot] = []
    for index, ref in enumerate(refs):
        if ref.id is not None and ref.base_currency is None:
            row = await _fetch_row(
                "SELECT id::text AS id, name, base_currency, quote_currency, rate, quote_id, "
                "       spot_days, business_day_convention, settlement_calendars "
                "FROM app.fx_spots "
                "WHERE id = :row_id AND owner_uid = :owner_uid AND deleted_at IS NULL",
                row_id=ref.id,
                owner_uid=owner_uid,
                ro_engine=ro_engine,
            )
            if row is None:
                raise FxForwardFxSpotResolutionFailedError(
                    reason=f"FX spot {ref.id} is not owned by the caller or has been soft-deleted.",
                    details=[{"fx_spot_id": str(ref.id), "index": index}],
                )
            calendars = row.get("settlement_calendars")
            resolved.append(
                ResolvedFxSpot(
                    id=UUID(str(row["id"])),
                    name=str(row["name"]),
                    base_currency=str(row["base_currency"]),
                    quote_currency=str(row["quote_currency"]),
                    rate=float(row["rate"]) if row.get("rate") is not None else None,
                    quote_id=row.get("quote_id"),
                    spot_days=int(row["spot_days"]),
                    settlement_calendars=[str(c) for c in calendars]
                    if isinstance(calendars, list)
                    else [],
                    business_day_convention=str(row["business_day_convention"]),
                )
            )
            continue
        missing = [
            f
            for f in (
                "base_currency",
                "quote_currency",
                "spot_days",
                "settlement_calendars",
                "business_day_convention",
            )
            if getattr(ref, f) is None
        ]
        if missing or (ref.name is None and ref.id is None):
            raise FxForwardFxSpotResolutionFailedError(
                reason=(
                    f"Inline FX spot at index {index} is missing required field(s): "
                    + ", ".join([*(["name"] if ref.name is None else []), *missing])
                    + "."
                ),
                details=[{"index": index, "missing": missing}],
            )
        resolved.append(
            ResolvedFxSpot(
                id=ref.id,
                name=ref.name or str(ref.id),
                base_currency=str(ref.base_currency),
                quote_currency=str(ref.quote_currency),
                rate=ref.rate,
                quote_id=ref.quote_id,
                spot_days=int(ref.spot_days or 0),
                settlement_calendars=list(ref.settlement_calendars or []),
                business_day_convention=str(ref.business_day_convention),
            )
        )
    return resolved


async def _materialise_indices(
    refs: Iterable[IndexRef], *, owner_uid: str, ro_engine: AsyncEngine
) -> list[ResolvedIndex]:
    resolved: list[ResolvedIndex] = []
    for index, ref in enumerate(refs):
        if ref.id is not None and ref.body is None:
            row = await _fetch_row(
                "SELECT id::text AS id, name, kind, currency, calendar, day_counter, body "
                "FROM app.indices "
                "WHERE id = :row_id AND owner_uid = :owner_uid AND deleted_at IS NULL",
                row_id=ref.id,
                owner_uid=owner_uid,
                ro_engine=ro_engine,
            )
            if row is None:
                raise FxForwardIndexResolutionFailedError(
                    reason=f"Index {ref.id} is not owned by the caller or has been soft-deleted.",
                    details=[{"index_id": str(ref.id), "index": index}],
                )
            raw_body = row.get("body")
            resolved.append(
                ResolvedIndex(
                    id=UUID(str(row["id"])),
                    name=str(row["name"]),
                    kind=str(row["kind"]),
                    currency=row.get("currency"),
                    calendar=row.get("calendar"),
                    day_counter=row.get("day_counter"),
                    body=raw_body if isinstance(raw_body, dict) else {},
                )
            )
            continue
        resolved.append(
            ResolvedIndex(
                id=ref.id,
                name=str(ref.name),
                kind=str(ref.kind),
                currency=ref.currency,
                calendar=ref.calendar,
                day_counter=ref.day_counter,
                body=dict(ref.body or {}),
            )
        )
    return resolved


async def _fetch_row(
    sql: str, *, row_id: UUID, owner_uid: str, ro_engine: AsyncEngine
) -> dict[str, Any] | None:
    async with ro_engine.connect() as conn:
        result = await conn.execute(text(sql), {"row_id": str(row_id), "owner_uid": owner_uid})
        row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Snapshot (same projection as swap_ir; duplicated per the product convention)
# ---------------------------------------------------------------------------


async def _load_snapshot(
    *, snapshot_id: UUID | None, owner_uid: str, ro_engine: AsyncEngine
) -> ResolvedSnapshot | None:
    if snapshot_id is None:
        return None
    row = await _fetch_row(
        "SELECT id::text AS id, name, content FROM app.snapshots "
        "WHERE id = :row_id AND owner_uid = :owner_uid AND deleted_at IS NULL",
        row_id=snapshot_id,
        owner_uid=owner_uid,
        ro_engine=ro_engine,
    )
    if row is None:
        raise FxForwardSnapshotNotFoundError(snapshot_id=snapshot_id)
    content = row.get("content") if isinstance(row.get("content"), dict) else {}
    pins: dict[str, dict[str, Any]] = {}
    raw_quotes = content.get("quotes") if isinstance(content, dict) else None
    if isinstance(raw_quotes, dict):
        for cid, entry in raw_quotes.items():
            if isinstance(entry, dict) and entry.get("value") is not None:
                pins[str(cid)] = dict(entry)
            elif isinstance(entry, (int, float)) and not isinstance(entry, bool):
                pins[str(cid)] = {"value": float(entry)}
    elif isinstance(raw_quotes, list):
        for entry in raw_quotes:
            if (
                isinstance(entry, dict)
                and entry.get("canonical_id")
                and entry.get("value") is not None
            ):
                pins[str(entry["canonical_id"])] = dict(entry)
    version_etag = content.get("md_version_etag") if isinstance(content, dict) else None
    return ResolvedSnapshot(
        id=UUID(str(row["id"])),
        name=str(row["name"]),
        pins=pins,
        version_etag=version_etag if isinstance(version_etag, str) and version_etag else None,
    )


__all__ = ["AssemblerOutput", "assemble"]
