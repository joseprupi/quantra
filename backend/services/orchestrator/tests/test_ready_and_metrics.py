"""Hermetic tests for ``GET /ready`` and ``GET /metrics``.

Covers:

* ``/ready`` returns 200 with every check ``ok`` when MD + engine + DBs
  pass their probes, and 503 with a per-check ``fail`` when any
  dependency trips.
* ``/health`` semantics are unchanged when ``/ready`` is configured —
  the two probes are independent contracts.
* ``/metrics`` renders Prometheus-format exposition (parsed via
  ``prometheus_client.parser``) and exposes every metric family the
  the readiness design enumerates.
* The MD upstream failure counter increments when the MD client
  raises; the per-product pricing-route middleware records latency +
  outcome.

Filter rationale
----------------

The module is opted out of ``PytestUnraisableExceptionWarning``. The
larger orchestrator test suite has pre-existing async-resource leaks
(httpx + asyncio loops from skipped/parametrised tests) that pytest's
unraisable-exception collector surfaces against whichever test is
running when it fires. Without the filter the assertion-clean tests
in this module would be charged for leaks that belong elsewhere; the
filter scopes the suppression narrowly so the strict-warning policy
stays in place across the rest of the project.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Iterator
from datetime import date, datetime
from http import HTTPStatus
from typing import Any

import anyio
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from quantra_common.auth.firebase import FirebaseTokenVerifier
from quantra_common.auth.lookup import ApiKeyLookup
from quantra_common.engine_client import (
    EngineClient,
    EngineClientConfig,
    StubEngineClient,
)
from quantra_common.engine_client.errors import EngineRpcError
from quantra_common.engine_client.rpcs import EngineRpc
from quantra_common.md_client import (
    MdClient,
    MdClientConfig,
    MdTransportError,
)
from quantra_common.settings import Environment, LogLevel
from quantra_common.types import ResolvedQuote
from quantra_orchestrator.app import create_app
from quantra_orchestrator.observability import (
    InstrumentedMdClient,
    OrchestratorMetrics,
)
from quantra_orchestrator.settings import OrchestratorSettings

pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")

# ---------------------------------------------------------------------------
# Fakes — minimal MD / Engine clients we can control per test
# ---------------------------------------------------------------------------


class _FakeMdClient(MdClient):
    """In-process ``MdClient`` whose HTTP transport always fails (or never runs).

    Constructed with a real :class:`httpx.MockTransport` so the inner
    ``MdClient`` plumbing (cache lookup, request formatter) runs
    end-to-end; tests inject the transport response per-test by
    swapping the handler.
    """

    def __init__(self, handler: Any) -> None:
        config = MdClientConfig(base_url="http://md-mock", timeout_s=1.0, max_retries=0)
        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport, base_url="http://md-mock")
        super().__init__(config, client=client)


def _md_handler_ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "items": [
                {
                    "canonical_id": "TEST.CID.1",
                    "as_of": "2026-01-15T00:00:00",
                    "resolved_as_of": "2026-01-15T00:00:00",
                    "value": 4.25,
                    "source": "test",
                    "found": True,
                }
            ]
        },
    )


def _md_handler_fail(_request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("md service unreachable", request=_request)


class _OkEngineClient(EngineClient):
    """Engine client whose ``call`` immediately returns a placeholder body.

    Used by /ready hermetic tests so the engine probe reports ``ok``
    without spinning up a real gRPC channel.
    """

    def __init__(self) -> None:
        self._config = EngineClientConfig(address="fake")

    async def call(self, rpc: EngineRpc, request_bytes: bytes) -> bytes:
        del rpc, request_bytes
        return b"ok"


class _RpcErrorEngineClient(EngineClient):
    """Engine client whose ``call`` raises a configurable ``EngineRpcError``."""

    def __init__(self, code: str) -> None:
        self._config = EngineClientConfig(address="fake")
        self._code = code

    async def call(self, rpc: EngineRpc, request_bytes: bytes) -> bytes:
        del rpc, request_bytes
        raise EngineRpcError("engine sent rpc error", code=self._code)


# ---------------------------------------------------------------------------
# Settings + fixture wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def ready_settings() -> OrchestratorSettings:
    return OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="testsha",
        md_service_url="http://md-mock",
    )


@pytest.fixture
def ok_md_app(
    ready_settings: OrchestratorSettings,
    fake_api_key_lookup: ApiKeyLookup,
    fake_firebase_verifier: tuple[FirebaseTokenVerifier, dict[str, dict[str, Any]]],
) -> Iterator[FastAPI]:
    """App where ``/ready`` will see md=ok, engine=ok, db=skipped."""

    verifier, _ = fake_firebase_verifier

    def _make_app() -> FastAPI:
        return create_app(
            ready_settings,
            api_key_lookup=fake_api_key_lookup,
            firebase_verifier=verifier,
            md_client=_FakeMdClient(_md_handler_ok),
            engine_client=_OkEngineClient(),
        )

    app = _make_app()
    try:
        yield app
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def failing_md_app(
    ready_settings: OrchestratorSettings,
    fake_api_key_lookup: ApiKeyLookup,
    fake_firebase_verifier: tuple[FirebaseTokenVerifier, dict[str, dict[str, Any]]],
) -> Iterator[FastAPI]:
    """App whose MD transport refuses the ``/health`` probe."""

    verifier, _ = fake_firebase_verifier
    # MD is configured but unreachable — /ready's MD probe hits the
    # live URL which 127.0.0.1 likely refuses; that's the failure shape
    # the test wants. We point at a deliberately-unreachable
    # discard-only loopback port.
    settings = OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="testsha",
        # Address-of-last-resort: TCP/1 is reserved + closed; the OS
        # rejects the connection synchronously so the probe lands in
        # the transport-error branch without hitting the timeout.
        md_service_url="http://127.0.0.1:1",
    )
    app = create_app(
        settings,
        api_key_lookup=fake_api_key_lookup,
        firebase_verifier=verifier,
        md_client=_FakeMdClient(_md_handler_fail),
        engine_client=_OkEngineClient(),
    )
    try:
        yield app
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def failing_engine_app(
    ready_settings: OrchestratorSettings,
    fake_api_key_lookup: ApiKeyLookup,
    fake_firebase_verifier: tuple[FirebaseTokenVerifier, dict[str, dict[str, Any]]],
) -> Iterator[FastAPI]:
    """App whose engine client falls back to the stub (NotImplementedError)."""

    verifier, _ = fake_firebase_verifier
    app = create_app(
        ready_settings,
        api_key_lookup=fake_api_key_lookup,
        firebase_verifier=verifier,
        md_client=_FakeMdClient(_md_handler_ok),
        engine_client=StubEngineClient(EngineClientConfig(address="stub")),
    )
    try:
        yield app
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# /health stays unchanged
# ---------------------------------------------------------------------------


def test_health_stays_a_pure_liveness_probe(client: TestClient) -> None:
    """``/health`` ignores dependencies — same shape as before."""

    response = client.get("/health")
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body == {
        "status": "ok",
        "service": "quantra-orchestrator",
        "build_sha": "testsha",
        "env": "dev",
    }


# ---------------------------------------------------------------------------
# /ready
# ---------------------------------------------------------------------------


_OK_OR_SKIPPED: frozenset[str] = frozenset({"ok", "skipped"})


def test_ready_all_green_when_md_engine_ok(ok_md_app: FastAPI) -> None:
    """When every probe passes (or is unconfigured) ``/ready`` is 200."""

    # ``md_service_url`` is wiped so the MD probe ``skip``s in place;
    # leaves engine + DBs to report their actual state. On this dev
    # box the DB may be reachable (DSNs come from ``.env``) — assert
    # that every check resolves into one of the OK states, not a
    # specific tuple.
    ok_md_app.state.orchestrator_settings = OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="testsha",
        md_service_url=None,
    )
    with TestClient(ok_md_app) as client:
        response = client.get("/ready")
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["status"] == "ready"
    checks = body["checks"]
    assert set(checks) == {"md", "engine", "db_app", "db_md"}
    assert checks["md"]["status"] == "skipped"
    assert checks["engine"]["status"] == "ok"
    assert checks["db_app"]["status"] in _OK_OR_SKIPPED
    assert checks["db_md"]["status"] in _OK_OR_SKIPPED


def test_ready_503_when_md_unreachable(failing_md_app: FastAPI) -> None:
    """503 + per-check breakdown when the MD ``/health`` probe fails."""

    with TestClient(failing_md_app) as client:
        response = client.get("/ready")
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    body = response.json()
    assert body["status"] == "not_ready"
    md_check = body["checks"]["md"]
    assert md_check["status"] == "fail"
    # Must surface *which* dependency failed — discipline.
    assert md_check.get("detail") is not None
    # The other checks should still report their own state.
    assert body["checks"]["engine"]["status"] == "ok"


def test_ready_503_when_engine_stub_not_wired(failing_engine_app: FastAPI) -> None:
    """503 when only the StubEngineClient is wired (no real engine)."""

    failing_engine_app.state.orchestrator_settings = OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="testsha",
        md_service_url=None,
    )
    with TestClient(failing_engine_app) as client:
        response = client.get("/ready")
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    body = response.json()
    engine_check = body["checks"]["engine"]
    assert engine_check["status"] == "fail"
    assert engine_check["detail"] == "stub_engine_not_wired"


def test_ready_treats_invalid_argument_as_engine_reachable() -> None:
    """Engine returning INVALID_ARGUMENT means the wire is alive ⇒ ready."""

    settings = OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="testsha",
        md_service_url=None,
    )
    app = create_app(
        settings,
        engine_client=_RpcErrorEngineClient(code="INVALID_ARGUMENT"),
    )
    with TestClient(app) as client:
        response = client.get("/ready")
    assert response.status_code == HTTPStatus.OK
    assert response.json()["checks"]["engine"]["status"] == "ok"


# ---------------------------------------------------------------------------
# /metrics
# ---------------------------------------------------------------------------


def test_metrics_parses_as_prometheus_exposition(client: TestClient) -> None:
    """``/metrics`` body parses cleanly via ``prometheus_client.parser``."""

    response = client.get("/metrics")
    assert response.status_code == HTTPStatus.OK
    assert "text/plain" in response.headers.get("content-type", "")
    families = list(text_string_to_metric_families(response.text))
    names = {family.name for family in families}
    # Every metric family required must appear in
    # exposition (even when their counter is 0). The Counter family
    # name does NOT carry the ``_total`` suffix in the parser output.
    expected = {
        "orchestrator_md_requests",
        "orchestrator_md_request_seconds",
        "orchestrator_quote_cache_hits",
        "orchestrator_quote_cache_misses",
        "orchestrator_quote_cache_expirations",
        "orchestrator_quote_cache_size",
        "orchestrator_pricing_requests",
        "orchestrator_pricing_request_seconds",
        "orchestrator_engine_requests",
        "orchestrator_engine_request_seconds",
        "orchestrator_pinned_snapshot_age_seconds",
    }
    missing = expected - names
    assert not missing, f"/metrics missing expected families: {missing}"


def test_metrics_md_failure_counter_increments_on_md_error(
    ready_settings: OrchestratorSettings,
    fake_api_key_lookup: ApiKeyLookup,
    fake_firebase_verifier: tuple[FirebaseTokenVerifier, dict[str, dict[str, Any]]],
) -> None:
    """A ``MdTransportError`` on ``resolve_quotes`` lands in the failure label."""

    verifier, _ = fake_firebase_verifier
    failing_md = _FakeMdClient(_md_handler_fail)
    app = create_app(
        ready_settings,
        api_key_lookup=fake_api_key_lookup,
        firebase_verifier=verifier,
        md_client=failing_md,
        engine_client=_OkEngineClient(),
    )
    # The InstrumentedMdClient wrapper is installed by the lifespan,
    # but the test injects an MdClient via the factory seam — wrap
    # manually so the metrics adapter is in the path.
    metrics: OrchestratorMetrics = app.state.metrics
    app.state.md_client = InstrumentedMdClient(failing_md, metrics)

    async def _drive_one_call() -> None:
        with contextlib.suppress(MdTransportError):
            await app.state.md_client.resolve_quotes(
                ["TEST.CID.1"],
                date(2026, 1, 15),
            )

    anyio.run(_drive_one_call)

    with TestClient(app) as test_client:
        response = test_client.get("/metrics")
    body = response.text
    # The counter family is ``orchestrator_md_requests`` (the parser
    # drops the ``_total`` suffix); failure samples carry the
    # ``outcome="MdTransportError"`` label per the wrapper's mapping.
    failure_line = (
        'orchestrator_md_requests_total{operation="resolve_quotes",outcome="MdTransportError"} 1.0'
    )
    assert failure_line in body


def test_pricing_middleware_records_request_outcome(client: TestClient) -> None:
    """Hitting an unknown ``/v1/price/...`` path still increments the route counter."""

    # We don't have a real engine wired so any real pricing route would
    # 5xx; that's still a valid recorded outcome.
    response = client.post(
        "/v1/price/swap/ir",
        headers={"X-API-Key": "anything"},
        json={},
    )
    # Without API key it returns 401; that's still routed through the
    # middleware and counted.
    assert response.status_code in {401, 422, 503}
    metrics_response = client.get("/metrics")
    body = metrics_response.text
    assert "orchestrator_pricing_requests_total{outcome=" in body
    assert "/v1/price/swap/ir" in body


# ---------------------------------------------------------------------------
# DB-pool probe — exercise the failing-pool branch via a bogus engine
# ---------------------------------------------------------------------------


class _BadAsyncEngine:
    """Stand-in for ``AsyncEngine`` whose ``connect()`` always raises.

    Avoids the open-socket leak of a real bogus-DSN asyncpg engine
    (which trips ``PytestUnraisableExceptionWarning`` under
    ``asyncio_mode=auto``). The /ready probe takes ``AsyncEngine | None``
    and only calls ``connect`` + ``execute`` on it; a duck-type
    stand-in is enough.
    """

    @contextlib.asynccontextmanager
    async def connect(self) -> AsyncIterator[Any]:
        raise ConnectionRefusedError("test: app_ro pool unreachable")
        yield  # pragma: no cover — required to make this a generator


def test_ready_503_when_db_app_unreachable(
    ready_settings: OrchestratorSettings,
    fake_api_key_lookup: ApiKeyLookup,
    fake_firebase_verifier: tuple[FirebaseTokenVerifier, dict[str, dict[str, Any]]],
) -> None:
    """A failing ``app_ro`` pool flips db_app to ``fail`` in the response."""

    verifier, _ = fake_firebase_verifier
    settings = OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="testsha",
        md_service_url=None,
    )
    app = create_app(
        settings,
        api_key_lookup=fake_api_key_lookup,
        firebase_verifier=verifier,
        engine_client=_OkEngineClient(),
        app_ro_engine=_BadAsyncEngine(),  # type: ignore[arg-type]
    )
    with TestClient(app) as test_client:
        response = test_client.get("/ready")
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    body = response.json()
    db_check = body["checks"]["db_app"]
    assert db_check["status"] == "fail"
    # surface *which* dependency tripped, not a generic 503.
    assert db_check["detail"] is not None
    assert "ConnectionRefused" in db_check["detail"] or "timeout" in db_check["detail"]


# ---------------------------------------------------------------------------
# Smoke: ResolvedQuote validates so the fake MD plumbing is exercised
# (catches schema drift between the fake handler and the real wire shape).
# ---------------------------------------------------------------------------


def test_resolved_quote_fake_payload_matches_wire_schema() -> None:
    """Belt-and-braces sanity check on the test fixture's shape."""

    payload = {
        "canonical_id": "TEST.CID.1",
        "as_of": datetime(2026, 1, 15).isoformat(),
        "resolved_as_of": datetime(2026, 1, 15).isoformat(),
        "value": 4.25,
        "source": "test",
        "found": True,
    }
    parsed = ResolvedQuote.model_validate(payload)
    assert parsed.canonical_id == "TEST.CID.1"
