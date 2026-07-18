"""End-to-end router tests for the data layer.

Drives the FastAPI ``TestClient`` so the auth dependency + owner_uid
extraction + repository SQL + error envelope all light up together. The
``FakeEngine`` from conftest stands in for the real ``app_rw`` /
``app_ro`` AsyncEngines — tests assert both the wire shape of the
response and the SQL the underlying repository emitted.

Coverage focuses on the cross-cutting behaviour shared across all
fourteen named entities (using ``curves`` as the canonical sample
because its two JSONB columns exercise the trickier code paths) plus
the immutable ``pricing_history`` view.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from http import HTTPStatus
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from quantra_common.auth.context import ApiKeyRecord
from quantra_common.auth.firebase import FirebaseTokenVerifier
from quantra_common.auth.lookup import ApiKeyLookup
from quantra_common.settings import Environment, LogLevel
from quantra_orchestrator.app import create_app
from quantra_orchestrator.settings import OrchestratorSettings

# ---------------------------------------------------------------------------
# Per-test app wired with FakeEngine + api-key auth
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> OrchestratorSettings:
    return OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="testsha",
    )


@pytest.fixture
def api_key_a() -> str:
    return "key-a"


@pytest.fixture
def api_key_b() -> str:
    return "key-b"


@pytest.fixture
def owner_a() -> str:
    return "user-A"


@pytest.fixture
def owner_b() -> str:
    return "user-B"


@pytest.fixture
def api_keys(
    api_key_a: str,
    api_key_b: str,
    owner_a: str,
    owner_b: str,
) -> dict[str, ApiKeyRecord]:
    return {
        api_key_a: ApiKeyRecord(
            api_key_id="ak-a",
            owner_uid=owner_a,
            name="A",
            email="a@example.com",
            tier="free",
            active=True,
        ),
        api_key_b: ApiKeyRecord(
            api_key_id="ak-b",
            owner_uid=owner_b,
            name="B",
            email="b@example.com",
            tier="free",
            active=True,
        ),
    }


@pytest.fixture
def auth_lookup(api_keys: dict[str, ApiKeyRecord]) -> ApiKeyLookup:
    async def _lookup(key: str) -> ApiKeyRecord | None:
        return api_keys.get(key)

    return _lookup


@pytest.fixture
def firebase_verifier() -> FirebaseTokenVerifier:
    def _verify(_token: str) -> dict[str, Any]:
        msg = "no firebase in this suite"
        raise ValueError(msg)

    return _verify


@pytest.fixture
def app(
    settings: OrchestratorSettings,
    auth_lookup: ApiKeyLookup,
    firebase_verifier: FirebaseTokenVerifier,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> Iterator[FastAPI]:
    instance = create_app(
        settings,
        api_key_lookup=auth_lookup,
        firebase_verifier=firebase_verifier,
        app_rw_engine=fake_rw_engine,
        app_ro_engine=fake_ro_engine,
    )
    try:
        yield instance
    finally:
        instance.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _curve_row(
    *,
    name: str = "EUR-OIS",
    deleted: bool = False,
    resource_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    return {
        "id": str(resource_id or uuid.uuid4()),
        "name": name,
        "currency": "EUR",
        "day_counter": "Actual/360",
        "helper_kind": "Discount",
        "reference_date": date(2026, 1, 15),
        "points": [],
        "body": {"interpolator": "Cubic"},
        "created_at": datetime(2026, 5, 15, 9, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 5, 15, 9, 0, tzinfo=UTC),
        "deleted_at": (datetime(2026, 5, 15, 10, 0, tzinfo=UTC) if deleted else None),
    }


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


def test_v1_curves_requires_auth(client: TestClient) -> None:
    response = client.get("/v1/curves")
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json()["code"] == "unauthenticated"


def test_v1_curves_responds_503_without_engine_configured(
    auth_lookup: ApiKeyLookup,
    firebase_verifier: FirebaseTokenVerifier,
    api_key_a: str,
) -> None:
    # Force settings without DSNs so the lifespan's "build an engine if
    # DSN is set" branch can't fire. Outside CI the local .env may
    # populate these — this fixture overrides explicitly to keep the
    # 503 path exercised regardless of the developer's environment.
    settings_no_dsn = OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="testsha",
        postgres_dsn_app_rw=None,
        postgres_dsn_app_ro=None,
    )
    app = create_app(
        settings_no_dsn,
        api_key_lookup=auth_lookup,
        firebase_verifier=firebase_verifier,
    )
    with TestClient(app, raise_server_exceptions=False) as tc:
        response = tc.get("/v1/curves", headers={"X-API-Key": api_key_a})
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["code"] == "storage_unavailable"


# ---------------------------------------------------------------------------
# Create / list / read happy path on /v1/curves
# ---------------------------------------------------------------------------


def test_curves_create_201_and_scopes_owner_uid(
    client: TestClient,
    api_key_a: str,
    owner_a: str,
    fake_rw_engine: Any,
) -> None:
    curve = _curve_row(name="EUR-OIS")
    fake_rw_engine.set_handler(lambda _sql, _params: [curve])

    payload = {
        "name": "EUR-OIS",
        "currency": "EUR",
        "day_counter": "Actual/360",
        "helper_kind": "Discount",
        "reference_date": "2026-01-15",
        "points": [{"tenor": "1Y", "rate": 0.02}],
        "body": {"interpolator": "Cubic"},
    }
    response = client.post("/v1/curves", json=payload, headers={"X-API-Key": api_key_a})

    assert response.status_code == HTTPStatus.CREATED
    body = response.json()
    assert body["name"] == "EUR-OIS"
    rec = fake_rw_engine.recordings[0]
    assert rec.mode == "write"
    assert "INSERT INTO app.curves" in rec.sql
    assert rec.params["owner_uid"] == owner_a


def test_curves_list_returns_page_meta(
    client: TestClient,
    api_key_a: str,
    fake_ro_engine: Any,
) -> None:
    rows = [_curve_row(name=f"curve-{i}") for i in range(3)]
    fake_ro_engine.set_handler(lambda _sql, _params: rows)

    response = client.get("/v1/curves", params={"limit": 50}, headers={"X-API-Key": api_key_a})
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert len(body["items"]) == 3
    assert body["page"] == {"limit": 50, "offset": 0, "has_more": False}


def test_curves_get_404_when_not_found(
    client: TestClient,
    api_key_a: str,
    fake_ro_engine: Any,
) -> None:
    fake_ro_engine.set_handler(lambda _sql, _params: [])
    response = client.get(f"/v1/curves/{uuid.uuid4()}", headers={"X-API-Key": api_key_a})
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "not_found"


def test_curves_get_404_uses_d54_envelope_for_soft_deleted_row(
    client: TestClient,
    api_key_a: str,
    fake_ro_engine: Any,
) -> None:
    # The repository's read query filters ``deleted_at IS NULL`` server-side,
    # so a soft-deleted row presents as "no rows" to the test handler.
    fake_ro_engine.set_handler(lambda _sql, _params: [])

    response = client.get(f"/v1/curves/{uuid.uuid4()}", headers={"X-API-Key": api_key_a})
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "not_found"

    rec = fake_ro_engine.recordings[0]
    assert "t.deleted_at IS NULL" in rec.sql


# ---------------------------------------------------------------------------
# Patch / delete / restore
# ---------------------------------------------------------------------------


def test_curves_patch_excludes_unset(
    client: TestClient,
    api_key_a: str,
    fake_rw_engine: Any,
) -> None:
    target_id = uuid.uuid4()
    fake_rw_engine.set_handler(
        lambda _sql, _params: [_curve_row(resource_id=target_id, name="renamed")]
    )

    response = client.patch(
        f"/v1/curves/{target_id}",
        json={"name": "renamed"},
        headers={"X-API-Key": api_key_a},
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json()["name"] == "renamed"
    rec = fake_rw_engine.recordings[0]
    # Only the ``name`` SET fragment shows up — other fields stay absent
    # because ``exclude_unset=True``.
    assert "name = :name" in rec.sql
    assert "currency = :currency" not in rec.sql


def test_curves_delete_returns_204(
    client: TestClient,
    api_key_a: str,
    fake_rw_engine: Any,
) -> None:
    fake_rw_engine.set_handler(lambda _sql, _params: [{"id": str(uuid.uuid4())}])

    response = client.delete(f"/v1/curves/{uuid.uuid4()}", headers={"X-API-Key": api_key_a})
    assert response.status_code == HTTPStatus.NO_CONTENT


def test_curves_restore_route_exists(
    client: TestClient,
    api_key_a: str,
    fake_rw_engine: Any,
    fake_ro_engine: Any,
) -> None:
    target_id = uuid.uuid4()
    fake_ro_engine.set_handler(
        lambda _sql, _params: [_curve_row(resource_id=target_id, name="EUR-OIS", deleted=True)]
    )
    fake_rw_engine.set_handler(
        lambda _sql, _params: [_curve_row(resource_id=target_id, name="EUR-OIS")]
    )

    response = client.post(f"/v1/curves/{target_id}:restore", headers={"X-API-Key": api_key_a})
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["deleted_at"] is None
    assert body["name"] == "EUR-OIS"


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


def test_cross_tenant_read_returns_404(
    client: TestClient,
    api_key_a: str,
    api_key_b: str,
    owner_a: str,
    owner_b: str,
    fake_ro_engine: Any,
) -> None:
    target_id = uuid.uuid4()
    # The fake engine only returns rows when params["owner_uid"] matches
    # owner_a — mimicking what the WHERE clause does against a real DB.
    owner_a_row = _curve_row(resource_id=target_id, name="EUR-OIS")

    def handler(_sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if params.get("owner_uid") == owner_a:
            return [owner_a_row]
        return []

    fake_ro_engine.set_handler(handler)

    response_a = client.get(f"/v1/curves/{target_id}", headers={"X-API-Key": api_key_a})
    assert response_a.status_code == HTTPStatus.OK
    assert response_a.json()["id"] == str(target_id)

    response_b = client.get(f"/v1/curves/{target_id}", headers={"X-API-Key": api_key_b})
    assert response_b.status_code == HTTPStatus.NOT_FOUND
    body = response_b.json()
    assert body["code"] == "not_found"
    # Verify the WHERE clause carried owner_uid=owner_b on user B's call.
    rec_b = fake_ro_engine.recordings[-1]
    assert rec_b.params["owner_uid"] == owner_b


# ---------------------------------------------------------------------------
# Product entities reuse the same template via ProductCreate
# ---------------------------------------------------------------------------


def test_swaps_ir_post_carries_owner_uid(
    client: TestClient,
    api_key_a: str,
    owner_a: str,
    fake_rw_engine: Any,
) -> None:
    swap_row = {
        "id": str(uuid.uuid4()),
        "name": "USD 5Y Vanilla",
        "request": {"fixed_rate": 0.04, "notional": 1_000_000},
        "created_at": datetime(2026, 5, 15, 9, tzinfo=UTC),
        "updated_at": datetime(2026, 5, 15, 9, tzinfo=UTC),
        "deleted_at": None,
    }
    fake_rw_engine.set_handler(lambda _sql, _params: [swap_row])

    response = client.post(
        "/v1/swaps/ir",
        json={"name": "USD 5Y Vanilla", "request": {"fixed_rate": 0.04, "notional": 1_000_000}},
        headers={"X-API-Key": api_key_a},
    )

    assert response.status_code == HTTPStatus.CREATED
    rec = fake_rw_engine.recordings[0]
    assert "INSERT INTO app.swaps_ir" in rec.sql
    assert rec.params["owner_uid"] == owner_a


# ---------------------------------------------------------------------------
# POST /auth/provision (users-row upsert)
# ---------------------------------------------------------------------------


def test_provision_requires_auth(client: TestClient) -> None:
    response = client.post("/auth/provision")
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json()["code"] == "unauthenticated"


def test_provision_upserts_the_callers_users_row(
    client: TestClient,
    api_key_a: str,
    owner_a: str,
    fake_rw_engine: Any,
) -> None:
    row = {
        "uid": owner_a,
        "email": "a@example.com",
        "display_name": "A",
        "tier": "free",
    }
    fake_rw_engine.set_handler(lambda _sql, _params: [row])

    response = client.post("/auth/provision", headers={"X-API-Key": api_key_a})

    assert response.status_code == HTTPStatus.OK
    assert response.json() == row
    rec = fake_rw_engine.recordings[0]
    assert "INSERT INTO app.users" in rec.sql
    assert "ON CONFLICT (uid) DO UPDATE" in rec.sql
    # tier is account management's business, never provisioning's.
    assert "tier" not in rec.sql.split("RETURNING")[0]
    assert rec.params["uid"] == owner_a
    assert rec.mode == "write"


# ---------------------------------------------------------------------------
# pricing_history read-only
# ---------------------------------------------------------------------------


def test_pricing_history_post_is_method_not_allowed(
    client: TestClient,
    api_key_a: str,
) -> None:
    response = client.post(
        "/v1/pricing-history",
        json={"product_kind": "swaps_ir"},
        headers={"X-API-Key": api_key_a},
    )
    assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED


def test_pricing_history_list_is_scoped_by_owner_uid(
    client: TestClient,
    api_key_a: str,
    owner_a: str,
    fake_ro_engine: Any,
) -> None:
    fake_ro_engine.set_handler(lambda _sql, _params: [])
    response = client.get("/v1/pricing-history", headers={"X-API-Key": api_key_a})
    assert response.status_code == HTTPStatus.OK
    rec = fake_ro_engine.recordings[0]
    assert "FROM app.pricing_history" in rec.sql
    assert rec.params["owner_uid"] == owner_a


# ---------------------------------------------------------------------------
# All entity prefixes are mounted under /v1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prefix",
    [
        "/v1/indices",
        "/v1/curves",
        "/v1/curve-sets",
        "/v1/credit-curves",
        "/v1/snapshots",
        "/v1/vol-surfaces",
        "/v1/swaption-models",
        "/v1/swaps/ir",
        "/v1/swaps/inflation",
        "/v1/swaptions",
        "/v1/bonds/fixed",
        "/v1/bonds/floating",
        "/v1/cds",
        "/v1/equity-options",
        "/v1/pricing-history",
    ],
)
def test_each_entity_list_route_exists_and_authenticates(
    prefix: str,
    client: TestClient,
    api_key_a: str,
    fake_ro_engine: Any,
) -> None:
    fake_ro_engine.set_handler(lambda _sql, _params: [])
    response = client.get(prefix, headers={"X-API-Key": api_key_a})
    # The route is mounted and the auth passes; the empty list is the
    # successful payload from the empty handler above.
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["items"] == []
    assert body["page"]["offset"] == 0
