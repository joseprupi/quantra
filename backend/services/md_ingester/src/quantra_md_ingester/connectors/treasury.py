"""US Treasury TextView + archive CSV connector.

Pulls daily yield / real-yield / bill-rate pages from the public
Treasury TextView UI for years >= 2024 and from the published archive
CSVs for earlier history.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from io import StringIO
from typing import Final
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from quantra_common.logging import get_logger
from quantra_md_ingester.models import QuoteRecord

logger = get_logger(__name__)

TREASURY_TEXTVIEW_URL: Final[str] = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView"
)
TREASURY_ARCHIVE_BASE_URL: Final[str] = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rate-archives"
)

TREASURY_ARCHIVE_CSV: Final[dict[str, str]] = {
    "daily_treasury_yield_curve": "par-yield-curve-rates-1990-2023.csv",
    "daily_treasury_real_yield_curve": "par-real-yield-curve-rates-2003-2023.csv",
    "daily_treasury_bill_rates": "bill-rates-2002-2023.csv",
}

_ARCHIVE_END_DATE: Final[date] = date(2023, 12, 31)

_TBODY_RE: Final[re.Pattern[str]] = re.compile(r"<tbody>(.*?)</tbody>", re.IGNORECASE | re.DOTALL)
_ROW_RE: Final[re.Pattern[str]] = re.compile(r"<tr>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL_RE: Final[re.Pattern[str]] = re.compile(
    r'<td[^>]*class="([^"]*)"[^>]*>(.*?)</td>', re.IGNORECASE | re.DOTALL
)
_DATE_RE: Final[re.Pattern[str]] = re.compile(r'datetime="(\d{4}-\d{2}-\d{2})')
_TAG_RE: Final[re.Pattern[str]] = re.compile(r"<[^>]+>")
_WS_RE: Final[re.Pattern[str]] = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class TreasurySeriesSpec:
    page_type: str
    value_class_token: str
    archive_column: str | None
    canonical_id: str
    tenor: str
    description: str


class TreasuryAdapter:
    """Read Treasury yield-curve pages directly from the public site."""

    def __init__(self, timeout_seconds: int = 45) -> None:
        self.timeout_seconds = timeout_seconds

    def fetch_series(
        self,
        specs: Sequence[TreasurySeriesSpec],
        as_of: date,
        start_date: date | None = None,
    ) -> list[QuoteRecord]:
        if len(specs) == 0:
            return []
        start = start_date or as_of
        out: list[QuoteRecord] = []

        grouped: dict[str, list[TreasurySeriesSpec]] = {}
        for spec in specs:
            grouped.setdefault(spec.page_type, []).append(spec)

        for page_type, page_specs in grouped.items():
            seen_keys: set[tuple[str, date]] = set()
            archive_filename = TREASURY_ARCHIVE_CSV.get(page_type)
            if archive_filename is not None and start <= _ARCHIVE_END_DATE:
                archive_url = f"{TREASURY_ARCHIVE_BASE_URL}/{archive_filename}"
                try:
                    archive_rows = self._fetch_archive_csv_rows(archive_url)
                    self._append_rows(
                        out=out,
                        seen_keys=seen_keys,
                        page_specs=page_specs,
                        rows=archive_rows,
                        start_date=start,
                        end_date=min(as_of, _ARCHIVE_END_DATE),
                        value_mode="archive",
                    )
                except RuntimeError as exc:
                    logger.warning(
                        "treasury.archive_skipped",
                        page_type=page_type,
                        error=str(exc),
                    )

            textview_start_year = max(start.year, _ARCHIVE_END_DATE.year + 1)
            if textview_start_year <= as_of.year:
                for year in range(textview_start_year, as_of.year + 1):
                    try:
                        textview_rows = self._fetch_textview_rows(
                            page_type=page_type,
                            field_tdr_date_value=str(year),
                        )
                        self._append_rows(
                            out=out,
                            seen_keys=seen_keys,
                            page_specs=page_specs,
                            rows=textview_rows,
                            start_date=start,
                            end_date=as_of,
                            value_mode="textview",
                        )
                    except RuntimeError as exc:
                        logger.warning(
                            "treasury.textview_skipped",
                            page_type=page_type,
                            year=year,
                            error=str(exc),
                        )
                        continue
        return out

    def _append_rows(
        self,
        out: list[QuoteRecord],
        seen_keys: set[tuple[str, date]],
        page_specs: Sequence[TreasurySeriesSpec],
        rows: list[tuple[date, dict[str, str]]],
        start_date: date,
        end_date: date,
        value_mode: str,
    ) -> None:
        for row_date, values in rows:
            if row_date < start_date or row_date > end_date:
                continue
            as_of_dt = datetime.combine(row_date, datetime.min.time(), tzinfo=UTC)
            for spec in page_specs:
                raw_text = self._pick_value(spec=spec, values=values, value_mode=value_mode)
                if raw_text is None:
                    continue
                try:
                    raw_percent = float(raw_text)
                except ValueError:
                    continue
                key = (spec.canonical_id, row_date)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                out.append(
                    QuoteRecord(
                        canonical_id=spec.canonical_id,
                        as_of=as_of_dt,
                        value=raw_percent / 100.0,
                        source="TREASURY",
                        vendor_id=f"{spec.page_type}:{value_mode}:{spec.value_class_token}",
                        quality_flags={},
                        meta={
                            "page_type": spec.page_type,
                            "value_mode": value_mode,
                            "column_token": spec.value_class_token,
                            "archive_column": spec.archive_column,
                            "series_description": spec.description,
                            "tenor": spec.tenor,
                            "vendor_unit": "percent",
                            "normalized_unit": "decimal_rate",
                            "raw_value_percent": raw_percent,
                        },
                    )
                )

    def _pick_value(
        self,
        spec: TreasurySeriesSpec,
        values: dict[str, str],
        value_mode: str,
    ) -> str | None:
        if value_mode == "archive":
            archive_column = spec.archive_column
            if archive_column is None:
                return None
            return values.get(_normalize_key(archive_column))
        return values.get(spec.value_class_token)

    def _fetch_textview_rows(
        self,
        page_type: str,
        field_tdr_date_value: str,
    ) -> list[tuple[date, dict[str, str]]]:
        query = urlencode({"type": page_type, "field_tdr_date_value": field_tdr_date_value})
        url = f"{TREASURY_TEXTVIEW_URL}?{query}"
        request = Request(  # noqa: S310 - constant HTTPS host
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; QuantraMarketDataBot/1.0)",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
            payload = response.read().decode("utf-8", errors="ignore")

        tbody_match = _TBODY_RE.search(payload)
        if not tbody_match:
            msg = "Treasury table body not found in TextView response"
            raise RuntimeError(msg)

        rows: list[tuple[date, dict[str, str]]] = []
        for row_html in _ROW_RE.findall(tbody_match.group(1)):
            date_match = _DATE_RE.search(row_html)
            if not date_match:
                continue
            row_date = date.fromisoformat(date_match.group(1))
            values: dict[str, str] = {}
            for class_attr, raw_cell in _CELL_RE.findall(row_html):
                text = _WS_RE.sub(" ", _TAG_RE.sub("", raw_cell)).strip()
                if text == "" or text.upper() == "N/A":
                    continue
                for token in class_attr.split():
                    # FIRST-write-wins per class token. Treasury's own HTML
                    # is buggy on the real-yield page: the 20Y cell carries
                    # BOTH ``tc20year`` AND ``views-field-field-tc-10year``,
                    # so a last-write-wins map overwrote the genuine 10Y
                    # value with the 20Y one (10Y == 20Y byte-identically
                    # every day). Columns are emitted in ascending-tenor
                    # order, so the first cell claiming a token is the
                    # genuine one; later duplicate claims are spurious.
                    values.setdefault(token, text)
            rows.append((row_date, values))

        if len(rows) == 0:
            msg = "No table rows parsed from Treasury TextView response"
            raise RuntimeError(msg)
        return rows

    def _fetch_archive_csv_rows(self, url: str) -> list[tuple[date, dict[str, str]]]:
        request = Request(  # noqa: S310 - constant HTTPS host
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; QuantraMarketDataBot/1.0)",
                "Accept": "text/csv,text/plain,*/*",
            },
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
            payload = response.read().decode("utf-8", errors="ignore")

        reader = csv.DictReader(StringIO(payload))
        rows: list[tuple[date, dict[str, str]]] = []
        for row in reader:
            raw_date = (row.get("date") or row.get("Date") or "").strip()
            if raw_date == "":
                continue
            try:
                row_date = datetime.strptime(raw_date, "%m/%d/%Y").date()
            except ValueError:
                continue
            values: dict[str, str] = {}
            for key, val in row.items():
                if key is None or val is None:
                    continue
                cell = val.strip()
                if cell == "" or cell.upper() == "N/A":
                    continue
                values[_normalize_key(key)] = cell
            rows.append((row_date, values))

        if len(rows) == 0:
            msg = "No rows parsed from Treasury archive CSV"
            raise RuntimeError(msg)
        return rows


def _normalize_key(value: str) -> str:
    return _WS_RE.sub(" ", value.strip().lower())
