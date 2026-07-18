"""Pydantic v2 request / response models for the orchestrator data layer.

Two layers:

1. **Generic envelopes.** ``ListPage`` (paged list response) and
   ``EntityTimestamps`` (the trigger-maintained fields every named
   entity carries) are shared across all entities.
2. **Per-entity schemas.** ``CreateXxx`` / ``UpdateXxx`` / ``XxxResponse``
   models, one set per table. The JSONB columns are typed as
   ``dict[str, Any]`` (or ``list`` for ``curves.points``) — the database
   accepts any JSON object / array; per-product code owns the
   inner-shape tightening where query shapes are known.

``model_config["from_attributes"] = True`` is not used: the repository
returns ``dict[str, Any]`` rows directly so we hand the dict to
``Model.model_validate(...)``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# Generic type parameter for the ``ListPage`` envelope. Declared with the
# classic ``TypeVar`` + ``Generic[T]`` spelling rather than the PEP-695
# ``class ListPage[T]`` form: Nuitka (module-compile of this package for the
# source-protected image) materializes a PEP-695 type parameter as a real
# ``T = T`` entry in the class namespace, which pydantic v2 then rejects as a
# "non-annotated attribute" field. The two spellings are semantically
# identical for pydantic/FastAPI (same generic model, same OpenAPI); this
# purely syntactic change moves no request/response shape and no pricing
# number.
T = TypeVar("T")

# ---------------------------------------------------------------------------
# Generic envelopes
# ---------------------------------------------------------------------------


class PageMeta(BaseModel):
    """Pagination metadata co-rendered with every list response.

    Offset pagination. ``has_more`` is computed by the route
    handler as ``len(items) == limit``; clients can re-issue with
    ``offset += limit`` until ``has_more`` is False.
    """

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(description="Maximum items in this page.")
    offset: int = Field(description="Zero-based index of the first item in this page.")
    has_more: bool = Field(
        description=(
            "True when the page returned exactly ``limit`` items. False is "
            "a definitive 'no more rows' signal; True is a heuristic — "
            "the next page may still be empty if the last page exactly "
            "filled."
        ),
    )


class ListPage(BaseModel, Generic[T]):  # noqa: UP046 — see the T = TypeVar note above (Nuitka + PEP 695 + pydantic)
    """Page envelope for list endpoints.

    Generic so per-entity routers can declare ``ListPage[CurveResponse]``
    and FastAPI emits the right schema in OpenAPI. Uses the classic
    ``Generic[T]`` spelling (see the ``T = TypeVar(...)`` note above) so the
    package compiles cleanly under Nuitka for the source-protected image.
    """

    model_config = ConfigDict(extra="forbid")

    items: list[T]
    page: PageMeta


class EntityTimestamps(BaseModel):
    """The trigger-maintained / soft-delete fields the named-entity template carries."""

    model_config = ConfigDict(extra="forbid")

    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


# ---------------------------------------------------------------------------
# indices
# ---------------------------------------------------------------------------

# `indices.kind` CHECK list from 0003_curves_and_indices migration.
IndexKind = str  # 'IBOR' | 'Overnight' | 'Inflation' — enforced server-side via DB CHECK.


class IndexCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    kind: IndexKind = Field(description="One of 'IBOR' / 'Overnight' / 'Inflation'.")
    currency: str | None = None
    calendar: str | None = None
    day_counter: str | None = None
    body: dict[str, Any] = Field(default_factory=dict)


class IndexUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1)
    kind: IndexKind | None = None
    currency: str | None = None
    calendar: str | None = None
    day_counter: str | None = None
    body: dict[str, Any] | None = None


class IndexResponse(EntityTimestamps):
    id: UUID
    name: str
    kind: IndexKind
    currency: str | None = None
    calendar: str | None = None
    day_counter: str | None = None
    body: dict[str, Any]


# ---------------------------------------------------------------------------
# curves
# ---------------------------------------------------------------------------


class CurveCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    currency: str | None = None
    day_counter: str | None = None
    helper_kind: str | None = None
    reference_date: date | None = None
    points: list[dict[str, Any]] = Field(default_factory=list)
    body: dict[str, Any] = Field(default_factory=dict)


class CurveUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1)
    currency: str | None = None
    day_counter: str | None = None
    helper_kind: str | None = None
    reference_date: date | None = None
    points: list[dict[str, Any]] | None = None
    body: dict[str, Any] | None = None


class CurveResponse(EntityTimestamps):
    id: UUID
    name: str
    currency: str | None = None
    day_counter: str | None = None
    helper_kind: str | None = None
    reference_date: date | None = None
    points: list[dict[str, Any]]
    body: dict[str, Any]


# ---------------------------------------------------------------------------
# curve_sets
# ---------------------------------------------------------------------------


class CurveSetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    currency: str | None = None
    body: dict[str, Any] = Field(default_factory=dict)


class CurveSetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1)
    currency: str | None = None
    body: dict[str, Any] | None = None


class CurveSetResponse(EntityTimestamps):
    id: UUID
    name: str
    currency: str | None = None
    body: dict[str, Any]


# ---------------------------------------------------------------------------
# credit_curves
# ---------------------------------------------------------------------------

# `credit_curves.source` CHECK list from 0003.
CreditCurveSource = str  # 'flat' | 'manual' | 'quote_book'.


class CreditCurveCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    reference_entity: str | None = None
    currency: str | None = None
    seniority: str | None = None
    source: CreditCurveSource
    recovery_rate: float = Field(ge=0, le=1)
    body: dict[str, Any] = Field(default_factory=dict)


class CreditCurveUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1)
    reference_entity: str | None = None
    currency: str | None = None
    seniority: str | None = None
    source: CreditCurveSource | None = None
    recovery_rate: float | None = Field(default=None, ge=0, le=1)
    body: dict[str, Any] | None = None


class CreditCurveResponse(EntityTimestamps):
    id: UUID
    name: str
    reference_entity: str | None = None
    currency: str | None = None
    seniority: str | None = None
    source: CreditCurveSource
    recovery_rate: float
    body: dict[str, Any]


# ---------------------------------------------------------------------------
# snapshots
# ---------------------------------------------------------------------------


class SnapshotCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    as_of: date
    description: str | None = None
    content: dict[str, Any] = Field(default_factory=dict)


class SnapshotUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1)
    as_of: date | None = None
    description: str | None = None
    content: dict[str, Any] | None = None


class SnapshotResponse(EntityTimestamps):
    id: UUID
    name: str
    as_of: date
    description: str | None = None
    content: dict[str, Any]


# ---------------------------------------------------------------------------
# vol_surfaces
# ---------------------------------------------------------------------------

# `vol_surfaces.kind` CHECK list from 0005.
VolSurfaceKind = str  # 'SwaptionVolSpec' | 'OptionletVolSpec' | 'BlackVolSpec'.


class VolSurfaceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    kind: VolSurfaceKind
    payload: dict[str, Any] = Field(default_factory=dict)


class VolSurfaceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1)
    kind: VolSurfaceKind | None = None
    payload: dict[str, Any] | None = None


class VolSurfaceResponse(EntityTimestamps):
    id: UUID
    name: str
    kind: VolSurfaceKind
    payload: dict[str, Any]


# ---------------------------------------------------------------------------
# swaption_models
# ---------------------------------------------------------------------------


class SwaptionModelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    # Default mirrors the DB-side ``server_default = 'HullWhiteLattice'``
    # so a minimal request still validates against the named-entity CHECK.
    kind: str = Field(default="HullWhiteLattice", min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class SwaptionModelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1)
    kind: str | None = Field(default=None, min_length=1)
    payload: dict[str, Any] | None = None


class SwaptionModelResponse(EntityTimestamps):
    id: UUID
    name: str
    kind: str
    payload: dict[str, Any]


# ---------------------------------------------------------------------------
# Product entities (swaps_*, swaptions, bonds_*, cds, equity_options)
# ---------------------------------------------------------------------------


class ProductCreate(BaseModel):
    """Shared shape for the seven product tables.

    Each product table carries ``id``, ``owner_uid``, ``name``, and a
    single ``request`` JSONB column holding the full pricing request
    payload. Per-product code owns any tightening of the inner shape.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    request: dict[str, Any] = Field(default_factory=dict)


class ProductUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1)
    request: dict[str, Any] | None = None


class ProductResponse(EntityTimestamps):
    id: UUID
    name: str
    request: dict[str, Any]


# ---------------------------------------------------------------------------
# pricing_history (immutable; read-only via this API)
# ---------------------------------------------------------------------------

# `pricing_history.product_kind` CHECK list from 0007.
PricingHistoryProductKind = str


class PricingHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    product_kind: PricingHistoryProductKind
    product_id: UUID | None = None
    as_of: date | None = None
    trade_entity_type: str | None = Field(
        default=None,
        description=(
            "Entity-versions type key of the saved product this call "
            "priced by reference (equals ``product_kind``); NULL for "
            "inline pricing calls."
        ),
    )
    trade_entity_id: UUID | None = Field(
        default=None,
        description=(
            "Id of the saved product priced by reference (equals "
            "``product_id``); NULL for inline pricing calls."
        ),
    )
    trade_version: int | None = Field(
        default=None,
        description=(
            "The priced entity's head ``version_no`` in the audit trail "
            "(``GET /v1/<entity>/{id}/versions``) at pricing time; NULL "
            "for inline pricing calls."
        ),
    )
    request: dict[str, Any]
    response: dict[str, Any]
    created_at: datetime


# ---------------------------------------------------------------------------
# entity versions (append-only audit trail — read-only via this API)
# ---------------------------------------------------------------------------

# `entity_versions.change_type` CHECK list from 0013.
EntityVersionChangeType = str  # 'create' | 'amend' | 'delete' | 'restore'


class EntityVersionSummary(BaseModel):
    """One audit-trail entry (metadata only; no snapshot payload)."""

    model_config = ConfigDict(extra="forbid")

    version_no: int = Field(ge=1)
    change_type: EntityVersionChangeType = Field(
        description="One of 'create' / 'amend' / 'delete' / 'restore'.",
    )
    change_reason: str | None = Field(
        default=None,
        description="Optional reason supplied via the X-Change-Reason request header.",
    )
    changed_by_uid: str
    changed_by_email: str | None = None
    changed_at: datetime
    request_id: str | None = Field(
        default=None,
        description="Groups all writes performed by one user action/request.",
    )


class EntityVersionDetail(EntityVersionSummary):
    """One audit-trail entry including the full post-change row snapshot."""

    payload: dict[str, Any] = Field(
        description=(
            "Full row snapshot after the change (for deletes: the row "
            "state at deletion). Clients compute diffs; no server-side "
            "diff is stored."
        ),
    )


class EntityVersionList(BaseModel):
    """Version history for one entity, newest first."""

    model_config = ConfigDict(extra="forbid")

    items: list[EntityVersionSummary] = Field(default_factory=list)


__all__ = [
    "CreditCurveCreate",
    "CreditCurveResponse",
    "CreditCurveUpdate",
    "CurveCreate",
    "CurveResponse",
    "CurveSetCreate",
    "CurveSetResponse",
    "CurveSetUpdate",
    "CurveUpdate",
    "EntityVersionDetail",
    "EntityVersionList",
    "EntityVersionSummary",
    "IndexCreate",
    "IndexResponse",
    "IndexUpdate",
    "PageMeta",
    "PricingHistoryResponse",
    "ProductCreate",
    "ProductResponse",
    "ProductUpdate",
    "SnapshotCreate",
    "SnapshotResponse",
    "SnapshotUpdate",
    "SwaptionModelCreate",
    "SwaptionModelResponse",
    "SwaptionModelUpdate",
    "VolSurfaceCreate",
    "VolSurfaceResponse",
    "VolSurfaceUpdate",
]
