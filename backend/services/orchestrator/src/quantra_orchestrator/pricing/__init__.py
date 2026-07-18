"""Pricing-side seams shared by the orchestrator's per-product code.

This package hosts the *seams* — the small, dependency-free types and
runners that each per-product package composes with its own typed
`Trade` models, FlatBuffers translators, and engine RPCs. The pricing
routes themselves live in the per-product subpackages
(``pricing/<product>/api.py``).

Currently exposed:

* :mod:`quantra_orchestrator.pricing.concurrency` — the concurrency seam:
  :class:`EngineBatch`, :class:`ConcurrencyPolicy`, the trivial
  :class:`OneTradePerCall` policy, the :func:`execute` runner, and the
  string-keyed :data:`POLICY_REGISTRY` (`"one_trade_per_call"` →
  :class:`OneTradePerCall`).

The seam preserves the north-star invariant that **QuantLib is not
threadsafe** — the engine fan-out lives across N processes behind
Envoy. The orchestrator's job is to slice a flat trade list into batches
and issue each batch as one concurrent gRPC call. Each product route
chooses the policy (and provides the ``price_batch`` callable that maps
:class:`EngineBatch` → the engine's wire payload).
"""

from quantra_orchestrator.pricing.concurrency import (
    POLICY_REGISTRY,
    ConcurrencyPolicy,
    EngineBatch,
    OneTradePerCall,
    execute,
    resolve_policy,
)

__all__ = [
    "POLICY_REGISTRY",
    "ConcurrencyPolicy",
    "EngineBatch",
    "OneTradePerCall",
    "execute",
    "resolve_policy",
]
