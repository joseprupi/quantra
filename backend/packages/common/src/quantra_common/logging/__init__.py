"""Structured logging for Quantra services.

Wraps `structlog` with two render profiles:

- **prod / staging** → single-line JSON (one event per line) for log aggregation.
- **dev** → colorized human-readable output for local terminals.

Always pulls the active level/env from `quantra_common.settings`. The request-ID
middleware wires `X-Request-Id` (configurable) through `structlog.contextvars`
so every log line emitted while handling a request is automatically tagged.
"""

from __future__ import annotations

from quantra_common.logging.config import configure_logging, get_logger
from quantra_common.logging.middleware import RequestIdMiddleware

__all__ = ["RequestIdMiddleware", "configure_logging", "get_logger"]
