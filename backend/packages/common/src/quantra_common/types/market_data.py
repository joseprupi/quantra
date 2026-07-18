"""Market-data shaped types shared across services."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ResolvedQuote(BaseModel):
    """Result of resolving a market-data reference (may be a miss)."""

    canonical_id: str
    requested_as_of: datetime | None = None
    found: bool
    is_exact: bool = False
    resolved_as_of: datetime | None = None
    value: float | None = None
    source: str | None = None
    vendor_id: str | None = None
