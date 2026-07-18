"""FRED (St. Louis Fed) connector.

The only
non-trivial change versus the legacy code is the import path (the
``QuoteRecord`` carrier is service-local) and structlog logging instead of
``print`` for the per-series warning when a fetch fails.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

from quantra_common.logging import get_logger
from quantra_md_ingester.models import QuoteRecord

logger = get_logger(__name__)

FRED_BASE_URL: Final[str] = "https://api.stlouisfed.org/fred/series/observations"


@dataclass(frozen=True, slots=True)
class FredSeriesSpec:
    series_id: str
    canonical_id: str
    tenor: str
    description: str
    # What the vendor publishes for this series: ``percent`` (rate series
    # such as DGS*/SOFR — normalised to decimal on ingest) or ``level``
    # (index levels such as CPIAUCSL/VIXCLS/SOFRINDEX/MOVE — stored
    # verbatim; dividing an index level by 100 corrupts it).
    vendor_unit: str = "percent"


class FredAdapter:
    """Pull rate / index observations from the FRED public API."""

    def __init__(self, api_key: str | None = None, timeout_seconds: int = 20) -> None:
        self.api_key = api_key or os.getenv("FRED_API_KEY")
        self.timeout_seconds = timeout_seconds

    def fetch_series(
        self,
        specs: Sequence[FredSeriesSpec],
        as_of: date,
        start_date: date | None = None,
    ) -> list[QuoteRecord]:
        if not self.api_key:
            msg = "FRED_API_KEY is required"
            raise RuntimeError(msg)
        if len(specs) == 0:
            msg = "No FRED series provided"
            raise RuntimeError(msg)
        start = start_date or as_of
        out: list[QuoteRecord] = []
        for spec in specs:
            try:
                out.extend(self._fetch_series(spec, start_date=start, end_date=as_of))
            except RuntimeError as exc:
                # Keep long ingestion runs resilient when one vendor series
                # is temporarily unavailable / invalid.
                logger.warning(
                    "fred.series_skipped",
                    series_id=spec.series_id,
                    error=str(exc),
                )
        return out

    def _fetch_series(
        self,
        spec: FredSeriesSpec,
        start_date: date,
        end_date: date,
    ) -> Iterable[QuoteRecord]:
        params = {
            "series_id": spec.series_id,
            "file_type": "json",
            "observation_start": start_date.isoformat(),
            "observation_end": end_date.isoformat(),
            "sort_order": "asc",
            "api_key": self.api_key or "",
        }
        url = f"{FRED_BASE_URL}?{urlencode(params)}"
        try:
            with urlopen(url, timeout=self.timeout_seconds) as response:  # noqa: S310 - HTTPS only
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            msg = f"FRED request failed for series {spec.series_id}: HTTP {exc.code}"
            raise RuntimeError(msg) from exc

        # Percent rate series are normalised to decimals; level series
        # (CPI index, VIX, SOFR compounding index, MOVE, …) are stored
        # verbatim — applying the /100 to a level was a data-corruption
        # bug (CPI ~314 stored as 3.14).
        is_level = spec.vendor_unit == "level"

        observations = payload.get("observations", [])
        for item in observations:
            raw_value = item.get("value")
            raw_date = item.get("date")
            if not raw_value or raw_value == "." or not raw_date:
                continue
            try:
                raw_number = float(raw_value)
                value = raw_number if is_level else raw_number / 100.0
                dt = datetime.fromisoformat(raw_date).replace(tzinfo=UTC)
            except ValueError:
                continue

            meta: dict[str, object] = {
                "series_id": spec.series_id,
                "series_description": spec.description,
                "vendor_unit": "level" if is_level else "percent",
                "normalized_unit": "level" if is_level else "decimal_rate",
                "tenor": spec.tenor,
            }
            if not is_level:
                meta["raw_value_percent"] = raw_number

            yield QuoteRecord(
                canonical_id=spec.canonical_id,
                as_of=dt,
                value=value,
                source="FRED",
                vendor_id=spec.series_id,
                quality_flags={},
                meta=meta,
                units="level" if is_level else "decimal_rate",
            )
