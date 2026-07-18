"""Engine-client construction + per-request dependency wiring.

The orchestrator owns one :class:`quantra_common.engine_client.EngineClient`
per process. The lifespan in ``quantra_orchestrator.app.create_app``
constructs it from settings, parks it on ``app.state.engine_client``,
and disposes the underlying gRPC channel on shutdown. Routes pull
the same instance via :func:`get_engine_client` so the channel is
shared across every request.

Per-request channel construction is a perf bug, not a correctness fix
— every PR touching this module should keep the singleton invariant.

Backend selection is settings-driven:

* When ``OrchestratorSettings.engine_grpc_target`` is unset, the
  factory falls back to :class:`StubEngineClient` — every RPC raises
  ``NotImplementedError`` and the error mapper translates that into an
  ``engine_unavailable`` envelope, so a deploy without a reachable
  engine fails loud and legible instead of hanging.
* When the target is set, the factory builds the gRPC-backed thin
  shell from :mod:`grpc_backend` and wraps it in
  :class:`quantra_common.engine_client.RetryingEngineClient` so
  transient ``UNAVAILABLE`` / ``DEADLINE_EXCEEDED`` failures are
  retried with bounded backoff.

The retry decorator's on/off default depends on the backend (a
deterministic ``NotImplementedError`` from the stub is not worth
retrying); ``OrchestratorSettings.engine_retry_enabled`` is the
explicit override.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from fastapi import HTTPException, Request, status

from quantra_common.engine_client import (
    EngineClient,
    EngineClientConfig,
    RetryingEngineClient,
    StubEngineClient,
)
from quantra_orchestrator.engine.grpc_backend import build_grpc_engine_client
from quantra_orchestrator.settings import (
    OrchestratorSettings,
    get_orchestrator_settings,
)

ENGINE_CLIENT_UNAVAILABLE_CODE: Final[str] = "engine_client_unavailable"


class EngineClientUnavailableError(HTTPException):
    """503 raised when no ``EngineClient`` is attached to ``app.state``.

    Same fail-closed posture as the MD client's
    :class:`MdClientUnavailableError`: a misconfigured deploy
    that forgot to wire the engine client must surface as an explicit
    "the dependency is unconfigured" rather than an opaque 500 on
    first traffic. Distinct from ``engine_unavailable`` (which is
    "the stub is in place but the real engine isn't wired") so
    operators can tell "I have the wrong settings" apart from "I have
    the right settings but ENGINE_GRPC_TARGET is unset, so the stub
    is answering".
    """

    code: Final[str] = ENGINE_CLIENT_UNAVAILABLE_CODE

    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Engine client is not configured. The orchestrator "
                "lifespan must run for app.state.engine_client to be "
                "wired."
            ),
        )


@dataclass(frozen=True, slots=True)
class EngineClientInfo:
    """Read-only snapshot of the active engine client returned by /debug/engine/info.

    Surfaces the backend class name, the retry configuration in effect,
    and whether the gRPC target is set. Useful for ops to confirm at a
    glance which backend is wired without inspecting Python state.
    """

    backend: str
    retry_enabled: bool
    max_retries: int
    backoff_base_s: float
    backoff_cap_s: float
    timeout_s: float
    grpc_target_configured: bool


def _engine_config_from_settings(settings: OrchestratorSettings) -> EngineClientConfig:
    """Build the static engine-client config from orchestrator settings.

    Uses a placeholder ``address="stub"`` when the gRPC target isn't
    set — the field is required by the pydantic model but unused by
    the stub backend, and the gRPC backend overrides it from the
    settings target via :func:`build_grpc_engine_client`.
    """

    address = settings.engine_grpc_target or "stub"
    return EngineClientConfig(
        address=address,
        timeout_s=settings.engine_timeout_s,
        max_retries=settings.engine_max_retries,
        backoff_base_s=settings.engine_backoff_base_s,
        backoff_cap_s=settings.engine_backoff_cap_s,
    )


def _resolve_retry_enabled(settings: OrchestratorSettings, *, grpc_backend: bool) -> bool:
    """Decide whether to wrap the backend in :class:`RetryingEngineClient`.

    Tri-state ``engine_retry_enabled`` overrides the default; ``None``
    falls back to "on for gRPC, off for stub" — the rationale recorded
    in the settings docstring. Retries with budget 0 are also off.
    """

    if settings.engine_retry_enabled is not None:
        return settings.engine_retry_enabled and settings.engine_max_retries > 0
    return grpc_backend and settings.engine_max_retries > 0


def build_engine_client(
    settings: OrchestratorSettings,
) -> EngineClient:
    """Construct the lifespan-owned :class:`EngineClient` from settings.

    Stub backend (default) → :class:`StubEngineClient` directly; no
    retry wrapper because retrying a deterministic
    ``NotImplementedError`` is wasted wall-clock.

    gRPC backend (target set) → :class:`GrpcEngineClient` opened via
    :func:`build_grpc_engine_client`, optionally wrapped in
    :class:`RetryingEngineClient`.
    """

    config = _engine_config_from_settings(settings)
    target = settings.engine_grpc_target

    if target:
        grpc_client = build_grpc_engine_client(target=target, config=config)
        if _resolve_retry_enabled(settings, grpc_backend=True):
            return RetryingEngineClient(grpc_client, config)
        return grpc_client

    stub_client: EngineClient = StubEngineClient(config)
    if _resolve_retry_enabled(settings, grpc_backend=False):
        return RetryingEngineClient(stub_client, config)
    return stub_client


def _unwrap_inner(client: EngineClient) -> EngineClient:
    """Walk past every known wrapper to the real backend.

    Today's wrapper stack (outermost → innermost) is:

    * :class:`quantra_orchestrator.observability.InstrumentedEngineClient`
      (only when /metrics is wired, which is the production default; the observability design).
    * :class:`quantra_common.engine_client.RetryingEngineClient`
      (only when retries are enabled per settings; the engine-client contract).
    * the actual backend (``StubEngineClient`` or
      :class:`GrpcEngineClient`).

    Each wrapper stashes the next layer on ``_inner``; we walk until
    the next ``_inner`` either isn't there or isn't an ``EngineClient``.
    Bounded to a handful of layers (the constant in
    :data:`_MAX_UNWRAP_DEPTH`) so a misbehaving wrapper can never
    spin forever.
    """

    current: EngineClient = client
    for _ in range(_MAX_UNWRAP_DEPTH):
        inner = getattr(current, "_inner", None)
        if isinstance(inner, EngineClient):
            current = inner
            continue
        break
    return current


# Number of wrapper layers ``_unwrap_inner`` will peel before giving up.
# Today's max is 2 (Instrumented → Retrying → backend); the cap is set
# a few above so a future wrapper does not silently truncate.
_MAX_UNWRAP_DEPTH: Final[int] = 6


def _is_retry_decorated(client: EngineClient) -> bool:
    """``True`` if a :class:`RetryingEngineClient` sits anywhere in the wrapper chain."""

    current: EngineClient = client
    for _ in range(_MAX_UNWRAP_DEPTH):
        if isinstance(current, RetryingEngineClient):
            return True
        inner = getattr(current, "_inner", None)
        if not isinstance(inner, EngineClient):
            return False
        current = inner
    return False


def describe_engine_client(
    client: EngineClient, settings: OrchestratorSettings
) -> EngineClientInfo:
    """Build the :class:`EngineClientInfo` snapshot for /debug/engine/info."""

    inner = _unwrap_inner(client)
    return EngineClientInfo(
        backend=type(inner).__name__,
        retry_enabled=_is_retry_decorated(client),
        max_retries=settings.engine_max_retries,
        backoff_base_s=settings.engine_backoff_base_s,
        backoff_cap_s=settings.engine_backoff_cap_s,
        timeout_s=settings.engine_timeout_s,
        grpc_target_configured=bool(settings.engine_grpc_target),
    )


def get_engine_client(request: Request) -> EngineClient:
    """Resolve the lifespan-owned :class:`EngineClient` or raise 503.

    Mirrors :func:`quantra_orchestrator.md.client.get_md_client`: read
    from ``app.state``, fail closed when missing. Tests inject a fake
    :class:`EngineClient` by setting ``app.state.engine_client``
    before the request runs (the same settings-override seam the
    other dependencies use).
    """

    client = getattr(request.app.state, "engine_client", None)
    if client is None:
        raise EngineClientUnavailableError()
    if not isinstance(client, EngineClient):
        msg = f"app.state.engine_client is the wrong type: {type(client).__name__}"
        raise TypeError(msg)
    return client


def get_engine_client_info(request: Request) -> EngineClientInfo:
    """Resolve the engine-client info snapshot for the active request.

    Reads both ``app.state.engine_client`` and the per-app settings
    instance (the same app-state-managed override the rest of the
    orchestrator uses). Raises ``EngineClientUnavailableError`` when
    no client is wired so the ``/debug/engine/info`` route uses the
    same 503 contract as ``/debug/engine/ping``.
    """

    client = get_engine_client(request)
    cfg: OrchestratorSettings | None = getattr(request.app.state, "orchestrator_settings", None)
    # ``orchestrator_settings`` is stamped on app.state by ``create_app``
    # before the lifespan runs (the same seam every other route
    # uses). Fall back to the cached singleton only if a test built
    # the app via something other than ``create_app``.
    if cfg is None:
        cfg = get_orchestrator_settings()
    return describe_engine_client(client, cfg)


__all__ = [
    "ENGINE_CLIENT_UNAVAILABLE_CODE",
    "EngineClientInfo",
    "EngineClientUnavailableError",
    "build_engine_client",
    "describe_engine_client",
    "get_engine_client",
    "get_engine_client_info",
]
