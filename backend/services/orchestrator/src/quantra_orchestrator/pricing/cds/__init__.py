"""CDS pricing endpoint.

the fourth per-product endpoint package in the
redesign. The first product to exercise the ``credit_curves`` entity
 and to ship **two distinct entity families** through the
assembler in one pass: ``app.curves`` (the discount curve, MD-
resolved through the standard walker) and ``app.credit_curves`` (the
credit curve, which carries its own hazard/probability inputs and
does NOT consult MD by design).

Following the six-module split.

Sub-modules:

* :mod:`quantra_orchestrator.pricing.cds.api` —
  ``POST /v1/price/cds`` route and the lifespan-agnostic
  composition glue.
* :mod:`quantra_orchestrator.pricing.cds.assembler` —
  ``app_ro``-side load of CDS / discount curve / credit curve /
  snapshot. The first product assembler to walk two distinct
  entity families.
* :mod:`quantra_orchestrator.pricing.cds.md_resolution` —
  product-local quote-ID resolution walker. **Discount-curve
  only** by design — credit curves never reach this module.
* :mod:`quantra_orchestrator.pricing.cds.engine_io` —
  ``build_cds_request`` / ``decode_cds_response`` /
  ``price_cds_batch`` (the ``price_batch=`` translator).
  RPC name is :attr:`EngineRpc.PRICE_CDS` (the canonical enum
  entry; it already names the engine RPC ``PriceCDS``).
* :mod:`quantra_orchestrator.pricing.cds.errors` — codes
  ``cds_*`` and the matching :class:`HTTPException` subclasses:
  404s (CDS itself + credit curve + discount curve + snapshot),
  per-bundle-stage 422s (credit-curve resolution is a **distinct
  code** from discount-curve resolution because the entity
  families are distinct), quote-resolution and missing-trade-field
  422s.
* :mod:`quantra_orchestrator.pricing.cds.models` —
  Pydantic v2 request / response / intermediate models. Shared
  ``CurveRef`` / ``ResolvedCurve`` / ``ResolvedQuoteValue`` shapes
  are deliberately duplicated from swap_ir / swaption / bonds —
  a shared module is a candidate future refactor once the shapes
  stop moving.
"""

from quantra_orchestrator.pricing.cds.api import router
from quantra_orchestrator.pricing.cds.errors import (
    CDS_CREDIT_CURVE_NOT_FOUND_CODE,
    CDS_CREDIT_CURVE_RESOLUTION_FAILED_CODE,
    CDS_CURVE_RESOLUTION_FAILED_CODE,
    CDS_DISCOUNT_CURVE_NOT_FOUND_CODE,
    CDS_MISSING_TRADE_FIELDS_CODE,
    CDS_NOT_FOUND_CODE,
    CDS_QUOTE_RESOLUTION_FAILED_CODE,
    CDS_SNAPSHOT_NOT_FOUND_CODE,
    CdsCreditCurveNotFoundError,
    CdsCreditCurveResolutionFailedError,
    CdsCurveResolutionFailedError,
    CdsDiscountCurveNotFoundError,
    CdsNotFoundError,
    CdsQuoteResolutionFailedError,
    CdsSnapshotNotFoundError,
)
from quantra_orchestrator.pricing.cds.models import (
    AssembledCdsRequest,
    CdsPriceRequest,
    CdsPriceResponse,
    CdsResult,
    CdsTrade,
    CreditCurveRef,
    CurveRef,
    ResolvedCreditCurve,
    ResolvedCurve,
    ResolvedQuoteValue,
)

__all__ = [
    "CDS_CREDIT_CURVE_NOT_FOUND_CODE",
    "CDS_CREDIT_CURVE_RESOLUTION_FAILED_CODE",
    "CDS_CURVE_RESOLUTION_FAILED_CODE",
    "CDS_DISCOUNT_CURVE_NOT_FOUND_CODE",
    "CDS_MISSING_TRADE_FIELDS_CODE",
    "CDS_NOT_FOUND_CODE",
    "CDS_QUOTE_RESOLUTION_FAILED_CODE",
    "CDS_SNAPSHOT_NOT_FOUND_CODE",
    "AssembledCdsRequest",
    "CdsCreditCurveNotFoundError",
    "CdsCreditCurveResolutionFailedError",
    "CdsCurveResolutionFailedError",
    "CdsDiscountCurveNotFoundError",
    "CdsNotFoundError",
    "CdsPriceRequest",
    "CdsPriceResponse",
    "CdsQuoteResolutionFailedError",
    "CdsResult",
    "CdsSnapshotNotFoundError",
    "CdsTrade",
    "CreditCurveRef",
    "CurveRef",
    "ResolvedCreditCurve",
    "ResolvedCurve",
    "ResolvedQuoteValue",
    "router",
]
