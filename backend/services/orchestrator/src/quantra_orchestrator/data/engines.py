"""Per-app engine resolution for data-layer routes.

The orchestrator factory parks ``app_rw`` and ``app_ro`` AsyncEngines on
``app.state`` (see ``quantra_orchestrator.app.create_app``). Production
deploys construct them in the lifespan from settings; tests inject
fakes before lifespan runs. Either path lands the engines in the same
attribute so the per-request dependencies below stay test-friendly.

A missing engine is a *deploy* error: the data-layer endpoint can't
serve traffic without a backing store. We surface it as a 503 carrying
the envelope token ``code="storage_unavailable"`` so a misconfigured
deploy is visible to clients rather than appearing as an opaque 500.
"""

from __future__ import annotations

from typing import Final

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncEngine

STORAGE_UNAVAILABLE_CODE: Final[str] = "storage_unavailable"


class AppDbUnavailableError(HTTPException):
    """503 raised when an ``app.*`` engine isn't attached to ``app.state``.

    The shared HTTPException handler reads the ``code`` class attribute
    and emits the envelope with ``code="storage_unavailable"``
    instead of the generic ``http_503`` token.
    """

    code: Final[str] = STORAGE_UNAVAILABLE_CODE

    def __init__(self, *, role: str) -> None:
        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Persistent storage is not configured (app_{role} engine "
                "missing). Set POSTGRES_DSN_APP_RW / POSTGRES_DSN_APP_RO."
            ),
        )


def get_app_rw_engine(request: Request) -> AsyncEngine:
    """Resolve the lifespan-owned ``app_rw`` engine or raise 503."""

    engine = getattr(request.app.state, "app_rw_engine", None)
    if engine is None:
        raise AppDbUnavailableError(role="rw")
    return engine  # type: ignore[no-any-return]


def get_app_ro_engine(request: Request) -> AsyncEngine:
    """Resolve the lifespan-owned ``app_ro`` engine or raise 503."""

    engine = getattr(request.app.state, "app_ro_engine", None)
    if engine is None:
        raise AppDbUnavailableError(role="ro")
    return engine  # type: ignore[no-any-return]


class MdWriteUnavailableError(HTTPException):
    """503 raised when the ``md_rw`` engine isn't attached to ``app.state``.

    The market-data import path is the ONLY orchestrator surface
    that writes to the shared ``md.*`` catalog; it needs a dedicated
    ``md_rw`` engine kept separate from the request-path ``md_ro`` reader.
    A missing engine is a deploy error (``POSTGRES_DSN_MD_RW`` unset), so
    we surface the same ``code="storage_unavailable"`` token as the
    ``app.*`` engines rather than an opaque 500.
    """

    code: Final[str] = STORAGE_UNAVAILABLE_CODE

    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Market-data import storage is not configured (md_rw engine "
                "missing). Set POSTGRES_DSN_MD_RW on the orchestrator."
            ),
        )


def get_md_rw_engine(request: Request) -> AsyncEngine:
    """Resolve the lifespan-owned ``md_rw`` engine or raise 503.

    Used ONLY by the market-data import route — deliberately kept
    distinct from the request-path ``md_ro`` engine so a user-import spike
    cannot contend with read traffic's pool.
    """

    engine = getattr(request.app.state, "md_rw_engine", None)
    if engine is None:
        raise MdWriteUnavailableError()
    return engine  # type: ignore[no-any-return]


__all__ = [
    "AppDbUnavailableError",
    "get_app_ro_engine",
    "get_app_rw_engine",
    "get_md_rw_engine",
]
