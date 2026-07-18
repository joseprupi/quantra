"""Shared fixtures for the orchestrator test suite."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import TextClause

from quantra_common.auth.context import ApiKeyRecord
from quantra_common.auth.firebase import FirebaseTokenVerifier
from quantra_common.auth.lookup import ApiKeyLookup
from quantra_common.settings import Environment, LogLevel
from quantra_orchestrator.app import create_app
from quantra_orchestrator.settings import OrchestratorSettings


@pytest.fixture
def orchestrator_settings() -> OrchestratorSettings:
    """Settings that bypass any real env / DSN config.

    The scaffolded app does not yet open any external connections, so a
    plain in-memory settings instance is enough. ``LOG_LEVEL=WARNING``
    keeps the test output quiet while still exercising the structlog
    pipeline.
    """

    return OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="testsha",
    )


@pytest.fixture
def fake_api_keys() -> dict[str, ApiKeyRecord]:
    """Per-test in-memory map ``raw_key -> ApiKeyRecord``.

    Tests can populate this before constructing the app; the
    ``fake_api_key_lookup`` fixture closes over it. Empty by default so
    that the "no creds / wrong creds" path is the natural baseline.
    """

    return {}


@pytest.fixture
def fake_api_key_lookup(fake_api_keys: dict[str, ApiKeyRecord]) -> ApiKeyLookup:
    """In-memory ``ApiKeyLookup`` driven by ``fake_api_keys``.

    Mirrors the production contract exactly: returns ``None`` for unknown
    keys, returns the record (which may be inactive) otherwise. We do
    NOT hash the key here — the production hashing only matters for the
    SQL adapter, not the verifier protocol.
    """

    async def _lookup(key: str) -> ApiKeyRecord | None:
        return fake_api_keys.get(key)

    return _lookup


@pytest.fixture
def fake_firebase_verifier() -> tuple[FirebaseTokenVerifier, dict[str, dict[str, Any]]]:
    """Sync stub of ``firebase_admin.auth.verify_id_token``.

    Returns ``(verifier, claims_by_token)`` so tests can add accepted
    tokens via ``claims_by_token["valid-token"] = {"uid": "u1", ...}``
    and trigger rejection by leaving a token absent.
    """

    claims_by_token: dict[str, dict[str, Any]] = {}

    def _verify(token: str) -> dict[str, Any]:
        if token not in claims_by_token:
            msg = "invalid firebase token"
            raise ValueError(msg)
        return claims_by_token[token]

    return _verify, claims_by_token


@pytest.fixture
def app(
    orchestrator_settings: OrchestratorSettings,
    fake_api_key_lookup: ApiKeyLookup,
    fake_firebase_verifier: tuple[FirebaseTokenVerifier, dict[str, dict[str, Any]]],
) -> Iterator[FastAPI]:
    """Fully-wired FastAPI app with injected auth stubs.

    Bypasses both the lifespan-managed Postgres engine (no DSN in test
    settings) and the network-touching Firebase verifier. Tests
    customise behaviour by mutating ``fake_api_keys`` /
    ``fake_firebase_verifier``'s claims map before issuing requests.
    """

    verifier, _ = fake_firebase_verifier
    instance = create_app(
        orchestrator_settings,
        api_key_lookup=fake_api_key_lookup,
        firebase_verifier=verifier,
    )
    try:
        yield instance
    finally:
        instance.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Stateless TestClient bound to the scaffolded app.

    Deliberately not used as a context manager: the lifespan handler is
    a no-op for the test settings (no DSN → no engine constructed) and
    tests that need lifespan coverage opt in explicitly with
    ``with TestClient(app) as ...``.
    """

    test_client = TestClient(app, raise_server_exceptions=False)
    try:
        yield test_client
    finally:
        test_client.close()


# ---------------------------------------------------------------------------
# Data-layer unit-test infrastructure
# ---------------------------------------------------------------------------
# These types are intentionally local to the test suite (they don't ship
# in the orchestrator package). They satisfy the surface that
# ``CrudRepository`` / ``ImmutableRepository`` call into so unit tests
# can assert which SQL ran with which parameters — most importantly the
# ``owner_uid = :owner_uid`` clause every query must carry (north_star
# invariant: "no cross-tenant reads").


@dataclass
class Recording:
    """One captured ``conn.execute(stmt, params)`` call."""

    sql: str
    params: dict[str, Any]
    mode: str  # "read" via .connect(); "write" via .begin()


SqlHandler = Callable[[str, dict[str, Any]], list[dict[str, Any]]]


class _FakeMappings:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def one(self) -> dict[str, Any]:
        if not self._rows:
            msg = "Fake result was empty; expected exactly one row."
            raise AssertionError(msg)
        return self._rows[0]

    def one_or_none(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._mappings = _FakeMappings(rows)

    def mappings(self) -> _FakeMappings:
        return self._mappings


class _FakeConnection:
    def __init__(
        self,
        recordings: list[Recording],
        handler: SqlHandler,
        mode: str,
    ) -> None:
        self._recordings = recordings
        self._handler = handler
        self._mode = mode

    async def execute(
        self,
        stmt: TextClause,
        params: dict[str, Any] | None = None,
    ) -> _FakeResult:
        sql = str(stmt)
        bound = dict(params or {})
        self._recordings.append(Recording(sql=sql, params=bound, mode=self._mode))
        rows = self._handler(sql, bound)
        return _FakeResult(rows)


def _default_handler(_sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
    return []


@dataclass
class FakeEngine:
    """``AsyncEngine`` substitute for repository unit tests.

    Construct without arguments; install per-test SQL handlers via
    ``set_handler(fn)``. After the test, inspect ``recordings`` to
    assert that every read carries ``WHERE owner_uid = :owner_uid``
    and that the bound params include the expected ``owner_uid``.
    """

    recordings: list[Recording] = field(default_factory=list)
    handler: SqlHandler = field(default=_default_handler)

    def set_handler(self, handler: SqlHandler) -> None:
        self.handler = handler

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[_FakeConnection]:
        yield _FakeConnection(self.recordings, self.handler, "read")

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[_FakeConnection]:
        yield _FakeConnection(self.recordings, self.handler, "write")

    def clear(self) -> None:
        self.recordings.clear()


@pytest.fixture
def fake_rw_engine() -> FakeEngine:
    """A fresh ``FakeEngine`` standing in for the ``app_rw`` AsyncEngine."""

    return FakeEngine()


@pytest.fixture
def fake_ro_engine() -> FakeEngine:
    """A fresh ``FakeEngine`` standing in for the ``app_ro`` AsyncEngine."""

    return FakeEngine()
