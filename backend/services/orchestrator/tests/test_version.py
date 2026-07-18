"""``GET /v1/version`` — orchestrator + pricing-engine versions.

Covers:

* auth is required (like the other ``/v1`` routes) and a valid API key
  returns 200 with the documented shape;
* the three reported fields (``orchestrator.version`` / ``.build_sha``,
  ``engine.version``, ``engine.source``);
* ``engine.source == "hardcoded"`` (the F3 placeholder flag);
* ``orchestrator.version`` is the SAME source of truth that ``create_app``
  hands to FastAPI — asserted against the shared ``ORCHESTRATOR_VERSION``
  constant and the app's OpenAPI ``info.version``, not a second literal.
"""

from __future__ import annotations

from http import HTTPStatus

from fastapi import FastAPI
from fastapi.testclient import TestClient

from quantra_common.auth.context import ApiKeyRecord
from quantra_orchestrator.api.version import (
    ENGINE_VERSION,
    ORCHESTRATOR_VERSION,
)


def test_version_requires_auth(client: TestClient) -> None:
    response = client.get("/v1/version")

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json()["code"] == "unauthenticated"


def test_version_returns_documented_shape(
    client: TestClient,
    fake_api_keys: dict[str, ApiKeyRecord],
) -> None:
    fake_api_keys["good-key"] = ApiKeyRecord(
        api_key_id="key-ver",
        owner_uid="uid-ver",
        name="Version Test",
        email="ver@example.com",
        tier="free",
        active=True,
    )

    response = client.get("/v1/version", headers={"X-API-Key": "good-key"})

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    # build_sha comes from the test settings (conftest -> "testsha").
    assert body == {
        "orchestrator": {
            "version": ORCHESTRATOR_VERSION,
            "build_sha": "testsha",
        },
        "engine": {
            "version": ENGINE_VERSION,
            "source": "hardcoded",
        },
    }


def test_engine_version_is_flagged_hardcoded(
    client: TestClient,
    fake_api_keys: dict[str, ApiKeyRecord],
) -> None:
    fake_api_keys["good-key"] = ApiKeyRecord(
        api_key_id="key-ver",
        owner_uid="uid-ver",
        name="Version Test",
        email="ver@example.com",
        tier="free",
        active=True,
    )

    response = client.get("/v1/version", headers={"X-API-Key": "good-key"})

    assert response.status_code == HTTPStatus.OK
    # The placeholder flag: consumers can tell the engine version is a
    # stopgap constant, not sourced from the engine (backlog F3).
    assert response.json()["engine"]["source"] == "hardcoded"


def test_orchestrator_version_matches_app_factory(
    app: FastAPI,
    client: TestClient,
    fake_api_keys: dict[str, ApiKeyRecord],
) -> None:
    """The reported version must be the SAME source of truth ``create_app``
    hands to FastAPI — one constant, no drift."""

    fake_api_keys["good-key"] = ApiKeyRecord(
        api_key_id="key-ver",
        owner_uid="uid-ver",
        name="Version Test",
        email="ver@example.com",
        tier="free",
        active=True,
    )

    response = client.get("/v1/version", headers={"X-API-Key": "good-key"})

    assert response.status_code == HTTPStatus.OK
    reported = response.json()["orchestrator"]["version"]
    # Same constant the factory uses...
    assert reported == ORCHESTRATOR_VERSION
    # ...and the very value FastAPI exposes as OpenAPI info.version.
    assert reported == app.version
    assert app.openapi()["info"]["version"] == ORCHESTRATOR_VERSION
