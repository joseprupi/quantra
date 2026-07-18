"""Entry point for ``python -m quantra_market_data``.

Mirrors the legacy ``services/api/main.py`` ``__main__`` block: bind uvicorn
to ``0.0.0.0:<MD_SERVICE_PORT>`` (default ``8082``) and serve the FastAPI
app. The Dockerfile uses this entry point too so the dev and prod
boot paths stay identical.
"""

from __future__ import annotations

import uvicorn

from quantra_market_data.app import create_app
from quantra_market_data.settings import get_md_settings


def main() -> None:
    settings = get_md_settings()
    app = create_app(settings)
    uvicorn.run(
        app,
        host="0.0.0.0",  # noqa: S104 — internal-only service; bind chosen by deploy env
        port=settings.md_service_port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
