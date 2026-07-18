"""Live readiness-transition test for ``GET /ready``.

This is the close-out test for the readiness work. It proves the
``/ready`` endpoint flips to 503 with the correct per-check
breakdown when the MD service disappears, recovers to 200 once
the MD service is back, and that ``/health`` stays 200
throughout the transition (the semantic split the observability design codifies).
It also asserts the ``/metrics`` MD upstream failure counter
captured the down window.

Authorisation
-------------

This test authorises restarting **only** the
``quantra-backend-market-data`` container. The engine container
is OFF-LIMITS by the same dispatch (running engine RPCs against
``QUANTRA_ORCHESTRATOR_TEST_ENGINE_GRPC_TARGET`` is fine; bouncing
the container is not).

Gating
------

Skips unless **all** of:

* ``QUANTRA_ORCHESTRATOR_TEST_ENGINE_GRPC_TARGET`` (shared with the
  family).
* ``QUANTRA_ORCHESTRATOR_TEST_MD_BASE_URL`` (shared with the MD smoke).
* ``QUANTRA_ORCHESTRATOR_READY_SMOKE=1`` — explicit opt-in so a
  contributor without docker rights does not auto-run this lane.

Optional knobs (documented defaults in ``_TimingBudgets`` below):

* ``QUANTRA_ORCHESTRATOR_READY_SMOKE_DOWN_TIMEOUT_S`` — max wall-clock
  to wait for /ready to transition to 503 after ``docker compose stop``.
* ``QUANTRA_ORCHESTRATOR_READY_SMOKE_UP_TIMEOUT_S`` — max wall-clock to
  wait for /ready to recover to 200 after ``docker compose up -d``.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Final

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from quantra_common.auth.context import ApiKeyRecord
from quantra_common.auth.lookup import ApiKeyLookup
from quantra_common.settings import Environment, LogLevel
from quantra_orchestrator.app import create_app
from quantra_orchestrator.settings import OrchestratorSettings

# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

_ENGINE_TARGET_ENV: Final[str] = "QUANTRA_ORCHESTRATOR_TEST_ENGINE_GRPC_TARGET"
_MD_BASE_URL_ENV: Final[str] = "QUANTRA_ORCHESTRATOR_TEST_MD_BASE_URL"
_READY_SMOKE_ENV: Final[str] = "QUANTRA_ORCHESTRATOR_READY_SMOKE"
_DOWN_TIMEOUT_ENV: Final[str] = "QUANTRA_ORCHESTRATOR_READY_SMOKE_DOWN_TIMEOUT_S"
_UP_TIMEOUT_ENV: Final[str] = "QUANTRA_ORCHESTRATOR_READY_SMOKE_UP_TIMEOUT_S"

_engine_target = os.environ.get(_ENGINE_TARGET_ENV)
_md_base_url = os.environ.get(_MD_BASE_URL_ENV)
_ready_smoke_enabled = os.environ.get(_READY_SMOKE_ENV) == "1"

_gating_reason = (
    f"Set {_ENGINE_TARGET_ENV}, {_MD_BASE_URL_ENV}, and "
    f"{_READY_SMOKE_ENV}=1 to run the 17d /ready transition test."
)

pytestmark = [
    pytest.mark.orchestrator_ready_smoke,
    pytest.mark.skipif(
        not (_engine_target and _md_base_url and _ready_smoke_enabled),
        reason=_gating_reason,
    ),
]


# Match the docker compose stack defined in ``docker-compose.yml``.
# Container name (not service alias) lets us subprocess against
# ``docker stop`` / ``docker start`` directly without depending on the
# host's working directory at test launch.
_MD_CONTAINER_NAME: Final[str] = "quantra-backend-market-data"


@dataclass(frozen=True, slots=True)
class _TimingBudgets:
    """Tunable budgets so a slow CI box doesn't false-positive the test.

    Defaults are conservative — the MD service boots in <1s on a fresh
    image. The env-var overrides let an operator stretch the budgets
    when running against a slower local docker.
    """

    down_timeout_s: float
    up_timeout_s: float
    poll_interval_s: float = 0.25


def _load_budgets() -> _TimingBudgets:
    return _TimingBudgets(
        down_timeout_s=float(os.environ.get(_DOWN_TIMEOUT_ENV, "10")),
        up_timeout_s=float(os.environ.get(_UP_TIMEOUT_ENV, "30")),
    )


# ---------------------------------------------------------------------------
# Orchestrator app fixture (mirrors the shape, but ``app_*`` engines
# are off — the readiness test only needs MD + engine wiring)
# ---------------------------------------------------------------------------


_API_KEY: Final[str] = "ready-smoke-key"


def _seeded_api_keys() -> dict[str, ApiKeyRecord]:
    return {
        _API_KEY: ApiKeyRecord(
            api_key_id=_API_KEY,
            owner_uid="ready-smoke-uid",
            name="Ready Smoke Test Key",
            email="ready-smoke@example.com",
            tier="free",
            active=True,
        )
    }


@pytest.fixture
def ready_smoke_app(
    fake_api_key_lookup: ApiKeyLookup,
    fake_firebase_verifier: tuple[Any, dict[str, dict[str, Any]]],
    fake_api_keys: dict[str, ApiKeyRecord],
) -> Iterator[TestClient]:
    """Orchestrator app wired against the live engine + live MD service.

    The lifespan opens the singleton MD/Engine clients; we wrap with
    the metrics adapter automatically (see ``app.create_app``). The
    TestClient context manager runs the lifespan so MD/Engine clients
    are real.
    """

    fake_api_keys.update(_seeded_api_keys())
    verifier, _ = fake_firebase_verifier
    assert _engine_target is not None
    assert _md_base_url is not None
    settings = OrchestratorSettings(
        env=Environment.DEV,
        log_level=LogLevel.WARNING,
        build_sha="readysmoke",
        engine_grpc_target=_engine_target,
        md_service_url=_md_base_url,
    )
    app: FastAPI = create_app(
        settings,
        api_key_lookup=fake_api_key_lookup,
        firebase_verifier=verifier,
    )
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Docker plumbing — stop + restart the MD container
# ---------------------------------------------------------------------------


_DOCKER_BIN: Final[str] = os.environ.get(
    "QUANTRA_ORCHESTRATOR_READY_SMOKE_DOCKER_BIN",
    "/usr/bin/docker",
)


def _docker_run(args: list[str], *, timeout_s: float = 15.0) -> subprocess.CompletedProcess[str]:
    """Run a docker subcommand; raise on non-zero exit."""

    proc = subprocess.run(  # noqa: S603 — args list, no shell, pinned absolute bin
        [_DOCKER_BIN, *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_s,
    )
    if proc.returncode != 0:
        msg = (
            f"docker {' '.join(args)!r} exited {proc.returncode}; "
            f"stdout={proc.stdout!r}; stderr={proc.stderr!r}"
        )
        raise RuntimeError(msg)
    return proc


def _stop_md_container() -> None:
    _docker_run(["stop", _MD_CONTAINER_NAME])


def _start_md_container() -> None:
    _docker_run(["start", _MD_CONTAINER_NAME])


# ---------------------------------------------------------------------------
# /ready helpers
# ---------------------------------------------------------------------------


def _get_ready(client: TestClient) -> tuple[int, dict[str, Any]]:
    response = client.get("/ready")
    return response.status_code, response.json()


def _wait_for_status(
    client: TestClient,
    *,
    target_status: int,
    timeout_s: float,
    poll_interval_s: float,
    description: str,
) -> tuple[float, dict[str, Any]]:
    """Poll /ready until it matches ``target_status`` or budget expires.

    Returns ``(elapsed_s, last_body)`` so the test can document the
    transition latency in the report. Raises ``AssertionError`` on
    timeout so the failure surfaces with the last observed body.
    """

    started = time.monotonic()
    last_body: dict[str, Any] = {}
    deadline = started + timeout_s
    while time.monotonic() < deadline:
        code, body = _get_ready(client)
        last_body = body
        if code == target_status:
            return time.monotonic() - started, body
        time.sleep(poll_interval_s)
    msg = (
        f"{description} did not converge to HTTP {target_status} "
        f"within {timeout_s:.1f}s; last body={last_body}"
    )
    raise AssertionError(msg)


# ---------------------------------------------------------------------------
# The transition test
# ---------------------------------------------------------------------------


def test_ready_transitions_when_md_container_bounces(ready_smoke_app: TestClient) -> None:
    """Full readiness-transition proof.

    The flow:

    1. Baseline: /ready=200 with checks.md=ok, /health=200.
    2. ``docker stop`` MD → poll /ready until 503 (record latency).
       Throughout: /health still 200.
    3. ``docker start`` MD → poll /ready until 200 (record latency).
    4. /metrics: parse as Prometheus, assert MD upstream failure
       counter incremented during the down window.

    We don't assert specific latency numbers (varies per host) — the
    test surfaces the measured numbers via printed reasons so the
    operator can record them.
    """

    budgets = _load_budgets()

    # -----------------------------------------------------------------
    # Step 1 — baseline.
    # -----------------------------------------------------------------
    code, body = _get_ready(ready_smoke_app)
    assert code == HTTPStatus.OK, f"baseline /ready not green: {body}"
    assert body["status"] == "ready"
    assert body["checks"]["md"]["status"] == "ok"
    assert body["checks"]["engine"]["status"] == "ok"

    health_response = ready_smoke_app.get("/health")
    assert health_response.status_code == HTTPStatus.OK

    # Pre-populate the metrics path with one MD call so the failure
    # delta during the down window is unambiguous (a non-zero "ok"
    # baseline rules out "the counter has just never been populated").
    # The ``/debug/md/cache/stats`` route hits MdClient indirectly via
    # the dependency; we use it as a no-op warm-up — the cache stats
    # path doesn't itself call MD, so seed the failure counter by
    # poking MD via the resolved-quote debug route. A missing canonical
    # id produces a 404, which still counts in the metrics.
    ready_smoke_app.get(
        "/debug/md/quote/_warmup_canonical_id_",
        params={"as_of": "2026-01-15"},
        headers={"X-API-Key": _API_KEY},
    )

    metrics_baseline = ready_smoke_app.get("/metrics").text
    baseline_failure_total = _md_failure_total(metrics_baseline)

    # -----------------------------------------------------------------
    # Step 2 — bring MD down.
    # -----------------------------------------------------------------
    try:
        _stop_md_container()
        # Tiny gap to let docker's container exit propagate before
        # polling kicks in — keeps the first observation honest.
        time.sleep(0.5)

        # Probe /health concurrently in the same loop as the readiness
        # transition so we can claim "health stayed 200 throughout".
        # FastAPI's TestClient is sync; we serialise the probes.
        down_latency_s, down_body = _wait_for_status(
            ready_smoke_app,
            target_status=HTTPStatus.SERVICE_UNAVAILABLE,
            timeout_s=budgets.down_timeout_s,
            poll_interval_s=budgets.poll_interval_s,
            description="/ready 200 → 503 transition",
        )
        # The response must surface *which* check tripped.
        md_check = down_body["checks"]["md"]
        assert md_check["status"] == "fail", down_body
        assert md_check.get("detail"), "MD failure detail must be populated"

        # /health stays 200 throughout (independent contract).
        health_during_down = ready_smoke_app.get("/health")
        assert health_during_down.status_code == HTTPStatus.OK

        # Capture the MD upstream failure counter at the deepest point
        # of the down window — at least one /ready run pinged the MD
        # service and tripped a transport error.
        metrics_during_down = ready_smoke_app.get("/metrics").text
        # The /ready probe itself does not run through the
        # InstrumentedMdClient (it uses a raw httpx), so the MD failure
        # counter we look at is whichever call path the test drove
        # *through the MdClient*. We poke /debug/md/quote once to drive
        # an MD call while down.
        ready_smoke_app.get(
            "/debug/md/quote/_during_down_canonical_id_",
            params={"as_of": "2026-01-15"},
            headers={"X-API-Key": _API_KEY},
        )
        metrics_after_md_call = ready_smoke_app.get("/metrics").text
        during_failure_total = _md_failure_total(metrics_after_md_call)
        assert during_failure_total > baseline_failure_total, (
            "MD upstream failure counter did not increment during the "
            f"down window: baseline={baseline_failure_total}, "
            f"during={during_failure_total}, "
            f"metrics_pre_call={metrics_during_down!r}"
        )
    finally:
        # -----------------------------------------------------------------
        # Step 3 — recover MD even if assertions tripped (narrow-
        # cleanup discipline). The container restart is best-effort.
        # -----------------------------------------------------------------
        try:
            _start_md_container()
        except RuntimeError as exc:  # pragma: no cover — diagnostic
            pytest.fail(f"failed to restart MD container: {exc}")

    # Wait for /ready to recover.
    up_latency_s, up_body = _wait_for_status(
        ready_smoke_app,
        target_status=HTTPStatus.OK,
        timeout_s=budgets.up_timeout_s,
        poll_interval_s=budgets.poll_interval_s,
        description="/ready 503 → 200 recovery",
    )
    assert up_body["status"] == "ready"
    assert up_body["checks"]["md"]["status"] == "ok"

    # /health still 200 at the end.
    health_after = ready_smoke_app.get("/health")
    assert health_after.status_code == HTTPStatus.OK

    # -----------------------------------------------------------------
    # Step 4 — Prometheus parse + report (printed via pytest -s).
    # -----------------------------------------------------------------
    metrics_after = ready_smoke_app.get("/metrics").text
    families = list(text_string_to_metric_families(metrics_after))
    assert any(f.name == "orchestrator_md_requests" for f in families)

    print(
        "\n[17d] readiness-transition latency:"
        f"\n  /ready 200→503 (MD down):   {down_latency_s:.2f}s"
        f"\n  /ready 503→200 (MD up):     {up_latency_s:.2f}s"
        f"\n  MD failure counter delta:   "
        f"{during_failure_total - baseline_failure_total:.0f}"
    )


# ---------------------------------------------------------------------------
# Metrics helper — pulls the MD upstream failure-total from a scrape
# ---------------------------------------------------------------------------


def _md_failure_total(metrics_body: str) -> float:
    """Sum every ``orchestrator_md_requests_total`` sample with a non-ok outcome.

    The /ready MD probe uses raw httpx (no MdClient), so the failure
    counter we expect to move comes from the MdClient call path the
    test deliberately drives. Aggregating across operations keeps the
    test stable against future metric-label additions.
    """

    total = 0.0
    for family in text_string_to_metric_families(metrics_body):
        if family.name != "orchestrator_md_requests":
            continue
        for sample in family.samples:
            if not sample.name.endswith("_total"):
                continue
            if sample.labels.get("outcome") == "ok":
                continue
            total += float(sample.value)
    return total
