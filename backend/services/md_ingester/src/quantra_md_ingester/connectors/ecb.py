"""ECB Statistical Data Warehouse connector.

Behavior
is unchanged; the only swap is the import path and the structlog
logging call replacing the legacy ``print``.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from io import StringIO
from typing import Final
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

from quantra_common.logging import get_logger
from quantra_md_ingester.models import QuoteRecord

logger = get_logger(__name__)

ECB_DATA_API_BASE: Final[str] = "https://data-api.ecb.europa.eu/service/data"


@dataclass(frozen=True, slots=True)
class EcbSeriesSpec:
    flow: str
    series_key: str
    canonical_id: str
    tenor: str
    description: str
    base_currency: str
    quote_currency: str
    normalized_unit: str = "spot_rate"


class EcbAdapter:
    """Pull series from the ECB Data API in CSV format."""

    def __init__(self, timeout_seconds: int = 20) -> None:
        self.timeout_seconds = timeout_seconds

    def fetch_series(
        self,
        specs: Sequence[EcbSeriesSpec],
        as_of: date,
        start_date: date | None = None,
    ) -> list[QuoteRecord]:
        if len(specs) == 0:
            return []
        start = start_date or as_of
        out: list[QuoteRecord] = []
        for spec in specs:
            try:
                out.extend(self._fetch_series(spec, start_date=start, end_date=as_of))
            except RuntimeError as exc:
                logger.warning(
                    "ecb.series_skipped",
                    series_key=spec.series_key,
                    error=str(exc),
                )
        return out

    def _fetch_series(
        self,
        spec: EcbSeriesSpec,
        start_date: date,
        end_date: date,
    ) -> Iterable[QuoteRecord]:
        freq = _series_frequency(spec.series_key)
        params = {
            "startPeriod": _format_period_for_freq(start_date, freq),
            "endPeriod": _format_period_for_freq(end_date, freq),
            "format": "csvdata",
        }
        query = urlencode(params)
        url = f"{ECB_DATA_API_BASE}/{spec.flow}/{spec.series_key}?{query}"
        try:
            with urlopen(url, timeout=self.timeout_seconds) as response:  # noqa: S310 - HTTPS only
                payload = response.read().decode("utf-8-sig")
        except HTTPError as exc:
            msg = f"ECB request failed for series {spec.series_key}: HTTP {exc.code}"
            raise RuntimeError(msg) from exc

        # Honor the unit contract the spec promises (bug: the connector
        # used to store OBS_VALUE verbatim for every series, so percent
        # rate series landed un-scaled — 2.339 where 0.02339 was meant).
        # * ``spot_rate`` — FX reference rates; OBS_VALUE is already the
        #   quoted rate, store verbatim.
        # * anything else (``decimal_rate``) — ECB rate series publish
        #   percent; normalise to decimal like the other connectors.
        is_spot = spec.normalized_unit == "spot_rate"

        reader = csv.DictReader(StringIO(payload))
        for row in reader:
            raw_date = row.get("TIME_PERIOD")
            raw_value = row.get("OBS_VALUE")
            if not raw_date or not raw_value:
                continue
            try:
                raw_number = float(raw_value)
                value = raw_number if is_spot else raw_number / 100.0
                dt = _parse_ecb_time_period(raw_date)
            except ValueError:
                continue

            meta: dict[str, object] = {
                "flow": spec.flow,
                "series_key": spec.series_key,
                "series_description": spec.description,
                "base_currency": spec.base_currency,
                "quote_currency": spec.quote_currency,
                "vendor_unit": (
                    f"{spec.quote_currency}_per_{spec.base_currency}" if is_spot else "percent"
                ),
                "normalized_unit": "spot_rate" if is_spot else "decimal_rate",
                "tenor": spec.tenor,
            }
            if not is_spot:
                meta["raw_value_percent"] = raw_number

            yield QuoteRecord(
                canonical_id=spec.canonical_id,
                as_of=dt,
                value=value,
                source="ECB",
                vendor_id=spec.series_key,
                quality_flags={},
                meta=meta,
                units="spot_rate" if is_spot else "decimal_rate",
            )


_ECB_DAILY_LEN: Final[int] = 10
_ECB_MONTHLY_LEN: Final[int] = 7


def _parse_ecb_time_period(raw: str) -> datetime:
    # ECB TIME_PERIOD can be daily (YYYY-MM-DD) or monthly (YYYY-MM).
    if len(raw) == _ECB_DAILY_LEN:
        return datetime.fromisoformat(raw).replace(tzinfo=UTC)
    if len(raw) == _ECB_MONTHLY_LEN:
        return datetime.strptime(raw, "%Y-%m").replace(tzinfo=UTC)
    msg = f"Unsupported ECB TIME_PERIOD format: {raw}"
    raise ValueError(msg)


def _series_frequency(series_key: str) -> str:
    token = series_key.split(".", 1)[0].strip().upper()
    return token if token != "" else "D"


def _format_period_for_freq(value: date, freq: str) -> str:
    if freq == "M":
        return value.strftime("%Y-%m")
    return value.isoformat()
