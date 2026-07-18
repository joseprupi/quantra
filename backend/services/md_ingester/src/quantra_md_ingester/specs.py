"""Connector-spec loaders.

Centralised here (rather than inline in ``pipeline.py``) so the CLI's
subcommands share one loader per vendor and the parser stays trivially
testable without an event loop.

JSON layout matches the legacy schema exactly so the in-tree
``configs/<vendor>/*.json`` files can be lifted byte-for-byte from
the pre-monorepo market-data service.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from quantra_md_ingester.connectors import (
    BoeGovtCurveSeriesSpec,
    BoeOisCurveSeriesSpec,
    BoeSeriesSpec,
    EcbSeriesSpec,
    FredSeriesSpec,
    SyntheticSeriesSpec,
    TreasurySeriesSpec,
)

# Configs are shipped *inside* the wheel under
# ``quantra_md_ingester/configs`` so a container run uses the exact JSON
# the repo was built with. The Path(__file__).parent / "configs"
# resolution works equally for editable installs and built wheels.
_CONFIGS_DIR: Path = Path(__file__).resolve().parent / "configs"


def default_fred_config_paths() -> tuple[Path, ...]:
    # multi-file FRED configs. The pipeline coalesces every
    # FRED-flavored JSON the wheel ships so adding a new canonical-id
    # vocabulary is a config-only change, not a code edit.
    # The former ``usd_swap_rates.json`` (DSWP* — discontinued by FRED in
    # 2016; a deep backfill would have written decade-old swap fixings as
    # fresh) and ``usd_sofr_swaps.json`` (series ids that do not exist on
    # FRED — guaranteed daily skip-warnings) placeholder configs were
    # DELETED: a "placeholder" that a connector actually executes is an
    # ingestion bug, not documentation. ``USD.IRS.*`` / ``USD.SOFR.*``
    # remain synthetic/manual-only until a real vendor lands.
    return (_CONFIGS_DIR / "fred" / "ust_yields.json",)


def default_ecb_config_paths() -> tuple[Path, ...]:
    # The former ``eur_swap_rates.json`` placeholder was DELETED: it mapped
    # all four ``EUR.IRS.*`` tenors to the SAME monthly EURIBOR-3M average
    # series (which resolves!), writing four identical, un-scaled junk
    # fixings per run. ``EUR.IRS.*`` stays synthetic/manual-only until a
    # real EUR swap-fixings vendor lands.
    return (
        _CONFIGS_DIR / "ecb" / "fx_reference_rates.json",
        _CONFIGS_DIR / "ecb" / "inflation_rates.json",
    )


def default_boe_config_path() -> Path:
    return _CONFIGS_DIR / "boe" / "sonia.json"


def default_boe_govt_curve_config_path() -> Path:
    return _CONFIGS_DIR / "boe" / "govt_curve.json"


def default_boe_ois_curve_config_path() -> Path:
    return _CONFIGS_DIR / "boe" / "ois_curve.json"


def default_treasury_config_paths() -> tuple[Path, ...]:
    return (
        _CONFIGS_DIR / "treasury" / "nominal_curve.json",
        _CONFIGS_DIR / "treasury" / "real_curve.json",
        _CONFIGS_DIR / "treasury" / "bill_rates.json",
    )


def default_synthetic_config_paths() -> tuple[Path, ...]:
    # synthetic standing-data demo dataset. Covers the full portal
    # canonical-id vocabulary so all six products price end-to-end through
    # real resolved MD. Behind the explicit ``--source synthetic`` opt-in
    # (not in the default vendor set) so it never silently mixes into a
    # real-vendor ingest run.
    return (_CONFIGS_DIR / "synthetic" / "standing_demo.json",)


def load_fred_series(path: Path) -> list[FredSeriesSpec]:
    return [
        FredSeriesSpec(
            series_id=str(item["series_id"]),
            canonical_id=str(item["canonical_id"]),
            tenor=str(item["tenor"]),
            description=str(item["description"]),
            # ``percent`` (default) is normalised /100 to a decimal rate;
            # ``level`` (CPI/VIX/SOFRINDEX/MOVE index levels) is stored
            # verbatim.
            vendor_unit=str(item.get("vendor_unit", "percent")),
        )
        for item in _read_series(path)
    ]


def load_ecb_series(path: Path) -> list[EcbSeriesSpec]:
    return [
        EcbSeriesSpec(
            flow=str(item.get("flow", "EXR")),
            series_key=str(item["series_key"]),
            canonical_id=str(item["canonical_id"]),
            tenor=str(item["tenor"]),
            description=str(item["description"]),
            base_currency=str(item.get("base_currency", "")),
            quote_currency=str(item.get("quote_currency", "")),
            normalized_unit=str(item.get("normalized_unit", "spot_rate")),
        )
        for item in _read_series(path)
    ]


def load_boe_series(path: Path) -> list[BoeSeriesSpec]:
    return [
        BoeSeriesSpec(
            series_code=str(item["series_code"]),
            canonical_id=str(item["canonical_id"]),
            tenor=str(item["tenor"]),
            description=str(item["description"]),
        )
        for item in _read_series(path)
    ]


def load_boe_govt_curve_series(path: Path) -> list[BoeGovtCurveSeriesSpec]:
    return [
        BoeGovtCurveSeriesSpec(
            maturity_years=float(item["maturity_years"]),
            canonical_id=str(item["canonical_id"]),
            tenor=str(item["tenor"]),
            description=str(item["description"]),
        )
        for item in _read_series(path)
    ]


def load_boe_ois_curve_series(path: Path) -> list[BoeOisCurveSeriesSpec]:
    return [
        BoeOisCurveSeriesSpec(
            maturity_years=float(item["maturity_years"]),
            canonical_id=str(item["canonical_id"]),
            tenor=str(item["tenor"]),
            description=str(item["description"]),
        )
        for item in _read_series(path)
    ]


def load_synthetic_series(path: Path) -> list[SyntheticSeriesSpec]:
    return [
        SyntheticSeriesSpec(
            canonical_id=str(item["canonical_id"]),
            tenor=str(item["tenor"]),
            quote_kind=str(item["quote_kind"]),
            curve=str(item.get("curve", "")),
            description=str(item["description"]),
        )
        for item in _read_series(path)
    ]


def load_treasury_series(path: Path) -> list[TreasurySeriesSpec]:
    return [
        TreasurySeriesSpec(
            page_type=str(item["page_type"]),
            value_class_token=str(item["value_class_token"]),
            archive_column=(
                str(item["archive_column"]) if item.get("archive_column") is not None else None
            ),
            canonical_id=str(item["canonical_id"]),
            tenor=str(item["tenor"]),
            description=str(item["description"]),
        )
        for item in _read_series(path)
    ]


def _read_series(path: Path) -> list[Mapping[str, Any]]:
    """Parse ``path`` and return its ``"series"`` list as typed mappings."""

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        msg = f"Spec file {path} did not contain a JSON object at the root"
        raise TypeError(msg)
    series = raw.get("series", [])
    if not isinstance(series, list):
        msg = f"Spec file {path} 'series' must be a list, got {type(series).__name__}"
        raise TypeError(msg)
    out: list[Mapping[str, Any]] = []
    for item in series:
        if not isinstance(item, dict):
            msg = f"Spec file {path} contains a non-object entry under 'series'"
            raise TypeError(msg)
        out.append(item)
    return out
