"""Unit tests for the orchestrator's MD client wiring.

Exercises everything the plan calls out as in-scope without booting a
real MD service:

* The two debug routes (``GET /debug/md/quote/{canonical_id}``,
  ``GET /debug/md/cache/stats``) are auth-gated and emit the
  envelope on failure.
* All three :class:`MdClientError` subclasses translate to their
  documented structured ``code`` tokens (``quote_not_found``,
  ``md_unreachable``, ``md_upstream_error``).
* A second request for the same ``(canonical_id, as_of)`` is served
  from the in-process cache (zero additional upstream calls).
* The MD client forwards the inbound ``X-Request-Id`` to the upstream
  call so MD logs correlate with the originating orchestrator request.
* The lifespan-owned :class:`MdClient` is the *singleton* every request
  reuses (no per-request reconstruction of the HTTP pool).

The fake :class:`MdClient` is a real ``MdClient`` instance built around
``httpx.MockTransport`` so the cache + retry / event-hook plumbing
inside the public API runs end-to-end. Tests that need to force a
specific :class:`MdClientError` use a stub subclass of ``MdClient``
(injected via the same ``md_client=`` seam ``create_app`` exposes) to
keep the failure path deterministic.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from http import HTTPStatus
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from quantra_common.auth.context import ApiKeyRecord
from quantra_common.auth.lookup import ApiKeyLookup
from quantra_common.md_client import (
    MdClient,
    MdClientConfig,
    MdHttpStatusError,
    MdNotFoundError,
    MdResponseError,
    MdTimeoutError,
    MdTransportError,
)
from quantra_common.settings import Environment, LogLevel
from quantra_orchestrator.app import create_app
from quantra_orchestrator.md import (
    MdClientUnavailableError,
    QuoteCacheStats,
    TtlBoundedQuoteCache,
    build_md_client,
    map_md_client_error,
)
from quantra_orchestrator.settings import OrchestratorSettings

QUOTE_CID = "USD.IRS.10Y.PAR_RATE"
QUOTE_AS_OF = "2026-05-13"


# --------------------------------------------------------------------- helpers


class _MockMdHandler:
    """Captures every request the MockTransport sees and returns canned bodies.

    Tests inspect ``calls`` to assert how many upstream requests the
    orchestrator emitted (cache effectiveness) and what headers each
    one carried (request-id propagation).
    """

    def __init__(self, responder: Callable[[httpx.Request], httpx.Response]) -> None:
        self._responder = responder
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return self._responder(request)


def _resolved_quote_payload(*, canonical_id: str, as_of: str, value: float) -> dict[str, Any]:
    return {
        "items": [
            {
                "canonical_id": canonical_id,
                "requested_as_of": f"{as_of}T00:00:00+00:00",
                "found": True,
                "is_exact": True,
                "resolved_as_of": f"{as_of}T00:00:00+00:00",
                "value": value,
                "source": "test",
                "vendor_id": "FAKE",
            }
        ]
    }


def _miss_payload(*, canonical_id: str, as_of: str) -> dict[str, Any]:
    return {
        "items": [
            {
                "canonical_id": canonical_id,
                "requested_as_of": f"{as_of}T00:00:00+00:00",
                "found": False,
                "is_exact": False,
                "resolved_as_of": None,
                "value": None,
                "source": None,
                "vendor_id": None,
            }
        ]
    }


def _make_md_client_with_handler(
    *,
    handler: _MockMdHandler,
    cache: TtlBoundedQuoteCache,
    request_id_header: str = "X-Request-Id",
) -> MdClient:
    """Build an :class:`MdClient` whose transport is a MockTransport.

    The orchestrator-side request-id event hook is wired identically to
    production via :func:`build_md_client` so the test exercises the
    same code path. We only swap the underlying TCP transport for a
    Mock so no real network call happens.
    """

    settings = OrchestratorSettings(
        md_service_url="http://md-mock",
        md_service_timeout_s=5.0,
        md_client_max_retries=0,
        md_client_backoff_base_s=0.01,
        md_client_backoff_cap_s=0.02,
    )
    real_client, real_transport = build_md_client(
        settings,
        cache=cache,
        request_id_header=request_id_header,
    )
    # Swap the underlying transport for a MockTransport without
    # rebuilding the rest of the wiring (event hooks, base_url,
    # timeout). httpx exposes the transport only via the constructor,
    # so we patch the live attribute here to keep the fixture short.
    real_transport._transport = httpx.MockTransport(handler)
    return real_client


def _stub_md_client(*, raise_exc: Exception) -> MdClient:
    """An MdClient subclass that raises ``raise_exc`` from ``resolve_quotes``.

    Used by the error-path tests so each :class:`MdClientError`
    subclass maps to its documented error envelope without depending on
    a particular MockTransport response shape.
    """

    config = MdClientConfig(base_url="http://stub", timeout_s=1.0, max_retries=0)

    class _RaisingClient(MdClient):
        async def resolve_quotes(
            self,
            canonical_ids: list[str],
            as_of: Any,
            *,
            snapshot_version: str | None = None,
        ) -> Any:
            del canonical_ids, as_of, snapshot_version
            raise raise_exc

    # Pass a real httpx client so MdClient.__init__ stays happy; we
    # never actually call it because ``resolve_quotes`` is overridden.
    return _RaisingClient(config, client=httpx.AsyncClient(base_url="http://stub"))


def _state_attr(test_client: TestClient, name: str) -> Any:
    """Read an attribute off ``TestClient.app.state`` with mypy quiet.

    ``TestClient.app`` is typed as the bare ASGI callable so mypy can't
    see ``state`` directly. The orchestrator factory always returns a
    ``FastAPI`` instance, so ``state`` is in fact present at runtime.
    """

    app: FastAPI = cast(FastAPI, test_client.app)
    return getattr(app.state, name, None)


def _md_state_client(test_client: TestClient) -> MdClient:
    md_client = _state_attr(test_client, "md_client")
    assert isinstance(md_client, MdClient)
    return md_client


def _md_state_cache(test_client: TestClient) -> TtlBoundedQuoteCache:
    cache = _state_attr(test_client, "md_cache")
    assert isinstance(cache, TtlBoundedQuoteCache)
    return cache


def _api_key_headers() -> dict[str, str]:
    return {"X-API-Key": "good-key"}


def _seeded_api_keys() -> dict[str, ApiKeyRecord]:
    return {
        "good-key": ApiKeyRecord(
            api_key_id="md-test-key",
            owner_uid="md-test-uid",
            name="MD Test Key",
            email="md@example.com",
            tier="free",
            active=True,
        )
    }


# ----------------------------------------------------------------------- fixtures


@pytest.fixture
def md_cache() -> TtlBoundedQuoteCache:
    return TtlBoundedQuoteCache(max_entries=64, ttl_s=300.0)


@pytest.fixture
def successful_handler() -> _MockMdHandler:
    """A handler that always returns one successful resolved quote."""

    def _ok(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            HTTPStatus.OK,
            json=_resolved_quote_payload(canonical_id=QUOTE_CID, as_of=QUOTE_AS_OF, value=4.25),
        )

    return _MockMdHandler(_ok)


@pytest.fixture
def miss_handler() -> _MockMdHandler:
    """A handler that always returns ``found=False`` for the requested id."""

    def _miss(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            HTTPStatus.OK,
            json=_miss_payload(canonical_id=QUOTE_CID, as_of=QUOTE_AS_OF),
        )

    return _MockMdHandler(_miss)


@pytest.fixture
def md_app_factory(
    orchestrator_settings: OrchestratorSettings,
    fake_api_key_lookup: ApiKeyLookup,
    fake_firebase_verifier: tuple[Any, dict[str, dict[str, Any]]],
    fake_api_keys: dict[str, ApiKeyRecord],
    md_cache: TtlBoundedQuoteCache,
) -> Callable[[MdClient], FastAPI]:
    """Build the orchestrator app with a swappable ``MdClient`` injected."""

    fake_api_keys.update(_seeded_api_keys())
    verifier, _ = fake_firebase_verifier

    def _build(md_client: MdClient) -> FastAPI:
        return create_app(
            orchestrator_settings,
            api_key_lookup=fake_api_key_lookup,
            firebase_verifier=verifier,
            md_client=md_client,
            md_cache=md_cache,
        )

    return _build


@pytest.fixture
def successful_app(
    md_app_factory: Callable[[MdClient], FastAPI],
    successful_handler: _MockMdHandler,
    md_cache: TtlBoundedQuoteCache,
) -> Iterator[tuple[TestClient, _MockMdHandler]]:
    md_client = _make_md_client_with_handler(handler=successful_handler, cache=md_cache)
    app = md_app_factory(md_client)
    test_client = TestClient(app, raise_server_exceptions=False)
    try:
        yield test_client, successful_handler
    finally:
        test_client.close()


# ------------------------------------------------------------------------- tests


# --- /debug/md/quote --------------------------------------------------------


def test_quote_endpoint_requires_auth(
    md_app_factory: Callable[[MdClient], FastAPI],
    successful_handler: _MockMdHandler,
    md_cache: TtlBoundedQuoteCache,
) -> None:
    """No credential → 401 with ``code="unauthenticated"`` envelope."""

    md_client = _make_md_client_with_handler(handler=successful_handler, cache=md_cache)
    app = md_app_factory(md_client)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            f"/debug/md/quote/{QUOTE_CID}",
            params={"as_of": QUOTE_AS_OF},
        )
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    body = response.json()
    assert body["code"] == "unauthenticated"
    # Auth runs before the MD client is touched.
    assert successful_handler.calls == []


def test_quote_cache_miss_then_hit_emits_one_upstream_call(
    successful_app: tuple[TestClient, _MockMdHandler],
) -> None:
    """Two GETs for the same ``(canonical_id, as_of)`` → one MD round-trip."""

    client, handler = successful_app

    first = client.get(
        f"/debug/md/quote/{QUOTE_CID}",
        params={"as_of": QUOTE_AS_OF},
        headers=_api_key_headers(),
    )
    second = client.get(
        f"/debug/md/quote/{QUOTE_CID}",
        params={"as_of": QUOTE_AS_OF},
        headers=_api_key_headers(),
    )

    assert first.status_code == HTTPStatus.OK
    assert second.status_code == HTTPStatus.OK
    assert first.json() == second.json()
    assert first.json()["value"] == 4.25
    assert first.json()["found"] is True
    # One miss, then a hit served entirely from the cache.
    assert len(handler.calls) == 1


def test_quote_cache_stats_track_hits_and_misses(
    successful_app: tuple[TestClient, _MockMdHandler],
) -> None:
    """Cache counters surface through ``GET /debug/md/cache/stats``."""

    client, _ = successful_app

    # Drive one miss + one hit through the orchestrator route.
    client.get(
        f"/debug/md/quote/{QUOTE_CID}",
        params={"as_of": QUOTE_AS_OF},
        headers=_api_key_headers(),
    )
    client.get(
        f"/debug/md/quote/{QUOTE_CID}",
        params={"as_of": QUOTE_AS_OF},
        headers=_api_key_headers(),
    )

    stats_response = client.get("/debug/md/cache/stats", headers=_api_key_headers())
    assert stats_response.status_code == HTTPStatus.OK
    stats = stats_response.json()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["expirations"] == 0
    assert stats["size"] == 1
    assert stats["max_entries"] == 64
    assert stats["ttl_s"] == 300.0


def test_quote_endpoint_propagates_request_id_to_upstream(
    successful_app: tuple[TestClient, _MockMdHandler],
) -> None:
    """The inbound ``X-Request-Id`` is forwarded to the MD service.

    Drives the path that exists for log-correlation: the orchestrator
    request runs under ``RequestIdMiddleware``, which binds ``request_id``
    in ``structlog.contextvars``; the MdClient's request event hook
    reads that binding and copies it into the outgoing httpx request.
    """

    client, handler = successful_app

    response = client.get(
        f"/debug/md/quote/{QUOTE_CID}",
        params={"as_of": QUOTE_AS_OF},
        headers={**_api_key_headers(), "X-Request-Id": "rid-md-123"},
    )

    assert response.status_code == HTTPStatus.OK
    assert handler.calls, "expected one upstream call on cache miss"
    forwarded = handler.calls[-1].headers.get("X-Request-Id")
    assert forwarded == "rid-md-123"


def test_quote_endpoint_request_id_hook_no_op_when_header_absent(
    successful_app: tuple[TestClient, _MockMdHandler],
) -> None:
    """No inbound ``X-Request-Id`` → middleware generates one and forwards it.

    The middleware always binds *some* ``request_id`` (auto-generated
    UUID4 hex if absent), so the upstream call always carries the
    header. Asserting on the value would be brittle; assert it is
    present and non-empty.
    """

    client, handler = successful_app

    response = client.get(
        f"/debug/md/quote/{QUOTE_CID}",
        params={"as_of": QUOTE_AS_OF},
        headers=_api_key_headers(),
    )

    assert response.status_code == HTTPStatus.OK
    assert handler.calls, "expected one upstream call on cache miss"
    forwarded = handler.calls[-1].headers.get("X-Request-Id")
    assert forwarded
    assert len(forwarded) >= 16


def test_quote_endpoint_returns_404_for_upstream_miss(
    md_app_factory: Callable[[MdClient], FastAPI],
    miss_handler: _MockMdHandler,
    md_cache: TtlBoundedQuoteCache,
) -> None:
    """``found=False`` from the MD service → structured ``quote_not_found`` envelope."""

    md_client = _make_md_client_with_handler(handler=miss_handler, cache=md_cache)
    app = md_app_factory(md_client)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            f"/debug/md/quote/{QUOTE_CID}",
            params={"as_of": QUOTE_AS_OF},
            headers={**_api_key_headers(), "X-Request-Id": "rid-miss"},
        )

    assert response.status_code == HTTPStatus.NOT_FOUND
    body = response.json()
    assert body["code"] == "quote_not_found"
    assert QUOTE_CID in body["error"]
    assert QUOTE_AS_OF in body["error"]
    assert body["request_id"] == "rid-miss"


def test_quote_endpoint_returns_404_when_md_returns_not_found(
    md_app_factory: Callable[[MdClient], FastAPI],
) -> None:
    """``MdNotFoundError`` from the client maps to ``quote_not_found``."""

    stub = _stub_md_client(raise_exc=MdNotFoundError(404, "no such quote"))
    with TestClient(md_app_factory(stub), raise_server_exceptions=False) as client:
        response = client.get(
            f"/debug/md/quote/{QUOTE_CID}",
            params={"as_of": QUOTE_AS_OF},
            headers={**_api_key_headers(), "X-Request-Id": "rid-nf"},
        )

    assert response.status_code == HTTPStatus.NOT_FOUND
    body = response.json()
    assert body["code"] == "quote_not_found"
    assert body["request_id"] == "rid-nf"


def test_quote_endpoint_returns_502_when_md_unreachable(
    md_app_factory: Callable[[MdClient], FastAPI],
) -> None:
    """``MdTransportError`` (or ``MdTimeoutError``) → ``md_unreachable`` (502)."""

    stub = _stub_md_client(raise_exc=MdTransportError("boom: connection refused"))
    with TestClient(md_app_factory(stub), raise_server_exceptions=False) as client:
        response = client.get(
            f"/debug/md/quote/{QUOTE_CID}",
            params={"as_of": QUOTE_AS_OF},
            headers={**_api_key_headers(), "X-Request-Id": "rid-unr"},
        )

    assert response.status_code == HTTPStatus.BAD_GATEWAY
    body = response.json()
    assert body["code"] == "md_unreachable"
    assert body["request_id"] == "rid-unr"


def test_quote_endpoint_returns_502_when_md_times_out(
    md_app_factory: Callable[[MdClient], FastAPI],
) -> None:
    """``MdTimeoutError`` is a transport subclass → same ``md_unreachable``."""

    stub = _stub_md_client(raise_exc=MdTimeoutError("timeout after 5s"))
    with TestClient(md_app_factory(stub), raise_server_exceptions=False) as client:
        response = client.get(
            f"/debug/md/quote/{QUOTE_CID}",
            params={"as_of": QUOTE_AS_OF},
            headers=_api_key_headers(),
        )

    assert response.status_code == HTTPStatus.BAD_GATEWAY
    assert response.json()["code"] == "md_unreachable"


def test_quote_endpoint_returns_502_when_md_returns_5xx(
    md_app_factory: Callable[[MdClient], FastAPI],
) -> None:
    """Upstream 5xx → ``md_upstream_error`` (502)."""

    stub = _stub_md_client(raise_exc=MdHttpStatusError(503, "upstream temporarily unavailable"))
    with TestClient(md_app_factory(stub), raise_server_exceptions=False) as client:
        response = client.get(
            f"/debug/md/quote/{QUOTE_CID}",
            params={"as_of": QUOTE_AS_OF},
            headers={**_api_key_headers(), "X-Request-Id": "rid-5xx"},
        )

    assert response.status_code == HTTPStatus.BAD_GATEWAY
    body = response.json()
    assert body["code"] == "md_upstream_error"
    assert body["request_id"] == "rid-5xx"


def test_quote_endpoint_returns_503_when_md_client_unconfigured(
    fake_api_key_lookup: ApiKeyLookup,
    fake_firebase_verifier: tuple[Any, dict[str, dict[str, Any]]],
    fake_api_keys: dict[str, ApiKeyRecord],
    md_cache: TtlBoundedQuoteCache,
) -> None:
    """No MdClient on app.state → 503 with ``md_client_unavailable``.

    Build the settings explicitly with ``md_service_url=None`` so the
    lifespan's MD-client opener short-circuits (otherwise the lifespan
    would happily build a real client against whatever ``MD_SERVICE_URL``
    the contributor's ``.env`` has set, defeating the test).
    """

    fake_api_keys.update(_seeded_api_keys())
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
        md_client=None,
        md_cache=md_cache,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            f"/debug/md/quote/{QUOTE_CID}",
            params={"as_of": QUOTE_AS_OF},
            headers=_api_key_headers(),
        )

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["code"] == "md_client_unavailable"


# --- /debug/md/cache/stats --------------------------------------------------


def test_cache_stats_endpoint_requires_auth(
    md_app_factory: Callable[[MdClient], FastAPI],
    successful_handler: _MockMdHandler,
    md_cache: TtlBoundedQuoteCache,
) -> None:
    md_client = _make_md_client_with_handler(handler=successful_handler, cache=md_cache)
    app = md_app_factory(md_client)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/debug/md/cache/stats")
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json()["code"] == "unauthenticated"


def test_cache_stats_baseline_for_empty_cache(
    md_app_factory: Callable[[MdClient], FastAPI],
    md_cache: TtlBoundedQuoteCache,
) -> None:
    # Build app without an MdClient — the cache stats route only needs
    # the cache to exist on app.state.
    md_client = _stub_md_client(raise_exc=RuntimeError("never called"))
    app = md_app_factory(md_client)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/debug/md/cache/stats", headers=_api_key_headers())

    assert response.status_code == HTTPStatus.OK
    stats = response.json()
    assert stats == {
        "hits": 0,
        "misses": 0,
        "expirations": 0,
        "size": 0,
        "max_entries": 64,
        "ttl_s": 300.0,
    }
    # No calls to the underlying upstream were attempted.
    assert isinstance(md_cache, TtlBoundedQuoteCache)
    assert md_cache.stats() == QuoteCacheStats(
        hits=0, misses=0, expirations=0, size=0, max_entries=64, ttl_s=300.0
    )


# --- error mapper unit -----------------------------------------------------


def test_map_md_client_error_translates_each_subclass() -> None:
    """Direct unit cover of the ``MdClientError`` → structured mapping table."""

    nf = map_md_client_error(MdNotFoundError(404, "missing"), canonical_id="A", as_of="2026-01-01")
    assert nf.status_code == HTTPStatus.NOT_FOUND
    assert getattr(nf, "code", None) == "quote_not_found"

    transport = map_md_client_error(MdTransportError("dns"), canonical_id="A", as_of="2026-01-01")
    assert transport.status_code == HTTPStatus.BAD_GATEWAY
    assert getattr(transport, "code", None) == "md_unreachable"

    timeout = map_md_client_error(MdTimeoutError("slow"), canonical_id="A", as_of="2026-01-01")
    assert timeout.status_code == HTTPStatus.BAD_GATEWAY
    assert getattr(timeout, "code", None) == "md_unreachable"

    http5xx = map_md_client_error(
        MdHttpStatusError(500, "kaboom"), canonical_id="A", as_of="2026-01-01"
    )
    assert http5xx.status_code == HTTPStatus.BAD_GATEWAY
    assert getattr(http5xx, "code", None) == "md_upstream_error"

    parse = map_md_client_error(
        MdResponseError("garbage body"), canonical_id="A", as_of="2026-01-01"
    )
    assert parse.status_code == HTTPStatus.BAD_GATEWAY
    assert getattr(parse, "code", None) == "md_upstream_error"


def test_md_client_unavailable_error_carries_d54_code() -> None:
    err = MdClientUnavailableError()
    assert err.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert err.code == "md_client_unavailable"


# --- singleton invariant ---------------------------------------------------


def test_md_client_is_a_singleton_per_process(
    successful_app: tuple[TestClient, _MockMdHandler],
) -> None:
    """Every request resolves the *same* MdClient instance from app.state."""

    client, handler = successful_app

    initial_md = _md_state_client(client)
    initial_cache = _md_state_cache(client)

    client.get(
        f"/debug/md/quote/{QUOTE_CID}",
        params={"as_of": QUOTE_AS_OF},
        headers=_api_key_headers(),
    )
    client.get(
        "/debug/md/cache/stats",
        headers=_api_key_headers(),
    )

    assert _md_state_client(client) is initial_md
    assert _md_state_cache(client) is initial_cache
    # Still only one upstream call across both routes (the second route
    # doesn't talk to the MD service at all).
    assert len(handler.calls) == 1


def test_md_client_post_payload_carries_canonical_id(
    successful_app: tuple[TestClient, _MockMdHandler],
) -> None:
    """Cross-check that the orchestrator drove ``POST /quotes/resolved``.

    Belt-and-braces against an accidental refactor that bypasses the
    MD client's resolve_quotes batch path.
    """

    client, handler = successful_app

    response = client.get(
        f"/debug/md/quote/{QUOTE_CID}",
        params={"as_of": QUOTE_AS_OF},
        headers=_api_key_headers(),
    )

    assert response.status_code == HTTPStatus.OK
    assert handler.calls
    posted = handler.calls[0]
    assert posted.method == "POST"
    assert posted.url.path == "/quotes/resolved"
    body = json.loads(posted.content.decode("utf-8"))
    assert body["canonical_ids"] == [QUOTE_CID]
    assert body["as_of"].startswith(QUOTE_AS_OF)
