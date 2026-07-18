"""Quantra market data read service.

Internal-only FastAPI app that serves the public/legacy MD endpoints used by
the portal and (later) the orchestrator's `MdClient`. The service is
read-only on the request path; ingestion lives in
`services/md_ingester` (D3 + the north-star invariant on MD writes).

Lifted into the monorepo from the standalone `quantra-market-data` repo by
the `market_data/01_move_into_monorepo.md` plan with no behavior change
visible to clients.
"""

from quantra_market_data.app import create_app

__version__ = "0.1.0"
__all__ = ["__version__", "create_app"]
