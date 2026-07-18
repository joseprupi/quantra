"""Hermetic regression tests for the confirmed data-corruption fixes.

Covers, per the 2026-07 production audit:

1. **ECB placeholder configs removed** — the four ``EUR.IRS.*`` ids all
   mapped the SAME monthly EURIBOR-3M average (which resolves!), writing
   four identical un-scaled junk fixings per run; the ``EUR.HICP.5Y``
   "5Y breakeven proxy" duplicated a vendor series and flip-flopped the
   ``md.vendor_mappings`` upsert. Same for the dead FRED placeholders
   (discontinued DSWP* + non-existent SOFRSWAP* ids).
2. **ECB percent→decimal scaling** — the connector stored ``OBS_VALUE``
   verbatim (2.339 where 0.02339 was meant) for rate series.
3. **Treasury real-yield 10Y == 20Y** — Treasury's own HTML puts BOTH
   ``tc20year`` and ``views-field-field-tc-10year`` on the 20Y cell;
   last-write-wins overwrote the genuine 10Y value.
4. **FRED level series divided by 100** — CPI ~314 stored as 3.14, VIX
   16.7 stored as 0.167, etc.
5. **Writer honesty** — per-quote ``units`` (levels are not
   ``decimal_rate``) and never clobbering a non-empty (user-edited)
   ``md.canonical_ids.description``.

Everything here is hermetic: vendor HTTP is monkeypatched at each
connector's module-level ``urlopen`` and the writer is exercised
against a recording stub. Behavior against a real Postgres is covered
by the ``md_ingester_db``-marked tests in ``test_db_integration.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from quantra_md_ingester.connectors.ecb import EcbAdapter, EcbSeriesSpec
from quantra_md_ingester.connectors.fred import FredAdapter, FredSeriesSpec
from quantra_md_ingester.connectors.treasury import TreasuryAdapter, TreasurySeriesSpec
from quantra_md_ingester.models import QuoteRecord
from quantra_md_ingester.specs import (
    _CONFIGS_DIR,
    default_ecb_config_paths,
    default_fred_config_paths,
    load_ecb_series,
    load_fred_series,
)
from quantra_md_ingester.writer import upsert_canonical, upsert_quote


class _FakeResponse:
    """Minimal stand-in for the ``urlopen`` context manager."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


# ---------------------------------------------------------------------------
# 1. The placeholder configs are gone — from disk AND from the default sets.
# ---------------------------------------------------------------------------


def test_deleted_placeholder_configs_are_not_loaded() -> None:
    fred_paths = default_fred_config_paths()
    assert [p.name for p in fred_paths] == ["ust_yields.json"]
    ecb_names = [p.name for p in default_ecb_config_paths()]
    assert "eur_swap_rates.json" not in ecb_names
    assert sorted(ecb_names) == ["fx_reference_rates.json", "inflation_rates.json"]

    # The files must also be gone from the shipped wheel payload, so no
    # stale loader / operator invocation can resurrect them.
    assert not (_CONFIGS_DIR / "ecb" / "eur_swap_rates.json").exists()
    assert not (_CONFIGS_DIR / "fred" / "usd_swap_rates.json").exists()
    assert not (_CONFIGS_DIR / "fred" / "usd_sofr_swaps.json").exists()


def test_ecb_defaults_carry_no_swap_or_breakeven_placeholders() -> None:
    specs = [spec for path in default_ecb_config_paths() for spec in load_ecb_series(path)]
    assert specs
    ids = {spec.canonical_id for spec in specs}
    assert not any(cid.startswith("EUR.IRS.") for cid in ids)
    assert "EUR.HICP.5Y" not in ids

    # One vendor series → exactly one canonical_id. Duplicate mappings
    # made the (vendor, vendor_id) upsert flip-flop every run.
    mapped: dict[tuple[str, str], str] = {}
    for spec in specs:
        key = (spec.flow, spec.series_key)
        assert mapped.setdefault(key, spec.canonical_id) == spec.canonical_id, (
            f"vendor series {key} mapped to multiple canonical ids"
        )


def test_fred_default_config_flags_exactly_the_level_series() -> None:
    specs = load_fred_series(default_fred_config_paths()[0])
    by_unit: dict[str, set[str]] = {"percent": set(), "level": set()}
    for spec in specs:
        by_unit[spec.vendor_unit].add(spec.series_id)
    assert by_unit["level"] == {
        "SOFRINDEX",
        "CPIAUCSL",
        "CPILFESL",
        "CPIAUCNS",
        "PCEPI",
        "PCEPILFE",
        "VIXCLS",
        "MOVE",
    }
    # Rates stay percent-normalised.
    assert "DGS10" in by_unit["percent"]
    # Every ``.LEVEL`` canonical id must be flagged level (and vice versa).
    for spec in specs:
        assert spec.canonical_id.endswith(".LEVEL") == (spec.vendor_unit == "level")


# ---------------------------------------------------------------------------
# 2. ECB percent→decimal scaling (and honest spot-rate passthrough).
# ---------------------------------------------------------------------------


def test_ecb_rate_series_scaled_percent_to_decimal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"TIME_PERIOD,OBS_VALUE\n2026-06,2.339\n"
    monkeypatch.setattr(
        "quantra_md_ingester.connectors.ecb.urlopen",
        lambda url, timeout: _FakeResponse(payload),
    )
    spec = EcbSeriesSpec(
        flow="ICP",
        series_key="M.U2.N.000000.4.ANR",
        canonical_id="EUR.INFLATION.HICP.OVERALL.1M.RATE",
        tenor="1M",
        description="HICP overall annual rate",
        base_currency="EUR",
        quote_currency="EUR",
        normalized_unit="decimal_rate",
    )
    quotes = EcbAdapter().fetch_series([spec], as_of=date(2026, 7, 15))
    assert len(quotes) == 1
    q = quotes[0]
    assert q.value == pytest.approx(0.02339)  # NOT 2.339
    assert q.units == "decimal_rate"
    assert q.meta["raw_value_percent"] == pytest.approx(2.339)
    assert q.meta["vendor_unit"] == "percent"
    assert q.meta["normalized_unit"] == "decimal_rate"


def test_ecb_fx_spot_rate_stored_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b"TIME_PERIOD,OBS_VALUE\n2026-07-15,1.0834\n"
    monkeypatch.setattr(
        "quantra_md_ingester.connectors.ecb.urlopen",
        lambda url, timeout: _FakeResponse(payload),
    )
    spec = EcbSeriesSpec(
        flow="EXR",
        series_key="D.USD.EUR.SP00.A",
        canonical_id="EURUSD.FX.SPOT.1D.RATE",
        tenor="1D",
        description="ECB EUR/USD reference rate",
        base_currency="EUR",
        quote_currency="USD",
        normalized_unit="spot_rate",
    )
    quotes = EcbAdapter().fetch_series([spec], as_of=date(2026, 7, 15))
    assert len(quotes) == 1
    q = quotes[0]
    assert q.value == pytest.approx(1.0834)  # verbatim, no /100
    assert q.units == "spot_rate"
    assert "raw_value_percent" not in q.meta


# ---------------------------------------------------------------------------
# 3. Treasury real-yield double-class cell: 10Y must NOT take the 20Y value.
# ---------------------------------------------------------------------------

# Faithful reduction of the live TextView markup bug: the 20Y cell
# carries BOTH its own ``tc20year`` token AND the 10Y column's
# ``views-field-field-tc-10year`` token.
_TREASURY_DOUBLE_CLASS_HTML = """
<html><body><table>
<tbody>
<tr>
  <td class="views-field views-field-field-tdr-date">
    <time datetime="2026-07-15T12:00:00Z">07/15/2026</time>
  </td>
  <td class="views-field views-field-field-tc-5year">1.44</td>
  <td class="views-field views-field-field-tc-7year">1.60</td>
  <td class="views-field views-field-field-tc-10year">1.71</td>
  <td class="views-field tc20year views-field-field-tc-10year">2.05</td>
  <td class="views-field tc30year">2.20</td>
</tr>
</tbody>
</table></body></html>
"""


def test_treasury_10y_not_overwritten_by_double_class_20y_cell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "quantra_md_ingester.connectors.treasury.urlopen",
        lambda request, timeout: _FakeResponse(_TREASURY_DOUBLE_CLASS_HTML.encode()),
    )
    specs = [
        TreasurySeriesSpec(
            page_type="daily_treasury_real_yield_curve",
            value_class_token="views-field-field-tc-10year",  # noqa: S106 - CSS class, not a secret
            archive_column="10 yr",
            canonical_id="USD.RATES.TIPS.REAL.10Y.YIELD",
            tenor="10Y",
            description="US Treasury real par yield curve 10Y",
        ),
        TreasurySeriesSpec(
            page_type="daily_treasury_real_yield_curve",
            value_class_token="tc20year",  # noqa: S106 - CSS class, not a secret
            archive_column="20 yr",
            canonical_id="USD.RATES.TIPS.REAL.20Y.YIELD",
            tenor="20Y",
            description="US Treasury real par yield curve 20Y",
        ),
    ]
    # Window entirely past the archive cutoff so only TextView is hit.
    quotes = TreasuryAdapter().fetch_series(
        specs, as_of=date(2026, 7, 15), start_date=date(2026, 7, 15)
    )
    by_id = {q.canonical_id: q for q in quotes}
    assert set(by_id) == {
        "USD.RATES.TIPS.REAL.10Y.YIELD",
        "USD.RATES.TIPS.REAL.20Y.YIELD",
    }
    ten = by_id["USD.RATES.TIPS.REAL.10Y.YIELD"]
    twenty = by_id["USD.RATES.TIPS.REAL.20Y.YIELD"]
    assert ten.value == pytest.approx(0.0171)  # the genuine 10Y cell
    assert twenty.value == pytest.approx(0.0205)
    assert ten.value != twenty.value


# ---------------------------------------------------------------------------
# 4. FRED: percent scaled, levels verbatim.
# ---------------------------------------------------------------------------


def _fred_payload(observations: list[dict[str, str]]) -> bytes:
    return json.dumps({"observations": observations}).encode()


def test_fred_percent_series_scaled_and_level_series_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = {
        "DGS10": _fred_payload([{"date": "2026-07-15", "value": "4.32"}]),
        "CPIAUCSL": _fred_payload([{"date": "2026-06-01", "value": "314.175"}]),
        "VIXCLS": _fred_payload([{"date": "2026-07-15", "value": "16.7"}]),
    }

    def _fake_urlopen(url: str, timeout: int) -> _FakeResponse:
        for series_id, payload in payloads.items():
            if f"series_id={series_id}&" in url:
                return _FakeResponse(payload)
        raise AssertionError(f"unexpected FRED url: {url}")

    monkeypatch.setattr("quantra_md_ingester.connectors.fred.urlopen", _fake_urlopen)

    specs = [
        FredSeriesSpec(
            series_id="DGS10",
            canonical_id="USD.RATES.UST.DGS.10Y.YIELD",
            tenor="10Y",
            description="UST constant maturity 10Y",
        ),
        FredSeriesSpec(
            series_id="CPIAUCSL",
            canonical_id="USD.INFLATION.CPI.CPIAUCSL.1M.LEVEL",
            tenor="1M",
            description="US CPI all urban consumers",
            vendor_unit="level",
        ),
        FredSeriesSpec(
            series_id="VIXCLS",
            canonical_id="USD.VOL.EQ.VIXCLS.1D.LEVEL",
            tenor="1D",
            description="CBOE VIX close",
            vendor_unit="level",
        ),
    ]
    quotes = FredAdapter(api_key="test-key").fetch_series(specs, as_of=date(2026, 7, 15))
    by_id = {q.canonical_id: q for q in quotes}

    rate = by_id["USD.RATES.UST.DGS.10Y.YIELD"]
    assert rate.value == pytest.approx(0.0432)
    assert rate.units == "decimal_rate"
    assert rate.meta["raw_value_percent"] == pytest.approx(4.32)

    cpi = by_id["USD.INFLATION.CPI.CPIAUCSL.1M.LEVEL"]
    assert cpi.value == pytest.approx(314.175)  # NOT 3.14175
    assert cpi.units == "level"
    assert cpi.meta["vendor_unit"] == "level"
    assert cpi.meta["normalized_unit"] == "level"
    assert "raw_value_percent" not in cpi.meta

    vix = by_id["USD.VOL.EQ.VIXCLS.1D.LEVEL"]
    assert vix.value == pytest.approx(16.7)  # NOT 0.167
    assert vix.units == "level"


# ---------------------------------------------------------------------------
# 5. Writer honesty: per-quote units + description preservation.
# ---------------------------------------------------------------------------


class _RecordingConn:
    """Captures the (SQL, params) pairs the writer emits."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, statement: Any, params: dict[str, Any]) -> None:
        self.calls.append((str(statement), params))


def _record(units: str = "decimal_rate") -> QuoteRecord:
    return QuoteRecord(
        canonical_id="USD.VOL.EQ.VIXCLS.1D.LEVEL",
        as_of=datetime(2026, 7, 15, tzinfo=UTC),
        value=16.7,
        source="FRED",
        vendor_id="VIXCLS",
        units=units,
    )


async def test_upsert_quote_writes_per_quote_units() -> None:
    conn = _RecordingConn()
    await upsert_quote(cast(AsyncConnection, conn), _record(units="level"))
    _sql, params = conn.calls[0]
    assert params["units"] == "level"

    conn = _RecordingConn()
    await upsert_quote(cast(AsyncConnection, conn), _record())
    _sql, params = conn.calls[0]
    assert params["units"] == "decimal_rate"


async def test_upsert_canonical_units_param_and_description_preservation() -> None:
    conn = _RecordingConn()
    await upsert_canonical(cast(AsyncConnection, conn), "USD.VOL.EQ.VIXCLS.1D.LEVEL", units="level")
    sql, params = conn.calls[0]
    assert params["units"] == "level"
    # The conflict branch must keep an existing non-empty description
    # (user edits via PATCH /v1/market-data/series survive nightly
    # ingests) and only fill NULL/empty ones with the auto label.
    assert "ELSE canonical_ids.description" in sql
    assert "THEN EXCLUDED.description" in sql
    # And it must never blind-assign the incoming description.
    assert "description = EXCLUDED.description" not in sql
