"""Synthetic standing-data connector.

A *non-vendor* connector that fabricates economically-plausible,
deterministic market data for the full portal canonical-id vocabulary so
all six products price end-to-end through real resolved MD — making the
system live-demo-able without a commercial market-data subscription.

This connector deliberately mirrors the
:class:`~quantra_md_ingester.connectors.fred.FredAdapter` shape
(``fetch_series(specs, as_of, start_date)`` returning
:class:`QuoteRecord`) so the pipeline wires it identically. It differs in
exactly one structural way: there is **no network call** — values come
from a closed-form economic model, not a vendor feed.

Provenance (HARD requirement). Every row is tagged ``source="synthetic"``
and carries ``meta["synthetic"] = True`` plus a human-readable
``meta["disclaimer"]``. This data must never masquerade as a real vendor:
the ``source`` column is the operational flag (``/sources`` on the MD
service reports it; ``md.snapshot_quotes.source`` carries it into the
snapshot), and the per-row ``meta`` is the row-level flag.

Economic model (see :data:`_CURVES` and the ``_value_*`` helpers):

* **Rates** (``USD.IRS``, ``USD.SOFR``, ``EUR.IRS``): an upward-sloping,
  strictly-monotone term structure ``base + slope * (1 - exp(-t/decay))``
  so the engine bootstraps cleanly (span past the longest product
  maturity; the configs reach 30Y). A per-curve, per-day parallel shift
  (seeded by ``as_of``) is added equally to every tenor, so the curve
  drifts day-to-day (exercises the snapshot etag) while staying
  strictly monotone and strictly positive — guaranteeing bootstrappability.
* **Inflation** (``EUR.HICP``): a plausible ~2.2% zero-coupon inflation
  rate with a small tenor slope.
* **Swaption vol** (``USD.SWPTN.ATM.*.VOL``): a sensible ~22% Black vol.
* **Dividend** (``AAPL.DIV.*``): a small positive ~1.2% dividend yield.

Determinism. For a fixed ``as_of`` the output is bit-identical run to
run; a different ``as_of`` yields a different (but still plausible)
snapshot. The seed is derived from ``as_of`` only, so the standing
dataset is reproducible for any historical replay.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final

from quantra_common.logging import get_logger
from quantra_md_ingester.models import QuoteRecord

logger = get_logger(__name__)

_SOURCE: Final[str] = "synthetic"
DATASET: Final[str] = "DEMO_STANDING"
_DISCLAIMER: Final[str] = (
    "DEMO SYNTHETIC DATA — economically-plausible standing values generated "
    "by the synthetic demo connector. NOT real market prices; not sourced "
    "from any commercial vendor."
)
_GENERATOR_VERSION: Final[str] = "synthetic_v1"

# Quote kinds the connector knows how to price. Carried on the spec so a
# new vocabulary entry is a config-only change.
QUOTE_KIND_RATE: Final[str] = "rate"
QUOTE_KIND_INFLATION: Final[str] = "inflation"
QUOTE_KIND_VOL: Final[str] = "vol"
QUOTE_KIND_DIVIDEND: Final[str] = "dividend"

_KNOWN_KINDS: Final[frozenset[str]] = frozenset(
    {QUOTE_KIND_RATE, QUOTE_KIND_INFLATION, QUOTE_KIND_VOL, QUOTE_KIND_DIVIDEND}
)


@dataclass(frozen=True, slots=True)
class _CurveModel:
    """Closed-form parameters for one rates term structure.

    ``base`` is the short-end level, ``slope`` the long-end pickup, and
    ``decay`` (years) the speed the curve flattens. The resulting
    ``base + slope * (1 - exp(-t/decay))`` is strictly increasing in ``t``
    and bounded in ``(base, base + slope)``. ``max_daily_shift`` bounds the
    per-day parallel drift so the curve never goes non-positive.
    """

    base: float
    slope: float
    decay: float
    max_daily_shift: float = 0.0010  # ±10bp parallel, seeded by as_of


# Per-curve economics. Keyed by the ``curve`` token on the spec. Distinct
# levels keep the three rate curves visibly different (USD > EUR; SOFR a
# touch below USD IRS) without breaking monotonicity within any one curve.
_CURVES: Final[dict[str, _CurveModel]] = {
    "USD.IRS": _CurveModel(base=0.0200, slope=0.0300, decay=5.0),
    "USD.SOFR": _CurveModel(base=0.0190, slope=0.0290, decay=5.0),
    "EUR.IRS": _CurveModel(base=0.0150, slope=0.0250, decay=5.0),
}
_DEFAULT_CURVE: Final[_CurveModel] = _CurveModel(base=0.0200, slope=0.0300, decay=5.0)


@dataclass(frozen=True, slots=True)
class SyntheticSeriesSpec:
    """One synthetic series to generate.

    ``quote_kind`` selects the economic model; ``curve`` groups rate
    points that share a single per-day parallel shift (so a whole curve
    moves together and stays monotone). ``curve`` is unused for
    non-rate kinds.
    """

    canonical_id: str
    tenor: str
    quote_kind: str
    curve: str
    description: str


class SyntheticAdapter:
    """Generate standing synthetic quotes for the demo vocabulary."""

    def fetch_series(
        self,
        specs: Sequence[SyntheticSeriesSpec],
        as_of: date,
        start_date: date | None = None,
    ) -> list[QuoteRecord]:
        """Return one :class:`QuoteRecord` per spec at ``as_of``.

        ``start_date`` is accepted for interface parity with the vendor
        adapters but ignored: synthetic data is *standing* (one
        point-in-time value per series per day), so a window collapses to
        the ``as_of`` snapshot. The window's start is still logged.
        """

        if len(specs) == 0:
            msg = "No synthetic series provided"
            raise RuntimeError(msg)

        as_of_dt = datetime(as_of.year, as_of.month, as_of.day, tzinfo=UTC)
        out: list[QuoteRecord] = []
        for spec in specs:
            if spec.quote_kind not in _KNOWN_KINDS:
                # A config typo should fail loudly, not silently emit a
                # zero — a degenerate quote would ABORT the engine bootstrap.
                msg = (
                    f"Unknown synthetic quote_kind {spec.quote_kind!r} for "
                    f"{spec.canonical_id!r}; expected one of {sorted(_KNOWN_KINDS)}"
                )
                raise RuntimeError(msg)
            value = _value_for(spec, as_of)
            out.append(_to_record(spec, value, as_of_dt))

        logger.info(
            "synthetic.generated",
            as_of=as_of.isoformat(),
            start_date=(start_date or as_of).isoformat(),
            series=len(out),
            dataset=DATASET,
        )
        return out


# ---------------------------------------------------------------------------
# Economic model
# ---------------------------------------------------------------------------


def _value_for(spec: SyntheticSeriesSpec, as_of: date) -> float:
    if spec.quote_kind == QUOTE_KIND_RATE:
        return _value_rate(spec, as_of)
    if spec.quote_kind == QUOTE_KIND_INFLATION:
        return _value_inflation(spec, as_of)
    if spec.quote_kind == QUOTE_KIND_VOL:
        return _value_vol(spec, as_of)
    # QUOTE_KIND_DIVIDEND — validated by the caller.
    return _value_dividend(spec, as_of)


def _value_rate(spec: SyntheticSeriesSpec, as_of: date) -> float:
    model = _CURVES.get(spec.curve, _DEFAULT_CURVE)
    t = _tenor_to_years(spec.tenor)
    base = model.base + model.slope * (1.0 - math.exp(-t / model.decay))
    # One parallel shift per (curve, day) keeps the whole term structure
    # monotone while still drifting day-to-day. Bounded so base stays > 0.
    shift = _signed_unit(f"{spec.curve}|{as_of.isoformat()}") * model.max_daily_shift
    return round(base + shift, 6)


def _value_inflation(spec: SyntheticSeriesSpec, as_of: date) -> float:
    t = _tenor_to_years(spec.tenor)
    base = 0.0210 + 0.0010 * (1.0 - math.exp(-t / 5.0))  # ~2.1%-2.2%
    drift = _signed_unit(f"{spec.canonical_id}|{as_of.isoformat()}") * 0.0008
    return round(base + drift, 6)


def _value_vol(spec: SyntheticSeriesSpec, as_of: date) -> float:
    # Sensible ATM Black vol ~22%, drifting ±1.5vol-pts day-to-day.
    drift = _signed_unit(f"{spec.canonical_id}|{as_of.isoformat()}") * 0.015
    return round(0.2200 + drift, 6)


def _value_dividend(spec: SyntheticSeriesSpec, as_of: date) -> float:
    # Small positive dividend yield ~1.2%, drifting ±0.2%.
    drift = _signed_unit(f"{spec.canonical_id}|{as_of.isoformat()}") * 0.0020
    return round(0.0120 + drift, 6)


def _tenor_to_years(tenor: str) -> float:
    """Convert a tenor token (``3M``, ``1Y``, ``13W``…) to years.

    Swaption grid tenors like ``5Y10Y`` use the leading expiry (``5Y``)
    purely to give the daily-drift seed a stable handle; the swaption vol
    value itself is tenor-independent.
    """

    token = tenor.strip().upper()
    digits = ""
    for ch in token:
        if ch.isdigit():
            digits += ch
        else:
            break
    if not digits:
        msg = f"Cannot parse tenor {tenor!r}"
        raise RuntimeError(msg)
    n = int(digits)
    unit = token[len(digits)]
    if unit == "D":
        return n / 365.0
    if unit == "W":
        return n * 7.0 / 365.0
    if unit == "M":
        return n / 12.0
    if unit == "Y":
        return float(n)
    msg = f"Unknown tenor unit in {tenor!r}"
    raise RuntimeError(msg)


def _signed_unit(seed: str) -> float:
    """Deterministic value in ``[-1.0, 1.0)`` from ``seed`` (stable hash).

    ``hash()`` is per-process salted (PYTHONHASHSEED), which would break
    determinism across runs — so we hash with blake2b instead.
    """

    digest = hashlib.blake2b(seed.encode("utf-8"), digest_size=8).digest()
    as_int = int.from_bytes(digest, "big")
    # Map the 64-bit integer onto [0, 1) then onto [-1, 1).
    unit = as_int / float(1 << 64)
    return unit * 2.0 - 1.0


def _to_record(spec: SyntheticSeriesSpec, value: float, as_of_dt: datetime) -> QuoteRecord:
    return QuoteRecord(
        canonical_id=spec.canonical_id,
        as_of=as_of_dt,
        value=value,
        source=_SOURCE,
        vendor_id=f"SYNTH:{spec.canonical_id}",
        quality_flags={"synthetic": True},
        meta={
            "synthetic": True,
            "disclaimer": _DISCLAIMER,
            "dataset": DATASET,
            "generator": _GENERATOR_VERSION,
            "quote_kind": spec.quote_kind,
            "curve": spec.curve,
            "tenor": spec.tenor,
            "normalized_unit": "decimal_rate",
            "raw_value_percent": round(value * 100.0, 6),
            "series_description": spec.description,
        },
    )
