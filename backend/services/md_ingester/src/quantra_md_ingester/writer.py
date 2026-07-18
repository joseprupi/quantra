"""Connector-free ``md.*`` writer SQL, shared by every write path.

The per-row upsert statements + helpers used to live inside
``pipeline.py``, but ``pipeline`` imports the network vendor connectors
(FRED/ECB/BoE/…, and openpyxl via the BoE curve adapter). The
user-import path is consumed by the *orchestrator* (an API process that
must never pull a network-connector / openpyxl import graph), so the raw
writer machinery is lifted here where it depends only on SQLAlchemy +
the canonical-id grammar + :class:`QuoteRecord`.

Both writers import from this module, so the ``md.quote_points`` /
``md.canonical_ids`` / ``md.vendor_mappings`` upsert SQL is single-sourced
(no duplication between the ingester and the orchestrator import route).

Bound parameters rely on ``md_rw``'s pinned ``search_path`` so the
table names are unqualified — identical to what the ingester pipeline and
the MD read service do.
"""

from __future__ import annotations

import json
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from quantra_md_ingester.canonical import humanize_canonical_id, parse_canonical_id
from quantra_md_ingester.models import QuoteRecord

_UPSERT_CANONICAL_SQL: Final[str] = """
    INSERT INTO canonical_ids
      (canonical_id, asset_class, family, instrument, currency, tenor, field,
       frequency, units, description)
    VALUES
      (:canonical_id, :asset_class, :family, :instrument, :currency, :tenor,
       :field, 'daily', :units, :description)
    ON CONFLICT (canonical_id)
    DO UPDATE SET
      asset_class = EXCLUDED.asset_class,
      family = EXCLUDED.family,
      instrument = EXCLUDED.instrument,
      currency = EXCLUDED.currency,
      tenor = EXCLUDED.tenor,
      field = EXCLUDED.field,
      description = CASE
        WHEN canonical_ids.description IS NULL
             OR btrim(canonical_ids.description) = ''
        THEN EXCLUDED.description
        ELSE canonical_ids.description
      END
"""


_UPSERT_VENDOR_MAPPING_SQL: Final[str] = """
    INSERT INTO vendor_mappings
      (vendor, vendor_id, vendor_field, canonical_id)
    VALUES
      (:vendor, :vendor_id, 'RATE', :canonical_id)
    ON CONFLICT (vendor, vendor_id)
    DO UPDATE SET
      canonical_id = EXCLUDED.canonical_id,
      vendor_field = EXCLUDED.vendor_field
"""


_UPSERT_QUOTE_SQL: Final[str] = """
    INSERT INTO quote_points
      (canonical_id, as_of, source, vendor_id, raw_value, value, units,
       quality_flags, meta)
    VALUES
      (:canonical_id, :as_of, :source, :vendor_id, :raw_value, :value, :units,
       CAST(:quality_flags AS JSONB), CAST(:meta AS JSONB))
    ON CONFLICT (canonical_id, as_of)
    DO UPDATE SET
      source = EXCLUDED.source,
      vendor_id = EXCLUDED.vendor_id,
      raw_value = EXCLUDED.raw_value,
      value = EXCLUDED.value,
      units = EXCLUDED.units,
      quality_flags = EXCLUDED.quality_flags,
      meta = EXCLUDED.meta
"""


async def upsert_canonical(
    conn: AsyncConnection,
    canonical_id: str,
    *,
    units: str = "decimal_rate",
) -> None:
    """Register (or refresh) one ``md.canonical_ids`` row from its parsed grammar.

    ``units`` is only applied when the series is first created; an existing
    row keeps whatever units it already declares. The auto-derived
    ``description`` likewise only fills a NULL/empty slot — a user-edited
    description (PATCH ``/v1/market-data/series``) is never clobbered by a
    subsequent nightly ingest tick (the CASE in the upsert SQL).
    """

    parts = parse_canonical_id(canonical_id)
    # A clean label derived from the id's own parts (e.g. "USD UST 10Y YIELD")
    # rather than an ugly "Auto-created from connector for ..." placeholder —
    # this is what the portal's Time Series Lab shows. Fall back to the id
    # itself if the parts yield nothing meaningful.
    description = humanize_canonical_id(parts) or canonical_id
    await conn.execute(
        text(_UPSERT_CANONICAL_SQL),
        {
            "canonical_id": canonical_id,
            "asset_class": parts["asset"],
            "family": parts["family"],
            "instrument": parts["instrument"],
            "currency": parts["currency"],
            "tenor": parts["tenor"],
            "field": parts["field"],
            "units": units,
            "description": description,
        },
    )


async def upsert_vendor_mapping(conn: AsyncConnection, quote: QuoteRecord) -> None:
    """Upsert the ``md.vendor_mappings`` row for a quote carrying a ``vendor_id``."""

    if not quote.vendor_id:
        return
    await conn.execute(
        text(_UPSERT_VENDOR_MAPPING_SQL),
        {
            "vendor": quote.source,
            "vendor_id": quote.vendor_id,
            "canonical_id": quote.canonical_id,
        },
    )


async def upsert_quote(conn: AsyncConnection, quote: QuoteRecord) -> None:
    """Upsert one ``md.quote_points`` row on the ``(canonical_id, as_of)`` PK."""

    raw_value = quote.meta.get("raw_value_percent") if quote.meta else None
    await conn.execute(
        text(_UPSERT_QUOTE_SQL),
        {
            "canonical_id": quote.canonical_id,
            "as_of": quote.as_of,
            "source": quote.source,
            "vendor_id": quote.vendor_id,
            "raw_value": raw_value,
            "value": quote.value,
            # Honest per-quote unit (bug: this used to hardcode
            # 'decimal_rate' for every row, mislabelling index levels /
            # FX spots). Connectors set QuoteRecord.units per series.
            "units": quote.units,
            "quality_flags": json.dumps(quote.quality_flags or {}),
            "meta": json.dumps(quote.meta or {}),
        },
    )


__all__ = [
    "upsert_canonical",
    "upsert_quote",
    "upsert_vendor_mapping",
]
