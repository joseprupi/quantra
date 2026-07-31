"""Orchestrator route modules.

Each module owns a coherent slice of the public surface. ``register_all``
mounts every router on the FastAPI app. Today we ship:

* ``health`` — liveness probe (anonymous).
* ``auth`` — ``/auth/whoami`` debug endpoint.
* ``data`` — the sixteen ``/v1/<entity>`` CRUD routers + the
  ``/v1/pricing-history`` immutable view, owned by
  .
* ``debug_md`` — verification-only ``/debug/md/quote/{canonical_id}``
  + ``/debug/md/cache/stats`` from.
  Explicitly **not** part of the public API; the per-product
  endpoints (06-12) and engine forwarder (15) are the only sanctioned
  consumers of the MD client.
* ``debug_engine`` — verification-only ``/debug/engine/ping`` +
  ``/debug/engine/info`` from.
  Same not-public-API caveat as the MD debug routes; per-product
  endpoints and the engine forwarder are the public consumers.
* ``pricing.swap_ir`` — ``POST /v1/price/swap/ir`` from
  . The first per-product pricing
  endpoint; the pattern this router establishes is replicated by
  ``07``-``11``.
* ``pricing.swaption`` — ``POST /v1/price/swaption``. Adds
  optionality on top of the IR-swap stack (vol surface + swaption
  model).
* ``pricing.bonds`` — ``POST /v1/price/bonds/fixed`` +
  ``POST /v1/price/bonds/floating`` from
  . Two routes from one package — the
  fixed variant is curves-only (single discount curve), the
  floating variant resolves discount + projection curves and a
  projection index.
* ``pricing.cds`` — ``POST /v1/price/cds`` from
  . First per-product endpoint to walk
  two distinct entity families (``app.curves`` for the discount
  curve, ``app.credit_curves`` for the credit curve). The MD
  walker stays discount-curve only by design.
* ``pricing.equity_options`` — ``POST /v1/price/equity-option``
  from. First per-product
  endpoint to bundle three market-data families: discount +
  dividend curves under ``app.curves``, an equity Black-vol
  surface under ``app.vol_surfaces`` (``kind = 'BlackVolSpec'``
  invariant), and the underlier spot quote (inline literal or
  canonical-id resolved via the MD client; the no-MD-fallback rule: equity options
  does NOT bypass MD).
* ``pricing.swaps_inflation`` — ``POST /v1/price/swaps/inflation``
  from. First per-product
  endpoint to bundle a nominal discount curve, an inflation
  curve (rates-shaped — flows through the MD walker)
  and an inflation index spec (consumed verbatim — no MD
  resolution). Branches between the
  ``PRICE_ZERO_COUPON_INFLATION_SWAP`` and
  ``PRICE_YEAR_ON_YEAR_INFLATION_SWAP`` engine RPCs based on
  the trade body's ``swap_kind`` discriminator (settled).

The engine forwarder and remaining per-product pricing endpoints
land via their own plans.
"""

from fastapi import FastAPI

from quantra_orchestrator.api import (
    auth,
    calendar,
    curve_preview,
    data,
    debug_engine,
    debug_md,
    health,
    import_doc,
    market_data_import,
    market_data_latest,
    market_data_series,
    metrics,
    ready,
    traces,
    version,
    vol_tools,
)
from quantra_orchestrator.pricing.bonds import router as bonds_router
from quantra_orchestrator.pricing.cds import router as cds_router
from quantra_orchestrator.pricing.equity_options import (
    router as equity_options_router,
)
from quantra_orchestrator.pricing.swap_ir import router as swap_ir_router
from quantra_orchestrator.pricing.swaps_inflation import (
    router as swaps_inflation_router,
)
from quantra_orchestrator.pricing.swaption import router as swaption_router


def register_all(app: FastAPI) -> None:
    """Mount every route module on ``app``."""

    app.include_router(health.router)
    app.include_router(version.router)
    app.include_router(ready.router)
    app.include_router(metrics.router)
    app.include_router(auth.router)
    app.include_router(debug_md.router)
    app.include_router(debug_engine.router)
    app.include_router(traces.router)
    app.include_router(curve_preview.router)
    app.include_router(calendar.router)
    app.include_router(vol_tools.router)
    app.include_router(market_data_import.router)
    app.include_router(import_doc.router)
    app.include_router(market_data_series.router)
    app.include_router(market_data_latest.router)
    data.register_data(app)
    app.include_router(swap_ir_router)
    app.include_router(swaption_router)
    app.include_router(bonds_router)
    app.include_router(cds_router)
    app.include_router(equity_options_router)
    app.include_router(swaps_inflation_router)


__all__ = ["register_all"]
