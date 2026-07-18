"""Shared pydantic v2 types.

Currently this package exposes a single cross-service model:
``ResolvedQuote``, the result of resolving a market-data reference through
``quantra_common.md_client``. The orchestrator consumes it when translating
quote-referencing pricing requests.

Curve models are *not* shared: each product owns its curve request models in
the orchestrator (``pricing/<product>/models.py``), and the market-data
service owns its own response schemas.
"""

from __future__ import annotations

from quantra_common.types.market_data import ResolvedQuote

__all__ = [
    "ResolvedQuote",
]
