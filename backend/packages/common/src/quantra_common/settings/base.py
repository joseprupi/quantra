"""`Settings` model and cached factory.

`Settings` is read at process start. All fields are optional so the model
stays importable even in unit tests that never touch a real environment.
Services that *require* a value (e.g. the orchestrator needs
`POSTGRES_DSN_APP_RW`) call the explicit `.require_*` accessors on top of
the raw fields.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Final

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[5]


class Environment(StrEnum):
    """Deploy environment label. Read by logging/auth/etc. for behavior switches."""

    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class Settings(BaseSettings):
    """Process-wide configuration.

    Field names map 1-for-1 to environment variables (case-insensitive).
    A `.env` file at the repo root is read automatically when present.
    Unknown env vars are ignored so individual services can layer their own
    settings classes on top without colliding.
    """

    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    env: Environment = Field(
        default=Environment.DEV,
        description="Deploy environment. Drives logging format and safety checks.",
    )
    log_level: LogLevel = Field(
        default=LogLevel.INFO,
        description="Root log level for structlog/stdlib loggers.",
    )

    postgres_dsn_app_rw: str | None = Field(
        default=None,
        description="Async DSN for app.* schema, RW role. Required by orchestrator writes.",
    )
    postgres_dsn_app_ro: str | None = Field(
        default=None,
        description="Async DSN for app.* schema, RO role. Used by read-only orchestrator paths.",
    )
    postgres_dsn_md_rw: str | None = Field(
        default=None,
        description="Async DSN for md.* schema, RW role. Used by the MD ingester only.",
    )
    postgres_dsn_md_ro: str | None = Field(
        default=None,
        description="Async DSN for md.* schema, RO role. Used by the MD service.",
    )
    postgres_dsn_admin: str | None = Field(
        default=None,
        description=(
            "Async DSN for the Postgres superuser/owner used to apply schema "
            "migrations (CREATE ROLE / CREATE SCHEMA / extensions). Required "
            "by the Alembic env; never used by runtime services."
        ),
    )

    # Per-role connection pool sizing. The four role pairs each get their
    # own steady-state `pool_size` and burst `max_overflow` so an ingestion
    # spike on `md_rw` cannot exhaust the slots reserved for orchestrator
    # `app_rw` traffic. The sum of `(pool_size + max_overflow)` across all
    # roles must stay below Postgres' `max_connections` minus
    # `pg_pool_admin_headroom`.
    pg_pool_size_app_rw: int = Field(
        default=20,
        ge=1,
        description="Per-process steady pool size for the app_rw role (orchestrator writes).",
    )
    pg_pool_max_overflow_app_rw: int = Field(
        default=10,
        ge=0,
        description="Burst overflow above pg_pool_size_app_rw.",
    )
    pg_pool_size_app_ro: int = Field(
        default=10,
        ge=1,
        description="Per-process steady pool size for the app_ro role (orchestrator reads).",
    )
    pg_pool_max_overflow_app_ro: int = Field(
        default=5,
        ge=0,
        description="Burst overflow above pg_pool_size_app_ro.",
    )
    pg_pool_size_md_rw: int = Field(
        default=4,
        ge=1,
        description=(
            "Per-process steady pool size for the md_rw role. The ingester is "
            "batch-oriented; small on purpose so a runaway loop cannot starve app.*."
        ),
    )
    pg_pool_max_overflow_md_rw: int = Field(
        default=4,
        ge=0,
        description="Burst overflow above pg_pool_size_md_rw.",
    )
    pg_pool_size_md_ro: int = Field(
        default=15,
        ge=1,
        description=(
            "Per-process steady pool size for the md_ro role (MD service / orchestrator reads)."
        ),
    )
    pg_pool_max_overflow_md_ro: int = Field(
        default=10,
        ge=0,
        description="Burst overflow above pg_pool_size_md_ro.",
    )
    pg_pool_timeout_s: float = Field(
        default=10.0,
        gt=0,
        description=(
            "Seconds a checkout waits for a free connection before raising "
            "TimeoutError. Short on purpose so saturation surfaces fast as a "
            "client-side error instead of queueing forever."
        ),
    )
    pg_pool_admin_headroom: int = Field(
        default=10,
        ge=0,
        description=(
            "Connections reserved for the admin/migration role + replication + "
            "operator psql sessions. Subtracted from Postgres' max_connections "
            "before per-role caps are sized."
        ),
    )

    firebase_project_id: str | None = Field(
        default=None,
        description="Firebase project ID. Required by auth/ for token verification.",
    )

    md_service_url: str | None = Field(
        default=None,
        description="Base URL of the internal market-data service (e.g. http://md:8082).",
    )
    md_service_timeout_s: float = Field(
        default=10.0,
        gt=0,
        description="Per-request timeout for the MD HTTP client.",
    )

    engine_grpc_addr: str | None = Field(
        default=None,
        description=(
            "host:port of the pricing engine gRPC service. Consumed only by "
            "EngineClientConfig.from_settings; the orchestrator uses its own "
            "ENGINE_GRPC_TARGET setting instead."
        ),
    )
    engine_grpc_timeout_s: float = Field(
        default=30.0,
        gt=0,
        description=(
            "Per-call timeout for the pricing engine gRPC client. Consumed only "
            "by EngineClientConfig.from_settings; the orchestrator has its own "
            "engine timeout setting."
        ),
    )

    request_id_header: str = Field(
        default="X-Request-Id",
        description="Inbound/outbound HTTP header used to thread a request ID through logs.",
    )

    def require_postgres_dsn_app_rw(self) -> str:
        return _require(self.postgres_dsn_app_rw, "POSTGRES_DSN_APP_RW")

    def require_postgres_dsn_app_ro(self) -> str:
        return _require(self.postgres_dsn_app_ro, "POSTGRES_DSN_APP_RO")

    def require_postgres_dsn_md_rw(self) -> str:
        return _require(self.postgres_dsn_md_rw, "POSTGRES_DSN_MD_RW")

    def require_postgres_dsn_md_ro(self) -> str:
        return _require(self.postgres_dsn_md_ro, "POSTGRES_DSN_MD_RO")

    def require_postgres_dsn_admin(self) -> str:
        return _require(self.postgres_dsn_admin, "POSTGRES_DSN_ADMIN")

    def require_md_service_url(self) -> str:
        return _require(self.md_service_url, "MD_SERVICE_URL")

    def require_engine_grpc_addr(self) -> str:
        return _require(self.engine_grpc_addr, "ENGINE_GRPC_ADDR")


def _require(value: str | None, name: str) -> str:
    if not value:
        msg = f"Required setting '{name}' is unset"
        raise RuntimeError(msg)
    return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide cached `Settings` instance.

    Use this in production code instead of constructing `Settings()` directly
    so every caller observes the same values for the lifetime of the process.
    Tests that need to override env vars should call `get_settings.cache_clear()`
    after mutating `os.environ`.
    """

    return Settings()
