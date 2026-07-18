"""Bonds pricing endpoints.

the third per-product endpoint package in the
redesign. Following the six-module split, this package ships
**two routes from one package** (``POST /v1/price/bonds/fixed`` +
``POST /v1/price/bonds/floating``). The two variants share every
helper (assembler entry points, MD walker, error codes, models)
and diverge only in the per-route trade body, the engine RPC name,
and the number of resolved curves per call (1 for fixed; 1 or 2
for floating with ``use_same_curve`` shorthand).

Sub-modules:

* :mod:`quantra_orchestrator.pricing.bonds.api` —
  ``POST /v1/price/bonds/fixed`` + ``POST /v1/price/bonds/floating``
  routes and the lifespan-agnostic composition glue. Both routes
  share the structured-error-envelope attachment / log-shape / batch-coerce
  helpers in this module.
* :mod:`quantra_orchestrator.pricing.bonds.assembler` —
  ``app_ro``-side load of bond / curve(s) / index / snapshot. One
  shared module with two public entry points (``assemble_fixed`` /
  ``assemble_floating``); every helper that isn't strictly variant-
  specific (curve load, snapshot load, ``_extract_uuid``) is shared.
* :mod:`quantra_orchestrator.pricing.bonds.md_resolution` —
  product-local quote-ID resolution walker (snapshot pins first,
  live MD second). Structurally identical to the swap_ir-side
  walker; the only difference is accepting a list of curves so the
  floating route can pass discount + projection in one call. Not
  factored into a shared module yet per the product plan's "wait until
  at least three products land — and even then defer to a post-
  Phase-3 plan" guidance.
* :mod:`quantra_orchestrator.pricing.bonds.engine_io` — six
  callables (build / decode / price_batch) — one trio per variant.
  RPC names are :attr:`EngineRpc.PRICE_FIXED_RATE_BOND` /
  :attr:`EngineRpc.PRICE_FLOATING_RATE_BOND` (the canonical enum
  entries).
* :mod:`quantra_orchestrator.pricing.bonds.errors` — codes
  ``bond_*`` and the matching :class:`HTTPException` subclasses:
  404s (one per variant + discount curve + projection curve +
  index + snapshot), a shared 422 for curve resolution, 422s for
  quote resolution and missing trade fields. Per-bundle-stage 422
  codes are deliberately collapsed to one
  (``bond_curve_resolution_failed``) with the role surfaced in
  ``details``.
* :mod:`quantra_orchestrator.pricing.bonds.models` —
  Pydantic v2 request / response / intermediate models. Shared
  ``CurveRef`` / ``ResolvedCurve`` / ``ResolvedQuoteValue`` shapes
  are deliberately duplicated from swap_ir / swaption — a shared
  module is a candidate future refactor.
"""

from quantra_orchestrator.pricing.bonds.api import router
from quantra_orchestrator.pricing.bonds.errors import (
    BOND_CURVE_RESOLUTION_FAILED_CODE,
    BOND_DISCOUNT_CURVE_NOT_FOUND_CODE,
    BOND_FIXED_NOT_FOUND_CODE,
    BOND_FLOATING_NOT_FOUND_CODE,
    BOND_INDEX_NOT_FOUND_CODE,
    BOND_MISSING_TRADE_FIELDS_CODE,
    BOND_PROJECTION_CURVE_NOT_FOUND_CODE,
    BOND_QUOTE_RESOLUTION_FAILED_CODE,
    BOND_SNAPSHOT_NOT_FOUND_CODE,
    BondCurveResolutionFailedError,
    BondDiscountCurveNotFoundError,
    BondFixedNotFoundError,
    BondFloatingNotFoundError,
    BondIndexNotFoundError,
    BondProjectionCurveNotFoundError,
    BondQuoteResolutionFailedError,
    BondSnapshotNotFoundError,
)
from quantra_orchestrator.pricing.bonds.models import (
    AssembledFixedBondRequest,
    AssembledFloatingBondRequest,
    CurveRef,
    FixedBondPriceRequest,
    FixedBondPriceResponse,
    FixedBondResult,
    FixedBondTrade,
    FloatingBondPriceRequest,
    FloatingBondPriceResponse,
    FloatingBondResult,
    FloatingBondTrade,
    IndexRef,
    ResolvedCurve,
    ResolvedIndex,
    ResolvedQuoteValue,
)

__all__ = [
    "BOND_CURVE_RESOLUTION_FAILED_CODE",
    "BOND_DISCOUNT_CURVE_NOT_FOUND_CODE",
    "BOND_FIXED_NOT_FOUND_CODE",
    "BOND_FLOATING_NOT_FOUND_CODE",
    "BOND_INDEX_NOT_FOUND_CODE",
    "BOND_MISSING_TRADE_FIELDS_CODE",
    "BOND_PROJECTION_CURVE_NOT_FOUND_CODE",
    "BOND_QUOTE_RESOLUTION_FAILED_CODE",
    "BOND_SNAPSHOT_NOT_FOUND_CODE",
    "AssembledFixedBondRequest",
    "AssembledFloatingBondRequest",
    "BondCurveResolutionFailedError",
    "BondDiscountCurveNotFoundError",
    "BondFixedNotFoundError",
    "BondFloatingNotFoundError",
    "BondIndexNotFoundError",
    "BondProjectionCurveNotFoundError",
    "BondQuoteResolutionFailedError",
    "BondSnapshotNotFoundError",
    "CurveRef",
    "FixedBondPriceRequest",
    "FixedBondPriceResponse",
    "FixedBondResult",
    "FixedBondTrade",
    "FloatingBondPriceRequest",
    "FloatingBondPriceResponse",
    "FloatingBondResult",
    "FloatingBondTrade",
    "IndexRef",
    "ResolvedCurve",
    "ResolvedIndex",
    "ResolvedQuoteValue",
    "router",
]
