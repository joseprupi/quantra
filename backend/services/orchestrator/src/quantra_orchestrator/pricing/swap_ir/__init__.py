"""IR-swap pricing endpoint.

The first per-product endpoint in the redesign;
establishes the auth → assemble (``app_ro``) → MD resolve → batched fan-
out → engine → respond pattern that ``07``-``11`` replicate.

Sub-modules:

* :mod:`quantra_orchestrator.pricing.swap_ir.api` —
  ``POST /v1/price/swap/ir`` route + the lifespan-agnostic
  composition glue.
* :mod:`quantra_orchestrator.pricing.swap_ir.assembler` —
  ``app_ro``-side load of swap / curves / snapshot. Squashes the
  ref-vs-inline branches into a single canonical shape.
* :mod:`quantra_orchestrator.pricing.swap_ir.md_resolution` —
  product-local quote-ID resolution walker (snapshot pins first,
  live MD second). The dual-surface low-level forwarder's shared
  walker is a possible future engine forwarder.
* :mod:`quantra_orchestrator.pricing.swap_ir.engine_io` —
  ``build_swap_ir_request`` / ``decode_swap_ir_response`` /
  ``price_swap_ir_batch`` (the ``price_batch=`` translator).
* :mod:`quantra_orchestrator.pricing.swap_ir.errors` — codes
  ``swap_ir_*`` and the matching :class:`HTTPException` subclasses.
* :mod:`quantra_orchestrator.pricing.swap_ir.models` —
  Pydantic v2 request / response / intermediate models.
"""

from quantra_orchestrator.pricing.swap_ir.api import router
from quantra_orchestrator.pricing.swap_ir.errors import (
    SWAP_IR_CURVE_RESOLUTION_FAILED_CODE,
    SWAP_IR_CURVE_SET_NOT_FOUND_CODE,
    SWAP_IR_MISSING_TRADE_FIELDS_CODE,
    SWAP_IR_NOT_FOUND_CODE,
    SWAP_IR_QUOTE_RESOLUTION_FAILED_CODE,
    SWAP_IR_SNAPSHOT_NOT_FOUND_CODE,
    SwapIrCurveResolutionFailedError,
    SwapIrCurveSetNotFoundError,
    SwapIrNotFoundError,
    SwapIrQuoteResolutionFailedError,
    SwapIrSnapshotNotFoundError,
)
from quantra_orchestrator.pricing.swap_ir.models import (
    AssembledIrSwapRequest,
    CurveRef,
    IrSwapPriceRequest,
    IrSwapPriceResponse,
    IrSwapResult,
    IrSwapTrade,
    ResolvedCurve,
    ResolvedQuoteValue,
)

__all__ = [
    "SWAP_IR_CURVE_RESOLUTION_FAILED_CODE",
    "SWAP_IR_CURVE_SET_NOT_FOUND_CODE",
    "SWAP_IR_MISSING_TRADE_FIELDS_CODE",
    "SWAP_IR_NOT_FOUND_CODE",
    "SWAP_IR_QUOTE_RESOLUTION_FAILED_CODE",
    "SWAP_IR_SNAPSHOT_NOT_FOUND_CODE",
    "AssembledIrSwapRequest",
    "CurveRef",
    "IrSwapPriceRequest",
    "IrSwapPriceResponse",
    "IrSwapResult",
    "IrSwapTrade",
    "ResolvedCurve",
    "ResolvedQuoteValue",
    "SwapIrCurveResolutionFailedError",
    "SwapIrCurveSetNotFoundError",
    "SwapIrNotFoundError",
    "SwapIrQuoteResolutionFailedError",
    "SwapIrSnapshotNotFoundError",
    "router",
]
