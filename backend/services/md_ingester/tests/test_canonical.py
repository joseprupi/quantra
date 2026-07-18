"""Canonical-ID parser tests.

The canonical-ID grammar is the only contract every connector and the
DB writer agree on (the parsed groups populate ``md.canonical_ids``).
A regression here corrupts the catalog — pin the cases here.
"""

from __future__ import annotations

import pytest

from quantra_md_ingester.canonical import (
    humanize_canonical_id,
    parse_canonical_id,
    validate_canonical_id,
)


@pytest.mark.parametrize(
    "value",
    [
        "USD.RATES.UST.DGS.10Y.YIELD",
        "EUR.RATES.ESTR.ESTRTRMD.1D.RATE",
        "GBP.RATES.UST.DGS.30Y.YIELD",
        "USD.INFLATION.CPI.CPIAUCSL.1M.LEVEL",
        "USD.RATES.SPREAD.T10Y2Y.1D.SPREAD",
    ],
)
def test_validate_canonical_id_accepts_known_shapes(value: str) -> None:
    assert validate_canonical_id(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "USD",
        "USD.rates.UST.DGS.10Y.YIELD",
        "usd.RATES.UST.DGS.10Y.YIELD",
        "USD.RATES.UST.DGS.10.YIELD",
        "USD.RATES.UST.DGS.10X.YIELD",
        "USD.RATES.UST.DGS.10Y",
        "USD.RATES.UST.DGS.10Y.YIELD.EXTRA",
    ],
)
def test_validate_canonical_id_rejects_malformed(value: str) -> None:
    assert not validate_canonical_id(value)


def test_parse_canonical_id_returns_named_groups() -> None:
    parts = parse_canonical_id("USD.RATES.UST.DGS.10Y.YIELD")
    assert parts == {
        "currency": "USD",
        "asset": "RATES",
        "family": "UST",
        "instrument": "DGS",
        "tenor": "10Y",
        "field": "YIELD",
    }


def test_parse_canonical_id_raises_on_garbage() -> None:
    with pytest.raises(ValueError, match="Invalid canonical_id"):
        parse_canonical_id("nope")


# ---------------------------------------------------------------------------
# Portal / demo short-id grammar. The synthetic standing-data
# connector writes these through the ingest path, so they must validate
# (else ``_write_quote_batch`` silently skips them) and parse into
# non-empty catalog columns.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "USD.IRS.1Y",
        "USD.IRS.3M",
        "USD.IRS.12Y",
        "USD.SOFR.2Y",
        "EUR.IRS.10Y",
        "EUR.HICP.5Y",
        "USD.SWPTN.ATM.5Y10Y.VOL",
        "AAPL.DIV.1Y",
    ],
)
def test_validate_accepts_portal_short_ids(value: str) -> None:
    assert validate_canonical_id(value)


def test_portal_ids_do_not_loosen_the_strict_reject_cases() -> None:
    # The strict-grammar reject cases must still reject under the extended
    # validator — the portal forms are 3-/5-part fixed shapes that never
    # overlap them.
    for value in (
        "",
        "USD",
        "usd.RATES.UST.DGS.10Y.YIELD",
        "USD.RATES.UST.DGS.10.YIELD",
        "USD.RATES.UST.DGS.10Y.YIELD.EXTRA",
    ):
        assert not validate_canonical_id(value)


def test_parse_portal_rate_id_synthesizes_columns() -> None:
    parts = parse_canonical_id("USD.IRS.5Y")
    assert parts["currency"] == "USD"
    assert parts["asset"] == "RATES"
    assert parts["family"] == "IRS"
    assert parts["tenor"] == "5Y"
    # Every NOT NULL catalog column is non-empty.
    for key in ("asset", "instrument", "currency", "field"):
        assert parts[key]


def test_parse_portal_inflation_id_is_classified_inflation() -> None:
    parts = parse_canonical_id("EUR.HICP.5Y")
    assert parts["asset"] == "INFLATION"
    assert parts["family"] == "HICP"


def test_parse_portal_swaption_vol_id() -> None:
    parts = parse_canonical_id("USD.SWPTN.ATM.5Y10Y.VOL")
    assert parts["currency"] == "USD"
    assert parts["family"] == "SWPTN"
    assert parts["field"] == "VOL"
    assert parts["instrument"]


def test_parse_portal_dividend_id() -> None:
    parts = parse_canonical_id("AAPL.DIV.1Y")
    assert parts["family"] == "DIV"
    assert parts["instrument"] == "AAPL"
    assert parts["currency"]  # synthesized, non-empty
    assert parts["field"] == "YIELD"


@pytest.mark.parametrize(
    ("canonical_id", "expected"),
    [
        # Six-field grammar → currency family instrument tenor field, with
        # consecutive duplicates collapsed.
        ("USD.RATES.UST.OFFICIAL.10Y.YIELD", "USD UST OFFICIAL 10Y YIELD"),
        ("GBP.RATES.BOE.OIS.10Y.PAR", "GBP BOE OIS 10Y PAR"),
        # Portal short form: family == instrument, deduped.
        ("USD.IRS.5Y", "USD IRS 5Y RATE"),
    ],
)
def test_humanize_canonical_id_is_clean_label(canonical_id: str, expected: str) -> None:
    assert humanize_canonical_id(parse_canonical_id(canonical_id)) == expected


def test_humanize_canonical_id_has_no_placeholder_prose() -> None:
    label = humanize_canonical_id(parse_canonical_id("USD.RATES.UST.OFFICIAL.10Y.YIELD"))
    assert "Auto-created" not in label
    assert "connector" not in label


def test_humanize_canonical_id_empty_parts_returns_empty() -> None:
    assert humanize_canonical_id({}) == ""
