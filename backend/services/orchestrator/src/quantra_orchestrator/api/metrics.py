"""``GET /metrics`` — Prometheus exposition.

Renders the orchestrator-scoped :class:`~quantra_orchestrator.observability.OrchestratorMetrics`
registry as the Prometheus text exposition format. Anonymous on
purpose (matches the convention every scrape pipeline expects); the
endpoint surfaces only counters / histograms / gauges declared in
:mod:`quantra_orchestrator.observability` and contains no per-user
data.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from quantra_orchestrator.observability import (
    METRICS_CONTENT_TYPE,
    render_metrics,
)

router = APIRouter()


@router.get(
    "/metrics",
    tags=["meta"],
    summary="Prometheus exposition for orchestrator-scoped metrics.",
    responses={
        200: {
            "description": (
                "Prometheus text-format exposition. Counters: "
                "``orchestrator_md_requests_total`` / "
                "``orchestrator_engine_requests_total`` / "
                "``orchestrator_pricing_requests_total``. Histograms: "
                "the corresponding ``_seconds`` siblings. Gauges: "
                "``orchestrator_quote_cache_size`` + "
                "``orchestrator_pinned_snapshot_age_seconds``."
            ),
            "content": {"text/plain": {}},
        },
    },
)
def metrics(request: Request) -> Response:
    """Return the registry as a Prometheus exposition body."""

    body = render_metrics(request)
    return Response(content=body, media_type=METRICS_CONTENT_TYPE)


__all__ = ["router"]
