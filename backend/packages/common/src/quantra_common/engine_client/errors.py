"""Exception hierarchy for engine_client failures."""

from __future__ import annotations


class EngineClientError(Exception):
    """Base class for everything raised by `EngineClient` implementations."""


class EngineRpcError(EngineClientError):
    """The engine returned an RPC-level error.

    `code` mirrors the gRPC status code (``UNKNOWN`` if the wire layer
    couldn't classify the failure); `details` is whatever payload the
    engine put in the trailers. Both default to ``None`` so non-grpc
    implementations (tests, future HTTP transport) can still raise this
    type.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


class EngineTimeoutError(EngineClientError):
    """The call did not complete within the configured deadline."""


class EngineRetryableError(EngineClientError):
    """Marker for errors the retry wrapper should re-attempt.

    `RetryingEngineClient` only retries on this type and on
    `EngineTimeoutError`. Concrete clients raise this for codes the engine
    classifies as transient (UNAVAILABLE, RESOURCE_EXHAUSTED).
    """
