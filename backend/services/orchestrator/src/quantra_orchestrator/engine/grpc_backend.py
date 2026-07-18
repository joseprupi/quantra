"""Thin gRPC backend for :class:`quantra_common.engine_client.EngineClient`.

This module is the orchestrator's gRPC side of the engine client:
it dispatches real bytes over a ``grpc.aio.Channel`` against the live
``quantraserver``.

What this module ships today:

1. :class:`RequestIdInterceptor` — a ``grpc.aio.UnaryUnaryClientInterceptor``
   that reads ``request_id`` from ``structlog.contextvars`` and copies
   it into outgoing gRPC metadata under the ``x-request-id`` key. This
   is the gRPC analog of the MD client's httpx event hook and
   keeps :mod:`quantra_common.engine_client` ignorant of FastAPI /
   structlog (per the contract that the public API stays stable).
2. :class:`GrpcEngineClient` — an :class:`EngineClient` subclass that
   constructs and owns one ``grpc.aio.Channel`` per orchestrator
   process (one channel per process, shared by all requests). Its ``call`` looks up
   the gRPC method path from a static :data:`ENGINE_RPC_METHOD_PATHS`
   mapping, dispatches the request bytes via
   ``channel.unary_unary(...)``, and returns the response bytes.
   Failures are translated to the engine_client error family via
   :func:`translate_aio_rpc_error` so the existing engine error mapper
   in :mod:`quantra_orchestrator.engine.errors` covers every code
   path with no new entries.
3. :func:`build_grpc_engine_client` — the factory the orchestrator's
   lifespan calls when ``ENGINE_GRPC_TARGET`` is set.

The static :data:`ENGINE_RPC_METHOD_PATHS` mapping is the pin the
test suite uses to verify each :class:`EngineRpc` enum entry has a
canonical ``/quantra.QuantraServer/<MethodName>`` path on the wire.
When the engine schema gains a new RPC the canonical enum entry + this
mapping are the only orchestrator-side surfaces that change.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import MappingProxyType
from typing import Final

import grpc
import structlog
from grpc.aio import (
    AioRpcError,
    Channel,
    ClientCallDetails,
    Metadata,
    UnaryUnaryCall,
    UnaryUnaryClientInterceptor,
)

from quantra_common.engine_client import (
    EngineClient,
    EngineClientConfig,
    EngineRetryableError,
    EngineRpc,
    EngineRpcError,
    EngineTimeoutError,
)

REQUEST_ID_METADATA_KEY: Final[str] = "x-request-id"
"""gRPC metadata key the orchestrator forwards request-ids under.

Lowercase per the gRPC spec (HTTP/2 trailers are normalized to
lowercase on the wire). The MD client uses the HTTP-style
``X-Request-Id`` header for the same purpose; the two surfaces
intentionally diverge on case because the wire formats themselves
differ.
"""


_ENGINE_GRPC_SERVICE: Final[str] = "quantra.QuantraServer"
"""Fully qualified gRPC service name from ``grpc/quantraserver.fbs``.

The engine's ``rpc_service QuantraServer { … }`` declaration is
under ``namespace quantra``, so every method path on the wire is
``/quantra.QuantraServer/<MethodName>``. Pinned here as the source
of truth for :data:`ENGINE_RPC_METHOD_PATHS`.
"""


ENGINE_RPC_METHOD_PATHS: Final[MappingProxyType[EngineRpc, str]] = MappingProxyType(
    {rpc: f"/{_ENGINE_GRPC_SERVICE}/{rpc.value}" for rpc in EngineRpc}
)
"""Static :class:`EngineRpc` → fully qualified gRPC method path mapping.

Built mechanically from the canonical :class:`EngineRpc` enum, which
is the pin against the engine's ``grpc/quantraserver.fbs``. Each
enum entry's ``.value`` is the canonical method name
(e.g. ``PriceVanillaSwap``), and the path is
``/quantra.QuantraServer/<MethodName>`` per
:data:`_ENGINE_GRPC_SERVICE`. Iteration order tracks the enum
definition order so a unit test can pin "every enum entry has a
path" with one assertion. Made ``MappingProxyType`` so it is
read-only.
"""


class RequestIdInterceptor(UnaryUnaryClientInterceptor):
    """gRPC client interceptor that forwards ``request_id`` to the engine.

    Reads ``request_id`` from ``structlog.contextvars`` (bound by
    ``RequestIdMiddleware``); if absent (e.g. an engine call from a
    background task with no request context), the interceptor is a
    no-op and the upstream call still proceeds.

    Mirrors :func:`quantra_orchestrator.md.client._make_request_id_hook`
    in spirit: the request-id flows at the *transport* layer
    so the public ``EngineClient`` API doesn't need a
    request-id parameter on every method.
    """

    async def intercept_unary_unary(  # type: ignore[override]
        self,
        continuation: Callable[
            [ClientCallDetails, object], Awaitable[UnaryUnaryCall[object, object]]
        ],
        client_call_details: ClientCallDetails,
        request: object,
    ) -> UnaryUnaryCall[object, object] | object:
        # ``request`` and the call's return type are intentionally
        # opaque here: the interceptor never reads the request body
        # and never decodes the response — it only edits metadata.
        # Callers keep
        # their own typed wrappers around this layer.
        request_id = _current_request_id()
        if not request_id:
            return await continuation(client_call_details, request)
        new_details = _with_request_id_metadata(client_call_details, request_id)
        return await continuation(new_details, request)


def _current_request_id() -> str | None:
    ctx = structlog.contextvars.get_contextvars()
    value = ctx.get("request_id")
    return value if isinstance(value, str) and value else None


def _with_request_id_metadata(details: ClientCallDetails, request_id: str) -> ClientCallDetails:
    """Return a copy of ``details`` with ``x-request-id`` appended to metadata.

    Builds a new :class:`Metadata` carrying the existing key/value
    pairs plus our request-id key, then constructs a fresh
    ``ClientCallDetails`` so any metadata the caller already set on a
    higher layer (e.g. an auth token) survives. We deliberately avoid
    the runtime-only ``ClientCallDetails._replace`` shortcut because
    grpc-aio's type stubs don't carry the NamedTuple shape.
    """

    new_metadata = Metadata()
    if details.metadata is not None:
        # ``Metadata`` is a Mapping; iteration yields keys, so use
        # ``.items()`` to preserve duplicate keys via ``add()``.
        for key, value in details.metadata.items():
            new_metadata.add(key, value)
    new_metadata.add(REQUEST_ID_METADATA_KEY, request_id)
    return ClientCallDetails(
        method=details.method,
        timeout=details.timeout,
        metadata=new_metadata,
        credentials=details.credentials,
        wait_for_ready=details.wait_for_ready,
    )


def _identity_codec(payload: bytes) -> bytes:
    """Identity (de)serializer for ``channel.unary_unary``.

    The orchestrator's per-product ``engine_io.py`` modules already
    own FlatBuffers encode + decode; the gRPC layer just moves
    bytes. ``unary_unary`` insists on a callable for both directions,
    so this no-op codec wires both. Pulled out of
    :meth:`GrpcEngineClient.call` so mypy can give it a real
    signature instead of an inline ``lambda``.
    """

    return payload


class GrpcEngineClient(EngineClient):
    """``grpc.aio.Channel``-backed :class:`EngineClient` (live dispatch).

    Holds the channel for the lifespan of the orchestrator process so
    the connection pool is shared across requests (the same singleton
    invariant the MD client enforces). ``call`` looks the
    fully qualified method path up from
    :data:`ENGINE_RPC_METHOD_PATHS`, dispatches the caller-supplied
    bytes via ``channel.unary_unary(...)`` with identity codecs, and
    returns the engine's response bytes for the caller's per-product
    decoder to interpret. Failures are translated via
    :func:`translate_aio_rpc_error` into the engine_client error
    family so the existing engine error mapper covers every wire code.

    The channel is interceptor-equipped so the orchestrator's
    ``request_id`` from :class:`RequestIdMiddleware` is forwarded to
    the engine on every call.
    """

    def __init__(
        self,
        *,
        channel: Channel,
        config: EngineClientConfig,
    ) -> None:
        self._channel = channel
        self._config = config

    @property
    def channel(self) -> Channel:
        """Expose the underlying channel for tests + future stub wiring."""

        return self._channel

    @property
    def config(self) -> EngineClientConfig:
        """Static config (target, timeout, retry knobs) used to build the client."""

        return self._config

    async def call(self, rpc: EngineRpc, request_bytes: bytes) -> bytes:
        """Dispatch one unary RPC and return the engine's response bytes.

        Pure transport: encoding lives in the per-product
        ``engine_io.py`` modules. Errors:

        * ``UNAVAILABLE`` — wrapped in :class:`EngineRetryableError`
          so the retry decorator can re-attempt.
        * ``DEADLINE_EXCEEDED`` — wrapped in
          :class:`EngineTimeoutError`.
        * Any other gRPC code — wrapped in :class:`EngineRpcError`
          with the code name on ``.code`` (the mapper catches
          ``INVALID_ARGUMENT`` / 5xx-shaped codes and routes them
          to ``engine_invalid_request`` / ``engine_upstream_error``).
        * Raw socket / DNS failures escape :class:`AioRpcError`'s
          wrapping; let them propagate so the route's
          ``ConnectionError | OSError`` ``except`` arm picks them
          up and the mapper emits ``engine_unreachable``.
        """

        try:
            method_path = ENGINE_RPC_METHOD_PATHS[rpc]
        except KeyError as exc:
            # An EngineRpc enum entry without a path mapping is a
            # programming bug (the mapping is constructed mechanically
            # from the enum so this is structurally impossible today;
            # the explicit raise documents the invariant).
            msg = f"GrpcEngineClient.call: no method path for {rpc.value!r}"
            raise EngineRpcError(msg, code="UNKNOWN_RPC") from exc

        unary = self._channel.unary_unary(
            method_path,
            request_serializer=_identity_codec,
            response_deserializer=_identity_codec,
        )
        try:
            response = await unary(request_bytes, timeout=self._config.timeout_s)
        except AioRpcError as exc:
            raise translate_aio_rpc_error(exc) from exc
        if not isinstance(response, bytes):
            # Defensive: the identity deserializer returns whatever the
            # wire layer hands it. ``grpc-aio`` guarantees ``bytes``
            # for the unary path; checking keeps the runtime contract
            # explicit so a future codec swap can't silently break the
            # caller's typed decode.
            msg = f"GrpcEngineClient.call: expected bytes response, got {type(response).__name__}"
            raise EngineRpcError(msg, code="INVALID_RESPONSE")
        return response

    async def close(self) -> None:
        # ``Channel.close`` is async-safe and idempotent. The 0s grace
        # forces in-flight RPCs to fail rather than queueing on shutdown
        # — appropriate for an orchestrator process that's already
        # being torn down.
        await self._channel.close(grace=None)


def build_grpc_engine_client(
    *,
    target: str,
    config: EngineClientConfig,
    extra_interceptors: tuple[UnaryUnaryClientInterceptor, ...] = (),
) -> GrpcEngineClient:
    """Construct a :class:`GrpcEngineClient` against ``target``.

    ``extra_interceptors`` is an injection seam for tests / future
    cross-cutting concerns (auth tokens, tracing); production callers
    only need the request-id interceptor and pass nothing.

    The channel is plaintext (``insecure_channel``) because the engine
    sits behind a private network boundary today. When the
    engine grows TLS the helper grows a ``credentials=`` argument; the
    orchestrator's call site stays unchanged.
    """

    interceptors: tuple[UnaryUnaryClientInterceptor, ...] = (
        RequestIdInterceptor(),
        *extra_interceptors,
    )
    channel = grpc.aio.insecure_channel(
        target,
        interceptors=list(interceptors),
    )
    return GrpcEngineClient(channel=channel, config=config)


def grpc_status_code_name(exc: AioRpcError) -> str:
    """Return the ``StatusCode`` name (``"UNAVAILABLE"`` etc.) of an RPC error.

    Grpc-aio exposes the status code as a ``grpc.StatusCode`` enum
    member; ``.name`` is the string the engine-client wire layer
    surfaces in :class:`EngineRpcError.code`. Defaults to ``UNKNOWN``
    if the wire layer couldn't classify.
    """

    code = exc.code()
    if isinstance(code, grpc.StatusCode):
        return code.name
    return "UNKNOWN"


def translate_aio_rpc_error(exc: AioRpcError) -> Exception:
    """Map a raw ``grpc.aio.AioRpcError`` into an engine_client subclass.

    Centralising the translation here means RPC call sites can do
    ``raise translate_aio_rpc_error(exc) from exc`` and the error
    mapper / retry decorator stay single-source-of-truth.

    Mapping:

    * ``UNAVAILABLE`` → :class:`EngineRetryableError` (the retry
      decorator re-attempts; if it exhausts, the error mapper turns it
      into ``engine_timeout``).
    * ``DEADLINE_EXCEEDED`` → :class:`EngineTimeoutError`.
    * Any other code → :class:`EngineRpcError` with the gRPC code name
      preserved on ``code=``.

    ``EngineRpcError.details`` carries ``exc.details`` verbatim so the
    mapper can translate a ``Missing <index> fixing for <date>`` engine
    message into an actionable ``missing_required_fixing`` (O47b) — this fires
    for any engine build that surfaces the QuantLib message as the gRPC
    ``error_message``. NOTE: the current engine build masks that message as a
    bare ``"QuantLib error"`` on the wire (the engine sends the full
    text only as the un-propagated 3rd ``grpc::Status`` arg), so the *live*
    seasoned-swap surface is produced by the orchestrator's pre-flight
    detection in ``pricing.swap_ir`` rather than by this translation.
    """

    name = grpc_status_code_name(exc)
    details = exc.details() or ""
    message = f"engine RPC failed: {name} ({details})" if details else f"engine RPC failed: {name}"
    if name == "UNAVAILABLE":
        return EngineRetryableError(message)
    if name == "DEADLINE_EXCEEDED":
        return EngineTimeoutError(message)
    return EngineRpcError(message, code=name, details=details or None)


__all__ = [
    "ENGINE_RPC_METHOD_PATHS",
    "REQUEST_ID_METADATA_KEY",
    "GrpcEngineClient",
    "RequestIdInterceptor",
    "build_grpc_engine_client",
    "grpc_status_code_name",
    "translate_aio_rpc_error",
]
