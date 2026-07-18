"""In-app pricing-trace rollout tests (swaption + bonds + cds + rest).

Mirrors ``test_swap_ir_tracing.py`` for the products instrumented in
 step 2. Reuses each
product's existing API-test builders (curve rows, inline payloads,
FlatBuffers response synthesisers, fake MD / engine clients) so the
trace assertions ride on the same fixtures that prove the pricing path
itself.

Covers, per product:

* The full stage timeline is written on success
  (input → load_entities → md_resolve → engine_request →
  engine_response → history_write), owner-scoped, single request_id,
  carrying the ``product`` column tag.
* ``md_resolve`` + ``engine_request`` payloads carry real content.
* The engine-error path captures the engine's REAL error text on the
  ``engine_response`` stage (not the mapped error token) and still emits
  the ``error`` stage.
* ``TRACE_CAPTURE`` off writes nothing.

A read-endpoint owner-scoping check rounds out invariant #5 for the
rollout.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_common.auth.context import ApiKeyRecord
from quantra_common.auth.firebase import FirebaseTokenVerifier
from quantra_common.auth.lookup import ApiKeyLookup
from quantra_common.engine_client import EngineClient
from quantra_common.md_client import MdClient
from quantra_common.settings import Environment, LogLevel
from quantra_orchestrator.app import create_app
from quantra_orchestrator.settings import OrchestratorSettings
from quantra_orchestrator.tracing import recorder as recorder_module

from . import test_bonds_api as bd
from . import test_cds_api as cd
from . import test_equity_options_api as eq
from . import test_swaps_inflation_api as si
from . import test_swaption_api as sw
from .conftest import FakeEngine

OWNER = "user-trace-rollout"
OTHER = "user-other-rollout"
API_KEY = "key-trace-rollout"
OTHER_KEY = "key-other-rollout"

ENGINE_ERR_TEXT = "negative time to maturity for ESTR bootstrap"

EXPECTED_STAGES = [
    "input",
    "load_entities",
    "md_resolve",
    "engine_request",
    "engine_response",
    "history_write",
]


# ---------------------------------------------------------------------------
# Per-product case construction (reuses each product's API-test builders)
# ---------------------------------------------------------------------------


class _Case:
    def __init__(
        self,
        *,
        product: str,
        url: str,
        ro_handler: Callable[[str, dict[str, Any]], list[dict[str, Any]]],
        md_client: MdClient,
        payload: dict[str, Any],
        first_canonical_id: str,
    ) -> None:
        self.product = product
        self.url = url
        self.ro_handler = ro_handler
        self.md_client = md_client
        self.payload = payload
        self.first_canonical_id = first_canonical_id


def _swaption_case(*, engine: EngineClient) -> tuple[_Case, EngineClient]:
    curve_id = uuid.uuid4()
    md = sw._FakeMdClient(
        results=[
            sw._resolved_quote(canonical_id="USD.IRS.1Y", value=4.25),
            sw._resolved_quote(canonical_id="USD.SWPTN.ATM.5Y10Y.VOL", value=0.65),
        ]
    )
    case = _Case(
        product="swaptions",
        url="/v1/price/swaption",
        ro_handler=lambda _sql, _p: [sw._curve_row(curve_id=curve_id)],
        md_client=md,
        payload=sw._inline_payload(curve_id=curve_id),
        first_canonical_id="USD.IRS.1Y",
    )
    return case, engine


def _bond_fixed_case(*, engine: EngineClient) -> tuple[_Case, EngineClient]:
    curve_id = uuid.uuid4()
    md = bd._FakeMdClient(results=[bd._resolved_quote(canonical_id="USD.IRS.1Y", value=4.25)])
    case = _Case(
        product="bonds_fixed",
        url="/v1/price/bonds/fixed",
        ro_handler=lambda _sql, _p: [bd._curve_row(curve_id=curve_id)],
        md_client=md,
        payload={
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
        first_canonical_id="USD.IRS.1Y",
    )
    return case, engine


def _bond_floating_case(*, engine: EngineClient) -> tuple[_Case, EngineClient]:
    discount_id = uuid.uuid4()
    projection_id = uuid.uuid4()

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            cid = params["curve_id"]
            if cid == str(discount_id):
                return [bd._curve_row(curve_id=discount_id)]
            if cid == str(projection_id):
                return [
                    bd._curve_row(
                        curve_id=projection_id,
                        name="USD-SOFR-3M",
                        quote_id="USD.IRS.3M",
                    )
                ]
            return []
        return []

    md = bd._FakeMdClient(
        results=[
            bd._resolved_quote(canonical_id="USD.IRS.1Y", value=4.25),
            bd._resolved_quote(canonical_id="USD.IRS.3M", value=4.35),
        ]
    )
    case = _Case(
        product="bonds_floating",
        url="/v1/price/bonds/floating",
        ro_handler=handler,
        md_client=md,
        payload={
            "bond": {
                "face_amount": 100.0,
                "issue_date": "2025-01-17",
                "effective_date": "2025-01-17",
                "termination_date": "2030-01-17",
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
        first_canonical_id="USD.IRS.1Y",
    )
    return case, engine


def _equity_option_case(*, engine: EngineClient) -> tuple[_Case, EngineClient]:
    discount_id = uuid.uuid4()
    surface_id = uuid.uuid4()

    def handler(sql: str, _p: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [eq._curve_row(curve_id=discount_id)]
        if "FROM app.vol_surfaces" in sql:
            return [eq._vol_surface_row(vol_surface_id=surface_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    md = eq._FakeMdClient(results=[eq._resolved_quote(canonical_id="USD.IRS.1Y", value=4.25)])
    case = _Case(
        product="equity_options",
        url="/v1/price/equity-option",
        ro_handler=handler,
        md_client=md,
        payload=eq._inline_payload(discount_id=discount_id, surface_id=surface_id),
        first_canonical_id="USD.IRS.1Y",
    )
    return case, engine


def _swaps_inflation_case(*, engine: EngineClient) -> tuple[_Case, EngineClient]:
    md = si._FakeMdClient(
        results=[
            si._resolved_quote(canonical_id="EUR.IRS.1Y", value=0.03),
            si._resolved_quote(canonical_id="EUR.HICP.1Y", value=0.02),
        ]
    )
    case = _Case(
        product="swaps_inflation",
        url="/v1/price/swaps/inflation",
        ro_handler=lambda _sql, _p: [],
        md_client=md,
        payload=si._inline_payload(swap_kind="zero_coupon"),
        first_canonical_id="EUR.IRS.1Y",
    )
    return case, engine


def _cds_case(*, engine: EngineClient) -> tuple[_Case, EngineClient]:
    curve_id = uuid.uuid4()
    credit_id = uuid.uuid4()

    def handler(sql: str, _p: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [cd._curve_row(curve_id=curve_id)]
        if "FROM app.credit_curves" in sql:
            return [cd._credit_curve_row(credit_curve_id=credit_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    md = cd._FakeMdClient(results=[cd._resolved_quote(canonical_id="USD.IRS.1Y", value=4.25)])
    case = _Case(
        product="cds",
        url="/v1/price/cds",
        ro_handler=handler,
        md_client=md,
        payload=cd._inline_payload(curve_id=curve_id, credit_curve_id=credit_id),
        first_canonical_id="USD.IRS.1Y",
    )
    return case, engine


def _ok_engine(product: str) -> EngineClient:
    if product == "swaptions":
        return sw._FakeEngineClient(response=sw._fb_response_bytes(npv=234_567.89))
    if product == "bonds_fixed":
        return bd._FakeEngineClient(response=bd._fb_fixed_response(npv=999.0, clean_price=99.5))
    if product == "bonds_floating":
        return bd._FakeEngineClient(response=bd._fb_floating_response(npv=2_000.0))
    if product == "equity_options":
        return eq._FakeEngineClient(response=eq._fb_response_bytes(npv=12_345.67))
    if product == "swaps_inflation":
        return si._FakeEngineClient(
            response=si._zciis_response_bytes(npv=12_345.0, fair_rate=0.0217)
        )
    return cd._FakeEngineClient(
        response=cd._fb_response_bytes(
            npv=123_456.78,
            fair_spread=0.0125,
            fair_upfront=0.012,
            protection_leg_npv=50_000.0,
            premium_leg_npv=-50_000.0,
        )
    )


def _err_engine(product: str) -> EngineClient:
    raises = RuntimeError(ENGINE_ERR_TEXT)
    if product == "swaptions":
        return sw._FakeEngineClient(raises=raises)
    if product in ("bonds_fixed", "bonds_floating"):
        return bd._FakeEngineClient(raises=raises)
    if product == "equity_options":
        return eq._FakeEngineClient(raises=raises)
    if product == "swaps_inflation":
        return si._FakeEngineClient(raises=raises)
    return cd._FakeEngineClient(raises=raises)


_CASE_BUILDERS: dict[str, Callable[..., tuple[_Case, EngineClient]]] = {
    "swaptions": _swaption_case,
    "bonds_fixed": _bond_fixed_case,
    "bonds_floating": _bond_floating_case,
    "cds": _cds_case,
    "equity_options": _equity_option_case,
    "swaps_inflation": _swaps_inflation_case,
}

_ALL_PRODUCTS = [
    "swaptions",
    "bonds_fixed",
    "bonds_floating",
    "cds",
    "equity_options",
    "swaps_inflation",
]


# ---------------------------------------------------------------------------
# Fixtures / harness
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
    md_client: MdClient,
    engine_client: EngineClient,
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


def _trace_inserts(rw_engine: FakeEngine) -> list[dict[str, Any]]:
    return [r.params for r in rw_engine.recordings if "INSERT INTO app.pricing_traces" in r.sql]


def _rw_handler(
    history_id: uuid.UUID, *, raise_on_trace: Exception | None = None
) -> Callable[[str, dict[str, Any]], list[dict[str, Any]]]:
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


def _post_case(
    case: _Case,
    engine: EngineClient,
    auth_lookup: ApiKeyLookup,
    firebase_verifier: FirebaseTokenVerifier,
    *,
    trace_capture: bool = True,
    rw: FakeEngine | None = None,
) -> tuple[Any, FakeEngine | None]:
    ro = FakeEngine()
    ro.set_handler(case.ro_handler)
    if rw is None:
        rw = FakeEngine()
        rw.set_handler(_rw_handler(uuid.uuid4()))
    app = _build(
        settings=_settings(trace_capture=trace_capture),
        auth_lookup=auth_lookup,
        firebase_verifier=firebase_verifier,
        ro_engine=ro,
        rw_engine=rw,
        md_client=case.md_client,
        engine_client=engine,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(case.url, json=case.payload, headers=_headers())
    return response, rw


# ---------------------------------------------------------------------------
# Success: full stage timeline + product tag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("product", _ALL_PRODUCTS)
def test_call_writes_full_trace_timeline(
    product: str,
    auth_lookup: ApiKeyLookup,
    firebase_verifier: FirebaseTokenVerifier,
) -> None:
    case, engine = _CASE_BUILDERS[product](engine=_ok_engine(product))
    response, rw = _post_case(case, engine, auth_lookup, firebase_verifier)
    assert response.status_code == HTTPStatus.OK, response.text
    assert rw is not None

    inserts = _trace_inserts(rw)
    assert [p["stage"] for p in inserts] == EXPECTED_STAGES
    # Owner-scoped, single request_id, orchestrator service, product tag.
    assert {p["owner_uid"] for p in inserts} == {OWNER}
    assert len({p["request_id"] for p in inserts}) == 1
    assert all(p["service"] == "orchestrator" for p in inserts)
    assert {p["product"] for p in inserts} == {product}

    by_stage = {p["stage"]: json.loads(p["payload"]) for p in inserts}
    by_summary = {p["stage"]: p["summary"] for p in inserts}
    # input carries the product tag inline too.
    assert by_stage["input"]["product"] == product
    # md_resolve carries the resolved value + provenance.
    md_payload = by_stage["md_resolve"]
    assert case.first_canonical_id in md_payload["requested_canonical_ids"]
    resolved_ids = [r["canonical_id"] for r in md_payload["resolved"]]
    assert case.first_canonical_id in resolved_ids
    assert all(r["from_snapshot"] is False for r in md_payload["resolved"])
    # engine_request now carries TWO clearly-labelled views.
    eng_req = by_stage["engine_request"]
    # (a) the orchestrator's assembled-inputs superset, retained + segregated.
    assembled_view = eng_req["assembled_request"]
    assert assembled_view["as_of"] == case.payload["as_of"]
    assembled_ids = [q["canonical_id"] for q in assembled_view["resolved_quotes"]]
    assert case.first_canonical_id in assembled_ids
    # (b) The EXACT bytes sent to the engine are now captured for EVERY
    # product (previously swap_ir only): sent, an rpc name, a non-empty base64
    # buffer that round-trips its length, and a decoded-FROM-those-bytes view.
    wire = eng_req["engine_wire"]
    assert wire["sent"] is True
    assert isinstance(wire["rpc"], str)
    assert wire["rpc"]
    assert wire["request_bytes_len"] > 0
    raw = base64.b64decode(wire["request_bytes_b64"])
    assert len(raw) == wire["request_bytes_len"]
    assert isinstance(wire["decoded"], dict)
    assert wire["decoded"]
    # The wire view carries NONE of the orchestrator-only fields.
    for orch_only in ("snapshot_id", "curve_set_id", "resolved_quotes"):
        assert orch_only not in wire
        assert orch_only not in wire["decoded"]
    # the stage summary reflects reality (sent/decoded), NOT the old
    # misleading "not sent — pre-send failure" a successful non-swap_ir price
    # used to show before wire capture existed.
    assert "not sent" not in by_summary["engine_request"]
    assert by_summary["engine_request"].startswith("Sent ")
    # engine_response carries npv on success; history_write records the id.
    assert by_stage["engine_response"]["npv"] is not None
    assert by_stage["history_write"]["recorded"] is True


# ---------------------------------------------------------------------------
# Engine-error path captures the REAL engine error text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("product", _ALL_PRODUCTS)
def test_engine_error_trace_captures_real_error_text(
    product: str,
    auth_lookup: ApiKeyLookup,
    firebase_verifier: FirebaseTokenVerifier,
) -> None:
    case, engine = _CASE_BUILDERS[product](engine=_err_engine(product))
    response, rw = _post_case(case, engine, auth_lookup, firebase_verifier)
    assert response.status_code == HTTPStatus.BAD_GATEWAY
    assert rw is not None

    by_stage = {p["stage"]: json.loads(p["payload"]) for p in _trace_inserts(rw)}
    assert by_stage["engine_response"]["error"] == ENGINE_ERR_TEXT
    assert by_stage["engine_response"]["exc_type"] == "RuntimeError"
    # The error stage (the envelope the client saw) is also captured,
    # carrying a mapped engine_* error code.
    assert "error" in by_stage
    assert by_stage["error"]["error"]["code"].startswith("engine_")


# ---------------------------------------------------------------------------
# Capture-off: no rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("product", _ALL_PRODUCTS)
def test_trace_capture_off_writes_no_rows(
    product: str,
    auth_lookup: ApiKeyLookup,
    firebase_verifier: FirebaseTokenVerifier,
) -> None:
    case, engine = _CASE_BUILDERS[product](engine=_ok_engine(product))
    response, rw = _post_case(case, engine, auth_lookup, firebase_verifier, trace_capture=False)
    assert response.status_code == HTTPStatus.OK, response.text
    assert rw is not None
    assert _trace_inserts(rw) == []


# ---------------------------------------------------------------------------
# Flush failure must not fail the price (one product is enough)
# ---------------------------------------------------------------------------


def test_trace_flush_failure_does_not_fail_price(
    auth_lookup: ApiKeyLookup,
    firebase_verifier: FirebaseTokenVerifier,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case, engine = _CASE_BUILDERS["cds"](engine=_ok_engine("cds"))
    rw = FakeEngine()
    rw.set_handler(_rw_handler(uuid.uuid4(), raise_on_trace=RuntimeError("traces db down")))

    warnings: list[dict[str, Any]] = []

    class _SpyLogger:
        def warning(self, event: str, **kw: Any) -> None:
            warnings.append({"event": event, **kw})

    monkeypatch.setattr(recorder_module, "_log", _SpyLogger())

    response, _ = _post_case(case, engine, auth_lookup, firebase_verifier, rw=rw)
    # Price still succeeds despite the trace-write blowing up.
    assert response.status_code == HTTPStatus.OK, response.text
    write_failed = [e for e in warnings if e["event"] == "orchestrator.pricing_trace.write_failed"]
    assert len(write_failed) == 1
    assert write_failed[0]["code"] == "pricing_trace_write_failed"


# ---------------------------------------------------------------------------
# Read endpoint owner-scoping (invariant #5) for the rollout
# ---------------------------------------------------------------------------


@pytest.fixture
def read_app(
    auth_lookup: ApiKeyLookup, firebase_verifier: FirebaseTokenVerifier
) -> Iterator[TestClient]:
    stored = [
        {
            "ts": datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC),
            "stage": "input",
            "level": "info",
            "duration_ms": None,
            "payload": {"product": "swaptions"},
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
        if params["owner_uid"] == OWNER and params["request_id"] == "req-roll":
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
        md_client=cd._FakeMdClient(results=[]),
        engine_client=cd._FakeEngineClient(response=cd._fb_response_bytes(npv=1.0)),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_read_endpoint_returns_owner_timeline(read_app: TestClient) -> None:
    response = read_app.get("/v1/traces/req-roll", headers=_headers())
    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert [s["stage"] for s in body["stages"]] == ["input", "engine_request"]


def test_read_endpoint_foreign_owner_is_404(read_app: TestClient) -> None:
    response = read_app.get("/v1/traces/req-roll", headers=_headers(OTHER_KEY))
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "trace_not_found"
