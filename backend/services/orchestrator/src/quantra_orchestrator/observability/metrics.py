"""Process-wide Prometheus metrics registry + instrumented client wrappers.

Every metric the operator can scrape from ``GET /metrics`` is declared
here so the metric *names + labels* are reviewable in one place. The
registry is owned by the lifespan (parked on
``app.state.metrics``) and disposed automatically with the app.

Wrapper clients
---------------

:class:`InstrumentedMdClient` and :class:`InstrumentedEngineClient`
subclass the public client classes so the existing ``isinstance``
checks in :func:`quantra_orchestrator.md.get_md_client` and
:func:`quantra_orchestrator.engine.get_engine_client` still pass.
They delegate every call to the wrapped instance and observe a counter
+ histogram around the call boundary; they do not change the upstream
behaviour in any way (no retries, no caching, no transformation of the
response or the exception).

Per-route pricing latency is captured by a separate ASGI middleware
(:class:`PricingMetricsMiddleware`), not here — the middleware sees the
matched route template (``/v1/price/swap/ir`` etc.) and is the cheapest
place to instrument every per-product endpoint uniformly.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from time import perf_counter
from typing import Final

from fastapi import Request
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from quantra_common.engine_client import EngineClient
from quantra_common.engine_client.errors import (
    EngineClientError,
)
from quantra_common.engine_client.rpcs import EngineRpc
from quantra_common.md_client import MdClient, MdClientError
from quantra_common.types import ResolvedQuote

# ---------------------------------------------------------------------------
# Public exposition content-type
# ---------------------------------------------------------------------------

METRICS_CONTENT_TYPE: Final[str] = CONTENT_TYPE_LATEST

# HTTP status code thresholds for outcome bucketing on
# ``orchestrator_pricing_requests_total`` (avoids PLR2004 magic values).
_HTTP_OK_FLOOR: Final[int] = 200
_HTTP_REDIRECT_CEILING: Final[int] = 400


# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class OrchestratorMetrics:
    """Bundle of every Prometheus metric the orchestrator exposes.

    Instances are 1-per-process; the lifespan builds one via
    :func:`build_metrics` and parks it on ``app.state.metrics``. Tests
    that need an isolated registry build their own instance with a
    fresh :class:`CollectorRegistry`.
    """

    registry: CollectorRegistry
    # MD upstream
    md_requests_total: Counter
    md_request_seconds: Histogram
    # Quote cache (snapshot read from cache.stats() at scrape time)
    quote_cache_hits_total: Counter
    quote_cache_misses_total: Counter
    quote_cache_expirations_total: Counter
    quote_cache_size: Gauge
    # Per-product pricing routes
    pricing_requests_total: Counter
    pricing_request_seconds: Histogram
    # Engine upstream
    engine_requests_total: Counter
    engine_request_seconds: Histogram
    # Snapshot freshness — age (s) of the most recently resolved
    # ``md.snapshots`` row that priced a request. Updated by the
    # assembler when an etag-pinned snapshot is loaded; surfaced as
    # a gauge.
    snapshot_age_seconds: Gauge


_PRICING_LATENCY_BUCKETS: Final[tuple[float, ...]] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)
# MD and engine upstream tend to live in the few-ms to few-seconds
# band; shared buckets keep the exposition compact.
_UPSTREAM_LATENCY_BUCKETS: Final[tuple[float, ...]] = _PRICING_LATENCY_BUCKETS


def build_metrics(registry: CollectorRegistry | None = None) -> OrchestratorMetrics:
    """Construct a fresh metrics bundle.

    Each call uses an independent :class:`CollectorRegistry` so unit
    tests that build many apps in one process do not accumulate
    duplicate-registration errors against the global default registry.
    """

    reg = registry if registry is not None else CollectorRegistry()
    return OrchestratorMetrics(
        registry=reg,
        md_requests_total=Counter(
            "orchestrator_md_requests_total",
            "MD upstream HTTP calls issued by the orchestrator.",
            labelnames=("operation", "outcome"),
            registry=reg,
        ),
        md_request_seconds=Histogram(
            "orchestrator_md_request_seconds",
            "Wall-clock latency of MD upstream HTTP calls.",
            labelnames=("operation",),
            buckets=_UPSTREAM_LATENCY_BUCKETS,
            registry=reg,
        ),
        quote_cache_hits_total=Counter(
            "orchestrator_quote_cache_hits_total",
            "Quote-cache hits served without an MD upstream call.",
            registry=reg,
        ),
        quote_cache_misses_total=Counter(
            "orchestrator_quote_cache_misses_total",
            "Quote-cache misses that forced an MD upstream call.",
            registry=reg,
        ),
        quote_cache_expirations_total=Counter(
            "orchestrator_quote_cache_expirations_total",
            "Quote-cache entries dropped on TTL expiry.",
            registry=reg,
        ),
        quote_cache_size=Gauge(
            "orchestrator_quote_cache_size",
            "Current quote-cache size (entries).",
            registry=reg,
        ),
        pricing_requests_total=Counter(
            "orchestrator_pricing_requests_total",
            "Per-product pricing HTTP requests handled.",
            labelnames=("route", "outcome"),
            registry=reg,
        ),
        pricing_request_seconds=Histogram(
            "orchestrator_pricing_request_seconds",
            "Wall-clock latency of per-product pricing HTTP requests.",
            labelnames=("route",),
            buckets=_PRICING_LATENCY_BUCKETS,
            registry=reg,
        ),
        engine_requests_total=Counter(
            "orchestrator_engine_requests_total",
            "Engine gRPC RPCs issued by the orchestrator.",
            labelnames=("rpc", "outcome"),
            registry=reg,
        ),
        engine_request_seconds=Histogram(
            "orchestrator_engine_request_seconds",
            "Wall-clock latency of engine gRPC RPCs.",
            labelnames=("rpc",),
            buckets=_UPSTREAM_LATENCY_BUCKETS,
            registry=reg,
        ),
        snapshot_age_seconds=Gauge(
            "orchestrator_pinned_snapshot_age_seconds",
            "Age in seconds of the most recently priced etag-pinned snapshot.",
            registry=reg,
        ),
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _set_counter_total(counter: Counter, target_total: float) -> None:
    """Reconcile a ``Counter`` to a known absolute total.

    Prometheus counters monotonically increase via ``inc(amount)``; the
    quote cache owns the canonical hit/miss/expiration totals
    internally (in ``TtlBoundedQuoteCache.stats()``) and we mirror
    those into the registry at scrape time. The reconciliation is a
    one-shot ``inc(delta)``: we read the counter's current value, then
    increment by the gap to the cache's running total. If the cache
    counter ever rolls backward (it does not today; the cache only
    counts up) the delta is clamped to zero so the Prometheus counter
    invariant is preserved.
    """

    # ``_value`` exposes the protected sample-store on the counter; using
    # ``Counter.collect()`` to read the current total is the documented
    # public alternative. We pick the latter to keep the interaction in
    # one supported API.
    samples = next(iter(counter.collect()), None)
    current = 0.0
    if samples is not None:
        for sample in samples.samples:
            if sample.name.endswith("_total"):
                current = float(sample.value)
                break
    delta = target_total - current
    if delta > 0:
        counter.inc(delta)


def _refresh_cache_snapshot(request_or_app_state: object, metrics: OrchestratorMetrics) -> None:
    """Update the quote-cache gauges from the live ``TtlBoundedQuoteCache.stats()``.

    The cache owns the canonical counters internally; we mirror them
    into Prometheus at scrape time rather than incrementing on every
    hit/miss so the source of truth stays in one place (the cache's
    ``stats()`` method, the same surface ``/debug/md/cache/stats``
    reads).
    """

    cache = getattr(request_or_app_state, "md_cache", None)
    if cache is None or not hasattr(cache, "stats"):
        return
    stats = cache.stats()
    _set_counter_total(metrics.quote_cache_hits_total, float(stats.hits))
    _set_counter_total(metrics.quote_cache_misses_total, float(stats.misses))
    _set_counter_total(metrics.quote_cache_expirations_total, float(stats.expirations))
    metrics.quote_cache_size.set(float(stats.size))


def render_metrics(request: Request) -> bytes:
    """Render the registry as Prometheus exposition (text format)."""

    metrics: OrchestratorMetrics | None = getattr(request.app.state, "metrics", None)
    if metrics is None:
        # No metrics configured on this app — return an empty body so
        # the route at least surfaces the content-type without
        # confusing operators with a 500. Tests that need to assert
        # /metrics is wired check for one of the canonical lines.
        return b""
    _refresh_cache_snapshot(request.app.state, metrics)
    return generate_latest(metrics.registry)


def get_metrics(request: Request) -> OrchestratorMetrics | None:
    """Return the app-scoped :class:`OrchestratorMetrics`, or ``None``.

    Per-product pricing routes call this to record outcomes/latency.
    Returning ``None`` (instead of raising) keeps the instrumentation
    a soft dependency: a test app that doesn't install metrics still
    serves pricing without spurious 500s.
    """

    return getattr(request.app.state, "metrics", None)


# ---------------------------------------------------------------------------
# Instrumented client wrappers
# ---------------------------------------------------------------------------


def _md_outcome_label(exc: BaseException | None) -> str:
    if exc is None:
        return "ok"
    if isinstance(exc, MdClientError):
        return type(exc).__name__
    return "error"


def _engine_outcome_label(exc: BaseException | None) -> str:
    if exc is None:
        return "ok"
    if isinstance(exc, EngineClientError):
        return type(exc).__name__
    if isinstance(exc, NotImplementedError):
        return "stub_not_implemented"
    return "error"


class InstrumentedMdClient(MdClient):
    """:class:`MdClient` subclass that records per-call metrics.

    Subclasses (rather than wraps) ``MdClient`` so the
    ``isinstance(client, MdClient)`` guard in
    :func:`quantra_orchestrator.md.get_md_client` keeps holding.
    The wrapped instance owns the underlying ``httpx`` transport and
    cache; this subclass re-uses those pieces verbatim so the
    singleton invariant (one HTTP pool per process) is preserved.
    """

    def __init__(self, inner: MdClient, metrics: OrchestratorMetrics) -> None:
        # Re-use the inner client's pieces (config / cache / transport)
        # so there is exactly one source of truth per process. We
        # explicitly pass ``client=inner._client`` into the base
        # ``MdClient.__init__`` so ``MdClient.aclose`` on this wrapper
        # does not close a transport the lifespan owns (per the
        # ``_owns_client`` invariant in ``MdClient.__init__``).
        super().__init__(
            inner._config,
            cache=inner._cache,
            client=inner._client,
        )
        self._inner = inner
        self._metrics = metrics

    async def resolve_quotes(
        self,
        canonical_ids: list[str],
        as_of: date | datetime,
        *,
        snapshot_version: str | None = None,
    ) -> list[ResolvedQuote]:
        return await self._observe_list_resolved(
            "resolve_quotes",
            self._inner.resolve_quotes(
                canonical_ids,
                as_of,
                snapshot_version=snapshot_version,
            ),
        )

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def _observe_list_resolved(
        self,
        operation: str,
        awaitable: Awaitable[list[ResolvedQuote]],
    ) -> list[ResolvedQuote]:
        start = perf_counter()
        exc: BaseException | None = None
        try:
            return await awaitable
        except BaseException as e:
            exc = e
            raise
        finally:
            self._record_md(operation, exc, perf_counter() - start)

    def _record_md(
        self,
        operation: str,
        exc: BaseException | None,
        elapsed: float,
    ) -> None:
        self._metrics.md_request_seconds.labels(operation=operation).observe(elapsed)
        self._metrics.md_requests_total.labels(
            operation=operation,
            outcome=_md_outcome_label(exc),
        ).inc()


class InstrumentedEngineClient(EngineClient):
    """:class:`EngineClient` subclass that records per-call metrics."""

    def __init__(self, inner: EngineClient, metrics: OrchestratorMetrics) -> None:
        self._inner = inner
        self._metrics = metrics

    async def call(self, rpc: EngineRpc, request_bytes: bytes) -> bytes:
        start = perf_counter()
        exc: BaseException | None = None
        try:
            return await self._inner.call(rpc, request_bytes)
        except BaseException as e:
            exc = e
            raise
        finally:
            elapsed = perf_counter() - start
            self._metrics.engine_request_seconds.labels(rpc=rpc.value).observe(elapsed)
            self._metrics.engine_requests_total.labels(
                rpc=rpc.value,
                outcome=_engine_outcome_label(exc),
            ).inc()

    async def close(self) -> None:
        await self._inner.close()


# ---------------------------------------------------------------------------
# Pricing-route middleware
# ---------------------------------------------------------------------------


def record_pricing_outcome(
    metrics: OrchestratorMetrics | None,
    route: str,
    *,
    status_code: int,
    elapsed_s: float,
) -> None:
    """Record one per-product pricing request.

    Centralised so the middleware (which calls this once per request)
    and any future code path that wants to record a manual outcome
    share one shape.
    """

    if metrics is None:
        return
    is_ok = _HTTP_OK_FLOOR <= status_code < _HTTP_REDIRECT_CEILING
    outcome = "ok" if is_ok else f"http_{status_code // 100}xx"
    metrics.pricing_requests_total.labels(route=route, outcome=outcome).inc()
    metrics.pricing_request_seconds.labels(route=route).observe(elapsed_s)


# Type alias for the FastAPI receive/send callables used by middleware
_Receive = Callable[[], Awaitable[dict[str, object]]]
_Send = Callable[[dict[str, object]], Awaitable[None]]
_AsgiApp = Callable[[dict[str, object], _Receive, _Send], Awaitable[None]]


class PricingMetricsMiddleware:
    """ASGI middleware recording per-product pricing latency + outcome.

    Lives outside the request handler so a 5xx that bypasses the
    handler (e.g. validation error) is still counted. The route
    template (the ``/v1/price/<product>`` family) is the metric label,
    matched at scope-resolution time so high-cardinality path params
    (``/v1/snapshots/<id>`` etc.) never leak in.
    """

    _PREFIX: Final[str] = "/v1/price"

    def __init__(self, app: _AsgiApp) -> None:
        self._app = app

    async def __call__(
        self,
        scope: dict[str, object],
        receive: _Receive,
        send: _Send,
    ) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        raw_path = scope.get("path")
        path: str = raw_path if isinstance(raw_path, str) else ""
        if not path.startswith(self._PREFIX):
            await self._app(scope, receive, send)
            return

        status_holder: list[int] = [500]

        async def _send(message: dict[str, object]) -> None:
            if message.get("type") == "http.response.start":
                code = message.get("status")
                if isinstance(code, int):
                    status_holder[0] = code
            await send(message)

        start = perf_counter()
        try:
            await self._app(scope, receive, _send)
        finally:
            elapsed = perf_counter() - start
            app_obj = scope.get("app")
            state = getattr(app_obj, "state", None)
            metrics: OrchestratorMetrics | None = (
                getattr(state, "metrics", None) if state is not None else None
            )
            record_pricing_outcome(
                metrics,
                route=path,
                status_code=status_holder[0],
                elapsed_s=elapsed,
            )


__all__ = [
    "METRICS_CONTENT_TYPE",
    "InstrumentedEngineClient",
    "InstrumentedMdClient",
    "OrchestratorMetrics",
    "PricingMetricsMiddleware",
    "build_metrics",
    "get_metrics",
    "render_metrics",
]
