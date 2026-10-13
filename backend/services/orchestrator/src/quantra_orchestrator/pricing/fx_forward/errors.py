"""Structured-error HTTPExceptions for the FX-forward pricing path.

Same pattern as :mod:`quantra_orchestrator.pricing.swap_ir.errors`: each
subclass pins its stable ``code`` as a class attribute and populates
``self.details`` with structured context; the shared global handler forwards
both into the error envelope.

Codes:

* ``fx_forward_not_found`` (404) — ``fx_forward_id`` not owned / soft-deleted.
* ``fx_forward_snapshot_not_found`` (404) — ``snapshot_id`` pin invisible.
* ``fx_forward_curve_resolution_failed`` (422) — a curve ref cannot be
  materialised, a curve point cannot be translated, or the trade's
  ``base_discounting_curve`` / ``quote_discounting_curve`` names no resolved
  curve.
* ``fx_forward_fx_spot_resolution_failed`` (422) — an FX spot ref cannot be
  materialised / translated, or the trade's ``fx_spot`` names no resolved spot.
* ``fx_forward_index_resolution_failed`` (422) — an ``indices[]`` ref cannot
  be materialised.
* ``fx_forward_quote_resolution_failed`` (422) — a quote id (curve point or FX
  spot) cannot be resolved at ``as_of``.
* ``fx_forward_missing_trade_fields`` (422) — a required economic field is
  absent or invalid on the trade body.
"""

from __future__ import annotations

from typing import Any, Final
from uuid import UUID

from fastapi import HTTPException, status

FX_FORWARD_NOT_FOUND_CODE: Final[str] = "fx_forward_not_found"
FX_FORWARD_SNAPSHOT_NOT_FOUND_CODE: Final[str] = "fx_forward_snapshot_not_found"
FX_FORWARD_CURVE_RESOLUTION_FAILED_CODE: Final[str] = "fx_forward_curve_resolution_failed"
FX_FORWARD_FX_SPOT_RESOLUTION_FAILED_CODE: Final[str] = "fx_forward_fx_spot_resolution_failed"
FX_FORWARD_INDEX_RESOLUTION_FAILED_CODE: Final[str] = "fx_forward_index_resolution_failed"
FX_FORWARD_QUOTE_RESOLUTION_FAILED_CODE: Final[str] = "fx_forward_quote_resolution_failed"
FX_FORWARD_MISSING_TRADE_FIELDS_CODE: Final[str] = "fx_forward_missing_trade_fields"
_BREADCRUMB_LIMIT: Final[int] = 5


class FxForwardNotFoundError(HTTPException):
    """404: the requested ``app.fx_forwards`` row is invisible to the caller."""

    code: Final[str] = FX_FORWARD_NOT_FOUND_CODE

    def __init__(self, *, fx_forward_id: UUID | str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"FX forward {fx_forward_id} not found.",
        )


class FxForwardSnapshotNotFoundError(HTTPException):
    """404: an ``snapshot_id`` pin is invisible to the caller."""

    code: Final[str] = FX_FORWARD_SNAPSHOT_NOT_FOUND_CODE

    def __init__(self, *, snapshot_id: UUID | str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Snapshot {snapshot_id} not found.",
        )


class _Resolution422(HTTPException):
    def __init__(self, *, reason: str, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=reason)
        self.details: list[dict[str, Any]] | None = details


class FxForwardCurveResolutionFailedError(_Resolution422):
    """422: curves cannot be resolved / translated / matched to the trade's refs."""

    code: Final[str] = FX_FORWARD_CURVE_RESOLUTION_FAILED_CODE


class FxForwardFxSpotResolutionFailedError(_Resolution422):
    """422: FX spots cannot be resolved / translated / matched to the trade's ref."""

    code: Final[str] = FX_FORWARD_FX_SPOT_RESOLUTION_FAILED_CODE


class FxForwardIndexResolutionFailedError(_Resolution422):
    """422: an ``indices[]`` reference cannot be materialised."""

    code: Final[str] = FX_FORWARD_INDEX_RESOLUTION_FAILED_CODE


class FxForwardQuoteResolutionFailedError(HTTPException):
    """422: one or more quote ids cannot be resolved at ``as_of``."""

    code: Final[str] = FX_FORWARD_QUOTE_RESOLUTION_FAILED_CODE

    def __init__(self, *, missing_canonical_ids: list[str], as_of: str) -> None:
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"{len(missing_canonical_ids)} quote id(s) could not be resolved "
                f"at as_of={as_of}: {', '.join(missing_canonical_ids[:_BREADCRUMB_LIMIT])}"
                f"{'...' if len(missing_canonical_ids) > _BREADCRUMB_LIMIT else ''}"
            ),
        )
        self.details: list[dict[str, Any]] = [
            {"canonical_id": cid, "as_of": as_of} for cid in missing_canonical_ids
        ]


class FxForwardMissingTradeFieldsError(HTTPException):
    """422: a required economic field is missing / invalid on the trade body."""

    code: Final[str] = FX_FORWARD_MISSING_TRADE_FIELDS_CODE

    def __init__(self, *, missing_fields: list[str], reason: str | None = None) -> None:
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=reason
            or (
                "FX forward trade body is missing required field(s): "
                + ", ".join(missing_fields)
                + "."
            ),
        )
        self.details: list[dict[str, Any]] = [{"field": f} for f in missing_fields]


__all__ = [
    "FX_FORWARD_CURVE_RESOLUTION_FAILED_CODE",
    "FX_FORWARD_FX_SPOT_RESOLUTION_FAILED_CODE",
    "FX_FORWARD_INDEX_RESOLUTION_FAILED_CODE",
    "FX_FORWARD_MISSING_TRADE_FIELDS_CODE",
    "FX_FORWARD_NOT_FOUND_CODE",
    "FX_FORWARD_QUOTE_RESOLUTION_FAILED_CODE",
    "FX_FORWARD_SNAPSHOT_NOT_FOUND_CODE",
    "FxForwardCurveResolutionFailedError",
    "FxForwardFxSpotResolutionFailedError",
    "FxForwardIndexResolutionFailedError",
    "FxForwardMissingTradeFieldsError",
    "FxForwardNotFoundError",
    "FxForwardQuoteResolutionFailedError",
    "FxForwardSnapshotNotFoundError",
]
