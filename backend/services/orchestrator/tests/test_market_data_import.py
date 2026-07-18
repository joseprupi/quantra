"""Route tests for ``POST /v1/market-data/import``.

Hermetic: the ``md_rw`` engine is a recording :class:`FakeEngine`
(from ``conftest``) so no Postgres is touched. These assert the request
contract (CSV upload + JSON batch), the per-row soft-error report, auth
gating, and the 503/400 hard-failure envelopes.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_common.settings import Environment, LogLevel
from quantra_orchestrator.app import create_app
from quantra_orchestrator.settings import OrchestratorSettings

from .conftest import FakeEngine, SqlHandler


def _settings(*, bypass: bool) -> OrchestratorSettings:
    return OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="testsha",
        dev_auth_bypass=bypass,
    )


def _as_engine(engine: FakeEngine) -> AsyncEngine:
    """Present the ``FakeEngine`` test double at the ``create_app`` boundary."""

    return cast(AsyncEngine, engine)


def _existing_series_handler(existing: set[str] | None) -> SqlHandler:
    """SQL handler reporting which canonical_ids the import existence-probe sees.

    The import path now REJECTS a value whose series is undefined, so its
    ``SELECT canonical_id ... = ANY(:ids)`` probe must return the ids that
    "exist". ``existing=None`` means "every requested id exists".
    """

    def _handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "SELECT canonical_id FROM canonical_ids WHERE canonical_id = ANY" in sql:
            ids = params.get("ids", [])
            if existing is None:
                return [{"canonical_id": c} for c in ids]
            return [{"canonical_id": c} for c in ids if c in existing]
        return []

    return _handler


@pytest.fixture
def md_rw_engine() -> FakeEngine:
    engine = FakeEngine()
    # Default: every series in a batch is treated as already defined so the
    # happy-path import assertions exercise the value-add flow.
    engine.set_handler(_existing_series_handler(None))
    return engine


@pytest.fixture
def client(md_rw_engine: FakeEngine) -> Iterator[TestClient]:
    """App with the auth bypass ON and a recording md_rw engine injected."""

    app = create_app(_settings(bypass=True), md_rw_engine=_as_engine(md_rw_engine))
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_json_batch_happy(client: TestClient, md_rw_engine: FakeEngine) -> None:
    resp = client.post(
        "/v1/market-data/import",
        json={
            "quotes": [
                {"canonical_id": "USD.IRS.5Y", "as_of": "2025-01-15", "value": 0.031},
                {
                    "canonical_id": "USD.SOFR.2Y",
                    "as_of": "2025-01-15",
                    "value": 0.0295,
                    "source": "mydesk",
                },
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"imported": 2, "skipped": 0, "errors": [], "source": "manual"}
    # Two rows -> (canonical + quote upsert) = 4 writes, all via .begin().
    quote_writes = [r for r in md_rw_engine.recordings if "quote_points" in r.sql]
    assert len(quote_writes) == 2
    assert all(r.mode == "write" for r in quote_writes)


def test_csv_upload_happy(client: TestClient, md_rw_engine: FakeEngine) -> None:
    csv_body = (
        b"canonical_id,as_of,value,source,meta_json\n"
        b"USD.IRS.5Y,2025-01-15,0.031\n"
        b'USD.SOFR.2Y,2025-01-15,0.0295,mydesk,{"note":"cpi"}\n'
    )
    resp = client.post(
        "/v1/market-data/import",
        files={"file": ("quotes.csv", csv_body, "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["imported"] == 2
    assert body["skipped"] == 0
    assert body["source"] == "csv"


def test_csv_bad_row_reported_not_500(client: TestClient) -> None:
    csv_body = (
        b"USD.IRS.5Y,2025-01-15,0.031\n"  # ok
        b"USD.IRS.2Y,2025-01-15,not-a-number\n"  # bad value
        b"not-canonical,2025-01-15,0.02\n"  # bad canonical id
    )
    resp = client.post(
        "/v1/market-data/import",
        files={"file": ("quotes.csv", csv_body, "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["imported"] == 1
    assert body["skipped"] == 2
    rows = {e["row"] for e in body["errors"]}
    assert rows == {2, 3}
    # errors sorted by row
    assert [e["row"] for e in body["errors"]] == [2, 3]


def test_json_bad_row_reported(client: TestClient) -> None:
    resp = client.post(
        "/v1/market-data/import",
        json={
            "quotes": [
                {"canonical_id": "USD.IRS.5Y", "as_of": "2025-01-15", "value": 0.031},
                {"canonical_id": "bad id", "as_of": "2025-01-15", "value": 0.02},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["imported"] == 1
    assert body["skipped"] == 1
    assert body["errors"][0]["row"] == 2
    assert "invalid canonical_id" in body["errors"][0]["reason"]


def test_import_unknown_series_rejected_not_autocreated() -> None:
    # Only USD.IRS.5Y is defined; the value for the undefined USD.MYDESK.7Y
    # must come back as a per-row error and NOT auto-create a canonical row.
    engine = FakeEngine()
    engine.set_handler(_existing_series_handler({"USD.IRS.5Y"}))
    app = create_app(_settings(bypass=True), md_rw_engine=_as_engine(engine))
    with TestClient(app, raise_server_exceptions=False) as unknown_client:
        resp = unknown_client.post(
            "/v1/market-data/import",
            json={
                "quotes": [
                    {"canonical_id": "USD.IRS.5Y", "as_of": "2025-01-15", "value": 0.031},
                    {"canonical_id": "USD.MYDESK.7Y", "as_of": "2025-01-15", "value": 0.05},
                ]
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["imported"] == 1
    assert body["skipped"] == 1
    assert body["errors"][0]["row"] == 2
    assert "does not exist" in body["errors"][0]["reason"]
    # No canonical_ids write happened for the unknown series.
    assert not any("INTO canonical_ids" in r.sql for r in engine.recordings)
    quote_ids = [r.params["canonical_id"] for r in engine.recordings if "quote_points" in r.sql]
    assert quote_ids == ["USD.IRS.5Y"]


def test_empty_json_batch_is_400(client: TestClient) -> None:
    resp = client.post("/v1/market-data/import", json={"quotes": []})
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "market_data_import_invalid_request"


def test_unauthenticated_is_401(md_rw_engine: FakeEngine) -> None:
    app = create_app(_settings(bypass=False), md_rw_engine=_as_engine(md_rw_engine))
    with TestClient(app, raise_server_exceptions=False) as no_auth_client:
        resp = no_auth_client.post(
            "/v1/market-data/import",
            json={"quotes": [{"canonical_id": "USD.IRS.5Y", "as_of": "2025-01-15", "value": 0.03}]},
        )
    assert resp.status_code == 401, resp.text
    assert resp.json()["code"] == "unauthenticated"


def test_md_rw_engine_missing_is_503() -> None:
    # Auth bypass on, but no md_rw engine wired → storage_unavailable.
    app = create_app(_settings(bypass=True))
    with TestClient(app, raise_server_exceptions=False) as bare_client:
        resp = bare_client.post(
            "/v1/market-data/import",
            json={"quotes": [{"canonical_id": "USD.IRS.5Y", "as_of": "2025-01-15", "value": 0.03}]},
        )
    assert resp.status_code == 503, resp.text
    assert resp.json()["code"] == "storage_unavailable"
