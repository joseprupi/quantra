"""``POST /v1/market-data/import`` — user market-data import.

The sanctioned, auth-gated, provenance-tagged write path into the SHARED
``md.*`` catalog. Until now ``md.quote_points`` was written only by the
ingester's vendor / synthetic connectors; this endpoint lets an
authenticated user load their own quotes and have the pricing stack
resolve them exactly like any other market data.

Request — EITHER of two content types on the same URL:

* ``multipart/form-data`` with a ``file`` part: a CSV whose rows are
  ``canonical_id, as_of (YYYY-MM-DD), value[, source, meta_json]`` (an
  optional header row is auto-detected). Default provenance ``source="csv"``
  (a per-row ``source`` column overrides it).
* ``application/json`` body ``{"quotes": [{"canonical_id", "as_of",
  "value", "source"?, "meta"?}, ...]}``. Default provenance
  ``source="manual"`` (a per-quote ``source`` overrides it).

Response ``200`` — the per-row import report::

    {
      "imported": <int>,   # rows upserted into md.quote_points
      "skipped":  <int>,   # rows dropped for a per-row reason
      "errors":   [ {"row": <int>, "reason": <str>}, ... ],
      "source":   <str>    # the effective default provenance tag
    }

Soft per-row failures (bad canonical-id grammar, unparseable value/date,
in-batch duplicate key) are reported in ``errors`` — NOT a 5xx. Hard
failures (missing file, unparseable body, empty batch, unsupported
content type) return the error envelope with a stable ``code``.

The upsert itself is delegated verbatim to the ingester's shared writer
(``quantra_md_ingester.user_import.import_user_quotes`` →
``quantra_md_ingester.writer``) so no MD SQL is duplicated here, and it
writes through a dedicated ``md_rw`` engine kept separate from the
request-path ``md_ro`` reader (pool isolation).
"""

from __future__ import annotations

from typing import Annotated, Any, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_common.auth.context import AuthContext
from quantra_md_ingester.user_import import (
    SOURCE_CSV,
    SOURCE_MANUAL,
    ImportReport,
    UserQuoteInput,
    import_user_quotes,
    parse_csv,
)
from quantra_orchestrator.auth.dependencies import get_auth_context
from quantra_orchestrator.data.engines import get_md_rw_engine

_log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["market-data-import"])

IMPORT_BAD_REQUEST_CODE: Final[str] = "market_data_import_invalid_request"
_MULTIPART_CT: Final[str] = "multipart/form-data"
_JSON_CT: Final[str] = "application/json"


class ImportBadRequestError(HTTPException):
    """400 for a hard, whole-request failure (bad envelope, not a bad row).

    Pins a stable ``code`` so a client can distinguish a malformed
    request from a well-formed batch that happened to contain bad rows
    (the latter is a 200 with a non-empty ``errors`` list).
    """

    code: Final[str] = IMPORT_BAD_REQUEST_CODE

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


class ImportQuoteIn(BaseModel):
    """One quote in the JSON import batch."""

    model_config = {"extra": "forbid"}

    canonical_id: str = Field(min_length=1, description="Target canonical id.")
    as_of: str = Field(description="Observation date, ``YYYY-MM-DD``.")
    value: float = Field(description="Quote value (decimal rate / level).")
    source: str | None = Field(
        default=None,
        description="Optional per-row provenance override (defaults to ``manual``).",
    )
    meta: dict[str, Any] | None = Field(
        default=None, description="Optional free-form per-row metadata."
    )


class MarketDataImportRequest(BaseModel):
    """The ``application/json`` import body."""

    model_config = {"extra": "forbid"}

    quotes: list[ImportQuoteIn] = Field(description="Quotes to import.")


class ImportRowErrorOut(BaseModel):
    """One soft, per-row failure surfaced in the report body."""

    row: int = Field(description="1-based row / array index that failed.")
    reason: str = Field(description="Human-readable reason the row was skipped.")


class MarketDataImportResponse(BaseModel):
    """The per-row import report (HTTP 200)."""

    imported: int = Field(description="Rows upserted into ``md.quote_points``.")
    skipped: int = Field(description="Rows dropped for a per-row reason.")
    errors: list[ImportRowErrorOut] = Field(
        description="Per-row failures; empty when every row imported."
    )
    source: str = Field(description="Effective default provenance tag (``csv`` or ``manual``).")


def _report_to_response(report: ImportReport, *, source: str) -> MarketDataImportResponse:
    return MarketDataImportResponse(
        imported=report.imported,
        skipped=report.skipped,
        errors=[ImportRowErrorOut(row=e.row, reason=e.reason) for e in report.errors],
        source=source,
    )


async def _read_csv_inputs(request: Request) -> list[UserQuoteInput]:
    """Extract the uploaded CSV, parse it, and fold parse errors into rows.

    Parse-stage errors (bad column count, non-numeric value, bad meta json)
    are turned into pre-failed :class:`UserQuoteInput`-less rows by raising
    them straight into the report: we return the parsed inputs and let the
    caller merge the parse errors. To keep the merge simple we attach the
    parse errors onto the request via the returned tuple.
    """

    form = await request.form()
    try:
        upload = form.get("file")
        if upload is None or isinstance(upload, str):
            raise ImportBadRequestError(
                "multipart/form-data request must include a 'file' part with the CSV upload."
            )
        raw = await upload.read()
        try:
            text_data = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ImportBadRequestError("Uploaded file is not valid UTF-8 text.") from exc
    finally:
        # Close the SpooledTemporaryFile backing every UploadFile so its
        # temp file is released promptly (an un-closed handle surfaces as a
        # ResourceWarning at GC time).
        await form.close()
    quotes, parse_errors = parse_csv(text_data)
    # Stash parse errors on the request state so the handler can merge them
    # into the DB-stage report (parse errors are per-row soft errors too).
    request.state.csv_parse_errors = parse_errors
    return quotes


async def _read_json_inputs(request: Request) -> list[UserQuoteInput]:
    try:
        body = await request.json()
    except (ValueError, TypeError) as exc:
        raise ImportBadRequestError("Request body is not valid JSON.") from exc
    try:
        parsed = MarketDataImportRequest.model_validate(body)
    except ValidationError as exc:
        raise ImportBadRequestError(f"Invalid import batch: {exc.errors()!r}") from exc
    return [
        UserQuoteInput(
            row=index + 1,
            canonical_id=q.canonical_id,
            as_of=q.as_of,
            value=q.value,
            source=q.source,
            meta=q.meta,
        )
        for index, q in enumerate(parsed.quotes)
    ]


@router.post(
    "/market-data/import",
    response_model=MarketDataImportResponse,
    responses={
        400: {"description": "Malformed request (bad body / missing file / empty batch)."},
        401: {"description": "Missing or invalid credentials."},
        503: {"description": "Market-data write storage is not configured."},
    },
)
async def import_market_data(
    request: Request,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    engine: Annotated[AsyncEngine, Depends(get_md_rw_engine)],
) -> MarketDataImportResponse:
    """Import user quotes (CSV upload or JSON batch) into the shared ``md.*``.

    Auth is required (401 with ``code="unauthenticated"`` otherwise). The
    content type selects the parser and the default provenance tag. Returns
    a per-row report; a batch with some bad rows still succeeds (200) and
    reports the bad rows in ``errors``.
    """

    content_type = request.headers.get("content-type", "").lower()
    request.state.csv_parse_errors = []

    if content_type.startswith(_MULTIPART_CT):
        default_source = SOURCE_CSV
        inputs = await _read_csv_inputs(request)
    elif content_type.startswith(_JSON_CT) or not content_type:
        default_source = SOURCE_MANUAL
        inputs = await _read_json_inputs(request)
    else:
        raise ImportBadRequestError(
            f"Unsupported content type {content_type!r}; use multipart/form-data "
            "(CSV file) or application/json (quotes batch)."
        )

    parse_errors = list(getattr(request.state, "csv_parse_errors", []))
    if not inputs and not parse_errors:
        raise ImportBadRequestError("Import batch is empty; supply at least one quote.")

    report = await import_user_quotes(engine, inputs, default_source=default_source)
    # Fold CSV parse-stage errors into the report so the caller sees one
    # unified per-row error list (parse failures + validation failures).
    if parse_errors:
        for err in parse_errors:
            report.errors.append(err)
        report.skipped += len(parse_errors)
        report.errors.sort(key=lambda e: e.row)

    _log.info(
        "orchestrator.market_data_import",
        uid=ctx.uid,
        default_source=default_source,
        imported=report.imported,
        skipped=report.skipped,
    )
    return _report_to_response(report, source=default_source)
