"""structlog setup. Idempotent — safe to call multiple times per process."""

from __future__ import annotations

import logging
import sys
from typing import cast

import structlog
from structlog.stdlib import BoundLogger
from structlog.types import EventDict, Processor, WrappedLogger

from quantra_common.settings.base import Environment, LogLevel, Settings


def configure_logging(settings: Settings | None = None) -> None:
    """Install structlog processors and route stdlib logging through them.

    Called from each service entry point before the FastAPI/worker app starts
    handling requests. Calling it more than once replaces the previous config,
    which is the desired behavior for tests that want isolation.
    """

    cfg = settings or Settings()
    level = _level_to_int(cfg.log_level)

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _add_environment(cfg.env),
    ]

    if cfg.env is Environment.DEV:
        renderer: Processor = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


def get_logger(name: str | None = None) -> BoundLogger:
    """Return a configured logger.

    Service code should import this rather than `structlog.get_logger`
    directly so a single typed surface is enforced.
    """

    return cast(BoundLogger, structlog.get_logger(name))


def _level_to_int(level: LogLevel) -> int:
    value = logging.getLevelName(level.value)
    if not isinstance(value, int):
        msg = f"Unknown log level: {level.value}"
        raise ValueError(msg)
    return value


def _add_environment(env: Environment) -> Processor:
    def _processor(
        _logger: WrappedLogger,
        _method: str,
        event_dict: EventDict,
    ) -> EventDict:
        event_dict.setdefault("env", env.value)
        return event_dict

    return _processor
