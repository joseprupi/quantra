"""Capture the exact request bytes transmitted to the engine.

The in-app pricing trace wants to show an investigator *exactly what
was sent to the pricing engine over gRPC* — the literal FlatBuffers buffer on
the wire, not a re-encoding of a parallel object built elsewhere.

:class:`CapturingEngineClient` is a transparent :class:`EngineClient`
decorator that records the request bytes of each call at the moment it hands
them to the wrapped client. For the real :class:`GrpcEngineClient` those same
bytes are identity-serialized straight onto the channel
(``unary(request_bytes)``), so the captured buffer is the indisputable wire
payload — there is no re-pack between capture and transmit.

The captured record is held on a plain instance attribute (not a contextvar)
so it survives the ``asyncio.gather`` fan-out the concurrency seam uses:
the route owns the wrapper instance, passes it into ``execute(...)``, and reads
:attr:`CapturingEngineClient.last_request` back after the gather completes —
mutations to a shared object propagate across tasks where a contextvar
reassignment inside a child task would not.

Only the most recent call is retained, which is exactly right for the
one-call-per-request products instrumented today (``OneTradePerCall`` issues a
single engine call per request). A future multi-batch fan-out keeps only the
last batch's bytes; that limitation is noted where it is wired in.
"""

from __future__ import annotations

from dataclasses import dataclass

from quantra_common.engine_client.client import EngineClient
from quantra_common.engine_client.rpcs import EngineRpc


@dataclass(frozen=True, slots=True)
class EngineWireRecord:
    """One captured engine request: the RPC name + the exact transmitted bytes.

    ``request_bytes`` is the *same* buffer object forwarded to the wrapped
    client's :meth:`EngineClient.call`, i.e. what the gRPC channel transmits.
    """

    rpc: str
    request_bytes: bytes


class CapturingEngineClient(EngineClient):
    """Transparent :class:`EngineClient` decorator that records the sent bytes.

    Wrap any concrete client with this at route scope; every :meth:`call`
    stores the request bytes on :attr:`last_request` *before* delegating, so
    the buffer is captured even when the underlying call fails. The wrapped
    client's behaviour is otherwise unchanged (same response, same errors).
    """

    def __init__(self, inner: EngineClient) -> None:
        self._inner = inner
        self.last_request: EngineWireRecord | None = None

    async def call(self, rpc: EngineRpc, request_bytes: bytes) -> bytes:
        # Record before delegating so a failed call still leaves the exact
        # bytes that *were* put on the wire available to the tracer.
        self.last_request = EngineWireRecord(rpc=rpc.value, request_bytes=request_bytes)
        return await self._inner.call(rpc, request_bytes)

    async def close(self) -> None:
        await self._inner.close()


__all__ = [
    "CapturingEngineClient",
    "EngineWireRecord",
]
