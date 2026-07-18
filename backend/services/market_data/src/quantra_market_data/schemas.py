"""Pydantic response models for the MD read API.

These mirror the pre-monorepo market-data service's legacy shapes
exactly so the wire format is identical pre- and post-lift. We intentionally
do **not** reuse ``quantra_common.types.market_data`` here yet — the shared
types carry extra fields (``kind``, ``currency``) that are not part of the
current public contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    service: str
    utc_time: str


class QuotePointResponse(BaseModel):
    canonical_id: str
    as_of: str
    value: float
    source: str | None = None
    vendor_id: str | None = None
    quality_flags: dict[str, Any]
    meta: dict[str, Any]


class SeriesResponse(BaseModel):
    canonical_id: str
    asset_class: str
    family: str | None = None
    instrument: str
    currency: str
    tenor: str | None = None
    field: str
    frequency: str
    units: str
    description: str | None = None


class ResolvedQuotesRequest(BaseModel):
    canonical_ids: list[str]
    as_of: datetime
    # optional snapshot pin. ``None`` (default) keeps
    # today's "live ``quote_points`` lookup as_of <= :as_of" semantics.
    # When set, the resolve route scopes the lookup to
    # ``md.snapshot_quotes`` joined to ``md.snapshots`` by
    # ``version_etag = :snapshot_version`` so the orchestrator's
    # cache invalidates as soon as the trigger advances the etag.
    snapshot_version: str | None = None


class ResolvedQuoteItemResponse(BaseModel):
    canonical_id: str
    requested_as_of: str
    found: bool
    is_exact: bool
    resolved_as_of: str | None = None
    value: float | None = None
    source: str | None = None
    vendor_id: str | None = None


class ResolvedQuotesResponse(BaseModel):
    items: list[ResolvedQuoteItemResponse]
