"""``GET /v1/version`` — orchestrator + pricing-engine versions.

Reports the orchestrator's own version (the single source of truth also
handed to FastAPI in ``app.create_app``) and the pricing-engine version so
the portal's About panel can display the backend and engine versions. The
portal reports its own version itself.

Dependency-free, exactly like ``GET /health``: no Postgres, no MD, no
engine call. It must answer even if the engine is down. Auth is the standard
``get_auth_context`` dependency (dev-bypass aware), matching the other
``/v1`` routes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from quantra_common.auth.context import AuthContext
from quantra_orchestrator.auth.dependencies import get_auth_context
from quantra_orchestrator.schemas import (
    EngineVersion,
    OrchestratorVersion,
    VersionResponse,
)
from quantra_orchestrator.settings import (
    OrchestratorSettings,
    get_orchestrator_settings,
)

# Single source of truth for the orchestrator's version. ``create_app`` reads
# this same constant when constructing the FastAPI app, so the value reported
# by ``GET /v1/version`` and the OpenAPI ``info.version`` can never drift.
ORCHESTRATOR_VERSION = "0.4.2"

# HARDCODED STOPGAP (backlog F3): the pricing engine does not yet expose its
# version over its gRPC surface. Until it does, we report this constant and
# flag ``source="hardcoded"`` so consumers can tell it is a placeholder. Keep
# it in sync with the engine image pinned in the compose files. When F3
# lands, replace this constant + the ``hardcoded`` source with a real gRPC
# meta call. Do NOT derive this from the image tag.
ENGINE_VERSION = "0.5.0"

router = APIRouter(prefix="/v1", tags=["meta"])


@router.get("/version", response_model=VersionResponse)
def version(
    _ctx: AuthContext = Depends(get_auth_context),
    settings: OrchestratorSettings = Depends(get_orchestrator_settings),
) -> VersionResponse:
    """Report orchestrator + engine versions — no downstream dependencies."""

    return VersionResponse(
        orchestrator=OrchestratorVersion(
            version=ORCHESTRATOR_VERSION,
            build_sha=settings.build_sha,
        ),
        engine=EngineVersion(
            version=ENGINE_VERSION,
            source="hardcoded",
        ),
    )
