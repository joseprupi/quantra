"""Pydantic v2 request / response models for ``POST /v1/price/cds``.

CDS is the first per-product endpoint to ship **two distinct entity
families** through the assembler: ``app.credit_curves`` (hazard-rate /
survival-probability inputs that carry their own data,
i.e. they do NOT reference vendor MD quotes) and ``app.curves`` (the
discount curve, which does reference MD quotes through the standard
walker). The split is reflected in the model layout below — separate
ref / resolved shapes for each family, and a separate per-bundle-
stage 422 error + the refinement.

Three layers:

1. **Request envelope.** :class:`CdsPriceRequest` accepts either a
   ``cds_id`` reference (load from ``app.cds``) or an inline ``cds``
   body. Either the discount curve and credit curve come from the
   saved CDS's body (``pricing.discount_curve_id`` /
   ``pricing.credit_curve_id``) or the caller supplies overrides via
   the top-level ``curves`` (single-element list for the discount
   curve, mirroring the bonds shape), ``credit_curve_id`` and
   ``credit_curve`` fields. Inline mode (``cds`` set) requires both
   a discount curve override and a credit curve override.
2. **Intermediate / engine-facing.** :class:`CurveRef` /
   :class:`CreditCurveRef` are the reference-or-inline shapes per
   D9; :class:`ResolvedCurve` / :class:`ResolvedCreditCurve` are
   the shapes after the assembler has loaded any ``app.*`` rows
   and squashed both branches into a single canonical form.
   :class:`ResolvedQuoteValue` is one MD point resolved server-side
   . :class:`CdsTrade` is the ``T`` parameter that flows
   through the concurrency seam.
3. **Response envelope.** :class:`AssembledCdsRequest` carries the
   fully resolved request the orchestrator would hand the engine;
   :class:`CdsResult` is the engine's decoded response for one
   trade; :class:`CdsPriceResponse` is what the route returns on
   success.

The duplication of :class:`CurveRef` / :class:`ResolvedCurve` /
:class:`ResolvedQuoteValue` between this module and the swap_ir /
swaption / bonds packages is **intentional**: factoring a shared
module before the shapes stop moving would couple the products
prematurely. A shared module is a candidate future refactor.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CurveRef(BaseModel):
    """A discount-curve reference *or* a fully inline curve definition.

    Either ``id`` points at an ``app.curves`` row owned by the caller
    (the orchestrator loads + projects it) or the inline fields
    (``points`` at minimum) carry a full definition the caller built
    client-side. Mirrors the swap_ir / bonds shape on purpose so a
    future shared module is a name-only refactor.
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


class CreditCurveRef(BaseModel):
    """A credit-curve reference *or* a fully inline credit-curve definition.

    Either ``id`` points at an ``app.credit_curves`` row owned by the
    caller (the orchestrator loads + projects it) or the inline
    fields (``recovery_rate`` + ``body`` at minimum) carry a full
    definition the caller built client-side.

    The credit-curve body shape mirrors what ``app.credit_curves.body``
    JSONB stores: at minimum either ``flat_hazard_rate`` (decimal
    hazard rate, e.g. ``0.02`` for 200bp) or a ``points`` list of
    inline CDS quotes (per the engine's ``CdsQuote`` schema: ``tenor``
    + ``quoted_par_spread`` / ``quoted_upfront``). The assembler
    refuses any body that requires vendor MD resolution per the
    plan's "credit curves carry their own inputs" rule.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = Field(
        default=None,
        description=(
            "Soft reference into ``app.credit_curves``. When set, the "
            "orchestrator loads the row from the database and "
            "ignores any inline fields below."
        ),
    )
    name: str | None = None
    reference_entity: str | None = None
    currency: str | None = None
    seniority: str | None = None
    source: str | None = Field(
        default=None,
        description=(
            "Mirrors ``app.credit_curves.source`` (``flat`` / ``manual`` "
            "/ ``quote_book``). The orchestrator only consumes ``flat`` "
            "and ``manual`` today; ``quote_book`` would require an MD "
            "walker extension and surfaces as 422 "
            "``cds_credit_curve_resolution_failed``."
        ),
    )
    recovery_rate: float | None = Field(
        default=None,
        description=(
            "Recovery rate as a decimal in ``[0, 1]``. Required for "
            "inline mode; loaded from the column for by-reference mode."
        ),
    )
    body: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Inline credit-curve body. Same JSONB shape "
            "``app.credit_curves.body`` carries (``flat_hazard_rate`` "
            "or ``points``, plus optional engine conventions)."
        ),
    )

    @model_validator(mode="after")
    def _validate_branches(self) -> Self:
        if self.id is None and self.body is None:
            msg = (
                "CreditCurveRef must provide either ``id`` (reference "
                "into app.credit_curves) or an inline ``body`` "
                "definition."
            )
            raise ValueError(msg)
        if self.id is None and self.recovery_rate is None:
            msg = "Inline CreditCurveRef must include ``recovery_rate``."
            raise ValueError(msg)
        return self


class ResolvedCurve(BaseModel):
    """A discount curve after the assembler has loaded / collapsed the branches.

    Mirrors the swap_ir / bonds shape so the walker logic between
    products stays a name-only difference. Quote-ID substitution
    happens downstream in
    :mod:`quantra_orchestrator.pricing.cds.md_resolution`.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = Field(
        default=None,
        description=(
            "The originating ``app.curves`` row when the curve came "
            "from a ref. ``None`` for inline-only definitions."
        ),
    )
    name: str = Field(min_length=1)
    currency: str | None = None
    day_counter: str | None = None
    helper_kind: str | None = None
    reference_date: date | None = None
    points: list[dict[str, Any]] = Field(default_factory=list)
    body: dict[str, Any] = Field(default_factory=dict)


class ResolvedCreditCurve(BaseModel):
    """A credit curve after the assembler has loaded / collapsed the branches.

    Carries everything the engine encoder needs to build a
    :class:`CreditCurveSpec` payload: a non-zero ``recovery_rate``
    plus a ``body`` JSONB that contains at minimum a
    ``flat_hazard_rate`` (preferred today) or a ``points`` list of
    inline CDS quotes. The credit curve does NOT carry a resolved-
    quote list because it never traverses vendor MD.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = Field(
        default=None,
        description=(
            "The originating ``app.credit_curves`` row when the curve "
            "came from a ref. ``None`` for inline-only definitions."
        ),
    )
    name: str = Field(min_length=1)
    reference_entity: str | None = None
    currency: str | None = None
    seniority: str | None = None
    source: str | None = None
    recovery_rate: float = Field(ge=0.0, le=1.0)
    body: dict[str, Any] = Field(default_factory=dict)


class ResolvedQuoteValue(BaseModel):
    """One quote ID resolved to its value at the request's ``as_of``.

    Mirrors the swap_ir / bonds shape verbatim. ``from_snapshot``
    carries whether the value came from a user-pinned ``app.snapshots``
    row (True) or the live MD service (False); the pricing-history
    hook uses this for provenance.
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


class CdsTrade(BaseModel):
    """One CDS trade flowing through the concurrency seam.

    Carries the saved-or-inline CDS body verbatim plus the optional
    ``cds_id`` / ``name`` so structured logs + the pricing-history
    hook can refer back to the originating row. The seam's ``T``
    type parameter is bound to this class so per-call fan-out can
    eventually group CDS trades by credit curve once a smarter
    policy lands (today the ``OneTradePerCall`` default keeps it at
    one trade per gRPC call; the reserved future-policy id is
    ``group_by_credit_curve``).
    """

    model_config = ConfigDict(extra="forbid")

    cds_id: UUID | None = None
    name: str | None = None
    cds: dict[str, Any] = Field(default_factory=dict)


class AssembledCdsRequest(BaseModel):
    """The full resolved pricing request the orchestrator would hand the engine.

    Echoed back in the response body (and in the ``details`` of the
    error envelope on engine failure,) so operators can
    confirm the end-to-end resolution path completed before the
    engine call failed.
    """

    model_config = ConfigDict(extra="forbid")

    as_of: date
    snapshot_id: UUID | None = None
    credit_curve_id: UUID | None = Field(
        default=None,
        description=(
            "Logical credit-curve identifier for the concurrency "
            "seam: trades sharing this value can be grouped onto one "
            "engine call once the reserved ``group_by_credit_curve`` "
            "policy lands. ``None`` for inline-only credit "
            "curves."
        ),
    )
    discount_curve_id: UUID | None = Field(
        default=None,
        description=(
            "Logical discount-curve identifier surfaced for log "
            "correlation and any future ``group_by_curve_set`` "
            "policy. ``None`` for inline-only discount curves."
        ),
    )
    trade: CdsTrade
    discount_curve: ResolvedCurve
    credit_curve: ResolvedCreditCurve
    resolved_quotes: list[ResolvedQuoteValue] = Field(default_factory=list)


class CdsResult(BaseModel):
    """Engine response for one CDS, decoded from FlatBuffers.

    Fields map 1:1 onto the engine's ``CDSValues`` flatbuffer
    (`cds_response.fbs`): ``npv``, ``fair_spread`` (par spread in
    decimal), ``fair_upfront``, ``default_leg_npv`` (also surfaced as
    ``protection_leg_npv`` in the per-product diagnostics), and
    ``premium_leg_npv``. The engine guarantees every field is
    populated on a non-error response so they're typed as ``float``
    here rather than ``float | None``.
    """

    model_config = ConfigDict(extra="forbid")

    npv: float | None = None
    fair_spread: float | None = None
    fair_upfront: float | None = None
    protection_leg_npv: float | None = None
    premium_leg_npv: float | None = None
    extras: dict[str, Any] = Field(default_factory=dict)


class CdsPriceRequest(BaseModel):
    """Endpoint input for ``POST /v1/price/cds``.

    Either by-reference (``cds_id``) or fully inline (``cds`` body).
    The discount curve can be overridden through the top-level
    ``curves`` field (single-element list, mirroring bonds_fixed);
    the credit curve can be overridden through ``credit_curve_id``
    (ref) or ``credit_curve`` (inline). Inline mode requires both
    overrides — there's no saved-CDS fallback for either curve.

    Validation rules (enforced by the post-init validator below):

    * Exactly one of ``cds_id`` or ``cds`` is set.
    * Inline mode (``cds`` set) must also supply a non-empty
      ``curves`` list and either ``credit_curve_id`` or
      ``credit_curve``.
    * By-reference mode MAY supply any of the override fields.
    * ``credit_curve_id`` and ``credit_curve`` are mutually exclusive.
    """

    model_config = ConfigDict(extra="forbid")

    cds_id: UUID | None = Field(
        default=None,
        description=(
            "ID of an ``app.cds`` row owned by the caller. The "
            "orchestrator loads it via ``app_ro`` and uses its "
            "``request`` JSONB as the trade body."
        ),
    )
    cds: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Inline CDS body. The shape matches what the portal would "
            "have saved into ``cds.request`` — i.e. the full pricing-"
            "request payload. Mutually exclusive with ``cds_id``."
        ),
    )
    curves: list[CurveRef] | None = Field(
        default=None,
        description=(
            "Optional discount-curve override. Each entry may be a ref "
            "into ``app.curves`` or a fully inline definition. "
            "Required for inline mode; optional for by-reference mode. "
            "The CDS endpoint consumes exactly one discount curve; a "
            "multi-entry list collapses to the first entry."
        ),
    )
    credit_curve_id: UUID | None = Field(
        default=None,
        description=(
            "Optional credit-curve ref override (soft reference into "
            "``app.credit_curves``). Mutually exclusive with "
            "``credit_curve``."
        ),
    )
    credit_curve: CreditCurveRef | None = Field(
        default=None,
        description=(
            "Optional inline credit-curve override. Mutually exclusive with ``credit_curve_id``."
        ),
    )
    as_of: date = Field(
        description=(
            "Resolution date for every quote ID in the resolved "
            "discount curve. Credit curves carry their own inputs "
            "and do not consult MD at all."
        ),
    )
    snapshot_id: UUID | None = Field(
        default=None,
        description=(
            "Optional pin into ``app.snapshots``. When set, every "
            "canonical ID the pinned snapshot covers is resolved from "
            "the snapshot's content rather than the live MD service."
        ),
    )

    @model_validator(mode="after")
    def _validate_branches(self) -> Self:
        if self.cds_id is None and self.cds is None:
            msg = "Either ``cds_id`` (by reference) or ``cds`` (inline) must be set."
            raise ValueError(msg)
        if self.cds_id is not None and self.cds is not None:
            msg = "``cds_id`` and ``cds`` are mutually exclusive; pick one."
            raise ValueError(msg)
        if self.credit_curve_id is not None and self.credit_curve is not None:
            msg = "``credit_curve_id`` and ``credit_curve`` are mutually exclusive; pick one."
            raise ValueError(msg)
        if self.cds is not None:
            if self.curves is None or len(self.curves) == 0:
                msg = "Inline mode (``cds`` set) requires a non-empty ``curves`` list."
                raise ValueError(msg)
            if self.credit_curve_id is None and self.credit_curve is None:
                msg = (
                    "Inline mode (``cds`` set) requires either "
                    "``credit_curve_id`` or ``credit_curve``."
                )
                raise ValueError(msg)
        return self


class CdsPriceResponse(BaseModel):
    """``POST /v1/price/cds`` success-path response.

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
    assembled_request: AssembledCdsRequest = Field(
        description=(
            "The fully resolved request the orchestrator handed (or "
            "would have handed) the engine. Echoed on every response "
            "so callers can verify the resolution path."
        ),
    )
    result: CdsResult


__all__ = [
    "AssembledCdsRequest",
    "CdsPriceRequest",
    "CdsPriceResponse",
    "CdsResult",
    "CdsTrade",
    "CreditCurveRef",
    "CurveRef",
    "ResolvedCreditCurve",
    "ResolvedCurve",
    "ResolvedQuoteValue",
]
