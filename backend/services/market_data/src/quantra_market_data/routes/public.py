"""Public catalog surface (``/catalog/series``).

The one public catalog listing the portal's Quote Book / series pickers
read. The other legacy ``/catalog/*`` / ``/timeseries/*`` / ``/market/*``
aliases were deleted as dead code — nothing called them.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_market_data.db import get_md_engine
from quantra_market_data.schemas import SeriesResponse

router = APIRouter()


@router.get("/catalog/series", response_model=list[SeriesResponse], tags=["public"])
async def public_catalog_series(
    engine: AsyncEngine = Depends(get_md_engine),
    source: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
) -> list[SeriesResponse]:
    query = """
        SELECT c.canonical_id, c.asset_class, c.family, c.instrument, c.currency,
               c.tenor, c.field, c.frequency, c.units, c.description
        FROM canonical_ids c
    """
    params: dict[str, Any] = {"limit": limit}
    if source:
        query += """
            INNER JOIN vendor_mappings vm ON vm.canonical_id = c.canonical_id
            WHERE vm.vendor = :source AND vm.active = TRUE
        """
        params["source"] = source
    query += " ORDER BY c.canonical_id LIMIT :limit"
    async with engine.begin() as conn:
        rows = (await conn.execute(text(query), params)).all()
    return [SeriesResponse(**dict(r._mapping)) for r in rows]
