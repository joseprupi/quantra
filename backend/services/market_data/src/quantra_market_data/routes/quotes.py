"""``GET /series/{id}`` range read + ``POST /quotes/resolved`` batch resolver."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_market_data._helpers import downsample_rows, iso
from quantra_market_data.db import get_md_engine
from quantra_market_data.schemas import (
    QuotePointResponse,
    ResolvedQuoteItemResponse,
    ResolvedQuotesRequest,
    ResolvedQuotesResponse,
)

router = APIRouter()


def _quote_point_from_row(row: Row[Any]) -> QuotePointResponse:
    return QuotePointResponse(
        canonical_id=row.canonical_id,
        as_of=iso(row.as_of),
        value=row.value,
        source=row.source,
        vendor_id=row.vendor_id,
        quality_flags=row.quality_flags or {},
        meta=row.meta or {},
    )


async def _fetch_quote_range(
    engine: AsyncEngine,
    canonical_id: str,
    start: datetime,
    end: datetime,
    limit: int,
    max_points: int | None,
) -> list[QuotePointResponse]:
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT canonical_id, as_of, value, source, vendor_id, quality_flags, meta
                    FROM quote_points
                    WHERE canonical_id = :canonical_id AND as_of >= :start AND as_of <= :end
                    ORDER BY as_of ASC LIMIT :limit
                    """
                ),
                {"canonical_id": canonical_id, "start": start, "end": end, "limit": limit},
            )
        ).all()
    rows = downsample_rows(list(rows), max_points)
    return [_quote_point_from_row(r) for r in rows]


@router.get("/series/{canonical_id}", response_model=list[QuotePointResponse], tags=["internal"])
async def get_series_range(
    canonical_id: str,
    engine: AsyncEngine = Depends(get_md_engine),
    start: datetime = Query(...),
    end: datetime = Query(...),
    limit: int = Query(default=5000, ge=1, le=50000),
    max_points: int | None = Query(default=None, ge=1, le=5000),
) -> list[QuotePointResponse]:
    return await _fetch_quote_range(engine, canonical_id, start, end, limit, max_points)


@router.post("/quotes/resolved", response_model=ResolvedQuotesResponse, tags=["internal"])
async def resolve_quotes_for_as_of(
    payload: ResolvedQuotesRequest,
    engine: AsyncEngine = Depends(get_md_engine),
) -> ResolvedQuotesResponse:
    canonical_ids = [cid.strip() for cid in payload.canonical_ids if cid and cid.strip()]
    if not canonical_ids:
        return ResolvedQuotesResponse(items=[])

    requested_as_of = payload.as_of.astimezone(UTC)
    snapshot_version = payload.snapshot_version
    async with engine.begin() as conn:
        if snapshot_version is not None:
            # etag-scoped lookup. ``snapshot_quotes`` is
            # an exact pin (no as_of <= fall-through); ``is_exact`` is
            # ``TRUE`` by construction when a row is returned. If the etag
            # doesn't match any ``md.snapshots`` row (stale pin), every
            # ``found`` is ``False`` and the orchestrator surfaces
            # ``<product>_quote_resolution_failed``.
            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT ids.canonical_id,
                               sq.resolved_as_of AS resolved_as_of,
                               sq.value AS value,
                               sq.source AS source,
                               sq.vendor_id AS vendor_id,
                               TRUE AS is_exact
                        FROM unnest(CAST(:canonical_ids AS TEXT[])) AS ids(canonical_id)
                        LEFT JOIN LATERAL (
                          SELECT sq.canonical_id, sq.value, sq.source,
                                 sq.vendor_id, sq.resolved_as_of
                          FROM md.snapshot_quotes sq
                          JOIN md.snapshots s ON s.id = sq.snapshot_id
                          WHERE s.version_etag = :snapshot_version
                            AND sq.canonical_id = ids.canonical_id
                          LIMIT 1
                        ) sq ON TRUE
                        """
                    ),
                    {
                        "canonical_ids": canonical_ids,
                        "snapshot_version": snapshot_version,
                    },
                )
            ).all()
        else:
            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT ids.canonical_id,
                               q.as_of AS resolved_as_of,
                               q.value AS value,
                               q.source AS source,
                               q.vendor_id AS vendor_id,
                               ex.exists_exact AS is_exact
                        FROM unnest(CAST(:canonical_ids AS TEXT[])) AS ids(canonical_id)
                        LEFT JOIN LATERAL (
                          SELECT canonical_id, as_of, value, source, vendor_id
                          FROM quote_points
                          WHERE canonical_id = ids.canonical_id AND as_of <= :as_of
                          ORDER BY as_of DESC
                          LIMIT 1
                        ) q ON TRUE
                        LEFT JOIN LATERAL (
                          SELECT TRUE AS exists_exact
                          FROM quote_points
                          WHERE canonical_id = ids.canonical_id AND as_of = :as_of
                          LIMIT 1
                        ) ex ON TRUE
                        """
                    ),
                    {"canonical_ids": canonical_ids, "as_of": requested_as_of},
                )
            ).all()

    items: list[ResolvedQuoteItemResponse] = []
    req_iso = iso(requested_as_of)
    for row in rows:
        found = row.resolved_as_of is not None
        items.append(
            ResolvedQuoteItemResponse(
                canonical_id=row.canonical_id,
                requested_as_of=req_iso,
                found=found,
                is_exact=bool(row.is_exact) if found else False,
                resolved_as_of=iso(row.resolved_as_of) if found else None,
                value=float(row.value) if found else None,
                source=row.source if found else None,
                vendor_id=row.vendor_id if found else None,
            )
        )
    return ResolvedQuotesResponse(items=items)
