"""Engine lifecycle + dependency injection for the MD read service.

The service uses ``quantra_common.db.make_md_engine(role="ro")`` so the
``md_ro`` per-role pool isolation applies on this rail just like it does
for the orchestrator's MD reads. ``POSTGRES_DSN_MD_RO`` points at the
``md.*`` schema in the shared Postgres instance.

The engine is built once during FastAPI's ``lifespan`` and disposed on
shutdown. Route handlers receive it via a typed dependency so tests can
override it without touching app state.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_common.db import make_md_engine
from quantra_common.logging import get_logger
from quantra_market_data.settings import MdServiceSettings

logger = get_logger(__name__)


@asynccontextmanager
async def md_engine_lifespan(
    app: FastAPI,
    settings: MdServiceSettings,
) -> AsyncIterator[None]:
    """Build the MD ``ro`` engine on startup, dispose it on shutdown."""

    engine = make_md_engine(role="ro", settings=settings)
    app.state.md_engine = engine
    logger.info(
        "md_service.started",
        port=settings.md_service_port,
        env=settings.env.value,
    )
    try:
        yield
    finally:
        await engine.dispose()
        logger.info("md_service.stopped")


def get_md_engine(request: Request) -> AsyncEngine:
    """FastAPI dependency that returns the request-scoped MD engine.

    Raises ``RuntimeError`` if the lifespan handler did not set the engine,
    which would only happen in misconfigured tests that forget to use the
    fixture.
    """

    engine = getattr(request.app.state, "md_engine", None)
    if engine is None:
        msg = "MD engine is not initialised; ensure the FastAPI lifespan ran"
        raise RuntimeError(msg)
    if not isinstance(engine, AsyncEngine):
        msg = f"app.state.md_engine has unexpected type {type(engine)!r}"
        raise RuntimeError(msg)
    return engine
