"""structured-error HTTPExceptions for the equity-options pricing path.

Each subclass pins its stable ``code`` as a class attribute and
populates ``self.details`` with structured context the route handler
already had on hand. The shared global handler in
:mod:`quantra_orchestrator.errors` honors both attributes
so the envelope emitted on the wire carries the per-product token
without per-route re-wiring.

Per the product-local
codes are (append-only, never re-used):

* ``equity_option_not_found`` (404) — id not owned or soft-deleted.
* ``equity_option_vol_surface_not_found`` (404) — referenced surface
  missing.
* ``equity_option_vol_surface_wrong_kind`` (422) — referenced surface
  is not ``kind = 'BlackVolSpec'`` (a rates / optionlet surface
  doesn't make sense for an equity option; the assembler refuses
  rather than letting the engine emit a confusing bootstrap error).
* ``equity_option_discount_curve_not_found`` (404) — referenced
  curve missing.
* ``equity_option_snapshot_not_found`` (404) — ``snapshot_id`` not
  owned or soft-deleted.
* ``equity_option_curve_resolution_failed`` (422) — discount or
  dividend curve cannot be resolved (refinement: same-bundle-
  type, different-role; the role lands in ``details``).
* ``equity_option_surface_resolution_failed`` (422) — vol surface
  cannot be resolved into an engine-ready shape (missing strikes /
  maturities, malformed grid, etc.).
* ``equity_option_spot_resolution_failed`` (422) — the underlier
  spot reference is malformed (e.g. neither inline value nor
  canonical id when reaching the assembler — most cases get caught
  by pydantic, but a saved-row variant can land here).
* ``equity_option_quote_resolution_failed`` (422) — one or more
  quote IDs (curve points or the spot canonical id) cannot be
  resolved at the requested ``as_of``. Wraps the MD layer's
  ``quote_not_found`` with product context.
* ``equity_option_invalid_request`` (400) — shape validation past
  pydantic (cross-field constraints).

Stable codes never change once shipped; new failure modes get a new
code rather than re-using an existing one (append-only rule).
"""

from __future__ import annotations

from typing import Any, Final
from uuid import UUID

from fastapi import HTTPException, status

EQUITY_OPTION_NOT_FOUND_CODE: Final[str] = "equity_option_not_found"
EQUITY_OPTION_VOL_SURFACE_NOT_FOUND_CODE: Final[str] = "equity_option_vol_surface_not_found"
EQUITY_OPTION_VOL_SURFACE_WRONG_KIND_CODE: Final[str] = "equity_option_vol_surface_wrong_kind"
EQUITY_OPTION_DISCOUNT_CURVE_NOT_FOUND_CODE: Final[str] = "equity_option_discount_curve_not_found"
EQUITY_OPTION_SNAPSHOT_NOT_FOUND_CODE: Final[str] = "equity_option_snapshot_not_found"
EQUITY_OPTION_CURVE_RESOLUTION_FAILED_CODE: Final[str] = "equity_option_curve_resolution_failed"
EQUITY_OPTION_SURFACE_RESOLUTION_FAILED_CODE: Final[str] = "equity_option_surface_resolution_failed"
EQUITY_OPTION_SPOT_RESOLUTION_FAILED_CODE: Final[str] = "equity_option_spot_resolution_failed"
EQUITY_OPTION_QUOTE_RESOLUTION_FAILED_CODE: Final[str] = "equity_option_quote_resolution_failed"
EQUITY_OPTION_INVALID_REQUEST_CODE: Final[str] = "equity_option_invalid_request"


class EquityOptionNotFoundError(HTTPException):
    """404 raised when the requested equity option is invisible to the caller.

    Mirrors the ``not_found`` semantics — soft-deleted rows /
    rows owned by a different user / rows that never existed all
    collapse to the same response so the orchestrator never leaks
    the existence of another tenant's row (north-star invariant #5).
    """

    code: Final[str] = EQUITY_OPTION_NOT_FOUND_CODE

    def __init__(self, *, equity_option_id: UUID | str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Equity option {equity_option_id} not found.",
        )


class EquityOptionVolSurfaceNotFoundError(HTTPException):
    """404 raised when a referenced vol surface is invisible to the caller."""

    code: Final[str] = EQUITY_OPTION_VOL_SURFACE_NOT_FOUND_CODE

    def __init__(self, *, vol_surface_id: UUID | str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Vol surface {vol_surface_id} not found.",
        )


class EquityOptionVolSurfaceWrongKindError(HTTPException):
    """422 raised when the referenced surface isn't a ``BlackVolSpec``.

    A swaption-side ``SwaptionVolSpec`` or an ``OptionletVolSpec``
    can't drive an equity-option pricing call; failing fast with a
    distinct code lets the frontend show the user a "this surface
    isn't an equity surface" hint without parsing strings.
    """

    code: Final[str] = EQUITY_OPTION_VOL_SURFACE_WRONG_KIND_CODE

    def __init__(
        self,
        *,
        vol_surface_id: UUID | str,
        actual_kind: str,
    ) -> None:
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Vol surface {vol_surface_id} has kind={actual_kind!r}; "
                "equity options require kind='BlackVolSpec'."
            ),
        )
        self.details: list[dict[str, Any]] | None = [
            {
                "vol_surface_id": str(vol_surface_id),
                "actual_kind": actual_kind,
                "expected_kind": "BlackVolSpec",
            }
        ]


class EquityOptionDiscountCurveNotFoundError(HTTPException):
    """404 raised when a referenced discount curve is invisible to the caller.

    The plan reserves a single code for the curve-family 404 surface
    even though the equity-options endpoint loads two curves
    (discount + dividend); the role lands in ``details``
    refinement so a missing dividend curve and a missing discount
    curve don't need separate codes.
    """

    code: Final[str] = EQUITY_OPTION_DISCOUNT_CURVE_NOT_FOUND_CODE

    def __init__(
        self,
        *,
        curve_id: UUID | str,
        role: str = "discount",
    ) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Curve {curve_id} not found.",
        )
        self.details: list[dict[str, Any]] | None = [{"curve_id": str(curve_id), "role": role}]


class EquityOptionSnapshotNotFoundError(HTTPException):
    """404 raised when an ``snapshot_id`` pin is invisible to the caller."""

    code: Final[str] = EQUITY_OPTION_SNAPSHOT_NOT_FOUND_CODE

    def __init__(self, *, snapshot_id: UUID | str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Snapshot {snapshot_id} not found.",
        )


class EquityOptionCurveResolutionFailedError(HTTPException):
    """422 raised when a curve can't be resolved into a request-ready shape.

    The discount-curve and dividend-curve
    failures share this code; the role lands in ``details`` so the
    frontend can branch on which curve failed without parsing the
    error string.
    """

    code: Final[str] = EQUITY_OPTION_CURVE_RESOLUTION_FAILED_CODE

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


class EquityOptionSurfaceResolutionFailedError(HTTPException):
    """422 raised when the equity vol surface can't be resolved.

    Common triggers: an inline payload with neither
    ``base.constant_vol`` nor any quote IDs, a saved surface whose
    body is empty, a price-grid surface missing
    ``price_strikes`` / ``price_expiries``.
    """

    code: Final[str] = EQUITY_OPTION_SURFACE_RESOLUTION_FAILED_CODE

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


class EquityOptionSpotResolutionFailedError(HTTPException):
    """422 raised when the underlier spot reference is malformed.

    The pydantic validator catches the obvious "neither inline nor
    canonical_id" branch; this code covers downstream cases where
    a saved-row spot block reaches the assembler with a malformed
    body (e.g. canonical id present but blank).
    """

    code: Final[str] = EQUITY_OPTION_SPOT_RESOLUTION_FAILED_CODE

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


class EquityOptionQuoteResolutionFailedError(HTTPException):
    """422 raised when one or more quote IDs can't be resolved at ``as_of``.

    Wraps the MD layer's ``quote_not_found`` with product
    context: ``details`` lists every missing canonical ID + the
    requested ``as_of`` so the client can either patch its quote
    IDs or pin a different snapshot.
    """

    code: Final[str] = EQUITY_OPTION_QUOTE_RESOLUTION_FAILED_CODE

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


class EquityOptionInvalidRequestError(HTTPException):
    """400 raised when the request shape past pydantic still isn't valid."""

    code: Final[str] = EQUITY_OPTION_INVALID_REQUEST_CODE

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


__all__ = [
    "EQUITY_OPTION_CURVE_RESOLUTION_FAILED_CODE",
    "EQUITY_OPTION_DISCOUNT_CURVE_NOT_FOUND_CODE",
    "EQUITY_OPTION_INVALID_REQUEST_CODE",
    "EQUITY_OPTION_NOT_FOUND_CODE",
    "EQUITY_OPTION_QUOTE_RESOLUTION_FAILED_CODE",
    "EQUITY_OPTION_SNAPSHOT_NOT_FOUND_CODE",
    "EQUITY_OPTION_SPOT_RESOLUTION_FAILED_CODE",
    "EQUITY_OPTION_SURFACE_RESOLUTION_FAILED_CODE",
    "EQUITY_OPTION_VOL_SURFACE_NOT_FOUND_CODE",
    "EQUITY_OPTION_VOL_SURFACE_WRONG_KIND_CODE",
    "EquityOptionCurveResolutionFailedError",
    "EquityOptionDiscountCurveNotFoundError",
    "EquityOptionInvalidRequestError",
    "EquityOptionNotFoundError",
    "EquityOptionQuoteResolutionFailedError",
    "EquityOptionSnapshotNotFoundError",
    "EquityOptionSpotResolutionFailedError",
    "EquityOptionSurfaceResolutionFailedError",
    "EquityOptionVolSurfaceNotFoundError",
    "EquityOptionVolSurfaceWrongKindError",
]
