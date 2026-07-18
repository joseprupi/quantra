"""Route tests for the ``/v1/market-data/series`` CRUD surface.

Hermetic: the ``md_rw`` engine is a recording :class:`FakeEngine` (from
``conftest``) driven by a tiny in-memory ``_FakeCatalog`` handler, so no
Postgres is touched. These assert the create/list/read/patch/delete
contracts, the 409 / 404 / 422 envelopes, auth gating, and the delete
cascade count.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_common.settings import Environment, LogLevel
from quantra_orchestrator.app import create_app
from quantra_orchestrator.settings import OrchestratorSettings

from .conftest import FakeEngine

_TS = datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC)
_INSERT_COLUMNS = (
    "canonical_id",
    "asset_class",
    "family",
    "instrument",
    "currency",
    "tenor",
    "field",
    "frequency",
    "units",
    "description",
)


def _settings(*, bypass: bool) -> OrchestratorSettings:
    return OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="testsha",
        dev_auth_bypass=bypass,
    )


_COLUMN_DEFAULTS = {
    "instrument": "INSTR",
    "field": "RATE",
    "frequency": "daily",
    "units": "decimal_rate",
}


def _full_row(params: dict[str, Any]) -> dict[str, Any]:
    row = {col: params.get(col, _COLUMN_DEFAULTS.get(col)) for col in _INSERT_COLUMNS}
    row["created_at"] = _TS
    row["updated_at"] = _TS
    return row


class _FakeCatalog:
    """In-memory ``md.canonical_ids`` + quote-count store for CRUD tests."""

    def __init__(
        self,
        rows: dict[str, dict[str, Any]] | None = None,
        quote_counts: dict[str, int] | None = None,
    ) -> None:
        self.rows: dict[str, dict[str, Any]] = dict(rows or {})
        self.quote_counts: dict[str, int] = dict(quote_counts or {})

    def handler(  # noqa: PLR0911
        self, sql: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        s = sql.strip()
        if "INSERT INTO canonical_ids" in s:
            cid = params["canonical_id"]
            if cid in self.rows:
                return []  # ON CONFLICT DO NOTHING → no returned row → 409
            self.rows[cid] = _full_row(params)
            return [self.rows[cid]]
        if s.startswith("SELECT") and "ORDER BY canonical_id" in s:
            return [self.rows[k] for k in sorted(self.rows)]
        if s.startswith("SELECT") and "WHERE canonical_id = :canonical_id" in s:
            cid = params["canonical_id"]
            return [self.rows[cid]] if cid in self.rows else []
        if s.startswith("UPDATE canonical_ids"):
            cid = params["canonical_id"]
            if cid not in self.rows:
                return []
            for key, value in params.items():
                if key != "canonical_id":
                    self.rows[cid][key] = value
            self.rows[cid]["updated_at"] = _TS
            return [self.rows[cid]]
        if "DELETE FROM quote_points" in s:
            return [{"deleted_quote_points": self.quote_counts.get(params["canonical_id"], 0)}]
        if s.startswith("DELETE FROM canonical_ids"):
            cid = params["canonical_id"]
            if cid in self.rows:
                del self.rows[cid]
                return [{"canonical_id": cid}]
            return []
        return []


def _client(catalog: _FakeCatalog, *, bypass: bool = True) -> Iterator[TestClient]:
    engine = FakeEngine()
    engine.set_handler(catalog.handler)
    # Present the test double at the create_app boundary as a real AsyncEngine.
    app = create_app(_settings(bypass=bypass), md_rw_engine=cast(AsyncEngine, engine))
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def catalog() -> _FakeCatalog:
    return _FakeCatalog()


@pytest.fixture
def client(catalog: _FakeCatalog) -> Iterator[TestClient]:
    yield from _client(catalog)


def test_create_series_happy(client: TestClient) -> None:
    resp = client.post(
        "/v1/market-data/series",
        json={
            "canonical_id": "USD.MYDESK.7Y",
            "asset_class": "RATES",
            "currency": "USD",
            "description": "My desk 7Y rate",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["canonical_id"] == "USD.MYDESK.7Y"
    assert body["asset_class"] == "RATES"
    assert body["currency"] == "USD"
    # Structural gaps derived from the canonical-id grammar.
    assert body["family"] == "MYDESK"
    assert body["instrument"] == "MYDESK"
    assert body["tenor"] == "7Y"
    assert body["field"] == "RATE"
    # Catalog defaults.
    assert body["frequency"] == "daily"
    assert body["units"] == "decimal_rate"
    assert body["description"] == "My desk 7Y rate"


def test_create_series_duplicate_is_409(catalog: _FakeCatalog) -> None:
    catalog.rows["USD.MYDESK.7Y"] = _full_row(
        {"canonical_id": "USD.MYDESK.7Y", "asset_class": "RATES", "currency": "USD"}
    )
    it = _client(catalog)
    dup_client = next(it)
    resp = dup_client.post(
        "/v1/market-data/series",
        json={"canonical_id": "USD.MYDESK.7Y", "asset_class": "RATES", "currency": "USD"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "market_data_series_exists"


def test_create_series_bad_id_is_422(client: TestClient) -> None:
    resp = client.post(
        "/v1/market-data/series",
        json={"canonical_id": "not a canonical id", "asset_class": "RATES", "currency": "USD"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "market_data_series_invalid"


def test_list_series(catalog: _FakeCatalog) -> None:
    for cid in ("USD.SOFR.2Y", "USD.IRS.5Y"):
        catalog.rows[cid] = _full_row(
            {"canonical_id": cid, "asset_class": "RATES", "currency": "USD"}
        )
    it = _client(catalog)
    list_client = next(it)
    resp = list_client.get("/v1/market-data/series")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 2
    # Ordered by canonical_id.
    assert [s["canonical_id"] for s in body["series"]] == ["USD.IRS.5Y", "USD.SOFR.2Y"]


def test_read_series_404(client: TestClient) -> None:
    resp = client.get("/v1/market-data/series/USD.NOPE.9Y")
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "market_data_series_not_found"


def test_patch_series_happy(catalog: _FakeCatalog) -> None:
    catalog.rows["USD.MYDESK.7Y"] = _full_row(
        {
            "canonical_id": "USD.MYDESK.7Y",
            "asset_class": "RATES",
            "currency": "USD",
            "family": "MYDESK",
            "instrument": "MYDESK",
            "tenor": "7Y",
            "field": "RATE",
            "frequency": "daily",
            "units": "decimal_rate",
            "description": "old",
        }
    )
    it = _client(catalog)
    patch_client = next(it)
    resp = patch_client.patch(
        "/v1/market-data/series/USD.MYDESK.7Y",
        json={"description": "new desc"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["description"] == "new desc"


def test_patch_series_404(client: TestClient) -> None:
    resp = client.patch("/v1/market-data/series/USD.NOPE.9Y", json={"description": "x"})
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "market_data_series_not_found"


def test_patch_series_empty_required_field_is_422(catalog: _FakeCatalog) -> None:
    catalog.rows["USD.MYDESK.7Y"] = _full_row(
        {"canonical_id": "USD.MYDESK.7Y", "asset_class": "RATES", "currency": "USD"}
    )
    it = _client(catalog)
    patch_client = next(it)
    resp = patch_client.patch("/v1/market-data/series/USD.MYDESK.7Y", json={"asset_class": "  "})
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "market_data_series_invalid"


def test_delete_series_happy_reports_cascade(catalog: _FakeCatalog) -> None:
    catalog.rows["USD.MYDESK.7Y"] = _full_row(
        {"canonical_id": "USD.MYDESK.7Y", "asset_class": "RATES", "currency": "USD"}
    )
    catalog.quote_counts["USD.MYDESK.7Y"] = 3
    it = _client(catalog)
    del_client = next(it)
    resp = del_client.delete("/v1/market-data/series/USD.MYDESK.7Y")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "canonical_id": "USD.MYDESK.7Y",
        "deleted": True,
        "deleted_quote_points": 3,
    }
    assert "USD.MYDESK.7Y" not in catalog.rows


def test_delete_series_404(client: TestClient) -> None:
    resp = client.delete("/v1/market-data/series/USD.NOPE.9Y")
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "market_data_series_not_found"


def test_create_series_unauthenticated_is_401(catalog: _FakeCatalog) -> None:
    it = _client(catalog, bypass=False)
    no_auth = next(it)
    resp = no_auth.post(
        "/v1/market-data/series",
        json={"canonical_id": "USD.MYDESK.7Y", "asset_class": "RATES", "currency": "USD"},
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["code"] == "unauthenticated"


def test_series_md_rw_engine_missing_is_503() -> None:
    app = create_app(_settings(bypass=True))
    with TestClient(app, raise_server_exceptions=False) as bare:
        resp = bare.get("/v1/market-data/series")
    assert resp.status_code == 503, resp.text
    assert resp.json()["code"] == "storage_unavailable"
