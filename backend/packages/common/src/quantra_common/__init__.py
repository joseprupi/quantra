"""Shared library for Quantra backend services.

Cross-service building blocks used by the orchestrator, market-data
service, and MD ingester:

- ``auth`` — Firebase ID-token / API-key verification primitives.
- ``db`` — async SQLAlchemy engine factories + Alembic helpers.
- ``engine_client`` — gRPC client surface for the pricing engine and the
  vendored FlatBuffers bindings.
- ``logging`` — structlog configuration shared by every service.
- ``md_client`` — async HTTP client for the internal market-data service.
- ``settings`` — pydantic-settings base shared by service settings classes.
- ``types`` — cross-service pydantic models (``ResolvedQuote``).
"""

__version__ = "0.3.0"
