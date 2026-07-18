"""Live-engine integration test for the CDS pricing endpoint (input-sensitivity).

Gated on the ``orchestrator_engine_cds`` marker so it
stays skipped in default CI runs (only opt-in CI runs that ship a live
``quantra-engine`` container should hit this test). The worker rules
forbid running ``live`` / ``orchestrator_engine_*`` markers locally, so
this test is **authored** for the gated CI lane and verified there, not
in the hermetic suite.

Required environment (export to opt in):

* ``QUANTRA_ORCHESTRATOR_TEST_ENGINE_GRPC_TARGET`` — gRPC ``host:port``
  of a reachable pricing engine (e.g. ``localhost:50051``). Shared with
  the cross-product gated test because a single live engine serves every
  RPC the orchestrator forwards.

Input-sensitivity, not just finite NPV
---------------------------------------------------------------
Previously the orchestrator discarded the caller's credit curve and
posted the canonical flat 2 % / 40 % fixture, which the engine priced to
**all-zero** CDS diagnostics. The faithful translator consumes
the resolved credit curve faithfully, so these gated assertions move from
"finite NPV" to **input-sensitivity**: a non-trivial hazard /
par-spread must yield non-zero ``fair_spread`` / ``protection_leg_npv`` /
``premium_leg_npv``, and a *larger* hazard must move ``fair_spread``.

All-zero-diagnostics closure trigger
-------------------
:func:`test_price_cds_par_spread_bootstrap_against_live_engine` is the
**first exercise of the engine par-spread bootstrap path anywhere** — it
seeds inline ``CdsQuote`` par-spreads and the engine bootstraps a non-flat
survival curve (``flat_hazard_rate == 0`` ⇒ bootstrap).
When this lane runs green (non-trivial survival probabilities ⇒ non-zero
diagnostics) the all-zero-diagnostics defect is **CLOSED** — the all-zero
diagnostics
were the canonical-placeholder symptom, not an engine bug.

Numeric parity against reference values is deliberately out of scope
here; we pin non-zero + input-sensitivity, not NPV to 1e-6.
"""

from __future__ import annotations

import math
import os
import uuid
from collections.abc import Iterator
from datetime import datetime
from http import HTTPStatus
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from quantra_common.auth.context import ApiKeyRecord
from quantra_common.auth.lookup import ApiKeyLookup
from quantra_common.md_client import MdClient, MdClientConfig
from quantra_common.settings import Environment, LogLevel
from quantra_common.types.market_data import ResolvedQuote
from quantra_orchestrator.app import create_app
from quantra_orchestrator.settings import OrchestratorSettings

_TARGET_ENV = "QUANTRA_ORCHESTRATOR_TEST_ENGINE_GRPC_TARGET"
_target = os.environ.get(_TARGET_ENV)
pytestmark = [
    pytest.mark.orchestrator_engine_cds,
    pytest.mark.skipif(
        not _target,
        reason=(
            f"Set {_TARGET_ENV} (e.g. localhost:50051) to run the "
            "orchestrator CDS live-engine integration test."
        ),
    ),
]


class _StaticMdClient(MdClient):
    """Returns one resolved-quote per requested canonical ID (all flat 3 %).

    Lets the integration test exercise the full vertical (assemble → MD
    resolve → engine call) without provisioning a live MD service. Every
    discount-curve quote id resolves to 0.03 so the bootstrapped discount
    curve is ~flat 3 %; the credit-curve hazard inputs are inline
    and never reach MD.
    """

    def __init__(self) -> None:
        super().__init__(
            MdClientConfig(base_url="http://stub", timeout_s=1.0, max_retries=0),
            client=httpx.AsyncClient(base_url="http://stub"),
        )

    async def resolve_quotes(
        self,
        canonical_ids: list[str],
        as_of: Any,
        *,
        snapshot_version: str | None = None,
    ) -> list[ResolvedQuote]:
        del as_of, snapshot_version
        return [
            ResolvedQuote(
                canonical_id=cid,
                requested_as_of=datetime(2025, 1, 15, 0, 0),
                resolved_as_of=datetime(2025, 1, 15, 0, 0),
                found=True,
                is_exact=True,
                value=0.03,
                source="live-engine-test",
                vendor_id="FAKE",
            )
            for cid in canonical_ids
        ]


def _api_key_headers() -> dict[str, str]:
    return {"X-API-Key": "live-cds-key"}


def _seeded_api_keys() -> dict[str, ApiKeyRecord]:
    return {
        "live-cds-key": ApiKeyRecord(
            api_key_id="live-cds-key",
            owner_uid="cds-live-uid",
            name="Live CDS Test Key",
            email="cds-live@example.com",
            tier="free",
            active=True,
        )
    }


def _deposit_point(quote_id: str) -> dict[str, Any]:
    return {
        "point_type": "DepositHelper",
        "point": {
            "tenor": {"n": 1, "unit": "Years"},
            "fixing_days": 2,
            "calendar": "TARGET",
            "business_day_convention": "ModifiedFollowing",
            "day_counter": "Actual365Fixed",
            "quote_id": quote_id,
        },
    }


def _swap_point(years: int, quote_id: str) -> dict[str, Any]:
    return {
        "point_type": "SwapHelper",
        "point": {
            "tenor": {"n": years, "unit": "Years"},
            "calendar": "TARGET",
            "sw_fixed_leg_frequency": "Annual",
            "sw_fixed_leg_convention": "ModifiedFollowing",
            "sw_fixed_leg_day_counter": "Thirty360",
            "quote_id": quote_id,
        },
    }


def _discount_curve() -> dict[str, Any]:
    """A bootstrappable ~flat 3 % discount curve spanning past the longest CDS.

    The faithful translator consumes these points; every quote id
    resolves to 0.03 via :class:`_StaticMdClient`, so the engine bootstraps a
    discount curve that can value the CDS schedule and bootstrap the credit
    curve helpers.

    The curve must extend **past** the longest credit-curve par-spread tenor's
    IMM-rolled maturity, not merely match it: a 10Y CDS helper rolls to the
    TwentiethIMM date (~March 2035, t≈10.18y), so a curve ending at the 10Y
    node (t≈10.011y) leaves the bootstrap discounting cashflows past the curve's
    last node — the engine then raises "time is past max curve time" and zeroes
    the result (one of the two all-zero symptoms). A 12Y node covers it with room to
    spare.
    """

    return {
        "name": "USD-OIS",
        "currency": "USD",
        "day_counter": "Actual365Fixed",
        "reference_date": "2025-01-15",
        "points": [
            _deposit_point("USD.IRS.1Y"),
            _swap_point(2, "USD.IRS.2Y"),
            _swap_point(5, "USD.IRS.5Y"),
            _swap_point(10, "USD.IRS.10Y"),
            _swap_point(12, "USD.IRS.12Y"),
        ],
    }


def _cds_body() -> dict[str, Any]:
    return {
        "notional": 10_000_000.0,
        "running_coupon": 0.01,
        "schedule": {
            "effective_date": "2025-01-15",
            "termination_date": "2030-01-15",
        },
    }


@pytest.fixture
def live_engine_app(
    fake_api_key_lookup: ApiKeyLookup,
    fake_firebase_verifier: tuple[Any, dict[str, dict[str, Any]]],
    fake_api_keys: dict[str, ApiKeyRecord],
) -> Iterator[TestClient]:
    """Orchestrator app wired against the live-engine target.

    Uses the stub MD client above + the lifespan-owned gRPC engine pointed at
    the live target. No ``app_ro`` engine is needed — the test exercises the
    inline credit-curve + inline discount-curve path which skips ``app.*``.
    """

    fake_api_keys.update(_seeded_api_keys())
    verifier, _ = fake_firebase_verifier
    target = _target
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
        md_client=_StaticMdClient(),
    )
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _price(client: TestClient, credit_curve: dict[str, Any]) -> dict[str, Any]:
    """POST one inline CDS request and return the parsed success body."""

    payload = {
        "cds": _cds_body(),
        "curves": [_discount_curve()],
        "credit_curve": credit_curve,
        "as_of": "2025-01-15",
    }
    response = client.post(
        "/v1/price/cds",
        json=payload,
        headers={**_api_key_headers(), "X-Request-Id": f"cds-live-{uuid.uuid4()}"},
    )
    assert response.status_code == HTTPStatus.OK, response.text
    body: dict[str, Any] = response.json()
    return body


def test_price_cds_flat_hazard_input_sensitivity_against_live_engine(
    live_engine_app: TestClient,
) -> None:
    """A distinctive flat hazard / recovery yields non-zero, input-sensitive diagnostics.

    the orchestrator now posts the caller's hazard (not the
    canonical 2 % / 40 %), so the engine returns non-zero ``fair_spread`` /
    ``protection_leg_npv`` / ``premium_leg_npv`` — the all-zero symptom is
    gone. A *larger* hazard must move ``fair_spread`` up (par spread grows with
    hazard * (1 - recovery)), the strongest input-sensitivity signal.
    """

    low = _price(
        live_engine_app,
        {
            "name": "ACME-SR low",
            "recovery_rate": 0.3,
            "source": "flat",
            "body": {"flat_hazard_rate": 0.02},
        },
    )
    high = _price(
        live_engine_app,
        {
            "name": "ACME-SR high",
            "recovery_rate": 0.3,
            "source": "flat",
            "body": {"flat_hazard_rate": 0.06},
        },
    )

    for body in (low, high):
        result = body["result"]
        assert math.isfinite(float(result["npv"]))
        assert math.isfinite(float(result["fair_spread"]))
        # All-zero closure signal: non-trivial hazard ⇒ non-zero diagnostics.
        assert float(result["fair_spread"]) > 0.0
        assert float(result["protection_leg_npv"]) != 0.0
        assert float(result["premium_leg_npv"]) != 0.0
        assert "default_leg_npv" in result["extras"]

    # Input-sensitivity: a higher hazard ⇒ a higher fair (par) spread.
    assert float(high["result"]["fair_spread"]) > float(low["result"]["fair_spread"])


def test_price_cds_par_spread_bootstrap_against_live_engine(
    live_engine_app: TestClient,
) -> None:
    """Inline par-spread quotes bootstrap a non-flat survival curve (closer).

    **First exercise of the engine par-spread bootstrap path anywhere.** The
    credit-curve body carries inline ``CdsQuote`` par-spreads with no
    ``flat_hazard_rate`` (⇒ ``flat_hazard_rate == 0`` ⇒ bootstrap).
    A green run here confirms non-trivial survival probabilities on the
    bootstrap path → the all-zero-diagnostics defect is **CLOSED**.
    """

    body = _price(
        live_engine_app,
        {
            "name": "ACME-SR bootstrapped",
            "recovery_rate": 0.4,
            "source": "manual",
            "body": {
                "points": [
                    {"tenor": "1Y", "quoted_par_spread": 0.010},
                    {"tenor": "3Y", "quoted_par_spread": 0.015},
                    {"tenor": "5Y", "quoted_par_spread": 0.020},
                    {"tenor": "10Y", "quoted_par_spread": 0.025},
                ]
            },
        },
    )

    result = body["result"]
    assert math.isfinite(float(result["npv"]))
    assert math.isfinite(float(result["fair_spread"]))
    # Bootstrapped survival curve ⇒ non-zero diagnostics (the closure check).
    assert float(result["fair_spread"]) > 0.0
    assert float(result["protection_leg_npv"]) != 0.0
    assert float(result["premium_leg_npv"]) != 0.0
    # The 5Y CDS fair spread should land near the seeded 5Y par spread region
    # (loose band — numeric parity is the deferred Phase-7 layer, not this one).
    assert 0.005 < float(result["fair_spread"]) < 0.05
    assert body["assembled_request"]["credit_curve"]["name"] == "ACME-SR bootstrapped"
