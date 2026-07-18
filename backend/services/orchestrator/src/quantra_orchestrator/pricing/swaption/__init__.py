"""Swaption pricing endpoint.

The second per-product endpoint in the redesign;
follows the six-module split established by ``06_swap_ir.md``.
Adds optionality on top of the IR-swap stack by introducing two
new ``shared_inputs``: vol surface (MD-resolved like curves) and
swaption model (pure config, not MD-resolved).

Sub-modules:

* :mod:`quantra_orchestrator.pricing.swaption.api` —
  ``POST /v1/price/swaption`` route + the lifespan-agnostic
  composition glue.
* :mod:`quantra_orchestrator.pricing.swaption.assembler` —
  ``app_ro``-side load of swaption / curves / vol surface /
  swaption model / snapshot. Five tables (vs. three for
  swap_ir).
* :mod:`quantra_orchestrator.pricing.swaption.md_resolution` —
  product-local quote-ID resolution walker. Traverses both
  curve point lists (same shape as swap_ir) and vol-surface
  payload bodies (``MatrixCell`` grids, ``quote_ids`` arrays,
  ``base.quote_id`` leaves).
* :mod:`quantra_orchestrator.pricing.swaption.engine_io` —
  ``build_swaption_request`` / ``decode_swaption_response`` /
  ``price_swaption_batch`` (the ``price_batch=`` translator).
* :mod:`quantra_orchestrator.pricing.swaption.errors` — codes
  ``swaption_*`` and the matching :class:`HTTPException`
  subclasses. Nine codes (vs. six for swap_ir; the extra three
  cover the new vol-surface / swaption-model loads + a distinct
  surface-resolution failure code).
* :mod:`quantra_orchestrator.pricing.swaption.models` —
  Pydantic v2 request / response / intermediate models.
"""

from quantra_orchestrator.pricing.swaption.api import router
from quantra_orchestrator.pricing.swaption.errors import (
    SWAPTION_CURVE_RESOLUTION_FAILED_CODE,
    SWAPTION_CURVE_SET_NOT_FOUND_CODE,
    SWAPTION_INVALID_REQUEST_CODE,
    SWAPTION_MISSING_TRADE_FIELDS_CODE,
    SWAPTION_MODEL_NOT_FOUND_CODE,
    SWAPTION_NOT_FOUND_CODE,
    SWAPTION_QUOTE_RESOLUTION_FAILED_CODE,
    SWAPTION_SNAPSHOT_NOT_FOUND_CODE,
    SWAPTION_SURFACE_RESOLUTION_FAILED_CODE,
    SWAPTION_VOL_SURFACE_NOT_FOUND_CODE,
    SwaptionCurveResolutionFailedError,
    SwaptionCurveSetNotFoundError,
    SwaptionInvalidRequestError,
    SwaptionModelNotFoundError,
    SwaptionNotFoundError,
    SwaptionQuoteResolutionFailedError,
    SwaptionSnapshotNotFoundError,
    SwaptionSurfaceResolutionFailedError,
    SwaptionVolSurfaceNotFoundError,
)
from quantra_orchestrator.pricing.swaption.models import (
    AssembledSwaptionRequest,
    CurveRef,
    ResolvedCurve,
    ResolvedQuoteValue,
    ResolvedSwaptionModel,
    ResolvedVolSurface,
    SwaptionModelRef,
    SwaptionPriceRequest,
    SwaptionPriceResponse,
    SwaptionResult,
    SwaptionTrade,
    VolSurfaceRef,
)

__all__ = [
    "SWAPTION_CURVE_RESOLUTION_FAILED_CODE",
    "SWAPTION_CURVE_SET_NOT_FOUND_CODE",
    "SWAPTION_INVALID_REQUEST_CODE",
    "SWAPTION_MISSING_TRADE_FIELDS_CODE",
    "SWAPTION_MODEL_NOT_FOUND_CODE",
    "SWAPTION_NOT_FOUND_CODE",
    "SWAPTION_QUOTE_RESOLUTION_FAILED_CODE",
    "SWAPTION_SNAPSHOT_NOT_FOUND_CODE",
    "SWAPTION_SURFACE_RESOLUTION_FAILED_CODE",
    "SWAPTION_VOL_SURFACE_NOT_FOUND_CODE",
    "AssembledSwaptionRequest",
    "CurveRef",
    "ResolvedCurve",
    "ResolvedQuoteValue",
    "ResolvedSwaptionModel",
    "ResolvedVolSurface",
    "SwaptionCurveResolutionFailedError",
    "SwaptionCurveSetNotFoundError",
    "SwaptionInvalidRequestError",
    "SwaptionModelNotFoundError",
    "SwaptionModelRef",
    "SwaptionNotFoundError",
    "SwaptionPriceRequest",
    "SwaptionPriceResponse",
    "SwaptionQuoteResolutionFailedError",
    "SwaptionResult",
    "SwaptionSnapshotNotFoundError",
    "SwaptionSurfaceResolutionFailedError",
    "SwaptionTrade",
    "SwaptionVolSurfaceNotFoundError",
    "VolSurfaceRef",
    "router",
]
