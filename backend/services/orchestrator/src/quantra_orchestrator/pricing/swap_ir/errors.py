"""structured-error HTTPExceptions for the IR-swap pricing path.

Each subclass pins its stable ``code`` as a class attribute and
populates ``self.details`` with structured context the route handler
already had on hand. The shared global handler in
:mod:`quantra_orchestrator.errors` honors both attributes so the
envelope emitted on the wire carries the per-product token without
per-route re-wiring.

The product-local codes are:

* ``swap_ir_not_found`` (404) — ``swap_id`` not owned or soft-deleted.
* ``swap_ir_curve_set_not_found`` (404) — referenced ``curve_set_id``
  not owned or soft-deleted.
* ``swap_ir_snapshot_not_found`` (404) — ``snapshot_id`` not owned
  or soft-deleted.
* ``swap_ir_curve_resolution_failed`` (422) — inline / referenced
  curves cannot be resolved (e.g. a curve ref points at a soft-
  deleted row, a required tenor is missing on an inline definition).
* ``swap_ir_quote_resolution_failed`` (422) — one or more quote IDs
  in the resolved curves cannot be resolved at the requested ``as_of``
  (wraps the MD layer's ``quote_not_found`` from the MD layer with product
  context so clients see the swap context, not a bare MD code).

Stable codes never change once shipped; new failure modes get a new
code rather than re-using an existing one.
"""

from __future__ import annotations

from typing import Any, Final
from uuid import UUID

from fastapi import HTTPException, status

SWAP_IR_NOT_FOUND_CODE: Final[str] = "swap_ir_not_found"
SWAP_IR_CURVE_SET_NOT_FOUND_CODE: Final[str] = "swap_ir_curve_set_not_found"
SWAP_IR_SNAPSHOT_NOT_FOUND_CODE: Final[str] = "swap_ir_snapshot_not_found"
SWAP_IR_CURVE_RESOLUTION_FAILED_CODE: Final[str] = "swap_ir_curve_resolution_failed"
SWAP_IR_QUOTE_RESOLUTION_FAILED_CODE: Final[str] = "swap_ir_quote_resolution_failed"
SWAP_IR_MISSING_TRADE_FIELDS_CODE: Final[str] = "swap_ir_missing_trade_fields"


class SwapIrNotFoundError(HTTPException):
    """404 raised when the requested swap row is invisible to the caller.

    Mirrors the ``not_found`` semantics — soft-deleted rows / rows
    owned by a different user / rows that never existed all collapse
    to the same response so the orchestrator never leaks the existence
    of another tenant's row (north-star invariant #5).
    """

    code: Final[str] = SWAP_IR_NOT_FOUND_CODE

    def __init__(self, *, swap_id: UUID | str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"IR swap {swap_id} not found.",
        )


class SwapIrCurveSetNotFoundError(HTTPException):
    """404 raised when a referenced curve set is invisible to the caller.

    Distinct from ``swap_ir_not_found`` so clients can tell "the swap
    you asked for doesn't exist" from "the swap exists but the curve
    set it references is missing or soft-deleted".
    """

    code: Final[str] = SWAP_IR_CURVE_SET_NOT_FOUND_CODE

    def __init__(self, *, curve_set_id: UUID | str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Curve set {curve_set_id} not found.",
        )


class SwapIrSnapshotNotFoundError(HTTPException):
    """404 raised when an ``snapshot_id`` pin is invisible to the caller."""

    code: Final[str] = SWAP_IR_SNAPSHOT_NOT_FOUND_CODE

    def __init__(self, *, snapshot_id: UUID | str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Snapshot {snapshot_id} not found.",
        )


class SwapIrCurveResolutionFailedError(HTTPException):
    """422 raised when curves can't be resolved into a request-ready shape.

    Carries the per-curve failure list in ``details`` so a client can
    surface a "curve X is missing tenor Y" message without re-issuing
    the call. Each item in ``details`` is
    ``{"curve_id"|"curve_index": ..., "reason": "..."}``.
    """

    code: Final[str] = SWAP_IR_CURVE_RESOLUTION_FAILED_CODE

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
        # See ``orchestrator.errors._make_http_handler`` — the global
        # handler reads ``details`` off the exception instance via
        # ``getattr(exc, 'details', None)`` and forwards it into the
        # error envelope. Symmetric with the ``code`` class attribute.
        self.details: list[dict[str, Any]] | None = details


class SwapIrQuoteResolutionFailedError(HTTPException):
    """422 raised when one or more quote IDs can't be resolved at ``as_of``.

    Wraps the MD layer's ``quote_not_found`` with product
    context: ``details`` lists every missing canonical ID + the
    requested ``as_of`` so the client can either patch its quote IDs
    or pin a different snapshot.
    """

    code: Final[str] = SWAP_IR_QUOTE_RESOLUTION_FAILED_CODE

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


class SwapIrMissingTradeFieldsError(HTTPException):
    """422 raised when a trade body omits required economic fields.

    Historically the wire layer silently substituted fixture defaults for
    missing economic fields (rate / notional / schedule dates) — pricing a
    trade the caller never described. Missing required trade fields are now
    a hard 422; the ``details`` payload lists each missing field so the
    client can fix its trade body. New code per the append-only rule.
    """

    code: Final[str] = SWAP_IR_MISSING_TRADE_FIELDS_CODE

    def __init__(self, *, product: str, missing_fields: list[str]) -> None:
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"IR swap trade body is missing required field(s): {', '.join(missing_fields)}."
            ),
        )
        self.details: list[dict[str, Any]] | None = [
            {"product": product, "field": field} for field in missing_fields
        ]


__all__ = [
    "SWAP_IR_CURVE_RESOLUTION_FAILED_CODE",
    "SWAP_IR_CURVE_SET_NOT_FOUND_CODE",
    "SWAP_IR_MISSING_TRADE_FIELDS_CODE",
    "SWAP_IR_NOT_FOUND_CODE",
    "SWAP_IR_QUOTE_RESOLUTION_FAILED_CODE",
    "SWAP_IR_SNAPSHOT_NOT_FOUND_CODE",
    "SwapIrCurveResolutionFailedError",
    "SwapIrCurveSetNotFoundError",
    "SwapIrMissingTradeFieldsError",
    "SwapIrNotFoundError",
    "SwapIrQuoteResolutionFailedError",
    "SwapIrSnapshotNotFoundError",
]
