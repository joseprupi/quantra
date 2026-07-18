"""Tests for `quantra_common.settings`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from quantra_common.settings import Settings, get_settings
from quantra_common.settings.base import Environment, LogLevel


def _make(**overrides: Any) -> Settings:
    """Build a Settings instance that ignores any on-disk `.env`.

    `_env_file` is a runtime keyword on `pydantic_settings.BaseSettings.__init__`
    that mypy can't see (no plugin / dynamic kwargs), hence the local ignore.
    """

    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


_ENV_VARS = (
    "ENV",
    "LOG_LEVEL",
    "POSTGRES_DSN_APP_RW",
    "POSTGRES_DSN_APP_RO",
    "POSTGRES_DSN_MD_RW",
    "POSTGRES_DSN_MD_RO",
    "POSTGRES_DSN_ADMIN",
    "PG_POOL_SIZE_APP_RW",
    "PG_POOL_SIZE_APP_RO",
    "PG_POOL_SIZE_MD_RW",
    "PG_POOL_SIZE_MD_RO",
    "PG_POOL_MAX_OVERFLOW_APP_RW",
    "PG_POOL_MAX_OVERFLOW_APP_RO",
    "PG_POOL_MAX_OVERFLOW_MD_RW",
    "PG_POOL_MAX_OVERFLOW_MD_RO",
    "PG_POOL_TIMEOUT_S",
    "PG_POOL_ADMIN_HEADROOM",
    "FIREBASE_PROJECT_ID",
    "MD_SERVICE_URL",
    "MD_SERVICE_TIMEOUT_S",
    "ENGINE_GRPC_ADDR",
    "ENGINE_GRPC_TIMEOUT_S",
    "REQUEST_ID_HEADER",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_defaults_when_nothing_in_env(
    clean_env: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clean_env.chdir(tmp_path)

    s = _make()

    assert s.env is Environment.DEV
    assert s.log_level is LogLevel.INFO
    assert s.postgres_dsn_app_rw is None
    assert s.md_service_timeout_s == 10.0
    assert s.request_id_header == "X-Request-Id"


def test_reads_environment(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("ENV", "prod")
    clean_env.setenv("LOG_LEVEL", "DEBUG")
    clean_env.setenv("POSTGRES_DSN_APP_RW", "postgresql+asyncpg://u:p@host/db")
    clean_env.setenv("PG_POOL_SIZE_APP_RW", "25")
    clean_env.setenv("PG_POOL_SIZE_MD_RW", "6")
    clean_env.setenv("PG_POOL_ADMIN_HEADROOM", "12")

    s = _make()

    assert s.env is Environment.PROD
    assert s.log_level is LogLevel.DEBUG
    assert s.postgres_dsn_app_rw == "postgresql+asyncpg://u:p@host/db"
    assert s.pg_pool_size_app_rw == 25
    assert s.pg_pool_size_md_rw == 6
    assert s.pg_pool_admin_headroom == 12


def test_pool_size_defaults_match_documented_math(clean_env: pytest.MonkeyPatch) -> None:
    """Defaults must agree with the math in the README's "Connection pool" section.

    Sum of (pool_size + max_overflow) across all four roles + admin headroom
    must fit under Postgres' default ``max_connections = 100`` so a fresh
    ``docker compose up postgres`` Just Works without raising the cluster
    cap. If you bump a default here, update the README math and the
    `0008_role_connection_limits.py` / `0006_role_connection_limits.py`
    migration values together.
    """

    s = _make()

    pairs = (
        (s.pg_pool_size_app_rw, s.pg_pool_max_overflow_app_rw),
        (s.pg_pool_size_app_ro, s.pg_pool_max_overflow_app_ro),
        (s.pg_pool_size_md_rw, s.pg_pool_max_overflow_md_rw),
        (s.pg_pool_size_md_ro, s.pg_pool_max_overflow_md_ro),
    )
    per_role_max = [size + overflow for size, overflow in pairs]
    assert per_role_max == [30, 15, 8, 25]
    assert sum(per_role_max) + s.pg_pool_admin_headroom <= 100


def test_require_raises_when_unset(clean_env: pytest.MonkeyPatch) -> None:
    s = _make()
    with pytest.raises(RuntimeError, match="POSTGRES_DSN_APP_RW"):
        s.require_postgres_dsn_app_rw()


def test_require_admin_dsn(clean_env: pytest.MonkeyPatch) -> None:
    s = _make()
    with pytest.raises(RuntimeError, match="POSTGRES_DSN_ADMIN"):
        s.require_postgres_dsn_admin()

    clean_env.setenv("POSTGRES_DSN_ADMIN", "postgresql+asyncpg://su:su@localhost/quantra")
    assert _make().require_postgres_dsn_admin() == "postgresql+asyncpg://su:su@localhost/quantra"


def test_require_returns_value_when_set(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("MD_SERVICE_URL", "http://md:8082")
    s = _make()
    assert s.require_md_service_url() == "http://md:8082"


def test_get_settings_is_cached(clean_env: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    clean_env.setenv("LOG_LEVEL", "WARNING")
    a = get_settings()
    clean_env.setenv("LOG_LEVEL", "ERROR")
    b = get_settings()
    assert a is b
    assert a.log_level is LogLevel.WARNING
    get_settings.cache_clear()
