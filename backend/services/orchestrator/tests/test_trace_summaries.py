"""Per-stage human-readable summaries (shared trace helpers).

The summaries are generated inside the SHARED stage helpers in
``quantra_orchestrator.tracing.stages`` from the data each stage already
has, so every product inherits them at once. These tests pin the wording
for the states that matter on the investigate screen — especially the
"correct but empty" states that otherwise look broken — and prove
``GET /v1/traces`` surfaces ``summary`` as a top-level field per stage.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any, cast

import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_common.auth.context import ApiKeyRecord
from quantra_common.auth.firebase import FirebaseTokenVerifier
from quantra_common.auth.lookup import ApiKeyLookup
from quantra_common.settings import Environment, LogLevel
from quantra_orchestrator.app import create_app
from quantra_orchestrator.settings import OrchestratorSettings
from quantra_orchestrator.tracing.stages import (
    _summarize_engine_error,
    _summarize_engine_request,
    _summarize_engine_response,
    _summarize_error,
    _summarize_history_write,
    _summarize_input,
    _summarize_load_entities,
    _summarize_md_resolve,
    failure_envelope,
)

from .conftest import FakeEngine

OWNER = "user-summary"
OTHER = "user-summary-other"
API_KEY = "key-summary"
OTHER_KEY = "key-summary-other"


# ---------------------------------------------------------------------------
# input
# ---------------------------------------------------------------------------


def test_input_summary_reads_like_a_trade_ticket() -> None:
    summary = _summarize_input(
        "swaps_ir",
        {
            "notional": 10_000_000.0,
            "swap_type": "Payer",
            "fixed_rate": 0.025,
            "effective_date": "2026-02-11",
            "termination_date": "2031-02-11",
            "as_of": "2026-02-09",
        },
    )
    assert summary == (
        "Priced swaps_ir: 10,000,000 Payer @ 2.5%, 2026-02-11 → 2031-02-11, as_of 2026-02-09."
    )


def test_input_summary_falls_back_to_ref_shape() -> None:
    summary = _summarize_input(
        "cds",
        {"cds_id": "abc-123", "as_of": "2026-05-13", "inline_cds": False},
    )
    assert summary == "Priced cds: saved abc-123, as_of 2026-05-13."


def test_input_summary_is_defensive_on_empty_params() -> None:
    assert _summarize_input("bonds_fixed", {}) == "Priced bonds_fixed."


# ---------------------------------------------------------------------------
# load_entities
# ---------------------------------------------------------------------------


def test_load_entities_summary_lists_named_curves() -> None:
    summary = _summarize_load_entities(
        {"curve_set_id": None, "curve_names": ["EUR-ESTR-OIS"], "index": "ESTR"}
    )
    assert summary == "Loaded 1 curve (EUR-ESTR-OIS), index ESTR from inputs."


def test_load_entities_summary_handles_role_prefixed_curves() -> None:
    summary = _summarize_load_entities({"discount_curve_id": "d-1", "credit_curve_id": "c-1"})
    assert "discount curve" in summary
    assert "credit curve" in summary


# ---------------------------------------------------------------------------
# md_resolve — the empty / inline case is the whole point
# ---------------------------------------------------------------------------


def test_md_resolve_summary_empty_explains_inline_rates() -> None:
    summary = _summarize_md_resolve(requested=[], resolved=0, live=0, snapshot=0, misses=0)
    assert summary == (
        "No market-data quotes resolved — curve uses inline rates, "
        "nothing pulled from the market-data service."
    )


def test_md_resolve_summary_counts_live_snapshot_and_misses() -> None:
    summary = _summarize_md_resolve(
        requested=["A", "B", "C"], resolved=2, live=1, snapshot=1, misses=1
    )
    assert summary == "Resolved 2 of 3 quotes (1 live, 1 from snapshot); 1 miss."


# ---------------------------------------------------------------------------
# engine_request
# ---------------------------------------------------------------------------


def test_engine_request_summary_names_rpc_bytes_index_flows() -> None:
    payload = {
        "engine_wire": {
            "sent": True,
            "rpc": "PriceVanillaSwap",
            "request_bytes_len": 1096,
            "decoded": {
                "include_flows": True,
                "pricing": {"rates": {"indices": [{"id": "ESTR"}]}},
            },
        }
    }
    summary = _summarize_engine_request(payload)
    assert summary == (
        "Sent PriceVanillaSwap to the engine (1096 bytes); "
        "curve registers index ESTR; include_flows=true."
    )


def test_engine_request_summary_handles_pre_send_failure() -> None:
    summary = _summarize_engine_request({"engine_wire": {"sent": False}})
    assert "not sent" in summary


# ---------------------------------------------------------------------------
# engine_response (success + error)
# ---------------------------------------------------------------------------


def test_engine_response_summary_names_npv_and_flow_counts() -> None:
    payload = {
        "npv": 16_756.17,
        "leg_npvs": [
            {"role": "fixed", "npv": -1_160_090.0},
            {"role": "floating", "npv": 1_176_847.0},
        ],
        "fixed_leg_flows": [{}] * 5,
        "floating_leg_flows": [{}] * 10,
    }
    summary = _summarize_engine_response(payload)
    assert summary == (
        "Engine returned NPV 16,756.17 (fixed -1,160,090 / floating 1,176,847); "
        "5 fixed + 10 floating cashflows."
    )


def test_engine_error_summary_surfaces_real_text() -> None:
    summary = _summarize_engine_error(RuntimeError("negative time to maturity for ESTR bootstrap"))
    assert summary == "Engine error: negative time to maturity for ESTR bootstrap"


# ---------------------------------------------------------------------------
# history_write — the skip must name a reason
# ---------------------------------------------------------------------------


def test_history_write_summary_records_id() -> None:
    assert _summarize_history_write("hist-9", outcome="success_row") == (
        "Recorded to pricing_history (id=hist-9)."
    )


def test_history_write_summary_skip_names_reason() -> None:
    summary = _summarize_history_write(None, outcome="success_row")
    assert summary.startswith("Audit-log write skipped")
    assert "app.users" in summary


# ---------------------------------------------------------------------------
# error
# ---------------------------------------------------------------------------


def test_error_summary_names_code_and_message() -> None:
    exc = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="nope")
    exc.code = "swap_ir_not_found"  # type: ignore[attr-defined]
    summary = _summarize_error(failure_envelope(exc))
    assert summary == "Failed: swap_ir_not_found — nope"


# ---------------------------------------------------------------------------
# GET /v1/traces surfaces the summary as a top-level field per stage
# ---------------------------------------------------------------------------


def _settings() -> OrchestratorSettings:
    return OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="testsha",
        trace_capture=True,
    )


@pytest.fixture
def auth_lookup() -> ApiKeyLookup:
    records = {
        API_KEY: ApiKeyRecord(
            api_key_id="ak",
            owner_uid=OWNER,
            name="S",
            email="s@example.com",
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


@pytest.fixture
def read_app(
    auth_lookup: ApiKeyLookup, firebase_verifier: FirebaseTokenVerifier
) -> Iterator[TestClient]:
    stored = [
        {
            "ts": datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC),
            "stage": "md_resolve",
            "level": "info",
            "duration_ms": 2,
            "summary": (
                "No market-data quotes resolved — curve uses inline rates, "
                "nothing pulled from the market-data service."
            ),
            "payload": {"requested_canonical_ids": []},
        },
        {
            "ts": datetime(2026, 5, 13, 12, 0, 1, tzinfo=UTC),
            "stage": "engine_response",
            "level": "info",
            "duration_ms": 5,
            "summary": "Engine returned NPV 16,756.17; 5 fixed + 10 floating cashflows.",
            "payload": {"npv": 16_756.17},
        },
    ]

    def _handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if params["owner_uid"] == OWNER and params["request_id"] == "req-s":
            return stored
        return []

    ro = FakeEngine()
    ro.set_handler(_handler)
    app = create_app(
        _settings(),
        api_key_lookup=auth_lookup,
        firebase_verifier=firebase_verifier,
        app_ro_engine=cast(AsyncEngine, ro),
        app_rw_engine=None,
        md_client=None,
        engine_client=None,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_get_traces_surfaces_summary_top_level(read_app: TestClient) -> None:
    response = read_app.get("/v1/traces/req-s", headers={"X-API-Key": API_KEY})
    assert response.status_code == HTTPStatus.OK, response.text
    stages = response.json()["stages"]
    # Every returned stage carries a non-empty top-level ``summary``.
    assert all(s["summary"] for s in stages)
    by_stage = {s["stage"]: s["summary"] for s in stages}
    assert "inline rates, nothing pulled" in by_stage["md_resolve"]
    assert "NPV 16,756.17" in by_stage["engine_response"]
    assert "cashflows" in by_stage["engine_response"]
