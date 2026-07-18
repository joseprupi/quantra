"""Inflation-swap pricing endpoint.

the sixth per-product endpoint package in the
redesign. First product to bundle a **nominal discount curve**, an
**inflation curve** (rates-shaped — flows through the MD
walker, does NOT bypass), and an **inflation index** (CPI-style
fixings supplied inline) through the assembler in one pass:

* ``app.curves`` — both the nominal discount curve and the
  inflation index curve. Same family, different roles. The
  refinement applies: a single 422 code names the bundle TYPE
  (``swap_inflation_curve_resolution_failed``); ``details``
  pins the role (``"nominal"`` or ``"inflation"``).
* ``app.indices`` — the inflation index spec referenced by both
  the inflation curve and the swap trade body. Loaded once,
  copied verbatim into the engine wire payload (the index is
  not MD-resolved — its ``fixings`` are historical CPI values,
  not vendor quotes).

Following the six-module split.

Sub-modules:

* :mod:`quantra_orchestrator.pricing.swaps_inflation.api` —
  ``POST /v1/price/swaps/inflation`` route and the lifespan-
  agnostic composition glue.
* :mod:`quantra_orchestrator.pricing.swaps_inflation.assembler`
  — ``app_ro``-side load of swap / nominal-curve /
  inflation-curve / inflation-index / snapshot.
* :mod:`quantra_orchestrator.pricing.swaps_inflation.md_resolution`
  — product-local quote-ID walker over BOTH curves in one
  batched ``MdClient.resolve_quotes`` call.
* :mod:`quantra_orchestrator.pricing.swaps_inflation.engine_io`
  — ``build_inflation_swap_request`` /
  ``decode_inflation_swap_response`` /
  ``price_inflation_swap_batch``. Dispatches to either
  :attr:`EngineRpc.PRICE_ZERO_COUPON_INFLATION_SWAP` or
  :attr:`EngineRpc.PRICE_YEAR_ON_YEAR_INFLATION_SWAP` based on
  the trade body's ``swap_kind`` discriminator (settled —
  the plan text says ``PRICE_SWAP_INFLATION`` but the canonical
  enum has two RPCs, one per swap kind; same shape).
* :mod:`quantra_orchestrator.pricing.swaps_inflation.errors` —
  error codes ``swap_inflation_*`` and the matching
  :class:`HTTPException` subclasses. Eight codes total: four
  404s (swap + nominal curve + inflation curve + inflation
  index — snapshot reuses the curve walker pattern via a
  shared 404), two 422s for the per-bundle-stage curve / index
  resolution failures (curves use ONE code with role in
  ``details`` refinement; inflation index has its own
  code), one 422 for quote resolution, and one 400 for shape
  past pydantic.
* :mod:`quantra_orchestrator.pricing.swaps_inflation.models` —
  Pydantic v2 request / response / intermediate models. Shared
  ``CurveRef`` / ``ResolvedCurve`` / ``ResolvedQuoteValue``
  shapes are deliberately duplicated from the other per-product
  packages — a shared ``pricing/_resolved/`` factor-out is a
  candidate future refactor.
"""

from quantra_orchestrator.pricing.swaps_inflation.api import router
from quantra_orchestrator.pricing.swaps_inflation.errors import (
    SWAP_INFLATION_CURVE_RESOLUTION_FAILED_CODE,
    SWAP_INFLATION_INDEX_NOT_FOUND_CODE,
    SWAP_INFLATION_INDEX_RESOLUTION_FAILED_CODE,
    SWAP_INFLATION_INFLATION_CURVE_NOT_FOUND_CODE,
    SWAP_INFLATION_INVALID_REQUEST_CODE,
    SWAP_INFLATION_MISSING_TRADE_FIELDS_CODE,
    SWAP_INFLATION_NOMINAL_CURVE_NOT_FOUND_CODE,
    SWAP_INFLATION_NOT_FOUND_CODE,
    SWAP_INFLATION_QUOTE_RESOLUTION_FAILED_CODE,
    SWAP_INFLATION_SNAPSHOT_NOT_FOUND_CODE,
    SwapInflationCurveResolutionFailedError,
    SwapInflationIndexNotFoundError,
    SwapInflationIndexResolutionFailedError,
    SwapInflationInflationCurveNotFoundError,
    SwapInflationInvalidRequestError,
    SwapInflationNominalCurveNotFoundError,
    SwapInflationNotFoundError,
    SwapInflationQuoteResolutionFailedError,
    SwapInflationSnapshotNotFoundError,
)
from quantra_orchestrator.pricing.swaps_inflation.models import (
    AssembledInflationSwapRequest,
    CurveRef,
    InflationIndexRef,
    InflationSwapPriceRequest,
    InflationSwapPriceResponse,
    InflationSwapResult,
    InflationSwapTrade,
    ResolvedCurve,
    ResolvedInflationIndex,
    ResolvedQuoteValue,
)

__all__ = [
    "SWAP_INFLATION_CURVE_RESOLUTION_FAILED_CODE",
    "SWAP_INFLATION_INDEX_NOT_FOUND_CODE",
    "SWAP_INFLATION_INDEX_RESOLUTION_FAILED_CODE",
    "SWAP_INFLATION_INFLATION_CURVE_NOT_FOUND_CODE",
    "SWAP_INFLATION_INVALID_REQUEST_CODE",
    "SWAP_INFLATION_MISSING_TRADE_FIELDS_CODE",
    "SWAP_INFLATION_NOMINAL_CURVE_NOT_FOUND_CODE",
    "SWAP_INFLATION_NOT_FOUND_CODE",
    "SWAP_INFLATION_QUOTE_RESOLUTION_FAILED_CODE",
    "SWAP_INFLATION_SNAPSHOT_NOT_FOUND_CODE",
    "AssembledInflationSwapRequest",
    "CurveRef",
    "InflationIndexRef",
    "InflationSwapPriceRequest",
    "InflationSwapPriceResponse",
    "InflationSwapResult",
    "InflationSwapTrade",
    "ResolvedCurve",
    "ResolvedInflationIndex",
    "ResolvedQuoteValue",
    "SwapInflationCurveResolutionFailedError",
    "SwapInflationIndexNotFoundError",
    "SwapInflationIndexResolutionFailedError",
    "SwapInflationInflationCurveNotFoundError",
    "SwapInflationInvalidRequestError",
    "SwapInflationNominalCurveNotFoundError",
    "SwapInflationNotFoundError",
    "SwapInflationQuoteResolutionFailedError",
    "SwapInflationSnapshotNotFoundError",
    "router",
]
