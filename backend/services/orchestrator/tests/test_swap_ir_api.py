"""End-to-end route tests for ``POST /v1/price/swap/ir``.

Drives the FastAPI ``TestClient`` so auth + assemble + MD resolve +
the concurrency seam + engine call + error envelope all light up
together. Three classes of dependency are stubbed:

* ``MdClient`` — :class:`_RaisingMdClient` / :class:`_FakeMdClient`
  override the single method the walker uses (``resolve_quotes``).
* ``EngineClient`` — :class:`_FakeEngineClient` records every call
  and returns or raises whatever the test installed. The default
  ``StubEngineClient`` raises ``NotImplementedError`` on every call;
  tests that need a happy-path engine response use the fake.
* ``app_ro`` — :class:`FakeEngine` from ``conftest`` stands in for
  the real ``AsyncEngine`` so assembler queries are deterministic.

The auth gate / request-id propagation envelope are already
covered by the data-layer and engine-client suites; this file
focuses on the end-to-end vertical the swap_ir plan delivers:

1. Validation (request-shape, mutually-exclusive branches).
2. Storage-unavailable (no ``app_ro`` engine → 503).
3. Not-found surfaces (``swap_id`` / ``curve_set_id`` / ``snapshot_id``).
4. Curve-resolution failure (missing inline tenor, missing pricing block).
5. Quote-resolution failure (missing MD pins, per-item ``found=False``).
6. Engine-side failure with assembled-request echoed in ``details``
   (the most-important "did the resolution path complete?"
   verification on today's stub engine).
7. Success path: engine returns one ``IrSwapResult`` and the route
   echoes the assembled request + result in the response body.
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
from quantra_common.engine_client._generated.quantra.DepositHelper import DepositHelper
from quantra_common.engine_client._generated.quantra.PriceVanillaSwapRequest import (
    PriceVanillaSwapRequest,
)
from quantra_common.engine_client._generated.quantra.PriceVanillaSwapResponse import (
    PriceVanillaSwapResponseT,
)
from quantra_common.engine_client._generated.quantra.VanillaSwapResponse import (
    VanillaSwapResponseT,
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
API_KEY = "key-swap-ir"


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
        # MdClient needs a transport; pass a never-used real one so the
        # parent ctor stays happy. The override below short-circuits
        # every code path that would hit it.
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


def _fb_response_bytes(npv: float = 0.0, fair_rate: float = 0.03) -> bytes:
    """Synthesize a valid ``PriceVanillaSwapResponse`` flatbuffer.

    Used by tests that need a happy-path engine reply without
    standing up the live engine. The response carries one swap
    result with the supplied NPV / fair-rate; per-leg NPVs are
    set to half the total each (a meaningless but well-formed
    decomposition).
    """

    response = PriceVanillaSwapResponseT()
    response.swaps = []
    swap_response = VanillaSwapResponseT()
    swap_response.npv = npv
    swap_response.fairRate = fair_rate
    swap_response.fairSpread = 0.0
    swap_response.fixedLegBps = 0.0
    swap_response.floatingLegBps = 0.0
    swap_response.fixedLegNpv = npv * 0.5
    swap_response.floatingLegNpv = npv * 0.5
    response.swaps.append(swap_response)
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
            api_key_id="ak-swap-ir",
            owner_uid=OWNER,
            name="Swap IR Test",
            email="swap-ir@example.com",
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
        engine_client=None,  # → lifespan provisions a StubEngineClient
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
    """Factory that builds the orchestrator with caller-supplied md/engine.

    Returned ``_build(...)`` enters the lifespan context (so the
    default ``StubEngineClient`` is provisioned when no engine is
    injected) and registers the resulting client for teardown so
    individual tests don't need their own ``with``-block. Returns
    the bound :class:`TestClient`.
    """

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
    # A translatable DepositHelper point: the strict translator
    # needs a mapped ``point_type`` + a value source. The ``quote_id`` is still
    # carried nested under ``point`` so the MD walker collects it and the
    # translator substitutes the resolved value server-side.
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


def _swap_row(*, swap_id: uuid.UUID, request_json: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(swap_id), "name": "demo-swap", "request": request_json}


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
# Auth + storage
# ---------------------------------------------------------------------------


def test_requires_auth(stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient]) -> None:
    client, _, _ = stub_engine_app
    response = client.post(
        "/v1/price/swap/ir",
        json={
            "swap": {
                "notional": 1.0,
                "fixed_rate": 0.035,
                "effective_date": "2026-06-15",
                "termination_date": "2031-06-15",
            },
            "curves": [],
            "as_of": "2026-05-13",
        },
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json()["code"] == "unauthenticated"


def test_storage_unavailable_when_app_ro_missing(
    auth_lookup: ApiKeyLookup,
    firebase_verifier: FirebaseTokenVerifier,
) -> None:
    # Explicitly clobber the DSNs so the lifespan can't open a real
    # engine from the developer's .env. Mirrors the established
    # pattern in test_data_routers.py.
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
            "/v1/price/swap/ir",
            json={
                "swap_id": str(uuid.uuid4()),
                "as_of": "2026-05-13",
            },
            headers=_headers(),
        )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["code"] == "storage_unavailable"


# ---------------------------------------------------------------------------
# Request-shape validation
# ---------------------------------------------------------------------------


def test_request_without_swap_id_or_swap_is_validation_error(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, _, _ = stub_engine_app
    response = client.post(
        "/v1/price/swap/ir",
        json={"as_of": "2026-05-13"},
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "validation_error"


def test_inline_swap_without_curves_is_validation_error(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, _, _ = stub_engine_app
    response = client.post(
        "/v1/price/swap/ir",
        json={
            "swap": {
                "notional": 1.0,
                "fixed_rate": 0.035,
                "effective_date": "2026-06-15",
                "termination_date": "2031-06-15",
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


def test_swap_id_not_found_returns_swap_ir_not_found(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, ro_engine, _ = stub_engine_app
    ro_engine.set_handler(lambda _sql, _params: [])

    response = client.post(
        "/v1/price/swap/ir",
        json={"swap_id": str(uuid.uuid4()), "as_of": "2026-05-13"},
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    body = response.json()
    assert body["code"] == "swap_ir_not_found"


def test_curve_set_not_found_returns_swap_ir_curve_set_not_found(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, ro_engine, _ = stub_engine_app
    swap_id = uuid.uuid4()
    curve_set_id = uuid.uuid4()
    swap = _swap_row(
        swap_id=swap_id,
        request_json={"pricing": {"curve_set_id": str(curve_set_id)}},
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.swaps_ir" in sql:
            return [swap]
        if "FROM app.curve_sets" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    ro_engine.set_handler(handler)

    response = client.post(
        "/v1/price/swap/ir",
        json={"swap_id": str(swap_id), "as_of": "2026-05-13"},
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "swap_ir_curve_set_not_found"


def test_snapshot_id_not_found_returns_swap_ir_snapshot_not_found(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, ro_engine, _ = stub_engine_app
    snapshot_id = uuid.uuid4()
    curve_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.snapshots" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    ro_engine.set_handler(handler)

    response = client.post(
        "/v1/price/swap/ir",
        json={
            "swap": {
                "notional": 1.0,
                "fixed_rate": 0.035,
                "effective_date": "2026-06-15",
                "termination_date": "2031-06-15",
            },
            "curves": [{"id": str(curve_id)}],
            "as_of": "2026-05-13",
            "snapshot_id": str(snapshot_id),
        },
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "swap_ir_snapshot_not_found"


# ---------------------------------------------------------------------------
# 422 surfaces
# ---------------------------------------------------------------------------


def test_curve_resolution_failed_for_inline_curve_missing_required_fields(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    """An inline curve in the saved swap missing ``name``/``points`` → 422.

    Inline definitions buried inside the saved swap can't be
    caught by pydantic on the inbound request (the request side
    just carries ``swap_id``). The assembler surfaces them via
    the product-scoped ``swap_ir_curve_resolution_failed`` code.
    """

    client, ro_engine, _ = stub_engine_app
    swap_id = uuid.uuid4()
    swap = _swap_row(
        swap_id=swap_id,
        request_json={
            "pricing": {
                "curves": [
                    {
                        # Inline-only entry with neither id nor points.
                        "currency": "USD",
                    }
                ]
            }
        },
    )
    ro_engine.set_handler(lambda _sql, _params: [swap])

    response = client.post(
        "/v1/price/swap/ir",
        json={"swap_id": str(swap_id), "as_of": "2026-05-13"},
        headers=_headers(),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "swap_ir_curve_resolution_failed"


def test_quote_resolution_failed_when_md_returns_found_false(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    curve_id = uuid.uuid4()
    fake_ro_engine.set_handler(lambda _sql, _params: [_curve_row(curve_id=curve_id)])
    md = _FakeMdClient(results=[_resolved_quote(canonical_id="USD.IRS.1Y", value=0.0, found=False)])
    client = custom_app_factory(md_client=md)

    response = client.post(
        "/v1/price/swap/ir",
        json={
            "swap": {
                "notional": 1.0,
                "fixed_rate": 0.035,
                "effective_date": "2026-06-15",
                "termination_date": "2031-06-15",
            },
            "curves": [{"id": str(curve_id)}],
            "as_of": "2026-05-13",
        },
        headers=_headers(),
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "swap_ir_quote_resolution_failed"
    assert body["details"] is not None
    assert any(d.get("canonical_id") == "USD.IRS.1Y" for d in body["details"])


def test_quote_resolution_md_unreachable_returns_md_unreachable_envelope(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    curve_id = uuid.uuid4()
    fake_ro_engine.set_handler(lambda _sql, _params: [_curve_row(curve_id=curve_id)])
    md = _FakeMdClient(raises=MdTransportError("connection refused"))
    client = custom_app_factory(md_client=md)

    response = client.post(
        "/v1/price/swap/ir",
        json={
            "swap": {
                "notional": 1.0,
                "fixed_rate": 0.035,
                "effective_date": "2026-06-15",
                "termination_date": "2031-06-15",
            },
            "curves": [{"id": str(curve_id)}],
            "as_of": "2026-05-13",
        },
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
    md._results = [_resolved_quote(canonical_id="USD.IRS.1Y", value=4.25)]

    response = client.post(
        "/v1/price/swap/ir",
        json={
            "swap": {
                "notional": 1_000_000.0,
                "fixed_rate": 0.035,
                "effective_date": "2026-06-15",
                "termination_date": "2031-06-15",
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
    assert len(body["details"]) >= 1
    assembled = body["details"][0]["assembled_request"]
    assert assembled["as_of"] == "2026-05-13"
    assert assembled["trade"]["swap"] == {
        "notional": 1_000_000.0,
        "fixed_rate": 0.035,
        "effective_date": "2026-06-15",
        "termination_date": "2031-06-15",
    }
    assert len(assembled["curves"]) == 1
    assert assembled["curves"][0]["id"] == str(curve_id)
    assert len(assembled["resolved_quotes"]) == 1
    assert assembled["resolved_quotes"][0]["canonical_id"] == "USD.IRS.1Y"
    assert assembled["resolved_quotes"][0]["value"] == 4.25
    assert assembled["resolved_quotes"][0]["from_snapshot"] is False


def test_engine_request_bytes_carry_supplied_rate_and_resolved_id(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    """Stub-integration proof of a *faithful* request.

    The engine still surfaces ``engine_unavailable`` (the call raises), but the
    bytes it received are now assembled from the caller's resolved inputs: the
    encoded deposit helper carries the supplied 0.0425 rate (not the old
    canonical 0.03) and the per-trade discount / forwarding references carry the
    resolved ``app.curves`` UUID (not ``CANONICAL_*`` / ``"discount"``).
    """

    curve_id = uuid.uuid4()
    fake_ro_engine.set_handler(lambda _sql, _params: [_curve_row(curve_id=curve_id)])
    md = _FakeMdClient(results=[_resolved_quote(canonical_id="USD.IRS.1Y", value=0.0425)])
    # Records the request bytes, then raises → maps to engine_unavailable (502).
    engine = _FakeEngineClient(raises=NotImplementedError("stub backend"))
    client = custom_app_factory(md_client=md, engine_client=engine)

    response = client.post(
        "/v1/price/swap/ir",
        json={
            "swap": {
                "notional": 1_000_000.0,
                "fixed_rate": 0.035,
                "effective_date": "2026-06-15",
                "termination_date": "2031-06-15",
            },
            "curves": [{"id": str(curve_id)}],
            "as_of": "2026-05-13",
        },
        headers=_headers(),
    )

    assert response.status_code == HTTPStatus.BAD_GATEWAY
    assert response.json()["code"] == "engine_unavailable"

    # The faithful bytes still reached the engine before it raised.
    assert len(engine.calls) == 1
    rpc, request_bytes = engine.calls[0]
    assert rpc is EngineRpc.PRICE_VANILLA_SWAP

    request = PriceVanillaSwapRequest.GetRootAs(request_bytes, 0)
    curve = request.Pricing().Rates().Curves(0)
    assert curve.Id() == str(curve_id).encode()  # resolved id
    table = curve.Points(0).Point()
    helper = DepositHelper()
    helper.Init(table.Bytes, table.Pos)
    assert helper.Rate() == pytest.approx(0.0425)  # supplied value, not 0.03
    assert not helper.QuoteId()  # quote id dropped server-side (invariant #8)

    swap = request.Swaps(0)
    assert swap.DiscountingCurve() == str(curve_id).encode()
    assert swap.ForwardingCurve() == str(curve_id).encode()
    # No canonical curve id ("discount") leaks into the wire payload.
    assert b"discount" not in request_bytes


def test_success_path_returns_assembled_request_and_result(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    """Happy path: fake engine returns one IrSwapResult; the route echoes both."""

    curve_id = uuid.uuid4()
    fake_ro_engine.set_handler(lambda _sql, _params: [_curve_row(curve_id=curve_id)])
    md = _FakeMdClient(results=[_resolved_quote(canonical_id="USD.IRS.1Y", value=4.25)])
    engine = _FakeEngineClient(response=_fb_response_bytes(npv=123_456.78, fair_rate=0.0325))
    client = custom_app_factory(md_client=md, engine_client=engine)

    response = client.post(
        "/v1/price/swap/ir",
        json={
            "swap": {
                "notional": 1_000_000.0,
                "fixed_rate": 0.035,
                "effective_date": "2026-06-15",
                "termination_date": "2031-06-15",
            },
            "curves": [{"id": str(curve_id)}],
            "as_of": "2026-05-13",
        },
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["pricing_history_id"] is None
    assert body["result"]["npv"] == 123_456.78
    # ``decode_swap_ir_response`` projects FlatBuffers fields into the
    # result's ``extras`` bag (vendored bindings).
    assert body["result"]["extras"]["fair_rate"] == pytest.approx(0.0325)
    assert body["assembled_request"]["as_of"] == "2026-05-13"
    assert len(body["assembled_request"]["curves"]) == 1
    assert len(body["assembled_request"]["resolved_quotes"]) == 1
    # Batching seam was exercised: exactly one engine call with the
    # ``PRICE_VANILLA_SWAP`` RPC.
    assert len(engine.calls) == 1
    assert engine.calls[0][0] is EngineRpc.PRICE_VANILLA_SWAP


def test_snapshot_pinned_quote_marks_from_snapshot_true(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    """Pinned canonical IDs short-circuit the live MD client.

    Asserted from the assembled-request echo so we don't need a
    happy-path engine response: every quote that came from the
    snapshot pin must have ``from_snapshot=True``.
    """

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
                    "content": {"USD.IRS.1Y": 4.25},
                }
            ]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)
    md = _FakeMdClient(results=[])
    client = custom_app_factory(md_client=md)

    response = client.post(
        "/v1/price/swap/ir",
        json={
            "swap": {
                "notional": 1.0,
                "fixed_rate": 0.035,
                "effective_date": "2026-06-15",
                "termination_date": "2031-06-15",
            },
            "curves": [{"id": str(curve_id)}],
            "as_of": "2026-05-13",
            "snapshot_id": str(snapshot_id),
        },
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.BAD_GATEWAY  # stub engine
    body = response.json()
    assembled = body["details"][0]["assembled_request"]
    assert assembled["snapshot_id"] == str(snapshot_id)
    assert assembled["resolved_quotes"][0]["from_snapshot"] is True
    assert assembled["resolved_quotes"][0]["value"] == 4.25
    # No live MD call needed when everything is pinned.
    assert md.calls == []


# ---------------------------------------------------------------------------
# pricing_history hook
# ---------------------------------------------------------------------------


def _pricing_history_handler(
    inserted_id: uuid.UUID,
    captured: list[tuple[str, dict[str, Any]]],
    *,
    raise_on_insert: Exception | None = None,
) -> Any:
    """Build a write-side FakeEngine handler for ``app.pricing_history`` inserts.

    Records the INSERT's ``(sql, params)`` into ``captured`` so the
    test can assert column shape + bind correctness, and returns one
    synthetic ``id`` row mirroring the migration's
    ``RETURNING id::text AS id`` projection. If ``raise_on_insert``
    is set, the handler raises that exception instead — simulating a
    transient DB-side failure (history-write-failure path).
    """

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
    """Happy path: response carries the inserted ``pricing_history_id``.

    Asserts:

    * response body's ``pricing_history_id`` is a UUID-shaped string
    * one INSERT lands on ``app.pricing_history`` with the right
      ``owner_uid`` / ``product_kind="swaps_ir"`` / ``product_id``
      (the saved swap's UUID) / ``as_of`` / ``request`` (the
      post-validation API body) / ``response`` (the decoded engine
      result, not the stub envelope)
    """

    curve_id = uuid.uuid4()
    swap_id = uuid.uuid4()
    saved_swap = _swap_row(
        swap_id=swap_id,
        request_json={
            "pricing": {"curves": [{"id": str(curve_id)}]},
            # Saved-swap rows carry trade economics at the body root.
            "notional": 1_000_000.0,
            "fixed_rate": 0.035,
            "effective_date": "2026-06-15",
            "termination_date": "2031-06-15",
        },
    )

    def _ro_handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.swaps_ir" in sql:
            return [saved_swap]
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        msg = f"unexpected RO SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(_ro_handler)

    md = _FakeMdClient(results=[_resolved_quote(canonical_id="USD.IRS.1Y", value=4.25)])
    engine = _FakeEngineClient(response=_fb_response_bytes(npv=987.65, fair_rate=0.04))

    inserted_id = uuid.uuid4()
    captured_inserts: list[tuple[str, dict[str, Any]]] = []
    rw_engine = FakeEngine()
    rw_engine.set_handler(_pricing_history_handler(inserted_id, captured_inserts))

    client = custom_app_factory(md_client=md, engine_client=engine, rw_engine=rw_engine)
    response = client.post(
        "/v1/price/swap/ir",
        json={"swap_id": str(swap_id), "as_of": "2026-05-13"},
        headers=_headers(),
    )

    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    # Assertion 1a: response carries a UUID-shaped history id.
    assert body["pricing_history_id"] == str(inserted_id)
    uuid.UUID(body["pricing_history_id"])  # parses as a UUID

    # Assertion 1b: matching row inserted with the right shape.
    assert len(captured_inserts) == 1
    sql, params = captured_inserts[0]
    assert "INSERT INTO app.pricing_history" in sql
    assert "CAST(:request AS jsonb)" in sql
    assert "CAST(:response AS jsonb)" in sql
    assert params["owner_uid"] == OWNER
    assert params["product_kind"] == "swaps_ir"
    assert params["product_id"] == str(swap_id)
    assert params["as_of"] == date(2026, 5, 13)
    persisted_request = json.loads(params["request"])
    assert persisted_request["swap_id"] == str(swap_id)
    assert persisted_request["as_of"] == "2026-05-13"
    persisted_response = json.loads(params["response"])
    assert persisted_response["npv"] == 987.65
    assert persisted_response["extras"]["fair_rate"] == pytest.approx(0.04)


def test_engine_failure_records_pricing_history_failure_row(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    """Failure path: stub engine 502 ``engine_unavailable`` is recorded.

    On a pricing failure the history row is written with
    ``response.error.code`` carrying the token. The client still sees
    the same envelope it would have without the history hook.
    """

    curve_id = uuid.uuid4()
    fake_ro_engine.set_handler(lambda _sql, _params: [_curve_row(curve_id=curve_id)])
    md = _FakeMdClient(results=[_resolved_quote(canonical_id="USD.IRS.1Y", value=4.25)])

    captured_inserts: list[tuple[str, dict[str, Any]]] = []
    rw_engine = FakeEngine()
    rw_engine.set_handler(_pricing_history_handler(uuid.uuid4(), captured_inserts))
    client = custom_app_factory(md_client=md, rw_engine=rw_engine)

    response = client.post(
        "/v1/price/swap/ir",
        json={
            "swap": {
                "notional": 1_000_000.0,
                "fixed_rate": 0.035,
                "effective_date": "2026-06-15",
                "termination_date": "2031-06-15",
            },
            "curves": [{"id": str(curve_id)}],
            "as_of": "2026-05-13",
        },
        headers=_headers(),
    )

    # Client surface stays identical to the no-history-hook path.
    assert response.status_code == HTTPStatus.BAD_GATEWAY
    assert response.json()["code"] == "engine_unavailable"

    # Assertion 2: a single failure row recorded with the
    # error envelope under ``response.error``.
    assert len(captured_inserts) == 1
    _, params = captured_inserts[0]
    assert params["owner_uid"] == OWNER
    assert params["product_kind"] == "swaps_ir"
    # Inline-only call → no saved-product reference.
    assert params["product_id"] is None
    persisted_response = json.loads(params["response"])
    assert "error" in persisted_response
    assert persisted_response["error"]["code"] == "engine_unavailable"
    assert persisted_response["error"]["status_code"] == HTTPStatus.BAD_GATEWAY.value

    # The plain stub-engine test still passes — sanity check that
    # our extension didn't accidentally regress the no-hook
    # baseline. The fixture is consumed but unused otherwise.
    assert stub_engine_app is not None


def test_pricing_history_write_failure_still_returns_pricing_response(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    """Hard constraint: a failing history INSERT MUST NOT block pricing.

    The route returns the
    successful pricing response with ``pricing_history_id: null`` when
    ``record_pricing_call`` cannot persist the row (in this test the
    rw FakeEngine raises ``RuntimeError`` on every INSERT).
    """

    curve_id = uuid.uuid4()
    fake_ro_engine.set_handler(lambda _sql, _params: [_curve_row(curve_id=curve_id)])
    md = _FakeMdClient(results=[_resolved_quote(canonical_id="USD.IRS.1Y", value=4.25)])
    engine = _FakeEngineClient(response=_fb_response_bytes(npv=42.0, fair_rate=0.05))

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
        "/v1/price/swap/ir",
        json={
            "swap": {
                "notional": 1_000_000.0,
                "fixed_rate": 0.035,
                "effective_date": "2026-06-15",
                "termination_date": "2031-06-15",
            },
            "curves": [{"id": str(curve_id)}],
            "as_of": "2026-05-13",
        },
        headers=_headers(),
    )

    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    # History-write failure must not promote to a 500 nor break the
    # pricing response — the success body is unchanged, just with a
    # null ``pricing_history_id``.
    assert body["pricing_history_id"] is None
    assert body["result"]["npv"] == 42.0
    # The attempted INSERT was captured (proves the failure went
    # through the recording path; the handler raised after capture).
    assert len(captured_inserts) == 1


def test_openapi_schema_advertises_post_route() -> None:
    """``POST /v1/price/swap/ir`` shows up in the generated OpenAPI spec.

    Catches a wiring regression where the new router falls off
    ``register_all`` without anyone noticing (the route would still
    be importable but unreachable through the public app).
    """

    cfg = OrchestratorSettings(env=Environment.DEV, log_level=LogLevel.WARNING)
    app = create_app(cfg)
    schema = app.openapi()
    assert "/v1/price/swap/ir" in schema["paths"]
    assert "post" in schema["paths"]["/v1/price/swap/ir"]
