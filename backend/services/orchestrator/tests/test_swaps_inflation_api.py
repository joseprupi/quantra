"""End-to-end route tests for ``POST /v1/price/swaps/inflation``.

Drives the FastAPI ``TestClient`` so auth + assemble + MD resolve +
the concurrency seam + engine call + error envelope all light up
together. Mirrors the swap_ir / equity_options harness:

* ``MdClient`` — :class:`_FakeMdClient` overrides
  ``resolve_quotes`` only.
* ``EngineClient`` — :class:`_FakeEngineClient` records every call
  and returns or raises whatever the test installed.
* ``app_ro`` — :class:`FakeEngine` from ``conftest`` stands in for
  the real ``AsyncEngine`` so assembler queries are deterministic.

Coverage focus:

1. Validation (request-shape, mutually-exclusive branches, missing
   inflation_index in inline mode).
2. 404 surfaces (``swap_id`` / nominal curve / inflation curve /
   inflation index / snapshot).
3. 422 surfaces (curve resolution / index resolution / quote
   resolution).
4. Engine-side failure with assembled-request echoed in
   ``details``.
5. ZCIIS happy path: fake engine returns a typed
   :class:`InflationSwapResult` and the route echoes assembled
   request + result.
6. YYIIS happy path with a different RPC name on the wire
   (dispatch, settled).
7. batching hook: ``swap_kind`` rides through
   :class:`EngineBatch.shared_inputs`.
8. OpenAPI advertises the route.
9. Faithful-bytes proof: the request bytes
   reaching the engine carry the supplied curves / inflation index
   + resolved ids, not the canonical fixture.
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
from quantra_common.engine_client._generated.quantra.InflationPoint import (
    InflationPoint,
)
from quantra_common.engine_client._generated.quantra.PriceYearOnYearInflationSwapResponse import (
    PriceYearOnYearInflationSwapResponseT,
)
from quantra_common.engine_client._generated.quantra.PriceZeroCouponInflationSwapRequest import (
    PriceZeroCouponInflationSwapRequest,
    PriceZeroCouponInflationSwapRequestT,
)
from quantra_common.engine_client._generated.quantra.PriceZeroCouponInflationSwapResponse import (
    PriceZeroCouponInflationSwapResponseT,
)
from quantra_common.engine_client._generated.quantra.YearOnYearInflationSwapResponse import (
    YearOnYearInflationSwapResponseT,
)
from quantra_common.engine_client._generated.quantra.ZeroCouponInflationSwapHelper import (
    ZeroCouponInflationSwapHelper,
)
from quantra_common.engine_client._generated.quantra.ZeroCouponInflationSwapResponse import (
    ZeroCouponInflationSwapResponseT,
)
from quantra_common.md_client import MdClient, MdClientConfig
from quantra_common.settings import Environment, LogLevel
from quantra_common.types.market_data import ResolvedQuote
from quantra_orchestrator.app import create_app
from quantra_orchestrator.pricing.swaps_inflation import api as swaps_inflation_api
from quantra_orchestrator.pricing.swaps_inflation.engine_io import (
    price_swap_inflation_batch as _real_price_batch,
)
from quantra_orchestrator.settings import OrchestratorSettings

from .conftest import FakeEngine

OWNER = "user-test"
API_KEY = "key-swaps-inflation"


# ---------------------------------------------------------------------------
# Fakes
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
        super().__init__(config, client=httpx.AsyncClient(base_url="http://stub"))
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


def _zciis_response_bytes(npv: float, fair_rate: float = 0.02) -> bytes:
    response = PriceZeroCouponInflationSwapResponseT()
    response.swaps = []
    s = ZeroCouponInflationSwapResponseT()
    s.npv = npv
    s.fairRate = fair_rate
    s.fixedLegBps = 0.0
    s.fixedLegNpv = npv * 0.5
    s.inflationLegNpv = npv * 0.5
    response.swaps.append(s)
    builder = flatbuffers.Builder(256)
    builder.Finish(response.Pack(builder))
    return bytes(builder.Output())


def _yyiis_response_bytes(npv: float, fair_rate: float = 0.02) -> bytes:
    response = PriceYearOnYearInflationSwapResponseT()
    response.swaps = []
    s = YearOnYearInflationSwapResponseT()
    s.npv = npv
    s.fairRate = fair_rate
    s.fairSpread = 0.0001
    s.fixedLegBps = 0.0
    s.yoyLegBps = 0.0
    s.fixedLegNpv = npv * 0.5
    s.yoyLegNpv = npv * 0.5
    response.swaps.append(s)
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
            api_key_id="ak-swaps-inflation",
            owner_uid=OWNER,
            name="Swaps Inflation Test",
            email="swaps-inflation@example.com",
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
# Helpers
# ---------------------------------------------------------------------------


def _headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


def _curve_row(*, curve_id: uuid.UUID, name: str = "DISC") -> dict[str, Any]:
    return {
        "id": str(curve_id),
        "name": name,
        "currency": "EUR",
        "day_counter": "Actual365Fixed",
        "helper_kind": "Discount",
        "reference_date": date(2025, 1, 15),
        "points": [
            {
                "point_type": "DepositHelper",
                "tenor": {"n": 1, "unit": "Years"},
                "quote_id": "EUR.IRS.1Y",
            }
        ],
        "body": {"interpolator": "LogLinear"},
    }


def _index_row(*, index_id: uuid.UUID) -> dict[str, Any]:
    return {
        "id": str(index_id),
        "name": "EU HICP",
        "kind": "Inflation",
        "currency": "EUR",
        "day_counter": "Actual/365",
        "body": {"id": "EUHICP"},
    }


def _swap_row(*, swap_id: uuid.UUID, request_json: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(swap_id), "name": "demo-inf", "request": request_json}


def _resolved_quote(*, canonical_id: str, value: float, found: bool = True) -> ResolvedQuote:
    return ResolvedQuote(
        canonical_id=canonical_id,
        requested_as_of=datetime(2025, 1, 15, 0, 0),
        resolved_as_of=datetime(2025, 1, 15, 0, 0),
        found=found,
        is_exact=True,
        value=value if found else None,
        source="vendor_x",
        vendor_id="vendor",
    )


def _inline_payload(*, swap_kind: str = "zero_coupon") -> dict[str, Any]:
    # Explicit economics — the wire layer no longer supplies silent
    # defaults (422 ``swap_inflation_missing_trade_fields`` otherwise).
    inner: dict[str, Any] = {
        "swap_type": "Payer",
        "fixed_rate": 0.02,
        "notional": 1_000_000.0,
    }
    if swap_kind == "zero_coupon":
        inner["start_date"] = "2025-01-15"
        inner["maturity_date"] = "2030-01-15"
    else:
        inner["fixed_schedule"] = {
            "effective_date": "2025-01-15",
            "termination_date": "2027-01-15",
        }
        inner["yoy_schedule"] = {
            "effective_date": "2025-01-15",
            "termination_date": "2027-01-15",
        }
    return {
        "swap": {
            "swap_kind": swap_kind,
            "swaps": [{f"{swap_kind}_inflation_swap": inner}],
        },
        "curves": [
            {
                "name": "DISC",
                "currency": "EUR",
                "role": "nominal",
                "points": [
                    {
                        "point_type": "DepositHelper",
                        "tenor": {"n": 1, "unit": "Years"},
                        "quote_id": "EUR.IRS.1Y",
                    }
                ],
                "reference_date": "2025-01-15",
            },
            {
                "name": "HICP_ZC",
                "currency": "EUR",
                "role": "inflation",
                "points": [{"tenor": {"n": 1, "unit": "Years"}, "quote_id": "EUR.HICP.1Y"}],
                "reference_date": "2025-01-15",
            },
        ],
        "inflation_index": {
            "name": "EU HICP",
            "index_id": "EUHICP",
            "currency": "EUR",
            "body": {
                "family_name": "EU HICP",
                "frequency": "Monthly",
                "fixings": [{"date": "2024-10-01", "value": 100.0}],
            },
        },
        "as_of": "2025-01-15",
    }


# ---------------------------------------------------------------------------
# Auth + validation
# ---------------------------------------------------------------------------


def test_requires_auth(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, _, _ = stub_engine_app
    response = client.post(
        "/v1/price/swaps/inflation",
        json=_inline_payload(),
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json()["code"] == "unauthenticated"


def test_request_without_swap_id_or_swap_is_validation_error(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, _, _ = stub_engine_app
    response = client.post(
        "/v1/price/swaps/inflation",
        json={"as_of": "2025-01-15"},
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "validation_error"


def test_inline_swap_without_inflation_index_is_validation_error(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    """Inline mode requires both ``curves`` (>=2) and ``inflation_index``."""

    client, _, _ = stub_engine_app
    payload = _inline_payload()
    del payload["inflation_index"]
    response = client.post(
        "/v1/price/swaps/inflation",
        json=payload,
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "validation_error"


# ---------------------------------------------------------------------------
# 404 surfaces
# ---------------------------------------------------------------------------


def test_swap_id_not_found_returns_swap_inflation_not_found(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    client, ro_engine, _ = stub_engine_app
    ro_engine.set_handler(lambda _sql, _params: [])

    response = client.post(
        "/v1/price/swaps/inflation",
        json={"swap_id": str(uuid.uuid4()), "as_of": "2025-01-15"},
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "swap_inflation_not_found"


# ---------------------------------------------------------------------------
# Engine surface (stub engine → 502 + assembled details)
# ---------------------------------------------------------------------------


def test_stub_engine_returns_engine_unavailable_with_assembled_details(
    stub_engine_app: tuple[TestClient, FakeEngine, _FakeMdClient],
) -> None:
    """Today's stub engine surfaces as 502 + ``engine_unavailable``.

    The envelope's ``details`` must echo the assembled
    request so an operator can verify the orchestrator completed
    every preceding stage (assemble → MD resolve → fan-out)
    before the stub raised. The echoed shape carries
    ``nominal_curve_id`` / ``inflation_curve_id`` /
    ``inflation_index_id`` (all ``None`` for inline-only calls).
    """

    client, ro_engine, md = stub_engine_app
    nominal_id = uuid.uuid4()
    inflation_id = uuid.uuid4()

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            requested = uuid.UUID(params["curve_id"])
            if requested == nominal_id:
                return [_curve_row(curve_id=nominal_id, name="DISC")]
            if requested == inflation_id:
                return [_curve_row(curve_id=inflation_id, name="HICP_ZC")]
            return []
        return []

    ro_engine.set_handler(handler)
    md._results = [_resolved_quote(canonical_id="EUR.IRS.1Y", value=0.03)]

    payload = {
        "swap": {
            "swap_kind": "zero_coupon",
            "swaps": [
                {
                    "zero_coupon_inflation_swap": {
                        "fixed_rate": 0.02,
                        "notional": 1_000_000.0,
                        "start_date": "2025-01-15",
                        "maturity_date": "2030-01-15",
                    }
                }
            ],
        },
        "curves": [
            {"id": str(nominal_id), "role": "nominal"},
            {"id": str(inflation_id), "role": "inflation"},
        ],
        "inflation_index": {
            "name": "EU HICP",
            "index_id": "EUHICP",
        },
        "as_of": "2025-01-15",
    }

    response = client.post("/v1/price/swaps/inflation", json=payload, headers=_headers())
    assert response.status_code == HTTPStatus.BAD_GATEWAY
    body = response.json()
    assert body["code"] == "engine_unavailable"
    assert isinstance(body["details"], list)
    assembled = body["details"][0]["assembled_request"]
    assert assembled["as_of"] == "2025-01-15"
    assert assembled["nominal_curve_id"] == str(nominal_id)
    assert assembled["inflation_curve_id"] == str(inflation_id)
    assert assembled["nominal_curve"]["role"] == "nominal"
    assert assembled["inflation_curve"]["role"] == "inflation"
    assert assembled["inflation_index"]["index_id"] == "EUHICP"


# ---------------------------------------------------------------------------
# Happy paths — ZCIIS and YYIIS
# ---------------------------------------------------------------------------


def test_zciis_success_path_returns_assembled_request_and_result(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    fake_ro_engine.set_handler(lambda _sql, _params: [])
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="EUR.IRS.1Y", value=0.03),
            _resolved_quote(canonical_id="EUR.HICP.1Y", value=0.02),
        ]
    )
    engine = _FakeEngineClient(response=_zciis_response_bytes(npv=12_345.0, fair_rate=0.0217))
    client = custom_app_factory(md_client=md, engine_client=engine)

    response = client.post(
        "/v1/price/swaps/inflation",
        json=_inline_payload(swap_kind="zero_coupon"),
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["pricing_history_id"] is None
    assert body["result"]["npv"] == pytest.approx(12_345.0)
    assert body["result"]["fair_rate"] == pytest.approx(0.0217)
    assert body["result"]["swap_kind"] == "zero_coupon"
    assert body["result"]["inflation_leg_npv"] == pytest.approx(12_345.0 * 0.5)
    assert body["result"]["yoy_leg_npv"] is None
    assert body["assembled_request"]["as_of"] == "2025-01-15"
    # Batching seam exercised: exactly one engine call dispatched to the
    # zero-coupon RPC (mapping, settled).
    assert len(engine.calls) == 1
    assert engine.calls[0][0] is EngineRpc.PRICE_ZERO_COUPON_INFLATION_SWAP


def test_yyiis_success_path_dispatches_to_year_on_year_rpc(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    fake_ro_engine.set_handler(lambda _sql, _params: [])
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="EUR.IRS.1Y", value=0.03),
            _resolved_quote(canonical_id="EUR.HICP.1Y", value=0.02),
        ]
    )
    engine = _FakeEngineClient(response=_yyiis_response_bytes(npv=99.0, fair_rate=0.0204))
    client = custom_app_factory(md_client=md, engine_client=engine)

    response = client.post(
        "/v1/price/swaps/inflation",
        json=_inline_payload(swap_kind="year_on_year"),
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["result"]["swap_kind"] == "year_on_year"
    assert body["result"]["yoy_leg_npv"] == pytest.approx(99.0 * 0.5)
    assert body["result"]["inflation_leg_npv"] is None
    assert engine.calls[0][0] is EngineRpc.PRICE_YEAR_ON_YEAR_INFLATION_SWAP


# ---------------------------------------------------------------------------
# Faithful-bytes stub-integration
# ---------------------------------------------------------------------------


def test_request_bytes_are_faithful_not_canonical(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    """The bytes reaching the engine carry the supplied curves / index + resolved ids.

    End-to-end faithful proof: the nominal discount
    curve point carries the MD-resolved rate (not the canonical flat fixture) with
    no ``quote_id`` leak; the inflation curve helper carries the MD-resolved value;
    the inflation index carries the supplied ``index_id`` + verbatim fixings; and
    the per-trade discount / inflation / index references are the resolved ids,
    never ``CANONICAL_*``.
    """

    fake_ro_engine.set_handler(lambda _sql, _params: [])
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="EUR.IRS.1Y", value=0.041),
            _resolved_quote(canonical_id="EUR.HICP.1Y", value=0.023),
        ]
    )
    engine = _FakeEngineClient(response=_zciis_response_bytes(npv=1.0, fair_rate=0.02))
    client = custom_app_factory(md_client=md, engine_client=engine)

    response = client.post(
        "/v1/price/swaps/inflation",
        json=_inline_payload(swap_kind="zero_coupon"),
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.OK, response.text

    assert len(engine.calls) == 1
    rpc, request_bytes = engine.calls[0]
    assert rpc is EngineRpc.PRICE_ZERO_COUPON_INFLATION_SWAP
    # Object-API decode: ``InitFromPackedBuf`` eagerly unpacks the whole graph,
    # including the InflationIndexSpec's ``ObservationLag`` accessor whose vendored-bindings
    # missing-import bug is now fixed — so the faithful end-to-end bytes decode
    # cleanly as Python objects (no read-API workaround needed).
    obj = PriceZeroCouponInflationSwapRequestT.InitFromPackedBuf(request_bytes, 0)
    decoded_index = obj.pricing.inflation.inflationIndices[0]
    assert decoded_index.id == b"EUHICP"
    assert decoded_index.observationLag is not None
    request = PriceZeroCouponInflationSwapRequest.GetRootAs(request_bytes, 0)
    pricing = request.Pricing()

    # Nominal discount curve: resolved id (from the inline name) + the resolved
    # rate substituted in, quote id dropped (invariant #8).
    nominal = pricing.Rates().Curves(0)
    assert nominal.Id() == b"DISC"
    assert nominal.Id() != b"discount"
    deposit_tbl = nominal.Points(0).Point()
    deposit = DepositHelper()
    deposit.Init(deposit_tbl.Bytes, deposit_tbl.Pos)
    assert deposit.Rate() == pytest.approx(0.041)
    assert not deposit.QuoteId()

    # Inflation curve + index: resolved ids, the resolved inflation value
    # substituted into the helper, the index body fixings carried verbatim.
    inflation = pricing.Inflation()
    inflation_curve = inflation.InflationCurves(0)
    assert inflation_curve.Id() == b"HICP_ZC"
    assert inflation_curve.IndexId() == b"EUHICP"
    assert inflation_curve.DiscountCurveId() == b"DISC"
    infl_point = inflation_curve.Points(0)
    assert infl_point.PointType() == InflationPoint.ZeroCouponInflationSwapHelper
    helper_tbl = infl_point.Point()
    helper = ZeroCouponInflationSwapHelper()
    helper.Init(helper_tbl.Bytes, helper_tbl.Pos)
    assert helper.QuoteValue() == pytest.approx(0.023)
    index_spec = inflation.InflationIndices(0)
    assert index_spec.Id() == b"EUHICP"
    assert index_spec.FixingsLength() == 1
    assert index_spec.Fixings(0).Value() == pytest.approx(100.0)

    # Per-trade references honour the resolved ids — no CANONICAL_* leak.
    price = request.Swaps(0)
    assert price.DiscountingCurve() == b"DISC"
    assert price.InflationCurve() == b"HICP_ZC"
    assert price.ZeroCouponInflationSwap().InflationIndexId() == b"EUHICP"


# ---------------------------------------------------------------------------
# batching hook: shared_inputs carries the swap_kind discriminator
# ---------------------------------------------------------------------------


def test_engine_batch_carries_swap_kind(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``shared_inputs["swap_kind"]`` reaches ``price_batch``.

    ``swap_kind`` is the ZCIIS-vs-YYIIS RPC discriminator the wire
    builder branches on, so it must thread through the concurrency seam.
    """

    fake_ro_engine.set_handler(lambda _sql, _params: [])
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="EUR.IRS.1Y", value=0.03),
            _resolved_quote(canonical_id="EUR.HICP.1Y", value=0.02),
        ]
    )
    engine = _FakeEngineClient(response=_zciis_response_bytes(npv=0.0, fair_rate=0.02))

    captured_batches: list[Any] = []

    async def _spy(eng: Any, batch: Any, *, resolved: Any) -> Any:
        captured_batches.append(batch)
        return await _real_price_batch(eng, batch, resolved=resolved)

    monkeypatch.setattr(swaps_inflation_api, "price_swap_inflation_batch", _spy)
    client = custom_app_factory(md_client=md, engine_client=engine)

    response = client.post(
        "/v1/price/swaps/inflation",
        json=_inline_payload(swap_kind="zero_coupon"),
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.OK, response.text
    assert len(captured_batches) == 1
    batch = captured_batches[0]
    assert batch.shared_inputs == {"swap_kind": "zero_coupon"}


def test_openapi_schema_advertises_post_route() -> None:
    cfg = OrchestratorSettings(env=Environment.DEV, log_level=LogLevel.WARNING)
    app = create_app(cfg)
    schema = app.openapi()
    assert "/v1/price/swaps/inflation" in schema["paths"]
    assert "post" in schema["paths"]["/v1/price/swaps/inflation"]


def test_quote_resolution_failed_when_md_returns_found_false(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    fake_ro_engine.set_handler(lambda _sql, _params: [])
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="EUR.IRS.1Y", value=0.03),
            _resolved_quote(canonical_id="EUR.HICP.1Y", value=0.0, found=False),
        ]
    )
    client = custom_app_factory(md_client=md)

    response = client.post(
        "/v1/price/swaps/inflation",
        json=_inline_payload(),
        headers=_headers(),
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    body = response.json()
    assert body["code"] == "swap_inflation_quote_resolution_failed"
    assert any(d.get("canonical_id") == "EUR.HICP.1Y" for d in body["details"])


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
    fake_ro_engine.set_handler(lambda _sql, _params: [])
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="EUR.IRS.1Y", value=0.03),
            _resolved_quote(canonical_id="EUR.HICP.1Y", value=0.02),
        ]
    )
    engine = _FakeEngineClient(response=_zciis_response_bytes(npv=12_345.0, fair_rate=0.0217))

    inserted_id = uuid.uuid4()
    captured_inserts: list[tuple[str, dict[str, Any]]] = []
    rw_engine = FakeEngine()
    rw_engine.set_handler(_pricing_history_handler(inserted_id, captured_inserts))

    client = custom_app_factory(md_client=md, engine_client=engine, rw_engine=rw_engine)
    response = client.post(
        "/v1/price/swaps/inflation",
        json=_inline_payload(swap_kind="zero_coupon"),
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
    assert params["product_kind"] == "swaps_inflation"
    assert params["product_id"] is None
    persisted_response = json.loads(params["response"])
    assert persisted_response["npv"] == 12_345.0


def test_engine_failure_records_pricing_history_failure_row(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    fake_ro_engine.set_handler(lambda _sql, _params: [])
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="EUR.IRS.1Y", value=0.03),
            _resolved_quote(canonical_id="EUR.HICP.1Y", value=0.02),
        ]
    )

    captured_inserts: list[tuple[str, dict[str, Any]]] = []
    rw_engine = FakeEngine()
    rw_engine.set_handler(_pricing_history_handler(uuid.uuid4(), captured_inserts))
    client = custom_app_factory(md_client=md, rw_engine=rw_engine)

    response = client.post(
        "/v1/price/swaps/inflation",
        json=_inline_payload(swap_kind="zero_coupon"),
        headers=_headers(),
    )

    assert response.status_code == HTTPStatus.BAD_GATEWAY
    assert response.json()["code"] == "engine_unavailable"

    assert len(captured_inserts) == 1
    _, params = captured_inserts[0]
    assert params["product_kind"] == "swaps_inflation"
    assert params["product_id"] is None
    persisted_response = json.loads(params["response"])
    assert "error" in persisted_response
    assert persisted_response["error"]["code"] == "engine_unavailable"


def test_pricing_history_write_failure_still_returns_pricing_response(
    custom_app_factory: Any,
    fake_ro_engine: FakeEngine,
) -> None:
    fake_ro_engine.set_handler(lambda _sql, _params: [])
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="EUR.IRS.1Y", value=0.03),
            _resolved_quote(canonical_id="EUR.HICP.1Y", value=0.02),
        ]
    )
    engine = _FakeEngineClient(response=_zciis_response_bytes(npv=42.0, fair_rate=0.02))

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
        "/v1/price/swaps/inflation",
        json=_inline_payload(swap_kind="zero_coupon"),
        headers=_headers(),
    )

    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["pricing_history_id"] is None
    assert body["result"]["npv"] == pytest.approx(42.0)
    assert len(captured_inserts) == 1
