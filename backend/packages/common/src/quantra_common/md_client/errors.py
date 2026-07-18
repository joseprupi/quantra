"""Structured exception hierarchy for MD client failures."""

from __future__ import annotations


class MdClientError(Exception):
    """Base class for everything raised by `MdClient`."""


class MdTransportError(MdClientError):
    """Network-level failure (connection refused, DNS, TLS, etc.)."""


class MdTimeoutError(MdTransportError):
    """The request did not complete within the configured timeout."""


class MdHttpStatusError(MdClientError):
    """The MD service responded with a non-success HTTP status."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"MD service returned HTTP {status_code}: {body[:200]}")
        self.status_code = status_code
        self.body = body


class MdNotFoundError(MdHttpStatusError):
    """The MD service returned 404 for a specific resource (snapshot, quote)."""


class MdResponseError(MdClientError):
    """The MD service returned 2xx but the body failed to parse / validate."""
