"""App construction + middleware + route enumeration tests.

These tests exercise the lifted service end-to-end at the request layer
without touching a database: the engine dependency is stubbed by
``conftest.app`` and any route that would hit the DB is not invoked here.

Behavior we lock in:

- ``/health`` returns 200 with the legacy payload shape (no DB required).
- The read-only middleware rejects PUT/PATCH/DELETE with 403, blocks
  unlisted POSTs with 403, and 404s the deprecated ``/md/*`` prefix.
- The ``X-Request-Id`` middleware echoes inbound IDs and assigns one
  when the caller omits it.
- The OpenAPI surface keeps every remaining route, so clients see no
  break.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

_EXPECTED_ROUTES_AND_METHODS: set[tuple[str, str]] = {
    ("GET", "/health"),
    ("GET", "/catalog/series"),
    ("GET", "/series/{canonical_id}"),
    ("POST", "/quotes/resolved"),
}


def test_health_returns_legacy_shape(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "market-data-api"
    assert "utc_time" in body
    assert isinstance(body["utc_time"], str)


def test_request_id_is_assigned_when_absent(client: TestClient) -> None:
    response = client.get("/health")
    assert "x-request-id" in {k.lower() for k in response.headers}


def test_request_id_is_echoed_when_provided(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-Id": "abc-123"})
    assert response.headers.get("x-request-id") == "abc-123"


def test_read_only_middleware_blocks_put(client: TestClient) -> None:
    response = client.put("/snapshots", json={})
    assert response.status_code == 403
    assert "read-only" in response.json()["detail"].lower()


def test_read_only_middleware_blocks_patch(client: TestClient) -> None:
    response = client.patch("/quotes/latest", json={})
    assert response.status_code == 403


def test_read_only_middleware_blocks_delete(client: TestClient) -> None:
    response = client.delete("/snapshots/abc")
    assert response.status_code == 403


def test_read_only_middleware_blocks_unlisted_post(client: TestClient) -> None:
    response = client.post("/snapshots", json={})
    assert response.status_code == 403
    assert "post is disabled" in response.json()["detail"].lower()


def test_md_prefix_returns_legacy_404(client: TestClient) -> None:
    response = client.get("/md/quotes/latest")
    assert response.status_code == 404
    assert "deprecated path prefix" in response.json()["detail"].lower()


def test_md_root_returns_legacy_404(client: TestClient) -> None:
    response = client.get("/md")
    assert response.status_code == 404


def test_allowed_post_paths_are_not_blocked_by_middleware(client: TestClient) -> None:
    """The compute-only POSTs are allowed past the middleware.

    They take an empty list of canonical_ids and short-circuit before any
    DB hit, so we get a real 200 back even with the stubbed engine.
    """

    response = client.post(
        "/quotes/resolved",
        json={"canonical_ids": [], "as_of": "2026-01-01T00:00:00Z"},
    )
    assert response.status_code == 200
    assert response.json() == {"items": []}


def test_app_exposes_every_expected_route(app: FastAPI) -> None:
    seen: set[tuple[str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if not path:
            continue
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            seen.add((method, path))
    missing = _EXPECTED_ROUTES_AND_METHODS - seen
    assert not missing, f"App dropped expected routes: {sorted(missing)}"


def test_openapi_includes_public_and_internal_tags(app: FastAPI) -> None:
    spec = app.openapi()
    tags = {t["name"] for t in spec.get("tags", [])}
    assert {"public", "internal"}.issubset(tags)


def test_openapi_title_matches_legacy(app: FastAPI) -> None:
    spec = app.openapi()
    assert spec["info"]["title"] == "Quantra Market Data API"
