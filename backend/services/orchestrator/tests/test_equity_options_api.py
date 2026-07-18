"""End-to-end route tests for ``POST /v1/price/equity-option``.

Drives the FastAPI ``TestClient`` so auth + assemble + MD resolve +
the concurrency seam + engine call + error envelope all light up
together.

Three classes of dependency are stubbed:

* ``MdClient`` — :class:`_FakeMdClient` overrides
  ``resolve_quotes``.
* ``EngineClient`` — :class:`_FakeEngineClient` records calls and
  returns canned bytes.
* ``app_ro`` — :class:`FakeEngine` from ``conftest`` stands in for
  the real ``AsyncEngine``.

Coverage focuses on the end-to-end vertical the 10-plan delivers:

1. Validation (request shape, mutually exclusive branches).
2. Storage-unavailable (no ``app_ro`` engine → 503).
3. Not-found surfaces (``equity_option_id`` / discount /
   ``vol_surface_id`` / ``snapshot_id``).
4. Per-bundle-stage 422s.
5. Quote-resolution failure (per-item ``found=False`` rolls into
   ``equity_option_quote_resolution_failed``).
6. Engine-side failure with assembled-request echoed in ``details``
   (the most-important "did the resolution path complete?"
   verification on today's stub engine).
7. Success path: fake engine returns one ``EquityOptionResult``
   and the route echoes the assembled request + result.
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
from quantra_common.engine_client._generated.quantra.enums.EquitySettlementType import (
    EquitySettlementType,
)
from quantra_common.engine_client._generated.quantra.EquityOptionResponse import (
    EquityOptionResponseT,
)
from quantra_common.engine_client._generated.quantra.PriceEquityOptionRequest import (
    PriceEquityOptionRequestT,
)
from quantra_common.engine_client._generated.quantra.PriceEquityOptionResponse import (
    PriceEquityOptionResponseT,
)
from quantra_common.md_client import (
    MdClient,
    MdClientConfig,
    MdTransportError,
)
from quantra_common.settings import Environment, LogLevel
from quantra_common.types.market_data import ResolvedQuote
from quantra_orchestrator.app import create_app
from quantra_orchestrator.settings import OrchestratorSettings

from .conftest import FakeEngine

OWNER = "user-test"
API_KEY = "key-eq"


# ---------------------------------------------------------------------------
# Fake clients
# ---------------------------------------------------------------------------


class _FakeMdClient(MdClient):
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
    npv: float = 12_345.67,
    delta: float = 0.55,
    gamma: float = 0.04,
    vega: float = 12.5,
    theta: float = -8.0,
    rho: float = 4.5,
    implied_volatility: float = 0.20,
    used_spot: float = 100.0,
    used_strike: float = 100.0,
    used_settlement: int = EquitySettlementType.Physical,
) -> bytes:
    response = PriceEquityOptionResponseT()
    response.options = []
    one = EquityOptionResponseT()
    one.tradeId = "eq"
    one.npv = npv
    one.delta = delta
    one.gamma = gamma
    one.vega = vega
    one.theta = theta
    one.rho = rho
    one.impliedVolatility = implied_volatility
    one.usedSpot = used_spot
    one.usedStrike = used_strike
    one.usedSettlement = used_settlement
    response.options.append(one)
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
            api_key_id="ak-eq",
            owner_uid=OWNER,
            name="Equity Test",
            email="eq@example.com",
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
        app_rw_engine=cast(AsyncEngine, fake_rw_engine) if fake_rw_engine is not None else None,
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
# Row builders
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
        "day_counter": "Actual/365",
        "helper_kind": "Discount",
        "reference_date": date(2026, 5, 13),
        "points": [
            {
                "point_type": "DepositHelper",
                "tenor": {"n": 1, "unit": "Years"},
                "quote_id": quote_id,
                "fixing_days": 2,
                "calendar": "TARGET",
                "business_day_convention": "ModifiedFollowing",
                "day_counter": "Actual365Fixed",
            }
        ],
        "body": {"interpolator": "LogLinear"},
    }


def _vol_surface_row(
    *,
    vol_surface_id: uuid.UUID,
    name: str = "AAPL-vol",
    kind: str = "BlackVolSpec",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": str(vol_surface_id),
        "name": name,
        "kind": kind,
        "payload": payload if payload is not None else {"base": {"constant_vol": 0.20}},
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


def _inline_payload(
    *,
    discount_id: uuid.UUID | None = None,
    dividend_id: uuid.UUID | None = None,
    surface_id: uuid.UUID | None = None,
    spot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a fully inline ``POST /v1/price/equity-option`` payload."""

    curves: list[dict[str, Any]] = []
    if discount_id is not None:
        curves.append({"id": str(discount_id), "role": "discount"})
    else:
        curves.append(
            {
                "name": "discount",
                "role": "discount",
                "points": [
                    {
                        "point_type": "DepositHelper",
                        "tenor": {"n": 1, "unit": "Years"},
                        "rate": 0.04,
                        "fixing_days": 2,
                        "calendar": "TARGET",
                        "business_day_convention": "ModifiedFollowing",
                        "day_counter": "Actual365Fixed",
                    }
                ],
            }
        )
    if dividend_id is not None:
        curves.append({"id": str(dividend_id), "role": "dividend"})

    if surface_id is not None:
        vol_surface: dict[str, Any] = {"id": str(surface_id)}
    else:
        vol_surface = {
            "kind": "BlackVolSpec",
            "payload": {"base": {"constant_vol": 0.20}},
        }

    payload: dict[str, Any] = {
        "equity_option": {"option": {"option_type": "Call", "strike": 100.0}},
        "curves": curves,
        "vol_surface": vol_surface,
        "spot": spot if spot is not None else {"value": 100.0},
        "as_of": "2026-05-13",
    }
    return payload


# ---------------------------------------------------------------------------
# Auth + storage
# ---------------------------------------------------------------------------


def test_requires_auth(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, _, _ = stub_engine_app
    response = client.post(
        "/v1/price/equity-option",
        json={"equity_option": {}, "as_of": "2026-05-13"},
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
            "/v1/price/equity-option",
            json={
                "equity_option_id": str(uuid.uuid4()),
                "as_of": "2026-05-13",
            },
            headers=_headers(),
        )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["code"] == "storage_unavailable"


# ---------------------------------------------------------------------------
# Request-shape validation
# ---------------------------------------------------------------------------


def test_request_without_id_or_inline_is_validation_error(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, _, _ = stub_engine_app
    response = client.post(
        "/v1/price/equity-option",
        json={"as_of": "2026-05-13"},
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "validation_error"


def test_inline_without_curves_is_validation_error(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, _, _ = stub_engine_app
    response = client.post(
        "/v1/price/equity-option",
        json={"equity_option": {"option": {}}, "as_of": "2026-05-13"},
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "validation_error"


def test_id_and_inline_are_mutually_exclusive(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, _, _ = stub_engine_app
    response = client.post(
        "/v1/price/equity-option",
        json={
            "equity_option_id": str(uuid.uuid4()),
            "equity_option": {"option": {}},
            "as_of": "2026-05-13",
        },
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "validation_error"


# ---------------------------------------------------------------------------
# 404 surfaces
# ---------------------------------------------------------------------------


def test_equity_option_id_not_found(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, ro_engine, _ = stub_engine_app
    ro_engine.set_handler(lambda _sql, _params: [])

    response = client.post(
        "/v1/price/equity-option",
        json={
            "equity_option_id": str(uuid.uuid4()),
            "as_of": "2026-05-13",
        },
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "equity_option_not_found"


def test_discount_curve_not_found_returns_404_with_role(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, ro_engine, _ = stub_engine_app
    discount_id = uuid.uuid4()
    surface_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return []
        if "FROM app.vol_surfaces" in sql:
            return [_vol_surface_row(vol_surface_id=surface_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    ro_engine.set_handler(handler)

    response = client.post(
        "/v1/price/equity-option",
        json=_inline_payload(discount_id=discount_id, surface_id=surface_id),
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    body = response.json()
    assert body["code"] == "equity_option_discount_curve_not_found"
    assert body["details"][0]["role"] == "discount"


def test_vol_surface_not_found(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, ro_engine, _ = stub_engine_app
    discount_id = uuid.uuid4()
    surface_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=discount_id)]
        if "FROM app.vol_surfaces" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    ro_engine.set_handler(handler)

    response = client.post(
        "/v1/price/equity-option",
        json=_inline_payload(discount_id=discount_id, surface_id=surface_id),
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "equity_option_vol_surface_not_found"


def test_vol_surface_wrong_kind_returns_422(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, ro_engine, _ = stub_engine_app
    discount_id = uuid.uuid4()
    surface_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=discount_id)]
        if "FROM app.vol_surfaces" in sql:
            return [_vol_surface_row(vol_surface_id=surface_id, kind="SwaptionVolSpec")]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    ro_engine.set_handler(handler)

    response = client.post(
        "/v1/price/equity-option",
        json=_inline_payload(discount_id=discount_id, surface_id=surface_id),
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    body = response.json()
    assert body["code"] == "equity_option_vol_surface_wrong_kind"
    assert body["details"][0]["expected_kind"] == "BlackVolSpec"


def test_snapshot_not_found(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, ro_engine, _ = stub_engine_app
    discount_id = uuid.uuid4()
    surface_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=discount_id)]
        if "FROM app.vol_surfaces" in sql:
            return [_vol_surface_row(vol_surface_id=surface_id)]
        if "FROM app.snapshots" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    ro_engine.set_handler(handler)

    payload = _inline_payload(discount_id=discount_id, surface_id=surface_id)
    payload["snapshot_id"] = str(snapshot_id)

    response = client.post("/v1/price/equity-option", json=payload, headers=_headers())
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "equity_option_snapshot_not_found"


# ---------------------------------------------------------------------------
# 422 surfaces — quote resolution
# ---------------------------------------------------------------------------


def test_quote_resolution_failed_when_md_returns_found_false(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    discount_id = uuid.uuid4()
    surface_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=discount_id)]
        if "FROM app.vol_surfaces" in sql:
            return [_vol_surface_row(vol_surface_id=surface_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)
    md = _FakeMdClient(results=[_resolved_quote(canonical_id="USD.IRS.1Y", value=0.0, found=False)])
    client = custom_app_factory(md_client=md)

    response = client.post(
        "/v1/price/equity-option",
        json=_inline_payload(discount_id=discount_id, surface_id=surface_id),
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    body = response.json()
    assert body["code"] == "equity_option_quote_resolution_failed"
    assert body["details"] is not None
    assert any(d.get("canonical_id") == "USD.IRS.1Y" for d in body["details"])


def test_md_unreachable_returns_md_unreachable_envelope(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    discount_id = uuid.uuid4()
    surface_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=discount_id)]
        if "FROM app.vol_surfaces" in sql:
            return [_vol_surface_row(vol_surface_id=surface_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)
    md = _FakeMdClient(raises=MdTransportError("connection refused"))
    client = custom_app_factory(md_client=md)

    response = client.post(
        "/v1/price/equity-option",
        json=_inline_payload(discount_id=discount_id, surface_id=surface_id),
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
    client, ro_engine, md = stub_engine_app
    discount_id = uuid.uuid4()
    surface_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=discount_id)]
        if "FROM app.vol_surfaces" in sql:
            return [_vol_surface_row(vol_surface_id=surface_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    ro_engine.set_handler(handler)
    md._results = [_resolved_quote(canonical_id="USD.IRS.1Y", value=4.25)]

    response = client.post(
        "/v1/price/equity-option",
        json=_inline_payload(discount_id=discount_id, surface_id=surface_id),
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.BAD_GATEWAY
    body = response.json()
    assert body["code"] == "engine_unavailable"
    assert isinstance(body["details"], list)
    assert len(body["details"]) >= 1
    assembled = body["details"][0]["assembled_request"]
    assert assembled["as_of"] == "2026-05-13"
    assert assembled["discount_curve"]["id"] == str(discount_id)
    assert assembled["vol_surface"]["id"] == str(surface_id)
    assert assembled["equity_surface_id"] == str(surface_id)
    assert assembled["discount_curve_id"] == str(discount_id)
    assert assembled["spot"]["value"] == 100.0
    assert len(assembled["resolved_quotes"]) == 1
    assert assembled["resolved_quotes"][0]["canonical_id"] == "USD.IRS.1Y"


def test_success_path_returns_assembled_request_and_result(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    discount_id = uuid.uuid4()
    surface_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=discount_id)]
        if "FROM app.vol_surfaces" in sql:
            return [_vol_surface_row(vol_surface_id=surface_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)
    md = _FakeMdClient(results=[_resolved_quote(canonical_id="USD.IRS.1Y", value=4.25)])
    engine = _FakeEngineClient(response=_fb_response_bytes(npv=12_345.67))
    client = custom_app_factory(md_client=md, engine_client=engine)

    response = client.post(
        "/v1/price/equity-option",
        json=_inline_payload(discount_id=discount_id, surface_id=surface_id),
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["pricing_history_id"] is None
    assert body["result"]["npv"] == pytest.approx(12_345.67)
    assert body["result"]["delta"] == pytest.approx(0.55)
    assert body["result"]["used_settlement"] == "Physical"
    assert body["assembled_request"]["as_of"] == "2026-05-13"
    assert body["assembled_request"]["vol_surface"]["id"] == str(surface_id)
    assert body["assembled_request"]["equity_surface_id"] == str(surface_id)
    assert len(engine.calls) == 1
    assert engine.calls[0][0] is EngineRpc.PRICE_EQUITY_OPTION


def test_request_bytes_are_faithful_not_canonical(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    """The bytes reaching the engine carry the supplied curve / spot / vol + resolved ids.

    End-to-end faithful proof: the discount curve
    point carries the MD-resolved rate (not the canonical 0.03) with no
    ``quote_id`` leak; the underlier spot carries the supplied value (not the
    canonical 100.0); the BlackVol surface carries the supplied vol (not the
    canonical 0.20); and the per-trade discount / volatility references are the
    resolved ``app.*`` ids, never ``CANONICAL_*``.
    """

    discount_id = uuid.uuid4()
    surface_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=discount_id)]
        if "FROM app.vol_surfaces" in sql:
            return [
                _vol_surface_row(
                    vol_surface_id=surface_id,
                    payload={"base": {"constant_vol": 0.27}},
                )
            ]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)
    md = _FakeMdClient(results=[_resolved_quote(canonical_id="USD.IRS.1Y", value=0.037)])
    engine = _FakeEngineClient(response=_fb_response_bytes())
    client = custom_app_factory(md_client=md, engine_client=engine)

    spot = {"value": 123.45}
    response = client.post(
        "/v1/price/equity-option",
        json=_inline_payload(discount_id=discount_id, surface_id=surface_id, spot=spot),
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.OK, response.text

    assert len(engine.calls) == 1
    request_bytes = engine.calls[0][1]
    request = PriceEquityOptionRequestT.InitFromPackedBuf(request_bytes, 0)
    pricing = request.pricing

    # Discount curve: resolved id (not the canonical "discount") + the resolved
    # rate substituted in, quote id dropped (invariant #8). The object API
    # decodes string fields as bytes.
    discount_curve = pricing.rates.curves[0]
    assert discount_curve.id == str(discount_id).encode()
    assert discount_curve.id != b"discount"
    deposit = discount_curve.points[0].point
    assert deposit.rate == pytest.approx(0.037)
    assert deposit.rate != pytest.approx(0.03)  # not the canonical flat rate
    assert not deposit.quoteId

    # Underlier spot: the supplied value, not the canonical 100.0 default.
    assert pricing.quotes[0].value == pytest.approx(123.45)
    assert pricing.quotes[0].value != pytest.approx(100.0)

    # BlackVol surface: resolved id + the supplied vol, not the canonical 0.20.
    surface = pricing.volatility.volSurfaces[0]
    assert surface.id == str(surface_id).encode()
    assert surface.payload.base.constantVol == pytest.approx(0.27)
    assert surface.payload.base.constantVol != pytest.approx(0.20)

    # Per-trade references honour the resolved ids; no CANONICAL_* leak.
    one = request.options[0]
    assert one.discountingCurve == str(discount_id).encode()
    assert one.volatility == str(surface_id).encode()
    assert one.discountingCurve not in (b"discount", b"equity_vol")
    assert one.volatility not in (b"discount", b"equity_vol")


def test_snapshot_pinned_quote_marks_from_snapshot_true(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    """Pinned canonical IDs short-circuit the live MD client."""

    discount_id = uuid.uuid4()
    surface_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=discount_id)]
        if "FROM app.vol_surfaces" in sql:
            return [_vol_surface_row(vol_surface_id=surface_id)]
        if "FROM app.snapshots" in sql:
            return [
                {
                    "id": str(snapshot_id),
                    "name": "EOD-AAPL",
                    "content": {"USD.IRS.1Y": 4.25},
                }
            ]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)
    md = _FakeMdClient(results=[])
    client = custom_app_factory(md_client=md)

    payload = _inline_payload(discount_id=discount_id, surface_id=surface_id)
    payload["snapshot_id"] = str(snapshot_id)

    response = client.post("/v1/price/equity-option", json=payload, headers=_headers())
    assert response.status_code == HTTPStatus.BAD_GATEWAY  # stub engine
    body = response.json()
    assembled = body["details"][0]["assembled_request"]
    assert assembled["snapshot_id"] == str(snapshot_id)
    assert assembled["resolved_quotes"][0]["from_snapshot"] is True
    assert assembled["resolved_quotes"][0]["value"] == 4.25
    assert md.calls == []


def test_openapi_schema_advertises_post_route() -> None:
    """``POST /v1/price/equity-option`` shows up in the generated OpenAPI spec."""

    cfg = OrchestratorSettings(env=Environment.DEV, log_level=LogLevel.WARNING)
    app = create_app(cfg)
    schema = app.openapi()
    assert "/v1/price/equity-option" in schema["paths"]
    assert "post" in schema["paths"]["/v1/price/equity-option"]


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
    discount_id = uuid.uuid4()
    surface_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=discount_id)]
        if "FROM app.vol_surfaces" in sql:
            return [_vol_surface_row(vol_surface_id=surface_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)
    md = _FakeMdClient(results=[_resolved_quote(canonical_id="USD.IRS.1Y", value=4.25)])
    engine = _FakeEngineClient(response=_fb_response_bytes(npv=12_345.67))

    inserted_id = uuid.uuid4()
    captured_inserts: list[tuple[str, dict[str, Any]]] = []
    rw_engine = FakeEngine()
    rw_engine.set_handler(_pricing_history_handler(inserted_id, captured_inserts))

    client = custom_app_factory(md_client=md, engine_client=engine, rw_engine=rw_engine)
    response = client.post(
        "/v1/price/equity-option",
        json=_inline_payload(discount_id=discount_id, surface_id=surface_id),
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
    assert params["product_kind"] == "equity_options"
    assert params["product_id"] is None
    persisted_response = json.loads(params["response"])
    assert persisted_response["npv"] == pytest.approx(12_345.67)


def test_engine_failure_records_pricing_history_failure_row(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    discount_id = uuid.uuid4()
    surface_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=discount_id)]
        if "FROM app.vol_surfaces" in sql:
            return [_vol_surface_row(vol_surface_id=surface_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)
    md = _FakeMdClient(results=[_resolved_quote(canonical_id="USD.IRS.1Y", value=4.25)])

    captured_inserts: list[tuple[str, dict[str, Any]]] = []
    rw_engine = FakeEngine()
    rw_engine.set_handler(_pricing_history_handler(uuid.uuid4(), captured_inserts))
    client = custom_app_factory(md_client=md, rw_engine=rw_engine)

    response = client.post(
        "/v1/price/equity-option",
        json=_inline_payload(discount_id=discount_id, surface_id=surface_id),
        headers=_headers(),
    )

    assert response.status_code == HTTPStatus.BAD_GATEWAY
    assert response.json()["code"] == "engine_unavailable"

    assert len(captured_inserts) == 1
    _, params = captured_inserts[0]
    assert params["product_kind"] == "equity_options"
    assert params["product_id"] is None
    persisted_response = json.loads(params["response"])
    assert "error" in persisted_response
    assert persisted_response["error"]["code"] == "engine_unavailable"


def test_pricing_history_write_failure_still_returns_pricing_response(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    discount_id = uuid.uuid4()
    surface_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=discount_id)]
        if "FROM app.vol_surfaces" in sql:
            return [_vol_surface_row(vol_surface_id=surface_id)]
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
        "/v1/price/equity-option",
        json=_inline_payload(discount_id=discount_id, surface_id=surface_id),
        headers=_headers(),
    )

    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["pricing_history_id"] is None
    assert body["result"]["npv"] == pytest.approx(42.0)
    assert len(captured_inserts) == 1
