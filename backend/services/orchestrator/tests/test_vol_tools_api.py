"""Route tests for the IR-vol utility endpoints.

Drives the FastAPI ``TestClient`` so auth + the inline-``Pricing`` translation
(curve + constant-vol swaption surface) + server-side MD quote resolution
(invariant #8) + the engine call + the envelope light up together. Both
the ``MdClient`` and ``EngineClient`` are recording stubs.

Coverage:
1. vol-surfaces/sample: inline curve + constant-vol surface → a
   ``SampleVolSurfaces`` call; canned response decodes to ``results``.
2. MD resolution: a curve ``quote_id`` is collected + resolved server-side and
   the resolved value is substituted (the engine never sees a ``quote_id``).
3. calibrate-swaption-vol: canned diagnostics decode.
4. calibrate-swaption-model: canned Hull-White output decodes.
5. Translation error: an empty ``curves`` list → 422 (never reaches engine).
6. Engine failure → 502.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any, cast

import flatbuffers
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_common.auth.context import ApiKeyRecord
from quantra_common.engine_client import EngineClient, EngineClientError, EngineRpc
from quantra_common.engine_client._generated.quantra.CalibrateSwaptionModelResponse import (
    CalibrateSwaptionModelResponseT,
)
from quantra_common.engine_client._generated.quantra.CalibrateSwaptionVolResponse import (
    CalibrateSwaptionVolResponseT,
)
from quantra_common.engine_client._generated.quantra.SampleVolSurfacesRequest import (
    SampleVolSurfacesRequest,
    SampleVolSurfacesRequestT,
)
from quantra_common.engine_client._generated.quantra.SampleVolSurfacesResponse import (
    SampleVolSurfacesResponseT,
)
from quantra_common.engine_client._generated.quantra.SwaptionVolDiagnostics import (
    SwaptionVolDiagnosticsT,
)
from quantra_common.engine_client._generated.quantra.SwaptionVolPayload import (
    SwaptionVolPayload,
)
from quantra_common.engine_client._generated.quantra.VolSurfaceSample import (
    VolSurfaceSampleT,
)
from quantra_common.md_client import MdClient, MdClientConfig
from quantra_common.settings import Environment, LogLevel
from quantra_common.types.market_data import ResolvedQuote
from quantra_orchestrator.app import create_app
from quantra_orchestrator.settings import OrchestratorSettings

from .conftest import FakeEngine

API_KEY = "key-vol-tools"
OWNER = "user-vol"


class _FakeEngine(EngineClient):
    def __init__(self, *, response: bytes | None = None, raises: Exception | None = None) -> None:
        self.calls: list[tuple[EngineRpc, bytes]] = []
        self._response = response
        self._raises = raises

    async def call(self, rpc: EngineRpc, request_bytes: bytes) -> bytes:
        self.calls.append((rpc, request_bytes))
        if self._raises is not None:
            raise self._raises
        assert self._response is not None
        return self._response

    async def close(self) -> None:
        return None


class _FakeMd(MdClient):
    def __init__(self, *, results: list[ResolvedQuote] | None = None) -> None:
        config = MdClientConfig(base_url="http://stub", timeout_s=1.0, max_retries=0)
        super().__init__(config, client=httpx.AsyncClient(base_url="http://stub"))
        self.calls: list[tuple[list[str], Any]] = []
        self._results = results or []

    async def resolve_quotes(
        self,
        canonical_ids: list[str],
        as_of: Any,
        *,
        snapshot_version: str | None = None,
    ) -> list[ResolvedQuote]:
        self.calls.append((list(canonical_ids), as_of))
        return list(self._results)


def _client(engine: EngineClient, md: MdClient) -> TestClient:
    async def _lookup(key: str) -> ApiKeyRecord | None:
        if key == API_KEY:
            return ApiKeyRecord(
                api_key_id="ak-vol",
                owner_uid=OWNER,
                name="Vol Tools Test",
                email="vol@example.com",
                tier="free",
                active=True,
            )
        return None

    def _verify(_token: str) -> dict[str, Any]:
        raise ValueError("no firebase")

    app = create_app(
        OrchestratorSettings(env=Environment.DEV, log_level=LogLevel.WARNING, build_sha="testsha"),
        api_key_lookup=_lookup,
        firebase_verifier=_verify,
        engine_client=engine,
        md_client=md,
    )
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


def _resolved_quote(*, canonical_id: str, value: float) -> ResolvedQuote:
    return ResolvedQuote(
        canonical_id=canonical_id,
        requested_as_of=datetime(2026, 5, 13, 0, 0),
        resolved_as_of=datetime(2026, 5, 13, 0, 0),
        found=True,
        is_exact=True,
        value=value,
        source="synthetic",
        vendor_id="vendor",
    )


def _deposit_point(*, quote_id: str | None = None, rate: float | None = 0.0425) -> dict[str, Any]:
    point: dict[str, Any] = {
        "tenor": {"n": 1, "unit": "Years"},
        "fixing_days": 2,
        "calendar": "TARGET",
        "business_day_convention": "ModifiedFollowing",
        "day_counter": "Actual365Fixed",
    }
    if quote_id is not None:
        point["quote_id"] = quote_id
    if rate is not None:
        point["rate"] = rate
    return {"point_type": "DepositHelper", "point": point}


def _pricing(*, points: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "as_of_date": "2026-05-13",
        "curves": [
            {
                "name": "USD-OIS",
                "currency": "USD",
                "day_counter": "Actual360",
                "reference_date": "2026-05-13",
                "interpolator": "LogLinear",
                "points": points if points is not None else [_deposit_point()],
            }
        ],
        "vol_surfaces": [
            {
                "name": "USD-ATM",
                "kind": "SwaptionVolSpec",
                "payload": {
                    "swap_index_id": "EUR_SWAP_6M",
                    "base": {"constant_vol": 0.20},
                },
            }
        ],
    }


def _sample_response() -> bytes:
    resp = SampleVolSurfacesResponseT()
    sample = VolSurfaceSampleT()
    sample.volId = "USD-ATM"
    sample.referenceDate = "2026-05-13"
    sample.nExpiries = 1
    sample.nTenors = 1
    sample.nStrikes = 1
    sample.expiries = ["2027-05-13"]
    sample.strikes = [0.03]
    sample.vols = [0.20]
    sample.atmLevels = [0.021]
    sample.error = None
    resp.results = [sample]
    resp.diagnostics = None
    builder = flatbuffers.Builder(512)
    builder.Finish(resp.Pack(builder))
    return bytes(builder.Output())


def _calibrate_vol_response() -> bytes:
    resp = CalibrateSwaptionVolResponseT()
    resp.volId = "USD-ATM"
    diag = SwaptionVolDiagnosticsT()
    diag.volId = "USD-ATM"
    diag.kind = 0
    diag.nExpiries = 1
    diag.nTenors = 1
    diag.forwardPerNode = [0.021]
    diag.atmVolPerNode = [0.20]
    diag.warnings = []
    resp.diagnostics = diag
    builder = flatbuffers.Builder(512)
    builder.Finish(resp.Pack(builder))
    return bytes(builder.Output())


def _calibrate_model_response() -> bytes:
    resp = CalibrateSwaptionModelResponseT()
    resp.modelId = "HW-1"
    resp.hwA = 0.03
    resp.hwSigma = 0.01
    resp.rmse = 0.0001
    resp.numHelpers = 4
    resp.gridRows = 2
    resp.gridCols = 2
    resp.gridPoints = 4
    builder = flatbuffers.Builder(256)
    builder.Finish(resp.Pack(builder))
    return bytes(builder.Output())


def test_sample_happy_path(headers: dict[str, str]) -> None:
    engine = _FakeEngine(response=_sample_response())
    md = _FakeMd()
    with _client(engine, md) as client:
        resp = client.post(
            "/v1/vol-surfaces/sample",
            headers=headers,
            json={
                "pricing": _pricing(),
                "queries": [
                    {
                        "vol_id": "USD-ATM",
                        "expiry_tenors": [{"n": 1, "unit": "Years"}],
                        "swap_tenors": [{"n": 5, "unit": "Years"}],
                        "strikes": [0.03],
                        "discounting_curve_id": "USD-OIS",
                        "forwarding_curve_id": "USD-OIS",
                    }
                ],
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"][0]["vol_id"] == "USD-ATM"
    assert body["results"][0]["vols"] == [0.20]
    assert engine.calls[0][0] == EngineRpc.SAMPLE_VOL_SURFACES


def test_sample_resolves_curve_quote_id_server_side(headers: dict[str, str]) -> None:
    engine = _FakeEngine(response=_sample_response())
    md = _FakeMd(results=[_resolved_quote(canonical_id="USD.IRS.1Y", value=0.05)])
    with _client(engine, md) as client:
        resp = client.post(
            "/v1/vol-surfaces/sample",
            headers=headers,
            json={
                "pricing": _pricing(points=[_deposit_point(quote_id="USD.IRS.1Y", rate=None)]),
                "queries": [{"vol_id": "USD-ATM"}],
            },
        )
    assert resp.status_code == 200, resp.text
    # The quote id was collected + resolved server-side (invariant #8).
    assert md.calls
    assert md.calls[0][0] == ["USD.IRS.1Y"]
    # The engine request never carries the raw quote id.
    request_bytes = engine.calls[0][1]
    assert b"USD.IRS.1Y" not in request_bytes
    decoded = SampleVolSurfacesRequestT.InitFromObj(
        SampleVolSurfacesRequest.GetRootAs(bytearray(request_bytes), 0)
    )
    assert decoded.pricing is not None


def _atm_matrix_surface() -> dict[str, Any]:
    return {
        "name": "USD-ATM",
        "payload_type": "SwaptionVolSpec",
        "payload": {
            "swap_index_id": "EUR_SWAP_6M",
            "payload_type": "SwaptionVolAtmMatrixSpec",
            "payload": {
                "base": {
                    "reference_date": "2026-05-13",
                    "calendar": "TARGET",
                    "business_day_convention": "ModifiedFollowing",
                    "day_counter": "Actual365Fixed",
                    "volatility_type": "Normal",
                    "shape": "AtmMatrix2D",
                },
                "expiries": [{"n": 1, "unit": "Years"}, {"n": 2, "unit": "Years"}],
                "tenors": [{"n": 5, "unit": "Years"}, {"n": 10, "unit": "Years"}],
                "vols": {"n_rows": 2, "n_cols": 2, "values": [0.006, 0.0065, 0.007, 0.0075]},
            },
        },
    }


def test_sample_accepts_rich_query_grids_and_matrix_surface(headers: dict[str, str]) -> None:
    """The portal's rich query shape (expiry_grid/tenor_grid/strike_grid + a matrix
    surface) reaches the engine with a strike grid populated (the engine rejects a
    query with no strike grid)."""

    engine = _FakeEngine(response=_sample_response())
    md = _FakeMd()
    pricing = _pricing()
    pricing["vol_surfaces"] = [_atm_matrix_surface()]
    with _client(engine, md) as client:
        resp = client.post(
            "/v1/vol-surfaces/sample",
            headers=headers,
            json={
                "pricing": pricing,
                "queries": [
                    {
                        "vol_id": "USD-ATM",
                        "surface_type": "Swaption",
                        "expiry_grid": {
                            "grid_type": "TenorGrid",
                            "grid": {
                                "tenors": [{"n": 1, "unit": "Years"}, {"n": 2, "unit": "Years"}],
                                "calendar": "TARGET",
                                "business_day_convention": "ModifiedFollowing",
                            },
                        },
                        "tenor_grid": {
                            "grid_type": "TenorGrid",
                            "grid": {
                                "tenors": [
                                    {"n": 5, "unit": "Years"},
                                    {"n": 10, "unit": "Years"},
                                ]
                            },
                        },
                        "strike_grid": {"axis": "AbsoluteStrike", "strikes": [0.02, 0.03, 0.04]},
                        "output_mode": "Cube",
                        "options": {"allow_extrapolation": True, "max_points": 40000},
                        "swap_index_id": "EUR_SWAP_6M",
                        "discounting_curve_id": "USD-OIS",
                        "forwarding_curve_id": "USD-OIS",
                    }
                ],
            },
        )
    assert resp.status_code == 200, resp.text
    request_bytes = engine.calls[0][1]
    decoded = SampleVolSurfacesRequestT.InitFromObj(
        SampleVolSurfacesRequest.GetRootAs(bytearray(request_bytes), 0)
    )
    query = decoded.queries[0]
    # The strike grid reached the engine with the requested absolute strikes.
    assert query.strikeGrid is not None
    assert len(query.strikeGrid.strikes) == 3
    assert query.expiryGrid is not None
    assert query.tenorGrid is not None
    assert query.options is not None
    assert query.options.maxPoints == 40000
    # The ATM-matrix surface was translated (not the constant payload).
    surface = decoded.pricing.volatility.volSurfaces[0]
    assert surface.payload.payloadType == SwaptionVolPayload.SwaptionVolAtmMatrixSpec
    matrix = surface.payload.payload
    assert matrix.vols.nRows == 2
    assert matrix.vols.nCols == 2
    assert len(matrix.vols.values) == 4


def test_sample_forwards_swap_index_definition(headers: dict[str, str]) -> None:
    engine = _FakeEngine(response=_sample_response())
    md = _FakeMd()
    pricing = _pricing()
    pricing["swap_indices"] = [
        {
            "id": "EUR_SWAP_6M",
            "kind": "IborSwapIndex",
            "float_index_id": "forwarding_index",
            "fixed": {"frequency": "Annual", "day_counter": "Thirty360"},
            "float": {"tenor": {"n": 6, "unit": "Months"}},
        }
    ]
    with _client(engine, md) as client:
        resp = client.post(
            "/v1/vol-surfaces/sample",
            headers=headers,
            json={"pricing": pricing, "queries": [{"vol_id": "USD-ATM"}]},
        )
    assert resp.status_code == 200, resp.text
    request_bytes = engine.calls[0][1]
    decoded = SampleVolSurfacesRequestT.InitFromObj(
        SampleVolSurfacesRequest.GetRootAs(bytearray(request_bytes), 0)
    )
    swap_indices = decoded.pricing.rates.swapIndices
    assert swap_indices is not None
    assert len(swap_indices) == 1
    assert swap_indices[0].id.decode() == "EUR_SWAP_6M"
    assert swap_indices[0].floatIndexId.decode() == "forwarding_index"


def test_calibrate_vol_happy_path(headers: dict[str, str]) -> None:
    engine = _FakeEngine(response=_calibrate_vol_response())
    md = _FakeMd()
    with _client(engine, md) as client:
        resp = client.post(
            "/v1/calibrate-swaption-vol",
            headers=headers,
            json={
                "pricing": _pricing(),
                "vol_id": "USD-ATM",
                "discounting_curve_id": "USD-OIS",
                "forwarding_curve_id": "USD-OIS",
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["vol_id"] == "USD-ATM"
    assert body["diagnostics"]["atm_vol_per_node"] == [0.20]
    assert engine.calls[0][0] == EngineRpc.CALIBRATE_SWAPTION_VOL


def test_calibrate_model_happy_path(headers: dict[str, str]) -> None:
    engine = _FakeEngine(response=_calibrate_model_response())
    md = _FakeMd()
    with _client(engine, md) as client:
        resp = client.post(
            "/v1/calibrate-swaption-model",
            headers=headers,
            json={
                "pricing": _pricing(),
                "model_id": "HW-1",
                "calibration": {
                    "swaption_vol_id": "USD-ATM",
                    "discount_curve_id": "USD-OIS",
                    "forwarding_curve_id": "USD-OIS",
                    "expiries": [{"n": 1, "unit": "Years"}],
                    "tenors": [{"n": 5, "unit": "Years"}],
                },
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["model_id"] == "HW-1"
    assert body["hw_a"] == 0.03
    assert body["num_helpers"] == 4
    assert engine.calls[0][0] == EngineRpc.CALIBRATE_SWAPTION_MODEL


def test_empty_curves_is_422(headers: dict[str, str]) -> None:
    engine = _FakeEngine(response=_sample_response())
    md = _FakeMd()
    with _client(engine, md) as client:
        resp = client.post(
            "/v1/vol-surfaces/sample",
            headers=headers,
            json={
                "pricing": {"as_of_date": "2026-05-13", "curves": []},
                "queries": [{"vol_id": "USD-ATM"}],
            },
        )
    assert resp.status_code == 422, resp.text
    assert engine.calls == []


def test_engine_failure_maps_to_502(headers: dict[str, str]) -> None:
    engine = _FakeEngine(raises=EngineClientError("boom"))
    md = _FakeMd()
    with _client(engine, md) as client:
        resp = client.post(
            "/v1/calibrate-swaption-vol",
            headers=headers,
            json={
                "pricing": _pricing(),
                "vol_id": "USD-ATM",
                "discounting_curve_id": "USD-OIS",
                "forwarding_curve_id": "USD-OIS",
            },
        )
    assert resp.status_code == 502, resp.text


# ---------------------------------------------------------------------------
# the vol-surfaces/sample route records an in-app pricing trace so the
# portal's Investigate page has a timeline for a sample (mirrors the pricing
# routes' tracing rollout).
# ---------------------------------------------------------------------------

SAMPLE_STAGES = ["input", "load_entities", "engine_request", "engine_response"]


def _rw_handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
    if "INSERT INTO app.pricing_traces" in sql:
        return []
    msg = f"unexpected rw SQL: {sql!r}"
    raise AssertionError(msg)


def _trace_client(
    engine: EngineClient,
    md: MdClient,
    rw: FakeEngine,
    *,
    trace_capture: bool = True,
) -> TestClient:
    async def _lookup(key: str) -> ApiKeyRecord | None:
        if key == API_KEY:
            return ApiKeyRecord(
                api_key_id="ak-vol",
                owner_uid=OWNER,
                name="Vol Tools Test",
                email="vol@example.com",
                tier="free",
                active=True,
            )
        return None

    def _verify(_token: str) -> dict[str, Any]:
        raise ValueError("no firebase")

    app = create_app(
        OrchestratorSettings(
            env=Environment.DEV,
            log_level=LogLevel.WARNING,
            build_sha="testsha",
            trace_capture=trace_capture,
        ),
        api_key_lookup=_lookup,
        firebase_verifier=_verify,
        engine_client=engine,
        md_client=md,
        app_rw_engine=cast(AsyncEngine, rw),
    )
    return TestClient(app, raise_server_exceptions=False)


def _trace_inserts(rw: FakeEngine) -> list[dict[str, Any]]:
    return [r.params for r in rw.recordings if "INSERT INTO app.pricing_traces" in r.sql]


def test_sample_records_trace_timeline(headers: dict[str, str]) -> None:
    engine = _FakeEngine(response=_sample_response())
    md = _FakeMd()
    rw = FakeEngine()
    rw.set_handler(_rw_handler)
    with _trace_client(engine, md, rw) as client:
        resp = client.post(
            "/v1/vol-surfaces/sample",
            headers=headers,
            json={
                "pricing": _pricing(),
                "queries": [
                    {
                        "vol_id": "USD-ATM",
                        "expiry_tenors": [{"n": 1, "unit": "Years"}],
                        "swap_tenors": [{"n": 5, "unit": "Years"}],
                        "strikes": [0.03],
                    }
                ],
            },
        )
    assert resp.status_code == 200, resp.text

    inserts = _trace_inserts(rw)
    assert [p["stage"] for p in inserts] == SAMPLE_STAGES
    # Owner-scoped (all stages one owner), single request_id, orchestrator
    # service, product tag. (The concrete uid is the resolved principal — under
    # the repo's dev-auth-bypass .env that is ``dev-user``, else the API key's
    # owner; owner-scoping is what matters here, not the specific value.)
    assert len({p["owner_uid"] for p in inserts}) == 1
    assert len({p["request_id"] for p in inserts}) == 1
    assert all(p["service"] == "orchestrator" for p in inserts)
    assert {p["product"] for p in inserts} == {"vol_sample"}

    by_stage = {p["stage"]: json.loads(p["payload"]) for p in inserts}
    # input carries the product tag inline + the query shape.
    assert by_stage["input"]["product"] == "vol_sample"
    assert by_stage["input"]["vol_ids"] == ["USD-ATM"]
    # load_entities surfaces the resolved curve + surface names.
    assert by_stage["load_entities"]["curve_names"] == ["USD-OIS"]
    assert by_stage["load_entities"]["vol_surface_names"] == ["USD-ATM"]
    # engine_request carries the EXACT bytes sent to the engine (wire view).
    wire = by_stage["engine_request"]["engine_wire"]
    assert wire["sent"] is True
    assert wire["rpc"]
    assert wire["request_bytes_len"] > 0
    raw = base64.b64decode(wire["request_bytes_b64"])
    assert len(raw) == wire["request_bytes_len"]
    assert isinstance(wire["decoded"], dict)
    assert wire["decoded"]
    # engine_response carries the per-surface sample summary.
    eng_resp = by_stage["engine_response"]
    assert eng_resp["result_count"] == 1
    assert eng_resp["results"][0]["vol_id"] == "USD-ATM"


def test_sample_engine_error_records_error_stage(headers: dict[str, str]) -> None:
    engine = _FakeEngine(raises=EngineClientError("engine boom"))
    md = _FakeMd()
    rw = FakeEngine()
    rw.set_handler(_rw_handler)
    with _trace_client(engine, md, rw) as client:
        resp = client.post(
            "/v1/vol-surfaces/sample",
            headers=headers,
            json={"pricing": _pricing(), "queries": [{"vol_id": "USD-ATM"}]},
        )
    assert resp.status_code == 502, resp.text

    by_stage = {p["stage"]: json.loads(p["payload"]) for p in _trace_inserts(rw)}
    # The engine's REAL error text lands on engine_response (not the token).
    assert by_stage["engine_response"]["error"] == "engine boom"
    assert by_stage["engine_response"]["exc_type"] == "EngineClientError"
    # The error stage (the envelope the client saw) is retrievable too.
    assert "error" in by_stage
    assert by_stage["error"]["error"]["code"].startswith("engine_")


def test_sample_unresolvable_quote_records_error_stage(
    headers: dict[str, str],
) -> None:
    # An empty MD result for a referenced quote id → 422 during resolve, and a
    # retrievable ``error`` stage in the trace (no engine call is made).
    engine = _FakeEngine(response=_sample_response())
    md = _FakeMd(results=[])
    rw = FakeEngine()
    rw.set_handler(_rw_handler)
    with _trace_client(engine, md, rw) as client:
        resp = client.post(
            "/v1/vol-surfaces/sample",
            headers=headers,
            json={
                "pricing": _pricing(points=[_deposit_point(quote_id="USD.IRS.1Y", rate=None)]),
                "queries": [{"vol_id": "USD-ATM"}],
            },
        )
    assert resp.status_code == 422, resp.text

    inserts = _trace_inserts(rw)
    stages = [p["stage"] for p in inserts]
    # input recorded, then the error stage; the engine was never reached.
    assert stages == ["input", "error"]
    assert engine.calls == []


def test_sample_trace_capture_off_writes_nothing(headers: dict[str, str]) -> None:
    engine = _FakeEngine(response=_sample_response())
    md = _FakeMd()
    rw = FakeEngine()
    rw.set_handler(_rw_handler)
    with _trace_client(engine, md, rw, trace_capture=False) as client:
        resp = client.post(
            "/v1/vol-surfaces/sample",
            headers=headers,
            json={"pricing": _pricing(), "queries": [{"vol_id": "USD-ATM"}]},
        )
    assert resp.status_code == 200, resp.text
    assert _trace_inserts(rw) == []
