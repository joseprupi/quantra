"""Entry point for ``python -m quantra_orchestrator``.

Mirrors the MD service's ``__main__`` block: bind uvicorn to
``0.0.0.0:<ORCHESTRATOR_PORT>`` (default ``8080``) and serve the
FastAPI app. The Dockerfile uses this entry point too so the dev and
prod boot paths stay identical.
"""

from __future__ import annotations

import uvicorn

from quantra_orchestrator.app import create_app
from quantra_orchestrator.settings import get_orchestrator_settings


def main() -> None:
    settings = get_orchestrator_settings()
    app = create_app(settings)
    uvicorn.run(
        app,
        host="0.0.0.0",  # noqa: S104 — bind interface chosen by the deploy environment
        port=settings.orchestrator_port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
