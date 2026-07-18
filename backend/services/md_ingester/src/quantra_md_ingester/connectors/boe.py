"""Bank of England Interactive Database (IADB) connector.

The
``urlopen`` call uses an HTTPS URL with a fixed User-Agent (the BoE
site rejects the default urllib agent); ``# noqa: S310`` is added on
that single line because Bandit's URL-scheme heuristic flags
``Request``-based calls even when the scheme is constant.
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
from urllib.request import Request, urlopen

from quantra_common.logging import get_logger
from quantra_md_ingester.models import QuoteRecord

logger = get_logger(__name__)

BOE_IADB_URL: Final[str] = (
    "https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp"
)
_DATE_FMT: Final[str] = "%d %b %Y"


@dataclass(frozen=True, slots=True)
class BoeSeriesSpec:
    series_code: str
    canonical_id: str
    tenor: str
    description: str


class BoeAdapter:
    """Pull the BoE IADB CSV feed for one or more series codes."""

    def __init__(self, timeout_seconds: int = 20) -> None:
        self.timeout_seconds = timeout_seconds

    def fetch_series(
        self,
        specs: Sequence[BoeSeriesSpec],
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
                    "boe.series_skipped",
                    series_code=spec.series_code,
                    error=str(exc),
                )
        return out

    def _fetch_series(
        self,
        spec: BoeSeriesSpec,
        start_date: date,
        end_date: date,
    ) -> Iterable[QuoteRecord]:
        params = {
            "csv.x": "yes",
            "Datefrom": start_date.strftime("%d/%b/%Y"),
            "Dateto": end_date.strftime("%d/%b/%Y"),
            # CN: CSV, column format, no title lines (simplest parser path).
            "CSVF": "CN",
            "SeriesCodes": spec.series_code,
            "UsingCodes": "Y",
        }
        url = f"{BOE_IADB_URL}?{urlencode(params)}"
        request = Request(  # noqa: S310 - constant HTTPS host
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; QuantraMarketDataBot/1.0)",
                "Accept": "text/csv,text/plain,*/*",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                payload = response.read().decode("utf-8", errors="ignore")
        except HTTPError as exc:
            msg = f"BoE request failed for series {spec.series_code}: HTTP {exc.code}"
            raise RuntimeError(msg) from exc

        if "DATE" not in payload or "VALUE" not in payload:
            msg = "Unexpected BoE CSV response format"
            raise RuntimeError(msg)

        reader = csv.DictReader(StringIO(payload))
        row_count = 0
        for row in reader:
            try:
                raw_percent = float((row.get("VALUE") or "").strip())
                value = raw_percent / 100.0
                dt = datetime.strptime((row.get("DATE") or "").strip(), _DATE_FMT).replace(
                    tzinfo=UTC,
                )
            except ValueError:
                continue
            row_count += 1

            yield QuoteRecord(
                canonical_id=spec.canonical_id,
                as_of=dt,
                value=value,
                source="BOE",
                vendor_id=spec.series_code,
                quality_flags={},
                meta={
                    "series_code": spec.series_code,
                    "series_description": spec.description,
                    "vendor_unit": "percent",
                    "normalized_unit": "decimal_rate",
                    "raw_value_percent": raw_percent,
                    "tenor": spec.tenor,
                },
            )
        if row_count == 0:
            msg = "No data rows parsed from BoE CSV response"
            raise RuntimeError(msg)
