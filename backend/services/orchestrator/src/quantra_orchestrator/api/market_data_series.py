"""``/v1/market-data/series`` — CRUD for quote SERIES definitions.

The structured catalog rows behind the Quote Book (``md.canonical_ids``):
the metadata (``asset_class``, ``family``, ``currency``, ``tenor``, ``field``,
``frequency``, ``units``, ``description``) that describes a series. The VALUES
of a series are ``md.quote_points``, written via
``POST /v1/market-data/import``; this router is the sanctioned,
auth-gated ``md_rw`` write path for the series DEFINITION itself — the
same principle, extended to ``canonical_ids`` create / edit / delete.

Routes:

* ``POST   /v1/market-data/series``                — create a series (409 if it
  already exists, 422 on bad canonical-id grammar).
* ``GET    /v1/market-data/series``                — list every series.
* ``GET    /v1/market-data/series/{canonical_id}`` — read one (404 if missing).
* ``PATCH  /v1/market-data/series/{canonical_id}`` — edit metadata (not the id;
  404 if missing).
* ``DELETE /v1/market-data/series/{canonical_id}`` — delete the series AND its
  ``md.quote_points`` values (404 if missing; reports ``deleted_quote_points``).

Owner-agnostic — the ``md.*`` catalog is global to the deployment (no
``owner_uid``). All SQL is delegated to
:mod:`quantra_md_ingester.series` (single-sourced ``md.*`` SQL). Reads and
writes both go through the dedicated ``md_rw`` engine wired for the
import path — this is a low-traffic management surface, and serving its
reads from the same engine keeps the CRUD screen consistent (a list issued
right after a create reflects the new row) without a second pool.
"""

from __future__ import annotations

from typing import Annotated, Any, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_common.auth.context import AuthContext
from quantra_md_ingester.canonical import parse_canonical_id, validate_canonical_id
from quantra_md_ingester.series import (
    EDITABLE_COLUMNS,
    create_series,
    delete_series,
    get_series,
    list_series,
    update_series,
)
from quantra_orchestrator.auth.dependencies import get_auth_context
from quantra_orchestrator.data.engines import get_md_rw_engine

_log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["market-data-series"])

SERIES_INVALID_CODE: Final[str] = "market_data_series_invalid"
SERIES_EXISTS_CODE: Final[str] = "market_data_series_exists"
SERIES_NOT_FOUND_CODE: Final[str] = "market_data_series_not_found"

_DEFAULT_FREQUENCY: Final[str] = "daily"
_DEFAULT_UNITS: Final[str] = "decimal_rate"


class SeriesInvalidError(HTTPException):
    """422 — bad canonical-id grammar or an empty required metadata field."""

    code: Final[str] = SERIES_INVALID_CODE

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=422, detail=detail)


class SeriesExistsError(HTTPException):
    """409 — a series with this ``canonical_id`` already exists."""

    code: Final[str] = SERIES_EXISTS_CODE

    def __init__(self, canonical_id: str) -> None:
        super().__init__(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"series {canonical_id!r} already exists.",
        )


class SeriesNotFoundError(HTTPException):
    """404 — no series with this ``canonical_id``."""

    code: Final[str] = SERIES_NOT_FOUND_CODE

    def __init__(self, canonical_id: str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"series {canonical_id!r} does not exist.",
        )


class SeriesCreateRequest(BaseModel):
    """Body for ``POST /v1/market-data/series``.

    ``canonical_id``, ``asset_class`` and ``currency`` are required; the
    remaining structural fields (``family``, ``instrument``, ``tenor``,
    ``field``) are derived from the canonical-id grammar when omitted, and
    ``frequency`` / ``units`` fall back to the catalog defaults. This keeps
    every ``NOT NULL`` column populated while matching the Quote Book's
    minimal "new series" form.
    """

    model_config = {"extra": "forbid"}

    canonical_id: str = Field(min_length=1, description="Series canonical id.")
    asset_class: str = Field(min_length=1, description="e.g. RATES, INFLATION, VOL.")
    currency: str = Field(min_length=1, description="3-letter currency / ticker.")
    family: str | None = Field(default=None, description="e.g. IRS, SOFR, HICP.")
    instrument: str | None = Field(default=None, description="Instrument descriptor.")
    tenor: str | None = Field(default=None, description="e.g. 5Y, 3M.")
    field: str | None = Field(default=None, description="e.g. RATE, VOL, YIELD.")
    frequency: str | None = Field(default=None, description="Default ``daily``.")
    units: str | None = Field(default=None, description="Default ``decimal_rate``.")
    description: str | None = Field(default=None, description="Free-form label.")


class SeriesPatchRequest(BaseModel):
    """Body for ``PATCH /v1/market-data/series/{canonical_id}``.

    Every field is optional; only the fields explicitly supplied are updated
    (``canonical_id`` itself is immutable and cannot be patched). A field set
    to an empty string that maps to a ``NOT NULL`` column is rejected (422).
    """

    model_config = {"extra": "forbid"}

    asset_class: str | None = None
    family: str | None = None
    instrument: str | None = None
    currency: str | None = None
    tenor: str | None = None
    field: str | None = None
    frequency: str | None = None
    units: str | None = None
    description: str | None = None


class SeriesOut(BaseModel):
    """One ``md.canonical_ids`` catalog row."""

    canonical_id: str
    asset_class: str
    family: str | None
    instrument: str
    currency: str
    tenor: str | None
    field: str
    frequency: str
    units: str
    description: str | None
    created_at: str
    updated_at: str


class SeriesListResponse(BaseModel):
    """The series catalog list (management table + dropdown source)."""

    series: list[SeriesOut]
    count: int


class SeriesDeleteResponse(BaseModel):
    """Outcome of a series delete, including the cascaded value count."""

    canonical_id: str
    deleted: bool
    deleted_quote_points: int


# Columns that map to a ``NOT NULL`` / non-empty-checked catalog column: a
# PATCH may not blank them out.
_REQUIRED_TEXT_COLUMNS: Final[frozenset[str]] = frozenset(
    {"asset_class", "instrument", "currency", "field", "frequency", "units"}
)


def _row_to_out(row: dict[str, Any]) -> SeriesOut:
    return SeriesOut(
        canonical_id=row["canonical_id"],
        asset_class=row["asset_class"],
        family=row["family"],
        instrument=row["instrument"],
        currency=row["currency"],
        tenor=row["tenor"],
        field=row["field"],
        frequency=row["frequency"],
        units=row["units"],
        description=row["description"],
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )


def _build_insert_fields(body: SeriesCreateRequest) -> dict[str, Any]:
    """Assemble the full insert column set, deriving structural gaps.

    The canonical-id grammar (validated by the caller) always yields a
    non-empty ``instrument`` / ``field`` (and ``family`` / ``tenor``), so any
    omitted structural column is back-filled from it — guaranteeing every
    ``NOT NULL`` catalog column is populated.
    """

    parts = parse_canonical_id(body.canonical_id)
    return {
        "canonical_id": body.canonical_id,
        "asset_class": body.asset_class,
        "family": body.family if body.family is not None else parts["family"],
        "instrument": body.instrument or parts["instrument"],
        "currency": body.currency,
        "tenor": body.tenor if body.tenor is not None else parts["tenor"],
        "field": body.field or parts["field"],
        "frequency": body.frequency or _DEFAULT_FREQUENCY,
        "units": body.units or _DEFAULT_UNITS,
        "description": body.description,
    }


@router.post(
    "/market-data/series",
    response_model=SeriesOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        401: {"description": "Missing or invalid credentials."},
        409: {"description": "A series with this canonical_id already exists."},
        422: {"description": "Bad canonical-id grammar or empty required field."},
        503: {"description": "Market-data write storage is not configured."},
    },
)
async def create_market_data_series(
    body: SeriesCreateRequest,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    engine: Annotated[AsyncEngine, Depends(get_md_rw_engine)],
) -> SeriesOut:
    """Define a new quote series (structured ``md.canonical_ids`` row)."""

    canonical_id = body.canonical_id.strip()
    if not validate_canonical_id(canonical_id):
        raise SeriesInvalidError(f"invalid canonical_id: {body.canonical_id!r}")
    fields = _build_insert_fields(body.model_copy(update={"canonical_id": canonical_id}))
    async with engine.begin() as conn:
        row = await create_series(conn, fields)
    if row is None:
        raise SeriesExistsError(canonical_id)
    _log.info("orchestrator.market_data_series.create", uid=ctx.uid, canonical_id=canonical_id)
    return _row_to_out(row)


@router.get("/market-data/series", response_model=SeriesListResponse)
async def list_market_data_series(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    engine: Annotated[AsyncEngine, Depends(get_md_rw_engine)],
) -> SeriesListResponse:
    """List every series (management table + dropdown source)."""

    async with engine.connect() as conn:
        rows = await list_series(conn)
    return SeriesListResponse(series=[_row_to_out(r) for r in rows], count=len(rows))


@router.get(
    "/market-data/series/{canonical_id}",
    response_model=SeriesOut,
    responses={404: {"description": "No series with this canonical_id."}},
)
async def read_market_data_series(
    canonical_id: str,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    engine: Annotated[AsyncEngine, Depends(get_md_rw_engine)],
) -> SeriesOut:
    """Read one series by canonical id."""

    async with engine.connect() as conn:
        row = await get_series(conn, canonical_id)
    if row is None:
        raise SeriesNotFoundError(canonical_id)
    return _row_to_out(row)


@router.patch(
    "/market-data/series/{canonical_id}",
    response_model=SeriesOut,
    responses={
        404: {"description": "No series with this canonical_id."},
        422: {"description": "Empty value for a required metadata field."},
    },
)
async def patch_market_data_series(
    canonical_id: str,
    body: SeriesPatchRequest,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    engine: Annotated[AsyncEngine, Depends(get_md_rw_engine)],
) -> SeriesOut:
    """Edit a series' metadata (never its canonical id)."""

    patch = {k: v for k, v in body.model_dump(exclude_unset=True).items() if k in EDITABLE_COLUMNS}
    for col in _REQUIRED_TEXT_COLUMNS & patch.keys():
        value = patch[col]
        if value is None or (isinstance(value, str) and not value.strip()):
            raise SeriesInvalidError(f"{col} must not be empty.")
    async with engine.begin() as conn:
        row = await update_series(conn, canonical_id, patch)
    if row is None:
        raise SeriesNotFoundError(canonical_id)
    _log.info("orchestrator.market_data_series.patch", uid=ctx.uid, canonical_id=canonical_id)
    return _row_to_out(row)


@router.delete(
    "/market-data/series/{canonical_id}",
    response_model=SeriesDeleteResponse,
    responses={404: {"description": "No series with this canonical_id."}},
)
async def delete_market_data_series(
    canonical_id: str,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    engine: Annotated[AsyncEngine, Depends(get_md_rw_engine)],
) -> SeriesDeleteResponse:
    """Delete a series and cascade-delete its ``md.quote_points`` values."""

    async with engine.begin() as conn:
        deleted_quote_points = await delete_series(conn, canonical_id)
    if deleted_quote_points is None:
        raise SeriesNotFoundError(canonical_id)
    _log.info(
        "orchestrator.market_data_series.delete",
        uid=ctx.uid,
        canonical_id=canonical_id,
        deleted_quote_points=deleted_quote_points,
    )
    return SeriesDeleteResponse(
        canonical_id=canonical_id,
        deleted=True,
        deleted_quote_points=deleted_quote_points,
    )
