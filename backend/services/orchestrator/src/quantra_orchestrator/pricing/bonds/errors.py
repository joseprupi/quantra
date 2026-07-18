"""structured-error HTTPExceptions for the bonds pricing path (fixed + floating).

Each subclass pins its stable ``code`` as a class attribute and
populates ``self.details`` with structured context the route handler
already had on hand. The shared global handler in
:mod:`quantra_orchestrator.errors` honors both attributes
so the envelope emitted on the wire carries the per-product token
without per-route re-wiring.

The product-local codes are:

* ``bond_fixed_not_found`` (404) — ``bond_id`` on the fixed route is
  not owned or has been soft-deleted.
* ``bond_floating_not_found`` (404) — same shape for floating.
* ``bond_discount_curve_not_found`` (404) — referenced discount-curve
  row is invisible to the caller. Shared between fixed and floating.
* ``bond_projection_curve_not_found`` (404) — referenced projection-
  curve row is invisible (floating-only).
* ``bond_index_not_found`` (404) — referenced ``app.indices`` row is
  invisible (floating-only).
* ``bond_curve_resolution_failed`` (422) — the assembler cannot
  decide which curve(s) to use (missing pricing block, missing
  required curve role, inline curve missing ``points``, &c.). This
  is the single per-bundle-stage 422 code shared between
  the discount-curve and projection-curve resolution steps; the
  ``details`` payload carries which role the failure happened in.
* ``bond_quote_resolution_failed`` (422) — one or more quote IDs
  on the resolved curves cannot be resolved at the requested
  ``as_of``. Wraps the MD layer's ``quote_not_found`` with
  product context.
  roles that can't be mapped onto the supplied ``curves`` list).

There is **no separate** ``bond_snapshot_not_found`` code. The shared
``snapshot_id`` lookup raises a 404 with the
``bond_snapshot_not_found`` code below; it is documented here under
"shared between fixed + floating" because the same exception fires
for either route.

``bond_snapshot_not_found`` ships explicitly so both routes
round-trip the four canonical 404 surfaces (self, discount curve,
projection curve, snapshot) and the floating route adds
index_not_found on top — matching the swap_ir / swaption precedent.

Stable codes never change once shipped; new failure modes get a new
code rather than re-using an existing one (append-only rule).
"""

from __future__ import annotations

from typing import Any, Final
from uuid import UUID

from fastapi import HTTPException, status

BOND_FIXED_NOT_FOUND_CODE: Final[str] = "bond_fixed_not_found"
BOND_FLOATING_NOT_FOUND_CODE: Final[str] = "bond_floating_not_found"
BOND_DISCOUNT_CURVE_NOT_FOUND_CODE: Final[str] = "bond_discount_curve_not_found"
BOND_PROJECTION_CURVE_NOT_FOUND_CODE: Final[str] = "bond_projection_curve_not_found"
BOND_INDEX_NOT_FOUND_CODE: Final[str] = "bond_index_not_found"
BOND_SNAPSHOT_NOT_FOUND_CODE: Final[str] = "bond_snapshot_not_found"
BOND_CURVE_RESOLUTION_FAILED_CODE: Final[str] = "bond_curve_resolution_failed"
BOND_QUOTE_RESOLUTION_FAILED_CODE: Final[str] = "bond_quote_resolution_failed"
BOND_MISSING_TRADE_FIELDS_CODE: Final[str] = "bond_missing_trade_fields"


class BondFixedNotFoundError(HTTPException):
    """404 raised when the requested fixed-rate bond row is invisible to the caller.

    Mirrors the ``not_found`` semantics — soft-deleted rows / rows
    owned by a different user / rows that never existed all collapse
    to the same response so the orchestrator never leaks the existence
    of another tenant's row (north-star invariant #5).
    """

    code: Final[str] = BOND_FIXED_NOT_FOUND_CODE

    def __init__(self, *, bond_id: UUID | str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Fixed-rate bond {bond_id} not found.",
        )


class BondFloatingNotFoundError(HTTPException):
    """404 raised when the requested floating-rate bond row is invisible."""

    code: Final[str] = BOND_FLOATING_NOT_FOUND_CODE

    def __init__(self, *, bond_id: UUID | str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Floating-rate bond {bond_id} not found.",
        )


class BondDiscountCurveNotFoundError(HTTPException):
    """404 raised when a referenced discount curve is invisible.

    Distinct from ``bond_*_not_found`` so clients can tell "the bond
    you asked for doesn't exist" from "the bond exists but the curve
    it references is missing or soft-deleted". Shared between the
    fixed and floating routes — both consume one discount curve.
    """

    code: Final[str] = BOND_DISCOUNT_CURVE_NOT_FOUND_CODE

    def __init__(self, *, curve_id: UUID | str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Discount curve {curve_id} not found.",
        )


class BondProjectionCurveNotFoundError(HTTPException):
    """404 raised when a referenced projection curve is invisible (floating-only)."""

    code: Final[str] = BOND_PROJECTION_CURVE_NOT_FOUND_CODE

    def __init__(self, *, curve_id: UUID | str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Projection curve {curve_id} not found.",
        )


class BondIndexNotFoundError(HTTPException):
    """404 raised when a referenced ``app.indices`` row is invisible (floating-only)."""

    code: Final[str] = BOND_INDEX_NOT_FOUND_CODE

    def __init__(self, *, index_id: UUID | str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Index {index_id} not found.",
        )


class BondSnapshotNotFoundError(HTTPException):
    """404 raised when an ``snapshot_id`` pin is invisible to the caller."""

    code: Final[str] = BOND_SNAPSHOT_NOT_FOUND_CODE

    def __init__(self, *, snapshot_id: UUID | str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Snapshot {snapshot_id} not found.",
        )


class BondCurveResolutionFailedError(HTTPException):
    """422 raised when curve(s) can't be resolved into a request-ready shape.

    This single code covers both the discount-curve and
    projection-curve resolution stages — operators read ``details``
    to learn which role failed and why. Common triggers:

    * Saved bond missing the ``pricing`` block.
    * Saved bond has neither ``discount_curve_id`` nor inline
      ``discount_curve``.
    * Floating bond has discount curve but no ``forecast_curve_id``
      and no inline projection curve.
    * Inline curve missing ``name`` / ``points``.
    * A required curve ref points at a soft-deleted ``app.curves``
      row (the bond_*_curve_not_found 404 fires for invisible rows;
      this 422 fires for "I have a row but its shape is unusable").
    """

    code: Final[str] = BOND_CURVE_RESOLUTION_FAILED_CODE

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


class BondQuoteResolutionFailedError(HTTPException):
    """422 raised when one or more quote IDs can't be resolved at ``as_of``.

    Wraps the MD layer's ``quote_not_found`` with product
    context: ``details`` lists every missing canonical ID + the
    requested ``as_of`` so the client can either patch its quote IDs
    or pin a different snapshot.
    """

    code: Final[str] = BOND_QUOTE_RESOLUTION_FAILED_CODE

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


class BondMissingTradeFieldsError(HTTPException):
    """422 raised when a trade body omits required economic fields.

    Historically the wire layer silently substituted fixture defaults
    (5 % coupon, a 2024→2029 schedule) for a missing ``coupon_rate`` /
    ``issue_date`` / ``effective_date`` / ``termination_date`` — a
    silent-default-class silent default that priced a bond the caller never
    described. Missing required trade fields are now a hard 422; the
    ``details`` payload lists each missing field so the client can fix
    its trade body. New code per the append-only rule.
    """

    code: Final[str] = BOND_MISSING_TRADE_FIELDS_CODE

    def __init__(self, *, product: str, missing_fields: list[str]) -> None:
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(f"Bond trade body is missing required field(s): {', '.join(missing_fields)}."),
        )
        self.details: list[dict[str, Any]] | None = [
            {"product": product, "field": field} for field in missing_fields
        ]


__all__ = [
    "BOND_CURVE_RESOLUTION_FAILED_CODE",
    "BOND_DISCOUNT_CURVE_NOT_FOUND_CODE",
    "BOND_FIXED_NOT_FOUND_CODE",
    "BOND_FLOATING_NOT_FOUND_CODE",
    "BOND_INDEX_NOT_FOUND_CODE",
    "BOND_MISSING_TRADE_FIELDS_CODE",
    "BOND_PROJECTION_CURVE_NOT_FOUND_CODE",
    "BOND_QUOTE_RESOLUTION_FAILED_CODE",
    "BOND_SNAPSHOT_NOT_FOUND_CODE",
    "BondCurveResolutionFailedError",
    "BondDiscountCurveNotFoundError",
    "BondFixedNotFoundError",
    "BondFloatingNotFoundError",
    "BondIndexNotFoundError",
    "BondMissingTradeFieldsError",
    "BondProjectionCurveNotFoundError",
    "BondQuoteResolutionFailedError",
    "BondSnapshotNotFoundError",
]
