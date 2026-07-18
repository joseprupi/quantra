"""Internal data carriers for the ingester pipeline.

These dataclasses are the contract between connector adapters and the
DB writer. They are intentionally *not* exposed to the read service —
``QuotePointResponse`` (see ``services/market_data/.../schemas.py``)
is the response surface and is decoupled on purpose.

Placed
service-local; see the docstring on ``canonical.py`` for the rationale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class QuoteRecord:
    """One observation produced by a connector.

    ``meta`` is open-ended; the writer JSON-serialises it into the
    ``meta`` JSONB column on ``md.quote_points`` so connectors can
    stash whatever per-observation context (raw vendor unit, series
    description, …) they need without a schema migration.
    """

    canonical_id: str
    as_of: datetime
    value: float
    source: str
    vendor_id: str | None = None
    quality_flags: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    # Honest storage unit for ``value`` (written to ``md.quote_points.units``
    # and, on first sight of a series, ``md.canonical_ids.units``). Rate
    # connectors normalise vendor percent to decimals (``decimal_rate``);
    # level series (CPI index, VIX, SOFR index, …) are ``level``; FX spot
    # is ``spot_rate``. Defaults to ``decimal_rate`` — the overwhelmingly
    # common case for the rate connectors.
    units: str = "decimal_rate"
