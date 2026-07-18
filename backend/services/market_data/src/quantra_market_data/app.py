"""FastAPI app factory.

Building the app via a function (rather than a module-level singleton) keeps
import-time side effects to zero, which is the contract the smoke tests
rely on. The legacy service constructed the engine at import time; we hold
that off until the lifespan handler runs so ``import quantra_market_data``
never needs ``POSTGRES_DSN_MD_RO`` to be set.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from quantra_common.logging import RequestIdMiddleware, configure_logging
from quantra_market_data.db import md_engine_lifespan
from quantra_market_data.middleware import enforce_read_only_api
from quantra_market_data.routes import register_all
from quantra_market_data.settings import MdServiceSettings, get_md_settings


def create_app(settings: MdServiceSettings | None = None) -> FastAPI:
    """Build a fully-wired FastAPI app for the MD read service.

    Tests inject a ``settings`` instance to bypass env-driven config; the
    production entry point in ``__main__`` calls ``get_md_settings()`` which
    is process-cached.
    """

    cfg = settings or get_md_settings()
    configure_logging(cfg)

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with md_engine_lifespan(app, cfg):
            yield

    app = FastAPI(
        title="Quantra Market Data API",
        version="0.1.0",
        lifespan=_lifespan,
        openapi_tags=[
            {"name": "public", "description": "Public, user-facing endpoints."},
            {
                "name": "internal",
                "description": "Internal/legacy endpoints retained for compatibility.",
            },
        ],
    )

    app.add_middleware(RequestIdMiddleware, settings=cfg)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )
    app.middleware("http")(enforce_read_only_api)

    register_all(app)
    return app
