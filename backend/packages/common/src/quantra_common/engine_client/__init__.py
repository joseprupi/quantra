"""Async client for the pricing engine's gRPC surface.

The pricing engine (`quantra_refractor/quantraserver`) is a stateless C++
service that speaks gRPC + FlatBuffers over a private network (decisions
log D2). This package defines:

- :class:`EngineRpc` — the canonical enumeration of RPC method names that
  match the `.fbs` rpc_service definition, so callers don't pass arbitrary
  strings.
- :class:`EngineClient` — the abstract async surface. Methods take a
  pre-encoded FlatBuffers request body (`bytes`) and return the response
  body (`bytes`). The orchestrator's per-product code is responsible for
  translating pydantic models to/from FlatBuffers using the existing
  schema bindings — that translation is intentionally not in this package.
- :class:`StubEngineClient` — an implementation that raises
  ``NotImplementedError`` for every RPC. **This is the default ship.** The
  real grpc-aio client lands in a later plan (the engine_client_real
  subplan); it would import `grpcio` and the engine's generated FlatBuffer
  bindings, neither of which we want to take as runtime deps in this
  shared library yet.
- :class:`RetryingEngineClient` — a decorator that wraps any concrete
  client with bounded exponential-backoff retries on transient errors.
"""

from __future__ import annotations

from quantra_common.engine_client.client import (
    EngineClient,
    EngineClientConfig,
    RetryingEngineClient,
    StubEngineClient,
)
from quantra_common.engine_client.errors import (
    EngineClientError,
    EngineRetryableError,
    EngineRpcError,
    EngineTimeoutError,
)
from quantra_common.engine_client.rpcs import EngineRpc
from quantra_common.engine_client.wire_capture import (
    CapturingEngineClient,
    EngineWireRecord,
)

__all__ = [
    "CapturingEngineClient",
    "EngineClient",
    "EngineClientConfig",
    "EngineClientError",
    "EngineRetryableError",
    "EngineRpc",
    "EngineRpcError",
    "EngineTimeoutError",
    "EngineWireRecord",
    "RetryingEngineClient",
    "StubEngineClient",
]
