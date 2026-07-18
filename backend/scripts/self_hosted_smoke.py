"""Self-hosted boot smoke — price REAL market data through the orchestrator.

Run this after ``docker compose up -d`` (repo root) has settled. It is the
capstone liveness gate for the self-hosted stack: it proves the whole chain —
orchestrator → MD service → gRPC engine — actually prices off the REAL public
market data the stack ingests at boot, and that the returned NPV is
*input-sensitive* (moves when the swap's fixed rate moves). The
input-sensitivity check guards against a stub / short-circuiting orchestrator
that returns a constant regardless of the request.

What it does:

1. Waits for the orchestrator's ``/health``.
2. Fetches ``GET /v1/curves`` and picks the two seeded real-data curves —
   **"GBP SONIA OIS (BoE, daily public)"** (Bank of England SONIA OIS par
   strip) and **"USD Treasury (public, daily)"** (US Treasury par-yield
   strip). Both reference quote ids resolved server-side by the MD service.
3. For each curve, prices a forward-starting 5Y payer swap TWICE — at a low
   (2%) and a high (6%) fixed rate — at the curve's own ``reference_date``,
   and asserts both price 200 with a materially different (and correctly
   ordered: payer NPV falls as the fixed rate rises) NPV, with a non-empty
   ``resolved_quotes`` echo.

Fresh-boot behaviour: the curves are seeded before the public-feed boot
ingests necessarily finish, so quote resolution can 422 for a short while.
The smoke retries briefly; if no market data ever resolves (e.g. an
air-gapped host where the public feeds are unreachable) it exits with the
DISTINCT exit code 3 and a clear "no market data ingested yet" message —
that state is not an install/upgrade failure, and the daily ingest cron
will keep retrying.

Exit codes: 0 = passed; 3 = app healthy but no market data ingested yet;
anything else = a real failure (diagnostic on stderr).

Deliberately stdlib-only (no httpx) so it also runs with a bare ``python3``
next to the deploy scripts (``deploy/upgrade.sh`` invokes it when present).

Usage::

    # from backend/, against the default stack (host :8080)
    uv run python scripts/self_hosted_smoke.py

    # against an isolated project's orchestrator (e.g. a proof stack on :8085)
    QUANTRA_ORCH_URL=http://localhost:8085 python3 scripts/self_hosted_smoke.py
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

ORCH_URL = os.environ.get("QUANTRA_ORCH_URL", "http://localhost:8080").rstrip("/")

# The two curves scripts/seed_demo_entities.py seeds; every pillar carries a
# quote_id into the real public-feed strips (BoE SONIA OIS / US Treasury).
_CURVE_NAMES = (
    "GBP SONIA OIS (BoE, daily public)",
    "USD Treasury (public, daily)",
)

# Keys of a /v1/curves item that the price endpoint's CurveRef accepts. The
# read model also carries response-only fields (created_at / updated_at /
# body / ...) that the strict request model rejects, so whitelist.
_CURVE_REF_KEYS = (
    "id",
    "name",
    "currency",
    "day_counter",
    "helper_kind",
    "reference_date",
    "points",
)

_HTTP_OK = 200
_HTTP_UNPROCESSABLE = 422

_RATE_LO = 0.02
_RATE_HI = 0.06

# A 5Y payer swap on 1MM notional has roughly 4.5 rate-DV, so a 4% fixed-rate
# swing moves NPV by ~180k; require far less (well above numerical noise).
_MIN_NPV_DELTA = 1_000.0

# How long to keep retrying while the boot ingests land quotes.
_MD_RETRY_SECONDS = 240.0
# The DISTINCT exit code for "app healthy, but no market data ingested yet".
EXIT_NO_MARKET_DATA = 3


class _NoMarketData(Exception):
    """Quote resolution failed because no ingested quotes cover the curve."""


def _request(
    method: str, path: str, payload: dict[str, Any] | None = None, timeout: float = 45.0
) -> tuple[int, dict[str, Any]]:
    """One HTTP round-trip; returns (status, parsed-json-or-{})."""

    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(  # noqa: S310 - fixed http(s) orchestrator URL
        f"{ORCH_URL}{path}",
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:  # non-2xx still carries a JSON body
        raw = exc.read()
        status = exc.code
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {"_raw": raw.decode(errors="replace")[:600]}
    return status, parsed if isinstance(parsed, dict) else {"_list": parsed}


def _wait_healthy(timeout_s: float = 120.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_err = "(no attempt)"
    while time.monotonic() < deadline:
        try:
            status, _ = _request("GET", "/health", timeout=5.0)
            if status == _HTTP_OK:
                return
            last_err = f"HTTP {status}"
        except OSError as exc:
            last_err = repr(exc)
        time.sleep(2.0)
    raise SystemExit(f"orchestrator at {ORCH_URL} never became healthy: {last_err}")


def _fetch_seeded_curves(timeout_s: float = 120.0) -> dict[str, dict[str, Any]]:
    """Fetch the two seeded real-data curves, waiting for init-seed if needed."""

    deadline = time.monotonic() + timeout_s
    found: dict[str, dict[str, Any]] = {}
    while time.monotonic() < deadline:
        status, data = _request("GET", "/v1/curves", timeout=15.0)
        if status == _HTTP_OK:
            items = data.get("items", [])
            found = {c["name"]: c for c in items if c.get("name") in _CURVE_NAMES}
            if len(found) == len(_CURVE_NAMES):
                return found
        time.sleep(5.0)
    missing = [n for n in _CURVE_NAMES if n not in found]
    raise SystemExit(
        f"Seeded curves never appeared on GET /v1/curves: missing {missing}. "
        "Has the init-seed one-shot container completed? "
        "Check: docker compose logs init-seed"
    )


def _curve_ref(curve: dict[str, Any]) -> dict[str, Any]:
    """Project a /v1/curves item onto the strict CurveRef request shape."""

    return {k: curve[k] for k in _CURVE_REF_KEYS if curve.get(k) is not None}


def _looks_like_unresolved_quotes(status: int, data: dict[str, Any]) -> bool:
    """A 422 whose error body points at quote/MD resolution, not the request."""

    if status != _HTTP_UNPROCESSABLE:
        return False
    blob = json.dumps(data).lower()
    return "quote" in blob and ("resolution" in blob or "resolve" in blob or "unresolved" in blob)


def _price(curve: dict[str, Any], as_of: str, fixed_rate: float) -> dict[str, Any]:
    effective = dt.date.fromisoformat(as_of) + dt.timedelta(days=7)
    termination = effective.replace(year=effective.year + 5)
    payload = {
        "swap": {
            "notional": 1_000_000.0,
            "effective_date": effective.isoformat(),
            "termination_date": termination.isoformat(),
            "fixed_rate": fixed_rate,
            "swap_type": "Payer",
        },
        "curves": [_curve_ref(curve)],
        "as_of": as_of,
    }
    # Retry loop: a cold engine can 502/503 while the gRPC channel warms, and
    # right after first boot the public-feed ingests may not have landed yet
    # (quote-resolution 422). Keep trying briefly before classifying.
    deadline = time.monotonic() + _MD_RETRY_SECONDS
    last_status, last_data = -1, {}
    while True:
        last_status, last_data = _request("POST", "/v1/price/swap/ir", payload)
        if last_status == _HTTP_OK:
            return last_data
        retryable = last_status in (502, 503, 504) or _looks_like_unresolved_quotes(
            last_status, last_data
        )
        if not retryable or time.monotonic() >= deadline:
            break
        time.sleep(10.0)
    if _looks_like_unresolved_quotes(last_status, last_data):
        raise _NoMarketData(json.dumps(last_data)[:600])
    raise SystemExit(
        f"price failed for curve {curve['name']!r} @ fixed_rate={fixed_rate}: "
        f"HTTP {last_status} — {json.dumps(last_data)[:600]}"
    )


def _check_curve(label: str, curve: dict[str, Any]) -> None:
    as_of = str(curve.get("reference_date") or "")
    if not as_of:
        raise SystemExit(f"[{label}] seeded curve has no reference_date — cannot pick an as-of")
    lo = _price(curve, as_of, _RATE_LO)
    hi = _price(curve, as_of, _RATE_HI)
    npv_lo = float(lo["result"]["npv"])
    npv_hi = float(hi["result"]["npv"])
    if not (math.isfinite(npv_lo) and math.isfinite(npv_hi)):
        raise SystemExit(f"[{label}] non-finite NPV: lo={npv_lo} hi={npv_hi}")
    if npv_lo == 0.0 and npv_hi == 0.0:
        raise SystemExit(f"[{label}] both NPVs are exactly zero — engine likely not pricing")
    delta = npv_lo - npv_hi
    if delta < _MIN_NPV_DELTA:
        raise SystemExit(
            f"[{label}] payer NPV did not fall as the fixed rate rose "
            f"(@{_RATE_LO:.0%}={npv_lo:,.2f}, @{_RATE_HI:.0%}={npv_hi:,.2f}, "
            f"delta={delta:,.2f} < {_MIN_NPV_DELTA:,.0f}); the orchestrator may be "
            "short-circuiting / returning a constant."
        )
    flips = " (sign-flips)" if npv_lo > 0.0 > npv_hi else ""
    print(
        f"  [{label}] OK @ as_of {as_of} — NPV @{_RATE_LO:.0%}={npv_lo:,.2f}, "
        f"@{_RATE_HI:.0%}={npv_hi:,.2f}, delta={delta:,.2f} (input-sensitive{flips})"
    )
    resolved = hi.get("assembled_request", {}).get("resolved_quotes", [])
    if not resolved:
        raise SystemExit(
            f"[{label}] resolved_quotes echo is EMPTY — the MD-service "
            "resolution path dropped every quote."
        )
    got = {q["canonical_id"]: q["value"] for q in resolved}
    preview = dict(list(got.items())[:4])
    print(f"  [{label}] resolved {len(got)} real quotes, e.g. {preview}")


def main() -> int:
    print(f"Self-hosted smoke — orchestrator {ORCH_URL} (real public market data)")
    _wait_healthy()
    print("Orchestrator healthy. Locating the seeded real-data curves...")
    curves = _fetch_seeded_curves()
    try:
        _check_curve("GBP SONIA OIS", curves[_CURVE_NAMES[0]])
        _check_curve("USD Treasury", curves[_CURVE_NAMES[1]])
    except _NoMarketData as exc:
        print(
            "SMOKE INCONCLUSIVE — the app is healthy and the curves are seeded, "
            "but NO market data has been ingested yet, so quote resolution "
            "failed. This is expected on an air-gapped host (the public BoE / "
            "US Treasury feeds are unreachable) or if the boot ingest has not "
            "finished. It is NOT an install/upgrade failure — do not roll "
            "back. Pricing activates once the first ingest succeeds (the "
            "daily cron keeps retrying). Check: docker compose logs "
            f"boe-ois-ingest treasury-ingest\nLast error: {exc}",
            file=sys.stderr,
        )
        return EXIT_NO_MARKET_DATA
    print("SMOKE PASSED — both real-data curves priced non-zero, input-sensitive NPVs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
