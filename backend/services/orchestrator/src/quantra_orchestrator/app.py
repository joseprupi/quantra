"""FastAPI app factory.

Building the app via a function (rather than a module-level singleton)
keeps import-time side effects to zero — the same contract the MD
service follows so ``import quantra_orchestrator`` never depends on env
vars being set. Long-lived resources attach their lifecycle to the
lifespan handler defined here.

What the factory wires:

- **Auth** — an optional ``api_key_lookup`` injection seam so tests /
  demos can swap an in-memory fake for the production
  ``SqlApiKeyLookup``, and an optional ``firebase_verifier`` seam
  (matching ``quantra_common.auth``).
- **Database engines** — lifespan-owned ``app_rw`` + ``app_ro`` engines
  parked on ``app.state`` for the data routers (via
  ``Depends(get_app_rw_engine)`` / ``Depends(get_app_ro_engine)``); the
  same ``app_ro`` engine backs ``SqlApiKeyLookup`` so we don't open two
  read-only pools. Optional ``app_rw_engine=`` / ``app_ro_engine=``
  injection seams let tests pass mocks without going through the
  lifespan. An ``md_rw`` engine backs the market-data import path.
- **MD client** — lifespan-owned :class:`MdClient` +
  :class:`TtlBoundedQuoteCache` parked on ``app.state.md_client`` /
  ``app.state.md_cache`` (one HTTP pool / one bounded LRU per process —
  never per request), with ``md_client=`` / ``md_cache=`` injection
  seams. Debug routes ``GET /debug/md/quote/{canonical_id}`` and
  ``GET /debug/md/cache/stats`` exist for verification only — they are
  not part of the public API.
- **Engine client** — lifespan-owned :class:`EngineClient` parked on
  ``app.state.engine_client`` (one gRPC channel per process — never per
  request). The real ``grpc.aio``-backed client is wired when
  ``ENGINE_GRPC_TARGET`` is set; without it the factory falls back to
  :class:`StubEngineClient` (every pricing call fails loud with a clear
  error instead of a hang). Optional ``engine_client=`` injection seam;
  debug routes ``GET /debug/engine/ping`` / ``GET /debug/engine/info``
  (same not-public-API caveat).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_common.auth.firebase import FirebaseTokenVerifier
from quantra_common.auth.lookup import ApiKeyLookup
from quantra_common.db.engine import make_app_engine, make_md_engine
from quantra_common.engine_client import EngineClient
from quantra_common.logging import RequestIdMiddleware, configure_logging
from quantra_common.md_client import MdClient
from quantra_common.settings import Environment
from quantra_orchestrator.api import register_all
from quantra_orchestrator.api.version import ORCHESTRATOR_VERSION
from quantra_orchestrator.auth.api_keys import SqlApiKeyLookup
from quantra_orchestrator.engine import build_engine_client
from quantra_orchestrator.errors import register_exception_handlers
from quantra_orchestrator.md import TtlBoundedQuoteCache, build_md_client
from quantra_orchestrator.observability import (
    InstrumentedEngineClient,
    InstrumentedMdClient,
    OrchestratorMetrics,
    build_metrics,
)
from quantra_orchestrator.observability.metrics import PricingMetricsMiddleware
from quantra_orchestrator.settings import (
    OrchestratorSettings,
    get_orchestrator_settings,
)
from quantra_orchestrator.tracing import TraceFlushMiddleware

_log = structlog.get_logger(__name__)


@dataclass(slots=True)
class _LifespanOwned:
    """Track resources the lifespan opened (vs. resources the caller injected).

    Disposed in reverse-of-construction order in the ``finally`` of the
    lifespan context manager. A ``None`` slot means the caller injected
    that resource (a test, typically) and we must not touch it.
    """

    app_rw_engine: AsyncEngine | None = None
    app_ro_engine: AsyncEngine | None = None
    md_ro_engine: AsyncEngine | None = None
    md_rw_engine: AsyncEngine | None = None
    md_client: MdClient | None = None
    md_transport: httpx.AsyncClient | None = None
    engine_client: EngineClient | None = None


def _open_app_engines(app_: FastAPI, cfg: OrchestratorSettings, owned: _LifespanOwned) -> None:
    if getattr(app_.state, "app_ro_engine", None) is None and cfg.postgres_dsn_app_ro:
        engine = make_app_engine("ro", settings=cfg)
        app_.state.app_ro_engine = engine
        app_.state.owned_app_ro_engine = engine
        owned.app_ro_engine = engine
    if getattr(app_.state, "app_rw_engine", None) is None and cfg.postgres_dsn_app_rw:
        engine = make_app_engine("rw", settings=cfg)
        app_.state.app_rw_engine = engine
        app_.state.owned_app_rw_engine = engine
        owned.app_rw_engine = engine
    # Open a read-only ``md.*`` engine when a DSN is
    # configured so ``/ready`` can ``SELECT 1`` it independently of
    # the MD service ``/health`` probe (catches the class where MD is
    # up but its DB pool is wedged). Stays optional: when no DSN is
    # set, ``/ready`` reports ``db_md`` as ``skipped`` and does not
    # gate readiness on it.
    if getattr(app_.state, "md_ro_engine", None) is None and cfg.postgres_dsn_md_ro:
        engine = make_md_engine("ro", settings=cfg)
        app_.state.md_ro_engine = engine
        app_.state.owned_md_ro_engine = engine
        owned.md_ro_engine = engine
    # open a WRITE ``md.*`` engine when ``POSTGRES_DSN_MD_RW`` is
    # configured so the sanctioned market-data import route can upsert
    # user quotes into the shared catalog. Kept SEPARATE from the
    # request-path ``md_ro`` reader above (its own pool) so a
    # user-import spike cannot starve read traffic. Stays optional: when
    # no DSN is set the import route returns a 503 ``storage_unavailable``
    # envelope rather than 500.
    if getattr(app_.state, "md_rw_engine", None) is None and cfg.postgres_dsn_md_rw:
        engine = make_md_engine("rw", settings=cfg)
        app_.state.md_rw_engine = engine
        app_.state.owned_md_rw_engine = engine
        owned.md_rw_engine = engine


def _wire_api_key_lookup(app_: FastAPI) -> None:
    # The SQL lookup shares the read-only engine with the data layer;
    # if the caller didn't inject a custom lookup, wire one now using
    # whichever ``app_ro`` engine we have on hand.
    if getattr(app_.state, "api_key_lookup", None) is not None:
        return
    ro_engine: AsyncEngine | None = app_.state.app_ro_engine
    if ro_engine is not None:
        app_.state.api_key_lookup = SqlApiKeyLookup(ro_engine)


def _open_engine_client(app_: FastAPI, cfg: OrchestratorSettings, owned: _LifespanOwned) -> None:
    # The engine client is *always* provisioned (unlike the MD client,
    # which short-circuits when MD_SERVICE_URL is unset) — the default
    # is the StubEngineClient that raises NotImplementedError per RPC,
    # mapped by the error handler to a 502 ``engine_unavailable``
    # envelope. The channel + interceptors are owned by
    # the lifespan so every request shares them.
    # Wrap in the metrics-observing adapter so every
    # engine RPC contributes to ``orchestrator_engine_requests_total``
    # and ``orchestrator_engine_request_seconds``. The adapter
    # delegates verbatim; no behaviour change beyond the counter +
    # histogram observations.
    if getattr(app_.state, "engine_client", None) is not None:
        return
    client = build_engine_client(cfg)
    metrics: OrchestratorMetrics | None = getattr(app_.state, "metrics", None)
    if metrics is not None:
        client = InstrumentedEngineClient(client, metrics)
    app_.state.engine_client = client
    owned.engine_client = client


def _open_md_client(app_: FastAPI, cfg: OrchestratorSettings, owned: _LifespanOwned) -> None:
    # Always provision a cache (cheap, in-process); the LRU + stats
    # surface is what feeds /debug/md/cache/stats. The MD client itself
    # only spins up when MD_SERVICE_URL is set so a scaffold-only
    # deploy stays serviceable.
    if getattr(app_.state, "md_cache", None) is None:
        app_.state.md_cache = TtlBoundedQuoteCache(
            max_entries=cfg.md_cache_max_entries,
            ttl_s=cfg.md_cache_ttl_s,
        )
    if getattr(app_.state, "md_client", None) is not None or not cfg.md_service_url:
        return
    client, transport = build_md_client(
        cfg,
        cache=app_.state.md_cache,
        request_id_header=cfg.request_id_header,
    )
    metrics: OrchestratorMetrics | None = getattr(app_.state, "metrics", None)
    if metrics is not None:
        client = InstrumentedMdClient(client, metrics)
    app_.state.md_client = client
    app_.state.md_transport = transport
    owned.md_client = client
    owned.md_transport = transport


async def _dispose_lifespan_owned(app_: FastAPI, owned: _LifespanOwned) -> None:
    if owned.engine_client is not None:
        await owned.engine_client.close()
        app_.state.engine_client = None
    if owned.md_client is not None:
        await owned.md_client.aclose()
        app_.state.md_client = None
    if owned.md_transport is not None:
        await owned.md_transport.aclose()
        app_.state.md_transport = None
    if owned.app_rw_engine is not None:
        await owned.app_rw_engine.dispose()
        app_.state.owned_app_rw_engine = None
    if owned.app_ro_engine is not None:
        await owned.app_ro_engine.dispose()
        app_.state.owned_app_ro_engine = None
    if owned.md_ro_engine is not None:
        await owned.md_ro_engine.dispose()
        app_.state.owned_md_ro_engine = None
    if owned.md_rw_engine is not None:
        await owned.md_rw_engine.dispose()
        app_.state.owned_md_rw_engine = None


def create_app(
    settings: OrchestratorSettings | None = None,
    *,
    api_key_lookup: ApiKeyLookup | None = None,
    firebase_verifier: FirebaseTokenVerifier | None = None,
    app_rw_engine: AsyncEngine | None = None,
    app_ro_engine: AsyncEngine | None = None,
    md_rw_engine: AsyncEngine | None = None,
    md_client: MdClient | None = None,
    md_cache: TtlBoundedQuoteCache | None = None,
    engine_client: EngineClient | None = None,
) -> FastAPI:
    """Build a fully-wired FastAPI app for the orchestrator.

    Production callers pass nothing (or the cached settings instance):
    the lifespan opens ``app_rw`` + ``app_ro`` engines, wraps the
    latter in ``SqlApiKeyLookup`` so the API-key dependency has a
    backing store, constructs the singleton :class:`MdClient` (one
    HTTP pool per process) when ``MD_SERVICE_URL`` is set, and
    constructs the singleton :class:`EngineClient` (one gRPC channel
    per process when ``ENGINE_GRPC_TARGET`` is set, otherwise the
    stub).

    Tests pass ``api_key_lookup=`` / ``firebase_verifier=`` /
    ``app_rw_engine=`` / ``app_ro_engine=`` / ``md_client=`` /
    ``md_cache=`` / ``engine_client=`` directly to skip any real I/O
    — the injected callables are attached to ``app.state`` before
    ``create_app`` returns so test clients that don't enter the
    lifespan context still see the overrides.
    """

    cfg = settings or get_orchestrator_settings()
    # defense in depth. ``OrchestratorSettings`` already refuses to
    # validate with the auth bypass on under ``ENV=prod``; re-assert it here
    # at app-construction time so any path that hands us a hand-built ``cfg``
    # (tests, embedders) still cannot boot a world-open API in production.
    if cfg.dev_auth_bypass and cfg.env is Environment.PROD:
        msg = (
            "Refusing to start: DEV_AUTH_BYPASS=true with ENV=prod. "
            "The auth bypass disables all credential checks; it must never "
            "run in production."
        )
        raise RuntimeError(msg)
    configure_logging(cfg)

    @asynccontextmanager
    async def _lifespan(app_: FastAPI) -> AsyncIterator[None]:
        if cfg.dev_auth_bypass:
            _log.warning(
                "orchestrator.auth.dev_bypass_enabled",
                banner=(
                    "############################################################\n"
                    "## DEV_AUTH_BYPASS IS ON — ALL AUTH IS DISABLED.          ##\n"
                    "## Every request resolves to uid='dev-user'.             ##\n"
                    "## DEV ONLY. Never run this with real users / data.       ##\n"
                    "############################################################"
                ),
            )
        owned = _LifespanOwned()
        _open_app_engines(app_, cfg, owned)
        _wire_api_key_lookup(app_)
        _open_md_client(app_, cfg, owned)
        _open_engine_client(app_, cfg, owned)
        try:
            yield
        finally:
            await _dispose_lifespan_owned(app_, owned)

    app = FastAPI(
        title="Quantra API",
        version=ORCHESTRATOR_VERSION,
        description=(
            "Quantra's pricing orchestrator — the single public REST surface "
            "over the open-source quantraserver pricing engine. Hosts the "
            "per-product pricing endpoints (POST /v1/price/*), CRUD for "
            "user-owned reference data and saved products (/v1/*), curve "
            "preview, vol and calendar tools, market-data import/series "
            "routes, and pricing traces. Market data referenced by quote id "
            "is resolved server-side before the engine is called; the engine "
            "only ever sees fully self-contained requests."
        ),
        lifespan=_lifespan,
        openapi_tags=[
            {
                "name": "meta",
                "description": "Service liveness / readiness / metadata endpoints.",
            },
            {
                "name": "auth",
                "description": "Authentication debug endpoints (/auth/whoami).",
            },
            {
                "name": "debug",
                "description": (
                    "Verification-only routes for ops / smoke tests. NOT a "
                    "public API surface — these may change or be removed at "
                    "any time."
                ),
            },
        ],
    )

    # Eagerly attach the injected overrides so tests that bypass the
    # lifespan context still see them; the lifespan only constructs a
    # default when none was supplied. ``state`` accepts arbitrary
    # attribute assignment.
    app.state.api_key_lookup = api_key_lookup
    app.state.firebase_verifier = firebase_verifier
    app.state.app_rw_engine = app_rw_engine
    app.state.app_ro_engine = app_ro_engine
    app.state.md_ro_engine = None
    app.state.md_rw_engine = md_rw_engine
    app.state.owned_app_rw_engine = None
    app.state.owned_app_ro_engine = None
    app.state.owned_md_ro_engine = None
    app.state.owned_md_rw_engine = None
    app.state.md_client = md_client
    app.state.md_cache = md_cache
    app.state.md_transport = None
    app.state.engine_client = engine_client
    # Build the process-wide Prometheus registry up-front
    # so middleware and the lifespan-owned MD/engine wrappers can
    # observe through it. ``build_metrics`` constructs an independent
    # :class:`CollectorRegistry` per app so test suites that build
    # many apps in one process don't trip duplicate-registration
    # errors.
    app.state.metrics = build_metrics()
    # Park the resolved settings on app.state so the engine /info debug
    # route (and any future per-app introspection) reads the same
    # instance ``create_app`` was built with — same seam routes
    # already use via ``Depends(get_orchestrator_settings)``.
    app.state.orchestrator_settings = cfg

    # Record per-product pricing route latency + outcome via an
    # ASGI middleware so a 5xx that bypasses the handler (e.g.
    # validation error) is still counted. Stacked under
    # ``RequestIdMiddleware`` so the request-id label is in scope for
    # any future per-request structured logging the metrics
    # middleware might want to emit.
    app.add_middleware(PricingMetricsMiddleware)
    # flush each pricing route's TraceRecorder after the response
    # body is sent. Stacked under ``RequestIdMiddleware`` so the request
    # id is bound while the flush logs any best-effort write failure.
    # The flush is invisible to the caller (runs post-response, swallows
    # every error) so it can never fail or slow a price.
    app.add_middleware(TraceFlushMiddleware)
    app.add_middleware(RequestIdMiddleware, settings=cfg)

    # DEV-ONLY cross-origin support. ``create_app`` ships NO CORS by
    # default (production fronts the orchestrator with a reverse proxy
    # that owns CORS/TLS); the middleware is installed only when
    # ``DEV_CORS_ORIGINS`` lists at least one origin. This lets the
    # Vite-served portal call ``python -m quantra_orchestrator`` directly
    # in local development. Credentials are
    # off because auth travels as a Bearer header, not a cookie; the
    # request-id header the portal mints is exposed so the browser can
    # read it back off the response for support correlation.
    cors_origins = cfg.cors_allow_origins()
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=[cfg.request_id_header],
        )

    # Make routes that ``Depends(get_orchestrator_settings)`` see the same
    # settings instance the factory was constructed with. In production
    # this is a no-op (``cfg`` came from ``get_orchestrator_settings``);
    # in tests / scripted demos it lets a caller inject settings without
    # mutating the process-cached singleton.
    app.dependency_overrides[get_orchestrator_settings] = lambda: cfg

    register_exception_handlers(app, settings=cfg)
    register_all(app)
    return app
