"""Route tests for the yield arm of ``POST /v1/curve-preview`` — index registration.

Drives the FastAPI ``TestClient`` (auth + translate + ``BootstrapCurves``
engine call together), mirroring ``test_curve_preview_inflation``'s harness:
``_FakeMdClient`` records the batched resolve call; ``_FakeEngineClient``
records every ``(rpc, bytes)`` call and returns a canned bootstrap response.

Coverage focus — the preview-vs-pricing index-catalog parity defect: the
pricing path registers term-IBOR forwarding indices from the shared
``_KNOWN_IBOR_INDICES`` catalog, but ``_build_preview_indices`` consulted only
``_KNOWN_OVERNIGHT_INDICES`` — so previewing ANY curve whose ``SwapHelper``
referenced an IBOR id (even the catalog id ``EURIBOR_6M``) 422ed with "no
IndexDef could be registered" while the same curve PRICED fine.

1. A ``SwapHelper`` referencing catalog ``EURIBOR_6M`` previews 200 and the
   engine request carries a faithful term-IBOR ``IndexDef`` (parity with the
   pricing path's registration).
2. An OIS helper referencing catalog ``SONIA`` still registers the overnight
   def (no regression from the catalog-order rework).
3. An unknown index id still surfaces the actionable 422 (engine never
   called), now naming BOTH catalogs.
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
from quantra_common.engine_client import EngineClient, EngineRpc
from quantra_common.engine_client._generated.quantra.BootstrapCurveResult import (
    BootstrapCurveResultT,
)
from quantra_common.engine_client._generated.quantra.BootstrapCurvesRequest import (
    BootstrapCurvesRequest,
    BootstrapCurvesRequestT,
)
from quantra_common.engine_client._generated.quantra.BootstrapCurvesResponse import (
    BootstrapCurvesResponseT,
)
from quantra_common.engine_client._generated.quantra.CurveSeries import CurveSeriesT
from quantra_common.engine_client._generated.quantra.enums.BootstrapTrait import (
    BootstrapTrait,
)
from quantra_common.engine_client._generated.quantra.enums.Calendar import Calendar
from quantra_common.engine_client._generated.quantra.enums.DayCounter import DayCounter
from quantra_common.engine_client._generated.quantra.enums.RateAveragingType import (
    RateAveragingType,
)
from quantra_common.engine_client._generated.quantra.enums.TimeUnit import TimeUnit
from quantra_common.engine_client._generated.quantra.IndexType import IndexType
from quantra_common.engine_client._generated.quantra.Point import Point
from quantra_common.md_client import MdClient, MdClientConfig
from quantra_common.settings import Environment, LogLevel
from quantra_common.types.market_data import ResolvedQuote
from quantra_orchestrator.app import create_app
from quantra_orchestrator.pricing._translator import DEFAULT_FORWARDING_INDEX_ID
from quantra_orchestrator.settings import OrchestratorSettings

from .conftest import FakeEngine

OWNER = "user-test"
API_KEY = "key-curve-preview"


# ---------------------------------------------------------------------------
# Fakes (mirrors test_curve_preview_inflation)
# ---------------------------------------------------------------------------


class _FakeMdClient(MdClient):
    """``MdClient`` whose ``resolve_quotes`` returns a canned list."""

    def __init__(self, *, results: list[ResolvedQuote] | None = None) -> None:
        config = MdClientConfig(base_url="http://stub", timeout_s=1.0, max_retries=0)
        super().__init__(config, client=httpx.AsyncClient(base_url="http://stub"))
        self.calls: list[tuple[list[str], Any]] = []
        self._results = results

    async def resolve_quotes(
        self,
        canonical_ids: list[str],
        as_of: Any,
        *,
        snapshot_version: str | None = None,
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


def _bootstrap_response_bytes(*, curve_id: str = "EUR-6M", zeros: list[float]) -> bytes:
    response = BootstrapCurvesResponseT()
    result = BootstrapCurveResultT()
    result.id = curve_id
    result.referenceDate = "2025-01-15"
    result.gridDates = [f"202{6 + i}-01-15" for i in range(len(zeros))]
    result.pillarDates = ["2030-01-15"]
    series = CurveSeriesT()
    series.measure = 1  # ZERO
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
            api_key_id="ak-curve-preview",
            owner_uid=OWNER,
            name="Curve Preview Test",
            email="curve-preview@example.com",
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


@pytest.fixture
def app_factory(
    settings: OrchestratorSettings,
    auth_lookup: ApiKeyLookup,
    firebase_verifier: FirebaseTokenVerifier,
    fake_ro_engine: FakeEngine,
) -> Iterator[Any]:
    opened: list[TestClient] = []

    def _build(*, md_client: MdClient, engine_client: EngineClient) -> TestClient:
        app: FastAPI = create_app(
            settings,
            api_key_lookup=auth_lookup,
            firebase_verifier=firebase_verifier,
            app_ro_engine=cast(AsyncEngine, fake_ro_engine),
            app_rw_engine=None,
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


def _resolved_quote(*, canonical_id: str, value: float) -> ResolvedQuote:
    return ResolvedQuote(
        canonical_id=canonical_id,
        requested_as_of=datetime(2025, 1, 15, 0, 0),
        resolved_as_of=datetime(2025, 1, 15, 0, 0),
        found=True,
        is_exact=True,
        value=value,
        source="synthetic",
        vendor_id="vendor",
    )


def _swap_point(years: int, *, rate: float, float_index_id: str) -> dict[str, Any]:
    return {
        "point_type": "SwapHelper",
        "point": {
            "tenor": {"n": years, "unit": "Years"},
            "rate": rate,
            "calendar": "TARGET",
            "sw_fixed_leg_frequency": "Annual",
            "float_index": {"id": float_index_id},
        },
    }


def _yield_payload(points: list[dict[str, Any]], *, name: str = "EUR-6M") -> dict[str, Any]:
    return {
        "pricing": {
            "as_of_date": "2025-01-15",
            "curves": [
                {
                    "name": name,
                    "currency": "EUR",
                    "day_counter": "Actual365Fixed",
                    "reference_date": "2025-01-15",
                    "interpolator": "LogLinear",
                    "points": [
                        {
                            "point_type": "DepositHelper",
                            "point": {"tenor": {"n": 6, "unit": "Months"}, "rate": 0.03},
                        },
                        *points,
                    ],
                }
            ],
        },
        "queries": [{"curve_id": name, "measures": ["ZERO"]}],
    }


def _decode_request(request_bytes: bytes) -> BootstrapCurvesRequestT:
    return BootstrapCurvesRequestT.InitFromObj(
        BootstrapCurvesRequest.GetRootAs(bytearray(request_bytes), 0)
    )


def _indices_by_id(decoded: BootstrapCurvesRequestT) -> dict[str, Any]:
    indices = decoded.pricing.rates.indices or []
    by_id = {idx.id.decode(): idx for idx in indices}
    assert len(by_id) == len(indices)  # no duplicate ids on the wire
    return by_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_preview_euribor6m_swap_helper_registers_ibor_indexdef_and_200s(
    app_factory: Any,
) -> None:
    """A SwapHelper referencing catalog ``EURIBOR_6M`` previews (the defect fix).

    Before: 422 "no IndexDef could be registered" for EVERY IBOR SwapHelper
    ref, because the preview arm consulted only the overnight catalog — while
    the same curve priced fine. Now the preview registers the same faithful
    term-IBOR def the pricing path does (Ibor, TARGET, Actual360, EUR, 6M
    tenor, 2 fixing days) and the bootstrap proceeds.
    """

    md = _FakeMdClient()
    engine = _FakeEngineClient(response=_bootstrap_response_bytes(zeros=[0.03, 0.032, 0.035]))
    client = app_factory(md_client=md, engine_client=engine)

    resp = client.post(
        "/v1/curve-preview",
        json=_yield_payload(
            [
                _swap_point(5, rate=0.035, float_index_id="EURIBOR_6M"),
                _swap_point(10, rate=0.038, float_index_id="EURIBOR_6M"),
            ]
        ),
        headers=_headers(),
    )

    assert resp.status_code == HTTPStatus.OK, resp.text
    assert resp.json()["results"][0]["series"][0]["values"] == [0.03, 0.032, 0.035]

    # The engine got the nominal bootstrap RPC with a faithful Ibor IndexDef
    # registered under the exact id the helpers reference.
    assert len(engine.calls) == 1
    rpc, request_bytes = engine.calls[0]
    assert rpc == EngineRpc.BOOTSTRAP_CURVES
    by_id = _indices_by_id(_decode_request(request_bytes))
    assert "EURIBOR_6M" in by_id
    euribor = by_id["EURIBOR_6M"]
    assert euribor.indexType == IndexType.Ibor
    assert euribor.calendar == Calendar.TARGET
    assert euribor.dayCounter == DayCounter.Actual360
    assert euribor.currency.decode() == "EUR"
    assert euribor.fixingDays == 2
    assert euribor.tenor.n == 6
    assert euribor.tenor.unit == TimeUnit.Months
    # The default forwarding index is still emitted alongside it.
    assert DEFAULT_FORWARDING_INDEX_ID in by_id


def test_preview_sonia_ois_helper_still_registers_overnight_indexdef(
    app_factory: Any,
) -> None:
    """The overnight catalog path is untouched by the IBOR wiring (no regression)."""

    md = _FakeMdClient()
    engine = _FakeEngineClient(response=_bootstrap_response_bytes(zeros=[0.04, 0.042]))
    client = app_factory(md_client=md, engine_client=engine)

    ois_point = {
        "point_type": "OISHelper",
        "point": {
            "tenor": {"n": 5, "unit": "Years"},
            "rate": 0.04,
            "overnight_index": {"id": "SONIA"},
        },
    }
    resp = client.post(
        "/v1/curve-preview",
        json=_yield_payload([ois_point], name="GBP-OIS"),
        headers=_headers(),
    )

    assert resp.status_code == HTTPStatus.OK, resp.text
    by_id = _indices_by_id(_decode_request(engine.calls[0][1]))
    assert "SONIA" in by_id
    sonia = by_id["SONIA"]
    assert sonia.indexType == IndexType.Overnight
    assert sonia.currency.decode() == "GBP"


def test_preview_ois_helper_carries_overnight_params_on_wire(app_factory: Any) -> None:
    """The preview path emits the five engine-0.6 OIS overnight params.

    The preview uses the same shared translator as pricing, so a legacy point
    (no new fields) must land the exact <=0.5.0-parity values on the decoded
    wire (payment_lag=0, Compound, 0, 0, False — present, not absent), and an
    explicit point must land its own values.
    """

    md = _FakeMdClient()
    engine = _FakeEngineClient(response=_bootstrap_response_bytes(zeros=[0.04, 0.042]))
    client = app_factory(md_client=md, engine_client=engine)

    legacy_point = {
        "point_type": "OISHelper",
        "point": {
            "tenor": {"n": 5, "unit": "Years"},
            "rate": 0.04,
            "overnight_index": {"id": "SONIA"},
        },
    }
    explicit_point = {
        "point_type": "OISHelper",
        "point": {
            "tenor": {"n": 10, "unit": "Years"},
            "rate": 0.042,
            "overnight_index": {"id": "SONIA"},
            "payment_lag": 2,
            "averaging_method": "Simple",
            "lookback_days": 2,
            "lockout_days": 2,
            "apply_observation_shift": True,
        },
    }
    resp = client.post(
        "/v1/curve-preview",
        json=_yield_payload([legacy_point, explicit_point], name="GBP-OIS"),
        headers=_headers(),
    )

    assert resp.status_code == HTTPStatus.OK, resp.text
    decoded = _decode_request(engine.calls[0][1])
    # points[0] is the _yield_payload deposit; the OIS points follow.
    legacy, explicit = (w.point for w in decoded.pricing.rates.curves[0].points[1:])

    assert legacy.paymentLag == 0
    assert legacy.averagingMethod == RateAveragingType.Compound
    assert legacy.lookbackDays == 0
    assert legacy.lockoutDays == 0
    assert legacy.applyObservationShift is False

    assert explicit.paymentLag == 2
    assert explicit.averagingMethod == RateAveragingType.Simple
    assert explicit.lookbackDays == 2
    assert explicit.lockoutDays == 2
    assert explicit.applyObservationShift is True


def test_preview_ois_helper_negative_payment_lag_is_422(app_factory: Any) -> None:
    """Pre-flight typed 422 (D54 envelope) — the engine is never called."""

    md = _FakeMdClient()
    engine = _FakeEngineClient(response=_bootstrap_response_bytes(zeros=[0.04]))
    client = app_factory(md_client=md, engine_client=engine)

    bad_point = {
        "point_type": "OISHelper",
        "point": {
            "tenor": {"n": 5, "unit": "Years"},
            "rate": 0.04,
            "overnight_index": {"id": "SONIA"},
            "payment_lag": -2,
        },
    }
    resp = client.post(
        "/v1/curve-preview",
        json=_yield_payload([bad_point], name="GBP-OIS"),
        headers=_headers(),
    )

    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, resp.text
    body = resp.json()
    assert "payment_lag" in body["error"]
    assert engine.calls == []


def test_preview_unknown_index_ref_still_422s_naming_both_catalogs(
    app_factory: Any,
) -> None:
    """An id outside both catalogs keeps the actionable 422; engine never called."""

    md = _FakeMdClient()
    engine = _FakeEngineClient(response=b"")
    client = app_factory(md_client=md, engine_client=engine)

    resp = client.post(
        "/v1/curve-preview",
        json=_yield_payload([_swap_point(5, rate=0.035, float_index_id="WIBOR_6M")]),
        headers=_headers(),
    )

    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, resp.text
    assert "no IndexDef could be registered" in resp.text
    assert "WIBOR_6M" in resp.text
    # The known-catalog listing now names BOTH families (actionable).
    assert "SONIA" in resp.text
    assert "EURIBOR_6M" in resp.text
    assert engine.calls == []


# ---------------------------------------------------------------------------
# Value-based curves (InterpolatedZero / InterpolatedDiscount / InterpolatedFwd)
# ---------------------------------------------------------------------------


def _value_curve_payload(
    points: list[dict[str, Any]], *, trait: str, name: str = "EUR-ZEROS"
) -> dict[str, Any]:
    return {
        "pricing": {
            "as_of_date": "2025-01-15",
            "curves": [
                {
                    "name": name,
                    "currency": "EUR",
                    "day_counter": "Actual365Fixed",
                    "reference_date": "2025-01-15",
                    "interpolator": "Linear",
                    "bootstrap_trait": trait,
                    "points": points,
                }
            ],
        },
        "queries": [{"curve_id": name, "measures": ["ZERO"]}],
    }


def test_preview_interpolated_zero_curve_resolves_quotes_and_packs_value_points(
    app_factory: Any,
) -> None:
    """A value curve previews: quote ids resolve via MD, values land inline."""

    md = _FakeMdClient(results=[_resolved_quote(canonical_id="EUR.ZERO.5Y", value=0.0312)])
    engine = _FakeEngineClient(
        response=_bootstrap_response_bytes(curve_id="EUR-ZEROS", zeros=[0.02, 0.025, 0.0312])
    )
    client = app_factory(md_client=md, engine_client=engine)

    resp = client.post(
        "/v1/curve-preview",
        json=_value_curve_payload(
            [
                {"point_type": "ZeroRatePoint", "point": {"date": "2025-01-15", "zero_rate": 0.02}},
                {
                    "point_type": "ZeroRatePoint",
                    "point": {"tenor": {"n": 1, "unit": "Years"}, "zero_rate": 0.025},
                },
                {
                    "point_type": "ZeroRatePoint",
                    "point": {"tenor": {"n": 5, "unit": "Years"}, "quote_id": "EUR.ZERO.5Y"},
                },
            ],
            trait="InterpolatedZero",
        ),
        headers=_headers(),
    )

    assert resp.status_code == HTTPStatus.OK, resp.text
    # The MD walker got the value point's quote id (same batched resolver).
    assert md.calls
    assert md.calls[0][0] == ["EUR.ZERO.5Y"]
    decoded = _decode_request(engine.calls[0][1])
    curve = decoded.pricing.rates.curves[0]
    assert curve.bootstrapTrait == BootstrapTrait.InterpolatedZero
    assert [w.pointType for w in curve.points] == [Point.ZeroRatePoint] * 3
    # Invariant #8: the resolved value rides inline on the wire.
    assert curve.points[2].point.zeroRate == pytest.approx(0.0312)


def test_preview_value_curve_validation_maps_to_422(app_factory: Any) -> None:
    """A pre-flight value-curve rule violation is a clean 422, engine untouched."""

    md = _FakeMdClient()
    engine = _FakeEngineClient(response=b"")
    client = app_factory(md_client=md, engine_client=engine)

    resp = client.post(
        "/v1/curve-preview",
        json=_value_curve_payload(
            [
                {
                    "point_type": "DiscountFactorPoint",
                    "point": {"date": "2025-01-15", "discount_factor": 0.98},
                }
            ],
            trait="InterpolatedDiscount",
        ),
        headers=_headers(),
    )

    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, resp.text
    assert "exactly 1.0" in resp.text
    assert engine.calls == []
