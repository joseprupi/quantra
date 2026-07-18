"""Data-layer HTTPExceptions with stable ``code`` tokens.

The shared HTTPException handler in ``quantra_orchestrator.errors``
inspects each subclass's ``code`` class attribute, so raising one of
these directly emits the envelope with the right machine token
without per-route handler wiring.
"""

from __future__ import annotations

from typing import Final

from fastapi import HTTPException, status

NOT_FOUND_CODE: Final[str] = "not_found"
NAME_CONFLICT_CODE: Final[str] = "name_conflict"


class NotFoundError(HTTPException):
    """404 for ``GET``/``PATCH``/``DELETE``/restore when no live row matches.

    The contract says soft-deleted rows are invisible (no leak of
    "the row exists but was deleted"). ``code="not_found"`` is the
    single token clients branch on for any missing-resource case.
    """

    code: Final[str] = NOT_FOUND_CODE

    def __init__(self, *, entity: str, resource_id: str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{entity} {resource_id} not found.",
        )


class NameConflictError(HTTPException):
    """409 raised when restoring a row would duplicate ``(owner_uid, name)``.

    The partial unique index permits exactly one live row per
    ``(owner_uid, name)``. Restore re-introduces a row to the "live"
    set; if another live row already owns the same name, the operation
    must fail explicitly so the client can rename the conflicting row
    first.
    """

    code: Final[str] = NAME_CONFLICT_CODE

    def __init__(self, *, entity: str, name: str) -> None:
        super().__init__(
            status_code=status.HTTP_409_CONFLICT,
            detail=(f'Cannot restore {entity}: another live row already uses name "{name}".'),
        )


__all__ = [
    "NameConflictError",
    "NotFoundError",
]
