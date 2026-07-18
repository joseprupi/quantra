"""Live MD service integration test for the orchestrator.

Skipped by default behind the ``orchestrator_md`` marker (the shared marker family). To run locally:

1. Boot the monorepo Postgres + the MD service::

       docker compose up -d postgres market_data
       uv run alembic -n app upgrade head
       uv run alembic -n md upgrade head

2. Seed at least one canonical id + a quote so the round-trip has
   something to return. The simplest path is an MD ingester run;
   the test below also
   accepts a pre-seeded test fixture if you have one.

3. Export the MD service base URL the orchestrator should hit::

       export QUANTRA_ORCHESTRATOR_TEST_MD_BASE_URL=http://localhost:8082
       export QUANTRA_ORCHESTRATOR_TEST_MD_CANONICAL_ID=US.TREASURY.10Y.YIELD
       export QUANTRA_ORCHESTRATOR_TEST_MD_AS_OF=2026-05-13

4. Run the marker::

       uv run pytest -m orchestrator_md services/orchestrator

The test exercises the same wiring production uses end-to-end:
``RequestIdMiddleware`` → auth dependency → ``get_md_client`` →
``MdClient.resolve_quotes`` → real HTTP → MD service → real response →
cache write → second call returns from cache without an additional
upstream hop.
"""

from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient

from quantra_common.auth.context import ApiKeyRecord
from quantra_common.auth.lookup import ApiKeyLookup
from quantra_common.settings import Environment, LogLevel
from quantra_orchestrator.app import create_app
from quantra_orchestrator.settings import OrchestratorSettings

pytestmark = pytest.mark.orchestrator_md

QUANTRA_ORCHESTRATOR_TEST_MD_BASE_URL_ENV = "QUANTRA_ORCHESTRATOR_TEST_MD_BASE_URL"
QUANTRA_ORCHESTRATOR_TEST_MD_CANONICAL_ID_ENV = "QUANTRA_ORCHESTRATOR_TEST_MD_CANONICAL_ID"
QUANTRA_ORCHESTRATOR_TEST_MD_AS_OF_ENV = "QUANTRA_ORCHESTRATOR_TEST_MD_AS_OF"


def _md_base_url_or_skip() -> str:
    url = os.environ.get(QUANTRA_ORCHESTRATOR_TEST_MD_BASE_URL_ENV)
    if not url:
        pytest.skip(
            f"Set {QUANTRA_ORCHESTRATOR_TEST_MD_BASE_URL_ENV} (e.g. "
            "http://localhost:8082) to run the orchestrator MD integration test.",
            allow_module_level=False,
        )
    return url


def _canonical_id() -> str:
    return os.environ.get(
        QUANTRA_ORCHESTRATOR_TEST_MD_CANONICAL_ID_ENV,
        "US.TREASURY.10Y.YIELD",
    )


def _as_of() -> str:
    return os.environ.get(QUANTRA_ORCHESTRATOR_TEST_MD_AS_OF_ENV, "2026-05-13")


def _seeded_api_keys() -> dict[str, ApiKeyRecord]:
    return {
        "md-int-key": ApiKeyRecord(
            api_key_id="md-int",
            owner_uid="md-int-uid",
            name="MD Integration Test Key",
            email="md-int@example.com",
            tier="free",
            active=True,
        )
    }


def _api_key_lookup(records: dict[str, ApiKeyRecord]) -> ApiKeyLookup:
    async def _lookup(key: str) -> ApiKeyRecord | None:
        return records.get(key)

    return _lookup


async def test_debug_md_quote_round_trips_against_real_md_service() -> None:
    """End-to-end exercise: orchestrator → MD service → response.

    Verifies the singleton MdClient + cache from the lifespan actually
    talk to a live MD service. The first call is a cache miss (one
    upstream HTTP request); the second is a cache hit (no additional
    upstream traffic). Cache stats reflect the difference.
    """

    base_url = _md_base_url_or_skip()
    canonical_id = _canonical_id()
    as_of = _as_of()
    keys = _seeded_api_keys()

    settings = OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="md-int",
        md_service_url=base_url,
        md_service_timeout_s=5.0,
        md_client_max_retries=0,
        md_cache_max_entries=64,
        md_cache_ttl_s=300.0,
    )
    instance = create_app(settings, api_key_lookup=_api_key_lookup(keys))

    transport = ASGITransport(app=instance)
    async with (
        AsyncClient(transport=transport, base_url="http://orch") as client,
        instance.router.lifespan_context(instance),
    ):
        first = await client.get(
            f"/debug/md/quote/{canonical_id}",
            params={"as_of": as_of},
            headers={
                "X-API-Key": "md-int-key",
                "X-Request-Id": "rid-md-int-1",
            },
        )
        second = await client.get(
            f"/debug/md/quote/{canonical_id}",
            params={"as_of": as_of},
            headers={
                "X-API-Key": "md-int-key",
                "X-Request-Id": "rid-md-int-2",
            },
        )
        stats = await client.get(
            "/debug/md/cache/stats",
            headers={"X-API-Key": "md-int-key"},
        )

    if first.status_code == 404:
        pytest.skip(
            f"MD service has no quote for canonical_id={canonical_id} at "
            f"as_of={as_of}. Seed one (e.g. with the MD ingester) or set "
            f"{QUANTRA_ORCHESTRATOR_TEST_MD_CANONICAL_ID_ENV} / "
            f"{QUANTRA_ORCHESTRATOR_TEST_MD_AS_OF_ENV} to a known-present "
            "canonical id + as_of."
        )

    assert first.status_code == 200, first.text
    body = first.json()
    assert body["canonical_id"] == canonical_id
    assert body["found"] is True
    assert isinstance(body["value"], (int, float))

    assert second.status_code == 200
    assert second.json() == body

    assert stats.status_code == 200
    stats_body = stats.json()
    assert stats_body["hits"] >= 1
    assert stats_body["misses"] >= 1
    assert stats_body["size"] >= 1
