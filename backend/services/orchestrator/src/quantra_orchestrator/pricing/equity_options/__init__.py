"""Equity-option pricing endpoint.

the fifth per-product endpoint package in the
redesign. The first product to bundle three distinct market-data
families through the assembler in one pass:

* ``app.curves`` (the **discount** curve and the **dividend**-yield
  curve, both MD-resolved through the standard walker; the error-code convention
  refinement: same code, role pinned in ``details``).
* ``app.vol_surfaces`` (the equity Black-vol surface; ``kind``
  invariant ``BlackVolSpec``).
* The underlier **spot** quote — either an inline value or a
  canonical id resolved through the same MD client the curves
  flow through (equity options does NOT bypass MD).

Following the six-module split.

Sub-modules:

* :mod:`quantra_orchestrator.pricing.equity_options.api` —
  ``POST /v1/price/equity-option`` route and the lifespan-agnostic
  composition glue.
* :mod:`quantra_orchestrator.pricing.equity_options.assembler` —
  ``app_ro``-side load of equity option / curves / vol surface /
  snapshot + the ``BlackVolSpec`` kind invariant.
* :mod:`quantra_orchestrator.pricing.equity_options.md_resolution` —
  product-local quote-ID resolution walker over both curves, the
  vol surface body, and the optional spot canonical id.
* :mod:`quantra_orchestrator.pricing.equity_options.engine_io` —
  ``build_equity_option_request`` /
  ``decode_equity_option_response`` /
  ``price_equity_option_batch`` (the ``price_batch=``
  translator). RPC name is :attr:`EngineRpc.PRICE_EQUITY_OPTION`
  (canonical entry; the orchestrator's enum already
  names it ``PriceEquityOption`` so no D# mismatch needs to be
  proposed).
* :mod:`quantra_orchestrator.pricing.equity_options.errors` —
  error codes ``equity_option_*`` and the matching
  :class:`HTTPException` subclasses. Ten codes total: four 404s
  (option itself + vol surface + curve family + snapshot), one
  422 for the surface-kind invariant, three per-bundle-stage
  422s (curve + surface + spot — curves use one code with role
  in ``details`` refinement), one 422 for quote
  resolution, one 400 for shape past pydantic.
* :mod:`quantra_orchestrator.pricing.equity_options.models` —
  Pydantic v2 request / response / intermediate models. Shared
  ``CurveRef`` / ``ResolvedCurve`` / ``ResolvedQuoteValue``
  shapes are deliberately duplicated from swap_ir / swaption /
  bonds / cds — a shared module is a candidate future refactor.
"""

from quantra_orchestrator.pricing.equity_options.api import router
from quantra_orchestrator.pricing.equity_options.errors import (
    EQUITY_OPTION_CURVE_RESOLUTION_FAILED_CODE,
    EQUITY_OPTION_DISCOUNT_CURVE_NOT_FOUND_CODE,
    EQUITY_OPTION_INVALID_REQUEST_CODE,
    EQUITY_OPTION_NOT_FOUND_CODE,
    EQUITY_OPTION_QUOTE_RESOLUTION_FAILED_CODE,
    EQUITY_OPTION_SNAPSHOT_NOT_FOUND_CODE,
    EQUITY_OPTION_SPOT_RESOLUTION_FAILED_CODE,
    EQUITY_OPTION_SURFACE_RESOLUTION_FAILED_CODE,
    EQUITY_OPTION_VOL_SURFACE_NOT_FOUND_CODE,
    EQUITY_OPTION_VOL_SURFACE_WRONG_KIND_CODE,
    EquityOptionCurveResolutionFailedError,
    EquityOptionDiscountCurveNotFoundError,
    EquityOptionInvalidRequestError,
    EquityOptionNotFoundError,
    EquityOptionQuoteResolutionFailedError,
    EquityOptionSnapshotNotFoundError,
    EquityOptionSpotResolutionFailedError,
    EquityOptionSurfaceResolutionFailedError,
    EquityOptionVolSurfaceNotFoundError,
    EquityOptionVolSurfaceWrongKindError,
)
from quantra_orchestrator.pricing.equity_options.models import (
    AssembledEquityOptionRequest,
    CurveRef,
    EquityOptionPriceRequest,
    EquityOptionPriceResponse,
    EquityOptionResult,
    EquityOptionTrade,
    ResolvedCurve,
    ResolvedQuoteValue,
    ResolvedSpotQuote,
    ResolvedVolSurface,
    SpotQuoteRef,
    VolSurfaceRef,
)

__all__ = [
    "EQUITY_OPTION_CURVE_RESOLUTION_FAILED_CODE",
    "EQUITY_OPTION_DISCOUNT_CURVE_NOT_FOUND_CODE",
    "EQUITY_OPTION_INVALID_REQUEST_CODE",
    "EQUITY_OPTION_NOT_FOUND_CODE",
    "EQUITY_OPTION_QUOTE_RESOLUTION_FAILED_CODE",
    "EQUITY_OPTION_SNAPSHOT_NOT_FOUND_CODE",
    "EQUITY_OPTION_SPOT_RESOLUTION_FAILED_CODE",
    "EQUITY_OPTION_SURFACE_RESOLUTION_FAILED_CODE",
    "EQUITY_OPTION_VOL_SURFACE_NOT_FOUND_CODE",
    "EQUITY_OPTION_VOL_SURFACE_WRONG_KIND_CODE",
    "AssembledEquityOptionRequest",
    "CurveRef",
    "EquityOptionCurveResolutionFailedError",
    "EquityOptionDiscountCurveNotFoundError",
    "EquityOptionInvalidRequestError",
    "EquityOptionNotFoundError",
    "EquityOptionPriceRequest",
    "EquityOptionPriceResponse",
    "EquityOptionQuoteResolutionFailedError",
    "EquityOptionResult",
    "EquityOptionSnapshotNotFoundError",
    "EquityOptionSpotResolutionFailedError",
    "EquityOptionSurfaceResolutionFailedError",
    "EquityOptionTrade",
    "EquityOptionVolSurfaceNotFoundError",
    "EquityOptionVolSurfaceWrongKindError",
    "ResolvedCurve",
    "ResolvedQuoteValue",
    "ResolvedSpotQuote",
    "ResolvedVolSurface",
    "SpotQuoteRef",
    "VolSurfaceRef",
    "router",
]
