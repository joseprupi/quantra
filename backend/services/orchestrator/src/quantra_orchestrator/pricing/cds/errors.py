"""structured-error HTTPExceptions for the CDS pricing path.

Each subclass pins its stable ``code`` as a class attribute and
populates ``self.details`` with structured context the route handler
already had on hand. The shared global handler in
:mod:`quantra_orchestrator.errors` honors both attributes
so the envelope emitted on the wire carries the per-product token
without per-route re-wiring.

Per the product-local codes are:

* ``cds_not_found`` (404) — ``cds_id`` not owned or soft-deleted.
* ``cds_credit_curve_not_found`` (404) — referenced ``credit_curve_id``
  row is invisible to the caller.
* ``cds_discount_curve_not_found`` (404) — referenced discount curve
  row is invisible to the caller.
* ``cds_snapshot_not_found`` (404) — ``snapshot_id`` pin is invisible
  to the caller. Ships explicitly so the snapshot-pin failure surface
  parallels swap_ir / bonds (append-only) — every product that
  accepts a ``snapshot_id`` ships one of these.
* ``cds_curve_resolution_failed`` (422) — the discount-curve
  resolution step failed (missing pricing block, inline curve
  missing ``points``, &c.). This is a
  per-bundle-stage code limited to the rates-curve family.
* ``cds_credit_curve_resolution_failed`` (422) — the credit-curve
  resolution step failed (missing ``body``, missing
  ``flat_hazard_rate``/``quotes``, malformed hazard inputs, &c.).
  Credit curves are a **distinct
  bundle type** from rates curves, so this code is intentionally
  separate from ``cds_curve_resolution_failed`` (which fires for the
  rates-side bundle) rather than collapsed with a ``role`` marker.
* ``cds_quote_resolution_failed`` (422) — one or more quote IDs on
  the resolved discount curve cannot be resolved at the requested
  ``as_of``. Wraps the MD layer's ``quote_not_found`` with
  product context. Credit curves never walk MD, so this
  code only ever fires on the rates side.

Stable codes never change once shipped; new failure modes get a new
code rather than re-using an existing one (append-only rule).
"""

from __future__ import annotations

from typing import Any, Final
from uuid import UUID

from fastapi import HTTPException, status

CDS_NOT_FOUND_CODE: Final[str] = "cds_not_found"
CDS_CREDIT_CURVE_NOT_FOUND_CODE: Final[str] = "cds_credit_curve_not_found"
CDS_DISCOUNT_CURVE_NOT_FOUND_CODE: Final[str] = "cds_discount_curve_not_found"
CDS_SNAPSHOT_NOT_FOUND_CODE: Final[str] = "cds_snapshot_not_found"
CDS_CURVE_RESOLUTION_FAILED_CODE: Final[str] = "cds_curve_resolution_failed"
CDS_CREDIT_CURVE_RESOLUTION_FAILED_CODE: Final[str] = "cds_credit_curve_resolution_failed"
CDS_QUOTE_RESOLUTION_FAILED_CODE: Final[str] = "cds_quote_resolution_failed"
CDS_MISSING_TRADE_FIELDS_CODE: Final[str] = "cds_missing_trade_fields"


class CdsNotFoundError(HTTPException):
    """404 raised when the requested CDS row is invisible to the caller.

    Mirrors the ``not_found`` semantics — soft-deleted rows / rows
    owned by a different user / rows that never existed all collapse
    to the same response so the orchestrator never leaks the existence
    of another tenant's row (north-star invariant #5).
    """

    code: Final[str] = CDS_NOT_FOUND_CODE

    def __init__(self, *, cds_id: UUID | str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"CDS {cds_id} not found.",
        )


class CdsCreditCurveNotFoundError(HTTPException):
    """404 raised when a referenced credit curve is invisible to the caller.

    Distinct from ``cds_not_found`` so clients can tell "the CDS you
    asked for doesn't exist" from "the CDS exists but the credit
    curve it references is missing or soft-deleted". Distinct from
    ``cds_discount_curve_not_found`` because credit curves and
    discount curves are different entity families (``app.credit_curves``
    vs ``app.curves``) — refinement requires a separate 404 per
    bundle type even on the not-found surface.
    """

    code: Final[str] = CDS_CREDIT_CURVE_NOT_FOUND_CODE

    def __init__(self, *, credit_curve_id: UUID | str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Credit curve {credit_curve_id} not found.",
        )


class CdsDiscountCurveNotFoundError(HTTPException):
    """404 raised when a referenced discount curve is invisible to the caller."""

    code: Final[str] = CDS_DISCOUNT_CURVE_NOT_FOUND_CODE

    def __init__(self, *, curve_id: UUID | str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Discount curve {curve_id} not found.",
        )


class CdsSnapshotNotFoundError(HTTPException):
    """404 raised when an ``snapshot_id`` pin is invisible to the caller."""

    code: Final[str] = CDS_SNAPSHOT_NOT_FOUND_CODE

    def __init__(self, *, snapshot_id: UUID | str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Snapshot {snapshot_id} not found.",
        )


class CdsCurveResolutionFailedError(HTTPException):
    """422 raised when the discount curve can't be resolved into a request-ready shape.

    Only fires for the rates-side curve family's per-bundle-
    stage scoping; credit-curve assembler failures raise
    :class:`CdsCreditCurveResolutionFailedError` instead. Carries
    structured context in ``details``: ``{"role": "discount", ...}``
    so a client can surface a "discount curve X is missing tenor Y"
    message without re-issuing the call.
    """

    code: Final[str] = CDS_CURVE_RESOLUTION_FAILED_CODE

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


class CdsCreditCurveResolutionFailedError(HTTPException):
    """422 raised when the credit curve can't be resolved into a request-ready shape.

    Distinct from :class:`CdsCurveResolutionFailedError` +
    the refinement: credit curves are a different bundle type
    from rates curves so the per-bundle-stage 422 code is **not**
    consolidated under a single ``role`` marker. Common triggers:

    * Saved credit curve row missing the ``body`` JSONB.
    * Body has neither ``flat_hazard_rate`` nor a non-empty
      ``quotes`` list with inline values.
    * Body references vendor MD quotes (the orchestrator's MD walker
      is discount-curve only; vendor refs on a credit curve fail
      fast here rather than reaching the engine).
    * Inline credit-curve override missing ``recovery_rate`` /
      ``body``.
    """

    code: Final[str] = CDS_CREDIT_CURVE_RESOLUTION_FAILED_CODE

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


class CdsQuoteResolutionFailedError(HTTPException):
    """422 raised when one or more quote IDs can't be resolved at ``as_of``.

    Wraps the MD layer's ``quote_not_found`` with product
    context: ``details`` lists every missing canonical ID + the
    requested ``as_of``. Only ever fires for discount-curve quotes
    (credit curves do not walk MD).
    """

    code: Final[str] = CDS_QUOTE_RESOLUTION_FAILED_CODE

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


class CdsMissingTradeFieldsError(HTTPException):
    """422 raised when a trade body omits required economic fields.

    Historically the wire layer silently substituted fixture defaults for
    missing economic fields (rate / notional / schedule dates) — pricing a
    trade the caller never described. Missing required trade fields are now
    a hard 422; the ``details`` payload lists each missing field so the
    client can fix its trade body. New code per the append-only rule.
    """

    code: Final[str] = CDS_MISSING_TRADE_FIELDS_CODE

    def __init__(self, *, product: str, missing_fields: list[str]) -> None:
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(f"CDS trade body is missing required field(s): {', '.join(missing_fields)}."),
        )
        self.details: list[dict[str, Any]] | None = [
            {"product": product, "field": field} for field in missing_fields
        ]


__all__ = [
    "CDS_CREDIT_CURVE_NOT_FOUND_CODE",
    "CDS_CREDIT_CURVE_RESOLUTION_FAILED_CODE",
    "CDS_CURVE_RESOLUTION_FAILED_CODE",
    "CDS_DISCOUNT_CURVE_NOT_FOUND_CODE",
    "CDS_MISSING_TRADE_FIELDS_CODE",
    "CDS_NOT_FOUND_CODE",
    "CDS_QUOTE_RESOLUTION_FAILED_CODE",
    "CDS_SNAPSHOT_NOT_FOUND_CODE",
    "CdsCreditCurveNotFoundError",
    "CdsCreditCurveResolutionFailedError",
    "CdsCurveResolutionFailedError",
    "CdsDiscountCurveNotFoundError",
    "CdsMissingTradeFieldsError",
    "CdsNotFoundError",
    "CdsQuoteResolutionFailedError",
    "CdsSnapshotNotFoundError",
]
