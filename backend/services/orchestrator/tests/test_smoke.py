"""Smoke test: orchestrator package imports and exposes its factory."""

from __future__ import annotations

import quantra_common
import quantra_orchestrator


def test_orchestrator_exports_create_app() -> None:
    assert callable(quantra_orchestrator.create_app)


def test_orchestrator_can_import_quantra_common() -> None:
    assert quantra_common.__version__


def test_create_app_factory_is_exported() -> None:
    assert callable(quantra_orchestrator.create_app)
