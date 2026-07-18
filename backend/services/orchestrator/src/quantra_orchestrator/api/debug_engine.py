"""``/debug/engine/*`` — verification-only routes for the engine wiring.

These endpoints exist so ops and smoke-test scripts can confirm the
orchestrator's engine-client wiring is healthy end-to-end without
standing up a per-product pricing endpoint. Per
 they are **not** part of the
public API and may change or be removed at any time; the per-product
endpoints and the engine forwarder are the
only sanctioned public consumers of the engine client.

Both routes require auth (any authenticated identity — the info
surface is non-sensitive but only authenticated callers should be
poking at this surface). Errors flow through the shared error envelope
with the codes documented on each route.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from quantra_common.auth.context import AuthContext
from quantra_common.engine_client import EngineClient, EngineRpc
from quantra_orchestrator.auth.dependencies import get_auth_context
from quantra_orchestrator.engine import (
    EngineClientInfo,
    get_engine_client,
    get_engine_client_info,
    map_engine_client_error,
)

# Dev tooling — keep the routes but hide them from the public OpenAPI.
router = APIRouter(prefix="/debug/engine", tags=["debug"], include_in_schema=False)

_log = structlog.get_logger(__name__)

# Default RPC for ``/debug/engine/ping`` — picked because it is the
# cheapest call in the engine's catalog (a single calendar lookup,
# no curve bootstrap, no model calibration). Using the enum's value
# rather than hard-coding the string keeps the choice tied to the
# canonical RPC list.
_PING_DEFAULT_RPC: EngineRpc = EngineRpc.CALENDAR_BUSINESS_DAYS


class EnginePingResponse(BaseModel):
    """Successful response for ``/debug/engine/ping``.

    A successful response from the *stub* backend is impossible by
    design (every RPC raises ``NotImplementedError`` → 502 structured
    envelope). This model exists for the future where a real engine
    backend answers the ping, in which case the route returns the
    RPC name + the byte length of the response (the body itself is
    FlatBuffers and not human-readable here).
    """

    rpc: str = Field(description="EngineRpc enum value the ping was issued against.")
    response_bytes: int = Field(
        ge=0,
        description=(
            "Length of the engine's raw response body in bytes. "
            "Non-zero confirms the engine answered — the body itself "
            "is FlatBuffers and not surfaced here."
        ),
    )


@router.get(
    "/ping",
    response_model=EnginePingResponse,
    summary="Round-trip a single low-cost RPC against the engine (verification only).",
    description=(
        "Issues a single RPC against the configured engine backend and "
        "returns the response's byte length. Default RPC is "
        "``CALENDAR_BUSINESS_DAYS`` (the cheapest call in the engine's "
        "catalog); pass ``?rpc=<EngineRpc>`` to ping a different one. "
        "Against the default :class:`StubEngineClient` backend this "
        "endpoint always returns the structured error envelope:\n\n"
        "* `engine_unavailable` (502) — stub backend (no real gRPC "
        "client wired; ``ENGINE_GRPC_TARGET`` unset).\n"
        "* `engine_unreachable` (502) — gRPC channel can't be reached.\n"
        "* `engine_timeout` (504) — gRPC `UNAVAILABLE` / "
        "`DEADLINE_EXCEEDED`.\n"
        "* `engine_invalid_request` (400) — gRPC `INVALID_ARGUMENT`.\n"
        "* `engine_upstream_error` (502) — any other engine-side "
        "error.\n"
        "* `engine_client_unavailable` (503) — no client wired on "
        "`app.state` (deploy-config safety net)."
    ),
    responses={
        400: {"description": "Engine rejected the request as malformed."},
        401: {"description": "Missing or invalid credentials."},
        502: {"description": "Engine unavailable / unreachable / upstream error."},
        503: {"description": "Engine client not configured on app.state."},
        504: {"description": "Engine timed out."},
    },
)
async def ping_engine(
    rpc: Annotated[
        EngineRpc,
        Query(
            description=(
                "Engine RPC to ping. Defaults to "
                "``CALENDAR_BUSINESS_DAYS`` — the cheapest call in the "
                "engine's catalog. Any value from "
                "``quantra_common.engine_client.EngineRpc`` is accepted."
            ),
        ),
    ] = _PING_DEFAULT_RPC,
    engine: EngineClient = Depends(get_engine_client),
    auth: AuthContext = Depends(get_auth_context),
) -> EnginePingResponse:
    """Issue one RPC against the engine; map every failure to the envelope."""

    del auth  # auth is enforced by Depends; we don't need the principal here
    try:
        # Empty request body — appropriate for the calendar RPCs and
        # for any RPC against the stub (which discards the bytes).
        # Real per-product code passes FlatBuffers
        # request bytes; this debug route deliberately stays generic.
        response_bytes = await engine.call(rpc, b"")
    except Exception as exc:
        # Catch broadly: ``engine.call`` may raise the stub's
        # ``NotImplementedError``, an :class:`EngineClientError`
        # subclass, an :class:`AioRpcError`, or a transport
        # ``OSError``. The mapper dispatches each into the right
        # error envelope; without the catch the catch-all 500 handler
        # would mask the engine-specific token.
        _log.warning(
            "orchestrator.debug_engine.ping_failed",
            rpc=rpc.value,
            exc_type=type(exc).__name__,
        )
        raise map_engine_client_error(exc) from exc

    return EnginePingResponse(rpc=rpc.value, response_bytes=len(response_bytes))


@router.get(
    "/info",
    response_model=EngineClientInfo,
    summary="Inspect the engine client's configuration (verification only).",
    description=(
        "Returns a snapshot of which engine backend is wired and the "
        "retry knobs in effect. Useful for confirming at a glance "
        "whether a deploy is on the stub or a real gRPC backend. "
        "Same not-public-API caveat as `/debug/engine/ping`."
    ),
    responses={
        401: {"description": "Missing or invalid credentials."},
        503: {"description": "Engine client not configured on app.state."},
    },
)
async def engine_info(
    info: EngineClientInfo = Depends(get_engine_client_info),
    auth: AuthContext = Depends(get_auth_context),
) -> EngineClientInfo:
    """Return the active engine client snapshot."""

    del auth
    return info


__all__ = ["router"]
