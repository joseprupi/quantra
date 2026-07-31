"""``POST /v1/import`` — engine-format JSON document → Quantra entities.

The raw import endpoint: the caller POSTs a JSON document in
(approximately) the ENGINE's request format — the same shape as a
``/price-vanilla-swap`` request or any fragment of its ``pricing``
block — and the orchestrator creates the corresponding Quantra entities
(indices, curves, credit curves, vol surfaces, swaption models),
returning a per-item report of ok / skipped / errors. This is the
deterministic backbone a future LLM-extraction layer will target.

Request body::

    {
      "document": { ... },       # engine-format doc (full request / pricing / fragment)
      "dry_run": false,          # validate + translate + conflict-precheck, write nothing
      "on_conflict": "error"     # "error" (default) | "skip" — per-item behavior
    }

Response is HTTP 200 even when items fail (the MD-import policy): the
per-item report carries ``imported`` / ``skipped`` / ``errors`` /
``warnings`` / ``unsupported``; ``ok`` is true iff ``errors`` is empty.
Out-of-scope sections (trades, swap_indices, coupon_pricers,
inflation.*, equity.*) are reported per-item in the dedicated
``unsupported`` list — they are never silently dropped and do NOT flip
``ok`` (they are a documented scope boundary, not a failure of the
importable items).

Atomicity is PER-ITEM, not per-document: each entity is created in its
own transaction, so one failing item never aborts (or rolls back) the
rest. Names never overwrite: an existing live ``(owner, name)`` row is a
``name_conflict`` — an error under ``on_conflict=error``, a ``skipped``
entry under ``on_conflict=skip``.

Envelope-level failures only are 4xx with a stable code
(``import_invalid_request`` for an unparseable / empty document); a
missing DSN is the usual 503 ``storage_unavailable``. Every write
records provenance in the audit trail (``change_reason`` = the
``X-Change-Reason`` header when provided, else
``"imported via /v1/import"``).
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_common.auth.context import AuthContext
from quantra_orchestrator.auth.dependencies import get_auth_context
from quantra_orchestrator.data.engines import get_app_ro_engine, get_app_rw_engine
from quantra_orchestrator.data.errors import NameConflictError
from quantra_orchestrator.data.repository import CrudRepository, EntitySpec
from quantra_orchestrator.data.specs import (
    CREDIT_CURVES_SPEC,
    CURVES_SPEC,
    INDICES_SPEC,
    SWAPTION_MODELS_SPEC,
    VOL_SURFACES_SPEC,
)
from quantra_orchestrator.importer import (
    ENTITY_CREDIT_CURVE,
    ENTITY_CURVE,
    ENTITY_INDEX,
    ENTITY_SWAPTION_MODEL,
    ENTITY_VOL_SURFACE,
    EmptyDocumentError,
    MappedItem,
    map_document,
)

_log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["import"])

IMPORT_BAD_REQUEST_CODE: Final[str] = "import_invalid_request"
DEFAULT_CHANGE_REASON: Final[str] = "imported via /v1/import"

# Persist order: indices → curves → credit_curves → vol_surfaces →
# models (dependencies first). ``map_document`` already emits items in
# this order; the spec map here is the write-side lookup.
_SPEC_BY_ENTITY: Final[dict[str, EntitySpec]] = {
    ENTITY_INDEX: INDICES_SPEC,
    ENTITY_CURVE: CURVES_SPEC,
    ENTITY_CREDIT_CURVE: CREDIT_CURVES_SPEC,
    ENTITY_VOL_SURFACE: VOL_SURFACES_SPEC,
    ENTITY_SWAPTION_MODEL: SWAPTION_MODELS_SPEC,
}


class ImportBadRequestError(HTTPException):
    """400 for a hard, whole-request failure (unparseable / empty document).

    Pins the stable ``code`` so a client can distinguish a malformed
    envelope from a well-formed document that happened to contain bad
    items (the latter is a 200 with a non-empty ``errors`` list).
    """

    code: Final[str] = IMPORT_BAD_REQUEST_CODE

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


class ImportRequest(BaseModel):
    """The ``POST /v1/import`` body."""

    model_config = ConfigDict(extra="forbid")

    document: dict[str, Any] = Field(
        description=(
            "Engine-format JSON document: a full ``/price-*`` request, a "
            "bare ``pricing`` object, or a fragment carrying any of "
            "``curves`` / ``indices`` / ``credit_curves`` / "
            "``vol_surfaces`` / ``models``. Both nested "
            "(``pricing.rates.curves``) and legacy flat "
            "(``pricing.curves``) layouts are accepted."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "When true: run mapping + translator validation + the "
            "name-conflict precheck and report the outcome, but write "
            "NOTHING. Imported entries carry ``id: null``."
        ),
    )
    on_conflict: Literal["error", "skip"] = Field(
        default="error",
        description=(
            "Per-item behavior when a live entity with the same name "
            "already exists: ``error`` reports it in ``errors``; ``skip`` "
            "reports it in ``skipped``. Existing entities are NEVER "
            "overwritten."
        ),
    )


class ImportedItemOut(BaseModel):
    entity_type: str
    source_id: str = Field(description="The engine-side ``id`` the entity came from.")
    name: str = Field(description="The Quantra entity name (== the engine id).")
    id: str | None = Field(
        default=None,
        description="Created row UUID; null on dry_run.",
    )


class SkippedItemOut(BaseModel):
    entity_type: str
    source_id: str
    name: str
    reason: str = Field(description="Why the item was skipped (``name_conflict``).")


class ErrorItemOut(BaseModel):
    entity_type: str
    source_id: str
    path: str = Field(description="Document location, e.g. ``pricing.rates.curves[0]``.")
    reason: str


class WarningItemOut(BaseModel):
    entity_type: str
    source_id: str
    message: str


class UnsupportedItemOut(BaseModel):
    section: str
    source_id: str
    path: str
    reason: str


class ImportResponse(BaseModel):
    """The per-item import report (HTTP 200 even when items fail)."""

    ok: bool = Field(description="True iff ``errors`` is empty.")
    dry_run: bool
    imported: list[ImportedItemOut]
    skipped: list[SkippedItemOut]
    errors: list[ErrorItemOut]
    warnings: list[WarningItemOut]
    unsupported: list[UnsupportedItemOut]


async def _name_exists(
    ro_engine: AsyncEngine,
    spec: EntitySpec,
    *,
    owner_uid: str,
    name: str,
) -> bool:
    """True when a LIVE row already uses ``(owner_uid, name)`` (conflict precheck)."""

    sql = text(
        f"SELECT 1 AS present FROM app.{spec.table} "  # noqa: S608 -- table from the static spec registry
        "WHERE owner_uid = :owner_uid AND name = :name AND deleted_at IS NULL "
        "LIMIT 1"
    )
    async with ro_engine.connect() as conn:
        result = await conn.execute(sql, {"owner_uid": owner_uid, "name": name})
        return result.mappings().one_or_none() is not None


@router.post(
    "/import",
    response_model=ImportResponse,
    summary=(
        "Import engine-format entities (indices / curves / credit curves / vol surfaces / models)"
    ),
    responses={
        400: {"description": "Unparseable or empty document."},
        401: {"description": "Missing or invalid credentials."},
        503: {"description": "Persistent storage is not configured."},
    },
)
async def import_document(
    payload: ImportRequest,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    rw_engine: Annotated[AsyncEngine, Depends(get_app_rw_engine)],
    ro_engine: Annotated[AsyncEngine, Depends(get_app_ro_engine)],
    change_reason: Annotated[
        str | None,
        Header(
            alias="X-Change-Reason",
            description=(
                "Optional audit-trail reason recorded on every created "
                "entity's version row; defaults to "
                f"``{DEFAULT_CHANGE_REASON}``."
            ),
        ),
    ] = None,
) -> ImportResponse:
    """Map an engine-format document to Quantra entities and create them.

    Per-item atomicity (one transaction per entity; a failing item never
    aborts the rest), per-item conflict semantics (``on_conflict``), and
    a full report — see the module docstring for the complete contract.
    """

    try:
        mapped = map_document(payload.document)
    except EmptyDocumentError as exc:
        raise ImportBadRequestError(str(exc)) from exc

    reason = change_reason or DEFAULT_CHANGE_REASON
    imported: list[ImportedItemOut] = []
    skipped: list[SkippedItemOut] = []
    errors: list[ErrorItemOut] = [
        ErrorItemOut(entity_type=e.entity_type, source_id=e.source_id, path=e.path, reason=e.reason)
        for e in mapped.errors
    ]
    # Names already taken within THIS call, per entity type — a document
    # repeating an id conflicts with itself exactly like a DB row would.
    batch_names: dict[str, set[str]] = {entity: set() for entity in _SPEC_BY_ENTITY}

    for item in mapped.items:
        outcome = await _persist_item(
            item,
            spec=_SPEC_BY_ENTITY[item.entity_type],
            rw_engine=rw_engine,
            ro_engine=ro_engine,
            owner_uid=ctx.uid,
            actor_email=ctx.email,
            change_reason=reason,
            dry_run=payload.dry_run,
            on_conflict=payload.on_conflict,
            batch_names=batch_names[item.entity_type],
        )
        if isinstance(outcome, ImportedItemOut):
            imported.append(outcome)
        elif isinstance(outcome, SkippedItemOut):
            skipped.append(outcome)
        else:
            errors.append(outcome)

    _log.info(
        "orchestrator.import_document",
        uid=ctx.uid,
        dry_run=payload.dry_run,
        on_conflict=payload.on_conflict,
        imported=len(imported),
        skipped=len(skipped),
        errors=len(errors),
        unsupported=len(mapped.unsupported),
    )
    return ImportResponse(
        ok=not errors,
        dry_run=payload.dry_run,
        imported=imported,
        skipped=skipped,
        errors=errors,
        warnings=[
            WarningItemOut(entity_type=w.entity_type, source_id=w.source_id, message=w.message)
            for w in mapped.warnings
        ],
        unsupported=[
            UnsupportedItemOut(
                section=u.section, source_id=u.source_id, path=u.path, reason=u.reason
            )
            for u in mapped.unsupported
        ],
    )


async def _persist_item(
    item: MappedItem,
    *,
    spec: EntitySpec,
    rw_engine: AsyncEngine,
    ro_engine: AsyncEngine,
    owner_uid: str,
    actor_email: str | None,
    change_reason: str,
    dry_run: bool,
    on_conflict: str,
    batch_names: set[str],
) -> ImportedItemOut | SkippedItemOut | ErrorItemOut:
    """Create one mapped entity (or precheck it on dry_run) with per-item isolation."""

    name = str(item.values.get("name", ""))

    def _conflict() -> SkippedItemOut | ErrorItemOut:
        if on_conflict == "skip":
            return SkippedItemOut(
                entity_type=item.entity_type,
                source_id=item.source_id,
                name=name,
                reason="name_conflict",
            )
        return ErrorItemOut(
            entity_type=item.entity_type,
            source_id=item.source_id,
            path=item.path,
            reason=(
                f"name_conflict: a live {spec.entity} named {name!r} already "
                "exists (imports never overwrite; delete or rename it, or "
                'use on_conflict="skip").'
            ),
        )

    try:
        if name in batch_names or await _name_exists(
            ro_engine, spec, owner_uid=owner_uid, name=name
        ):
            return _conflict()

        if dry_run:
            batch_names.add(name)
            return ImportedItemOut(
                entity_type=item.entity_type, source_id=item.source_id, name=name, id=None
            )

        repo = CrudRepository(spec, rw_engine=rw_engine, ro_engine=ro_engine)
        row = await repo.create(
            owner_uid=owner_uid,
            values=item.values,
            actor_uid=owner_uid,
            actor_email=actor_email,
            change_reason=change_reason,
        )
    except NameConflictError:
        # Precheck race: another writer created the name between the
        # SELECT and the INSERT. Same per-item semantics as the precheck.
        return _conflict()
    except Exception as exc:  # per-item isolation: one bad item must not abort the batch
        _log.warning(
            "orchestrator.import_document.item_failed",
            entity_type=item.entity_type,
            source_id=item.source_id,
            error=str(exc),
        )
        return ErrorItemOut(
            entity_type=item.entity_type,
            source_id=item.source_id,
            path=item.path,
            reason=f"create failed: {exc}",
        )

    batch_names.add(name)
    return ImportedItemOut(
        entity_type=item.entity_type,
        source_id=item.source_id,
        name=name,
        id=str(row.get("id")) if row.get("id") is not None else None,
    )


__all__ = ["router"]
