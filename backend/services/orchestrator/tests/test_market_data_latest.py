"""Route tests for ``GET /v1/market-data/latest-date``.

Hermetic: the ``md_rw`` engine is a recording :class:`FakeEngine` driven by a
tiny handler that returns the ``max(as_of)`` per (source, prefix) filter, so no
Postgres is touched. These pin the response shape, the optional source/prefix
filters (and their bound params), the empty-catalog ``null`` behavior, and auth
gating.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi.testclient import TestClient

from quantra_common.settings import Environment, LogLevel
from quantra_orchestrator.app import create_app
from quantra_orchestrator.settings import OrchestratorSettings

from .conftest import FakeEngine


def _settings(*, bypass: bool) -> OrchestratorSettings:
    return OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="testsha",
        dev_auth_bypass=bypass,
    )


def _handler(
    *,
    latest_all: date | None,
    by_source: dict[str, date] | None = None,
    by_prefix: dict[str, date] | None = None,
) -> Any:
    src = by_source or {}
    pfx = by_prefix or {}

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "max(as_of)" not in sql:
            return []
        source = params.get("source")
        prefix = params.get("prefix")
        if prefix is not None:
            return [{"latest_date": pfx.get(prefix.rstrip("%"))}]
        if source is not None:
            return [{"latest_date": src.get(source)}]
        return [{"latest_date": latest_all}]

    return handler


def _build(handler: Any, *, bypass: bool = True) -> tuple[TestClient, FakeEngine]:
    engine = FakeEngine()
    engine.set_handler(handler)
    app = create_app(_settings(bypass=bypass), md_rw_engine=engine)  # type: ignore[arg-type]
    return TestClient(app, raise_server_exceptions=False), engine


def test_latest_date_overall() -> None:
    client, _ = _build(_handler(latest_all=date(2026, 7, 14)))
    with client:
        resp = client.get("/v1/market-data/latest-date")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"latest_date": "2026-07-14", "source": None, "prefix": None}


def test_latest_date_by_prefix() -> None:
    client, engine = _build(
        _handler(
            latest_all=date(2026, 7, 14),
            by_prefix={"GBP.RATES.BOE.OIS.": date(2026, 7, 11)},
        )
    )
    with client:
        resp = client.get("/v1/market-data/latest-date", params={"prefix": "GBP.RATES.BOE.OIS."})
    assert resp.status_code == 200, resp.text
    assert resp.json()["latest_date"] == "2026-07-11"
    assert resp.json()["prefix"] == "GBP.RATES.BOE.OIS."
    reads = [r for r in engine.recordings if "max(as_of)" in r.sql]
    assert reads[-1].params["prefix"] == "GBP.RATES.BOE.OIS.%"


def test_latest_date_by_source() -> None:
    client, _ = _build(_handler(latest_all=date(2026, 7, 14), by_source={"BOE": date(2026, 7, 11)}))
    with client:
        resp = client.get("/v1/market-data/latest-date", params={"source": "BOE"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["latest_date"] == "2026-07-11"
    assert resp.json()["source"] == "BOE"


def test_latest_date_empty_catalog_returns_null() -> None:
    client, _ = _build(_handler(latest_all=None))
    with client:
        resp = client.get("/v1/market-data/latest-date")
    assert resp.status_code == 200, resp.text
    assert resp.json()["latest_date"] is None


def test_latest_date_requires_auth() -> None:
    client, _ = _build(_handler(latest_all=date(2026, 7, 14)), bypass=False)
    with client:
        resp = client.get("/v1/market-data/latest-date")
    assert resp.status_code == 401, resp.text
