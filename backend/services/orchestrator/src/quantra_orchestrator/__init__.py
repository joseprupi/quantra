"""Quantra orchestrator service.

The single public REST surface of the backend. Hosts the high-level
product-shaped pricing endpoints, the data-layer CRUD surface, the
market-data endpoints, and the operational endpoints (health probe,
structured logging, request-ID middleware, and the global exception
handlers).
"""

from quantra_orchestrator.app import create_app

__all__ = ["create_app"]
