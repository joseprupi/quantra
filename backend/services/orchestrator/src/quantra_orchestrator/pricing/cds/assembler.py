"""Load + collapse the CDS pricing request into an engine-ready shape.

Reads ``app.cds`` / ``app.credit_curves`` / ``app.curves`` /
``app.snapshots`` via the ``app_ro`` engine. Writes nothing.

The CDS assembler is the first to walk **two distinct entity families**
in one resolution pass:

* The discount curve (``app.curves``) is loaded and projected the same
  way swap_ir / bonds do — quote-ID resolution happens downstream in
  :mod:`md_resolution`.
* The credit curve (``app.credit_curves``) is loaded and projected
  into a separate :class:`ResolvedCreditCurve` shape. Per the plan
  ("credit curves carry their own hazard/probability inputs (not
  vendor quotes)") the assembler refuses any credit-curve body that
  requires vendor MD resolution and raises
  ``cds_credit_curve_resolution_failed`` for that case rather
  than silently extending the MD walker.

The assembler's three jobs:

1. **Branch collapsing.** Either ``cds_id`` (by reference) or ``cds``
   (inline) populates the :class:`CdsTrade`. Either the route's
   ``curves`` / ``credit_curve_id`` / ``credit_curve`` overrides or
   the saved CDS body's ``pricing.discount_curve_id`` /
   ``pricing.credit_curve_id`` fields fill the two resolved-curve
   slots. Cross-entity refs inside the saved CDS's JSONB are soft
   : missing referents surface as the per-stage 404s
   (``cds_not_found`` / ``cds_credit_curve_not_found`` /
   ``cds_discount_curve_not_found``) rather than silently resolving
   to null.

2. **Per-bundle-type fail-fast.** Discount-curve failures raise
   :class:`CdsCurveResolutionFailedError`; credit-curve failures
   raise :class:`CdsCreditCurveResolutionFailedError`. The two
   codes are intentionally distinct + the refinement
   because the entity families are distinct, even though both surface
   as 422 with structured ``details``.

3. **Snapshot pinning.** When ``snapshot_id`` is set, the
   :class:`ResolvedSnapshot` returned alongside the other outputs
   gives :mod:`md_resolution` a precomputed ``canonical_id → value``
   map. Quotes that aren't pinned fall through to the live MD
   service. Credit curves do NOT consult snapshots; the pin
   only matters for the discount-curve walker.

This module deliberately duplicates structure from
``pricing/bonds/assembler.py`` for the discount-curve / snapshot
slices — a shared walker is a candidate future refactor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_orchestrator.pricing.cds.errors import (
    CdsCreditCurveNotFoundError,
    CdsCreditCurveResolutionFailedError,
    CdsCurveResolutionFailedError,
    CdsDiscountCurveNotFoundError,
    CdsNotFoundError,
    CdsSnapshotNotFoundError,
)
from quantra_orchestrator.pricing.cds.models import (
    CdsPriceRequest,
    CdsTrade,
    CreditCurveRef,
    CurveRef,
    ResolvedCreditCurve,
    ResolvedCurve,
)

_CDS_PRICING_KEY = "pricing"
_CDS_DISCOUNT_CURVE_ID_KEYS: tuple[str, ...] = (
    "discount_curve_id",
    "discountCurveId",
    "discounting_curve_id",
    "discountingCurveId",
)
_CDS_CREDIT_CURVE_ID_KEYS: tuple[str, ...] = (
    "credit_curve_id",
    "creditCurveId",
)
_CDS_CURVES_KEYS: tuple[str, ...] = ("curves",)
_CURVE_REF_ID_KEYS: tuple[str, ...] = ("curve_id", "curveId", "id")

_ROLE_DISCOUNT = "discount"
_ROLE_CREDIT = "credit"

# Credit-curve source markers we know how to consume today. Per the
# plan, ``flat`` and ``manual`` carry their own data (a flat hazard
# rate, or inline quotes with embedded values); ``quote_book`` would
# require an MD walker extension and is rejected with the per-stage
# 422 so the scope expansion is an explicit decision not a silent
# regression.
_SUPPORTED_CREDIT_CURVE_SOURCES: frozenset[str] = frozenset({"flat", "manual"})


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedSnapshot:
    """A loaded ``app.snapshots`` row, projected for MD resolution.

    Same shape (and same projection logic) as the swap_ir / bonds-
    side ``ResolvedSnapshot``; the MD-resolution walker consults
    ``pins`` first and only fires live MD calls for canonical IDs
    the pin doesn't cover. Credit curves never reach this walker.
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
class CdsAssemblerOutput:
    """Everything the CDS route needs to call MD walker + engine.

    ``trade`` packs the CDS body. ``discount_curve`` is the resolved
    discount curve (rates family); ``credit_curve`` is the resolved
    credit curve (credit family). ``credit_curve_id`` /
    ``discount_curve_id`` carry the logical bundle ids for
    ``shared_inputs`` (the reserved ``group_by_credit_curve``
    policy keys off ``credit_curve_id``; the discount-curve
    id is surfaced for log correlation + the assembled-request echo).
    ``snapshot`` is the optional MD pin.
    """

    trade: CdsTrade
    discount_curve: ResolvedCurve
    credit_curve: ResolvedCreditCurve
    discount_curve_id: UUID | None
    credit_curve_id: UUID | None
    snapshot: ResolvedSnapshot | None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def assemble(
    request: CdsPriceRequest,
    *,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> CdsAssemblerOutput:
    """Resolve a CDS pricing request.

    Steps:

    1. Materialise the trade (load ``app.cds`` or copy inline).
    2. Resolve the discount curve via the request → saved-CDS chain
       (request override → ``pricing.discount_curve_id``).
    3. Resolve the credit curve via the request → saved-CDS chain
       (``credit_curve_id`` ref override → ``credit_curve`` inline
       override → ``pricing.credit_curve_id``).
    4. Optionally load the pinned ``app.snapshots`` row.

    Every read goes through ``app_ro``. Soft references
    that resolve to ``None`` raise the appropriate per-stage 404
    (``cds_not_found`` / ``cds_discount_curve_not_found`` /
    ``cds_credit_curve_not_found`` / ``cds_snapshot_not_found``);
    shape failures raise the per-bundle-stage 422.
    """

    trade, cds_body = await _load_trade(request=request, owner_uid=owner_uid, ro_engine=ro_engine)

    discount_curve, discount_curve_id = await _resolve_discount_curve(
        request=request,
        cds_body=cds_body,
        owner_uid=owner_uid,
        ro_engine=ro_engine,
    )

    credit_curve, credit_curve_id = await _resolve_credit_curve(
        request=request,
        cds_body=cds_body,
        owner_uid=owner_uid,
        ro_engine=ro_engine,
    )

    snapshot = await _load_snapshot(
        snapshot_id=request.snapshot_id,
        owner_uid=owner_uid,
        ro_engine=ro_engine,
    )

    return CdsAssemblerOutput(
        trade=trade,
        discount_curve=discount_curve,
        credit_curve=credit_curve,
        discount_curve_id=discount_curve_id,
        credit_curve_id=credit_curve_id,
        snapshot=snapshot,
    )


# ---------------------------------------------------------------------------
# Trade loader
# ---------------------------------------------------------------------------


async def _load_trade(
    *,
    request: CdsPriceRequest,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> tuple[CdsTrade, dict[str, Any]]:
    """Return ``(trade, raw_request_body)`` for the requested CDS.

    The raw request body is returned alongside the typed trade so
    the curve-resolution steps can inspect the saved CDS's
    ``pricing.discount_curve_id`` / ``pricing.credit_curve_id`` /
    ``pricing.curves`` without re-loading.
    """

    if request.cds is not None:
        return (
            CdsTrade(cds_id=None, name=None, cds=dict(request.cds)),
            dict(request.cds),
        )

    assert request.cds_id is not None  # nosec B101 - guaranteed by pydantic
    row = await _fetch_cds_row(owner_uid=owner_uid, cds_id=request.cds_id, ro_engine=ro_engine)
    if row is None:
        raise CdsNotFoundError(cds_id=request.cds_id)
    raw_request = row["request"] if isinstance(row.get("request"), dict) else {}
    trade = CdsTrade(
        cds_id=request.cds_id,
        name=row.get("name"),
        cds=raw_request,
    )
    return trade, raw_request


async def _fetch_cds_row(
    *,
    owner_uid: str,
    cds_id: UUID,
    ro_engine: AsyncEngine,
) -> dict[str, Any] | None:
    """Load one live ``app.cds`` row by ``(owner_uid, id)``."""

    sql = text(
        "SELECT id::text AS id, name, request "
        "FROM app.cds "
        "WHERE id = :cds_id "
        "  AND owner_uid = :owner_uid "
        "  AND deleted_at IS NULL"
    )
    async with ro_engine.connect() as conn:
        result = await conn.execute(sql, {"cds_id": str(cds_id), "owner_uid": owner_uid})
        row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Discount-curve resolution (rates family — bundle stage "curve")
# ---------------------------------------------------------------------------


async def _resolve_discount_curve(
    *,
    request: CdsPriceRequest,
    cds_body: dict[str, Any],
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> tuple[ResolvedCurve, UUID | None]:
    """Resolve the single discount curve for a CDS trade.

    Source priority:

    1. ``request.curves[0]`` (explicit override or inline-mode
       payload). The CDS endpoint consumes exactly one discount
       curve; multi-entry lists collapse to the first entry.
    2. ``cds.pricing.discount_curve_id`` → ``app.curves`` row.
    3. ``cds.pricing.curves[0]`` (per-curve refs or inline
       definitions baked into the saved CDS).

    Returns ``(discount_curve, discount_curve_id)``. The id is
    ``None`` for purely-inline curves.
    """

    if request.curves is not None and len(request.curves) > 0:
        ref = request.curves[0]
        resolved = await _materialise_single_curve(
            ref=ref,
            owner_uid=owner_uid,
            ro_engine=ro_engine,
        )
        return resolved, ref.id

    pricing = cds_body.get(_CDS_PRICING_KEY)
    if not isinstance(pricing, dict):
        raise CdsCurveResolutionFailedError(
            reason=(
                "Saved CDS is missing the ``pricing`` block; cannot "
                "determine which discount curve to use. Re-save the "
                "CDS with a ``pricing.discount_curve_id`` field or "
                "pass an explicit ``curves`` override in the pricing "
                "request."
            ),
            details=[{"role": _ROLE_DISCOUNT, "cds_pricing": pricing}],
        )

    discount_id = _extract_uuid(pricing, _CDS_DISCOUNT_CURVE_ID_KEYS)
    if discount_id is not None:
        row = await _fetch_curve(curve_id=discount_id, owner_uid=owner_uid, ro_engine=ro_engine)
        if row is None:
            raise CdsDiscountCurveNotFoundError(curve_id=discount_id)
        return _resolved_curve_from_row(row), discount_id

    inline_curves_raw = pricing.get(_CDS_CURVES_KEYS[0])
    if not isinstance(inline_curves_raw, list) or len(inline_curves_raw) == 0:
        raise CdsCurveResolutionFailedError(
            reason=(
                "Saved CDS has no ``pricing.discount_curve_id`` and no "
                "``pricing.curves`` list; the orchestrator cannot infer "
                "which discount curve to use."
            ),
            details=[{"role": _ROLE_DISCOUNT}],
        )
    refs = _refs_from_inline_curves(inline_curves_raw)
    if not refs:
        raise CdsCurveResolutionFailedError(
            reason=(
                "Saved CDS ``pricing.curves`` collapsed to an empty "
                "ref list; cannot pick a discount curve."
            ),
            details=[{"role": _ROLE_DISCOUNT}],
        )
    first = refs[0]
    resolved = await _materialise_single_curve(
        ref=first,
        owner_uid=owner_uid,
        ro_engine=ro_engine,
    )
    return resolved, first.id


async def _materialise_single_curve(
    *,
    ref: CurveRef,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> ResolvedCurve:
    """Collapse one :class:`CurveRef` into a :class:`ResolvedCurve` (discount)."""

    if ref.id is not None and ref.points is None:
        row = await _fetch_curve(curve_id=ref.id, owner_uid=owner_uid, ro_engine=ro_engine)
        if row is None:
            raise CdsDiscountCurveNotFoundError(curve_id=ref.id)
        return _resolved_curve_from_row(row)

    if ref.name is None or ref.points is None:
        raise CdsCurveResolutionFailedError(
            reason="Inline discount curve is missing ``name`` or ``points``.",
            details=[{"role": _ROLE_DISCOUNT}],
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


def _refs_from_inline_curves(raw: list[Any]) -> list[CurveRef]:
    """Coerce a saved ``pricing.curves`` list into :class:`CurveRef` instances."""

    refs: list[CurveRef] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise CdsCurveResolutionFailedError(
                reason=(f"Saved CDS ``pricing.curves[{index}]`` is not an object."),
                details=[{"index": index, "role": _ROLE_DISCOUNT}],
            )
        candidate_id = _extract_uuid(entry, _CURVE_REF_ID_KEYS)
        if candidate_id is not None and not _has_inline_curve_payload(entry):
            refs.append(CurveRef(id=candidate_id))
            continue
        try:
            refs.append(CurveRef.model_validate(entry))
        except ValueError as exc:
            raise CdsCurveResolutionFailedError(
                reason=(f"Saved CDS ``pricing.curves[{index}]`` is not a valid CurveRef shape."),
                details=[{"index": index, "role": _ROLE_DISCOUNT, "error": str(exc)}],
            ) from exc
    return refs


def _has_inline_curve_payload(entry: dict[str, Any]) -> bool:
    """Decide whether an entry that *also* has an ``id`` should be inline."""

    keys = set(entry.keys())
    return bool(keys - {"id", "curve_id", "curveId"})


# ---------------------------------------------------------------------------
# Credit-curve resolution (credit family — bundle stage "credit_curve")
# ---------------------------------------------------------------------------


async def _resolve_credit_curve(
    *,
    request: CdsPriceRequest,
    cds_body: dict[str, Any],
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> tuple[ResolvedCreditCurve, UUID | None]:
    """Resolve the single credit curve for a CDS trade.

    Source priority:

    1. ``request.credit_curve_id`` (ref override) → ``app.credit_curves``
       row.
    2. ``request.credit_curve`` (inline override) — materialised as
       :class:`ResolvedCreditCurve` directly.
    3. ``cds.pricing.credit_curve_id`` → ``app.credit_curves`` row.

    Returns ``(credit_curve, credit_curve_id)``. The id is ``None``
    for purely-inline credit curves.
    """

    if request.credit_curve_id is not None:
        row = await _fetch_credit_curve(
            credit_curve_id=request.credit_curve_id,
            owner_uid=owner_uid,
            ro_engine=ro_engine,
        )
        if row is None:
            raise CdsCreditCurveNotFoundError(credit_curve_id=request.credit_curve_id)
        resolved = _resolved_credit_curve_from_row(row)
        _validate_credit_curve_payload(resolved)
        return resolved, request.credit_curve_id

    if request.credit_curve is not None:
        resolved = _materialise_inline_credit_curve(request.credit_curve)
        _validate_credit_curve_payload(resolved)
        return resolved, request.credit_curve.id

    pricing = cds_body.get(_CDS_PRICING_KEY)
    if not isinstance(pricing, dict):
        raise CdsCreditCurveResolutionFailedError(
            reason=(
                "Saved CDS is missing the ``pricing`` block; cannot "
                "determine which credit curve to use. Re-save the CDS "
                "with a ``pricing.credit_curve_id`` field or pass an "
                "explicit ``credit_curve_id`` / ``credit_curve`` "
                "override in the pricing request."
            ),
            details=[{"role": _ROLE_CREDIT, "cds_pricing": pricing}],
        )

    credit_id = _extract_uuid(pricing, _CDS_CREDIT_CURVE_ID_KEYS)
    if credit_id is None:
        raise CdsCreditCurveResolutionFailedError(
            reason=(
                "Saved CDS has no ``pricing.credit_curve_id``; the "
                "orchestrator cannot infer which credit curve to use."
            ),
            details=[{"role": _ROLE_CREDIT}],
        )
    row = await _fetch_credit_curve(
        credit_curve_id=credit_id, owner_uid=owner_uid, ro_engine=ro_engine
    )
    if row is None:
        raise CdsCreditCurveNotFoundError(credit_curve_id=credit_id)
    resolved = _resolved_credit_curve_from_row(row)
    _validate_credit_curve_payload(resolved)
    return resolved, credit_id


async def _fetch_credit_curve(
    *,
    credit_curve_id: UUID,
    owner_uid: str,
    ro_engine: AsyncEngine,
) -> dict[str, Any] | None:
    sql = text(
        "SELECT id::text AS id, name, reference_entity, currency, "
        "       seniority, source, recovery_rate, body "
        "FROM app.credit_curves "
        "WHERE id = :credit_curve_id "
        "  AND owner_uid = :owner_uid "
        "  AND deleted_at IS NULL"
    )
    async with ro_engine.connect() as conn:
        result = await conn.execute(
            sql,
            {"credit_curve_id": str(credit_curve_id), "owner_uid": owner_uid},
        )
        row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


def _resolved_credit_curve_from_row(row: dict[str, Any]) -> ResolvedCreditCurve:
    raw_body = row.get("body")
    body: dict[str, Any] = raw_body if isinstance(raw_body, dict) else {}
    return ResolvedCreditCurve(
        id=UUID(str(row["id"])),
        name=str(row["name"]),
        reference_entity=row.get("reference_entity"),
        currency=row.get("currency"),
        seniority=row.get("seniority"),
        source=row.get("source"),
        recovery_rate=float(row["recovery_rate"]),
        body=body,
    )


def _materialise_inline_credit_curve(
    ref: CreditCurveRef,
) -> ResolvedCreditCurve:
    """Collapse one inline :class:`CreditCurveRef` into a :class:`ResolvedCreditCurve`.

    Only used when the caller supplied ``request.credit_curve`` (i.e.
    no DB round-trip). The validator on :class:`CreditCurveRef` has
    already enforced that ``recovery_rate`` + ``body`` are present;
    we still defensively coerce so a missing key surfaces as the
    typed 422 rather than a ``KeyError`` higher up.
    """

    if ref.recovery_rate is None or ref.body is None:
        raise CdsCreditCurveResolutionFailedError(
            reason=("Inline credit curve is missing ``recovery_rate`` or ``body``."),
            details=[{"role": _ROLE_CREDIT}],
        )
    name = ref.name or ref.reference_entity or "inline-credit-curve"
    return ResolvedCreditCurve(
        id=ref.id,
        name=name,
        reference_entity=ref.reference_entity,
        currency=ref.currency,
        seniority=ref.seniority,
        source=ref.source,
        recovery_rate=float(ref.recovery_rate),
        body=dict(ref.body),
    )


def _validate_credit_curve_payload(credit_curve: ResolvedCreditCurve) -> None:
    """Refuse credit-curve bodies that would need vendor MD resolution.

    Per the plan: credit curves carry their own hazard / probability
    inputs and the MD walker is **discount-curve only**. We accept
    two valid body shapes:

    * ``{"flat_hazard_rate": <number>, ...}`` — the canonical "I
      already know the hazard rate" shape (also what the engine's
      ``cds_request.json`` example uses).
    * ``{"points": [{"quote_type": "...", "quoted_par_spread": ...,
      ...}], ...}`` with every point carrying an inline value (no
      ``quote_id``).

    Anything else (missing inputs / vendor refs / ``quote_book``
    source) raises ``cds_credit_curve_resolution_failed`` so the
    scope expansion to a credit-side MD walker remains an explicit
    decision, not a silent regression.
    """

    if (
        credit_curve.source is not None
        and credit_curve.source not in _SUPPORTED_CREDIT_CURVE_SOURCES
    ):
        raise CdsCreditCurveResolutionFailedError(
            reason=(
                f"Credit curve has source={credit_curve.source!r}; "
                "the orchestrator only consumes credit curves whose "
                "body carries inline hazard inputs "
                f"({sorted(_SUPPORTED_CREDIT_CURVE_SOURCES)}). Vendor "
                "MD resolution for credit curves is not supported — "
                "supply a flat hazard rate or inline quote values."
            ),
            details=[
                {
                    "role": _ROLE_CREDIT,
                    "credit_curve_id": (
                        str(credit_curve.id) if credit_curve.id is not None else None
                    ),
                    "source": credit_curve.source,
                }
            ],
        )

    body = credit_curve.body
    flat_hazard_rate = body.get("flat_hazard_rate")
    if isinstance(flat_hazard_rate, (int, float)):
        return

    points = body.get("points")
    if isinstance(points, list) and points:
        for index, point in enumerate(points):
            if not isinstance(point, dict):
                raise CdsCreditCurveResolutionFailedError(
                    reason=(f"Credit-curve point at index {index} is not an object."),
                    details=[{"role": _ROLE_CREDIT, "index": index}],
                )
            quote_id_raw = point.get("quote_id")
            if quote_id_raw:
                raise CdsCreditCurveResolutionFailedError(
                    reason=(
                        f"Credit-curve point at index {index} references "
                        "a vendor quote id; market-data resolution applies "
                        "to the discount curve only — supply inline values "
                        "on credit-curve points."
                    ),
                    details=[
                        {
                            "role": _ROLE_CREDIT,
                            "index": index,
                            "quote_id": quote_id_raw,
                        }
                    ],
                )
            inline_value = (
                point.get("quoted_par_spread")
                if isinstance(point.get("quoted_par_spread"), (int, float))
                else point.get("quoted_upfront")
            )
            if not isinstance(inline_value, (int, float)):
                raise CdsCreditCurveResolutionFailedError(
                    reason=(
                        f"Credit-curve point at index {index} is missing "
                        "an inline ``quoted_par_spread`` / "
                        "``quoted_upfront`` value."
                    ),
                    details=[{"role": _ROLE_CREDIT, "index": index}],
                )
        return

    raise CdsCreditCurveResolutionFailedError(
        reason=(
            "Credit-curve body has neither ``flat_hazard_rate`` nor a "
            "non-empty ``points`` list with inline values. The "
            "orchestrator cannot infer hazard inputs."
        ),
        details=[
            {
                "role": _ROLE_CREDIT,
                "credit_curve_id": (str(credit_curve.id) if credit_curve.id is not None else None),
            }
        ],
    )


# ---------------------------------------------------------------------------
# Snapshot (identical to swap_ir / bonds — deliberately duplicated;
# a shared module is a candidate future refactor).
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
        raise CdsSnapshotNotFoundError(snapshot_id=snapshot_id)
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


__all__ = [
    "CdsAssemblerOutput",
    "ResolvedSnapshot",
    "assemble",
]
