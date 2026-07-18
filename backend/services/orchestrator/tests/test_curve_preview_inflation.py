"""Route tests for the inflation arm of ``POST /v1/curve-preview`` (inflation).

Drives the FastAPI ``TestClient`` so auth + the inflation dispatch + MD resolve
(invariant #8) + the ``BootstrapInflationCurves`` engine call + the envelope
all light up together. Mirrors the swaps_inflation / curve harness:

* ``MdClient`` — :class:`_FakeMdClient` overrides ``resolve_quotes`` only,
  recording the batched call so the walker contract is asserted.
* ``EngineClient`` — :class:`_FakeEngineClient` records every ``(rpc, bytes)``
  call and returns / raises whatever the test installed.

Coverage focus:

1. Dispatch: a request carrying ``inflation_curves`` hits
   ``BootstrapInflationCurves`` (not ``BootstrapCurves``).
2. MD resolution: the inflation helper + nominal quote ids are collected and
   resolved server-side; the resolved values are substituted into the engine
   request bytes (faithful-bytes proof).
3. Engine call shape: the request decodes to a ``BootstrapInflationCurvesRequest``
   with the inflation index (body verbatim) + curve under
   ``Pricing.inflation`` and the query's ``ZeroRate`` measure.
4. Error codes: an unresolved quote → 422; a missing inflation index → 422; an
   engine failure → 502; a malformed index ``fixings`` block → 422.
5. YYIIS dispatch: ``kind = YoYInflation`` selects the ``YoYRate`` measure.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
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
from quantra_common.engine_client import EngineClient, EngineClientError, EngineRpc
from quantra_common.engine_client._generated.quantra.BootstrapInflationCurveResult import (
    BootstrapInflationCurveResultT,
)
from quantra_common.engine_client._generated.quantra.BootstrapInflationCurvesRequest import (
    BootstrapInflationCurvesRequest,
    BootstrapInflationCurvesRequestT,
)
from quantra_common.engine_client._generated.quantra.BootstrapInflationCurvesResponse import (
    BootstrapInflationCurvesResponseT,
)
from quantra_common.engine_client._generated.quantra.enums.InflationCurveKind import (
    InflationCurveKind,
)
from quantra_common.engine_client._generated.quantra.enums.InflationCurveMeasure import (
    InflationCurveMeasure,
)
from quantra_common.engine_client._generated.quantra.InflationCurveSeries import (
    InflationCurveSeriesT,
)
from quantra_common.md_client import MdClient, MdClientConfig
from quantra_common.settings import Environment, LogLevel
from quantra_common.types.market_data import ResolvedQuote
from quantra_orchestrator.app import create_app
from quantra_orchestrator.settings import OrchestratorSettings

from .conftest import FakeEngine

OWNER = "user-test"
API_KEY = "key-curve-preview-inflation"


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


def _bootstrap_response_bytes(
    *,
    curve_id: str = "hicp_zc",
    zeros: list[float],
    measure: int = InflationCurveMeasure.ZeroRate,
) -> bytes:
    response = BootstrapInflationCurvesResponseT()
    result = BootstrapInflationCurveResultT()
    result.id = curve_id
    result.referenceDate = "2025-01-15"
    result.gridDates = [f"203{i}-01-15" for i in range(len(zeros))]
    result.pillarDates = ["2030-01-15"]
    series = InflationCurveSeriesT()
    series.measure = measure
    series.values = list(zeros)
    result.series = [series]
    result.error = None
    response.results = [result]
    builder = flatbuffers.Builder(256)
    builder.Finish(response.Pack(builder))
    return bytes(builder.Output())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> OrchestratorSettings:
    return OrchestratorSettings(
        env=Environment.DEV, log_level=LogLevel.WARNING, build_sha="testsha"
    )


@pytest.fixture
def api_keys() -> dict[str, ApiKeyRecord]:
    return {
        API_KEY: ApiKeyRecord(
            api_key_id="ak-curve-preview-inflation",
            owner_uid=OWNER,
            name="Curve Preview Inflation Test",
            email="curve-preview-inflation@example.com",
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
    md_client: MdClient,
    engine_client: EngineClient,
) -> FastAPI:
    return create_app(
        settings,
        api_key_lookup=auth_lookup,
        firebase_verifier=firebase_verifier,
        app_ro_engine=cast(AsyncEngine, fake_ro_engine),
        app_rw_engine=None,
        md_client=md_client,
        engine_client=engine_client,
    )


@pytest.fixture
def app_factory(
    settings: OrchestratorSettings,
    auth_lookup: ApiKeyLookup,
    firebase_verifier: FirebaseTokenVerifier,
    fake_ro_engine: FakeEngine,
) -> Iterator[Any]:
    opened: list[TestClient] = []

    def _build(*, md_client: MdClient, engine_client: EngineClient) -> TestClient:
        app = _build_app(
            settings=settings,
            auth_lookup=auth_lookup,
            firebase_verifier=firebase_verifier,
            fake_ro_engine=fake_ro_engine,
            md_client=md_client,
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


def _resolved_quote(*, canonical_id: str, value: float, found: bool = True) -> ResolvedQuote:
    return ResolvedQuote(
        canonical_id=canonical_id,
        requested_as_of=datetime(2025, 1, 15, 0, 0),
        resolved_as_of=datetime(2025, 1, 15, 0, 0),
        found=found,
        is_exact=True,
        value=value if found else None,
        source="synthetic",
        vendor_id="vendor",
    )


def _inflation_index(*, index_id: str = "EUHICP") -> dict[str, Any]:
    return {
        "id": index_id,
        "family_name": "EU HICP",
        "currency": "EUR",
        "frequency": "Monthly",
        "availability_lag": {"n": 2, "unit": "Months"},
        "observation_lag": {"n": 3, "unit": "Months"},
        "fixings": [
            {"date": "2024-10-01", "value": 100.0},
            {"date": "2024-11-01", "value": 100.2},
            {"date": "2024-12-01", "value": 100.4},
        ],
    }


def _inflation_payload(
    *,
    kind: str = "ZeroInflation",
    measures: list[str] | None = None,
    index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    measures = measures or (["YoYRate"] if kind == "YoYInflation" else ["ZeroRate"])
    return {
        "pricing": {
            "as_of_date": "2025-01-15",
            "inflation_indices": [index if index is not None else _inflation_index()],
            "inflation_curves": [
                {
                    "id": "hicp_zc",
                    "reference_date": "2025-01-15",
                    "kind": kind,
                    "index_id": "EUHICP",
                    "day_counter": "Actual365Fixed",
                    "calendar": "TARGET",
                    "business_day_convention": "ModifiedFollowing",
                    "interpolator": "Linear",
                    "points": [
                        {
                            "point_type": (
                                "YearOnYearInflationSwapHelper"
                                if kind == "YoYInflation"
                                else "ZeroCouponInflationSwapHelper"
                            ),
                            "point": {
                                "tenor": "5Y",
                                "quote_id": "EUR.HICP.5Y",
                                "swap_observation_lag": {"n": 3, "unit": "Months"},
                            },
                        },
                        {
                            "point_type": (
                                "YearOnYearInflationSwapHelper"
                                if kind == "YoYInflation"
                                else "ZeroCouponInflationSwapHelper"
                            ),
                            "point": {
                                "tenor": "10Y",
                                "quote_id": "EUR.HICP.10Y",
                                "swap_observation_lag": {"n": 3, "unit": "Months"},
                            },
                        },
                    ],
                }
            ],
        },
        "queries": [{"curve_id": "hicp_zc", "measures": measures}],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_inflation_preview_happy_path_dispatches_and_resolves(
    app_factory: Any,
) -> None:
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="EUR.HICP.5Y", value=0.0212),
            _resolved_quote(canonical_id="EUR.HICP.10Y", value=0.0221),
        ]
    )
    engine = _FakeEngineClient(response=_bootstrap_response_bytes(zeros=[0.0212, 0.0221]))
    client = app_factory(md_client=md, engine_client=engine)

    resp = client.post("/v1/curve-preview", json=_inflation_payload(), headers=_headers())

    assert resp.status_code == HTTPStatus.OK, resp.text
    body = resp.json()
    assert body["results"][0]["id"] == "hicp_zc"
    series = body["results"][0]["series"][0]
    assert series["measure"] == "ZeroRate"
    assert series["values"] == [0.0212, 0.0221]

    # MD walker saw BOTH inflation helper quote ids in one batched call.
    assert len(md.calls) == 1
    assert set(md.calls[0][0]) == {"EUR.HICP.5Y", "EUR.HICP.10Y"}

    # The engine call used the inflation RPC, not the nominal one.
    assert len(engine.calls) == 1
    rpc, _ = engine.calls[0]
    assert rpc == EngineRpc.BOOTSTRAP_INFLATION_CURVES


def test_inflation_request_bytes_carry_resolved_values(app_factory: Any) -> None:
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="EUR.HICP.5Y", value=0.0212),
            _resolved_quote(canonical_id="EUR.HICP.10Y", value=0.0221),
        ]
    )
    engine = _FakeEngineClient(response=_bootstrap_response_bytes(zeros=[0.0212, 0.0221]))
    client = app_factory(md_client=md, engine_client=engine)

    resp = client.post("/v1/curve-preview", json=_inflation_payload(), headers=_headers())
    assert resp.status_code == HTTPStatus.OK, resp.text

    _, request_bytes = engine.calls[0]
    decoded = BootstrapInflationCurvesRequestT.InitFromObj(
        BootstrapInflationCurvesRequest.GetRootAs(bytearray(request_bytes), 0)
    )
    inflation = decoded.pricing.inflation
    # Index body consumed verbatim: the resolved id + family survive.
    assert inflation.inflationIndices[0].id.decode() == "EUHICP"
    # The inflation curve carries the ZCIIS helpers with the resolved quote
    # values substituted — no quote ids reach the engine.
    curve = inflation.inflationCurves[0]
    assert curve.id.decode() == "hicp_zc"
    helper_values = sorted(wrapper.point.quoteValue for wrapper in curve.points)
    assert helper_values == [0.0212, 0.0221]
    # The query measure is the inflation ZeroRate.
    assert decoded.queries[0].measures[0] == InflationCurveMeasure.ZeroRate


def test_inflation_unresolved_quote_is_422(app_factory: Any) -> None:
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="EUR.HICP.5Y", value=0.0212),
            _resolved_quote(canonical_id="EUR.HICP.10Y", value=0.0, found=False),
        ]
    )
    engine = _FakeEngineClient(response=b"")
    client = app_factory(md_client=md, engine_client=engine)

    resp = client.post("/v1/curve-preview", json=_inflation_payload(), headers=_headers())
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, resp.text
    assert "EUR.HICP.10Y" in resp.text
    # The engine is never called when a quote fails to resolve.
    assert engine.calls == []


def test_inflation_missing_index_is_422(app_factory: Any) -> None:
    md = _FakeMdClient(results=[])
    engine = _FakeEngineClient(response=b"")
    client = app_factory(md_client=md, engine_client=engine)

    payload = _inflation_payload()
    payload["pricing"].pop("inflation_indices")

    resp = client.post("/v1/curve-preview", json=payload, headers=_headers())
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, resp.text
    assert "inflation_indices" in resp.text
    assert engine.calls == []


def test_inflation_malformed_fixings_is_422(app_factory: Any) -> None:
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="EUR.HICP.5Y", value=0.0212),
            _resolved_quote(canonical_id="EUR.HICP.10Y", value=0.0221),
        ]
    )
    engine = _FakeEngineClient(response=b"")
    bad_index = _inflation_index()
    bad_index["fixings"] = "not-a-list"
    client = app_factory(md_client=md, engine_client=engine)

    resp = client.post(
        "/v1/curve-preview",
        json=_inflation_payload(index=bad_index),
        headers=_headers(),
    )
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, resp.text
    assert engine.calls == []


def test_inflation_engine_failure_is_502(app_factory: Any) -> None:
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="EUR.HICP.5Y", value=0.0212),
            _resolved_quote(canonical_id="EUR.HICP.10Y", value=0.0221),
        ]
    )
    engine = _FakeEngineClient(raises=EngineClientError("engine boom"))
    client = app_factory(md_client=md, engine_client=engine)

    resp = client.post("/v1/curve-preview", json=_inflation_payload(), headers=_headers())
    assert resp.status_code == HTTPStatus.BAD_GATEWAY, resp.text


def test_inflation_yoy_dispatch_selects_yoy_measure(app_factory: Any) -> None:
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="EUR.HICP.5Y", value=0.0205),
            _resolved_quote(canonical_id="EUR.HICP.10Y", value=0.0210),
        ]
    )
    engine = _FakeEngineClient(
        response=_bootstrap_response_bytes(
            zeros=[0.0205, 0.0210], measure=InflationCurveMeasure.YoYRate
        )
    )
    client = app_factory(md_client=md, engine_client=engine)

    resp = client.post(
        "/v1/curve-preview",
        json=_inflation_payload(kind="YoYInflation"),
        headers=_headers(),
    )
    assert resp.status_code == HTTPStatus.OK, resp.text
    assert resp.json()["results"][0]["series"][0]["measure"] == "YoYRate"

    # Use the low-level reader API (not the object-API ``InitFromObj``): the
    # generated YoY-helper unpacker has a pre-existing ``Period`` import bug, so
    # eagerly unpacking the whole request would trip it. The reader reaches the
    # curve ``kind`` + query ``measure`` without touching the helper points.
    _, request_bytes = engine.calls[0]
    request = BootstrapInflationCurvesRequest.GetRootAs(bytearray(request_bytes), 0)
    curve = request.Pricing().Inflation().InflationCurves(0)
    assert curve.Kind() == InflationCurveKind.YoYInflation
    assert request.Queries(0).Measures(0) == InflationCurveMeasure.YoYRate
