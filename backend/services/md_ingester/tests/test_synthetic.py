"""Synthetic standing-data connector tests.

These pin the three properties the live demo depends on:

* **Plausibility / bootstrappability** — rate curves are strictly
  monotone, strictly positive, and span past the longest product
  maturity, so the engine bootstraps rather than ABORTs. Checked
  across several ``as_of`` days so the daily-drift shift can never break
  monotonicity or positivity.
* **Determinism** — a fixed ``as_of`` is reproducible; a different
  ``as_of`` produces a different snapshot (so the etag advances).
* **Provenance** — every row is tagged ``source="synthetic"`` with a
  ``meta`` disclaimer, so demo data can never masquerade as a vendor.

All hermetic — no DB, no network.
"""

from __future__ import annotations

from datetime import date
from itertools import pairwise

import pytest

from quantra_md_ingester.canonical import validate_canonical_id
from quantra_md_ingester.connectors.synthetic import (
    _SOURCE,
    SyntheticAdapter,
    SyntheticSeriesSpec,
    _tenor_to_years,
)
from quantra_md_ingester.specs import (
    default_synthetic_config_paths,
    load_synthetic_series,
)

# A spread of dates (incl. leap-day-adjacent and year boundaries) so the
# per-day drift is exercised, not just a single lucky seed.
_SAMPLE_DATES = [
    date(2026, 1, 1),
    date(2026, 6, 2),
    date(2026, 6, 3),
    date(2026, 12, 31),
    date(2027, 2, 28),
]

# The portal vocabulary the six products resolve (mirrors the live
# smoke's per-product quote sets). Each must be present in the dataset.
_REQUIRED_VOCABULARY = {
    # swap_ir / cds / bonds / swaption rates curve
    "USD.IRS.3M",
    "USD.IRS.1Y",
    "USD.IRS.2Y",
    "USD.IRS.5Y",
    "USD.IRS.10Y",
    "USD.IRS.12Y",
    # bonds_floating SOFR projection
    "USD.SOFR.2Y",
    "USD.SOFR.5Y",
    # swaption vol
    "USD.SWPTN.ATM.5Y10Y.VOL",
    # swaps_inflation
    "EUR.IRS.1Y",
    "EUR.IRS.2Y",
    "EUR.IRS.5Y",
    "EUR.IRS.10Y",
    "EUR.HICP.5Y",
    # equity dividend
    "AAPL.DIV.1Y",
}

# Rate curves that must stay strictly monotone increasing in tenor.
_RATE_CURVE_TENORS = {
    "USD.IRS": ["3M", "1Y", "2Y", "5Y", "10Y", "12Y", "15Y", "20Y", "30Y"],
    "USD.SOFR": ["2Y", "5Y", "10Y"],
    "EUR.IRS": ["1Y", "2Y", "5Y", "10Y", "12Y", "20Y", "30Y"],
}


def _load() -> list[SyntheticSeriesSpec]:
    return load_synthetic_series(default_synthetic_config_paths()[0])


def _values(as_of: date) -> dict[str, float]:
    recs = SyntheticAdapter().fetch_series(_load(), as_of)
    return {r.canonical_id: r.value for r in recs}


# ---------------------------------------------------------------------------
# Vocabulary coverage
# ---------------------------------------------------------------------------


def test_dataset_covers_the_full_product_vocabulary() -> None:
    produced = set(_values(date(2026, 6, 2)))
    missing = _REQUIRED_VOCABULARY - produced
    assert not missing, f"synthetic dataset missing required ids: {sorted(missing)}"


def test_every_id_passes_canonical_validation() -> None:
    # If any id failed validation the pipeline's ``_write_quote_batch``
    # would silently *skip* it (skipped_invalid_canonical_id) and the
    # product would never resolve. This is the gate the portal short-id
    # grammar extension exists to satisfy.
    for cid in _values(date(2026, 6, 2)):
        assert validate_canonical_id(cid), cid


# ---------------------------------------------------------------------------
# Plausibility / bootstrappability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("as_of", _SAMPLE_DATES)
@pytest.mark.parametrize("curve", sorted(_RATE_CURVE_TENORS))
def test_rate_curves_are_strictly_monotone_and_positive(curve: str, as_of: date) -> None:
    values = _values(as_of)
    series = [values[f"{curve}.{t}"] for t in _RATE_CURVE_TENORS[curve]]
    assert all(v > 0.0 for v in series), f"{curve} has a non-positive rate: {series}"
    assert all(a < b for a, b in pairwise(series)), (
        f"{curve} term structure not strictly increasing on {as_of}: {series}"
    )


@pytest.mark.parametrize("as_of", _SAMPLE_DATES)
def test_rates_stay_in_a_plausible_band(as_of: date) -> None:
    values = _values(as_of)
    for curve, tenors in _RATE_CURVE_TENORS.items():
        for t in tenors:
            v = values[f"{curve}.{t}"]
            assert 0.005 < v < 0.08, f"{curve}.{t}={v} outside the plausible 0.5%-8% band"


def test_dataset_spans_past_the_longest_product_maturity() -> None:
    # CDS 10Y bootstraps off helpers extending to ~10.18y; the curve
    # must reach well past that. 30Y points give a comfortable span.
    values = _values(date(2026, 6, 2))
    assert "USD.IRS.30Y" in values
    assert "EUR.IRS.30Y" in values


@pytest.mark.parametrize("as_of", _SAMPLE_DATES)
def test_vol_inflation_dividend_levels_are_sensible(as_of: date) -> None:
    values = _values(as_of)
    vol = values["USD.SWPTN.ATM.5Y10Y.VOL"]
    assert 0.05 < vol < 0.60, f"swaption Black vol {vol} implausible"
    hicp = values["EUR.HICP.5Y"]
    assert 0.0 < hicp < 0.06, f"HICP inflation {hicp} implausible"
    div = values["AAPL.DIV.1Y"]
    assert 0.0 < div < 0.05, f"dividend yield {div} implausible"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_as_of_is_bit_identical() -> None:
    a = _values(date(2026, 6, 2))
    b = _values(date(2026, 6, 2))
    assert a == b


def test_different_as_of_changes_the_snapshot() -> None:
    a = _values(date(2026, 6, 2))
    b = _values(date(2026, 6, 3))
    # Some id must change so a daily rebuild advances the etag.
    assert a != b
    assert a["USD.IRS.5Y"] != b["USD.IRS.5Y"]


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_every_record_is_source_tagged_synthetic() -> None:
    recs = SyntheticAdapter().fetch_series(_load(), date(2026, 6, 2))
    assert recs
    for r in recs:
        assert r.source == _SOURCE == "synthetic"
        assert r.meta["synthetic"] is True
        assert "DEMO SYNTHETIC DATA" in r.meta["disclaimer"]
        assert r.meta["dataset"] == "DEMO_STANDING"
        assert r.quality_flags == {"synthetic": True}
        assert r.vendor_id == f"SYNTH:{r.canonical_id}"


# ---------------------------------------------------------------------------
# Guard rails
# ---------------------------------------------------------------------------


def test_empty_specs_raises() -> None:
    with pytest.raises(RuntimeError, match="No synthetic series"):
        SyntheticAdapter().fetch_series([], date(2026, 6, 2))


def test_unknown_quote_kind_raises_loudly() -> None:
    # A degenerate / zero quote would ABORT the engine bootstrap, so a
    # config typo must fail rather than emit a bad value.
    bad = SyntheticSeriesSpec(
        canonical_id="USD.IRS.5Y",
        tenor="5Y",
        quote_kind="bogus",
        curve="USD.IRS",
        description="bad",
    )
    with pytest.raises(RuntimeError, match="Unknown synthetic quote_kind"):
        SyntheticAdapter().fetch_series([bad], date(2026, 6, 2))


@pytest.mark.parametrize(
    ("tenor", "expected"),
    [
        ("3M", 0.25),
        ("1Y", 1.0),
        ("12Y", 12.0),
        ("13W", 13 * 7 / 365.0),
        ("30D", 30 / 365.0),
        ("5Y10Y", 5.0),  # swaption grid — leading expiry only
    ],
)
def test_tenor_to_years(tenor: str, expected: float) -> None:
    assert _tenor_to_years(tenor) == pytest.approx(expected)
