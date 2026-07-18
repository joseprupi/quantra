"""Unit tests for the orchestrator's engine-client wiring.

Exercises everything the plan calls out as in-scope without booting a
real gRPC engine:

* Stub backend (the default ship): every entry in
:class:`EngineRpc` surfaces as a 502 ``engine_unavailable``
  envelope on ``/debug/engine/ping``. The stub raises
  ``NotImplementedError`` on every call; the mapper translates it.
* Fake-client error mapping: a per-test ``EngineClient`` injected via
  the ``app.state.engine_client`` seam exercises every structured mapping
  (``engine_unreachable`` / ``engine_timeout`` /
  ``engine_invalid_request`` / ``engine_upstream_error`` /
  ``engine_client_unavailable``).
* Retry decorator: retries on ``EngineRetryableError`` /
  ``EngineTimeoutError`` (gRPC ``UNAVAILABLE`` / ``DEADLINE_EXCEEDED``);
  does not retry on ``EngineRpcError`` with a non-transient code
  (``INVALID_ARGUMENT``).
* gRPC backend factory wires the request-id interceptor and exposes
  the channel on the singleton client (a later phase's RPC stubs will
  consume it).
* The ``/debug/engine/info`` endpoint returns the backend class name
  and the retry knobs in effect.

The tests deliberately avoid spinning up any real gRPC channel: the
retry tests use a hand-rolled fake :class:`EngineClient`; the
``/debug/engine/ping`` tests with the stub backend assert the same
``engine_unavailable`` envelope the gRPC backend would emit until the
real RPC stubs land (per the plan's "raise ``NotImplementedError`` if
the channel is set but no RPC stub is wired" contract).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from http import HTTPStatus
from typing import Any, cast

import grpc
import grpc.aio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from quantra_common.auth.context import ApiKeyRecord
from quantra_common.auth.lookup import ApiKeyLookup
from quantra_common.engine_client import (
    EngineClient,
    EngineClientConfig,
    EngineRetryableError,
    EngineRpc,
    EngineRpcError,
    EngineTimeoutError,
    RetryingEngineClient,
    StubEngineClient,
)
from quantra_common.settings import Environment, LogLevel
from quantra_orchestrator.app import create_app
from quantra_orchestrator.engine import (
    ENGINE_CLIENT_UNAVAILABLE_CODE,
    ENGINE_INVALID_REQUEST_CODE,
    ENGINE_TIMEOUT_CODE,
    ENGINE_UNAVAILABLE_CODE,
    ENGINE_UNREACHABLE_CODE,
    ENGINE_UPSTREAM_ERROR_CODE,
    MISSING_REQUIRED_FIXING_CODE,
    PRICING_AS_OF_BEFORE_CURVE_DATE_CODE,
    EngineClientUnavailableError,
    EngineDateCoherenceError,
    GrpcEngineClient,
    RequestIdInterceptor,
    build_engine_client,
    build_grpc_engine_client,
    describe_engine_client,
    map_engine_client_error,
    parse_missing_fixing,
)
from quantra_orchestrator.engine.grpc_backend import (
    ENGINE_RPC_METHOD_PATHS,
    grpc_status_code_name,
    translate_aio_rpc_error,
)
from quantra_orchestrator.settings import OrchestratorSettings

# --------------------------------------------------------------------- helpers


def _api_key_headers() -> dict[str, str]:
    return {"X-API-Key": "good-key"}


def _seeded_api_keys() -> dict[str, ApiKeyRecord]:
    return {
        "good-key": ApiKeyRecord(
            api_key_id="engine-test-key",
            owner_uid="engine-test-uid",
            name="Engine Test Key",
            email="engine@example.com",
            tier="free",
            active=True,
        )
    }


def _state_attr(test_client: TestClient, name: str) -> Any:
    """Read an attribute off ``TestClient.app.state`` with mypy quiet."""

    app: FastAPI = cast(FastAPI, test_client.app)
    return getattr(app.state, name, None)


class _FakeEngineClient(EngineClient):
    """Hand-rolled :class:`EngineClient` for error-mapping tests.

    Captures every ``call(rpc, request_bytes)`` invocation and returns
    or raises whatever the test installed via ``set_response`` /
    ``set_exception``. Lets the route-level tests force each
    :class:`EngineClientError` subclass without spinning up a real
    gRPC channel.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[EngineRpc, bytes]] = []
        self._response: bytes | None = None
        self._exception: BaseException | None = None
        self._closed = False

    def set_response(self, response: bytes) -> None:
        self._response = response
        self._exception = None

    def set_exception(self, exc: BaseException) -> None:
        self._exception = exc
        self._response = None

    async def call(self, rpc: EngineRpc, request_bytes: bytes) -> bytes:
        self.calls.append((rpc, request_bytes))
        if self._exception is not None:
            raise self._exception
        if self._response is None:
            msg = "FakeEngineClient: no response/exception configured"
            raise AssertionError(msg)
        return self._response

    async def close(self) -> None:
        self._closed = True


# ----------------------------------------------------------------------- fixtures


@pytest.fixture
def engine_app_factory(
    orchestrator_settings: OrchestratorSettings,
    fake_api_key_lookup: ApiKeyLookup,
    fake_firebase_verifier: tuple[Any, dict[str, dict[str, Any]]],
    fake_api_keys: dict[str, ApiKeyRecord],
) -> Callable[[EngineClient | None], FastAPI]:
    """Build the orchestrator app with a swappable ``EngineClient`` injected.

    Passing ``None`` triggers the stub-backend default the lifespan
    constructs (the real production code path). Passing a fake lets
    each test force a deterministic failure / success.
    """

    fake_api_keys.update(_seeded_api_keys())
    verifier, _ = fake_firebase_verifier

    def _build(engine_client: EngineClient | None) -> FastAPI:
        return create_app(
            orchestrator_settings,
            api_key_lookup=fake_api_key_lookup,
            firebase_verifier=verifier,
            engine_client=engine_client,
        )

    return _build


@pytest.fixture
def stub_app(
    engine_app_factory: Callable[[EngineClient | None], FastAPI],
) -> Iterator[TestClient]:
    """App where the lifespan constructs the default ``StubEngineClient``.

    Uses ``with TestClient(...)`` so the lifespan actually runs (the
    factory passes ``engine_client=None`` and the lifespan provisions
    a stub from settings). The lifespan is opt-in for tests in this
    suite — see the conftest's ``client`` fixture comment.
    """

    app = engine_app_factory(None)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def fake_engine() -> _FakeEngineClient:
    return _FakeEngineClient()


@pytest.fixture
def fake_engine_app(
    engine_app_factory: Callable[[EngineClient | None], FastAPI],
    fake_engine: _FakeEngineClient,
) -> Iterator[tuple[TestClient, _FakeEngineClient]]:
    app = engine_app_factory(fake_engine)
    test_client = TestClient(app, raise_server_exceptions=False)
    try:
        yield test_client, fake_engine
    finally:
        test_client.close()


# ------------------------------------------------------------------------- tests
# --- /debug/engine/ping with the default stub backend ----------------------


def test_ping_endpoint_requires_auth(
    engine_app_factory: Callable[[EngineClient | None], FastAPI],
) -> None:
    """No credential → 401 with ``code="unauthenticated"`` envelope."""

    app = engine_app_factory(None)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/debug/engine/ping")
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json()["code"] == "unauthenticated"


@pytest.mark.parametrize(
    "rpc",
    list(EngineRpc),
    ids=lambda rpc: rpc.value,
)
def test_ping_against_stub_emits_engine_unavailable_for_every_rpc(
    stub_app: TestClient,
    rpc: EngineRpc,
) -> None:
    """Every entry in :class:`EngineRpc` returns the same envelope.

    ``StubEngineClient.call`` raises ``NotImplementedError`` regardless
    of which RPC was passed in. The mapper turns that into a
    deterministic 502 + ``engine_unavailable`` so any pricing route
    that hits the stub fails predictably until the real backend is
    wired (a later phase).
    """

    response = stub_app.get(
        "/debug/engine/ping",
        params={"rpc": rpc.value},
        headers={**_api_key_headers(), "X-Request-Id": f"rid-{rpc.value}"},
    )

    assert response.status_code == HTTPStatus.BAD_GATEWAY, response.text
    body = response.json()
    assert body["code"] == ENGINE_UNAVAILABLE_CODE
    assert body["request_id"] == f"rid-{rpc.value}"


def test_ping_default_rpc_is_calendar_business_days(stub_app: TestClient) -> None:
    """Omitting ``?rpc`` defaults to a low-cost calendar RPC.

    Exercises the same stub error path; we only assert the RPC the
    error envelope mentions implicitly via the mapper (the route
    doesn't echo the RPC into the envelope, so we instead assert the
    contract via the OpenAPI schema below).
    """

    response = stub_app.get("/debug/engine/ping", headers=_api_key_headers())
    assert response.status_code == HTTPStatus.BAD_GATEWAY
    assert response.json()["code"] == ENGINE_UNAVAILABLE_CODE


def test_ping_rejects_unknown_rpc(stub_app: TestClient) -> None:
    """A bogus ``?rpc=`` value → 422 ``validation_error`` (FastAPI level)."""

    response = stub_app.get(
        "/debug/engine/ping",
        params={"rpc": "DefinitelyNotARealRpc"},
        headers=_api_key_headers(),
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "validation_error"


# --- /debug/engine/ping with a fake EngineClient -----------------------------


def test_ping_with_engine_unreachable_returns_502(
    fake_engine_app: tuple[TestClient, _FakeEngineClient],
) -> None:
    """Transport-level failure (``ConnectionError``) → ``engine_unreachable``."""

    client, engine = fake_engine_app
    engine.set_exception(ConnectionError("dns lookup failed"))
    response = client.get(
        "/debug/engine/ping",
        headers={**_api_key_headers(), "X-Request-Id": "rid-unr"},
    )

    assert response.status_code == HTTPStatus.BAD_GATEWAY
    body = response.json()
    assert body["code"] == ENGINE_UNREACHABLE_CODE
    assert body["request_id"] == "rid-unr"


def test_ping_with_engine_timeout_returns_504(
    fake_engine_app: tuple[TestClient, _FakeEngineClient],
) -> None:
    """``EngineTimeoutError`` → 504 ``engine_timeout``."""

    client, engine = fake_engine_app
    engine.set_exception(EngineTimeoutError("deadline 30s exceeded"))
    response = client.get(
        "/debug/engine/ping",
        headers={**_api_key_headers(), "X-Request-Id": "rid-deadline"},
    )

    assert response.status_code == HTTPStatus.GATEWAY_TIMEOUT
    body = response.json()
    assert body["code"] == ENGINE_TIMEOUT_CODE
    assert body["request_id"] == "rid-deadline"


def test_ping_with_engine_retryable_returns_504(
    fake_engine_app: tuple[TestClient, _FakeEngineClient],
) -> None:
    """``EngineRetryableError`` (gRPC ``UNAVAILABLE``) → 504 ``engine_timeout``.

    The retry decorator is off in this test (the lifespan injected a
    fake client unwrapped), so the error reaches the route directly
    and the mapper converts it to the post-retry-budget surface.
    """

    client, engine = fake_engine_app
    engine.set_exception(EngineRetryableError("UNAVAILABLE: backend unreachable"))
    response = client.get("/debug/engine/ping", headers=_api_key_headers())

    assert response.status_code == HTTPStatus.GATEWAY_TIMEOUT
    assert response.json()["code"] == ENGINE_TIMEOUT_CODE


def test_ping_with_invalid_argument_returns_400(
    fake_engine_app: tuple[TestClient, _FakeEngineClient],
) -> None:
    """gRPC ``INVALID_ARGUMENT`` → 400 ``engine_invalid_request``."""

    client, engine = fake_engine_app
    engine.set_exception(
        EngineRpcError(
            "engine RPC failed: INVALID_ARGUMENT (unknown calendar)",
            code="INVALID_ARGUMENT",
            details="unknown calendar",
        )
    )
    response = client.get("/debug/engine/ping", headers=_api_key_headers())

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["code"] == ENGINE_INVALID_REQUEST_CODE


def test_ping_with_internal_returns_502(
    fake_engine_app: tuple[TestClient, _FakeEngineClient],
) -> None:
    """gRPC ``INTERNAL`` → 502 ``engine_upstream_error``."""

    client, engine = fake_engine_app
    engine.set_exception(
        EngineRpcError(
            "engine RPC failed: INTERNAL (segfault)",
            code="INTERNAL",
            details="segfault",
        )
    )
    response = client.get("/debug/engine/ping", headers=_api_key_headers())

    assert response.status_code == HTTPStatus.BAD_GATEWAY
    assert response.json()["code"] == ENGINE_UPSTREAM_ERROR_CODE


def test_ping_with_unknown_engine_rpc_error_returns_502(
    fake_engine_app: tuple[TestClient, _FakeEngineClient],
) -> None:
    """``EngineRpcError`` with a code we don't know → ``engine_upstream_error``."""

    client, engine = fake_engine_app
    engine.set_exception(EngineRpcError("???", code="UNAUTHENTICATED"))
    response = client.get("/debug/engine/ping", headers=_api_key_headers())

    assert response.status_code == HTTPStatus.BAD_GATEWAY
    assert response.json()["code"] == ENGINE_UPSTREAM_ERROR_CODE


def test_ping_succeeds_with_responsive_engine(
    fake_engine_app: tuple[TestClient, _FakeEngineClient],
) -> None:
    """When the fake engine answers, the route returns the byte length."""

    client, engine = fake_engine_app
    engine.set_response(b"\x01\x02\x03\x04")
    response = client.get("/debug/engine/ping", headers=_api_key_headers())

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body == {
        "rpc": EngineRpc.CALENDAR_BUSINESS_DAYS.value,
        "response_bytes": 4,
    }
    assert engine.calls == [(EngineRpc.CALENDAR_BUSINESS_DAYS, b"")]


# --- 503 deploy-config safety net ------------------------------------------


def test_ping_returns_503_when_engine_client_missing(
    orchestrator_settings: OrchestratorSettings,
    fake_api_key_lookup: ApiKeyLookup,
    fake_firebase_verifier: tuple[Any, dict[str, dict[str, Any]]],
    fake_api_keys: dict[str, ApiKeyRecord],
) -> None:
    """Removing ``app.state.engine_client`` after build → 503 envelope.

    The lifespan provisions a stub by default, so to simulate the
    "engine client never wired" deploy-config bug we build the app
    with an injected client (skipping the lifespan default), then
    null out ``app.state.engine_client`` before the request, then
    bypass the lifespan entirely by **not** entering ``TestClient``
    as a context manager. Mirrors the MD-side
    ``test_quote_endpoint_returns_503_when_md_client_unconfigured``
    pattern from the MD client.
    """

    fake_api_keys.update(_seeded_api_keys())
    verifier, _ = fake_firebase_verifier
    app = create_app(
        orchestrator_settings,
        api_key_lookup=fake_api_key_lookup,
        firebase_verifier=verifier,
        engine_client=_FakeEngineClient(),
    )
    app.state.engine_client = None
    client = TestClient(app, raise_server_exceptions=False)
    try:
        response = client.get(
            "/debug/engine/ping",
            headers={**_api_key_headers(), "X-Request-Id": "rid-503"},
        )
    finally:
        client.close()

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    body = response.json()
    assert body["code"] == ENGINE_CLIENT_UNAVAILABLE_CODE
    assert body["request_id"] == "rid-503"


# --- /debug/engine/info ----------------------------------------------------


def test_info_endpoint_requires_auth(
    engine_app_factory: Callable[[EngineClient | None], FastAPI],
) -> None:
    app = engine_app_factory(None)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/debug/engine/info")
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json()["code"] == "unauthenticated"


def test_info_with_stub_backend_describes_stub_client(stub_app: TestClient) -> None:
    """Default deploy → ``backend="StubEngineClient"`` and retries off."""

    response = stub_app.get("/debug/engine/info", headers=_api_key_headers())
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["backend"] == "StubEngineClient"
    assert body["retry_enabled"] is False
    assert body["max_retries"] == 2  # default from settings
    assert body["timeout_s"] == 30.0
    assert body["grpc_target_configured"] is False


def test_info_with_grpc_target_describes_grpc_backend(
    fake_api_key_lookup: ApiKeyLookup,
    fake_firebase_verifier: tuple[Any, dict[str, dict[str, Any]]],
    fake_api_keys: dict[str, ApiKeyRecord],
) -> None:
    """Setting ``engine_grpc_target`` flips the backend + retry defaults.

    Constructs the gRPC channel during the lifespan; we tear it down
    immediately so the test doesn't leak a half-open channel into the
    pytest worker. The channel is never used (no RPC is dispatched).
    """

    fake_api_keys.update(_seeded_api_keys())
    verifier, _ = fake_firebase_verifier
    settings = OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="testsha",
        engine_grpc_target="engine.example:50051",
    )
    app = create_app(
        settings,
        api_key_lookup=fake_api_key_lookup,
        firebase_verifier=verifier,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/debug/engine/info", headers=_api_key_headers())

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["backend"] == "GrpcEngineClient"
    assert body["retry_enabled"] is True
    assert body["grpc_target_configured"] is True


# --- error mapper unit tests ------------------------------------------------


def test_map_engine_client_error_translates_each_subclass() -> None:
    """Direct unit cover of the engine-error → structured mapping table.

    Mirrors the MD client's ``test_map_md_client_error_translates_each_subclass``
    exhaustive test.
    """

    nie = map_engine_client_error(NotImplementedError("stub"))
    assert nie.status_code == HTTPStatus.BAD_GATEWAY
    assert getattr(nie, "code", None) == ENGINE_UNAVAILABLE_CODE

    timeout = map_engine_client_error(EngineTimeoutError("slow"))
    assert timeout.status_code == HTTPStatus.GATEWAY_TIMEOUT
    assert getattr(timeout, "code", None) == ENGINE_TIMEOUT_CODE

    retryable = map_engine_client_error(EngineRetryableError("UNAVAILABLE"))
    assert retryable.status_code == HTTPStatus.GATEWAY_TIMEOUT
    assert getattr(retryable, "code", None) == ENGINE_TIMEOUT_CODE

    invalid = map_engine_client_error(EngineRpcError("bad", code="INVALID_ARGUMENT", details="x"))
    assert invalid.status_code == HTTPStatus.BAD_REQUEST
    assert getattr(invalid, "code", None) == ENGINE_INVALID_REQUEST_CODE

    internal = map_engine_client_error(EngineRpcError("internal failure", code="INTERNAL"))
    assert internal.status_code == HTTPStatus.BAD_GATEWAY
    assert getattr(internal, "code", None) == ENGINE_UPSTREAM_ERROR_CODE

    rpc_unavailable_code = map_engine_client_error(EngineRpcError("from rpc", code="UNAVAILABLE"))
    assert rpc_unavailable_code.status_code == HTTPStatus.GATEWAY_TIMEOUT
    assert getattr(rpc_unavailable_code, "code", None) == ENGINE_TIMEOUT_CODE

    rpc_no_code = map_engine_client_error(EngineRpcError("no code"))
    assert rpc_no_code.status_code == HTTPStatus.BAD_GATEWAY
    assert getattr(rpc_no_code, "code", None) == ENGINE_UPSTREAM_ERROR_CODE

    transport = map_engine_client_error(ConnectionError("refused"))
    assert transport.status_code == HTTPStatus.BAD_GATEWAY
    assert getattr(transport, "code", None) == ENGINE_UNREACHABLE_CODE

    osfail = map_engine_client_error(OSError("ENETUNREACH"))
    assert osfail.status_code == HTTPStatus.BAD_GATEWAY
    assert getattr(osfail, "code", None) == ENGINE_UNREACHABLE_CODE

    fallthrough = map_engine_client_error(RuntimeError("???"))
    assert fallthrough.status_code == HTTPStatus.BAD_GATEWAY
    assert getattr(fallthrough, "code", None) == ENGINE_UPSTREAM_ERROR_CODE


def test_missing_fixing_maps_to_dedicated_d54_code_not_raw_abort() -> None:
    """A QuantLib ``Missing <index> fixing for <date>`` abort → a clean structured error.

    O47b(A): pricing a seasoned / already-started swap whose first past
    fixing is absent makes the engine abort inside QuantLib. That failure
    must translate into the stable ``missing_required_fixing`` code (422)
    whose ``details`` name WHICH fixing is missing (index + date) — NOT
    the opaque ``engine_upstream_error`` (502) it used to collapse into.
    """

    # Engine surfaces the QuantLib message in the gRPC error details; the
    # status code is INTERNAL (not INVALID_ARGUMENT) — detection is by text.
    exc = EngineRpcError(
        "engine RPC failed: INTERNAL (Missing Euribor6M Actual/360 fixing for January 15th, 2025)",
        code="INTERNAL",
        details="Missing Euribor6M Actual/360 fixing for January 15th, 2025",
    )
    mapped = map_engine_client_error(exc)

    assert mapped.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert getattr(mapped, "code", None) == MISSING_REQUIRED_FIXING_CODE
    # ``details`` names the missing fixing (index + date) + the raw engine text.
    details = getattr(mapped, "details", None)
    assert isinstance(details, list)
    assert len(details) == 1
    entry = details[0]
    assert entry["index"] == "Euribor6M Actual/360"
    assert entry["fixing_date"] == "January 15th, 2025"
    assert "Missing" in entry["engine_detail"]
    # Human message is actionable — names the index + tells the caller what to do.
    assert "Euribor6M Actual/360" in mapped.detail
    assert "January 15th, 2025" in mapped.detail


def test_negative_time_abort_maps_to_typed_date_coherence_422() -> None:
    """A QuantLib ``negative time`` abort → the typed 422, not an opaque 502.

    The cross-source As-Of defect: the default As-Of (e.g. BoE latest,
    T-1) can predate a curve auto-rolled to another feed's later date
    (US Treasury, T+0); shapes whose math runs at the as_of (bond
    settlement/accrual/yield) then abort with ``ABORTED (negative time
    (-0.0027) given)``. Translation only — no pre-flight rejection (the
    engine legitimately ACCEPTS as_of < reference_date for many shapes,
    e.g. vol sampling / swaptions / T-1 GBP swaps): when the engine DOES
    abort, the boundary maps it to the stable
    ``pricing_as_of_before_curve_date`` (422) with an actionable
    date-coherence message + the raw engine text for an investigator.
    """

    exc = EngineRpcError(
        "engine RPC failed: ABORTED (negative time (-0.0027397260273972603) given)",
        code="ABORTED",
        details="negative time (-0.0027397260273972603) given",
    )
    mapped = map_engine_client_error(exc)

    assert mapped.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert getattr(mapped, "code", None) == PRICING_AS_OF_BEFORE_CURVE_DATE_CODE
    assert isinstance(mapped, EngineDateCoherenceError)
    # Generic form (the boundary has no request context): names the cause,
    # what to do, and keeps the raw engine text.
    assert "date-coherence" in mapped.detail
    assert "predates a curve's reference date" in mapped.detail
    assert "negative time" in mapped.detail
    details = getattr(mapped, "details", None)
    assert isinstance(details, list)
    assert "negative time" in details[0]["engine_detail"]
    assert "re-select" in details[0]["guidance"].lower()

    # A non-matching ABORTED message keeps the plain generic 502 mapping.
    generic = map_engine_client_error(EngineRpcError("engine RPC failed: ABORTED (boom)"))
    assert generic.status_code == HTTPStatus.BAD_GATEWAY
    assert getattr(generic, "code", None) == ENGINE_UPSTREAM_ERROR_CODE
    assert "date-coherence" not in generic.detail


def test_date_coherence_add_date_context_names_as_of_and_curve_dates() -> None:
    """``add_date_context`` upgrades the generic 422 with the request's dates.

    A route holding the resolved curves (bonds today) enriches the mapped
    error: the human message names the as-of + the offending curve + its
    reference date, and the per-curve entries are PREPENDED to ``details``
    (the raw ``engine_detail`` entry stays last).
    """

    @dataclass
    class _Curve:
        id: uuid.UUID | None
        name: str
        reference_date: date | None

    err = EngineDateCoherenceError(engine_detail="negative time (-0.0027) given")
    curve_id = uuid.uuid4()
    err.add_date_context(
        as_of="2026-07-16",
        curves=[
            _Curve(curve_id, "USD Treasury (public, daily)", date(2026, 7, 17)),
            _Curve(None, "NO-REF", None),  # dateless curves are skipped
        ],
    )
    assert err.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert err.code == PRICING_AS_OF_BEFORE_CURVE_DATE_CODE
    # Message names both dates + the curve + the action.
    assert "2026-07-16" in err.detail
    assert "predates the reference date of curve 'USD Treasury (public, daily)'" in err.detail
    assert "2026-07-17" in err.detail
    assert "re-select" in err.detail.lower()
    # Curve entry first, raw engine detail last.
    assert err.details[0] == {
        "as_of": "2026-07-16",
        "curve": "USD Treasury (public, daily)",
        "curve_id": str(curve_id),
        "reference_date": "2026-07-17",
        "guidance": (
            "Price at the curve's reference date, or re-select an As-Of date "
            "on/after every curve's reference date."
        ),
    }
    assert "engine_detail" in err.details[-1]


def test_parse_missing_fixing_variants() -> None:
    """The detector matches the QuantLib text across wrapping / casing / date forms."""

    # ISO date form, unwrapped.
    parsed = parse_missing_fixing("Missing ESTR fixing for 2025-01-15")
    assert parsed == ("ESTR", "2025-01-15")

    # Wrapped in the wire-layer's ``... (message)`` envelope.
    parsed = parse_missing_fixing(
        "engine RPC failed: INTERNAL (Missing Euribor 6M fixing for January 15th, 2025)"
    )
    assert parsed == ("Euribor 6M", "January 15th, 2025")

    # Non-matching text falls through (returns None → generic mapping).
    assert parse_missing_fixing("some other engine failure") is None
    assert parse_missing_fixing("INTERNAL: bootstrap negative time") is None


def test_engine_client_unavailable_error_carries_d54_code() -> None:
    err = EngineClientUnavailableError()
    assert err.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert err.code == ENGINE_CLIENT_UNAVAILABLE_CODE


# --- retry decorator -------------------------------------------------------


class _RecordingFlakyClient(EngineClient):
    """Raises a sequence of exceptions, then succeeds on the Nth call.

    Lets the retry tests assert how many attempts the decorator made
    before giving up vs succeeding.
    """

    def __init__(self, *, exceptions: list[BaseException], success: bytes) -> None:
        self._queue = list(exceptions)
        self._success = success
        self.attempts = 0

    async def call(self, rpc: EngineRpc, request_bytes: bytes) -> bytes:
        del rpc, request_bytes
        self.attempts += 1
        if self._queue:
            raise self._queue.pop(0)
        return self._success


def _retry_config() -> EngineClientConfig:
    return EngineClientConfig(
        address="stub",
        timeout_s=1.0,
        max_retries=3,
        backoff_base_s=0.0001,
        backoff_cap_s=0.001,
    )


@pytest.mark.asyncio
async def test_retry_decorator_retries_on_unavailable_then_succeeds() -> None:
    inner = _RecordingFlakyClient(
        exceptions=[
            EngineRetryableError("UNAVAILABLE 1"),
            EngineRetryableError("UNAVAILABLE 2"),
        ],
        success=b"ok",
    )
    decorated = RetryingEngineClient(inner, _retry_config())

    result = await decorated.call(EngineRpc.PRICE_VANILLA_SWAP, b"")
    assert result == b"ok"
    assert inner.attempts == 3  # 2 retries + 1 success


@pytest.mark.asyncio
async def test_retry_decorator_retries_on_deadline_exceeded() -> None:
    inner = _RecordingFlakyClient(
        exceptions=[EngineTimeoutError("deadline 1")],
        success=b"ok",
    )
    decorated = RetryingEngineClient(inner, _retry_config())

    result = await decorated.call(EngineRpc.PRICE_VANILLA_SWAP, b"")
    assert result == b"ok"
    assert inner.attempts == 2


@pytest.mark.asyncio
async def test_retry_decorator_does_not_retry_on_invalid_argument() -> None:
    inner = _RecordingFlakyClient(
        exceptions=[EngineRpcError("bad", code="INVALID_ARGUMENT")],
        success=b"ok",
    )
    decorated = RetryingEngineClient(inner, _retry_config())

    with pytest.raises(EngineRpcError) as excinfo:
        await decorated.call(EngineRpc.PRICE_VANILLA_SWAP, b"")
    assert excinfo.value.code == "INVALID_ARGUMENT"
    assert inner.attempts == 1


@pytest.mark.asyncio
async def test_retry_decorator_gives_up_after_max_retries() -> None:
    inner = _RecordingFlakyClient(
        exceptions=[
            EngineRetryableError("UNAVAILABLE 1"),
            EngineRetryableError("UNAVAILABLE 2"),
            EngineRetryableError("UNAVAILABLE 3"),
            EngineRetryableError("UNAVAILABLE 4"),
        ],
        success=b"ok",
    )
    decorated = RetryingEngineClient(inner, _retry_config())  # max_retries=3

    with pytest.raises(EngineRetryableError):
        await decorated.call(EngineRpc.PRICE_VANILLA_SWAP, b"")
    assert inner.attempts == 4  # 1 initial + 3 retries


# --- backend factory invariants --------------------------------------------


def test_build_engine_client_defaults_to_stub_unwrapped() -> None:
    """Default settings → :class:`StubEngineClient`, no retry decorator."""

    settings = OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="testsha",
    )
    client = build_engine_client(settings)
    assert isinstance(client, StubEngineClient)
    assert not isinstance(client, RetryingEngineClient)


def test_build_engine_client_with_target_returns_retry_wrapped_grpc() -> None:
    """Target set → :class:`GrpcEngineClient` wrapped in retry decorator."""

    settings = OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="testsha",
        engine_grpc_target="engine.example:50051",
    )
    client = build_engine_client(settings)
    assert isinstance(client, RetryingEngineClient)
    info = describe_engine_client(client, settings)
    assert info.backend == "GrpcEngineClient"
    assert info.retry_enabled is True


def test_build_engine_client_retry_disabled_via_settings_for_grpc() -> None:
    """Tri-state ``engine_retry_enabled=False`` overrides the gRPC default."""

    settings = OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="testsha",
        engine_grpc_target="engine.example:50051",
        engine_retry_enabled=False,
    )
    client = build_engine_client(settings)
    assert isinstance(client, GrpcEngineClient)
    assert not isinstance(client, RetryingEngineClient)


def test_build_engine_client_retry_enabled_via_settings_for_stub() -> None:
    """Tri-state ``engine_retry_enabled=True`` forces retries on the stub."""

    settings = OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="testsha",
        engine_retry_enabled=True,
    )
    client = build_engine_client(settings)
    assert isinstance(client, RetryingEngineClient)


def test_build_engine_client_zero_retries_disables_decorator() -> None:
    """A 0-budget retry config is just a stub even if grpc target is set."""

    settings = OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="testsha",
        engine_grpc_target="engine.example:50051",
        engine_max_retries=0,
    )
    client = build_engine_client(settings)
    assert isinstance(client, GrpcEngineClient)
    assert not isinstance(client, RetryingEngineClient)


# --- gRPC backend (channel + interceptor wiring) ----------------------------


@pytest.mark.asyncio
async def test_build_grpc_engine_client_attaches_channel_and_interceptor() -> None:
    """The factory opens a real grpc.aio.Channel with our interceptor.

    We don't make an actual RPC (no engine to talk to here — that's
    the gated ``orchestrator_engine_*`` suite). The unit-level
    assertion is that the channel + config land on the singleton.
    Post-08c the singleton's ``call`` dispatches real bytes (the
    ``NotImplementedError`` placeholder went away when the vendored bindings landed
    real bindings + RPC dispatch); attempting it here without an
    engine would either time out or raise an
    :class:`EngineRetryableError` from ``UNAVAILABLE`` — neither
    of which is a unit-level invariant worth pinning here.
    """

    config = EngineClientConfig(address="engine.example:50051", timeout_s=5.0)
    client = build_grpc_engine_client(target="engine.example:50051", config=config)
    try:
        assert client.channel is not None
        assert isinstance(client.config, EngineClientConfig)
    finally:
        await client.close()


def test_engine_rpc_method_paths_pin_canonical_grpc_paths() -> None:
    """Every :class:`EngineRpc` enum entry maps to a canonical gRPC path.

    Pins the static :data:`ENGINE_RPC_METHOD_PATHS` mapping: each
    enum's value is the engine's canonical method
    name; each path is ``/quantra.QuantraServer/<MethodName>``. A
    future enum rename or namespace move trips this assertion
    rather than silently retargeting the orchestrator.
    """

    assert set(ENGINE_RPC_METHOD_PATHS) == set(EngineRpc)
    for rpc in EngineRpc:
        assert ENGINE_RPC_METHOD_PATHS[rpc] == f"/quantra.QuantraServer/{rpc.value}", rpc


def test_request_id_interceptor_is_constructible() -> None:
    """``RequestIdInterceptor`` is a no-arg construct with the right type."""

    interceptor = RequestIdInterceptor()
    assert isinstance(interceptor, grpc.aio.UnaryUnaryClientInterceptor)


# --- gRPC translation helpers ----------------------------------------------


class _FakeAioRpcError(Exception):
    """Stand-in for ``grpc.aio.AioRpcError`` for the translator unit tests.

    The real AioRpcError is hard to instantiate without a live channel;
    the translator only reads ``code()`` / ``details()`` so a tiny
    fake is enough.
    """

    def __init__(self, *, code_name: str, details: str = "") -> None:
        super().__init__(f"{code_name}: {details}")
        self._code_name = code_name
        self._details = details

    def code(self) -> Any:
        return getattr(grpc.StatusCode, self._code_name)

    def details(self) -> str:
        return self._details


def test_translate_aio_rpc_error_unavailable_to_retryable() -> None:
    err = cast(Any, _FakeAioRpcError(code_name="UNAVAILABLE", details="boom"))
    translated = translate_aio_rpc_error(err)
    assert isinstance(translated, EngineRetryableError)


def test_translate_aio_rpc_error_deadline_to_timeout() -> None:
    err = cast(Any, _FakeAioRpcError(code_name="DEADLINE_EXCEEDED"))
    translated = translate_aio_rpc_error(err)
    assert isinstance(translated, EngineTimeoutError)


def test_translate_aio_rpc_error_invalid_argument_keeps_code() -> None:
    err = cast(Any, _FakeAioRpcError(code_name="INVALID_ARGUMENT", details="bad calendar"))
    translated = translate_aio_rpc_error(err)
    assert isinstance(translated, EngineRpcError)
    assert translated.code == "INVALID_ARGUMENT"
    assert translated.details == "bad calendar"


def test_engine_message_naming_missing_fixing_maps_to_clean_d54() -> None:
    """An engine that surfaces the QuantLib text as its gRPC message → clean structured error.

    O47b(A): when the engine reports ``Missing <index> fixing for <date>`` as
    the gRPC ``error_message`` (``exc.details()``), the translator carries it
    onto ``EngineRpcError.details`` and the mapper turns it into an
    actionable ``missing_required_fixing`` (422) naming the fixing — not the
    opaque ``engine_upstream_error`` (502) it used to collapse into.
    """

    err = cast(
        Any,
        _FakeAioRpcError(
            code_name="ABORTED",
            details=("2nd leg: Missing Euribor6M Actual/360 fixing for January 13th, 2025"),
        ),
    )
    translated = translate_aio_rpc_error(err)
    assert isinstance(translated, EngineRpcError)

    mapped = map_engine_client_error(translated)
    assert mapped.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert getattr(mapped, "code", None) == MISSING_REQUIRED_FIXING_CODE
    details = getattr(mapped, "details", None)
    assert isinstance(details, list)
    entry = details[0]
    assert entry["index"] == "Euribor6M Actual/360"
    assert entry["fixing_date"] == "January 13th, 2025"


def test_grpc_status_code_name_extracts_name() -> None:
    err = cast(Any, _FakeAioRpcError(code_name="INTERNAL"))
    assert grpc_status_code_name(err) == "INTERNAL"
