"""Data-layer route registration.

One function per entity-family (reference data, products, immutable
history, singletons) keeps the wiring grouped by responsibility. Each
returns an ``APIRouter`` mounted under ``/v1/...`` by ``register_data``.

The routers themselves are built by ``quantra_orchestrator.data.routers``
from per-entity ``EntitySpec`` instances and Pydantic schemas; this
module is the place to add a new entity's URL prefix without touching
the generic builder.
"""

from __future__ import annotations

from fastapi import FastAPI

from quantra_orchestrator.data.routers import (
    build_crud_router,
    build_immutable_router,
)
from quantra_orchestrator.data.schemas import (
    CreditCurveCreate,
    CreditCurveResponse,
    CreditCurveUpdate,
    CurveCreate,
    CurveResponse,
    CurveSetCreate,
    CurveSetResponse,
    CurveSetUpdate,
    CurveUpdate,
    IndexCreate,
    IndexResponse,
    IndexUpdate,
    PricingHistoryResponse,
    ProductCreate,
    ProductResponse,
    ProductUpdate,
    SnapshotCreate,
    SnapshotResponse,
    SnapshotUpdate,
    SwaptionModelCreate,
    SwaptionModelResponse,
    SwaptionModelUpdate,
    VolSurfaceCreate,
    VolSurfaceResponse,
    VolSurfaceUpdate,
)
from quantra_orchestrator.data.specs import (
    BONDS_FIXED_SPEC,
    BONDS_FLOATING_SPEC,
    CDS_SPEC,
    CREDIT_CURVES_SPEC,
    CURVE_SETS_SPEC,
    CURVES_SPEC,
    EQUITY_OPTIONS_SPEC,
    INDICES_SPEC,
    PRICING_HISTORY_SPEC,
    SNAPSHOTS_SPEC,
    SWAPS_INFLATION_SPEC,
    SWAPS_IR_SPEC,
    SWAPTION_MODELS_SPEC,
    SWAPTIONS_SPEC,
    VOL_SURFACES_SPEC,
)


def register_data(app: FastAPI) -> None:
    """Mount every data-layer router on ``app``.

    The URL prefixes follow:

    * ``/v1/indices``, ``/v1/curves``, ``/v1/curve-sets``,
      ``/v1/credit-curves``, ``/v1/snapshots``,
      ``/v1/vol-surfaces``, ``/v1/swaption-models``.
    * Products: ``/v1/swaps/ir``, ``/v1/swaps/inflation``,
      ``/v1/swaptions``, ``/v1/bonds/fixed``, ``/v1/bonds/floating``,
      ``/v1/cds``, ``/v1/equity-options``.
    * Immutable view: ``/v1/pricing-history``.
    """

    # Reference data ---------------------------------------------------------
    app.include_router(
        build_crud_router(
            spec=INDICES_SPEC,
            prefix="/v1/indices",
            tag="data:indices",
            create_model=IndexCreate,
            update_model=IndexUpdate,
            response_model=IndexResponse,
        )
    )
    app.include_router(
        build_crud_router(
            spec=CURVES_SPEC,
            prefix="/v1/curves",
            tag="data:curves",
            create_model=CurveCreate,
            update_model=CurveUpdate,
            response_model=CurveResponse,
        )
    )
    app.include_router(
        build_crud_router(
            spec=CURVE_SETS_SPEC,
            prefix="/v1/curve-sets",
            tag="data:curve-sets",
            create_model=CurveSetCreate,
            update_model=CurveSetUpdate,
            response_model=CurveSetResponse,
        )
    )
    app.include_router(
        build_crud_router(
            spec=CREDIT_CURVES_SPEC,
            prefix="/v1/credit-curves",
            tag="data:credit-curves",
            create_model=CreditCurveCreate,
            update_model=CreditCurveUpdate,
            response_model=CreditCurveResponse,
        )
    )

    # User market data -------------------------------------------------------
    app.include_router(
        build_crud_router(
            spec=SNAPSHOTS_SPEC,
            prefix="/v1/snapshots",
            tag="data:snapshots",
            create_model=SnapshotCreate,
            update_model=SnapshotUpdate,
            response_model=SnapshotResponse,
        )
    )

    # Vol surfaces + short-rate models ---------------------------------------
    app.include_router(
        build_crud_router(
            spec=VOL_SURFACES_SPEC,
            prefix="/v1/vol-surfaces",
            tag="data:vol-surfaces",
            create_model=VolSurfaceCreate,
            update_model=VolSurfaceUpdate,
            response_model=VolSurfaceResponse,
        )
    )
    app.include_router(
        build_crud_router(
            spec=SWAPTION_MODELS_SPEC,
            prefix="/v1/swaption-models",
            tag="data:swaption-models",
            create_model=SwaptionModelCreate,
            update_model=SwaptionModelUpdate,
            response_model=SwaptionModelResponse,
        )
    )

    # Products ---------------------------------------------------------------
    app.include_router(
        build_crud_router(
            spec=SWAPS_IR_SPEC,
            prefix="/v1/swaps/ir",
            tag="data:swaps:ir",
            create_model=ProductCreate,
            update_model=ProductUpdate,
            response_model=ProductResponse,
        )
    )
    app.include_router(
        build_crud_router(
            spec=SWAPS_INFLATION_SPEC,
            prefix="/v1/swaps/inflation",
            tag="data:swaps:inflation",
            create_model=ProductCreate,
            update_model=ProductUpdate,
            response_model=ProductResponse,
        )
    )
    app.include_router(
        build_crud_router(
            spec=SWAPTIONS_SPEC,
            prefix="/v1/swaptions",
            tag="data:swaptions",
            create_model=ProductCreate,
            update_model=ProductUpdate,
            response_model=ProductResponse,
        )
    )
    app.include_router(
        build_crud_router(
            spec=BONDS_FIXED_SPEC,
            prefix="/v1/bonds/fixed",
            tag="data:bonds:fixed",
            create_model=ProductCreate,
            update_model=ProductUpdate,
            response_model=ProductResponse,
        )
    )
    app.include_router(
        build_crud_router(
            spec=BONDS_FLOATING_SPEC,
            prefix="/v1/bonds/floating",
            tag="data:bonds:floating",
            create_model=ProductCreate,
            update_model=ProductUpdate,
            response_model=ProductResponse,
        )
    )
    app.include_router(
        build_crud_router(
            spec=CDS_SPEC,
            prefix="/v1/cds",
            tag="data:cds",
            create_model=ProductCreate,
            update_model=ProductUpdate,
            response_model=ProductResponse,
        )
    )
    app.include_router(
        build_crud_router(
            spec=EQUITY_OPTIONS_SPEC,
            prefix="/v1/equity-options",
            tag="data:equity-options",
            create_model=ProductCreate,
            update_model=ProductUpdate,
            response_model=ProductResponse,
        )
    )

    # Immutable view ---------------------------------------------------------
    app.include_router(
        build_immutable_router(
            spec=PRICING_HISTORY_SPEC,
            prefix="/v1/pricing-history",
            tag="data:pricing-history",
            response_model=PricingHistoryResponse,
        )
    )


__all__ = ["register_data"]
