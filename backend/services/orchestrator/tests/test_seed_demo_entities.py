"""Hermetic tests for the real-data-only demo seeder.

``scripts/seed_demo_entities.py`` is a standalone script vendored into the
orchestrator image (run by the ``init-seed`` compose step); it lives in the
repo-root ``scripts/`` dir, outside any workspace member / testpath, so it is
loaded here by path via ``importlib``.

Covered:

* the seeder plants ONLY the two REAL MD-backed curves (GBP SONIA OIS + USD
  Treasury) and NO synthetic curve;
* it seeds the real SONIA/SOFR overnight index conventions;
* the USD Treasury real-curve builder (marker, currency, quote-id pillars);
* that each real curve's ``local_id`` marker matches an ingester roll target,
  so ``roll-curve-dates`` advances its ``reference_date`` automatically.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from quantra_md_ingester.roll import DEFAULT_ROLL_TARGETS

_SEED_PATH = Path(__file__).resolve().parents[3] / "scripts" / "seed_demo_entities.py"

_spec = importlib.util.spec_from_file_location("seed_demo_entities", _SEED_PATH)
assert _spec is not None
assert _spec.loader is not None
seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed)


# --- fakes ----------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int = 200, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {"items": []}
        self.text = ""

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _RecordingClient:
    """Minimal async httpx stand-in: GET returns empty lists, POST records."""

    def __init__(self) -> None:
        self.posted: list[tuple[str, dict[str, Any]]] = []
        self._n = 0

    async def get(self, path: str, params: dict[str, Any] | None = None) -> _FakeResp:
        return _FakeResp(200, {"items": []})

    async def post(self, path: str, json: dict[str, Any] | None = None) -> _FakeResp:
        self.posted.append((path, json or {}))
        self._n += 1
        return _FakeResp(201, {"id": f"uuid-{self._n}"})


_GBP_NAME = "GBP SONIA OIS (BoE, daily public)"
_UST_NAME = "USD Treasury (public, daily)"


# --- USD Treasury real-curve builder --------------------------------------


def test_usd_treasury_curve_builder() -> None:
    curve = seed._usd_treasury_demo_curve()
    assert curve["name"] == _UST_NAME
    assert curve["currency"] == "USD"
    assert curve["body"]["local_id"] == "md-usd-treasury"

    quote_ids = [p["point"]["quote_id"] for p in curve["points"]]
    # Every pillar is quote-id referenced into the real UST par-yield strip.
    assert quote_ids, "expected quote-referenced pillars"
    assert all(q.startswith("USD.RATES.UST.OFFICIAL.") for q in quote_ids)
    assert all(q.endswith(".YIELD") for q in quote_ids)
    # A mix of money-market deposits (<=6M) and par swap helpers (>=1Y).
    kinds = {p["point_type"] for p in curve["points"]}
    assert kinds == {"DepositHelper", "SwapHelper"}


def test_real_curves_markers_match_roll_targets() -> None:
    """Each seeded real curve's marker must be a real roll target so it advances."""

    for builder in (seed._gbp_ois_demo_curve, seed._usd_treasury_demo_curve):
        curve = builder()
        local_id = curve["body"]["local_id"]
        match = next((t for t in DEFAULT_ROLL_TARGETS if t.curve_local_id == local_id), None)
        assert match is not None, f"no roll target for {local_id}"
        for point in curve["points"]:
            assert point["point"]["quote_id"].startswith(match.series_prefix)


# --- seeder: real-data-only ------------------------------------------------


async def test_seed_curves_seeds_only_real_curves() -> None:
    client = _RecordingClient()
    seeder = seed.Seeder(client)
    await seeder.seed_curves()

    names = [payload["name"] for _path, payload in client.posted]
    # EXACTLY the two real MD-backed curves — no synthetic curve.
    assert names == [_GBP_NAME, _UST_NAME]
    assert not any("SOFR Discount" in n or "IRS (MD quotes" in n or "ESTR" in n for n in names)


async def test_seed_indices_seeds_real_overnight_conventions() -> None:
    client = _RecordingClient()
    seeder = seed.Seeder(client)
    await seeder.seed_indices(list(seed._REAL_INDICES))

    posted = [(path, payload) for path, payload in client.posted]
    assert all(path == "/v1/indices" for path, _ in posted)
    names = {payload["name"] for _path, payload in posted}
    assert names == {"SONIA", "SOFR"}
    # Real overnight conventions, mapped to the DB's Overnight kind.
    assert all(payload["kind"] == "Overnight" for _path, payload in posted)


def test_real_indices_are_only_sonia_sofr() -> None:
    ids = {idx["id"] for idx in seed._REAL_INDICES}
    assert ids == {"SONIA", "SOFR"}
