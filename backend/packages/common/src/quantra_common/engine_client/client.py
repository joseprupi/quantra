"""`EngineClient` interface, stub implementation, retry wrapper."""

from __future__ import annotations

import abc
import asyncio
import logging
from types import TracebackType
from typing import Self

from pydantic import BaseModel, Field

from quantra_common.engine_client.errors import (
    EngineRetryableError,
    EngineTimeoutError,
)
from quantra_common.engine_client.rpcs import EngineRpc
from quantra_common.settings.base import Settings

_logger = logging.getLogger(__name__)


class EngineClientConfig(BaseModel):
    """Static configuration for an `EngineClient` instance."""

    address: str = Field(description="host:port of the engine gRPC service.")
    timeout_s: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=2, ge=0)
    backoff_base_s: float = Field(default=0.1, gt=0)
    backoff_cap_s: float = Field(default=2.0, gt=0)

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            address=settings.require_engine_grpc_addr(),
            timeout_s=settings.engine_grpc_timeout_s,
        )


class EngineClient(abc.ABC):
    """Abstract async client over the pricing engine's gRPC surface.

    Concrete implementations own the wire transport (gRPC channel, retry
    policy, connection pool). The orchestrator's product code calls
    :meth:`call` with a pre-built FlatBuffers request body and decodes the
    returned bytes back into pydantic models — that translation is *not*
    in this package because it depends on the engine's generated bindings,
    which are versioned alongside the engine repo.
    """

    @abc.abstractmethod
    async def call(self, rpc: EngineRpc, request_bytes: bytes) -> bytes:
        """Invoke a unary RPC by enum name."""

    async def close(self) -> None:  # noqa: B027 - intentional default no-op
        """Release transport-level resources. Override in subclasses if needed."""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()


class StubEngineClient(EngineClient):
    """Default ship-it client.

    Every call raises :class:`NotImplementedError`. The real grpc-aio client
    lives in a later plan (`engine_client_real`) — bringing it in here would
    couple this shared library to ``grpcio`` and to the engine's generated
    FlatBuffer bindings, which are intentionally not runtime deps yet.

    Use this in any service whose code path doesn't yet talk to the engine
    (orchestrator startup smoke checks, MD ingester) and override per-test
    when you need fake response bytes.
    """

    def __init__(self, config: EngineClientConfig | None = None) -> None:
        self._config = config

    async def call(self, rpc: EngineRpc, request_bytes: bytes) -> bytes:
        del request_bytes
        msg = (
            f"StubEngineClient cannot dispatch {rpc.value!r}: "
            "real gRPC client lands in engine_client_real subplan"
        )
        raise NotImplementedError(msg)


class RetryingEngineClient(EngineClient):
    """Decorator that adds bounded exponential-backoff retry to any client.

    Retries on :class:`EngineRetryableError` and :class:`EngineTimeoutError`.
    All other exceptions (validation, unknown RPC, RPC error with
    non-retryable code) propagate immediately so callers get a fast failure
    instead of an N-second-delayed one.
    """

    def __init__(self, inner: EngineClient, config: EngineClientConfig) -> None:
        self._inner = inner
        self._config = config

    async def call(self, rpc: EngineRpc, request_bytes: bytes) -> bytes:
        attempt = 0
        while True:
            try:
                return await self._inner.call(rpc, request_bytes)
            except (EngineRetryableError, EngineTimeoutError) as e:
                attempt += 1
                if attempt > self._config.max_retries:
                    raise
                sleep_s = min(
                    self._config.backoff_cap_s,
                    self._config.backoff_base_s * (2 ** (attempt - 1)),
                )
                _logger.debug(
                    "engine call retrying",
                    extra={
                        "rpc": rpc.value,
                        "attempt": attempt,
                        "sleep_s": sleep_s,
                        "error": str(e),
                    },
                )
                await asyncio.sleep(sleep_s)

    async def close(self) -> None:
        await self._inner.close()
