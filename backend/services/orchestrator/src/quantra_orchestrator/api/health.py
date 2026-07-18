"""``GET /health`` — orchestrator liveness probe.

Returns the four fields enumerated:
``status``, ``service``, ``build_sha`` (stuff the build SHA into
``/health`` instead of standing up a separate ``/version`` endpoint),
and ``env``. Never touches Postgres, the MD service, or the engine —
the dependencies that need readiness checks come with their wiring
plans.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from quantra_orchestrator.schemas import HealthResponse
from quantra_orchestrator.settings import (
    OrchestratorSettings,
    get_orchestrator_settings,
)

router = APIRouter()


@router.get("/health", response_model=HealthResponse, tags=["meta"])
def health(
    settings: OrchestratorSettings = Depends(get_orchestrator_settings),
) -> HealthResponse:
    """Liveness — no dependencies, no I/O."""

    return HealthResponse(
        status="ok",
        service="quantra-orchestrator",
        build_sha=settings.build_sha,
        env=settings.env.value,
    )
