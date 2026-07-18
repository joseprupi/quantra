"""Vendor connector adapters for the MD ingester.

Each connector exposes a ``fetch_series(specs, as_of, start_date)``
method that returns a list of :class:`QuoteRecord`. Connectors are
intentionally synchronous (urllib + stdlib parsers); the pipeline
runs them inside ``asyncio.to_thread`` so the orchestrator stays on
the asyncio event loop while the connectors block on network I/O.

Source order matches the legacy ``quantra-market-data`` worker.
"""

from __future__ import annotations

from quantra_md_ingester.connectors.boe import BoeAdapter, BoeSeriesSpec
from quantra_md_ingester.connectors.boe_govt_curve import (
    BoeGovtCurveAdapter,
    BoeGovtCurveSeriesSpec,
)
from quantra_md_ingester.connectors.boe_ois_curve import (
    BoeOisCurveAdapter,
    BoeOisCurveSeriesSpec,
)
from quantra_md_ingester.connectors.ecb import EcbAdapter, EcbSeriesSpec
from quantra_md_ingester.connectors.fred import FredAdapter, FredSeriesSpec
from quantra_md_ingester.connectors.synthetic import SyntheticAdapter, SyntheticSeriesSpec
from quantra_md_ingester.connectors.treasury import TreasuryAdapter, TreasurySeriesSpec

__all__ = [
    "BoeAdapter",
    "BoeGovtCurveAdapter",
    "BoeGovtCurveSeriesSpec",
    "BoeOisCurveAdapter",
    "BoeOisCurveSeriesSpec",
    "BoeSeriesSpec",
    "EcbAdapter",
    "EcbSeriesSpec",
    "FredAdapter",
    "FredSeriesSpec",
    "SyntheticAdapter",
    "SyntheticSeriesSpec",
    "TreasuryAdapter",
    "TreasurySeriesSpec",
]
