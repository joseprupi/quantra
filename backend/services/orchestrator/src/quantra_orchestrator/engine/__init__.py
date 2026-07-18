"""Engine-client wiring for the orchestrator.

Owns the singleton :class:`quantra_common.engine_client.EngineClient`
instance, the gRPC backend (when configured), the request-id
interceptor, and the FastAPI dependency (``get_engine_client``) that
hands the client to routes via ``app.state`` (the same settings-
override seam the data layer and MD client use).

The engine is the orchestrator's compute primitive. Per the plan,
the client is constructed once per process in the FastAPI lifespan so
every request shares one gRPC channel — per-request channel
construction is a perf bug, not a correctness fix.

Today the orchestrator ships :class:`StubEngineClient` by default.
The seam is here so the real FlatBuffers-backed dispatch can land on
top without touching the lifespan, the dependency, the debug routes,
or the error mapper.
"""

from quantra_orchestrator.engine.client import (
    ENGINE_CLIENT_UNAVAILABLE_CODE,
    EngineClientInfo,
    EngineClientUnavailableError,
    build_engine_client,
    describe_engine_client,
    get_engine_client,
    get_engine_client_info,
)
from quantra_orchestrator.engine.errors import (
    ENGINE_INVALID_REQUEST_CODE,
    ENGINE_TIMEOUT_CODE,
    ENGINE_UNAVAILABLE_CODE,
    ENGINE_UNREACHABLE_CODE,
    ENGINE_UPSTREAM_ERROR_CODE,
    MISSING_REQUIRED_FIXING_CODE,
    PRICING_AS_OF_BEFORE_CURVE_DATE_CODE,
    EngineDateCoherenceError,
    EngineInvalidRequestError,
    EngineMissingFixingError,
    EngineTimeoutMappedError,
    EngineUnavailableError,
    EngineUnreachableError,
    EngineUpstreamError,
    map_engine_client_error,
    parse_missing_fixing,
)
from quantra_orchestrator.engine.grpc_backend import (
    REQUEST_ID_METADATA_KEY,
    GrpcEngineClient,
    RequestIdInterceptor,
    build_grpc_engine_client,
    grpc_status_code_name,
    translate_aio_rpc_error,
)

__all__ = [
    "ENGINE_CLIENT_UNAVAILABLE_CODE",
    "ENGINE_INVALID_REQUEST_CODE",
    "ENGINE_TIMEOUT_CODE",
    "ENGINE_UNAVAILABLE_CODE",
    "ENGINE_UNREACHABLE_CODE",
    "ENGINE_UPSTREAM_ERROR_CODE",
    "MISSING_REQUIRED_FIXING_CODE",
    "PRICING_AS_OF_BEFORE_CURVE_DATE_CODE",
    "REQUEST_ID_METADATA_KEY",
    "EngineClientInfo",
    "EngineClientUnavailableError",
    "EngineDateCoherenceError",
    "EngineInvalidRequestError",
    "EngineMissingFixingError",
    "EngineTimeoutMappedError",
    "EngineUnavailableError",
    "EngineUnreachableError",
    "EngineUpstreamError",
    "GrpcEngineClient",
    "RequestIdInterceptor",
    "build_engine_client",
    "build_grpc_engine_client",
    "describe_engine_client",
    "get_engine_client",
    "get_engine_client_info",
    "grpc_status_code_name",
    "map_engine_client_error",
    "parse_missing_fixing",
    "translate_aio_rpc_error",
]
