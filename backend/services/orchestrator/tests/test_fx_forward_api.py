"""End-to-end route tests for ``POST /v1/price/fx-forward``.

Same harness as ``test_swap_ir_api``: ``TestClient`` over the real app with
a fake MD client, a fake engine client (canned ``PriceFxForwardResponse``
bytes) and the ``FakeEngine`` standing in for ``app_ro`` / ``app_rw``.

1. Validation (mutually exclusive branches; inline needs curves + spots).
2. Not-found: ``fx_forward_id`` → 404 ``fx_forward_not_found``.
3. Resolution 422s: missing trade fields, a spot ref that names nothing.
4. Success inline: curves / spot / indices by reference load from ``app.*``,
   the engine request carries ``Pricing.fx`` + the trade, the route echoes
   the assembled request + result, one ``PRICE_FX_FORWARD`` engine call.
5. Success by reference: the saved row's ``pricing`` lists drive the
   resolution and the pricing-history row carries ``product_kind
   fx_forwards`` + the trade id.
6. The FX spot ``quote_id`` is resolved through MD (invariant #8) and the
   resolved value lands on the wire; a miss is a 422.
"""

from __future__ import annotations

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
from quantra_common.engine_client._generated.quantra.enums.Calendar import Calendar
from quantra_common.engine_client._generated.quantra.FxForwardResponse import (
    FxForwardResponseT,
)
from quantra_common.engine_client._generated.quantra.PriceFxForwardRequest import (
    PriceFxForwardRequest,
)
from quantra_common.engine_client._generated.quantra.PriceFxForwardResponse import (
    PriceFxForwardResponseT,
)
from quantra_common.md_client import MdClient, MdClientConfig
from quantra_common.settings import Environment, LogLevel
from quantra_common.types.market_data import ResolvedQuote
from quantra_orchestrator.app import create_app
from quantra_orchestrator.settings import OrchestratorSettings

from .conftest import FakeEngine

OWNER = "user-test"
API_KEY = "key-fx-forward"
EUR_CURVE = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
USD_CURVE = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000002")
SPOT = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000001")
INDEX = uuid.UUID("cccccccc-0000-0000-0000-000000000001")
TRADE = uuid.UUID("dddddddd-0000-0000-0000-000000000001")
SPOT_QUOTE = "EUR.FX.ECB.EURUSD.1D.SPOT"


class _FakeMdClient(MdClient):
    def __init__(self, *, results: list[ResolvedQuote] | None = None) -> None:
        config = MdClientConfig(base_url="http://stub", timeout_s=1.0, max_retries=0)
        super().__init__(config, client=httpx.AsyncClient(base_url="http://stub"))
        self.calls: list[tuple[list[str], Any]] = []
        self._results = results

    async def resolve_quotes(
        self, canonical_ids: list[str], as_of: Any, *, snapshot_version: str | None = None
    ) -> list[ResolvedQuote]:
        self.calls.append((list(canonical_ids), as_of))
        return list(self._results or [])


class _FakeEngineClient(EngineClient):
    def __init__(self, *, response: bytes | None = None, raises: Exception | None = None) -> None:
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


@pytest.fixture
def settings() -> OrchestratorSettings:
    return OrchestratorSettings(env=Environment.DEV, log_level=LogLevel.WARNING, build_sha="t")


@pytest.fixture
def auth_lookup() -> ApiKeyLookup:
    record = ApiKeyRecord(
        api_key_id="ak-fx",
        owner_uid=OWNER,
        name="FX Test",
        email="fx@example.com",
        tier="free",
        active=True,
    )

    async def _lookup(key: str) -> ApiKeyRecord | None:
        return record if key == API_KEY else None

    return _lookup


@pytest.fixture
def firebase_verifier() -> FirebaseTokenVerifier:
    def _verify(_token: str) -> dict[str, Any]:
        msg = "no firebase in this suite"
        raise ValueError(msg)

    return _verify


@pytest.fixture
def app_factory(
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
        app: FastAPI = create_app(
            settings,
            api_key_lookup=auth_lookup,
            firebase_verifier=firebase_verifier,
            app_ro_engine=cast(AsyncEngine, fake_ro_engine),
            app_rw_engine=(cast(AsyncEngine, rw_engine) if rw_engine is not None else None),
            md_client=md_client if md_client is not None else _FakeMdClient(results=[]),
            engine_client=engine_client,
        )
        tc = TestClient(app, raise_server_exceptions=False)
        tc.__enter__()
        opened.append(tc)
        return tc

    try:
        yield _build
    finally:
        for tc in opened:
            tc.__exit__(None, None, None)


def _headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


def _deposit(rate: float) -> dict[str, Any]:
    return {
        "point_type": "DepositHelper",
        "point": {
            "tenor": {"n": 1, "unit": "Years"},
            "rate": rate,
            "fixing_days": 2,
            "calendar": "TARGET",
            "business_day_convention": "ModifiedFollowing",
            "day_counter": "Actual360",
        },
    }


def _curve_row(curve_id: uuid.UUID, name: str, rate: float) -> dict[str, Any]:
    return {
        "id": str(curve_id),
        "name": name,
        "currency": None,
        "day_counter": "Actual365Fixed",
        "helper_kind": "Discount",
        "reference_date": date(2025, 1, 15),
        "points": [_deposit(rate)],
        "body": {"interpolator": "LogLinear", "bootstrap_trait": "Discount"},
    }


def _spot_row(*, quote_id: str | None = None) -> dict[str, Any]:
    return {
        "id": str(SPOT),
        "name": "EURUSD",
        "base_currency": "EUR",
        "quote_currency": "USD",
        "rate": None if quote_id else 1.085,
        "quote_id": quote_id,
        "spot_days": 2,
        "business_day_convention": "ModifiedFollowing",
        "settlement_calendars": ["TARGET", "UnitedStatesSettlement"],
    }


def _index_row() -> dict[str, Any]:
    return {
        "id": str(INDEX),
        "name": "USD_SOFR",
        "kind": "Overnight",
        "currency": "USD",
        "calendar": "UnitedStatesGovernmentBond",
        "day_counter": "Actual360",
        "body": {"tenor": {"n": 0, "unit": "Days"}, "fixing_days": 0, "end_of_month": True},
    }


def _trade_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "base_currency": "EUR",
        "quote_currency": "USD",
        "direction": "BuyBase",
        "base_notional": 10_000_000.0,
        "forward_rate": 1.095,
        "maturity_date": "2025-07-17",
        "fx_spot": str(SPOT),
        "base_discounting_curve": str(EUR_CURVE),
        "quote_discounting_curve": str(USD_CURVE),
        "reporting_currency": "USD",
    }
    body.update(overrides)
    return body


def _ro_handler(*, spot_quote_id: str | None = None, saved: dict[str, Any] | None = None) -> Any:
    def _handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            rows = {str(EUR_CURVE): _curve_row(EUR_CURVE, "EUR-DISC", 0.03)}
            rows[str(USD_CURVE)] = _curve_row(USD_CURVE, "USD-DISC", 0.05)
            row = rows.get(params["row_id"])
            return [row] if row else []
        if "FROM app.fx_spots" in sql:
            return [_spot_row(quote_id=spot_quote_id)] if params["row_id"] == str(SPOT) else []
        if "FROM app.indices" in sql:
            return [_index_row()] if params["row_id"] == str(INDEX) else []
        if "FROM app.fx_forwards" in sql:
            if saved is not None and params["fx_forward_id"] == str(TRADE):
                return [{"id": str(TRADE), "name": "fwd-1", "request": saved}]
            return []
        return []

    return _handler


def _engine_response(npv: float = 30201.2) -> bytes:
    response = PriceFxForwardResponseT()
    row = FxForwardResponseT()
    row.npv = npv
    row.fairForwardRate = 1.0981
    row.forwardPoints = 0.0131
    row.baseLegPv = 9_861_310.0
    row.quoteLegPv = -10_667_900.0
    row.spotDate = "2025-01-17"
    row.maturityDate = "2025-07-17"
    row.fxSpotUsed = 1.085
    response.fxForwards = [row]
    builder = flatbuffers.Builder(256)
    builder.Finish(response.Pack(builder))
    return bytes(builder.Output())


def _resolved_quote(canonical_id: str, value: float, *, found: bool = True) -> ResolvedQuote:
    return ResolvedQuote(
        canonical_id=canonical_id,
        requested_as_of=datetime(2025, 1, 15),
        resolved_as_of=datetime(2025, 1, 15),
        found=found,
        is_exact=True,
        value=value if found else None,
        source="ecb",
        vendor_id="ecb",
    )


def _inline_request(**trade_overrides: Any) -> dict[str, Any]:
    return {
        "fx_forward": _trade_body(**trade_overrides),
        "curves": [{"id": str(EUR_CURVE)}, {"id": str(USD_CURVE)}],
        "fx_spots": [{"id": str(SPOT)}],
        "indices": [{"id": str(INDEX)}],
        "as_of": "2025-01-15",
    }


# ---------------------------------------------------------------------------


def test_requires_auth(app_factory: Any) -> None:
    client = app_factory()
    response = client.post("/v1/price/fx-forward", json=_inline_request())
    assert response.status_code == HTTPStatus.UNAUTHORIZED


@pytest.mark.parametrize(
    "payload",
    [
        {"as_of": "2025-01-15"},
        {"fx_forward_id": str(TRADE), "fx_forward": {}, "as_of": "2025-01-15"},
        {"fx_forward": _trade_body(), "fx_spots": [{"id": str(SPOT)}], "as_of": "2025-01-15"},
        {"fx_forward": _trade_body(), "curves": [{"id": str(EUR_CURVE)}], "as_of": "2025-01-15"},
    ],
)
def test_request_shape_validation(app_factory: Any, payload: dict[str, Any]) -> None:
    client = app_factory()
    response = client.post("/v1/price/fx-forward", json=payload, headers=_headers())
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "validation_error"


def test_fx_forward_id_not_found(app_factory: Any, fake_ro_engine: FakeEngine) -> None:
    fake_ro_engine.set_handler(_ro_handler())
    client = app_factory()
    response = client.post(
        "/v1/price/fx-forward",
        json={"fx_forward_id": str(TRADE), "as_of": "2025-01-15"},
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "fx_forward_not_found"


def test_missing_trade_fields_422(app_factory: Any, fake_ro_engine: FakeEngine) -> None:
    fake_ro_engine.set_handler(_ro_handler())
    engine = _FakeEngineClient(response=_engine_response())
    client = app_factory(engine_client=engine)
    payload = _inline_request()
    payload["fx_forward"] = {"base_currency": "EUR"}
    response = client.post("/v1/price/fx-forward", json=payload, headers=_headers())
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    body = response.json()
    assert body["code"] == "fx_forward_missing_trade_fields"
    assert {"forward_rate", "maturity_date", "base_notional"} <= {
        d["field"] for d in body["details"]
    }
    assert engine.calls == []


def test_unknown_spot_ref_422(app_factory: Any, fake_ro_engine: FakeEngine) -> None:
    fake_ro_engine.set_handler(_ro_handler())
    client = app_factory(engine_client=_FakeEngineClient(response=_engine_response()))
    response = client.post(
        "/v1/price/fx-forward", json=_inline_request(fx_spot="nope"), headers=_headers()
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "fx_forward_fx_spot_resolution_failed"


def test_success_inline_by_reference_entities(app_factory: Any, fake_ro_engine: FakeEngine) -> None:
    fake_ro_engine.set_handler(_ro_handler())
    engine = _FakeEngineClient(response=_engine_response())
    client = app_factory(engine_client=engine)
    response = client.post("/v1/price/fx-forward", json=_inline_request(), headers=_headers())
    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["result"]["npv"] == pytest.approx(30201.2)
    assert body["result"]["reporting_currency"] == "USD"
    assert body["result"]["fair_forward_rate"] == pytest.approx(1.0981)
    assert body["result"]["spot_date"] == "2025-01-17"
    assert body["result"]["maturity_date"] == "2025-07-17"
    assert [c["name"] for c in body["assembled_request"]["curves"]] == ["EUR-DISC", "USD-DISC"]
    assert body["assembled_request"]["fx_spots"][0]["rate"] == pytest.approx(1.085)
    assert body["assembled_request"]["indices"][0]["name"] == "USD_SOFR"
    assert body["pricing_history_id"] is None

    assert len(engine.calls) == 1
    rpc, raw = engine.calls[0]
    assert rpc is EngineRpc.PRICE_FX_FORWARD
    request = PriceFxForwardRequest.GetRootAs(raw, 0)
    assert request.Pricing().Fx().Spots(0).Id() == str(SPOT).encode()
    assert request.Pricing().Fx().Spots(0).Rate() == pytest.approx(1.085)
    indices = request.Pricing().Rates()
    by_id = {indices.Indices(i).Id(): indices.Indices(i) for i in range(indices.IndicesLength())}
    assert by_id[str(INDEX).encode()].Calendar() == Calendar.UnitedStatesGovernmentBond
    row = request.FxForwards(0)
    assert row.BaseDiscountingCurve() == str(EUR_CURVE).encode()
    assert row.QuoteDiscountingCurve() == str(USD_CURVE).encode()
    assert row.FxSpot() == str(SPOT).encode()


def test_success_by_reference_records_history(
    app_factory: Any, fake_ro_engine: FakeEngine, fake_rw_engine: FakeEngine
) -> None:
    saved = {
        **_trade_body(),
        "pricing": {
            "curves": [{"id": str(EUR_CURVE)}, {"id": str(USD_CURVE)}],
            "fx_spots": [{"id": str(SPOT)}],
            "indices": [{"id": str(INDEX)}],
        },
    }
    fake_ro_engine.set_handler(_ro_handler(saved=saved))
    history_id = uuid.uuid4()
    writes: list[dict[str, Any]] = []

    def _rw(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "INSERT INTO app.pricing_history" in sql:
            writes.append(params)
            return [{"id": history_id}]
        return []

    fake_rw_engine.set_handler(_rw)
    client = app_factory(
        engine_client=_FakeEngineClient(response=_engine_response()), rw_engine=fake_rw_engine
    )
    response = client.post(
        "/v1/price/fx-forward",
        json={"fx_forward_id": str(TRADE), "as_of": "2025-01-15"},
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["pricing_history_id"] == str(history_id)
    assert body["assembled_request"]["trade"]["fx_forward_id"] == str(TRADE)
    assert len(writes) == 1
    assert writes[0]["product_kind"] == "fx_forwards"
    assert writes[0]["product_id"] == str(TRADE)


def test_spot_quote_id_resolved_through_md(app_factory: Any, fake_ro_engine: FakeEngine) -> None:
    fake_ro_engine.set_handler(_ro_handler(spot_quote_id=SPOT_QUOTE))
    md = _FakeMdClient(results=[_resolved_quote(SPOT_QUOTE, 1.0912)])
    engine = _FakeEngineClient(response=_engine_response())
    client = app_factory(md_client=md, engine_client=engine)
    response = client.post("/v1/price/fx-forward", json=_inline_request(), headers=_headers())
    assert response.status_code == HTTPStatus.OK, response.text
    assert md.calls == [([SPOT_QUOTE], date(2025, 1, 15))]
    body = response.json()
    assert body["assembled_request"]["resolved_quotes"][0]["canonical_id"] == SPOT_QUOTE
    request = PriceFxForwardRequest.GetRootAs(engine.calls[0][1], 0)
    assert request.Pricing().Fx().Spots(0).Rate() == pytest.approx(1.0912)
    assert request.Pricing().Fx().Spots(0).QuoteId() is None

    missing = _FakeMdClient(results=[_resolved_quote(SPOT_QUOTE, 0.0, found=False)])
    client2 = app_factory(
        md_client=missing, engine_client=_FakeEngineClient(response=_engine_response())
    )
    response2 = client2.post("/v1/price/fx-forward", json=_inline_request(), headers=_headers())
    assert response2.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response2.json()["code"] == "fx_forward_quote_resolution_failed"


def test_openapi_advertises_route_and_entities(app_factory: Any) -> None:
    client = app_factory()
    paths = client.get("/openapi.json").json()["paths"]
    assert "/v1/price/fx-forward" in paths
    assert "/v1/fx-spots" in paths
    assert "/v1/fx-forwards" in paths
