"""Auth wiring tests for the orchestrator.

Covers deliverables that don't require a
real database:

* ``GET /auth/whoami`` returns 401 with the envelope
  (``code="unauthenticated"``) when no credential is supplied.
* Invalid bearer / invalid X-API-Key produce the same 401 envelope; the
  failure reason never leaks into the response.
* Valid Firebase token returns ``auth_method="firebase"`` with the
  expected ``uid`` / ``email``.
* Valid X-API-Key returns ``auth_method="api_key"`` with the resolved
  user metadata.
* Inactive ``ApiKeyRecord`` collapses to the unknown-key 401 (spec).
* ``/health`` remains anonymous.
* ``SqlApiKeyLookup`` hash helper produces the expected SHA-256 hex.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from fastapi.testclient import TestClient

from quantra_common.auth.context import ApiKeyRecord
from quantra_common.settings import Environment, LogLevel
from quantra_orchestrator.app import create_app
from quantra_orchestrator.auth.api_keys import hash_api_key
from quantra_orchestrator.settings import OrchestratorSettings


def test_health_remains_anonymous(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == HTTPStatus.OK
    assert response.json()["status"] == "ok"


def test_whoami_without_credentials_returns_unauthenticated_envelope(
    client: TestClient,
) -> None:
    response = client.get("/auth/whoami", headers={"X-Request-Id": "rid-noauth"})

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    body = response.json()
    assert body == {
        "error": "Authentication required.",
        "code": "unauthenticated",
        "request_id": "rid-noauth",
    }
    assert response.headers.get("x-request-id") == "rid-noauth"


def test_whoami_with_invalid_bearer_returns_unauthenticated_envelope(
    client: TestClient,
) -> None:
    response = client.get(
        "/auth/whoami",
        headers={
            "Authorization": "Bearer not-a-real-jwt",
            "X-Request-Id": "rid-badjwt",
        },
    )

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    body = response.json()
    assert body["code"] == "unauthenticated"
    assert body["error"] == "Invalid or expired bearer token."
    assert body["request_id"] == "rid-badjwt"


def test_whoami_with_unknown_api_key_returns_unauthenticated_envelope(
    client: TestClient,
) -> None:
    response = client.get(
        "/auth/whoami",
        headers={"X-API-Key": "totally-unknown"},
    )

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    body = response.json()
    assert body["code"] == "unauthenticated"
    assert body["error"] == "Invalid or inactive API key."


def test_whoami_with_inactive_api_key_returns_unauthenticated_envelope(
    client: TestClient,
    fake_api_keys: dict[str, ApiKeyRecord],
) -> None:
    fake_api_keys["disabled-key"] = ApiKeyRecord(
        api_key_id="key-1",
        owner_uid="uid-disabled",
        name="Disabled Key",
        email="disabled@example.com",
        tier="free",
        active=False,
    )

    response = client.get(
        "/auth/whoami",
        headers={"X-API-Key": "disabled-key"},
    )

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json()["code"] == "unauthenticated"


def test_whoami_with_valid_api_key_returns_200(
    client: TestClient,
    fake_api_keys: dict[str, ApiKeyRecord],
) -> None:
    fake_api_keys["good-key"] = ApiKeyRecord(
        api_key_id="key-2",
        owner_uid="uid-good",
        name="CLI Key",
        email="good@example.com",
        tier="pro",
        active=True,
    )

    response = client.get(
        "/auth/whoami",
        headers={"X-API-Key": "good-key"},
    )

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body == {
        "uid": "uid-good",
        "email": "good@example.com",
        "auth_method": "api_key",
    }


def test_whoami_with_valid_bearer_returns_200(
    client: TestClient,
    fake_firebase_verifier: tuple[object, dict[str, dict[str, Any]]],
) -> None:
    _, claims_by_token = fake_firebase_verifier
    claims_by_token["valid-token"] = {
        "uid": "fb-user-1",
        "email": "fb@example.com",
        "name": "Firebase User",
    }

    response = client.get(
        "/auth/whoami",
        headers={"Authorization": "Bearer valid-token"},
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "uid": "fb-user-1",
        "email": "fb@example.com",
        "auth_method": "firebase",
    }


def test_whoami_prefers_bearer_when_both_headers_present(
    client: TestClient,
    fake_api_keys: dict[str, ApiKeyRecord],
    fake_firebase_verifier: tuple[object, dict[str, dict[str, Any]]],
) -> None:
    _, claims_by_token = fake_firebase_verifier
    claims_by_token["valid-token"] = {"uid": "fb-user-2", "email": "fb2@example.com"}
    fake_api_keys["good-key"] = ApiKeyRecord(
        api_key_id="key-3",
        owner_uid="uid-other",
        name="Excel Key",
        email="excel@example.com",
        tier="free",
        active=True,
    )

    response = client.get(
        "/auth/whoami",
        headers={
            "Authorization": "Bearer valid-token",
            "X-API-Key": "good-key",
        },
    )

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["uid"] == "fb-user-2"
    assert body["auth_method"] == "firebase"


def test_whoami_unauthenticated_response_carries_www_authenticate_header(
    client: TestClient,
) -> None:
    response = client.get("/auth/whoami")
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.headers.get("www-authenticate") == "Bearer"


def test_dev_auth_bypass_short_circuits_to_dev_principal() -> None:
    """With ``dev_auth_bypass=True`` an unauthenticated call resolves.

    The fixed dev principal (uid='dev-user', email='dev@quantra.local')
    is returned for a request carrying NO credentials at all. Settings
    are constructed explicitly so the test is hermetic regardless of any
    local ``.env`` value.
    """

    settings = OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        dev_auth_bypass=True,
    )
    instance = create_app(settings)
    try:
        response = TestClient(instance).get("/auth/whoami")
    finally:
        instance.dependency_overrides.clear()

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "uid": "dev-user",
        "email": "dev@quantra.local",
        "auth_method": "firebase",
    }


def test_dev_auth_bypass_defaults_off_and_still_401s() -> None:
    """The bypass is off by default: no credential still yields 401.

    Explicit ``dev_auth_bypass=False`` proves the flag — not some other
    wiring — gates the short-circuit, and that the unchanged path keeps
    rejecting anonymous callers.
    """

    settings = OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        dev_auth_bypass=False,
    )
    instance = create_app(settings)
    try:
        response = TestClient(instance).get("/auth/whoami")
    finally:
        instance.dependency_overrides.clear()

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json()["code"] == "unauthenticated"


def test_hash_api_key_is_lowercase_hex_sha256() -> None:
    digest = hash_api_key("hello")
    # Hex SHA-256 of "hello" — pinned so a future migration can verify
    # the digest format stays compatible with app.api_keys.key_hash.
    assert digest == ("2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")


def test_hash_api_key_is_stable_across_calls() -> None:
    assert hash_api_key("a very long secret api key value") == hash_api_key(
        "a very long secret api key value"
    )
