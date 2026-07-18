"""structured-error HTTPExceptions for the inflation-swap pricing path.

Each subclass pins its stable ``code`` as a class attribute and
populates ``self.details`` with structured context the route handler
already had on hand. The shared global handler in
:mod:`quantra_orchestrator.errors` honors both attributes
so the envelope emitted on the wire carries the per-product token
without per-route re-wiring.

Per and the per-entity-family
refinement (08_bonds.md), the product-local codes are (append-
only, never re-used):

* ``swap_inflation_not_found`` (404) — id not owned or soft-deleted.
* ``swap_inflation_nominal_curve_not_found`` (404) — nominal-curve
  ref missing.
* ``swap_inflation_inflation_curve_not_found`` (404) —
  inflation-curve ref missing.
* ``swap_inflation_index_not_found`` (404) — inflation-index ref
  missing or has the wrong ``kind``.
* ``swap_inflation_snapshot_not_found`` (404) — ``snapshot_id`` not
  owned or soft-deleted.
* ``swap_inflation_curve_resolution_failed`` (422) — either curve
  cannot be resolved into an engine-ready shape (refinement:
  same code for nominal + inflation curves; the role lands in
  ``details`` as ``{"role": "nominal"}`` or
  ``{"role": "inflation"}``). The plan's table reserves ONE shared
  code for the curve family per the explicit hard constraint
  ("the plan ships ONE shared swap_inflation_curve_resolution_failed
  for either curve").
* ``swap_inflation_index_resolution_failed`` (422) — the inflation
  index cannot be resolved into an engine-ready shape (missing
  ``index_id`` / ``frequency`` / fixings; saved row body is empty;
  inline body lacks the required engine conventions). The
  index is its own bundle stage and gets its own 422 — distinct
  from the curve code.
* ``swap_inflation_quote_resolution_failed`` (422) — one or more
  quote IDs (curve points across BOTH curves) cannot be resolved at
  the requested ``as_of``. Wraps the MD layer's ``quote_not_found``
 with product context.
* ``swap_inflation_invalid_request`` (400) — shape validation past
  pydantic (cross-field constraints).

Stable codes never change once shipped; new failure modes get a new
code rather than re-using an existing one (append-only rule).
"""

from __future__ import annotations

from typing import Any, Final
from uuid import UUID

from fastapi import HTTPException, status

SWAP_INFLATION_NOT_FOUND_CODE: Final[str] = "swap_inflation_not_found"
SWAP_INFLATION_NOMINAL_CURVE_NOT_FOUND_CODE: Final[str] = "swap_inflation_nominal_curve_not_found"
SWAP_INFLATION_INFLATION_CURVE_NOT_FOUND_CODE: Final[str] = (
    "swap_inflation_inflation_curve_not_found"
)
SWAP_INFLATION_INDEX_NOT_FOUND_CODE: Final[str] = "swap_inflation_index_not_found"
SWAP_INFLATION_SNAPSHOT_NOT_FOUND_CODE: Final[str] = "swap_inflation_snapshot_not_found"
SWAP_INFLATION_CURVE_RESOLUTION_FAILED_CODE: Final[str] = "swap_inflation_curve_resolution_failed"
SWAP_INFLATION_INDEX_RESOLUTION_FAILED_CODE: Final[str] = "swap_inflation_index_resolution_failed"
SWAP_INFLATION_QUOTE_RESOLUTION_FAILED_CODE: Final[str] = "swap_inflation_quote_resolution_failed"
SWAP_INFLATION_INVALID_REQUEST_CODE: Final[str] = "swap_inflation_invalid_request"
SWAP_INFLATION_MISSING_TRADE_FIELDS_CODE: Final[str] = "swap_inflation_missing_trade_fields"


class SwapInflationNotFoundError(HTTPException):
    """404 raised when the requested inflation swap is invisible to the caller.

    Mirrors the ``not_found`` semantics — soft-deleted rows /
    rows owned by a different user / rows that never existed all
    collapse to the same response so the orchestrator never leaks
    the existence of another tenant's row (north-star invariant #5).
    """

    code: Final[str] = SWAP_INFLATION_NOT_FOUND_CODE

    def __init__(self, *, swap_id: UUID | str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Inflation swap {swap_id} not found.",
        )


class SwapInflationNominalCurveNotFoundError(HTTPException):
    """404 raised when a referenced nominal discount curve is invisible.

    Distinct from the inflation-curve 404 because the role tag is
    different and operators should be able to branch on which
    curve the user has to fix without parsing the error string.
    """

    code: Final[str] = SWAP_INFLATION_NOMINAL_CURVE_NOT_FOUND_CODE

    def __init__(self, *, curve_id: UUID | str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Nominal curve {curve_id} not found.",
        )
        self.details: list[dict[str, Any]] | None = [{"curve_id": str(curve_id), "role": "nominal"}]


class SwapInflationInflationCurveNotFoundError(HTTPException):
    """404 raised when a referenced inflation curve is invisible to the caller."""

    code: Final[str] = SWAP_INFLATION_INFLATION_CURVE_NOT_FOUND_CODE

    def __init__(self, *, curve_id: UUID | str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Inflation curve {curve_id} not found.",
        )
        self.details: list[dict[str, Any]] | None = [
            {"curve_id": str(curve_id), "role": "inflation"}
        ]


class SwapInflationIndexNotFoundError(HTTPException):
    """404 raised when a referenced inflation index is invisible / wrong kind.

    Soft-deleted rows / rows owned by a different user / rows whose
    ``app.indices.kind`` is not ``'Inflation'`` collapse to the same
    response. The wrong-kind branch carries ``actual_kind`` in
    ``details`` so an operator who pointed at a rates index by
    accident gets actionable feedback.
    """

    code: Final[str] = SWAP_INFLATION_INDEX_NOT_FOUND_CODE

    def __init__(
        self,
        *,
        index_id: UUID | str,
        actual_kind: str | None = None,
    ) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Inflation index {index_id} not found.",
        )
        details: list[dict[str, Any]] = [{"inflation_index_id": str(index_id)}]
        if actual_kind is not None:
            details[0]["actual_kind"] = actual_kind
            details[0]["expected_kind"] = "Inflation"
        self.details: list[dict[str, Any]] | None = details


class SwapInflationSnapshotNotFoundError(HTTPException):
    """404 raised when an ``snapshot_id`` pin is invisible to the caller."""

    code: Final[str] = SWAP_INFLATION_SNAPSHOT_NOT_FOUND_CODE

    def __init__(self, *, snapshot_id: UUID | str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Snapshot {snapshot_id} not found.",
        )


class SwapInflationCurveResolutionFailedError(HTTPException):
    """422 raised when either curve can't be resolved into a request-ready shape.

    The nominal-curve and inflation-curve
    resolution failures share this code; the role lands in
    ``details`` so the frontend can branch on which curve failed
    without parsing the error string.
    """

    code: Final[str] = SWAP_INFLATION_CURVE_RESOLUTION_FAILED_CODE

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


class SwapInflationIndexResolutionFailedError(HTTPException):
    """422 raised when the inflation index can't be resolved.

    Common triggers: a saved index row whose body is missing the
    engine-side conventions (``frequency`` / ``observation_lag`` /
    ``calendar``); an inline ref that omits both ``name`` and
    ``index_id``; a saved row whose ``body`` JSONB is not an object.
    The index is its own bundle stage so it has a code
    distinct from the curve resolution failure.
    """

    code: Final[str] = SWAP_INFLATION_INDEX_RESOLUTION_FAILED_CODE

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


class SwapInflationQuoteResolutionFailedError(HTTPException):
    """422 raised when one or more quote IDs can't be resolved at ``as_of``.

    Wraps the MD layer's ``quote_not_found`` with product
    context: ``details`` lists every missing canonical ID + the
    requested ``as_of`` so the client can either patch its quote
    IDs or pin a different snapshot. Quotes can come from BOTH
    curves (nominal + inflation) per the single-walker contract;
    the error doesn't separate the two — operators can identify
    the offending side from the canonical-id prefix.
    """

    code: Final[str] = SWAP_INFLATION_QUOTE_RESOLUTION_FAILED_CODE

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


class SwapInflationInvalidRequestError(HTTPException):
    """400 raised when the request shape past pydantic still isn't valid."""

    code: Final[str] = SWAP_INFLATION_INVALID_REQUEST_CODE

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


class SwapInflationMissingTradeFieldsError(HTTPException):
    """422 raised when a trade body omits required economic fields.

    Historically the wire layer silently substituted fixture defaults for
    missing economic fields (rate / notional / schedule dates) — pricing a
    trade the caller never described. Missing required trade fields are now
    a hard 422; the ``details`` payload lists each missing field so the
    client can fix its trade body. New code per the append-only rule.
    """

    code: Final[str] = SWAP_INFLATION_MISSING_TRADE_FIELDS_CODE

    def __init__(self, *, product: str, missing_fields: list[str]) -> None:
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Inflation swap trade body is missing required field(s): "
                f"{', '.join(missing_fields)}."
            ),
        )
        self.details: list[dict[str, Any]] | None = [
            {"product": product, "field": field} for field in missing_fields
        ]


__all__ = [
    "SWAP_INFLATION_CURVE_RESOLUTION_FAILED_CODE",
    "SWAP_INFLATION_INDEX_NOT_FOUND_CODE",
    "SWAP_INFLATION_INDEX_RESOLUTION_FAILED_CODE",
    "SWAP_INFLATION_INFLATION_CURVE_NOT_FOUND_CODE",
    "SWAP_INFLATION_INVALID_REQUEST_CODE",
    "SWAP_INFLATION_MISSING_TRADE_FIELDS_CODE",
    "SWAP_INFLATION_NOMINAL_CURVE_NOT_FOUND_CODE",
    "SWAP_INFLATION_NOT_FOUND_CODE",
    "SWAP_INFLATION_QUOTE_RESOLUTION_FAILED_CODE",
    "SWAP_INFLATION_SNAPSHOT_NOT_FOUND_CODE",
    "SwapInflationCurveResolutionFailedError",
    "SwapInflationIndexNotFoundError",
    "SwapInflationIndexResolutionFailedError",
    "SwapInflationInflationCurveNotFoundError",
    "SwapInflationInvalidRequestError",
    "SwapInflationMissingTradeFieldsError",
    "SwapInflationNominalCurveNotFoundError",
    "SwapInflationNotFoundError",
    "SwapInflationQuoteResolutionFailedError",
    "SwapInflationSnapshotNotFoundError",
]
