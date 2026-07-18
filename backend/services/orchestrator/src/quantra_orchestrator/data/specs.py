"""``EntitySpec`` instances + URL-path metadata for every ``app.*`` entity.

Centralising the specs here keeps the layout authority in one place;
per-entity routers in ``quantra_orchestrator.api.data`` only need to
import the spec + Pydantic schemas and call the router factory.

The spec set captured here matches the migration layout:

* Named entities with the standard template (soft delete, partial
  unique on ``(owner_uid, name)``):
    indices, curves, curve_sets, credit_curves, snapshots,
    vol_surfaces, swaption_models,
    swaps_ir, swaps_inflation, swaptions, bonds_fixed, bonds_floating,
    cds, equity_options.
* Immutable view:
    pricing_history.
"""

from __future__ import annotations

from quantra_orchestrator.data.repository import EntitySpec

# ---------------------------------------------------------------------------
# Reference data (0003_curves_and_indices)
# ---------------------------------------------------------------------------

INDICES_SPEC: EntitySpec = EntitySpec(
    entity="index",
    table="indices",
    scalar_columns=("name", "kind", "currency", "calendar", "day_counter"),
    jsonb_columns=("body",),
)

CURVES_SPEC: EntitySpec = EntitySpec(
    entity="curve",
    table="curves",
    scalar_columns=(
        "name",
        "currency",
        "day_counter",
        "helper_kind",
        "reference_date",
    ),
    jsonb_columns=("points", "body"),
)

CURVE_SETS_SPEC: EntitySpec = EntitySpec(
    entity="curve_set",
    table="curve_sets",
    scalar_columns=("name", "currency"),
    jsonb_columns=("body",),
)

CREDIT_CURVES_SPEC: EntitySpec = EntitySpec(
    entity="credit_curve",
    table="credit_curves",
    scalar_columns=(
        "name",
        "reference_entity",
        "currency",
        "seniority",
        "source",
        "recovery_rate",
    ),
    jsonb_columns=("body",),
)

# ---------------------------------------------------------------------------
# User-owned market data (0004_snapshots_and_quote_book)
# ---------------------------------------------------------------------------

SNAPSHOTS_SPEC: EntitySpec = EntitySpec(
    entity="snapshot",
    table="snapshots",
    scalar_columns=("name", "as_of", "description"),
    jsonb_columns=("content",),
)

# The legacy `app.quote_book` singleton table still exists in the
# migration history but is no longer exposed over the API (the feature
# was write-only; nothing read it back). `quotes_saved` (static
# single-value quotes, `/v1/quotes`) was dropped earlier — see migration
# 0012_drop_quotes_saved.

# ---------------------------------------------------------------------------
# Vol surfaces + short-rate models (0005_vol_surfaces_and_models)
# ---------------------------------------------------------------------------

VOL_SURFACES_SPEC: EntitySpec = EntitySpec(
    entity="vol_surface",
    table="vol_surfaces",
    scalar_columns=("name", "kind"),
    jsonb_columns=("payload",),
)

SWAPTION_MODELS_SPEC: EntitySpec = EntitySpec(
    entity="swaption_model",
    table="swaption_models",
    scalar_columns=("name", "kind"),
    jsonb_columns=("payload",),
)

# ---------------------------------------------------------------------------
# Product tables (0006_products)
# ---------------------------------------------------------------------------


def _product_spec(entity: str, table: str) -> EntitySpec:
    """All seven product tables share the (name, request) layout.

    They also carry portal "Save" wrapper rows alongside their
    by-reference save-graph rows in the same table, so they opt into the
    wrapper→graph delete cascade (``wrapper_graph_cascade``).
    """

    return EntitySpec(
        entity=entity,
        table=table,
        scalar_columns=("name",),
        jsonb_columns=("request",),
        wrapper_graph_cascade=True,
    )


SWAPS_IR_SPEC: EntitySpec = _product_spec("swap_ir", "swaps_ir")
SWAPS_INFLATION_SPEC: EntitySpec = _product_spec("swap_inflation", "swaps_inflation")
SWAPTIONS_SPEC: EntitySpec = _product_spec("swaption", "swaptions")
BONDS_FIXED_SPEC: EntitySpec = _product_spec("bond_fixed", "bonds_fixed")
BONDS_FLOATING_SPEC: EntitySpec = _product_spec("bond_floating", "bonds_floating")
CDS_SPEC: EntitySpec = _product_spec("cds", "cds")
EQUITY_OPTIONS_SPEC: EntitySpec = _product_spec("equity_option", "equity_options")

# ---------------------------------------------------------------------------
# Pricing history (0007 — immutable view)
# ---------------------------------------------------------------------------

PRICING_HISTORY_SPEC: EntitySpec = EntitySpec(
    entity="pricing_history",
    table="pricing_history",
    scalar_columns=(
        "product_kind",
        "product_id",
        "as_of",
        "trade_entity_type",
        "trade_entity_id",
        "trade_version",
    ),
    jsonb_columns=("request", "response"),
    has_soft_delete=False,
    has_updated_at=False,
    name_column=None,
    unique_constraint=None,
    # Immutable by construction (append-only writes via the pricing
    # flow); it IS the other audit surface, not a versioned entity.
    versioned=False,
)


# ---------------------------------------------------------------------------
# Aggregation — useful for tests / sanity checks
# ---------------------------------------------------------------------------

NAMED_ENTITY_SPECS: tuple[EntitySpec, ...] = (
    INDICES_SPEC,
    CURVES_SPEC,
    CURVE_SETS_SPEC,
    CREDIT_CURVES_SPEC,
    SNAPSHOTS_SPEC,
    VOL_SURFACES_SPEC,
    SWAPTION_MODELS_SPEC,
    SWAPS_IR_SPEC,
    SWAPS_INFLATION_SPEC,
    SWAPTIONS_SPEC,
    BONDS_FIXED_SPEC,
    BONDS_FLOATING_SPEC,
    CDS_SPEC,
    EQUITY_OPTIONS_SPEC,
)
"""All entities that follow the standard named-entity template (soft delete + partial unique)."""


__all__ = [
    "BONDS_FIXED_SPEC",
    "BONDS_FLOATING_SPEC",
    "CDS_SPEC",
    "CREDIT_CURVES_SPEC",
    "CURVES_SPEC",
    "CURVE_SETS_SPEC",
    "EQUITY_OPTIONS_SPEC",
    "INDICES_SPEC",
    "NAMED_ENTITY_SPECS",
    "PRICING_HISTORY_SPEC",
    "SNAPSHOTS_SPEC",
    "SWAPS_INFLATION_SPEC",
    "SWAPS_IR_SPEC",
    "SWAPTIONS_SPEC",
    "SWAPTION_MODELS_SPEC",
    "VOL_SURFACES_SPEC",
]
