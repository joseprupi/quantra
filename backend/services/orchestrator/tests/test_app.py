"""App construction + middleware + error-handler tests for the orchestrator scaffold.

Locks in the four acceptance bullets:

- ``GET /health`` returns 200 with the four documented fields.
- ``/docs`` and ``/openapi.json`` are exposed.
- The request-ID middleware echoes inbound IDs and assigns one when absent.
- Global exception handlers convert validation / HTTPException / unhandled
  errors into the structured JSON envelope ``{error, code, request_id}``.
"""

from __future__ import annotations

from http import HTTPStatus

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from quantra_orchestrator.api.version import ORCHESTRATOR_VERSION


def test_health_returns_documented_fields(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body == {
        "status": "ok",
        "service": "quantra-orchestrator",
        "build_sha": "testsha",
        "env": "dev",
    }


def test_health_response_carries_request_id_header(client: TestClient) -> None:
    response = client.get("/health")
    assert "x-request-id" in {k.lower() for k in response.headers}


def test_request_id_is_echoed_when_provided(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-Id": "fixed-1234"})
    assert response.headers.get("x-request-id") == "fixed-1234"


def test_request_id_is_assigned_when_absent(client: TestClient) -> None:
    response = client.get("/health")
    assigned = response.headers.get("x-request-id")
    assert assigned is not None
    assert len(assigned) >= 16


def test_docs_endpoint_is_exposed(client: TestClient) -> None:
    response = client.get("/docs")
    assert response.status_code == HTTPStatus.OK
    assert "text/html" in response.headers.get("content-type", "")


def test_openapi_metadata_matches_factory(app: FastAPI) -> None:
    spec = app.openapi()
    assert spec["info"]["title"] == "Quantra API"
    assert spec["info"]["version"] == ORCHESTRATOR_VERSION
    tags = {t["name"] for t in spec.get("tags", [])}
    assert "meta" in tags


def test_validation_error_returns_structured_envelope(app: FastAPI) -> None:
    @app.get("/__test__/needs-int")
    def _needs_int(value: int) -> dict[str, int]:
        return {"value": value}

    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/__test__/needs-int", params={"value": "notanint"})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    body = response.json()
    assert body["error"] == "Request validation failed."
    assert body["code"] == "validation_error"
    assert isinstance(body["request_id"], str)
    assert body["request_id"]
    details = body.get("details")
    assert isinstance(details, list)
    assert details


def test_http_exception_returns_structured_envelope(app: FastAPI) -> None:
    @app.get("/__test__/forbidden")
    def _forbidden() -> None:
        raise HTTPException(status_code=403, detail="nope")

    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get(
            "/__test__/forbidden",
            headers={"X-Request-Id": "rid-403"},
        )

    assert response.status_code == HTTPStatus.FORBIDDEN
    body = response.json()
    assert body == {
        "error": "nope",
        "code": "http_403",
        "request_id": "rid-403",
    }


def test_unhandled_exception_returns_structured_500(app: FastAPI) -> None:
    @app.get("/__test__/boom")
    def _boom() -> None:
        raise RuntimeError("boom")

    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get(
            "/__test__/boom",
            headers={"X-Request-Id": "rid-500"},
        )

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    body = response.json()
    assert body == {
        "error": "Internal server error.",
        "code": "internal_error",
        "request_id": "rid-500",
    }
    assert "<html" not in response.text.lower()


def test_404_uses_structured_envelope(client: TestClient) -> None:
    response = client.get("/__definitely_not_a_route__")
    assert response.status_code == HTTPStatus.NOT_FOUND
    body = response.json()
    assert body["code"] == "http_404"
    assert "error" in body
    assert "request_id" in body
