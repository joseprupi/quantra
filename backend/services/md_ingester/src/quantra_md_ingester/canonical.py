"""Canonical-ID grammar shared by every vendor connector.

A canonical ID is the deployment-wide name for one market-data series. The
parsed structural descriptors (asset class, family, instrument, currency,
tenor, field) populate the matching columns on ``md.canonical_ids`` so the
catalog endpoint can return them without a join.

Kept service-local rather than under ``packages/common`` because today
the ingester is the only consumer — the MD read service queries the
``md.canonical_ids`` columns directly and never parses the ID string.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

CANONICAL_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<currency>[A-Z]{3})\."
    r"(?P<asset>[A-Z_]+)\."
    r"(?P<family>[A-Z0-9_]+)\."
    r"(?P<instrument>[A-Z0-9_]+)\."
    r"(?P<tenor>[0-9]+[DWMY])\."
    r"(?P<field>[A-Z_]+)$"
)

# ---------------------------------------------------------------------------
# Portal / demo "short" canonical IDs.
# The portal and the six product quote-resolution paths emit a
# compact id vocabulary that predates the strict six-field grammar above:
# ``USD.IRS.5Y``, ``USD.SOFR.2Y``, ``EUR.HICP.5Y``,
# ``USD.SWPTN.ATM.5Y10Y.VOL``, ``AAPL.DIV.1Y``. These never flowed through
# the ingest write path before — they were only ever seeded row-by-row by
# the per-product live smokes — so the synthetic standing-data
# connector needs the writer to accept and catalog them.
# Downstream resolution is purely by ``canonical_id`` string (the MD read
# service looks up the literal id, it does not re-parse it), so the
# structural columns we synthesize below feed *only* the ``md.canonical_ids``
# catalog. Every NOT NULL column (``asset_class``, ``instrument``,
# ``currency``, ``field``) is given a non-empty value here.
# ---------------------------------------------------------------------------

_PORTAL_TENOR: Final[str] = r"[0-9]+[DWMY]"

# ``CCY.SWPTN.ATM.<expiry><tenor>.VOL`` — swaption ATM vol cube point.
PORTAL_SWAPTION_VOL_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<currency>[A-Z]{3})\.SWPTN\.(?P<strike>ATM)\.(?P<grid>[0-9]+Y[0-9]+Y)\.VOL$"
)

# ``TICKER.DIV.<tenor>`` — equity dividend yield (ticker is not a currency).
PORTAL_DIVIDEND_ID_RE: Final[re.Pattern[str]] = re.compile(
    rf"^(?P<ticker>[A-Z]{{1,6}})\.DIV\.(?P<tenor>{_PORTAL_TENOR})$"
)

# ``CCY.FAMILY.<tenor>`` — rate / inflation fixings (IRS, SOFR, HICP, …).
PORTAL_RATE_ID_RE: Final[re.Pattern[str]] = re.compile(
    rf"^(?P<currency>[A-Z]{{3}})\.(?P<family>[A-Z]{{2,6}})\.(?P<tenor>{_PORTAL_TENOR})$"
)

_INFLATION_FAMILIES: Final[frozenset[str]] = frozenset({"HICP", "CPI", "INFL", "RPI"})


def _parse_portal_canonical_id(value: str) -> dict[str, str] | None:
    """Return synthesized catalog columns for a portal short-id, else ``None``.

    Checked most-specific-first (swaption vol, then dividend, then the
    generic ``CCY.FAMILY.TENOR`` rate form) so an id like ``AAPL.DIV.1Y``
    is classified as a dividend rather than mis-parsed as a rate.
    """

    swaption = PORTAL_SWAPTION_VOL_ID_RE.match(value)
    if swaption:
        grid = swaption["grid"]
        return {
            "currency": swaption["currency"],
            "asset": "VOL",
            "family": "SWPTN",
            "instrument": grid,
            "tenor": grid,
            "field": "VOL",
        }

    dividend = PORTAL_DIVIDEND_ID_RE.match(value)
    if dividend:
        return {
            "currency": "USD",
            "asset": "EQUITY",
            "family": "DIV",
            "instrument": dividend["ticker"],
            "tenor": dividend["tenor"],
            "field": "YIELD",
        }

    rate = PORTAL_RATE_ID_RE.match(value)
    if rate:
        family = rate["family"]
        asset = "INFLATION" if family in _INFLATION_FAMILIES else "RATES"
        return {
            "currency": rate["currency"],
            "asset": asset,
            "family": family,
            "instrument": family,
            "tenor": rate["tenor"],
            "field": "RATE",
        }

    return None


def validate_canonical_id(value: str) -> bool:
    """Return True iff ``value`` parses as a canonical ID.

    Accepts both the strict six-field grammar and the portal/demo short
    forms. The short forms never collide with the strict-grammar
    reject cases (they are 3- and 5-part, all-upper, fixed shapes).
    """

    if CANONICAL_ID_RE.match(value):
        return True
    return _parse_portal_canonical_id(value) is not None


def parse_canonical_id(value: str) -> dict[str, str]:
    """Return the named (or synthesized) catalog groups of ``value``.

    Tries the strict six-field grammar first, then the portal/demo short
    forms. Raises ``ValueError`` on a bad input — callers that
    don't want the exception should pre-filter with ``validate_canonical_id``.
    """

    match = CANONICAL_ID_RE.match(value)
    if match:
        return match.groupdict()
    portal = _parse_portal_canonical_id(value)
    if portal is not None:
        return portal
    msg = f"Invalid canonical_id: {value}"
    raise ValueError(msg)


def humanize_canonical_id(parts: Mapping[str, str]) -> str:
    """Return a clean human-readable label from a parsed canonical id.

    Joins the meaningful descriptors (currency, family, instrument, tenor,
    field) into a compact label, dropping empties and consecutive duplicates
    (e.g. ``USD.IRS.5Y`` parses family==instrument==``IRS`` → ``USD IRS 5Y
    RATE``; ``USD.RATES.UST.OFFICIAL.10Y.YIELD`` → ``USD UST OFFICIAL 10Y
    YIELD``). Used as the ``md.canonical_ids.description`` default instead of a
    placeholder like "Auto-created from connector for X", which read poorly in
    the portal's Time Series Lab. Returns ``""`` if nothing meaningful is present
    (the UI then shows the id itself).
    """

    ordered = (
        parts.get("currency"),
        parts.get("family"),
        parts.get("instrument"),
        parts.get("tenor"),
        parts.get("field"),
    )
    tokens: list[str] = []
    for token in ordered:
        if token and (not tokens or tokens[-1] != token):
            tokens.append(token)
    return " ".join(tokens)
