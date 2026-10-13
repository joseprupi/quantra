"""Pydantic v2 request / response models for ``POST /v1/price/fx-forward``.

Mirrors the swap_ir layering (request envelope → resolved intermediates →
response envelope). The curve / quote intermediates (:class:`CurveRef`,
:class:`ResolvedCurve`, :class:`ResolvedQuoteValue`) are reused from swap_ir
verbatim (same shape, same translator contract); this module adds the FX
spot ref/resolved pair, the index ref/resolved pair and the trade + result.

Trade body (inline ``fx_forward`` or the saved ``app.fx_forwards.request``):

* ``base_currency`` / ``quote_currency`` — ISO codes; must equal the spot's
  pair in the same order (the engine never inverts a spot).
* ``direction`` — ``"BuyBase"`` | ``"SellBase"`` (the party whose NPV is
  reported).
* ``base_notional`` (alias ``notional``) — base-currency units, ``> 0``.
* ``forward_rate`` — contract rate, quote per base, ``> 0``.
* ``maturity_date`` — ``YYYY-MM-DD`` delivery date (the engine adjusts it on
  the spot's joint calendar / convention and reports the adjusted date).
* ``fx_spot`` — the resolved spot's id (``app.fx_spots`` UUID as text, or the
  inline name). Optional when the request resolves exactly one spot.
* ``base_discounting_curve`` / ``quote_discounting_curve`` — the resolved
  curve ids (``app.curves`` UUID as text, or the inline name) discounting the
  base / quote cash flow. Required.
* ``reporting_currency`` — ``base_currency`` or ``quote_currency``; defaults
  to the quote currency.
* ``pricing`` (saved rows only) — ``{curves: [...], fx_spots: [...],
  indices: [...]}`` ref-or-inline lists, overridable per request.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from quantra_orchestrator.pricing.swap_ir.models import (
    CurveRef,
    ResolvedCurve,
    ResolvedIndex,
    ResolvedQuoteValue,
)


class FxSpotRef(BaseModel):
    """An ``app.fx_spots`` reference *or* a fully inline FX spot definition."""

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = Field(
        default=None,
        description="Soft reference into ``app.fx_spots``; inline fields ignored when set.",
    )
    name: str | None = None
    base_currency: str | None = None
    quote_currency: str | None = None
    rate: float | None = None
    quote_id: str | None = None
    spot_days: int | None = None
    settlement_calendars: list[str] | None = None
    business_day_convention: str | None = None

    @model_validator(mode="after")
    def _validate_branches(self) -> Self:
        if self.id is None and (self.base_currency is None or self.quote_currency is None):
            msg = (
                "FxSpotRef must provide either ``id`` (reference into app.fx_spots) or "
                "an inline definition (``base_currency`` + ``quote_currency`` + "
                "``rate``|``quote_id`` + ``spot_days`` + ``settlement_calendars`` + "
                "``business_day_convention``)."
            )
            raise ValueError(msg)
        return self


class ResolvedFxSpot(BaseModel):
    """An FX spot after ref/inline collapsing (the translator's ``FxSpotSpec`` input)."""

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    name: str = Field(min_length=1)
    base_currency: str
    quote_currency: str
    rate: float | None = None
    quote_id: str | None = None
    spot_days: int = 0
    settlement_calendars: list[str] = Field(default_factory=list)
    business_day_convention: str = ""


class IndexRef(BaseModel):
    """An ``app.indices`` reference *or* an inline index (same body as ``app.indices``)."""

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    name: str | None = None
    kind: str | None = None
    currency: str | None = None
    calendar: str | None = None
    day_counter: str | None = None
    body: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _validate_branches(self) -> Self:
        if self.id is None and (self.body is None or self.kind is None or self.name is None):
            msg = (
                "IndexRef must provide either ``id`` (reference into app.indices) or an "
                "inline definition (``name`` + ``kind`` + ``body``)."
            )
            raise ValueError(msg)
        return self


class FxForwardTrade(BaseModel):
    """One FX forward flowing through the concurrency seam."""

    model_config = ConfigDict(extra="forbid")

    fx_forward_id: UUID | None = None
    name: str | None = None
    fx_forward: dict[str, Any] = Field(default_factory=dict)


class AssembledFxForwardRequest(BaseModel):
    """The fully resolved request echoed on every response (and in error ``details``)."""

    model_config = ConfigDict(extra="forbid")

    as_of: date
    snapshot_id: UUID | None = None
    trade: FxForwardTrade
    curves: list[ResolvedCurve] = Field(default_factory=list)
    fx_spots: list[ResolvedFxSpot] = Field(default_factory=list)
    indices: list[ResolvedIndex] = Field(default_factory=list)
    resolved_quotes: list[ResolvedQuoteValue] = Field(default_factory=list)


class FxForwardResult(BaseModel):
    """Engine ``FxForwardResponse`` decoded for one trade.

    Every numeric field is a computed value (the engine reports ``0.0`` only
    as a genuine zero — ``docs/http-api.md`` § "FX forward" / Response); the
    two dates are the engine-resolved spot date and adjusted maturity.
    ``reporting_currency`` labels ``npv`` (the request's choice, echoed).
    """

    model_config = ConfigDict(extra="forbid")

    npv: float
    reporting_currency: str
    fair_forward_rate: float
    forward_points: float
    base_leg_pv: float
    quote_leg_pv: float
    fx_spot_used: float
    spot_date: str | None = None
    maturity_date: str | None = None


class FxForwardPriceRequest(BaseModel):
    """Endpoint input: by reference (``fx_forward_id``) or fully inline (``fx_forward``)."""

    model_config = ConfigDict(extra="forbid")

    fx_forward_id: UUID | None = Field(
        default=None, description="ID of an ``app.fx_forwards`` row owned by the caller."
    )
    fx_forward: dict[str, Any] | None = Field(
        default=None,
        description="Inline trade body (see the module docstring for the keys).",
    )
    curves: list[CurveRef] | None = Field(
        default=None,
        description=(
            "Curves (ref into ``app.curves`` or inline). Required inline; overrides "
            "the saved ``pricing.curves`` by reference."
        ),
    )
    fx_spots: list[FxSpotRef] | None = Field(
        default=None,
        description=(
            "FX spots (ref into ``app.fx_spots`` or inline). Required inline; overrides "
            "the saved ``pricing.fx_spots`` by reference."
        ),
    )
    indices: list[IndexRef] | None = Field(
        default=None,
        description=(
            "Indices the curve helpers reference (ref into ``app.indices`` or inline). "
            "Registered verbatim in the engine request; optional (known overnight / "
            "IBOR ids resolve from the catalog)."
        ),
    )
    as_of: date
    snapshot_id: UUID | None = None

    @model_validator(mode="after")
    def _validate_branches(self) -> Self:
        if self.fx_forward_id is None and self.fx_forward is None:
            msg = "Either ``fx_forward_id`` (by reference) or ``fx_forward`` (inline) must be set."
            raise ValueError(msg)
        if self.fx_forward_id is not None and self.fx_forward is not None:
            msg = "``fx_forward_id`` and ``fx_forward`` are mutually exclusive; pick one."
            raise ValueError(msg)
        if self.fx_forward is not None:
            if not self.curves:
                msg = "Inline mode (``fx_forward`` set) requires a non-empty ``curves`` list."
                raise ValueError(msg)
            if not self.fx_spots:
                msg = "Inline mode (``fx_forward`` set) requires a non-empty ``fx_spots`` list."
                raise ValueError(msg)
        return self


class FxForwardPriceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pricing_history_id: str | None = None
    assembled_request: AssembledFxForwardRequest
    result: FxForwardResult


__all__ = [
    "AssembledFxForwardRequest",
    "CurveRef",
    "FxForwardPriceRequest",
    "FxForwardPriceResponse",
    "FxForwardResult",
    "FxForwardTrade",
    "FxSpotRef",
    "IndexRef",
    "ResolvedCurve",
    "ResolvedFxSpot",
    "ResolvedIndex",
    "ResolvedQuoteValue",
]
