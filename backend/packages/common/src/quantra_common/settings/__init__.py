"""Shared application settings.

Loads from environment variables (with `.env` fallback) using
`pydantic-settings`. Every Quantra Python service reads its config through
this module so secrets/DSNs/log levels live in one place. See `.env.example`
at the repo root for the canonical defaults; the rationale for picking
pydantic-settings was a deliberate choice.
"""

from __future__ import annotations

from quantra_common.settings.base import Environment, LogLevel, Settings, get_settings

__all__ = ["Environment", "LogLevel", "Settings", "get_settings"]
