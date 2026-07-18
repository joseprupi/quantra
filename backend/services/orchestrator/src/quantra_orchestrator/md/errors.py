"""structured-error HTTPExceptions for MD-client failure paths.

Three orchestrator-side error families translate the
:class:`quantra_common.md_client.MdClientError` subclasses into
stable envelope tokens:

* ``QuoteNotFoundError`` — 404 with ``code="quote_not_found"`` for both
  the explicit upstream 404 (snapshot / quote missing) and the
  ``ResolvedQuote.found is False`` case from ``/quotes/resolved``.
* ``MdUnreachableError`` — 502 with ``code="md_unreachable"`` for the
  network-layer failures (transport, DNS, TLS, timeout).
* ``MdUpstreamError`` — 502 with ``code="md_upstream_error"`` for any
  non-404 4xx/5xx the MD service emits (the 5xx case is what the plan
  highlights, but a 4xx that isn't 404 is also "the upstream said no"
  and gets the same surface).

The shared HTTPException handler in ``quantra_orchestrator.errors``
inspects each subclass's ``code`` class attribute, so raising one of
these directly emits the envelope without per-route wiring.
"""

from __future__ import annotations

from typing import Final

from fastapi import HTTPException, status

from quantra_common.md_client import (
    MdHttpStatusError,
    MdNotFoundError,
    MdTransportError,
)

QUOTE_NOT_FOUND_CODE: Final[str] = "quote_not_found"
MD_UNREACHABLE_CODE: Final[str] = "md_unreachable"
MD_UPSTREAM_ERROR_CODE: Final[str] = "md_upstream_error"


class QuoteNotFoundError(HTTPException):
    """404 raised when the MD service has no quote for the requested key.

    Covers both the upstream 404 (returned by snapshot / single-quote
    paths) and the per-item ``found=False`` marker the
    ``/quotes/resolved`` endpoint uses.
    """

    code: Final[str] = QUOTE_NOT_FOUND_CODE

    def __init__(self, *, canonical_id: str, as_of: str) -> None:
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No quote found for canonical_id={canonical_id} at as_of={as_of}.",
        )


class MdUnreachableError(HTTPException):
    """502 raised when the MD service can't be reached or timed out.

    The orchestrator is the only public surface; an upstream connection
    failure must surface to the caller as an explicit "the dependency
    is down" rather than an opaque 500.
    """

    code: Final[str] = MD_UNREACHABLE_CODE

    def __init__(self, *, reason: str = "MD service unreachable.") -> None:
        super().__init__(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=reason,
        )


class MdUpstreamError(HTTPException):
    """502 raised when the MD service answered with a non-success status.

    Distinct from ``MdUnreachableError`` so clients can branch on
    "upstream is alive but returning an error" vs. "upstream is dead."
    The status of the upstream response is folded into ``detail`` for
    triage; the ``code`` stays stable.
    """

    code: Final[str] = MD_UPSTREAM_ERROR_CODE

    def __init__(self, *, upstream_status: int, reason: str | None = None) -> None:
        message = reason or f"MD service returned HTTP {upstream_status}."
        super().__init__(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=message,
        )


def map_md_client_error(
    exc: Exception,
    *,
    canonical_id: str,
    as_of: str,
) -> HTTPException:
    """Translate an :class:`MdClientError` subclass into the envelope.

    Keeps the route handler free of error-mapping branches. Returns a
    ready-to-raise ``HTTPException`` so the route can ``raise
    map_md_client_error(...)`` in one line.
    """

    if isinstance(exc, MdNotFoundError):
        return QuoteNotFoundError(canonical_id=canonical_id, as_of=as_of)
    if isinstance(exc, MdTransportError):
        return MdUnreachableError(reason=str(exc) or "MD service unreachable.")
    if isinstance(exc, MdHttpStatusError):
        return MdUpstreamError(upstream_status=exc.status_code, reason=str(exc))
    # The remaining MdClientError subclasses (``MdResponseError``) mean
    # the MD service responded but the body couldn't be parsed; that's
    # also "upstream is alive but returning an error" so we surface it
    # as ``md_upstream_error`` rather than ``internal_error``.
    return MdUpstreamError(
        upstream_status=status.HTTP_502_BAD_GATEWAY,
        reason=str(exc) or "MD service returned an unparseable response.",
    )


__all__ = [
    "MD_UNREACHABLE_CODE",
    "MD_UPSTREAM_ERROR_CODE",
    "QUOTE_NOT_FOUND_CODE",
    "MdUnreachableError",
    "MdUpstreamError",
    "QuoteNotFoundError",
    "map_md_client_error",
]
