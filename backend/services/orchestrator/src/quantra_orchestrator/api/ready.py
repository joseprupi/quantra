"""``GET /ready`` — orchestrator readiness probe.

Distinct from ``/health`` (process liveness, no I/O) by design:
``/health`` is what container restart policies probe, ``/ready`` is
what request routers and load balancers probe to decide whether to
send traffic. Conflating them constrains the operator's choice of
orchestrator (k8s, Cloud Run, ECS all separate liveness from
readiness).

The endpoint runs four short-timeout probes in parallel:

* **md**     — HTTP ``GET /health`` on ``MD_SERVICE_URL``.
* **engine** — a cheap engine RPC (``CALENDAR_BUSINESS_DAYS`` with an
  empty body — the same shape ``/debug/engine/ping`` uses).
* **db_app** — ``SELECT 1`` on the ``app_ro`` pool.
* **db_md**  — ``SELECT 1`` on the ``md_ro`` pool, when the orchestrator
  has one wired. Pool is optional today (the orchestrator never reads
  ``md.*`` directly — MD service owns it), so an unwired pool reports
  ``"skipped"`` and does not gate readiness.

A check that times out, errors, or returns the wrong status code is
surfaced verbatim in the response body. The endpoint never swallows
which dependency failed (discipline).
"""

from __future__ import annotations

import asyncio
from typing import Any, Final

import httpx
import structlog
from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_common.engine_client import EngineClient
from quantra_common.engine_client.errors import EngineRpcError
from quantra_common.engine_client.rpcs import EngineRpc
from quantra_orchestrator.settings import (
    OrchestratorSettings,
    get_orchestrator_settings,
)

router = APIRouter()

_log = structlog.get_logger(__name__)

# Per-probe timeout (seconds). Each probe is capped at ~1s; the union
# stays under 3s when all four run sequentially, but they actually
# run in parallel so the worst-case wall-clock is ~1s.
_PROBE_TIMEOUT_S: Final[float] = 1.0

# Engine readiness probe — same RPC the existing ``/debug/engine/ping``
# uses (the cheapest call in the engine's catalog; a single calendar
# lookup, no curve bootstrap). Empty bytes is fine for the calendar
# RPCs (and produces a deterministic ``INVALID_ARGUMENT`` against
# strict implementations, which still counts as "engine answered" for
# readiness purposes).
_READY_ENGINE_RPC: Final[EngineRpc] = EngineRpc.CALENDAR_BUSINESS_DAYS

# gRPC status codes that mean the engine did NOT answer (the wire was
# down or the deadline elapsed). Any other code on
# :class:`EngineRpcError.code` proves the engine took the call and
# sent a structured response back, so the readiness probe accepts it
# — the empty body is a deliberately invalid request that real engines
# tend to reject as ``INVALID_ARGUMENT`` (strict FlatBuffers parsers)
# or ``INTERNAL`` (lenient FlatBuffers parsers that get past the
# header and fail later). Both still count as "wire up".
_ENGINE_UNREACHABLE_CODES: Final[frozenset[str]] = frozenset({"UNAVAILABLE", "DEADLINE_EXCEEDED"})

# When all four checks are "ok" (or "skipped" for db_md without a
# wired pool) the endpoint returns 200; any "fail" flips the overall
# response to 503 per the standard readiness contract.
_OK_STATES: Final[frozenset[str]] = frozenset({"ok", "skipped"})


class CheckResult(BaseModel):
    """One readiness probe outcome.

    ``status`` is one of:

    * ``ok``      — probe succeeded.
    * ``fail``    — probe ran but produced a non-OK result.
    * ``skipped`` — dependency is not configured (e.g. no MD URL).
    """

    status: str = Field(description="``ok`` / ``fail`` / ``skipped``.")
    detail: str | None = Field(
        default=None,
        description="Short reason on ``fail`` / ``skipped`` (omitted on ``ok``).",
    )
    duration_ms: int = Field(
        ge=0,
        description="Wall-clock probe duration in milliseconds.",
    )


class ReadyResponse(BaseModel):
    """Aggregate readiness response.

    Mirrors the required dependency-breakdown shape: a
    top-level ``status`` plus a per-check map so operators tell which
    dependency tripped the gate without inspecting logs.
    """

    status: str = Field(description="``ready`` when every check is OK, ``not_ready`` otherwise.")
    checks: dict[str, CheckResult] = Field(
        description="Per-dependency probe outcomes (``md`` / ``engine`` / ``db_app`` / ``db_md``)."
    )


async def _probe_md(settings: OrchestratorSettings) -> CheckResult:
    """``GET <MD_SERVICE_URL>/health`` with a short timeout."""

    if not settings.md_service_url:
        return CheckResult(status="skipped", detail="md_service_url unset", duration_ms=0)
    base_url = settings.md_service_url.rstrip("/")
    started = asyncio.get_event_loop().time()
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S) as transport:
            response = await transport.get(f"{base_url}/health")
    except httpx.TimeoutException:
        elapsed_ms = int((asyncio.get_event_loop().time() - started) * 1000)
        return CheckResult(status="fail", detail="timeout", duration_ms=elapsed_ms)
    except httpx.HTTPError as exc:
        elapsed_ms = int((asyncio.get_event_loop().time() - started) * 1000)
        return CheckResult(
            status="fail",
            detail=f"transport_error: {type(exc).__name__}",
            duration_ms=elapsed_ms,
        )
    elapsed_ms = int((asyncio.get_event_loop().time() - started) * 1000)
    if response.status_code != status.HTTP_200_OK:
        return CheckResult(
            status="fail",
            detail=f"http_{response.status_code}",
            duration_ms=elapsed_ms,
        )
    return CheckResult(status="ok", duration_ms=elapsed_ms)


def _classify_engine_failure(exc: BaseException) -> str:
    """Translate an engine probe exception into a stable ``detail`` token."""

    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, NotImplementedError):
        return "stub_engine_not_wired"
    if isinstance(exc, EngineRpcError):
        code = exc.code or "unknown"
        return f"grpc_{code}"
    return type(exc).__name__


async def _probe_engine(engine: EngineClient | None) -> CheckResult:
    """Cheap engine RPC, treating ``INVALID_ARGUMENT`` as "answered"."""

    if engine is None:
        return CheckResult(status="skipped", detail="engine_client_unset", duration_ms=0)
    started = asyncio.get_event_loop().time()
    try:
        await asyncio.wait_for(
            engine.call(_READY_ENGINE_RPC, b""),
            timeout=_PROBE_TIMEOUT_S,
        )
    except Exception as exc:
        elapsed_ms = int((asyncio.get_event_loop().time() - started) * 1000)
        if isinstance(exc, EngineRpcError) and (exc.code or "") not in _ENGINE_UNREACHABLE_CODES:
            # Engine answered the wire — code is structured rejection.
            return CheckResult(status="ok", duration_ms=elapsed_ms)
        return CheckResult(
            status="fail",
            detail=_classify_engine_failure(exc),
            duration_ms=elapsed_ms,
        )
    elapsed_ms = int((asyncio.get_event_loop().time() - started) * 1000)
    return CheckResult(status="ok", duration_ms=elapsed_ms)


async def _probe_db(engine: AsyncEngine | None, *, name: str) -> CheckResult:
    """``SELECT 1`` against a SQLAlchemy async engine, or ``skipped``."""

    if engine is None:
        return CheckResult(
            status="skipped",
            detail=f"{name}_engine_unset",
            duration_ms=0,
        )
    started = asyncio.get_event_loop().time()
    try:

        async def _run() -> None:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))

        await asyncio.wait_for(_run(), timeout=_PROBE_TIMEOUT_S)
    except TimeoutError:
        elapsed_ms = int((asyncio.get_event_loop().time() - started) * 1000)
        return CheckResult(status="fail", detail="timeout", duration_ms=elapsed_ms)
    except Exception as exc:
        elapsed_ms = int((asyncio.get_event_loop().time() - started) * 1000)
        return CheckResult(
            status="fail",
            detail=f"{type(exc).__name__}",
            duration_ms=elapsed_ms,
        )
    elapsed_ms = int((asyncio.get_event_loop().time() - started) * 1000)
    return CheckResult(status="ok", duration_ms=elapsed_ms)


@router.get(
    "/ready",
    response_model=ReadyResponse,
    tags=["meta"],
    responses={
        200: {"description": "All readiness probes passed."},
        503: {"description": "At least one readiness probe failed; see ``checks`` for which."},
    },
)
async def ready(
    request: Request,
    response: Response,
) -> ReadyResponse:
    """Run every readiness probe in parallel and aggregate the result."""

    settings: OrchestratorSettings = (
        getattr(request.app.state, "orchestrator_settings", None) or get_orchestrator_settings()
    )
    engine_client: EngineClient | None = getattr(request.app.state, "engine_client", None)
    app_ro_engine: AsyncEngine | None = getattr(request.app.state, "app_ro_engine", None)
    md_ro_engine: AsyncEngine | None = getattr(request.app.state, "md_ro_engine", None)

    md_check, engine_check, db_app_check, db_md_check = await asyncio.gather(
        _probe_md(settings),
        _probe_engine(engine_client),
        _probe_db(app_ro_engine, name="app_ro"),
        _probe_db(md_ro_engine, name="md_ro"),
    )

    checks: dict[str, CheckResult] = {
        "md": md_check,
        "engine": engine_check,
        "db_app": db_app_check,
        "db_md": db_md_check,
    }

    all_ok = all(c.status in _OK_STATES for c in checks.values())
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        failing: dict[str, Any] = {
            name: check.detail or check.status
            for name, check in checks.items()
            if check.status == "fail"
        }
        _log.warning("orchestrator.ready.not_ready", failing=failing)
    return ReadyResponse(
        status="ready" if all_ok else "not_ready",
        checks=checks,
    )


__all__ = ["router"]
