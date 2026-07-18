"""Service-local settings layered on top of `quantra_common.settings`.

`quantra_common.Settings` carries the cross-service config (DSNs, pool
sizes, log level, request-id header). The orchestrator adds two
service-local fields it surfaces in ``GET /health``:

- ``orchestrator_port`` — the uvicorn bind port for ``python -m
  quantra_orchestrator``.
- ``build_sha`` — the build / commit identifier reported by ``/health``.
  Defaults to ``"dev"`` so the model imports cleanly outside of CI; the
  real value is injected at image build time via the ``BUILD_SHA`` env
  var (the Dockerfile passes it through).

The MD-client knobs (``md_client_max_retries`` etc.) layer on top of the
shared ``md_service_url`` / ``md_service_timeout_s`` from the base
settings. They land here so the orchestrator can tune retry / cache
behavior without dragging the ingester (which doesn't share the same
retry budget) into the same config knobs.

The engine-client knobs (``engine_grpc_target`` etc.) layer on top of
the base ``engine_grpc_addr`` / ``engine_grpc_timeout_s``. The orchestrator picks
``engine_grpc_target`` over the legacy base ``engine_grpc_addr`` so the
gating env var (``ENGINE_GRPC_TARGET``) is unambiguous about *which*
process owns the channel — only the orchestrator does. The retry knobs
(``engine_max_retries`` etc.) decorate the chosen backend with
:class:`quantra_common.engine_client.RetryingEngineClient`; defaults
disable retries when the stub is the backend (no point retrying a
deterministic ``NotImplementedError``) and enable them when a real
gRPC backend is wired.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, model_validator

from quantra_common.settings import Environment, Settings


class OrchestratorSettings(Settings):
    """Settings for the orchestrator FastAPI service."""

    orchestrator_port: int = Field(
        default=8080,
        ge=1,
        le=65535,
        description=(
            "Port uvicorn binds when the orchestrator is launched as "
            "``python -m quantra_orchestrator``."
        ),
    )
    build_sha: str = Field(
        default="dev",
        description=(
            "Build / commit identifier echoed back by ``GET /health``. "
            "Set to the git SHA at image build time; defaults to 'dev' "
            "for local runs that have not been built into an image."
        ),
    )
    dev_auth_bypass: bool = Field(
        default=False,
        description=(
            "DEV-ONLY auth bypass (env ``DEV_AUTH_BYPASS``). When true the "
            "auth dependency short-circuits all token / API-key "
            "verification and returns a fixed development principal "
            "(uid='dev-user', email='dev@quantra.local'). Defaults OFF; "
            "must be explicitly set true to take effect. Emits a loud "
            "startup warning when enabled. NEVER set this in any "
            "environment exposed to real users."
        ),
    )
    dev_cors_origins: str = Field(
        default="",
        description=(
            "DEV-ONLY comma-separated list of browser origins allowed to "
            "call the orchestrator cross-origin (env ``DEV_CORS_ORIGINS``, "
            "e.g. ``http://localhost:5173``). Empty (the default) means NO "
            "``CORSMiddleware`` is installed — the production posture, where "
            "a reverse proxy owns CORS/TLS. When non-empty the app factory "
            "installs a ``CORSMiddleware`` allowing exactly these origins "
            "(credentials off — auth is a Bearer header, not a cookie). This "
            "is a local-browser-demo convenience so the Vite-served portal "
            "can reach ``python -m quantra_orchestrator`` directly; NEVER set "
            "it in an environment fronted by a CORS-owning proxy."
        ),
    )
    trace_capture: bool = Field(
        default=True,
        description=(
            "In-app pricing-trace capture (env ``TRACE_CAPTURE``). "
            "When true (the dev default) every instrumented pricing route "
            "records its per-stage view of the call into "
            "``app.pricing_traces`` via a best-effort post-response flush "
            "so a user can investigate a call by transaction id. Set false "
            "to disable persistence (e.g. a prod that does not want to "
            "store every payload); the read endpoint stays mounted and "
            "simply returns 404 for ids that were never captured. The "
            "flush is fire-and-forget regardless: a trace-write failure "
            "never fails or slows a price."
        ),
    )
    trace_max_payload_bytes: int = Field(
        default=65536,
        ge=256,
        description=(
            "Per-stage payload size cap (bytes) for pricing traces. "
            "A stage payload whose JSON encoding exceeds this is stored "
            "truncated with a ``__truncated__`` marker + a preview so a "
            "giant payload can't bloat a trace row. Default 64 KiB — bumped "
            "from 32 KiB because the ``engine_request`` stage now carries BOTH "
            "the orchestrator's assembled inputs AND the exact engine wire "
            "bytes (base64 + a decoded-from-bytes view incl. the full curve + "
            "rates.indices registry), which roughly doubles its size; extreme "
            "cases (very long curves / cashflow sets) still truncate gracefully."
        ),
    )
    md_client_max_retries: int = Field(
        default=3,
        ge=0,
        description=(
            "Per-call retry budget for the MD HTTP client. "
            "Forwarded into ``MdClientConfig.max_retries``; controls the "
            "number of additional attempts after the first failure on "
            "transport errors / 5xx responses."
        ),
    )
    md_client_backoff_base_s: float = Field(
        default=0.1,
        gt=0,
        description=(
            "Base delay (seconds) for exponential backoff between MD "
            "retries; forwarded into ``MdClientConfig.backoff_base_s``."
        ),
    )
    md_client_backoff_cap_s: float = Field(
        default=2.0,
        gt=0,
        description=(
            "Maximum delay (seconds) between MD retries; forwarded into "
            "``MdClientConfig.backoff_cap_s``. Caps the exponential "
            "backoff so a 5xx storm cannot stretch a single request beyond "
            "this bound per attempt."
        ),
    )
    md_cache_max_entries: int = Field(
        default=4096,
        ge=1,
        description=(
            "Max entries the orchestrator's in-process resolved-quote "
            "cache holds before evicting the least-recently-used entry "
            "Bounded LRU; per-process; not shared across replicas."
        ),
    )
    md_cache_ttl_s: float = Field(
        default=300.0,
        gt=0,
        description=(
            "Per-entry TTL (seconds) on the resolved-quote cache. After "
            "this many seconds the entry is treated as a miss and the "
            "MD service is re-queried. Default 5 minutes; "
            "the LRU bound (``md_cache_max_entries``) still applies."
        ),
    )
    engine_grpc_target: str | None = Field(
        default=None,
        description=(
            "gRPC ``host:port`` of the pricing engine. When set, the "
            "orchestrator builds a real ``grpc.aio`` channel-backed "
            "engine client; when unset, falls back to "
            "``StubEngineClient`` which raises ``NotImplementedError`` "
            "for every RPC. Mirrors how "
            "``md_service_url`` gates the MD client."
        ),
    )
    engine_timeout_s: float = Field(
        default=30.0,
        gt=0,
        description=(
            "Per-call deadline (seconds) for engine RPCs. Forwarded "
            "into ``EngineClientConfig.timeout_s`` and used by the "
            "real gRPC client to set ``timeout=`` on every unary call."
        ),
    )
    engine_max_retries: int = Field(
        default=2,
        ge=0,
        description=(
            "Per-call retry budget for the engine client. Decorates "
            "the backend with "
            ":class:`quantra_common.engine_client.RetryingEngineClient` "
            "when ``engine_retry_enabled`` resolves to true. Retries "
            "fire only on ``EngineRetryableError`` / "
            "``EngineTimeoutError`` (gRPC ``UNAVAILABLE`` / "
            "``DEADLINE_EXCEEDED``); ``InvalidArgument`` and other "
            "non-transient errors fail fast."
        ),
    )
    engine_backoff_base_s: float = Field(
        default=0.1,
        gt=0,
        description=(
            "Base delay (seconds) for exponential backoff between "
            "engine retries; forwarded into "
            "``EngineClientConfig.backoff_base_s``."
        ),
    )
    engine_backoff_cap_s: float = Field(
        default=2.0,
        gt=0,
        description=(
            "Maximum delay (seconds) between engine retries; "
            "forwarded into ``EngineClientConfig.backoff_cap_s``."
        ),
    )
    engine_retry_enabled: bool | None = Field(
        default=None,
        description=(
            "Tri-state override for the retry decorator: ``True`` "
            "forces retries on, ``False`` forces them off, ``None`` "
            "(default) lets the factory decide based on the backend "
            "— off for the stub (a deterministic "
            "``NotImplementedError`` is not worth retrying), on for "
            "the real gRPC backend."
        ),
    )
    concurrency_policy_swap_ir: str = Field(
        default="one_trade_per_call",
        description=(
            "Per-product concurrency policy id for the IR-swap pricing "
            "endpoint. Resolved against "
            ":data:`quantra_orchestrator.pricing.POLICY_REGISTRY` by the "
            "swap_ir route. A future ``GroupByCurveSet`` policy can land "
            "as a new registry entry; operators then flip this string."
        ),
    )
    concurrency_policy_swaption: str = Field(
        default="one_trade_per_call",
        description=(
            "Per-product concurrency policy id for the swaption pricing "
            "endpoint. Resolved against "
            ":data:`quantra_orchestrator.pricing.POLICY_REGISTRY` "
            "by the swaption route. A future ``GroupByVolSurface`` policy "
            "could co-locate swaptions sharing a surface (surface "
            "bootstrap is significantly heavier than curve bootstrap); "
            "when it lands as a registry entry operators flip this "
            "string without a code change."
        ),
    )
    concurrency_policy_bonds_fixed: str = Field(
        default="one_trade_per_call",
        description=(
            "Per-product concurrency policy id for the fixed-rate bond "
            "pricing endpoint. Resolved against "
            ":data:`quantra_orchestrator.pricing.POLICY_REGISTRY` "
            "by the bonds_fixed route. A future ``GroupByCurveSet`` "
            "policy could co-locate bonds + swaps sharing a "
            "discount-curve set; operators flip this string once that "
            "policy is registered."
        ),
    )
    concurrency_policy_bonds_floating: str = Field(
        default="one_trade_per_call",
        description=(
            "Per-product concurrency policy id for the floating-rate "
            "bond pricing endpoint. Resolved against "
            ":data:`quantra_orchestrator.pricing.POLICY_REGISTRY` by the "
            "bonds_floating route. Same default and same future "
            "``GroupByCurveSet`` policy as the fixed variant — floating "
            "bonds share the rates-side curve set with their fixed-rate "
            "counterparts and with swaps."
        ),
    )
    concurrency_policy_cds: str = Field(
        default="one_trade_per_call",
        description=(
            "Per-product concurrency policy id for the CDS pricing "
            "endpoint. Resolved against "
            ":data:`quantra_orchestrator.pricing.POLICY_REGISTRY` by "
            "the cds route. A future ``GroupByCreditCurve`` policy "
            "could co-locate CDS trades sharing a reference entity / "
            "credit curve; when it lands as a registry entry operators "
            "flip this string without a code change."
        ),
    )
    concurrency_policy_equity_options: str = Field(
        default="one_trade_per_call",
        description=(
            "Per-product concurrency policy id for the equity-option "
            "pricing endpoint. Resolved against "
            ":data:`quantra_orchestrator.pricing.POLICY_REGISTRY` by "
            "the equity_options route. A future ``GroupByEquitySurface`` "
            "policy could co-locate equity options sharing the same "
            "Black-vol surface; when it lands as a registry entry "
            "operators flip this string without a code change."
        ),
    )
    concurrency_policy_swaps_inflation: str = Field(
        default="one_trade_per_call",
        description=(
            "Per-product concurrency policy id for the inflation-swap "
            "pricing endpoint. Resolved against "
            ":data:`quantra_orchestrator.pricing.POLICY_REGISTRY` by "
            "the swaps_inflation route. A future "
            "``GroupByInflationCurve`` policy could co-locate inflation "
            "swaps sharing the same inflation curve / index (QuantLib's "
            "inflation-curve bootstrap is the dominant per-call cost); "
            "when it lands as a registry entry operators flip this "
            "string without a code change."
        ),
    )

    @model_validator(mode="after")
    def _forbid_dev_bypass_in_production(self) -> OrchestratorSettings:
        """Refuse to start with the auth bypass on in production.

        ``DEV_AUTH_BYPASS`` disables ALL credential checks and resolves
        every request to ``uid='dev-user'``; it exists solely for the
        local self-hosted bundle. If it ever leaked into a hosted deploy
        (``ENV=prod``) the whole API would be world-open. Rather than
        rely on operators noticing the loud startup banner, hard-fail
        construction of the settings object — which the app factory
        builds at import/boot time — so the process cannot come up at
        all. Staging/dev are unaffected; only the production signal is
        fatal.
        """

        if self.dev_auth_bypass and self.env is Environment.PROD:
            msg = (
                "DEV_AUTH_BYPASS=true is forbidden when ENV=prod: the "
                "auth bypass disables all credential checks and resolves every "
                "request to uid='dev-user'. Unset DEV_AUTH_BYPASS (or ENV) "
                "before deploying to production."
            )
            raise ValueError(msg)
        return self

    def cors_allow_origins(self) -> list[str]:
        """Parse ``dev_cors_origins`` (CSV) into a clean origin list.

        Returns an empty list when unset (the production default — no
        CORS middleware is installed). A CSV string is used rather than a
        list field to sidestep pydantic-settings' JSON-list env parsing,
        mirroring the throwaway demo launcher's ``DEMO_CORS_ORIGINS``.
        """

        return [o.strip() for o in self.dev_cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_orchestrator_settings() -> OrchestratorSettings:
    """Process-wide cached service settings instance."""

    return OrchestratorSettings()
