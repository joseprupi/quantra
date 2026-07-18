"""structured-error HTTPExceptions for the swaption pricing path.

Each subclass pins its stable ``code`` as a class attribute and
populates ``self.details`` with structured context the route handler
already had on hand. The shared global handler in
:mod:`quantra_orchestrator.errors` honors both attributes
so the envelope emitted on the wire carries the per-product token
without per-route re-wiring.

The product-local codes are:

* ``swaption_not_found`` (404) — ``swaption_id`` not owned or soft-deleted.
* ``swaption_curve_set_not_found`` (404) — referenced ``curve_set_id``
  not owned or soft-deleted.
* ``swaption_vol_surface_not_found`` (404) — referenced
  ``vol_surface_id`` not owned or soft-deleted.
* ``swaption_model_not_found`` (404) — referenced ``swaption_model_id``
  not owned or soft-deleted.
* ``swaption_snapshot_not_found`` (404) — ``snapshot_id`` not owned
  or soft-deleted.
* ``swaption_curve_resolution_failed`` (422) — inline / referenced
  curves cannot be resolved (e.g. a curve ref points at a soft-
  deleted row, a required tenor is missing on an inline definition).
* ``swaption_surface_resolution_failed`` (422) — the vol surface
  cannot be resolved into an engine-ready shape (missing strikes /
  tenors, malformed grid, etc.). Distinct from
  ``swaption_curve_resolution_failed`` so frontend retry / fallback
  logic can branch on which resolved input failed.
* ``swaption_quote_resolution_failed`` (422) — one or more quote IDs
  in the resolved curves / vol surface cannot be resolved at the
  requested ``as_of`` (wraps the MD layer's ``quote_not_found`` with
  product context so clients see the swaption context,
  not a bare MD code).
* ``swaption_invalid_request`` (400) — shape validation past pydantic
  (e.g. inconsistent dates, mutually exclusive fields populated).

Stable codes never change once shipped; new failure modes get a new
code rather than re-using an existing one (append-only rule).
"""

from __future__ import annotations

from typing import Any, Final
from uuid import UUID

from fastapi import HTTPException, status

SWAPTION_NOT_FOUND_CODE: Final[str] = "swaption_not_found"
SWAPTION_CURVE_SET_NOT_FOUND_CODE: Final[str] = "swaption_curve_set_not_found"
SWAPTION_VOL_SURFACE_NOT_FOUND_CODE: Final[str] = "swaption_vol_surface_not_found"
SWAPTION_MODEL_NOT_FOUND_CODE: Final[str] = "swaption_model_not_found"
SWAPTION_SNAPSHOT_NOT_FOUND_CODE: Final[str] = "swaption_snapshot_not_found"
SWAPTION_CURVE_RESOLUTION_FAILED_CODE: Final[str] = "swaption_curve_resolution_failed"
SWAPTION_SURFACE_RESOLUTION_FAILED_CODE: Final[str] = "swaption_surface_resolution_failed"
SWAPTION_QUOTE_RESOLUTION_FAILED_CODE: Final[str] = "swaption_quote_resolution_failed"
SWAPTION_INDEX_NOT_FOUND_CODE: Final[str] = "swaption_index_not_found"
SWAPTION_INDEX_RESOLUTION_FAILED_CODE: Final[str] = "swaption_index_resolution_failed"
SWAPTION_INVALID_REQUEST_CODE: Final[str] = "swaption_invalid_request"
SWAPTION_MISSING_TRADE_FIELDS_CODE: Final[str] = "swaption_missing_trade_fields"


class SwaptionNotFoundError(HTTPException):
    """404 raised when the requested swaption row is invisible to the caller.

    Mirrors the ``not_found`` semantics — soft-deleted rows / rows
    owned by a different user / rows that never existed all collapse
    to the same response so the orchestrator never leaks the existence
    of another tenant's row (north-star invariant #5).
    """

    code: Final[str] = SWAPTION_NOT_FOUND_CODE

    def __init__(self, *, swaption_id: UUID | str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Swaption {swaption_id} not found.",
        )


class SwaptionCurveSetNotFoundError(HTTPException):
    """404 raised when a referenced curve set is invisible to the caller."""

    code: Final[str] = SWAPTION_CURVE_SET_NOT_FOUND_CODE

    def __init__(self, *, curve_set_id: UUID | str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Curve set {curve_set_id} not found.",
        )


class SwaptionVolSurfaceNotFoundError(HTTPException):
    """404 raised when a referenced vol surface is invisible to the caller.

    Distinct from ``swaption_curve_set_not_found`` so clients can tell
    the two different missing-reference cases apart without parsing
    the error string.
    """

    code: Final[str] = SWAPTION_VOL_SURFACE_NOT_FOUND_CODE

    def __init__(self, *, vol_surface_id: UUID | str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Vol surface {vol_surface_id} not found.",
        )


class SwaptionModelNotFoundError(HTTPException):
    """404 raised when a referenced swaption model is invisible to the caller."""

    code: Final[str] = SWAPTION_MODEL_NOT_FOUND_CODE

    def __init__(self, *, swaption_model_id: UUID | str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Swaption model {swaption_model_id} not found.",
        )


class SwaptionSnapshotNotFoundError(HTTPException):
    """404 raised when an ``snapshot_id`` pin is invisible to the caller."""

    code: Final[str] = SWAPTION_SNAPSHOT_NOT_FOUND_CODE

    def __init__(self, *, snapshot_id: UUID | str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Snapshot {snapshot_id} not found.",
        )


class SwaptionCurveResolutionFailedError(HTTPException):
    """422 raised when curves can't be resolved into a request-ready shape.

    Carries the per-curve failure list in ``details`` so a client can
    surface a "curve X is missing tenor Y" message without re-issuing
    the call. Each item in ``details`` is
    ``{"curve_id"|"curve_index": ..., "reason": "..."}``.
    """

    code: Final[str] = SWAPTION_CURVE_RESOLUTION_FAILED_CODE

    def __init__(
        self,
        *,
        reason: str,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=reason,
        )
        self.details: list[dict[str, Any]] | None = details


class SwaptionSurfaceResolutionFailedError(HTTPException):
    """422 raised when the vol surface can't be resolved into a request-ready shape.

    The surface-side analogue of ``swaption_curve_resolution_failed``.
    Common triggers: an inline payload missing required axes
    (``axes_expiries`` / ``axes_tenors``), a saved surface with no
    grid + no SABR params, a SABR cube whose ``axes_strikes`` length
    doesn't match the data tensor.

    Future products with their own surface-shaped inputs (cap/floor
    vol, equity vol surfaces) follow the same naming convention.
    """

    code: Final[str] = SWAPTION_SURFACE_RESOLUTION_FAILED_CODE

    def __init__(
        self,
        *,
        reason: str,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=reason,
        )
        self.details: list[dict[str, Any]] | None = details


class SwaptionQuoteResolutionFailedError(HTTPException):
    """422 raised when one or more quote IDs can't be resolved at ``as_of``.

    Wraps the MD layer's ``quote_not_found`` with product
    context: ``details`` lists every missing canonical ID + the
    requested ``as_of`` so the client can either patch its quote IDs
    or pin a different snapshot.
    """

    code: Final[str] = SWAPTION_QUOTE_RESOLUTION_FAILED_CODE

    def __init__(
        self,
        *,
        missing_canonical_ids: list[str],
        as_of: str,
    ) -> None:
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Failed to resolve {len(missing_canonical_ids)} quote ID(s) at as_of={as_of}."
            ),
        )
        self.details: list[dict[str, Any]] | None = [
            {"canonical_id": cid, "as_of": as_of} for cid in missing_canonical_ids
        ]


class SwaptionIndexNotFoundError(HTTPException):
    """404 raised when a referenced underlying float index is invisible to the caller.

    The index-side analogue of ``swaption_vol_surface_not_found``:
    ``request.index`` carried an ``id`` that resolves to no live
    ``app.indices`` row for this owner.
    """

    code: Final[str] = SWAPTION_INDEX_NOT_FOUND_CODE

    def __init__(self, *, index_id: UUID | str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Index {index_id} not found.",
        )


class SwaptionIndexResolutionFailedError(HTTPException):
    """422 raised when the supplied underlying float index can't be resolved.

    Triggers: an inline ``index`` missing ``kind`` / ``body``. Distinct
    from ``swaption_curve_resolution_failed`` so a client can tell which
    resolved input failed.
    """

    code: Final[str] = SWAPTION_INDEX_RESOLUTION_FAILED_CODE

    def __init__(
        self,
        *,
        reason: str,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=reason,
        )
        self.details: list[dict[str, Any]] | None = details


class SwaptionInvalidRequestError(HTTPException):
    """400 raised when the request shape past pydantic still isn't valid.

    Reserved for cross-field validation the per-field pydantic
    declarations can't express in isolation (e.g. an inline swaption
    body referencing a vol surface that wasn't declared inline).
    """

    code: Final[str] = SWAPTION_INVALID_REQUEST_CODE

    def __init__(
        self,
        *,
        reason: str,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=reason,
        )
        self.details: list[dict[str, Any]] | None = details


class SwaptionMissingTradeFieldsError(HTTPException):
    """422 raised when a trade body omits required economic fields.

    Historically the wire layer silently substituted fixture defaults for
    missing economic fields (rate / notional / schedule dates) — pricing a
    trade the caller never described. Missing required trade fields are now
    a hard 422; the ``details`` payload lists each missing field so the
    client can fix its trade body. New code per the append-only rule.
    """

    code: Final[str] = SWAPTION_MISSING_TRADE_FIELDS_CODE

    def __init__(self, *, product: str, missing_fields: list[str]) -> None:
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Swaption trade body is missing required field(s): {', '.join(missing_fields)}."
            ),
        )
        self.details: list[dict[str, Any]] | None = [
            {"product": product, "field": field} for field in missing_fields
        ]


__all__ = [
    "SWAPTION_CURVE_RESOLUTION_FAILED_CODE",
    "SWAPTION_CURVE_SET_NOT_FOUND_CODE",
    "SWAPTION_INDEX_NOT_FOUND_CODE",
    "SWAPTION_INDEX_RESOLUTION_FAILED_CODE",
    "SWAPTION_INVALID_REQUEST_CODE",
    "SWAPTION_MISSING_TRADE_FIELDS_CODE",
    "SWAPTION_MODEL_NOT_FOUND_CODE",
    "SWAPTION_NOT_FOUND_CODE",
    "SWAPTION_QUOTE_RESOLUTION_FAILED_CODE",
    "SWAPTION_SNAPSHOT_NOT_FOUND_CODE",
    "SWAPTION_SURFACE_RESOLUTION_FAILED_CODE",
    "SWAPTION_VOL_SURFACE_NOT_FOUND_CODE",
    "SwaptionCurveResolutionFailedError",
    "SwaptionCurveSetNotFoundError",
    "SwaptionIndexNotFoundError",
    "SwaptionIndexResolutionFailedError",
    "SwaptionInvalidRequestError",
    "SwaptionMissingTradeFieldsError",
    "SwaptionModelNotFoundError",
    "SwaptionNotFoundError",
    "SwaptionQuoteResolutionFailedError",
    "SwaptionSnapshotNotFoundError",
    "SwaptionSurfaceResolutionFailedError",
    "SwaptionVolSurfaceNotFoundError",
]
