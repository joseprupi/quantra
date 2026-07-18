"""Smoke test: market data service package imports."""

from __future__ import annotations

import quantra_common
import quantra_market_data


def test_market_data_version_exported() -> None:
    assert isinstance(quantra_market_data.__version__, str)
    assert quantra_market_data.__version__


def test_market_data_can_import_quantra_common() -> None:
    assert quantra_common.__version__


def test_create_app_factory_is_exported() -> None:
    assert callable(quantra_market_data.create_app)
