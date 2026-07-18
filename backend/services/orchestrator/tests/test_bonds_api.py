"""End-to-end route tests for ``POST /v1/price/bonds/{fixed,floating}``.

Both routes are exercised through the FastAPI ``TestClient`` so
auth + assemble + MD resolve + the concurrency seam + engine
call + error envelope light up together. Three classes of dependency
are stubbed:

* ``MdClient`` — :class:`_FakeMdClient` overrides
  ``resolve_quotes``.
* ``EngineClient`` — :class:`_FakeEngineClient` records every call
  and returns or raises whatever the test installed. The default
  ``StubEngineClient`` raises ``NotImplementedError``; tests that
  need a happy-path engine response use the fake.
* ``app_ro`` — :class:`FakeEngine` from ``conftest`` stands in for
  the real ``AsyncEngine`` so assembler queries are deterministic.

Coverage focuses on the 08-plan deliverables that aren't already
pinned by the unit-test suites:

1. Validation (request-shape, mutually-exclusive branches).
2. Storage-unavailable (no ``app_ro`` engine → 503).
3. Not-found surfaces (per-product 404s).
4. Engine-side failure with assembled-request echoed in
   ``details`` — including the floating bond's projection
   curve + index in the echo.
5. Success path: engine returns a result, the route echoes the
   assembled request + result in the response body.
6. Both routes show up in the generated OpenAPI spec.
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
from quantra_common.engine_client import EngineClient, EngineRpc, EngineRpcError
from quantra_common.engine_client._generated.quantra.FixedRateBondResponse import (
    FixedRateBondResponseT,
)
from quantra_common.engine_client._generated.quantra.FloatingRateBondResponse import (
    FloatingRateBondResponseT,
)
from quantra_common.engine_client._generated.quantra.PriceFixedRateBondResponse import (
    PriceFixedRateBondResponseT,
)
from quantra_common.engine_client._generated.quantra.PriceFloatingRateBondResponse import (
    PriceFloatingRateBondResponseT,
)
from quantra_common.md_client import MdClient, MdClientConfig
from quantra_common.settings import Environment, LogLevel
from quantra_common.types.market_data import ResolvedQuote
from quantra_orchestrator.app import create_app
from quantra_orchestrator.settings import OrchestratorSettings

from .conftest import FakeEngine

OWNER = "user-bonds"
API_KEY = "key-bonds"


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


def _fb_fixed_response(npv: float = 0.0, clean_price: float = 0.0) -> bytes:
    """Synthesise a ``PriceFixedRateBondResponse`` flatbuffer with one bond."""

    response = PriceFixedRateBondResponseT()
    response.bonds = []
    b = FixedRateBondResponseT()
    b.npv = npv
    b.cleanPrice = clean_price
    b.dirtyPrice = clean_price
    b.accruedAmount = 0.0
    b.yield_ = 0.04
    b.accruedDays = 0.0
    b.macaulayDuration = 3.7
    b.modifiedDuration = 3.5
    b.convexity = 17.6
    b.bps = 0.0
    response.bonds.append(b)
    builder = flatbuffers.Builder(256)
    builder.Finish(response.Pack(builder))
    return bytes(builder.Output())


def _fb_floating_response(npv: float = 0.0) -> bytes:
    """Synthesise a ``PriceFloatingRateBondResponse`` flatbuffer with one bond."""

    response = PriceFloatingRateBondResponseT()
    response.bonds = []
    b = FloatingRateBondResponseT()
    b.npv = npv
    b.cleanPrice = npv
    b.dirtyPrice = npv
    b.accruedAmount = 0.0
    b.yield_ = 0.03
    b.accruedDays = 0.0
    b.macaulayDuration = 4.7
    b.modifiedDuration = 4.6
    b.convexity = 26.6
    b.bps = 0.0
    response.bonds.append(b)
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
            api_key_id="ak-bonds",
            owner_uid=OWNER,
            name="Bonds Test",
            email="bonds@example.com",
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
    """App with the default ``StubEngineClient`` (raises NotImplementedError)."""

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
    """Factory that builds the orchestrator with caller-supplied md/engine."""

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
# Row helpers
# ---------------------------------------------------------------------------


def _headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


def _curve_row(
    *,
    curve_id: uuid.UUID,
    name: str = "USD-OIS",
    quote_id: str = "USD.IRS.1Y",
) -> dict[str, Any]:
    return {
        "id": str(curve_id),
        "name": name,
        "currency": "USD",
        "day_counter": "Actual360",
        "helper_kind": "Discount",
        "reference_date": date(2026, 5, 13),
        # Translatable wrapped DepositHelper point so the faithful translator
        # consumes it — quote-substituted server-side.
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


# ---------------------------------------------------------------------------
# Auth + storage (shared across both routes)
# ---------------------------------------------------------------------------


def test_fixed_requires_auth(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, _, _ = stub_engine_app
    response = client.post(
        "/v1/price/bonds/fixed",
        json={
            "bond": {
                "face_amount": 100.0,
                "coupon_rate": 0.05,
                "issue_date": "2024-01-15",
                "effective_date": "2024-01-15",
                "termination_date": "2029-01-15",
            },
            "curves": [],
            "as_of": "2026-05-13",
        },
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED


def test_floating_requires_auth(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, _, _ = stub_engine_app
    response = client.post(
        "/v1/price/bonds/floating",
        json={
            "bond": {
                "face_amount": 100.0,
                "coupon_rate": 0.05,
                "issue_date": "2024-01-15",
                "effective_date": "2024-01-15",
                "termination_date": "2029-01-15",
            },
            "curves": [],
            "as_of": "2026-05-13",
        },
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED


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
            "/v1/price/bonds/fixed",
            json={"bond_id": str(uuid.uuid4()), "as_of": "2026-05-13"},
            headers=_headers(),
        )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["code"] == "storage_unavailable"


# ---------------------------------------------------------------------------
# Request-shape validation
# ---------------------------------------------------------------------------


def test_fixed_without_bond_id_or_bond_is_validation_error(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, _, _ = stub_engine_app
    response = client.post(
        "/v1/price/bonds/fixed",
        json={"as_of": "2026-05-13"},
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "validation_error"


def test_floating_inline_without_index_is_validation_error(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    """Inline floating bond requires an explicit ``index`` override.

    There's no saved-bond ``index_id`` to fall back to, so the
    request shape must include one. Caught by the pydantic
    model_validator on :class:`FloatingBondPriceRequest`.
    """

    client, _, _ = stub_engine_app
    response = client.post(
        "/v1/price/bonds/floating",
        json={
            "bond": {
                "face_amount": 100.0,
                "coupon_rate": 0.05,
                "issue_date": "2024-01-15",
                "effective_date": "2024-01-15",
                "termination_date": "2029-01-15",
            },
            "curves": [{"name": "x", "points": [{"quote_id": "y"}]}],
            "as_of": "2026-05-13",
        },
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


# ---------------------------------------------------------------------------
# 404 surfaces
# ---------------------------------------------------------------------------


def test_fixed_bond_id_not_found_returns_bond_fixed_not_found(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, ro_engine, _ = stub_engine_app
    ro_engine.set_handler(lambda _sql, _params: [])

    response = client.post(
        "/v1/price/bonds/fixed",
        json={"bond_id": str(uuid.uuid4()), "as_of": "2026-05-13"},
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "bond_fixed_not_found"


def test_floating_bond_id_not_found_returns_bond_floating_not_found(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, ro_engine, _ = stub_engine_app
    ro_engine.set_handler(lambda _sql, _params: [])

    response = client.post(
        "/v1/price/bonds/floating",
        json={"bond_id": str(uuid.uuid4()), "as_of": "2026-05-13"},
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "bond_floating_not_found"


def test_fixed_discount_curve_not_found_returns_404(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, ro_engine, _ = stub_engine_app
    ro_engine.set_handler(lambda _sql, _params: [])
    response = client.post(
        "/v1/price/bonds/fixed",
        json={
            "bond": {
                "face_amount": 100.0,
                "coupon_rate": 0.05,
                "issue_date": "2024-01-15",
                "effective_date": "2024-01-15",
                "termination_date": "2029-01-15",
            },
            "curves": [{"id": str(uuid.uuid4())}],
            "as_of": "2026-05-13",
        },
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "bond_discount_curve_not_found"


def test_floating_snapshot_id_not_found_returns_bond_snapshot_not_found(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, ro_engine, _ = stub_engine_app
    discount_id = uuid.uuid4()
    projection_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            cid = params["curve_id"]
            if cid == str(discount_id):
                return [_curve_row(curve_id=discount_id)]
            if cid == str(projection_id):
                return [
                    _curve_row(
                        curve_id=projection_id,
                        name="USD-SOFR-3M",
                        quote_id="USD.IRS.3M",
                    )
                ]
            return []
        if "FROM app.snapshots" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    ro_engine.set_handler(handler)

    response = client.post(
        "/v1/price/bonds/floating",
        json={
            "bond": {
                "face_amount": 100.0,
                "coupon_rate": 0.05,
                "issue_date": "2024-01-15",
                "effective_date": "2024-01-15",
                "termination_date": "2029-01-15",
            },
            "curves": [
                {"id": str(discount_id), "body": {"role": "discount"}},
                {"id": str(projection_id), "body": {"role": "projection"}},
            ],
            "index": {
                "kind": "OvernightIndex",
                "body": {"fixingDays": 2},
                "name": "inline",
            },
            "as_of": "2026-05-13",
            "snapshot_id": str(snapshot_id),
        },
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "bond_snapshot_not_found"


# ---------------------------------------------------------------------------
# Engine surface — details echo
# ---------------------------------------------------------------------------


def test_fixed_stub_engine_returns_engine_unavailable_with_assembled_details(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    """Today's stub engine surfaces as 502 + ``engine_unavailable``.

    The envelope's ``details`` must echo the assembled fixed-
    bond request so the operator can verify the orchestrator
    completed every preceding stage before the stub raised.
    """

    client, ro_engine, md = stub_engine_app
    curve_id = uuid.uuid4()
    ro_engine.set_handler(lambda _sql, _params: [_curve_row(curve_id=curve_id)])
    md._results = [_resolved_quote(canonical_id="USD.IRS.1Y", value=4.25)]

    response = client.post(
        "/v1/price/bonds/fixed",
        json={
            "bond": {
                "face_amount": 100.0,
                "coupon_rate": 0.05,
                "issue_date": "2024-01-15",
                "effective_date": "2024-01-15",
                "termination_date": "2029-01-15",
            },
            "curves": [{"id": str(curve_id)}],
            "as_of": "2026-05-13",
        },
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.BAD_GATEWAY
    body = response.json()
    assert body["code"] == "engine_unavailable"
    assert isinstance(body["details"], list)
    assembled = body["details"][0]["assembled_request"]
    assert assembled["as_of"] == "2026-05-13"
    assert assembled["trade"]["bond"] == {
        "face_amount": 100.0,
        "coupon_rate": 0.05,
        "issue_date": "2024-01-15",
        "effective_date": "2024-01-15",
        "termination_date": "2029-01-15",
    }
    assert assembled["discount_curve"]["id"] == str(curve_id)
    assert len(assembled["resolved_quotes"]) == 1
    assert assembled["resolved_quotes"][0]["canonical_id"] == "USD.IRS.1Y"
    assert assembled["resolved_quotes"][0]["value"] == 4.25
    assert assembled["resolved_quotes"][0]["from_snapshot"] is False


def test_floating_stub_engine_echoes_projection_and_index(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    """Floating-bond assembled echo must carry projection_curve + index."""

    client, ro_engine, md = stub_engine_app
    discount_id = uuid.uuid4()
    projection_id = uuid.uuid4()

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            cid = params["curve_id"]
            if cid == str(discount_id):
                return [_curve_row(curve_id=discount_id)]
            if cid == str(projection_id):
                return [
                    _curve_row(
                        curve_id=projection_id,
                        name="USD-SOFR-3M",
                        quote_id="USD.IRS.3M",
                    )
                ]
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    ro_engine.set_handler(handler)
    md._results = [
        _resolved_quote(canonical_id="USD.IRS.1Y", value=4.25),
        _resolved_quote(canonical_id="USD.IRS.3M", value=4.35),
    ]

    response = client.post(
        "/v1/price/bonds/floating",
        json={
            "bond": {
                "face_amount": 100.0,
                "coupon_rate": 0.05,
                "issue_date": "2024-01-15",
                "effective_date": "2024-01-15",
                "termination_date": "2029-01-15",
            },
            "curves": [
                {"id": str(discount_id), "body": {"role": "discount"}},
                {"id": str(projection_id), "body": {"role": "projection"}},
            ],
            "index": {
                "kind": "OvernightIndex",
                "body": {"fixingDays": 2},
                "name": "inline",
            },
            "as_of": "2026-05-13",
        },
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.BAD_GATEWAY
    body = response.json()
    assert body["code"] == "engine_unavailable"
    assembled = body["details"][0]["assembled_request"]
    assert assembled["discount_curve"]["id"] == str(discount_id)
    assert assembled["projection_curve"]["id"] == str(projection_id)
    assert assembled["index"]["kind"] == "OvernightIndex"
    canonical_ids = [q["canonical_id"] for q in assembled["resolved_quotes"]]
    assert canonical_ids == ["USD.IRS.1Y", "USD.IRS.3M"]


# ---------------------------------------------------------------------------
# Success path + batching hooks
# ---------------------------------------------------------------------------


def test_fixed_success_path_returns_assembled_request_and_result(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    curve_id = uuid.uuid4()
    fake_ro_engine.set_handler(lambda _sql, _params: [_curve_row(curve_id=curve_id)])
    md = _FakeMdClient(results=[_resolved_quote(canonical_id="USD.IRS.1Y", value=4.25)])
    engine = _FakeEngineClient(response=_fb_fixed_response(npv=999.0, clean_price=99.5))
    client = custom_app_factory(md_client=md, engine_client=engine)

    response = client.post(
        "/v1/price/bonds/fixed",
        json={
            "bond": {
                "face_amount": 100.0,
                "coupon_rate": 0.05,
                "issue_date": "2024-01-15",
                "effective_date": "2024-01-15",
                "termination_date": "2029-01-15",
            },
            "curves": [{"id": str(curve_id)}],
            "as_of": "2026-05-13",
        },
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["pricing_history_id"] is None
    assert body["result"]["npv"] == 999.0
    assert body["result"]["clean_price"] == 99.5
    assert len(engine.calls) == 1
    rpc, _ = engine.calls[0]
    assert rpc is EngineRpc.PRICE_FIXED_RATE_BOND


def test_floating_success_path_returns_assembled_request_and_result(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    discount_id = uuid.uuid4()
    projection_id = uuid.uuid4()

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            cid = params["curve_id"]
            if cid == str(discount_id):
                return [_curve_row(curve_id=discount_id)]
            if cid == str(projection_id):
                return [
                    _curve_row(
                        curve_id=projection_id,
                        name="USD-SOFR-3M",
                        quote_id="USD.IRS.3M",
                    )
                ]
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="USD.IRS.1Y", value=4.25),
            _resolved_quote(canonical_id="USD.IRS.3M", value=4.35),
        ]
    )
    engine = _FakeEngineClient(response=_fb_floating_response(npv=1500.0))
    client = custom_app_factory(md_client=md, engine_client=engine)

    response = client.post(
        "/v1/price/bonds/floating",
        json={
            "bond": {
                "face_amount": 100.0,
                "coupon_rate": 0.05,
                "issue_date": "2024-01-15",
                "effective_date": "2024-01-15",
                "termination_date": "2029-01-15",
            },
            "curves": [
                {"id": str(discount_id), "body": {"role": "discount"}},
                {"id": str(projection_id), "body": {"role": "projection"}},
            ],
            "index": {
                "kind": "OvernightIndex",
                "body": {"fixingDays": 2},
                "name": "inline",
            },
            "as_of": "2026-05-13",
        },
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["result"]["npv"] == 1500.0
    assert len(engine.calls) == 1
    rpc, _ = engine.calls[0]
    assert rpc is EngineRpc.PRICE_FLOATING_RATE_BOND


def test_openapi_schema_advertises_both_bond_routes() -> None:
    """Both bond routes show up in the generated OpenAPI spec.

    Catches a wiring regression where the new router falls off
    ``register_all`` — the routes would still be importable but
    unreachable through the public app.
    """

    cfg = OrchestratorSettings(env=Environment.DEV, log_level=LogLevel.WARNING)
    app = create_app(cfg)
    schema = app.openapi()
    assert "/v1/price/bonds/fixed" in schema["paths"]
    assert "/v1/price/bonds/floating" in schema["paths"]


# ---------------------------------------------------------------------------
# pricing_history hook (fixed + floating)
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


def test_fixed_success_records_pricing_history_row(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    curve_id = uuid.uuid4()
    fake_ro_engine.set_handler(lambda _sql, _params: [_curve_row(curve_id=curve_id)])
    md = _FakeMdClient(results=[_resolved_quote(canonical_id="USD.IRS.1Y", value=4.25)])
    engine = _FakeEngineClient(response=_fb_fixed_response(npv=111.0, clean_price=99.0))

    inserted_id = uuid.uuid4()
    captured_inserts: list[tuple[str, dict[str, Any]]] = []
    rw_engine = FakeEngine()
    rw_engine.set_handler(_pricing_history_handler(inserted_id, captured_inserts))

    client = custom_app_factory(md_client=md, engine_client=engine, rw_engine=rw_engine)
    response = client.post(
        "/v1/price/bonds/fixed",
        json={
            "bond": {
                "face_amount": 100.0,
                "coupon_rate": 0.05,
                "issue_date": "2024-01-15",
                "effective_date": "2024-01-15",
                "termination_date": "2029-01-15",
            },
            "curves": [{"id": str(curve_id)}],
            "as_of": "2026-05-13",
        },
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
    assert params["product_kind"] == "bonds_fixed"
    assert params["product_id"] is None
    persisted_response = json.loads(params["response"])
    assert persisted_response["npv"] == 111.0


def test_fixed_engine_failure_records_pricing_history_failure_row(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    curve_id = uuid.uuid4()
    fake_ro_engine.set_handler(lambda _sql, _params: [_curve_row(curve_id=curve_id)])
    md = _FakeMdClient(results=[_resolved_quote(canonical_id="USD.IRS.1Y", value=4.25)])

    captured_inserts: list[tuple[str, dict[str, Any]]] = []
    rw_engine = FakeEngine()
    rw_engine.set_handler(_pricing_history_handler(uuid.uuid4(), captured_inserts))
    client = custom_app_factory(md_client=md, rw_engine=rw_engine)

    response = client.post(
        "/v1/price/bonds/fixed",
        json={
            "bond": {
                "face_amount": 100.0,
                "coupon_rate": 0.05,
                "issue_date": "2024-01-15",
                "effective_date": "2024-01-15",
                "termination_date": "2029-01-15",
            },
            "curves": [{"id": str(curve_id)}],
            "as_of": "2026-05-13",
        },
        headers=_headers(),
    )

    assert response.status_code == HTTPStatus.BAD_GATEWAY
    assert response.json()["code"] == "engine_unavailable"

    assert len(captured_inserts) == 1
    _, params = captured_inserts[0]
    assert params["product_kind"] == "bonds_fixed"
    persisted_response = json.loads(params["response"])
    assert persisted_response["error"]["code"] == "engine_unavailable"


def test_fixed_pricing_history_write_failure_still_returns_pricing_response(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    curve_id = uuid.uuid4()
    fake_ro_engine.set_handler(lambda _sql, _params: [_curve_row(curve_id=curve_id)])
    md = _FakeMdClient(results=[_resolved_quote(canonical_id="USD.IRS.1Y", value=4.25)])
    engine = _FakeEngineClient(response=_fb_fixed_response(npv=55.0, clean_price=98.0))

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
        "/v1/price/bonds/fixed",
        json={
            "bond": {
                "face_amount": 100.0,
                "coupon_rate": 0.05,
                "issue_date": "2024-01-15",
                "effective_date": "2024-01-15",
                "termination_date": "2029-01-15",
            },
            "curves": [{"id": str(curve_id)}],
            "as_of": "2026-05-13",
        },
        headers=_headers(),
    )

    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["pricing_history_id"] is None
    assert body["result"]["npv"] == 55.0
    assert len(captured_inserts) == 1


def test_floating_success_records_pricing_history_row(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    discount_id = uuid.uuid4()
    projection_id = uuid.uuid4()

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            cid = params["curve_id"]
            if cid == str(discount_id):
                return [_curve_row(curve_id=discount_id)]
            if cid == str(projection_id):
                return [
                    _curve_row(
                        curve_id=projection_id,
                        name="USD-SOFR-3M",
                        quote_id="USD.IRS.3M",
                    )
                ]
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="USD.IRS.1Y", value=4.25),
            _resolved_quote(canonical_id="USD.IRS.3M", value=4.35),
        ]
    )
    engine = _FakeEngineClient(response=_fb_floating_response(npv=2000.0))

    inserted_id = uuid.uuid4()
    captured_inserts: list[tuple[str, dict[str, Any]]] = []
    rw_engine = FakeEngine()
    rw_engine.set_handler(_pricing_history_handler(inserted_id, captured_inserts))

    client = custom_app_factory(md_client=md, engine_client=engine, rw_engine=rw_engine)
    response = client.post(
        "/v1/price/bonds/floating",
        json={
            "bond": {
                "face_amount": 100.0,
                "coupon_rate": 0.05,
                "issue_date": "2024-01-15",
                "effective_date": "2024-01-15",
                "termination_date": "2029-01-15",
            },
            "curves": [
                {"id": str(discount_id), "body": {"role": "discount"}},
                {"id": str(projection_id), "body": {"role": "projection"}},
            ],
            "index": {
                "kind": "OvernightIndex",
                "body": {"fixingDays": 2},
                "name": "inline",
            },
            "as_of": "2026-05-13",
        },
        headers=_headers(),
    )

    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["pricing_history_id"] == str(inserted_id)
    uuid.UUID(body["pricing_history_id"])

    assert len(captured_inserts) == 1
    _, params = captured_inserts[0]
    assert params["owner_uid"] == OWNER
    assert params["product_kind"] == "bonds_floating"
    assert params["product_id"] is None
    persisted_response = json.loads(params["response"])
    assert persisted_response["npv"] == 2000.0


def test_floating_engine_failure_records_pricing_history_failure_row(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    discount_id = uuid.uuid4()
    projection_id = uuid.uuid4()

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            cid = params["curve_id"]
            if cid == str(discount_id):
                return [_curve_row(curve_id=discount_id)]
            if cid == str(projection_id):
                return [
                    _curve_row(
                        curve_id=projection_id,
                        name="USD-SOFR-3M",
                        quote_id="USD.IRS.3M",
                    )
                ]
            return []
        return []

    fake_ro_engine.set_handler(handler)
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="USD.IRS.1Y", value=4.25),
            _resolved_quote(canonical_id="USD.IRS.3M", value=4.35),
        ]
    )

    captured_inserts: list[tuple[str, dict[str, Any]]] = []
    rw_engine = FakeEngine()
    rw_engine.set_handler(_pricing_history_handler(uuid.uuid4(), captured_inserts))
    client = custom_app_factory(md_client=md, rw_engine=rw_engine)

    response = client.post(
        "/v1/price/bonds/floating",
        json={
            "bond": {
                "face_amount": 100.0,
                "coupon_rate": 0.05,
                "issue_date": "2024-01-15",
                "effective_date": "2024-01-15",
                "termination_date": "2029-01-15",
            },
            "curves": [
                {"id": str(discount_id), "body": {"role": "discount"}},
                {"id": str(projection_id), "body": {"role": "projection"}},
            ],
            "index": {
                "kind": "OvernightIndex",
                "body": {"fixingDays": 2},
                "name": "inline",
            },
            "as_of": "2026-05-13",
        },
        headers=_headers(),
    )

    assert response.status_code == HTTPStatus.BAD_GATEWAY
    assert response.json()["code"] == "engine_unavailable"

    assert len(captured_inserts) == 1
    _, params = captured_inserts[0]
    assert params["product_kind"] == "bonds_floating"
    persisted_response = json.loads(params["response"])
    assert persisted_response["error"]["code"] == "engine_unavailable"


def test_floating_pricing_history_write_failure_still_returns_pricing_response(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    discount_id = uuid.uuid4()
    projection_id = uuid.uuid4()

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            cid = params["curve_id"]
            if cid == str(discount_id):
                return [_curve_row(curve_id=discount_id)]
            if cid == str(projection_id):
                return [
                    _curve_row(
                        curve_id=projection_id,
                        name="USD-SOFR-3M",
                        quote_id="USD.IRS.3M",
                    )
                ]
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="USD.IRS.1Y", value=4.25),
            _resolved_quote(canonical_id="USD.IRS.3M", value=4.35),
        ]
    )
    engine = _FakeEngineClient(response=_fb_floating_response(npv=3000.0))

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
        "/v1/price/bonds/floating",
        json={
            "bond": {
                "face_amount": 100.0,
                "coupon_rate": 0.05,
                "issue_date": "2024-01-15",
                "effective_date": "2024-01-15",
                "termination_date": "2029-01-15",
            },
            "curves": [
                {"id": str(discount_id), "body": {"role": "discount"}},
                {"id": str(projection_id), "body": {"role": "projection"}},
            ],
            "index": {
                "kind": "OvernightIndex",
                "body": {"fixingDays": 2},
                "name": "inline",
            },
            "as_of": "2026-05-13",
        },
        headers=_headers(),
    )

    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["pricing_history_id"] is None
    assert body["result"]["npv"] == 3000.0
    assert len(captured_inserts) == 1


# ---------------------------------------------------------------------------
# Date-coherence translation (cross-source As-Of mismatch)
# ---------------------------------------------------------------------------


def test_fixed_negative_time_abort_maps_to_typed_422_naming_the_dates(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    """The engine's ``negative time`` abort → 422 ``pricing_as_of_before_curve_date``.

    The live defect: the app's default As-Of (BoE latest, T-1) can predate
    the USD Treasury curve's auto-rolled reference_date (T+0); the bond's
    settlement/accrual math then aborts inside QuantLib and used to surface
    as an opaque 502 ``engine_upstream_error``. TRANSLATION ONLY — the
    request still goes to the engine (many shapes price fine with
    ``as_of < reference_date``); when the engine does abort, the mapped 422
    must name the as-of, the curve + its reference date, and what to do.
    """

    curve_id = uuid.uuid4()
    fake_ro_engine.set_handler(lambda _sql, _params: [_curve_row(curve_id=curve_id)])
    md = _FakeMdClient(results=[_resolved_quote(canonical_id="USD.IRS.1Y", value=4.25)])
    engine = _FakeEngineClient(
        raises=EngineRpcError(
            "engine RPC failed: ABORTED (negative time (-0.0027397260273972603) given)",
            code="ABORTED",
            details="negative time (-0.0027397260273972603) given",
        )
    )
    client = custom_app_factory(md_client=md, engine_client=engine)

    response = client.post(
        "/v1/price/bonds/fixed",
        json={
            "bond": {
                "face_amount": 100.0,
                "coupon_rate": 0.05,
                "issue_date": "2024-01-15",
                "effective_date": "2024-01-15",
                "termination_date": "2029-01-15",
            },
            "curves": [{"id": str(curve_id)}],
            # One day BEFORE the stored curve's reference_date (2026-05-13).
            "as_of": "2026-05-12",
        },
        headers=_headers(),
    )

    # The request DID reach the engine (no pre-flight rejection).
    assert len(engine.calls) == 1
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, response.text
    body = response.json()
    assert body["code"] == "pricing_as_of_before_curve_date"
    # Actionable message: names both dates, the curve, and the action.
    assert "2026-05-12" in body["error"]
    assert "predates the reference date of curve 'USD-OIS'" in body["error"]
    assert "2026-05-13" in body["error"]
    assert "re-select" in body["error"].lower()
    # Structured details: the date context leads; the raw engine text and the
    # assembled-request echo ride behind it.
    entry = body["details"][0]
    assert entry["as_of"] == "2026-05-12"
    assert entry["curve"] == "USD-OIS"
    assert entry["curve_id"] == str(curve_id)
    assert entry["reference_date"] == "2026-05-13"
    assert "re-select" in entry["guidance"].lower()
    assert any("engine_detail" in d for d in body["details"])
    assert any("assembled_request" in d for d in body["details"])
