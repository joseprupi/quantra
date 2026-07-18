"""End-to-end route tests for ``POST /v1/price/swaption``.

Mirrors :mod:`tests.test_swap_ir_api` shape (per-product split).
Three classes of dependency are stubbed:

* ``MdClient`` — :class:`_FakeMdClient` overrides the single method
  the walker uses (``resolve_quotes``).
* ``EngineClient`` — :class:`_FakeEngineClient` records every call
  and returns / raises whatever the test installed. The default
  ``StubEngineClient`` raises ``NotImplementedError`` on every call;
  the success-path test uses the fake.
* ``app_ro`` — :class:`FakeEngine` from ``conftest`` stands in for
  the real ``AsyncEngine`` so assembler queries are deterministic.

Coverage matrix (one test per row, additive over the swap_ir suite):

1. Validation — request-shape, mutually-exclusive branches, inline-mode
   requires curves / vol_surface / swaption_model.
2. Storage-unavailable (no ``app_ro`` engine → 503).
3. Not-found surfaces (``swaption_id`` / ``curve_set_id`` /
   ``vol_surface_id`` / ``swaption_model_id`` / ``snapshot_id``).
4. Curve resolution failed (no pricing block on saved swaption).
5. Surface resolution failed (no inline payload + no saved ref).
6. Quote resolution failed (per-item ``found=False``) + MD-unreachable.
7. Engine-side failure with assembled-request echo in structured ``details``.
8. Success path: engine returns one ``SwaptionResult`` and the route
   echoes the assembled request + the result.
9. Snapshot-pinned ID short-circuits the MD client (also covers the
   surface-side quote pin since the walker is curve+surface).
10. OpenAPI schema advertises the route (regression for
    ``register_all`` falling off).
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
from quantra_common.engine_client._generated.quantra.PriceSwaptionRequest import (
    PriceSwaptionRequest,
)
from quantra_common.engine_client._generated.quantra.PriceSwaptionResponse import (
    PriceSwaptionResponseT,
)
from quantra_common.engine_client._generated.quantra.SwaptionResponse import (
    SwaptionResponseT,
)
from quantra_common.engine_client._generated.quantra.SwaptionVolConstantSpec import (
    SwaptionVolConstantSpec,
)
from quantra_common.engine_client._generated.quantra.SwaptionVolSpec import (
    SwaptionVolSpec,
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
API_KEY = "key-swaption"


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
        super().__init__(
            MdClientConfig(base_url="http://stub", timeout_s=1.0, max_retries=0),
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


def _fb_response_bytes(npv: float = 0.0, vega: float = 0.0) -> bytes:
    """Synthesise a ``PriceSwaptionResponse`` flatbuffer with one swaption."""

    response = PriceSwaptionResponseT()
    response.swaptions = []
    s = SwaptionResponseT()
    s.npv = npv
    s.vega = vega
    s.delta = 0.5
    s.gamma = 0.0
    s.theta = 0.0
    s.dv01 = 0.0
    s.impliedVolatility = 0.20
    s.atmForward = 0.03
    s.annuity = 4.4e6
    response.swaptions.append(s)
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
            api_key_id="ak-swaption",
            owner_uid=OWNER,
            name="Swaption Test",
            email="swaption@example.com",
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
# Helpers
# ---------------------------------------------------------------------------


def _headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


def _curve_row(*, curve_id: uuid.UUID, name: str = "USD-OIS") -> dict[str, Any]:
    # A translatable DepositHelper point: the strict
    # translator needs a mapped ``point_type`` + a value source. The
    # ``quote_id`` is carried nested under ``point`` so the MD walker collects
    # it and the translator substitutes the resolved value server-side.
    return {
        "id": str(curve_id),
        "name": name,
        "currency": "USD",
        "day_counter": "Actual360",
        "helper_kind": "Discount",
        "reference_date": date(2026, 5, 13),
        "points": [
            {
                "point_type": "DepositHelper",
                "point": {
                    "quote_id": "USD.IRS.1Y",
                    "tenor": {"n": 1, "unit": "Years"},
                    "fixing_days": 2,
                    "calendar": "TARGET",
                    "business_day_convention": "ModifiedFollowing",
                    "day_counter": "Actual365Fixed",
                },
            }
        ],
        "body": {"interpolator": "LogLinear"},
    }


def _vol_surface_row(
    *,
    vol_surface_id: uuid.UUID,
    name: str = "USD-ATM",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": str(vol_surface_id),
        "name": name,
        "kind": "SwaptionVolSpec",
        "payload": payload or {"base": {"quote_id": "USD.SWPTN.ATM.5Y10Y.VOL"}},
    }


def _swaption_model_row(*, model_id: uuid.UUID, name: str = "HW-LATTICE") -> dict[str, Any]:
    return {
        "id": str(model_id),
        "name": name,
        "kind": "HullWhiteLattice",
        "payload": {"hw_a": 0.05, "hw_sigma": 0.01},
    }


def _swaption_row(*, swaption_id: uuid.UUID, request_json: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(swaption_id), "name": "demo-swaption", "request": request_json}


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
    vol_surface_id: uuid.UUID | None = None,
    swaption_model_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Build a fully-inline-mode pricing request body.

    Used by tests that don't actually exercise the swaption-row
    load path; an explicit curve/vol-surface/model ref keeps the
    assembler away from ``app.swaptions`` so we can keep the
    ``app_ro`` handler tiny.
    """

    payload: dict[str, Any] = {
        # Explicit economics — the wire layer no longer supplies silent
        # defaults (422 ``swaption_missing_trade_fields`` otherwise).
        "swaption": {
            "exercise_type": "European",
            "notional": 1_000_000.0,
            "strike": 0.035,
            "exercise_date": "2026-01-15",
            "effective_date": "2026-01-17",
            "termination_date": "2031-01-17",
        },
        "as_of": "2026-05-13",
    }
    payload["curves"] = (
        [{"id": str(curve_id)}]
        if curve_id is not None
        else [
            {
                "name": "USD-OIS",
                "points": [
                    {
                        "point_type": "DepositHelper",
                        "point": {
                            "quote_id": "USD.IRS.1Y",
                            "tenor": {"n": 1, "unit": "Years"},
                        },
                    }
                ],
            }
        ]
    )
    if vol_surface_id is not None:
        payload["vol_surface"] = {"id": str(vol_surface_id)}
    else:
        payload["vol_surface"] = {
            "kind": "SwaptionVolSpec",
            "payload": {"base": {"quote_id": "USD.SWPTN.ATM.5Y10Y.VOL"}},
        }
    if swaption_model_id is not None:
        payload["swaption_model"] = {"id": str(swaption_model_id)}
    else:
        payload["swaption_model"] = {
            "kind": "HullWhiteLattice",
            "payload": {"hw_a": 0.05, "hw_sigma": 0.01},
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
        "/v1/price/swaption",
        json=_inline_payload(),
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
            "/v1/price/swaption",
            json={
                "swaption_id": str(uuid.uuid4()),
                "as_of": "2026-05-13",
            },
            headers=_headers(),
        )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["code"] == "storage_unavailable"


# ---------------------------------------------------------------------------
# Request-shape validation
# ---------------------------------------------------------------------------


def test_request_without_swaption_id_or_swaption_is_validation_error(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, _, _ = stub_engine_app
    response = client.post(
        "/v1/price/swaption",
        json={"as_of": "2026-05-13"},
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "validation_error"


def test_inline_swaption_without_curves_is_validation_error(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, _, _ = stub_engine_app
    response = client.post(
        "/v1/price/swaption",
        json={
            "swaption": {"exercise_type": "European"},
            "vol_surface": {
                "kind": "SwaptionVolSpec",
                "payload": {"base": {"quote_id": "X"}},
            },
            "swaption_model": {
                "kind": "HullWhiteLattice",
                "payload": {"hw_a": 0.05},
            },
            "as_of": "2026-05-13",
        },
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "validation_error"


def test_inline_swaption_without_vol_surface_is_validation_error(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, _, _ = stub_engine_app
    response = client.post(
        "/v1/price/swaption",
        json={
            "swaption": {"exercise_type": "European"},
            "curves": [
                {
                    "name": "USD-OIS",
                    "points": [{"tenor": "1Y", "quote_id": "USD.IRS.1Y"}],
                }
            ],
            "swaption_model": {
                "kind": "HullWhiteLattice",
                "payload": {"hw_a": 0.05},
            },
            "as_of": "2026-05-13",
        },
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "validation_error"


def test_inline_swaption_without_swaption_model_is_validation_error(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, _, _ = stub_engine_app
    response = client.post(
        "/v1/price/swaption",
        json={
            "swaption": {"exercise_type": "European"},
            "curves": [
                {
                    "name": "USD-OIS",
                    "points": [{"tenor": "1Y", "quote_id": "USD.IRS.1Y"}],
                }
            ],
            "vol_surface": {
                "kind": "SwaptionVolSpec",
                "payload": {"base": {"quote_id": "X"}},
            },
            "as_of": "2026-05-13",
        },
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "validation_error"


# ---------------------------------------------------------------------------
# 404 surfaces
# ---------------------------------------------------------------------------


def test_swaption_id_not_found_returns_swaption_not_found(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, ro_engine, _ = stub_engine_app
    ro_engine.set_handler(lambda _sql, _params: [])

    response = client.post(
        "/v1/price/swaption",
        json={"swaption_id": str(uuid.uuid4()), "as_of": "2026-05-13"},
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "swaption_not_found"


def test_curve_set_not_found_returns_swaption_curve_set_not_found(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, ro_engine, _ = stub_engine_app
    swaption_id = uuid.uuid4()
    curve_set_id = uuid.uuid4()
    saved = _swaption_row(
        swaption_id=swaption_id,
        request_json={"pricing": {"curve_set_id": str(curve_set_id)}},
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.swaptions" in sql:
            return [saved]
        if "FROM app.curve_sets" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    ro_engine.set_handler(handler)

    response = client.post(
        "/v1/price/swaption",
        json={"swaption_id": str(swaption_id), "as_of": "2026-05-13"},
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "swaption_curve_set_not_found"


def test_vol_surface_id_not_found_returns_swaption_vol_surface_not_found(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, ro_engine, _ = stub_engine_app
    vol_surface_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.vol_surfaces" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    ro_engine.set_handler(handler)

    response = client.post(
        "/v1/price/swaption",
        json=_inline_payload(vol_surface_id=vol_surface_id),
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "swaption_vol_surface_not_found"


def test_swaption_model_id_not_found_returns_swaption_model_not_found(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, ro_engine, _ = stub_engine_app
    model_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.swaption_models" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    ro_engine.set_handler(handler)

    response = client.post(
        "/v1/price/swaption",
        json=_inline_payload(swaption_model_id=model_id),
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "swaption_model_not_found"


def test_snapshot_id_not_found_returns_swaption_snapshot_not_found(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, ro_engine, _ = stub_engine_app
    snapshot_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.snapshots" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    ro_engine.set_handler(handler)

    payload = _inline_payload()
    payload["snapshot_id"] = str(snapshot_id)

    response = client.post(
        "/v1/price/swaption",
        json=payload,
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "swaption_snapshot_not_found"


# ---------------------------------------------------------------------------
# 422 surfaces
# ---------------------------------------------------------------------------


def test_curve_resolution_failed_for_saved_swaption_without_pricing_block(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    """A saved swaption with no ``pricing`` block → 422 curve resolution."""

    client, ro_engine, _ = stub_engine_app
    swaption_id = uuid.uuid4()
    saved = _swaption_row(
        swaption_id=swaption_id,
        request_json={"exercise_type": "European"},  # no pricing key
    )
    ro_engine.set_handler(lambda _sql, _params: [saved])

    response = client.post(
        "/v1/price/swaption",
        json={"swaption_id": str(swaption_id), "as_of": "2026-05-13"},
        headers=_headers(),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "swaption_curve_resolution_failed"


def test_surface_resolution_failed_when_saved_has_no_vol_surface_ref(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    """Saved swaption has curves but no vol_surface info → 422 surface."""

    client, ro_engine, _ = stub_engine_app
    swaption_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    saved = _swaption_row(
        swaption_id=swaption_id,
        request_json={
            "pricing": {
                "curves": [{"id": str(curve_id)}],
                # No vol_surface_id, no vol_surfaces[]
            }
        },
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.swaptions" in sql:
            return [saved]
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    ro_engine.set_handler(handler)

    response = client.post(
        "/v1/price/swaption",
        json={"swaption_id": str(swaption_id), "as_of": "2026-05-13"},
        headers=_headers(),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "swaption_surface_resolution_failed"


def test_quote_resolution_failed_when_md_returns_found_false(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    curve_id = uuid.uuid4()
    fake_ro_engine.set_handler(lambda _sql, _params: [_curve_row(curve_id=curve_id)])
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="USD.IRS.1Y", value=4.25),
            _resolved_quote(canonical_id="USD.SWPTN.ATM.5Y10Y.VOL", value=0.0, found=False),
        ]
    )
    client = custom_app_factory(md_client=md)

    response = client.post(
        "/v1/price/swaption",
        json=_inline_payload(curve_id=curve_id),
        headers=_headers(),
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "swaption_quote_resolution_failed"
    assert body["details"] is not None
    assert any(d.get("canonical_id") == "USD.SWPTN.ATM.5Y10Y.VOL" for d in body["details"])


def test_quote_resolution_md_unreachable_returns_md_unreachable_envelope(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    curve_id = uuid.uuid4()
    fake_ro_engine.set_handler(lambda _sql, _params: [_curve_row(curve_id=curve_id)])
    md = _FakeMdClient(raises=MdTransportError("connection refused"))
    client = custom_app_factory(md_client=md)

    response = client.post(
        "/v1/price/swaption",
        json=_inline_payload(curve_id=curve_id),
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
    ``details`` must echo the assembled request so the operator can
    verify the orchestrator completed every preceding stage
    (assemble → MD resolve → fan-out) before the stub raised.
    """

    client, ro_engine, md = stub_engine_app
    curve_id = uuid.uuid4()
    ro_engine.set_handler(lambda _sql, _params: [_curve_row(curve_id=curve_id)])
    md._results = [
        _resolved_quote(canonical_id="USD.IRS.1Y", value=4.25),
        _resolved_quote(canonical_id="USD.SWPTN.ATM.5Y10Y.VOL", value=0.65),
    ]

    response = client.post(
        "/v1/price/swaption",
        json=_inline_payload(curve_id=curve_id),
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.BAD_GATEWAY
    body = response.json()
    assert body["code"] == "engine_unavailable"
    assert isinstance(body["details"], list)
    assert len(body["details"]) >= 1
    assembled = body["details"][0]["assembled_request"]
    assert assembled["as_of"] == "2026-05-13"
    assert assembled["trade"]["swaption"] == {
        "exercise_type": "European",
        "notional": 1_000_000.0,
        "strike": 0.035,
        "exercise_date": "2026-01-15",
        "effective_date": "2026-01-17",
        "termination_date": "2031-01-17",
    }
    assert len(assembled["curves"]) == 1
    assert assembled["curves"][0]["id"] == str(curve_id)
    assert assembled["vol_surface"]["kind"] == "SwaptionVolSpec"
    assert assembled["swaption_model"]["kind"] == "HullWhiteLattice"
    assert len(assembled["resolved_quotes"]) == 2
    resolved_ids = {q["canonical_id"] for q in assembled["resolved_quotes"]}
    assert resolved_ids == {"USD.IRS.1Y", "USD.SWPTN.ATM.5Y10Y.VOL"}
    for entry in assembled["resolved_quotes"]:
        assert entry["from_snapshot"] is False


def test_engine_request_bytes_are_faithful_no_canonical_leak(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    """The bytes ``EngineClient.call`` received encode the resolved inputs.

    Against a stub engine (raises ``NotImplementedError`` → 502
    ``engine_unavailable``) the request bytes are still captured: the per-trade
    discount / forwarding / volatility / model references carry the resolved
    entity ids (no ``CANONICAL_*`` leak) and the encoded surface carries
    the supplied vol level (0.65), not the canonical 0.20.
    """

    curve_id = uuid.uuid4()
    fake_ro_engine.set_handler(lambda _sql, _params: [_curve_row(curve_id=curve_id)])
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="USD.IRS.1Y", value=4.25),
            _resolved_quote(canonical_id="USD.SWPTN.ATM.5Y10Y.VOL", value=0.65),
        ]
    )
    engine = _FakeEngineClient(raises=NotImplementedError("stub backend"))
    client = custom_app_factory(md_client=md, engine_client=engine)

    response = client.post(
        "/v1/price/swaption",
        json=_inline_payload(curve_id=curve_id),
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.BAD_GATEWAY
    assert response.json()["code"] == "engine_unavailable"

    # The stub raised, but the bytes were assembled and handed to ``call``.
    assert len(engine.calls) == 1
    rpc, request_bytes = engine.calls[0]
    assert rpc is EngineRpc.PRICE_SWAPTION
    request = PriceSwaptionRequest.GetRootAs(request_bytes, 0)
    swaption = request.Swaptions(0)
    assert swaption is not None
    # Per-trade references honour the resolved ids (inline surface / model →
    # their resolved name; curve → its UUID), never the canonical placeholders.
    assert swaption.DiscountingCurve() == str(curve_id).encode()
    assert swaption.ForwardingCurve() == str(curve_id).encode()
    assert swaption.Volatility() == b"inline-vol-surface"
    assert swaption.Model() == b"inline-swaption-model"
    for ref in (
        swaption.DiscountingCurve(),
        swaption.ForwardingCurve(),
        swaption.Volatility(),
        swaption.Model(),
    ):
        assert ref not in (b"discount", b"swaption_vol", b"black_model")

    # The resolved vol (0.65) is in the encoded surface; the quote id is gone.
    volatility = request.Pricing().Volatility()
    assert volatility is not None
    surface = volatility.VolSurfaces(0)
    assert surface is not None
    assert surface.Id() == b"inline-vol-surface"
    sv = SwaptionVolSpec()
    sv_table = surface.Payload()
    sv.Init(sv_table.Bytes, sv_table.Pos)
    const = SwaptionVolConstantSpec()
    const_table = sv.Payload()
    const.Init(const_table.Bytes, const_table.Pos)
    base = const.Base()
    assert base is not None
    assert base.ConstantVol() == pytest.approx(0.65)
    assert not base.QuoteId()


def test_success_path_returns_assembled_request_and_result(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    """Happy path: fake engine returns one SwaptionResult; the route echoes both."""

    curve_id = uuid.uuid4()
    fake_ro_engine.set_handler(lambda _sql, _params: [_curve_row(curve_id=curve_id)])
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="USD.IRS.1Y", value=4.25),
            _resolved_quote(canonical_id="USD.SWPTN.ATM.5Y10Y.VOL", value=0.65),
        ]
    )
    engine = _FakeEngineClient(response=_fb_response_bytes(npv=234_567.89, vega=12.5))
    client = custom_app_factory(md_client=md, engine_client=engine)

    response = client.post(
        "/v1/price/swaption",
        json=_inline_payload(curve_id=curve_id),
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["pricing_history_id"] is None
    assert body["result"]["npv"] == pytest.approx(234_567.89)
    assert body["result"]["vega"] == pytest.approx(12.5)
    # ``decode_swaption_response`` projects FlatBuffers fields into the
    # result's ``extras`` bag (vendored bindings).
    assert "implied_volatility" in body["result"]["extras"]
    assert body["assembled_request"]["as_of"] == "2026-05-13"
    assert len(body["assembled_request"]["curves"]) == 1
    assert body["assembled_request"]["vol_surface"]["kind"] == "SwaptionVolSpec"
    assert body["assembled_request"]["swaption_model"]["kind"] == "HullWhiteLattice"
    assert len(body["assembled_request"]["resolved_quotes"]) == 2
    # Batching seam exercised: exactly one engine call with PRICE_SWAPTION.
    assert len(engine.calls) == 1
    assert engine.calls[0][0] is EngineRpc.PRICE_SWAPTION


def test_snapshot_pinned_quote_marks_from_snapshot_true(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    """Pinned canonical IDs (both curve + surface) short-circuit MD."""

    curve_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.snapshots" in sql:
            return [
                {
                    "id": str(snapshot_id),
                    "name": "EOD-USD",
                    "content": {
                        "USD.IRS.1Y": 4.25,
                        "USD.SWPTN.ATM.5Y10Y.VOL": 0.65,
                    },
                }
            ]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)
    md = _FakeMdClient(results=[])
    client = custom_app_factory(md_client=md)

    payload = _inline_payload(curve_id=curve_id)
    payload["snapshot_id"] = str(snapshot_id)

    response = client.post(
        "/v1/price/swaption",
        json=payload,
        headers=_headers(),
    )
    # Stub engine — envelope, details carry the assembled request.
    assert response.status_code == HTTPStatus.BAD_GATEWAY
    body = response.json()
    assembled = body["details"][0]["assembled_request"]
    assert assembled["snapshot_id"] == str(snapshot_id)
    assert len(assembled["resolved_quotes"]) == 2
    for entry in assembled["resolved_quotes"]:
        assert entry["from_snapshot"] is True
    # No live MD call needed when everything is pinned.
    assert md.calls == []


def test_openapi_schema_advertises_post_route() -> None:
    """``POST /v1/price/swaption`` shows up in the generated OpenAPI spec.

    Catches a wiring regression where the new router falls off
    ``register_all`` without anyone noticing (the route would still
    be importable but unreachable through the public app).
    """

    cfg = OrchestratorSettings(env=Environment.DEV, log_level=LogLevel.WARNING)
    app = create_app(cfg)
    schema = app.openapi()
    assert "/v1/price/swaption" in schema["paths"]


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
    fake_ro_engine.set_handler(lambda _sql, _params: [_curve_row(curve_id=curve_id)])
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="USD.IRS.1Y", value=4.25),
            _resolved_quote(canonical_id="USD.SWPTN.ATM.5Y10Y.VOL", value=0.65),
        ]
    )
    engine = _FakeEngineClient(response=_fb_response_bytes(npv=234_567.89, vega=12.5))

    inserted_id = uuid.uuid4()
    captured_inserts: list[tuple[str, dict[str, Any]]] = []
    rw_engine = FakeEngine()
    rw_engine.set_handler(_pricing_history_handler(inserted_id, captured_inserts))

    client = custom_app_factory(md_client=md, engine_client=engine, rw_engine=rw_engine)
    response = client.post(
        "/v1/price/swaption",
        json=_inline_payload(curve_id=curve_id),
        headers=_headers(),
    )

    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["pricing_history_id"] == str(inserted_id)
    uuid.UUID(body["pricing_history_id"])

    assert len(captured_inserts) == 1
    sql, params = captured_inserts[0]
    assert "INSERT INTO app.pricing_history" in sql
    assert "CAST(:request AS jsonb)" in sql
    assert params["owner_uid"] == OWNER
    assert params["product_kind"] == "swaptions"
    assert params["product_id"] is None
    persisted_response = json.loads(params["response"])
    assert persisted_response["npv"] == pytest.approx(234_567.89)


def test_engine_failure_records_pricing_history_failure_row(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    curve_id = uuid.uuid4()
    fake_ro_engine.set_handler(lambda _sql, _params: [_curve_row(curve_id=curve_id)])
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="USD.IRS.1Y", value=4.25),
            _resolved_quote(canonical_id="USD.SWPTN.ATM.5Y10Y.VOL", value=0.65),
        ]
    )

    captured_inserts: list[tuple[str, dict[str, Any]]] = []
    rw_engine = FakeEngine()
    rw_engine.set_handler(_pricing_history_handler(uuid.uuid4(), captured_inserts))
    client = custom_app_factory(md_client=md, rw_engine=rw_engine)

    response = client.post(
        "/v1/price/swaption",
        json=_inline_payload(curve_id=curve_id),
        headers=_headers(),
    )

    assert response.status_code == HTTPStatus.BAD_GATEWAY
    assert response.json()["code"] == "engine_unavailable"

    assert len(captured_inserts) == 1
    _, params = captured_inserts[0]
    assert params["owner_uid"] == OWNER
    assert params["product_kind"] == "swaptions"
    assert params["product_id"] is None
    persisted_response = json.loads(params["response"])
    assert "error" in persisted_response
    assert persisted_response["error"]["code"] == "engine_unavailable"
    assert persisted_response["error"]["status_code"] == HTTPStatus.BAD_GATEWAY.value


def test_pricing_history_write_failure_still_returns_pricing_response(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    curve_id = uuid.uuid4()
    fake_ro_engine.set_handler(lambda _sql, _params: [_curve_row(curve_id=curve_id)])
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="USD.IRS.1Y", value=4.25),
            _resolved_quote(canonical_id="USD.SWPTN.ATM.5Y10Y.VOL", value=0.65),
        ]
    )
    engine = _FakeEngineClient(response=_fb_response_bytes(npv=42.0, vega=1.5))

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
        "/v1/price/swaption",
        json=_inline_payload(curve_id=curve_id),
        headers=_headers(),
    )

    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["pricing_history_id"] is None
    assert body["result"]["npv"] == pytest.approx(42.0)
    assert len(captured_inserts) == 1
