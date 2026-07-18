"""MD service route modules.

``register_all`` mounts every router on the FastAPI app. The surface is
deliberately small: the health probe, the public catalog listing, the
series range read, and the batch quote resolver — everything the
orchestrator and the portal actually call. The legacy snapshot / status /
timeseries-alias routes were deleted as dead code.
"""

from fastapi import FastAPI

from quantra_market_data.routes import health, public, quotes


def register_all(app: FastAPI) -> None:
    """Mount every route module on ``app``."""

    app.include_router(health.router)
    app.include_router(quotes.router)
    app.include_router(public.router)


__all__ = ["register_all"]
