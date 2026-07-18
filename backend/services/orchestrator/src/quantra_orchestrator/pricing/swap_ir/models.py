"""Pydantic v2 request / response models for ``POST /v1/price/swap/ir``.

Three layers:

1. **Request envelope.** :class:`IrSwapPriceRequest` accepts either a
   ``swap_id`` reference (load from ``app.swaps_ir``) or an inline
   ``swap`` + ``curves`` payload (price without persisting). Both
   carry the mandatory ``as_of`` and the optional ``snapshot_id``
   pin.
2. **Intermediate / engine-facing.** :class:`CurveRef` is the
   reference-or-inline shape; :class:`ResolvedCurve` is the
   shape after the assembler has loaded any ``app.curves`` rows
   and squashed both branches into a single canonical form.
   :class:`ResolvedQuoteValue` is one MD point resolved server-side
   per the invariant (the engine never sees quote IDs).
   :class:`IrSwapTrade` is the ``T`` parameter that flows through
   the concurrency seam.
3. **Response envelope.** :class:`AssembledIrSwapRequest` carries
   the full resolved request the orchestrator would hand the
   engine; :class:`IrSwapResult` is the engine's decoded response
   for one trade; :class:`IrSwapPriceResponse` is what the route
   returns on success (today's stub backend never reaches this
   path — it raises ``NotImplementedError`` before any decode runs;
   the response shape is reified now so the wave-two real-engine
   wiring is a body-of-the-route change, not a schema change).

The product subplan deliberately keeps inner ``dict[str, Any]``
fields loose for the saved-swap body and inline curve bodies: the
``app.swaps_ir.request`` JSONB has no tightened schema (the
inner shape evolves per product). a frontend cutover may
tighten these once the portal's wire shape settles.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CurveRef(BaseModel):
    """A curve reference *or* a fully inline curve definition.

    Either ``id`` points at an ``app.curves`` row owned by the caller
    (the orchestrator loads + projects it) or the inline fields
    (``points`` at minimum) carry a full definition the caller built
    client-side. Both shapes coexist because the portal sometimes
    saves curves and sometimes constructs them on the fly.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = Field(
        default=None,
        description=(
            "Soft reference into ``app.curves``. When set, the "
            "orchestrator loads the row from the database and "
            "ignores any inline fields below."
        ),
    )
    name: str | None = None
    currency: str | None = None
    day_counter: str | None = None
    helper_kind: str | None = None
    reference_date: date | None = None
    points: list[dict[str, Any]] | None = None
    body: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _validate_branches(self) -> Self:
        if self.id is None and (self.points is None or len(self.points) == 0):
            msg = (
                "CurveRef must provide either ``id`` (reference into "
                "app.curves) or a non-empty ``points`` list (inline "
                "definition)."
            )
            raise ValueError(msg)
        return self


class ResolvedCurve(BaseModel):
    """A curve after the assembler has loaded / collapsed the ref-vs-inline branches.

    The orchestrator never forwards a raw :class:`CurveRef` to the
    rest of the pipeline; the assembler squashes refs into rows
    pulled from ``app.curves`` and inline payloads into the same
    canonical shape. Quote-ID substitution happens downstream in
    :mod:`quantra_orchestrator.pricing.swap_ir.md_resolution`.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = Field(
        default=None,
        description=(
            "The originating ``app.curves`` row when the curve came from a "
            "ref. ``None`` for inline-only definitions."
        ),
    )
    name: str = Field(min_length=1)
    currency: str | None = None
    day_counter: str | None = None
    helper_kind: str | None = None
    reference_date: date | None = None
    points: list[dict[str, Any]] = Field(default_factory=list)
    body: dict[str, Any] = Field(default_factory=dict)


class ResolvedIndex(BaseModel):
    """The swap's float-leg index after ref/inline collapsing.

    An internal intermediate (not a request/response model): the assembler
    projects the inline forwarding-index body the saved-swap pricing block
    carries into this shape so the translator can build a faithful ``IndexDef``
    , replacing the interim hardcoded Euribor-6M placeholder. Not
    MD-resolved — the body is pure config (tenor / fixing days / calendar /
    day counter). Mirrors the bonds-side shape (duplication intentional + temporary,
    per the product plan; a shared ``pricing/_resolved`` lands later).
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    name: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    currency: str | None = None
    calendar: str | None = None
    day_counter: str | None = None
    body: dict[str, Any] = Field(default_factory=dict)


class ResolvedQuoteValue(BaseModel):
    """One quote ID resolved to its value at the request's ``as_of``.

    ``source`` carries the upstream tag (vendor / snapshot) so the
    pricing-history hook can persist a
    full provenance record. ``from_snapshot`` discriminates the two
    resolution paths: True when the value came from a user-pinned
    ``app.snapshots`` row, False when it came from the live MD
    service.
    """

    model_config = ConfigDict(extra="forbid")

    canonical_id: str = Field(min_length=1)
    as_of: date | datetime
    value: float
    source: str | None = None
    from_snapshot: bool = Field(
        default=False,
        description=(
            "True when the value came from an ``app.snapshots`` pin "
            "(reproducible across reruns); False when it came from a "
            "live MD service call (subject to upstream corrections)."
        ),
    )


class IrSwapTrade(BaseModel):
    """One IR-swap trade flowing through the concurrency seam.

    Carries the swap-body shape verbatim (saved or inline) plus the
    optional ``swap_id`` / ``name`` so structured logs + the
    pricing-history hook can refer back to the originating row. The
    seam's ``T`` type parameter is bound to this class so per-call
    fan-out groups trades by curve set + as_of via the
    ``EngineBatch.shared_inputs`` map.
    """

    model_config = ConfigDict(extra="forbid")

    swap_id: UUID | None = None
    name: str | None = None
    swap: dict[str, Any] = Field(default_factory=dict)


class AssembledIrSwapRequest(BaseModel):
    """The full resolved pricing request the orchestrator would hand the engine.

    Echoed back in the response body (and in the ``details`` of the
    error envelope on engine failure) so operators can confirm the
    end-to-end resolution path completed before the engine call
    failed — particularly important on today's stub backend where
    every call surfaces as ``code="engine_unavailable"`` regardless
    of the request shape.
    """

    model_config = ConfigDict(extra="forbid")

    as_of: date
    snapshot_id: UUID | None = None
    curve_set_id: UUID | None = Field(
        default=None,
        description=(
            "Logical curve-set identifier for the concurrency "
            "seam: trades sharing this value can be grouped onto one "
            "engine call once the ``GroupByCurveSet`` policy lands. "
            "``None`` for purely-inline pricing requests."
        ),
    )
    trade: IrSwapTrade
    curves: list[ResolvedCurve] = Field(default_factory=list)
    resolved_quotes: list[ResolvedQuoteValue] = Field(default_factory=list)


class SwapFlow(BaseModel):
    """One per-period cashflow on a swap leg, decoded from ``SwapLegFlow``.

    Field names mirror the engine's ``SwapLegFlow`` FlatBuffers table
    (``vanilla_swap_response.fbs``) one-for-one, snake-cased. The engine
    only emits these when the request sets ``include_flows``; the
    orchestrator now sets it unconditionally so every priced swap surfaces
    its schedule. ``discount`` is the per-period discount factor;
    ``present_value`` is ``amount * discount``; ``rate`` is the fixed-leg
    coupon rate or the effective floating coupon rate. The floating-only
    fields (``fixing_date`` / ``index_fixing`` / ``spread`` / CMS metadata)
    are populated for float legs and left at their defaults for the fixed
    leg.
    """

    model_config = ConfigDict(extra="forbid")

    payment_date: str | None = None
    accrual_start_date: str | None = None
    accrual_end_date: str | None = None
    amount: float = 0.0
    accrual_year_fraction: float = 0.0
    gearing: float = 1.0
    discount: float = 0.0
    present_value: float = 0.0
    fixing_date: str | None = None
    index_fixing: float = 0.0
    has_cms_swap_rate: bool = False
    cms_swap_rate: float = 0.0
    spread: float = 0.0
    rate: float = 0.0


class IrSwapResult(BaseModel):
    """Engine response for one IR swap, decoded from FlatBuffers.

    Carries the headline NPV / per-leg NPVs / extras (fair rate, etc.) plus
    the per-period cashflows for each leg (``fixed_leg_flows`` /
    ``floating_leg_flows``). The engine already supports detailed flows behind
    the request's ``include_flows`` bool; the orchestrator now requests and
    decodes them. The inner ``leg_npvs`` / ``extras`` fields stay
    deliberately loose because the engine's wire shape is still in flux
    upstream; the engine's own ``.fbs`` is the source of truth there. The
    flow lists default to empty so a backend that does not emit flows (or an
    older response) stays back-compatible.
    """

    model_config = ConfigDict(extra="forbid")

    npv: float | None = None
    leg_npvs: list[dict[str, Any]] = Field(default_factory=list)
    extras: dict[str, Any] = Field(default_factory=dict)
    fixed_leg_flows: list[SwapFlow] = Field(default_factory=list)
    floating_leg_flows: list[SwapFlow] = Field(default_factory=list)


class IrSwapPriceRequest(BaseModel):
    """Endpoint input: either by-reference or fully inline.

    Validation rules (enforced by the post-init validator below):

    * Exactly one of ``swap_id`` or ``swap`` is set.
    * Inline mode (``swap`` set) must also supply ``curves`` so the
      orchestrator can resolve quote IDs without consulting
      ``app.curve_sets``.
    * By-reference mode (``swap_id`` set) MAY supply ``curves`` to
      override the swap's saved curve refs.

    ``as_of`` is the resolution date for every quote ID; pinning to
    a particular ``snapshot_id`` makes the call reproducible (the
    pinned snapshot's content overrides the live MD lookup for
    canonical IDs it contains).
    """

    model_config = ConfigDict(extra="forbid")

    swap_id: UUID | None = Field(
        default=None,
        description=(
            "ID of an ``app.swaps_ir`` row owned by the caller. The "
            "orchestrator loads it via ``app_ro`` and uses its "
            "``request`` JSONB as the trade body."
        ),
    )
    swap: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Inline swap body. The shape matches what the portal "
            "would have saved into ``swaps_ir.request`` — i.e. the "
            "full pricing-request payload. Mutually exclusive with "
            "``swap_id``."
        ),
    )
    curves: list[CurveRef] | None = Field(
        default=None,
        description=(
            "Optional curve overrides. Each entry may be a ref into "
            '``app.curves`` (``{"id": "..."}``) or a fully inline '
            "definition. Required for inline mode; optional for "
            "by-reference mode (defaults to whatever the saved swap "
            "references)."
        ),
    )
    as_of: date = Field(
        description=(
            "Resolution date for every quote ID in the resolved "
            "curves. The orchestrator passes this through to the MD "
            "service via ``MdClient.resolve_quotes(..., as_of)``."
        ),
    )
    snapshot_id: UUID | None = Field(
        default=None,
        description=(
            "Optional pin into ``app.snapshots``. When set, every "
            "canonical ID the pinned snapshot covers is resolved "
            "from the snapshot's content rather than the live MD "
            "service, making the call reproducible across reruns."
        ),
    )

    @model_validator(mode="after")
    def _validate_branches(self) -> Self:
        if self.swap_id is None and self.swap is None:
            msg = "Either ``swap_id`` (by reference) or ``swap`` (inline) must be set."
            raise ValueError(msg)
        if self.swap_id is not None and self.swap is not None:
            msg = "``swap_id`` and ``swap`` are mutually exclusive; pick one."
            raise ValueError(msg)
        if self.swap is not None and (self.curves is None or len(self.curves) == 0):
            msg = "Inline mode (``swap`` set) requires a non-empty ``curves`` list."
            raise ValueError(msg)
        return self


class IrSwapPriceResponse(BaseModel):
    """Endpoint output. Success path; failure paths emit the envelope.

    ``pricing_history_id`` carries the best-effort ``app.pricing_history``
    row id recorded for the call (``None`` when history capture is
    disabled or the write did not complete).
    """

    model_config = ConfigDict(extra="forbid")

    pricing_history_id: str | None = Field(
        default=None,
        description=(
            "Best-effort ``app.pricing_history`` row id recorded for "
            "this call. ``None`` when history capture is disabled or "
            "the write did not complete."
        ),
    )
    assembled_request: AssembledIrSwapRequest = Field(
        description=(
            "The fully resolved request the orchestrator handed (or "
            "would have handed) the engine. Echoed on every response "
            "so callers can verify the resolution path."
        ),
    )
    result: IrSwapResult


__all__ = [
    "AssembledIrSwapRequest",
    "CurveRef",
    "IrSwapPriceRequest",
    "IrSwapPriceResponse",
    "IrSwapResult",
    "IrSwapTrade",
    "ResolvedCurve",
    "ResolvedIndex",
    "ResolvedQuoteValue",
    "SwapFlow",
]
