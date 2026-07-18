"""Tests for the in-app pricing trace on the swap_ir pilot.

Covers:

* Trace rows are written for a swap_ir call when ``TRACE_CAPTURE`` is on,
  carrying every pipeline stage (input → load_entities → md_resolve →
  engine_request → engine_response → history_write).
* No trace rows when ``TRACE_CAPTURE`` is off.
* A failing trace flush does NOT fail the price (200 + warning).
* The ``md_resolve`` + ``engine_request`` payloads carry real content.
* The engine-error path captures the engine's REAL error text.
* ``GET /v1/traces/{request_id}`` is owner-scoped (foreign id → 404).
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
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
from quantra_common.engine_client._generated.quantra.PriceVanillaSwapResponse import (
    PriceVanillaSwapResponseT,
)
from quantra_common.engine_client._generated.quantra.VanillaSwapResponse import (
    VanillaSwapResponseT,
)
from quantra_common.md_client import MdClient, MdClientConfig
from quantra_common.settings import Environment, LogLevel
from quantra_common.types.market_data import ResolvedQuote
from quantra_orchestrator.app import create_app
from quantra_orchestrator.pricing.swap_ir.engine_io import (
    decode_swap_ir_request_wire,
)
from quantra_orchestrator.settings import OrchestratorSettings
from quantra_orchestrator.tracing import recorder as recorder_module

from .conftest import FakeEngine

OWNER = "user-trace"
OTHER = "user-other"
API_KEY = "key-trace"
OTHER_KEY = "key-other"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeMdClient(MdClient):
    def __init__(self, *, results: list[ResolvedQuote] | None = None) -> None:
        super().__init__(
            MdClientConfig(base_url="http://stub", timeout_s=1.0, max_retries=0),
            client=httpx.AsyncClient(base_url="http://stub"),
        )
        self._results = results or []

    async def resolve_quotes(
        self,
        canonical_ids: list[str],
        as_of: Any,
        *,
        snapshot_version: str | None = None,
    ) -> list[ResolvedQuote]:
        return list(self._results)


class _FakeEngineClient(EngineClient):
    def __init__(self, *, response: bytes | None = None, raises: Exception | None = None) -> None:
        self._response = response
        self._raises = raises

    async def call(self, rpc: EngineRpc, request_bytes: bytes) -> bytes:
        if self._raises is not None:
            raise self._raises
        assert self._response is not None
        return self._response

    async def close(self) -> None:
        return None


def _fb_response_bytes(npv: float = 0.0, fair_rate: float = 0.03) -> bytes:
    response = PriceVanillaSwapResponseT()
    swap = VanillaSwapResponseT()
    swap.npv = npv
    swap.fairRate = fair_rate
    swap.fairSpread = 0.0
    swap.fixedLegBps = 0.0
    swap.floatingLegBps = 0.0
    swap.fixedLegNpv = npv * 0.5
    swap.floatingLegNpv = npv * 0.5
    response.swaps = [swap]
    builder = flatbuffers.Builder(256)
    builder.Finish(response.Pack(builder))
    return bytes(builder.Output())


def _curve_row(*, curve_id: uuid.UUID) -> dict[str, Any]:
    # An EUR ESTR OIS curve: the OISHelper references the ``ESTR`` overnight
    # index by id, so the translator registers a faithful ESTR ``IndexDef`` in
    # the engine's ``rates.indices`` registry. The engine_wire
    # view must therefore show ESTR in the decoded-from-bytes pricing block.
    return {
        "id": str(curve_id),
        "name": "EUR-ESTR-OIS",
        "currency": "EUR",
        "day_counter": "Actual360",
        "helper_kind": "Discount",
        "reference_date": datetime(2026, 5, 13).date(),
        "points": [
            {
                "point_type": "OISHelper",
                "point": {
                    "quote_id": "USD.IRS.1Y",
                    "tenor": {"n": 1, "unit": "Years"},
                    "overnight_index": {"id": "ESTR"},
                    "settlement_days": 2,
                    "calendar": "TARGET",
                    "fixed_leg_frequency": "Annual",
                    "fixed_leg_convention": "ModifiedFollowing",
                    "fixed_leg_day_counter": "Actual365Fixed",
                },
            }
        ],
        "body": {"interpolator": "LogLinear"},
    }


def _resolved_quote(*, value: float) -> ResolvedQuote:
    return ResolvedQuote(
        canonical_id="USD.IRS.1Y",
        requested_as_of=datetime(2026, 5, 13),
        resolved_as_of=datetime(2026, 5, 13),
        found=True,
        is_exact=True,
        value=value,
        source="vendor_x",
        vendor_id="vendor",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _settings(*, trace_capture: bool = True) -> OrchestratorSettings:
    return OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="testsha",
        trace_capture=trace_capture,
    )


@pytest.fixture
def auth_lookup() -> ApiKeyLookup:
    records = {
        API_KEY: ApiKeyRecord(
            api_key_id="ak-trace",
            owner_uid=OWNER,
            name="Trace Test",
            email="trace@example.com",
            tier="free",
            active=True,
        ),
        OTHER_KEY: ApiKeyRecord(
            api_key_id="ak-other",
            owner_uid=OTHER,
            name="Other",
            email="other@example.com",
            tier="free",
            active=True,
        ),
    }

    async def _lookup(key: str) -> ApiKeyRecord | None:
        return records.get(key)

    return _lookup


@pytest.fixture
def firebase_verifier() -> FirebaseTokenVerifier:
    def _verify(_token: str) -> dict[str, Any]:
        msg = "no firebase here"
        raise ValueError(msg)

    return _verify


def _build(
    *,
    settings: OrchestratorSettings,
    auth_lookup: ApiKeyLookup,
    firebase_verifier: FirebaseTokenVerifier,
    ro_engine: FakeEngine,
    rw_engine: FakeEngine | None,
    md_client: MdClient | None,
    engine_client: EngineClient | None,
) -> FastAPI:
    return create_app(
        settings,
        api_key_lookup=auth_lookup,
        firebase_verifier=firebase_verifier,
        app_ro_engine=cast(AsyncEngine, ro_engine),
        app_rw_engine=(cast(AsyncEngine, rw_engine) if rw_engine is not None else None),
        md_client=md_client,
        engine_client=engine_client,
    )


def _headers(key: str = API_KEY) -> dict[str, str]:
    return {"X-API-Key": key}


def _trace_inserts(
    rw_engine: FakeEngine,
) -> list[dict[str, Any]]:
    return [r.params for r in rw_engine.recordings if "INSERT INTO app.pricing_traces" in r.sql]


def _rw_handler(
    history_id: uuid.UUID,
    *,
    raise_on_trace: Exception | None = None,
) -> Any:
    def _handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "INSERT INTO app.pricing_history" in sql:
            return [{"id": str(history_id)}]
        if "INSERT INTO app.pricing_traces" in sql:
            if raise_on_trace is not None:
                raise raise_on_trace
            return []
        msg = f"unexpected rw SQL: {sql!r}"
        raise AssertionError(msg)

    return _handler


# ---------------------------------------------------------------------------
# Capture-on: full stage timeline written
# ---------------------------------------------------------------------------


def test_swap_ir_call_writes_full_trace_timeline(
    auth_lookup: ApiKeyLookup, firebase_verifier: FirebaseTokenVerifier
) -> None:
    curve_id = uuid.uuid4()
    ro = FakeEngine()
    ro.set_handler(lambda _sql, _p: [_curve_row(curve_id=curve_id)])
    rw = FakeEngine()
    rw.set_handler(_rw_handler(uuid.uuid4()))
    md = _FakeMdClient(results=[_resolved_quote(value=4.25)])
    engine = _FakeEngineClient(response=_fb_response_bytes(npv=123.0, fair_rate=0.03))

    app = _build(
        settings=_settings(),
        auth_lookup=auth_lookup,
        firebase_verifier=firebase_verifier,
        ro_engine=ro,
        rw_engine=rw,
        md_client=md,
        engine_client=engine,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
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

    inserts = _trace_inserts(rw)
    stages = [p["stage"] for p in inserts]
    assert stages == [
        "input",
        "load_entities",
        "md_resolve",
        "engine_request",
        "engine_response",
        "history_write",
    ]
    # Every row carries the owner + a single shared request_id.
    assert {p["owner_uid"] for p in inserts} == {OWNER}
    request_ids = {p["request_id"] for p in inserts}
    assert len(request_ids) == 1
    assert all(p["service"] == "orchestrator" for p in inserts)

    by_stage = {p["stage"]: json.loads(p["payload"]) for p in inserts}
    # md_resolve carries the resolved value + provenance.
    md_payload = by_stage["md_resolve"]
    assert md_payload["requested_canonical_ids"] == ["USD.IRS.1Y"]
    assert md_payload["resolved"][0]["value"] == 4.25
    assert md_payload["resolved"][0]["from_snapshot"] is False
    assert md_payload["misses"] == []
    # engine_request now carries TWO clearly-labelled views.
    eng_req = by_stage["engine_request"]
    # (a) the orchestrator's assembled-inputs superset, retained + segregated.
    assembled_view = eng_req["assembled_request"]
    assert assembled_view["as_of"] == "2026-05-13"
    assert assembled_view["curves"][0]["id"] == str(curve_id)
    assert assembled_view["resolved_quotes"][0]["canonical_id"] == "USD.IRS.1Y"
    # (b) the EXACT bytes sent to the engine, base64 + decoded-FROM-those-bytes.
    wire = eng_req["engine_wire"]
    assert wire["sent"] is True
    assert wire["rpc"] == "PriceVanillaSwap"
    decoded = wire["decoded"]
    # The decoded view is the engine-native request shape.
    assert decoded["include_flows"] is True
    assert len(decoded["swaps"]) == 1
    # The rates.indices registry shows ESTR (the registration),
    # proving the float/overnight index reached the engine wire.
    index_ids = {idx["id"] for idx in decoded["pricing"]["rates"]["indices"]}
    assert "ESTR" in index_ids
    # The wire view carries NONE of the orchestrator-only fields.
    assert "snapshot_id" not in wire
    assert "curve_set_id" not in wire
    assert "resolved_quotes" not in wire
    assert "snapshot_id" not in decoded
    assert "curve_set_id" not in decoded
    assert "resolved_quotes" not in decoded
    # Round-trip: re-decoding the captured base64 reproduces the decoded view,
    # proving the JSON is derived from the exact transmitted bytes.
    raw = base64.b64decode(wire["request_bytes_b64"])
    assert len(raw) == wire["request_bytes_len"]
    assert decode_swap_ir_request_wire(raw) == decoded
    # engine_response carries npv on success.
    assert by_stage["engine_response"]["npv"] == 123.0
    # history_write records the persisted id.
    assert by_stage["history_write"]["recorded"] is True

    # Trace summaries: every persisted stage carries a non-empty one-liner.
    _assert_success_summaries(inserts)


def _assert_success_summaries(inserts: list[dict[str, Any]]) -> None:
    """Pin the per-stage summaries on the happy-path timeline."""

    by_summary = {p["stage"]: p["summary"] for p in inserts}
    assert all(by_summary.values()), by_summary
    # md_resolve summary interprets the resolved-vs-requested counts.
    assert by_summary["md_resolve"].startswith("Resolved 1 of 1 quote")
    # engine_response summary names the NPV + the (here empty) flow counts.
    assert "NPV 123" in by_summary["engine_response"]
    assert "cashflow" in by_summary["engine_response"]
    # history_write summary names the persisted id.
    assert "Recorded to pricing_history" in by_summary["history_write"]


# ---------------------------------------------------------------------------
# Capture-off: no rows
# ---------------------------------------------------------------------------


def test_trace_capture_off_writes_no_rows(
    auth_lookup: ApiKeyLookup, firebase_verifier: FirebaseTokenVerifier
) -> None:
    curve_id = uuid.uuid4()
    ro = FakeEngine()
    ro.set_handler(lambda _sql, _p: [_curve_row(curve_id=curve_id)])
    rw = FakeEngine()
    rw.set_handler(_rw_handler(uuid.uuid4()))
    md = _FakeMdClient(results=[_resolved_quote(value=4.25)])
    engine = _FakeEngineClient(response=_fb_response_bytes(npv=1.0))

    app = _build(
        settings=_settings(trace_capture=False),
        auth_lookup=auth_lookup,
        firebase_verifier=firebase_verifier,
        ro_engine=ro,
        rw_engine=rw,
        md_client=md,
        engine_client=engine,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
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
    assert response.status_code == HTTPStatus.OK
    assert _trace_inserts(rw) == []


# ---------------------------------------------------------------------------
# Flush failure must not fail the price
# ---------------------------------------------------------------------------


def test_trace_flush_failure_does_not_fail_price(
    auth_lookup: ApiKeyLookup,
    firebase_verifier: FirebaseTokenVerifier,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    curve_id = uuid.uuid4()
    ro = FakeEngine()
    ro.set_handler(lambda _sql, _p: [_curve_row(curve_id=curve_id)])
    rw = FakeEngine()
    rw.set_handler(_rw_handler(uuid.uuid4(), raise_on_trace=RuntimeError("traces db down")))
    md = _FakeMdClient(results=[_resolved_quote(value=4.25)])
    engine = _FakeEngineClient(response=_fb_response_bytes(npv=42.0))

    # Patch the recorder's module logger directly: ``structlog`` caches
    # bound loggers on first use, so ``capture_logs`` is unreliable once
    # the recorder logger has been bound by an earlier test in the suite.
    warnings: list[dict[str, Any]] = []

    class _SpyLogger:
        def warning(self, event: str, **kw: Any) -> None:
            warnings.append({"event": event, **kw})

    monkeypatch.setattr(recorder_module, "_log", _SpyLogger())

    app = _build(
        settings=_settings(),
        auth_lookup=auth_lookup,
        firebase_verifier=firebase_verifier,
        ro_engine=ro,
        rw_engine=rw,
        md_client=md,
        engine_client=engine,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
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

    # Price still succeeds despite the trace-write blowing up.
    assert response.status_code == HTTPStatus.OK
    assert response.json()["result"]["npv"] == 42.0
    # The failure was logged at warning with the token.
    write_failed = [e for e in warnings if e["event"] == "orchestrator.pricing_trace.write_failed"]
    assert len(write_failed) == 1
    assert write_failed[0]["code"] == "pricing_trace_write_failed"


# ---------------------------------------------------------------------------
# Engine-error path captures the real engine error text
# ---------------------------------------------------------------------------


def test_engine_error_trace_captures_real_error_text(
    auth_lookup: ApiKeyLookup, firebase_verifier: FirebaseTokenVerifier
) -> None:
    curve_id = uuid.uuid4()
    ro = FakeEngine()
    ro.set_handler(lambda _sql, _p: [_curve_row(curve_id=curve_id)])
    rw = FakeEngine()
    rw.set_handler(_rw_handler(uuid.uuid4()))
    md = _FakeMdClient(results=[_resolved_quote(value=4.25)])
    engine = _FakeEngineClient(raises=RuntimeError("negative time to maturity for ESTR bootstrap"))

    app = _build(
        settings=_settings(),
        auth_lookup=auth_lookup,
        firebase_verifier=firebase_verifier,
        ro_engine=ro,
        rw_engine=rw,
        md_client=md,
        engine_client=engine,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
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

    by_stage = {p["stage"]: json.loads(p["payload"]) for p in _trace_inserts(rw)}
    assert by_stage["engine_response"]["error"] == ("negative time to maturity for ESTR bootstrap")
    # The error stage (the envelope the client saw) is also captured.
    assert "error" in by_stage

    # Trace summary: the engine_response summary surfaces the engine's REAL
    # error text, and the error stage summary names the code + message.
    by_summary = {p["stage"]: p["summary"] for p in _trace_inserts(rw)}
    assert by_summary["engine_response"] == (
        "Engine error: negative time to maturity for ESTR bootstrap"
    )
    assert by_summary["error"].startswith("Failed: ")


# ---------------------------------------------------------------------------
# Read endpoint owner-scoping
# ---------------------------------------------------------------------------


@pytest.fixture
def read_app(
    auth_lookup: ApiKeyLookup, firebase_verifier: FirebaseTokenVerifier
) -> Iterator[TestClient]:
    """App whose app_ro returns trace rows only for OWNER + request 'req-1'."""

    stored = [
        {
            "ts": datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC),
            "stage": "input",
            "level": "info",
            "duration_ms": None,
            "payload": {"product": "swaps_ir"},
        },
        {
            "ts": datetime(2026, 5, 13, 12, 0, 1, tzinfo=UTC),
            "stage": "engine_request",
            "level": "info",
            "duration_ms": 5,
            "payload": {"as_of": "2026-05-13"},
        },
    ]

    def _handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        assert "owner_uid = :owner_uid" in sql
        if params["owner_uid"] == OWNER and params["request_id"] == "req-1":
            return stored
        return []

    ro = FakeEngine()
    ro.set_handler(_handler)
    app = _build(
        settings=_settings(),
        auth_lookup=auth_lookup,
        firebase_verifier=firebase_verifier,
        ro_engine=ro,
        rw_engine=None,
        md_client=_FakeMdClient(),
        engine_client=_FakeEngineClient(response=_fb_response_bytes()),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_read_endpoint_returns_owner_timeline(read_app: TestClient) -> None:
    response = read_app.get("/v1/traces/req-1", headers=_headers())
    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["request_id"] == "req-1"
    assert [s["stage"] for s in body["stages"]] == ["input", "engine_request"]
    assert body["stages"][1]["payload"]["as_of"] == "2026-05-13"
    assert body["stages"][1]["duration_ms"] == 5


def test_read_endpoint_foreign_owner_is_404(read_app: TestClient) -> None:
    # OTHER owns no rows for req-1 → 404, never 403 (invariant #5).
    response = read_app.get("/v1/traces/req-1", headers=_headers(OTHER_KEY))
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "trace_not_found"


def test_read_endpoint_unknown_id_is_404(read_app: TestClient) -> None:
    response = read_app.get("/v1/traces/does-not-exist", headers=_headers())
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "trace_not_found"


def test_read_endpoint_requires_auth(read_app: TestClient) -> None:
    response = read_app.get("/v1/traces/req-1")
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json()["code"] == "unauthenticated"
