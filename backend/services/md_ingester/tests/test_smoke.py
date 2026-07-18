"""Smoke test: md ingester package imports."""

from __future__ import annotations

import quantra_common
import quantra_md_ingester


def test_md_ingester_version_exported() -> None:
    assert isinstance(quantra_md_ingester.__version__, str)
    assert quantra_md_ingester.__version__


def test_md_ingester_can_import_quantra_common() -> None:
    assert quantra_common.__version__
