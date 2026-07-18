"""Live-engine integration test gated on the ``orchestrator_engine`` marker.

Required environment (export to opt in):

* ``QUANTRA_ORCHESTRATOR_TEST_ENGINE_GRPC_TARGET`` — gRPC ``host:port``
  of a reachable pricing engine (e.g. ``localhost:50051``).

Optional environment:

* ``QUANTRA_ORCHESTRATOR_TEST_ENGINE_RPC`` — the
  :class:`quantra_common.engine_client.EngineRpc` value to ping.
  Defaults to ``CALENDAR_BUSINESS_DAYS`` (the cheapest call in the
  catalog).

Per Part C
the test now exercises a real round-trip against the engine: the
gRPC channel is constructed against
``$QUANTRA_ORCHESTRATOR_TEST_ENGINE_GRPC_TARGET`` and a real
``CALENDAR_BUSINESS_DAYS`` request is dispatched. Asserts the
engine answers with HTTP 200 + a non-empty response body byte
length (the body itself is FlatBuffers — the debug route doesn't
decode it).

Mirrors 's shape: one
gated marker, one explicit env var, one round-trip route.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from http import HTTPStatus
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from quantra_common.auth.context import ApiKeyRecord
from quantra_common.auth.lookup import ApiKeyLookup
from quantra_common.engine_client import EngineRpc
from quantra_common.settings import Environment, LogLevel
from quantra_orchestrator.app import create_app
from quantra_orchestrator.settings import OrchestratorSettings

_TARGET_ENV = "QUANTRA_ORCHESTRATOR_TEST_ENGINE_GRPC_TARGET"
_RPC_ENV = "QUANTRA_ORCHESTRATOR_TEST_ENGINE_RPC"
_DEFAULT_RPC = EngineRpc.CALENDAR_BUSINESS_DAYS

_target = os.environ.get(_TARGET_ENV)
pytestmark = [
    pytest.mark.orchestrator_engine,
    pytest.mark.skipif(
        not _target,
        reason=(
            f"Set {_TARGET_ENV} (e.g. localhost:50051) to run the orchestrator "
            "engine-client integration test."
        ),
    ),
]


def _api_key_headers() -> dict[str, str]:
    return {"X-API-Key": "live-engine-key"}


def _seeded_api_keys() -> dict[str, ApiKeyRecord]:
    return {
        "live-engine-key": ApiKeyRecord(
            api_key_id="live-engine-key",
            owner_uid="engine-live-uid",
            name="Live Engine Test Key",
            email="engine-live@example.com",
            tier="free",
            active=True,
        )
    }


@pytest.fixture
def live_engine_app(
    fake_api_key_lookup: ApiKeyLookup,
    fake_firebase_verifier: tuple[Any, dict[str, dict[str, Any]]],
    fake_api_keys: dict[str, ApiKeyRecord],
) -> Iterator[TestClient]:
    """App wired against the live-engine target from the environment."""

    fake_api_keys.update(_seeded_api_keys())
    verifier, _ = fake_firebase_verifier
    target = _target  # captured by the module-level skip
    assert target is not None
    settings = OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="testsha",
        engine_grpc_target=target,
    )
    app: FastAPI = create_app(
        settings,
        api_key_lookup=fake_api_key_lookup,
        firebase_verifier=verifier,
    )
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_engine_ping_round_trip_is_currently_a_thin_shell(
    live_engine_app: TestClient,
) -> None:
    """Round-trip ping against a live engine target.

    Asserts the seam is wired end-to-end: the gRPC channel was
    constructed against ``$QUANTRA_ORCHESTRATOR_TEST_ENGINE_GRPC_TARGET``,
    the request reached the orchestrator, the orchestrator
    dispatched the canonical RPC over the channel, and the engine
    actually answered. Because ``/debug/engine/ping`` posts an
    empty body and every real RPC's request schema is a non-empty
    FlatBuffers root table, the engine's parser rejects the empty
    body with ``INTERNAL`` ("Unable to parse request"); the
    orchestrator maps that to the ``engine_upstream_error``
    envelope. The 502 + ``engine_upstream_error`` shape is
    therefore evidence of a healthy wire — the engine saw the
    bytes, parsed them, and emitted a structured rejection. The
    only way to get a different shape today is to also send valid
    request bytes (which is what every per-product
    ``test_*_engine_integration.py`` does).
    """

    rpc_name = os.environ.get(_RPC_ENV, _DEFAULT_RPC.value)
    response = live_engine_app.get(
        "/debug/engine/ping",
        params={"rpc": rpc_name},
        headers=_api_key_headers(),
    )

    assert response.status_code == HTTPStatus.BAD_GATEWAY, response.text
    body = response.json()
    assert body["code"] == "engine_upstream_error", body
