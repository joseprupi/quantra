"""Seed the REAL reference entities through the orchestrator API.

The platform is **real-data-only**: this seeder plants only genuine,
daily-updated public market data — no synthetic demo curves/products. A fresh
install therefore needs internet on first boot for the real feeds (BoE /
Treasury) to ingest before these curves resolve; that tradeoff is accepted (no
synthetic fallback).

What it seeds (all programmatic — no fixture file):

* two REAL overnight index conventions — **SONIA** (GBP) and **SOFR** (USD) —
  so the real OIS/rates curves have a forwarding index to price against;
* two REAL MD-backed curves, every pillar quote-referenced so it resolves
  server-side against the daily public feeds:
    - **"GBP SONIA OIS (BoE, daily public)"** — Bank of England SONIA OIS par
      strip (``GBP.RATES.BOE.OIS.*.PAR``, ``boe_ois`` connector);
    - **"USD Treasury (public, daily)"** — US Treasury par-yield strip
      (``USD.RATES.UST.OFFICIAL.*.YIELD``, ``treasury`` connector).

Both curves carry a ``body.local_id`` marker (``md-gbp-boe-ois`` /
``md-usd-treasury``) that matches a ``roll-curve-dates`` target, so their
``reference_date`` advances to the latest ingested business day automatically.

Deliberately NOT seeded:

* any synthetic data (removed — the ``synthetic-ingest`` boot step is gone too).
* ``quotes`` / ``quoteBook`` — dead surfaces (market data
  comes from the MD server; the Quote Book is a read-only catalog view).
* products (``swaps`` / ``swaptions`` / ``cds`` / bonds / …) — the persisted
  product contract is the save-graph. Products enter the DB by
  pricing + saving in the portal.

Auth / ownership
----------------
The rows land under whichever user the orchestrator resolves:

* ``QUANTRA_SEED_API_KEY`` set → sent as ``X-API-Key`` (rows belong to the
  key's user — use this to seed a real Firebase account).
* otherwise → relies on ``DEV_AUTH_BYPASS=true`` on the orchestrator
  (rows belong to ``dev-user``).

Idempotent: entity names already present on the server are skipped, so the
``init-seed`` one-shot container in the ``self-hosted`` compose profile can
run it on every bring-up.

Usage::

    uv run python scripts/seed_demo_entities.py           # dev bypass
    QUANTRA_SEED_API_KEY=... uv run python scripts/seed_demo_entities.py
    QUANTRA_ORCH_URL=http://localhost:8000 uv run python scripts/seed_demo_entities.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from http import HTTPStatus
from typing import Any

import httpx

# Index keys that map onto backend scalar columns; every other key rides along
# inside the JSONB ``body`` so nothing in the original object is lost.
_INDEX_SCALARS = ("name", "currency", "calendar", "day_counter")

# --- Real overnight index conventions --------------------------------------
# SONIA (GBP) and SOFR (USD) are genuine market index CONVENTIONS (not
# synthetic market data): the real OIS/rates curves need a forwarding index to
# price a swap against. Shapes mirror the vetted portal fixture entries.
_REAL_INDICES: tuple[dict[str, Any], ...] = (
    {
        "id": "SONIA",
        "name": "SONIA",
        "index_type": "Overnight",
        "currency": "GBP",
        "tenor_number": 0,
        "tenor_time_unit": "Days",
        "fixing_days": 0,
        "calendar": "UnitedKingdom",
        "day_counter": "Actual365Fixed",
        "description": "Sterling Overnight Index Average",
    },
    {
        "id": "SOFR",
        "name": "SOFR",
        "index_type": "Overnight",
        "currency": "USD",
        "tenor_number": 0,
        "tenor_time_unit": "Days",
        "fixing_days": 0,
        "calendar": "UnitedStates",
        "day_counter": "Actual360",
        "description": "Secured Overnight Financing Rate",
    },
)


# The GBP SONIA OIS quote-referencing curve. Pillars reference the real
# Bank of England OIS series (``GBP.RATES.BOE.OIS.*.PAR``) ingested by the
# ``boe_ois`` connector: par OIS swap rates derived from the BoE
# continuously-compounded spot (zero) curve (the workbook has no par sheet),
# so feeding them into annual SwapHelpers reconstructs the BoE discount
# factors exactly. Anchored at the freshest BoE OIS business day the feed
# carried at seed time (the "latest" workbook lands ~yesterday's fixing); the
# operator advances both this ``reference_date`` and the pricing as-of
# together as the feed refreshes (the resolver keys on latest quote with
# as_of <= pricing as_of, so an exact-match date is the coherent choice).
_GBP_OIS_REFERENCE_DATE = "2026-07-14"
_GBP_OIS_QUOTE_TENORS: tuple[tuple[int, str], ...] = (
    (1, "GBP.RATES.BOE.OIS.1Y.PAR"),
    (2, "GBP.RATES.BOE.OIS.2Y.PAR"),
    (3, "GBP.RATES.BOE.OIS.3Y.PAR"),
    (5, "GBP.RATES.BOE.OIS.5Y.PAR"),
    (7, "GBP.RATES.BOE.OIS.7Y.PAR"),
    (10, "GBP.RATES.BOE.OIS.10Y.PAR"),
    (15, "GBP.RATES.BOE.OIS.15Y.PAR"),
    (20, "GBP.RATES.BOE.OIS.20Y.PAR"),
    (25, "GBP.RATES.BOE.OIS.25Y.PAR"),
)


# The USD Treasury quote-referencing curve. Pillars reference the real US
# Treasury nominal par-yield strip (``USD.RATES.UST.OFFICIAL.*.YIELD``) ingested
# by the ``treasury`` connector (par yields from the daily public
# home.treasury.gov feed). Fed into money-market deposits (<=6M) + par
# SwapHelpers (>=1Y), the annual/semiannual bootstrap reconstructs the Treasury
# par discount curve. Anchored at the freshest business day the treasury feed
# carried at seed time; ``roll-curve-dates`` (marker ``md-usd-treasury``)
# advances both this ``reference_date`` and the pricing as-of together as the
# feed refreshes.
_USD_UST_REFERENCE_DATE = "2026-07-14"
_USD_UST_DEPOSIT_TENORS: tuple[tuple[int, str, str], ...] = (
    (1, "Months", "USD.RATES.UST.OFFICIAL.1M.YIELD"),
    (3, "Months", "USD.RATES.UST.OFFICIAL.3M.YIELD"),
    (6, "Months", "USD.RATES.UST.OFFICIAL.6M.YIELD"),
)
_USD_UST_SWAP_TENORS: tuple[tuple[int, str], ...] = (
    (1, "USD.RATES.UST.OFFICIAL.1Y.YIELD"),
    (2, "USD.RATES.UST.OFFICIAL.2Y.YIELD"),
    (5, "USD.RATES.UST.OFFICIAL.5Y.YIELD"),
    (7, "USD.RATES.UST.OFFICIAL.7Y.YIELD"),
    (10, "USD.RATES.UST.OFFICIAL.10Y.YIELD"),
    (20, "USD.RATES.UST.OFFICIAL.20Y.YIELD"),
    (30, "USD.RATES.UST.OFFICIAL.30Y.YIELD"),
)


def _usd_treasury_demo_curve() -> dict[str, Any]:
    """A real US Treasury (government) discount curve sourced from the UST feed.

    Every pillar carries a ``quote_id`` into the
    ``USD.RATES.UST.OFFICIAL.*.YIELD`` strip the ``treasury`` connector produces
    from the US Treasury's daily public par-yield curve (home.treasury.gov):
    money-market deposits at <=6M and par SwapHelpers at >=1Y (Treasuries pay
    semiannual coupons, act/act(Bond)), so the bootstrap reconstructs the
    Treasury par discount factors. Resolved server-side at pricing.

    HONEST LABEL: this is a **government / Treasury** curve — correct for
    discounting Treasury bonds; a *proxy* for USD swap discounting (it is NOT a
    SOFR-OIS curve — there is no free daily public SOFR swap feed). The
    ``md-usd-treasury`` ``local_id`` marker matches ``roll-curve-dates`` so the
    ``reference_date`` advances automatically with the daily feed.
    """

    deposits = [
        {
            "point_type": "DepositHelper",
            "point": {
                "tenor_number": n,
                "tenor_time_unit": unit,
                "fixing_days": 2,
                "calendar": "UnitedStatesGovernmentBond",
                "business_day_convention": "ModifiedFollowing",
                "day_counter": "Actual360",
                "quote_id": quote_id,
            },
        }
        for n, unit, quote_id in _USD_UST_DEPOSIT_TENORS
    ]
    swap_points = [
        {
            "point_type": "SwapHelper",
            "point": {
                "tenor_number": n,
                "tenor_time_unit": "Years",
                "calendar": "UnitedStatesGovernmentBond",
                "sw_fixed_leg_frequency": "Semiannual",
                "sw_fixed_leg_convention": "ModifiedFollowing",
                "sw_fixed_leg_day_counter": "ActualActualBond",
                "quote_id": quote_id,
            },
        }
        for n, quote_id in _USD_UST_SWAP_TENORS
    ]
    return {
        "name": "USD Treasury (public, daily)",
        "currency": "USD",
        "day_counter": "Actual365Fixed",
        "reference_date": _USD_UST_REFERENCE_DATE,
        "points": [*deposits, *swap_points],
        "body": {
            "role": "discount",
            "interpolator": "LogLinear",
            "bootstrap_trait": "Discount",
            "description": (
                "Real US Treasury government curve - every pillar carries a "
                "quote_id for a Treasury par yield (USD.RATES.UST.OFFICIAL.*."
                "YIELD), ingested daily from the US Treasury public feed and "
                "resolved server-side via the MD service at pricing. "
                "Government/Treasury curve (correct for Treasury bonds; a proxy "
                "for USD swap discounting, NOT a SOFR-OIS curve)."
            ),
            "local_id": "md-usd-treasury",
        },
    }


def _gbp_ois_demo_curve() -> dict[str, Any]:
    """A real GBP SONIA OIS discount+forward curve sourced from the BoE feed.

    Every pillar carries a ``quote_id`` into the ``GBP.RATES.BOE.OIS.*.PAR``
    strip the ``boe_ois`` connector produces from the Bank of England's daily
    public OIS spot-curve workbook — par OIS swap rates derived from the BoE
    zero curve (DF(t)=exp(-z*t)), so the annual SwapHelper bootstrap
    reconstructs the genuine SONIA OIS discount factors (not a government-curve
    proxy, and not the ~8bp-wrong zero-as-par approximation). Resolved
    server-side at pricing.
    """

    swap_points = [
        {
            "point_type": "SwapHelper",
            "point": {
                "tenor_number": n,
                "tenor_time_unit": "Years",
                "calendar": "UnitedKingdom",
                "sw_fixed_leg_frequency": "Annual",
                "sw_fixed_leg_convention": "ModifiedFollowing",
                "sw_fixed_leg_day_counter": "Actual365Fixed",
                "quote_id": quote_id,
            },
        }
        for n, quote_id in _GBP_OIS_QUOTE_TENORS
    ]
    deposit = {
        "point_type": "DepositHelper",
        "point": {
            "tenor_number": 6,
            "tenor_time_unit": "Months",
            "fixing_days": 0,
            "calendar": "UnitedKingdom",
            "business_day_convention": "ModifiedFollowing",
            "day_counter": "Actual365Fixed",
            "quote_id": "GBP.RATES.BOE.OIS.6M.PAR",
        },
    }
    return {
        "name": "GBP SONIA OIS (BoE, daily public)",
        "currency": "GBP",
        "day_counter": "Actual365Fixed",
        "reference_date": _GBP_OIS_REFERENCE_DATE,
        "points": [deposit, *swap_points],
        "body": {
            "role": "discount",
            "interpolator": "LogLinear",
            "bootstrap_trait": "Discount",
            "description": (
                "Real GBP SONIA OIS curve - every pillar carries a quote_id "
                "for a par OIS swap rate derived from the Bank of England OIS "
                "spot (zero) curve (GBP.RATES.BOE.OIS.*.PAR), ingested daily "
                "from the BoE public feed and resolved server-side via the MD "
                "service at pricing. Genuine OIS curve, not a "
                "government-curve proxy."
            ),
            "local_id": "md-gbp-boe-ois",
        },
    }


def _normalise_index_kind(raw: str | None) -> str:
    """Map the portal's kind spellings onto the DB CHECK list.

    The portal writes ``'Ibor'`` / ``'Overnight'`` / ``'Inflation'``;
    ``app.indices.kind`` CHECKs ``'IBOR' | 'Overnight' | 'Inflation'``
    (found live: ``indices_kind_valid`` violation → opaque 500).
    """

    mapping = {"ibor": "IBOR", "overnight": "Overnight", "inflation": "Inflation"}
    return mapping.get((raw or "").lower(), "IBOR")


def _split(
    obj: dict[str, Any],
    scalars: tuple[str, ...],
    *,
    body_key: str = "body",
    drop: tuple[str, ...] = ("createdAt", "updatedAt"),
) -> dict[str, Any]:
    """Split a portal fixture object into scalar columns + a JSONB body.

    The portal's local ``id`` is preserved as ``body.local_id`` so the
    one-time import can detect already-seeded entities.
    """

    payload: dict[str, Any] = {k: obj[k] for k in scalars if obj.get(k) is not None}
    rest = {
        k: v
        for k, v in obj.items()
        if k not in scalars and k not in drop and k not in {"id", "points"}
    }
    if "id" in obj:
        rest["local_id"] = obj["id"]
    payload[body_key] = rest
    return payload


class Seeder:
    """Posts the real reference entities, skipping names that already exist."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.created: list[str] = []
        self.skipped: list[str] = []

    async def _existing_names(self, path: str) -> set[str]:
        resp = await self.client.get(path, params={"limit": 200})
        resp.raise_for_status()
        return {item.get("name", "") for item in resp.json()["items"]}

    async def _post(self, path: str, payload: dict[str, Any], label: str) -> dict[str, Any] | None:
        resp = await self.client.post(path, json=payload)
        if resp.status_code == HTTPStatus.CONFLICT:
            self.skipped.append(label)
            return None
        if resp.status_code != HTTPStatus.CREATED:
            raise RuntimeError(f"{label}: POST {path} -> {resp.status_code}: {resp.text[:300]}")
        self.created.append(label)
        return dict(resp.json())

    async def seed_indices(self, indices: list[dict[str, Any]]) -> None:
        existing = await self._existing_names("/v1/indices")
        for idx in indices:
            # The backend uniques on (owner, name); the index ``id`` is the
            # unique, stable handle every soft ref (curve helpers, saved
            # products) points at — use it as the name.
            name = idx.get("id") or idx.get("name") or "unnamed-index"
            if name in existing:
                self.skipped.append(f"index:{name}")
                continue
            payload = _split({**idx, "name": name}, _INDEX_SCALARS)
            payload["kind"] = _normalise_index_kind(idx.get("index_type") or idx.get("type"))
            # Preserve the human family name (e.g. "Euribor") — the unique
            # backend `name` carries the business id ("EURIBOR_6M").
            if idx.get("name") and idx.get("name") != name:
                payload["body"]["source_name"] = idx["name"]
            await self._post("/v1/indices", payload, f"index:{name}")

    async def seed_curves(self) -> None:
        existing = await self._existing_names("/v1/curves")
        # Real-data-only: the two genuine MD-backed curves that resolve against
        # the daily public feeds (BoE SONIA OIS + US Treasury). No synthetic.
        for curve in (_gbp_ois_demo_curve(), _usd_treasury_demo_curve()):
            name = curve["name"]
            if name in existing:
                self.skipped.append(f"curve:{name}")
                continue
            # The real curves are already backend-shaped (they carry ``body``).
            await self._post("/v1/curves", dict(curve), f"curve:{name}")


async def main() -> int:
    base_url = os.environ.get("QUANTRA_ORCH_URL", "http://localhost:8000")
    headers: dict[str, str] = {}
    api_key = os.environ.get("QUANTRA_SEED_API_KEY")
    if api_key:
        headers["X-API-Key"] = api_key

    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30.0) as client:
        # Make sure the caller's app.users row exists first — every entity
        # table FKs owner_uid -> app.users.uid (finding).
        prov = await client.post("/auth/provision")
        prov.raise_for_status()
        print(f"Provisioned user: {prov.json()['uid']}")

        seeder = Seeder(client)
        # Real-data-only: the overnight index conventions the real curves price
        # against, then the two real MD-backed curves. No synthetic entities.
        await seeder.seed_indices(list(_REAL_INDICES))
        await seeder.seed_curves()

    owner = "API-key user" if api_key else "dev-user (DEV_AUTH_BYPASS)"
    print(f"Seeded via {base_url} as {owner} — mode: real-data-only (GBP OIS + USD Treasury)")
    print(f"  created ({len(seeder.created)}): {', '.join(seeder.created) or '-'}")
    print(f"  skipped ({len(seeder.skipped)}): {', '.join(seeder.skipped) or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
