"""``/v1/market-data/latest-date`` — freshest ingested business day in ``md.*``.

The self-hosted bundle ingests real public feeds (BoE SONIA OIS, US Treasury)
on a daily cron, so the newest ``md.quote_points`` date advances over time.
The portal reads this endpoint to default the pricing As-Of to the freshest
available date, so a fresh user prices the "always current" seeded curves on a
date the feed actually has data for (instead of a stale hard-coded default that
would resolve no quotes).

Owner-agnostic — ``md.*`` is global to the deployment (no ``owner_uid``; the
same principle as ``/v1/market-data/series``). Optional filters:

* ``?source=BOE``   — restrict to one connector's ``quote_points.source`` tag.
* ``?prefix=GBP.RATES.BOE.OIS.`` — restrict to a ``canonical_id`` family
  (more precise than ``source`` when one connector writes several families).

Returns ``latest_date=null`` (HTTP 200) when nothing matches, so the caller can
fall back to its own default rather than treating an empty catalog as an error.
Reads through the ``md_rw`` engine (the same low-traffic management engine the
series CRUD uses) so no extra pool/DSN is required.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_common.auth.context import AuthContext
from quantra_orchestrator.auth.dependencies import get_auth_context
from quantra_orchestrator.data.engines import get_md_rw_engine

_log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["market-data"])

# Four compile-time-literal query forms (no user text ever reaches the SQL
# grammar — filters arrive only as bound parameters). ``::date`` maps the
# timestamptz max onto a plain calendar date.
_SQL_ALL: Final[str] = "SELECT max(as_of)::date AS latest_date FROM quote_points"
_SQL_SOURCE: Final[str] = (
    "SELECT max(as_of)::date AS latest_date FROM quote_points WHERE source = :source"
)
_SQL_PREFIX: Final[str] = (
    "SELECT max(as_of)::date AS latest_date FROM quote_points WHERE canonical_id LIKE :prefix"
)
_SQL_SOURCE_PREFIX: Final[str] = (
    "SELECT max(as_of)::date AS latest_date FROM quote_points "
    "WHERE source = :source AND canonical_id LIKE :prefix"
)


class LatestDateResponse(BaseModel):
    """The freshest ingested business day (or ``null`` if none) + echoed filters."""

    latest_date: date | None
    source: str | None
    prefix: str | None


def _select_query(source: str | None, prefix: str | None) -> tuple[str, dict[str, str]]:
    params: dict[str, str] = {}
    if source is not None:
        params["source"] = source
    if prefix is not None:
        params["prefix"] = f"{prefix}%"
    if source is not None and prefix is not None:
        return _SQL_SOURCE_PREFIX, params
    if source is not None:
        return _SQL_SOURCE, params
    if prefix is not None:
        return _SQL_PREFIX, params
    return _SQL_ALL, params


@router.get(
    "/market-data/latest-date",
    response_model=LatestDateResponse,
    responses={
        401: {"description": "Missing or invalid credentials."},
        503: {"description": "Market-data storage is not configured."},
    },
)
async def get_market_data_latest_date(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    engine: Annotated[AsyncEngine, Depends(get_md_rw_engine)],
    source: Annotated[str | None, Query(description="Filter by connector source tag.")] = None,
    prefix: Annotated[
        str | None, Query(description="Filter by canonical_id prefix (LIKE 'prefix%').")
    ] = None,
) -> LatestDateResponse:
    """Return the latest ingested business day in ``md.quote_points``."""

    src = source.strip() if source and source.strip() else None
    pfx = prefix.strip() if prefix and prefix.strip() else None
    sql, params = _select_query(src, pfx)
    async with engine.connect() as conn:
        result = await conn.execute(text(sql), params)
        row = result.mappings().one_or_none()
    latest = row["latest_date"] if row is not None else None
    _log.info(
        "orchestrator.market_data.latest_date",
        uid=ctx.uid,
        source=src,
        prefix=pfx,
        latest_date=latest.isoformat() if latest else None,
    )
    return LatestDateResponse(latest_date=latest, source=src, prefix=pfx)
