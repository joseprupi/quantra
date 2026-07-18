"""Observability surfaces for the orchestrator.

Owns the process-wide Prometheus :class:`CollectorRegistry`, the
metric objects every instrumented call site updates, and the wrapper
clients that adapt :class:`MdClient` / :class:`EngineClient` to record
upstream counters + latency histograms without changing the public
client APIs.

The exposition format is rendered by :func:`render_metrics` (used by
the ``/metrics`` route) and parses cleanly under
``prometheus_client.parser`` (asserted in tests).

Distinct from ``/health`` (process-up) and ``/ready`` (dependency
readiness) by design — operators wire the three to different
container-orchestrator probes.
"""

from quantra_orchestrator.observability.metrics import (
    METRICS_CONTENT_TYPE,
    InstrumentedEngineClient,
    InstrumentedMdClient,
    OrchestratorMetrics,
    build_metrics,
    get_metrics,
    render_metrics,
)

__all__ = [
    "METRICS_CONTENT_TYPE",
    "InstrumentedEngineClient",
    "InstrumentedMdClient",
    "OrchestratorMetrics",
    "build_metrics",
    "get_metrics",
    "render_metrics",
]
