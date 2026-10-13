"""FX-forward pricing endpoint (``POST /v1/price/fx-forward``, engine >= 0.9.0).

Same vertical as swap_ir: auth → assemble (``app_ro``: trade / curves / FX
spots / indices / snapshot) → MD resolve (curve quotes + FX spot quotes) →
batched engine call (``PriceFxForward``) → decode → history + trace.
"""

from quantra_orchestrator.pricing.fx_forward.api import router
from quantra_orchestrator.pricing.fx_forward.models import (
    FxForwardPriceRequest,
    FxForwardPriceResponse,
    FxForwardResult,
)

__all__ = ["FxForwardPriceRequest", "FxForwardPriceResponse", "FxForwardResult", "router"]
