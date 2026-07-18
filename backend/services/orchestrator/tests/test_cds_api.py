"""End-to-end route tests for ``POST /v1/price/cds``.

Drives the FastAPI ``TestClient`` so auth + assemble + MD resolve +
the concurrency seam + engine call + error envelope all light up
together. Three classes of dependency are stubbed:

* ``MdClient`` — :class:`_FakeMdClient` overrides the single method
  the walker uses (``resolve_quotes``).
* ``EngineClient`` — :class:`_FakeEngineClient` records every call
  and returns or raises whatever the test installed. The default
  ``StubEngineClient`` raises ``NotImplementedError`` on every call;
  tests that need a happy-path engine response use the fake.
* ``app_ro`` — :class:`FakeEngine` from ``conftest`` stands in for
  the real ``AsyncEngine`` so assembler queries are deterministic.

The auth gate / request-id propagation envelope are already
covered by the data-layer and engine-client suites; this file
focuses on the end-to-end vertical the 09 plan delivers:

1. Validation (request shape, mutually exclusive branches).
2. Storage-unavailable (no ``app_ro`` engine → 503).
3. Not-found surfaces (``cds_id`` / discount curve / credit curve /
   ``snapshot_id``).
4. Per-bundle-stage 422s  for the two distinct
   entity families: ``cds_curve_resolution_failed`` (rates) vs
   ``cds_credit_curve_resolution_failed`` (credit).
5. Quote-resolution failure (per-item ``found=False`` rolls into
   ``cds_quote_resolution_failed``).
6. Engine-side failure with assembled-request echoed in ``details``
   (the most-important "did the resolution path complete?"
   verification on today's stub engine).
7. Success path: fake engine returns one ``CdsResult`` and the
   route echoes the assembled request + result in the response body.
8. ``shared_inputs["as_of"]`` (the one key the CDS wire builder
   reads) threads through the concurrency seam.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import date, datetime
from http import HTTPStatus
from typing import Any, cast

import flatbuffers
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_common.auth.context import ApiKeyRecord
from quantra_common.auth.firebase import FirebaseTokenVerifier
from quantra_common.auth.lookup import ApiKeyLookup
from quantra_common.engine_client import EngineClient, EngineRpc
from quantra_common.engine_client._generated.quantra.CDSValues import (
    CDSValuesT,
)
from quantra_common.engine_client._generated.quantra.PriceCDSResponse import (
    PriceCDSResponseT,
)
from quantra_common.md_client import (
    MdClient,
    MdClientConfig,
    MdTransportError,
)
from quantra_common.settings import Environment, LogLevel
from quantra_common.types.market_data import ResolvedQuote
from quantra_orchestrator.app import create_app
from quantra_orchestrator.pricing.cds import api as cds_api
from quantra_orchestrator.pricing.cds.engine_io import (
    price_cds_batch as _real_cds_price_batch,
)
from quantra_orchestrator.settings import OrchestratorSettings

from .conftest import FakeEngine

OWNER = "user-test"
API_KEY = "key-cds"


# ---------------------------------------------------------------------------
# Fake clients
# ---------------------------------------------------------------------------


class _FakeMdClient(MdClient):
    """``MdClient`` whose ``resolve_quotes`` returns a canned list or raises."""

    def __init__(
        self,
        *,
        results: list[ResolvedQuote] | None = None,
        raises: Exception | None = None,
    ) -> None:
        config = MdClientConfig(base_url="http://stub", timeout_s=1.0, max_retries=0)
        super().__init__(
            config,
            client=httpx.AsyncClient(base_url="http://stub"),
        )
        self.calls: list[tuple[list[str], Any]] = []
        self._results = results
        self._raises = raises

    async def resolve_quotes(
        self,
        canonical_ids: list[str],
        as_of: Any,
        *,
        snapshot_version: str | None = None,
    ) -> list[ResolvedQuote]:
        self.calls.append((list(canonical_ids), as_of))
        if self._raises is not None:
            raise self._raises
        return list(self._results or [])


class _FakeEngineClient(EngineClient):
    """``EngineClient`` returning a canned bytes response or raising."""

    def __init__(
        self,
        *,
        response: bytes | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[EngineRpc, bytes]] = []
        self._response = response
        self._raises = raises

    async def call(self, rpc: EngineRpc, request_bytes: bytes) -> bytes:
        self.calls.append((rpc, request_bytes))
        if self._raises is not None:
            raise self._raises
        if self._response is None:
            msg = "_FakeEngineClient: no response configured"
            raise AssertionError(msg)
        return self._response

    async def close(self) -> None:
        return None


def _fb_response_bytes(
    npv: float = 123_456.78,
    fair_spread: float = 0.0125,
    fair_upfront: float = 0.012,
    protection_leg_npv: float = 50_000.0,
    premium_leg_npv: float = -50_000.0,
) -> bytes:
    """Synthesize a valid ``PriceCDSResponse`` flatbuffer.

    Used by tests that need a happy-path engine reply without
    standing up the live engine. The response carries one CDS
    result with the supplied diagnostics; the orchestrator's
    decoder projects ``default_leg_npv`` onto
    ``protection_leg_npv`` per ``cds_response.fbs``.
    """

    response = PriceCDSResponseT()
    response.cdsList = []
    cds_response = CDSValuesT()
    cds_response.npv = npv
    cds_response.fairSpread = fair_spread
    cds_response.fairUpfront = fair_upfront
    cds_response.defaultLegNpv = protection_leg_npv
    cds_response.premiumLegNpv = premium_leg_npv
    response.cdsList.append(cds_response)
    builder = flatbuffers.Builder(256)
    builder.Finish(response.Pack(builder))
    return bytes(builder.Output())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> OrchestratorSettings:
    return OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="testsha",
    )


@pytest.fixture
def api_keys() -> dict[str, ApiKeyRecord]:
    return {
        API_KEY: ApiKeyRecord(
            api_key_id="ak-cds",
            owner_uid=OWNER,
            name="CDS Test",
            email="cds@example.com",
            tier="free",
            active=True,
        )
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


def _build_app(
    *,
    settings: OrchestratorSettings,
    auth_lookup: ApiKeyLookup,
    firebase_verifier: FirebaseTokenVerifier,
    fake_ro_engine: FakeEngine,
    md_client: MdClient | None,
    engine_client: EngineClient | None,
    fake_rw_engine: FakeEngine | None = None,
) -> FastAPI:
    return create_app(
        settings,
        api_key_lookup=auth_lookup,
        firebase_verifier=firebase_verifier,
        app_ro_engine=cast(AsyncEngine, fake_ro_engine),
        app_rw_engine=(cast(AsyncEngine, fake_rw_engine) if fake_rw_engine is not None else None),
        md_client=md_client,
        engine_client=engine_client,
    )


@pytest.fixture
def stub_engine_app(
    settings: OrchestratorSettings,
    auth_lookup: ApiKeyLookup,
    firebase_verifier: FirebaseTokenVerifier,
    fake_ro_engine: FakeEngine,
) -> Iterator[tuple[TestClient, FakeEngine, _FakeMdClient]]:
    md_client = _FakeMdClient(results=[])
    app = _build_app(
        settings=settings,
        auth_lookup=auth_lookup,
        firebase_verifier=firebase_verifier,
        fake_ro_engine=fake_ro_engine,
        md_client=md_client,
        engine_client=None,
    )
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client, fake_ro_engine, md_client


@pytest.fixture
def custom_app_factory(
    settings: OrchestratorSettings,
    auth_lookup: ApiKeyLookup,
    firebase_verifier: FirebaseTokenVerifier,
    fake_ro_engine: FakeEngine,
) -> Iterator[Any]:
    opened: list[TestClient] = []

    def _build(
        *,
        md_client: MdClient | None = None,
        engine_client: EngineClient | None = None,
        rw_engine: FakeEngine | None = None,
    ) -> TestClient:
        client = md_client if md_client is not None else _FakeMdClient(results=[])
        app = _build_app(
            settings=settings,
            auth_lookup=auth_lookup,
            firebase_verifier=firebase_verifier,
            fake_ro_engine=fake_ro_engine,
            md_client=client,
            engine_client=engine_client,
            fake_rw_engine=rw_engine,
        )
        test_client = TestClient(app, raise_server_exceptions=False)
        test_client.__enter__()
        opened.append(test_client)
        return test_client

    try:
        yield _build
    finally:
        for tc in opened:
            tc.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Row builders / helpers
# ---------------------------------------------------------------------------


def _headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


def _curve_row(
    *, curve_id: uuid.UUID, name: str = "USD-OIS", quote_id: str = "USD.IRS.1Y"
) -> dict[str, Any]:
    return {
        "id": str(curve_id),
        "name": name,
        "currency": "USD",
        "day_counter": "Actual/360",
        "helper_kind": "Discount",
        "reference_date": date(2026, 5, 13),
        # Translatable DepositHelper point (faithful path): the
        # orchestrator now consumes the curve points + resolves the quote id
        # rather than discarding them for a canonical fixture.
        "points": [
            {
                "point_type": "DepositHelper",
                "point": {
                    "tenor": {"n": 1, "unit": "Years"},
                    "fixing_days": 2,
                    "calendar": "TARGET",
                    "business_day_convention": "ModifiedFollowing",
                    "day_counter": "Actual365Fixed",
                    "quote_id": quote_id,
                },
            }
        ],
        "body": {"interpolator": "LogLinear"},
    }


def _credit_curve_row(
    *,
    credit_curve_id: uuid.UUID,
    name: str = "ACME-SR",
    source: str = "flat",
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": str(credit_curve_id),
        "name": name,
        "reference_entity": "ACME",
        "currency": "USD",
        "seniority": "Senior Unsecured",
        "source": source,
        "recovery_rate": 0.4,
        "body": body if body is not None else {"flat_hazard_rate": 0.02},
    }


def _cds_row(
    *, cds_id: uuid.UUID, request_json: dict[str, Any], name: str = "demo-cds"
) -> dict[str, Any]:
    return {"id": str(cds_id), "name": name, "request": request_json}


def _resolved_quote(*, canonical_id: str, value: float, found: bool = True) -> ResolvedQuote:
    return ResolvedQuote(
        canonical_id=canonical_id,
        requested_as_of=datetime(2026, 5, 13, 0, 0),
        resolved_as_of=datetime(2026, 5, 13, 0, 0),
        found=found,
        is_exact=True,
        value=value if found else None,
        source="vendor_x",
        vendor_id="vendor",
    )


def _inline_payload(
    *,
    curve_id: uuid.UUID | None = None,
    credit_curve_id: uuid.UUID | None = None,
    inline_credit_curve: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a fully inline ``POST /v1/price/cds`` payload."""

    payload: dict[str, Any] = {
        # Explicit economics — the wire layer no longer supplies silent
        # defaults (422 ``cds_missing_trade_fields`` otherwise).
        "cds": {
            "notional": 10_000_000.0,
            "running_coupon": 0.01,
            "schedule": {
                "effective_date": "2025-01-15",
                "termination_date": "2030-01-15",
            },
        },
        "as_of": "2026-05-13",
    }
    if curve_id is not None:
        payload["curves"] = [{"id": str(curve_id)}]
    if credit_curve_id is not None:
        payload["credit_curve_id"] = str(credit_curve_id)
    if inline_credit_curve is not None:
        payload["credit_curve"] = inline_credit_curve
    return payload


# ---------------------------------------------------------------------------
# Auth + storage
# ---------------------------------------------------------------------------


def test_requires_auth(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, _, _ = stub_engine_app
    response = client.post(
        "/v1/price/cds",
        json={"cds": {}, "as_of": "2026-05-13"},
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json()["code"] == "unauthenticated"


def test_storage_unavailable_when_app_ro_missing(
    auth_lookup: ApiKeyLookup,
    firebase_verifier: FirebaseTokenVerifier,
) -> None:
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
        md_client=_FakeMdClient(results=[]),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/v1/price/cds",
            json={"cds_id": str(uuid.uuid4()), "as_of": "2026-05-13"},
            headers=_headers(),
        )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["code"] == "storage_unavailable"


# ---------------------------------------------------------------------------
# Request-shape validation
# ---------------------------------------------------------------------------


def test_request_without_cds_id_or_cds_is_validation_error(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, _, _ = stub_engine_app
    response = client.post(
        "/v1/price/cds",
        json={"as_of": "2026-05-13"},
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "validation_error"


def test_inline_cds_without_curves_is_validation_error(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, _, _ = stub_engine_app
    response = client.post(
        "/v1/price/cds",
        json={"cds": {"notional": 1.0}, "as_of": "2026-05-13"},
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "validation_error"


def test_inline_cds_with_curves_but_no_credit_curve_is_validation_error(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    """Inline mode requires both a discount-curve override AND a credit-curve override."""

    client, _, _ = stub_engine_app
    response = client.post(
        "/v1/price/cds",
        json={
            "cds": {"notional": 1.0},
            "curves": [{"id": str(uuid.uuid4())}],
            "as_of": "2026-05-13",
        },
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "validation_error"


def test_credit_curve_id_and_inline_are_mutually_exclusive(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, _, _ = stub_engine_app
    response = client.post(
        "/v1/price/cds",
        json={
            "cds": {"notional": 1.0},
            "curves": [{"id": str(uuid.uuid4())}],
            "credit_curve_id": str(uuid.uuid4()),
            "credit_curve": {
                "name": "x",
                "recovery_rate": 0.4,
                "source": "flat",
                "body": {"flat_hazard_rate": 0.02},
            },
            "as_of": "2026-05-13",
        },
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "validation_error"


# ---------------------------------------------------------------------------
# 404 surfaces — distinct per entity family (refinement)
# ---------------------------------------------------------------------------


def test_cds_id_not_found_returns_cds_not_found(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, ro_engine, _ = stub_engine_app
    ro_engine.set_handler(lambda _sql, _params: [])

    response = client.post(
        "/v1/price/cds",
        json={"cds_id": str(uuid.uuid4()), "as_of": "2026-05-13"},
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "cds_not_found"


def test_discount_curve_not_found_returns_cds_discount_curve_not_found(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, ro_engine, _ = stub_engine_app
    curve_id = uuid.uuid4()
    credit_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return []
        if "FROM app.credit_curves" in sql:
            return [_credit_curve_row(credit_curve_id=credit_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    ro_engine.set_handler(handler)

    response = client.post(
        "/v1/price/cds",
        json=_inline_payload(curve_id=curve_id, credit_curve_id=credit_id),
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "cds_discount_curve_not_found"


def test_credit_curve_not_found_returns_cds_credit_curve_not_found(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    """the error-code convention refinement: distinct entity family → distinct 404 code."""

    client, ro_engine, _ = stub_engine_app
    curve_id = uuid.uuid4()
    credit_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.credit_curves" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    ro_engine.set_handler(handler)

    response = client.post(
        "/v1/price/cds",
        json=_inline_payload(curve_id=curve_id, credit_curve_id=credit_id),
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "cds_credit_curve_not_found"


def test_snapshot_id_not_found_returns_cds_snapshot_not_found(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, ro_engine, _ = stub_engine_app
    curve_id = uuid.uuid4()
    credit_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.credit_curves" in sql:
            return [_credit_curve_row(credit_curve_id=credit_id)]
        if "FROM app.snapshots" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    ro_engine.set_handler(handler)

    payload = _inline_payload(curve_id=curve_id, credit_curve_id=credit_id)
    payload["snapshot_id"] = str(snapshot_id)

    response = client.post("/v1/price/cds", json=payload, headers=_headers())
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "cds_snapshot_not_found"


# ---------------------------------------------------------------------------
# 422 surfaces — per-bundle-stage (distinct code per bundle stage)
# ---------------------------------------------------------------------------


def test_missing_pricing_block_returns_cds_curve_resolution_failed(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    """Saved CDS with no ``pricing`` block → rates-side 422."""

    client, ro_engine, _ = stub_engine_app
    cds_id = uuid.uuid4()
    ro_engine.set_handler(
        lambda _sql, _params: [_cds_row(cds_id=cds_id, request_json={"notional": 10_000_000.0})]
    )

    response = client.post(
        "/v1/price/cds",
        json={"cds_id": str(cds_id), "as_of": "2026-05-13"},
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "cds_curve_resolution_failed"


def test_credit_curve_quote_book_returns_cds_credit_curve_resolution_failed(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    """Credit curve with ``source='quote_book'`` → credit-side 422 (distinct code).

    The credit-curve family gets its
    own 422 code rather than reusing the rates one. The orchestrator
    refuses to extend the MD walker silently; the plan calls out
    that scope-expansion is an explicit decision.
    """

    client, ro_engine, _ = stub_engine_app
    curve_id = uuid.uuid4()
    credit_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.credit_curves" in sql:
            return [
                _credit_curve_row(
                    credit_curve_id=credit_id,
                    source="quote_book",
                    body={"points": [{"quote_id": "ACME.5Y", "tenor": "5Y"}]},
                )
            ]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    ro_engine.set_handler(handler)

    response = client.post(
        "/v1/price/cds",
        json=_inline_payload(curve_id=curve_id, credit_curve_id=credit_id),
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    body = response.json()
    assert body["code"] == "cds_credit_curve_resolution_failed"
    assert body["details"] is not None


def test_quote_resolution_failed_when_md_returns_found_false(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    curve_id = uuid.uuid4()
    credit_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.credit_curves" in sql:
            return [_credit_curve_row(credit_curve_id=credit_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)
    md = _FakeMdClient(results=[_resolved_quote(canonical_id="USD.IRS.1Y", value=0.0, found=False)])
    client = custom_app_factory(md_client=md)

    response = client.post(
        "/v1/price/cds",
        json=_inline_payload(curve_id=curve_id, credit_curve_id=credit_id),
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    body = response.json()
    assert body["code"] == "cds_quote_resolution_failed"
    assert body["details"] is not None
    assert any(d.get("canonical_id") == "USD.IRS.1Y" for d in body["details"])


def test_md_unreachable_returns_md_unreachable_envelope(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    curve_id = uuid.uuid4()
    credit_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.credit_curves" in sql:
            return [_credit_curve_row(credit_curve_id=credit_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)
    md = _FakeMdClient(raises=MdTransportError("connection refused"))
    client = custom_app_factory(md_client=md)

    response = client.post(
        "/v1/price/cds",
        json=_inline_payload(curve_id=curve_id, credit_curve_id=credit_id),
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.BAD_GATEWAY
    assert response.json()["code"] == "md_unreachable"


# ---------------------------------------------------------------------------
# Engine surface
# ---------------------------------------------------------------------------


def test_stub_engine_returns_engine_unavailable_with_assembled_details(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    """Today's stub engine surfaces as 502 + ``engine_unavailable``.

    The most-important assertion of the plan: the envelope's
    ``details`` must echo the assembled request so the operator
    can verify the orchestrator completed every preceding stage
    (assemble → MD resolve → fan-out) before the stub raised.
    Also pins that *both* curve families landed in the echo.
    """

    client, ro_engine, md = stub_engine_app
    curve_id = uuid.uuid4()
    credit_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.credit_curves" in sql:
            return [_credit_curve_row(credit_curve_id=credit_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    ro_engine.set_handler(handler)
    md._results = [_resolved_quote(canonical_id="USD.IRS.1Y", value=4.25)]

    response = client.post(
        "/v1/price/cds",
        json=_inline_payload(curve_id=curve_id, credit_curve_id=credit_id),
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.BAD_GATEWAY
    body = response.json()
    assert body["code"] == "engine_unavailable"
    assert isinstance(body["details"], list)
    assert len(body["details"]) >= 1
    assembled = body["details"][0]["assembled_request"]
    assert assembled["as_of"] == "2026-05-13"
    assert assembled["trade"]["cds"] == {
        "notional": 10_000_000.0,
        "running_coupon": 0.01,
        "schedule": {
            "effective_date": "2025-01-15",
            "termination_date": "2030-01-15",
        },
    }
    assert assembled["discount_curve"]["id"] == str(curve_id)
    assert assembled["credit_curve"]["id"] == str(credit_id)
    assert assembled["credit_curve_id"] == str(credit_id)
    assert assembled["discount_curve_id"] == str(curve_id)
    assert len(assembled["resolved_quotes"]) == 1
    assert assembled["resolved_quotes"][0]["canonical_id"] == "USD.IRS.1Y"


def test_success_path_returns_assembled_request_and_result(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    """Happy path: fake engine returns one ``CdsResult``; the route echoes both."""

    curve_id = uuid.uuid4()
    credit_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.credit_curves" in sql:
            return [_credit_curve_row(credit_curve_id=credit_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)
    md = _FakeMdClient(results=[_resolved_quote(canonical_id="USD.IRS.1Y", value=4.25)])
    engine = _FakeEngineClient(
        response=_fb_response_bytes(
            npv=123_456.78,
            fair_spread=0.0125,
            fair_upfront=0.012,
            protection_leg_npv=50_000.0,
            premium_leg_npv=-50_000.0,
        )
    )
    client = custom_app_factory(md_client=md, engine_client=engine)

    response = client.post(
        "/v1/price/cds",
        json=_inline_payload(curve_id=curve_id, credit_curve_id=credit_id),
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["pricing_history_id"] is None
    assert body["result"]["npv"] == 123_456.78
    assert body["result"]["fair_spread"] == pytest.approx(0.0125)
    assert body["result"]["fair_upfront"] == pytest.approx(0.012)
    assert body["result"]["protection_leg_npv"] == 50_000.0
    assert body["result"]["premium_leg_npv"] == -50_000.0
    # The decoder also surfaces the engine's own naming on ``extras``.
    assert body["result"]["extras"]["default_leg_npv"] == 50_000.0
    assert body["assembled_request"]["as_of"] == "2026-05-13"
    assert body["assembled_request"]["discount_curve"]["id"] == str(curve_id)
    assert body["assembled_request"]["credit_curve"]["id"] == str(credit_id)
    # Batching seam: exactly one engine call with ``PRICE_CDS``.
    assert len(engine.calls) == 1
    assert engine.calls[0][0] is EngineRpc.PRICE_CDS


def test_engine_batch_carries_as_of_in_shared_inputs(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``shared_inputs["as_of"]`` reaches ``price_batch``.

    ``as_of`` anchors the CDS protection-leg schedule date inside the
    wire builder, so it must thread through the concurrency seam.
    """

    curve_id = uuid.uuid4()
    credit_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.credit_curves" in sql:
            return [_credit_curve_row(credit_curve_id=credit_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)
    md = _FakeMdClient(results=[_resolved_quote(canonical_id="USD.IRS.1Y", value=4.25)])
    engine = _FakeEngineClient(response=_fb_response_bytes())

    captured_batches: list[Any] = []

    async def _spy(eng: Any, batch: Any, *, resolved: Any) -> Any:
        captured_batches.append(batch)
        return await _real_cds_price_batch(eng, batch, resolved=resolved)

    monkeypatch.setattr(cds_api, "price_cds_batch", _spy)
    client = custom_app_factory(md_client=md, engine_client=engine)

    response = client.post(
        "/v1/price/cds",
        json=_inline_payload(curve_id=curve_id, credit_curve_id=credit_id),
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.OK, response.text
    rpc, _request_bytes = engine.calls[0]
    assert rpc is EngineRpc.PRICE_CDS
    assert len(captured_batches) == 1
    batch = captured_batches[0]
    # ``as_of`` is the one shared input the CDS wire builder reads.
    assert batch.shared_inputs == {"as_of": "2026-05-13"}


def test_snapshot_pinned_quote_marks_from_snapshot_true(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    """Pinned canonical IDs short-circuit the live MD client."""

    curve_id = uuid.uuid4()
    credit_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.credit_curves" in sql:
            return [_credit_curve_row(credit_curve_id=credit_id)]
        if "FROM app.snapshots" in sql:
            return [
                {
                    "id": str(snapshot_id),
                    "name": "EOD-USD",
                    "content": {"USD.IRS.1Y": 4.25},
                }
            ]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)
    md = _FakeMdClient(results=[])
    client = custom_app_factory(md_client=md)

    payload = _inline_payload(curve_id=curve_id, credit_curve_id=credit_id)
    payload["snapshot_id"] = str(snapshot_id)

    response = client.post("/v1/price/cds", json=payload, headers=_headers())
    assert response.status_code == HTTPStatus.BAD_GATEWAY  # stub engine
    body = response.json()
    assembled = body["details"][0]["assembled_request"]
    assert assembled["snapshot_id"] == str(snapshot_id)
    assert assembled["resolved_quotes"][0]["from_snapshot"] is True
    assert assembled["resolved_quotes"][0]["value"] == 4.25
    assert md.calls == []


def test_openapi_schema_advertises_post_route() -> None:
    """``POST /v1/price/cds`` shows up in the generated OpenAPI spec.

    Catches a wiring regression where the new router falls off
    ``register_all`` without anyone noticing (the route would still
    be importable but unreachable through the public app).
    """

    cfg = OrchestratorSettings(env=Environment.DEV, log_level=LogLevel.WARNING)
    app = create_app(cfg)
    schema = app.openapi()
    assert "/v1/price/cds" in schema["paths"]


# ---------------------------------------------------------------------------
# pricing_history hook
# ---------------------------------------------------------------------------


def _pricing_history_handler(
    inserted_id: uuid.UUID,
    captured: list[tuple[str, dict[str, Any]]],
    *,
    raise_on_insert: Exception | None = None,
) -> Any:
    def _handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "INSERT INTO app.pricing_history" in sql:
            captured.append((sql, params))
            if raise_on_insert is not None:
                raise raise_on_insert
            return [{"id": str(inserted_id)}]
        msg = f"unexpected SQL in pricing_history fixture: {sql!r}"
        raise AssertionError(msg)

    return _handler


def test_success_path_records_pricing_history_row(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    curve_id = uuid.uuid4()
    credit_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.credit_curves" in sql:
            return [_credit_curve_row(credit_curve_id=credit_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)
    md = _FakeMdClient(results=[_resolved_quote(canonical_id="USD.IRS.1Y", value=4.25)])
    engine = _FakeEngineClient(response=_fb_response_bytes(npv=99_000.0, fair_spread=0.01))

    inserted_id = uuid.uuid4()
    captured_inserts: list[tuple[str, dict[str, Any]]] = []
    rw_engine = FakeEngine()
    rw_engine.set_handler(_pricing_history_handler(inserted_id, captured_inserts))

    client = custom_app_factory(md_client=md, engine_client=engine, rw_engine=rw_engine)
    response = client.post(
        "/v1/price/cds",
        json=_inline_payload(curve_id=curve_id, credit_curve_id=credit_id),
        headers=_headers(),
    )

    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["pricing_history_id"] == str(inserted_id)
    uuid.UUID(body["pricing_history_id"])

    assert len(captured_inserts) == 1
    sql, params = captured_inserts[0]
    assert "INSERT INTO app.pricing_history" in sql
    assert params["owner_uid"] == OWNER
    assert params["product_kind"] == "cds"
    assert params["product_id"] is None
    persisted_response = json.loads(params["response"])
    assert persisted_response["npv"] == 99_000.0


def test_engine_failure_records_pricing_history_failure_row(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    curve_id = uuid.uuid4()
    credit_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.credit_curves" in sql:
            return [_credit_curve_row(credit_curve_id=credit_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)
    md = _FakeMdClient(results=[_resolved_quote(canonical_id="USD.IRS.1Y", value=4.25)])

    captured_inserts: list[tuple[str, dict[str, Any]]] = []
    rw_engine = FakeEngine()
    rw_engine.set_handler(_pricing_history_handler(uuid.uuid4(), captured_inserts))
    client = custom_app_factory(md_client=md, rw_engine=rw_engine)

    response = client.post(
        "/v1/price/cds",
        json=_inline_payload(curve_id=curve_id, credit_curve_id=credit_id),
        headers=_headers(),
    )

    assert response.status_code == HTTPStatus.BAD_GATEWAY
    assert response.json()["code"] == "engine_unavailable"

    assert len(captured_inserts) == 1
    _, params = captured_inserts[0]
    assert params["product_kind"] == "cds"
    assert params["product_id"] is None
    persisted_response = json.loads(params["response"])
    assert "error" in persisted_response
    assert persisted_response["error"]["code"] == "engine_unavailable"


def test_pricing_history_write_failure_still_returns_pricing_response(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    curve_id = uuid.uuid4()
    credit_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.credit_curves" in sql:
            return [_credit_curve_row(credit_curve_id=credit_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)
    md = _FakeMdClient(results=[_resolved_quote(canonical_id="USD.IRS.1Y", value=4.25)])
    engine = _FakeEngineClient(response=_fb_response_bytes(npv=42.0))

    captured_inserts: list[tuple[str, dict[str, Any]]] = []
    rw_engine = FakeEngine()
    rw_engine.set_handler(
        _pricing_history_handler(
            uuid.uuid4(),
            captured_inserts,
            raise_on_insert=RuntimeError("history db down"),
        )
    )
    client = custom_app_factory(md_client=md, engine_client=engine, rw_engine=rw_engine)

    response = client.post(
        "/v1/price/cds",
        json=_inline_payload(curve_id=curve_id, credit_curve_id=credit_id),
        headers=_headers(),
    )

    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["pricing_history_id"] is None
    assert body["result"]["npv"] == 42.0
    assert len(captured_inserts) == 1
