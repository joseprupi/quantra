"""Service-local settings layered on top of `quantra_common.settings`.

`quantra_common.Settings` carries the cross-service config (DSNs, pool
sizes, log level, request-id header). The MD service adds one field that
only it consumes: the listen port for the uvicorn entry point.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field

from quantra_common.settings import Settings


class MdServiceSettings(Settings):
    """Settings for the MD read service.

    Inherits the shared base, then layers in service-local fields. Pydantic
    Settings reads env vars case-insensitively, so ``MD_SERVICE_PORT`` is
    honoured in both lower and upper case.
    """

    md_service_port: int = Field(
        default=8082,
        ge=1,
        le=65535,
        description=(
            "Port uvicorn binds when the service is launched as "
            "``python -m quantra_market_data``. Matches the legacy PORT env var."
        ),
    )


@lru_cache(maxsize=1)
def get_md_settings() -> MdServiceSettings:
    """Process-wide cached service settings instance."""

    return MdServiceSettings()
