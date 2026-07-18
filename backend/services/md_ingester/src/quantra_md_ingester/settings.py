"""Service-local settings for the MD ingester.

Inherits the cross-service base (``quantra_common.Settings``); the
ingester currently adds no fields of its own. Connector credentials
(e.g. ``FRED_API_KEY``) are read directly from the environment by the
connector that needs them so each stays testable in isolation.
"""

from __future__ import annotations

from functools import lru_cache

from quantra_common.settings import Settings


class MdIngesterSettings(Settings):
    """Settings for the MD ingester worker."""


@lru_cache(maxsize=1)
def get_md_ingester_settings() -> MdIngesterSettings:
    """Process-wide cached settings instance."""

    return MdIngesterSettings()
