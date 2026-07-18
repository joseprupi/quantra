"""Pydantic v2 request / response models for ``POST /v1/price/bonds/{fixed,floating}``.

The bonds package is the first to ship **two routes from one
package**. The
fixed and floating variants share almost every shape — by-reference vs.
inline branching, curve resolution, snapshot pinning — and diverge in
only two places: the bond body itself (saved into a different
``app.bonds_{fixed,floating}.request`` JSONB) and which engine RPC the
request lands on (`PRICE_FIXED_RATE_BOND` vs. `PRICE_FLOATING_RATE_BOND`
per the canonical :class:`quantra_common.engine_client.EngineRpc`).

Three layers (each present in two flavours where the trade body differs):

1. **Request envelope.** :class:`FixedBondPriceRequest` /
   :class:`FloatingBondPriceRequest` accept either a ``bond_id``
   reference (load from ``app.bonds_{fixed,floating}``) or an inline
   ``bond`` + ``curves`` payload (price without persisting). Both carry
   the mandatory ``as_of`` and the optional ``snapshot_id`` pin. The
   floating variant additionally accepts an ``index`` ref-or-inline
   override so callers can swap projection indices ad-hoc; the assembler
   defers to the saved-bond's pricing block otherwise.
2. **Intermediate / engine-facing.** :class:`CurveRef` / :class:`IndexRef`
   are the reference-or-inline shapes; :class:`ResolvedCurve` /
   :class:`ResolvedIndex` are the shapes after the assembler has loaded
   any ``app.*`` rows and squashed both branches into a single canonical
   form. :class:`ResolvedQuoteValue` is one MD point resolved server-side
   per the invariant. :class:`FixedBondTrade` /
   :class:`FloatingBondTrade` are the per-RPC ``T`` parameters flowing
   through the concurrency seam.
3. **Response envelope.** :class:`AssembledFixedBondRequest` /
   :class:`AssembledFloatingBondRequest` carry the full resolved request
   the orchestrator would hand the engine; :class:`FixedBondResult` /
   :class:`FloatingBondResult` are the engine's decoded responses;
   :class:`FixedBondPriceResponse` / :class:`FloatingBondPriceResponse`
   are what the routes return on success.

The duplication of :class:`CurveRef` / :class:`ResolvedCurve` /
:class:`ResolvedQuoteValue` between this module and the swap_ir /
swaption packages is **intentional**: factoring a shared module
before the shapes stop moving would couple the products prematurely.
A shared module is a candidate future refactor.
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
    client-side. Mirrors the swap_ir / swaption shape on purpose so a
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


class IndexRef(BaseModel):
    """A floating-rate index reference *or* a fully inline index spec.

    Either ``id`` points at an ``app.indices`` row owned by the caller
    (the orchestrator loads + projects it) or the inline fields
    (``kind`` + ``currency`` + ``body``) carry a full definition. The
    portal's saved-bond shape may carry the index either way (legacy
    rows pre-date the ``app.indices`` entity's bonds-side
    discussion of soft refs).

    The index body is **not** MD-resolved — its calendar / day-counter
    / fixing-days knobs are pure config — but the assembler still
    loads ``app.indices`` so the endpoint fails fast on a stale
    reference rather than surfacing a confusing engine-side error.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = Field(
        default=None,
        description=(
            "Soft reference into ``app.indices``. When set, the "
            "orchestrator loads the row from the database and "
            "ignores any inline fields below."
        ),
    )
    name: str | None = None
    kind: str | None = Field(
        default=None,
        description=(
            "Index discriminator mirroring ``app.indices.kind`` "
            "(``IborIndex``, ``OvernightIndex``, …). Required for inline "
            "mode so the engine knows which bootstrap shape to build."
        ),
    )
    currency: str | None = None
    calendar: str | None = None
    day_counter: str | None = None
    body: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Inline index body. Same JSONB shape ``app.indices.body`` "
            "carries (tenor, fixing days, business-day convention, …)."
        ),
    )

    @model_validator(mode="after")
    def _validate_branches(self) -> Self:
        if self.id is None and self.body is None:
            msg = (
                "IndexRef must provide either ``id`` (reference into "
                "app.indices) or an inline ``body`` definition."
            )
            raise ValueError(msg)
        if self.id is None and self.kind is None:
            msg = "Inline IndexRef must include ``kind`` (e.g. ``IborIndex``)."
            raise ValueError(msg)
        return self


class ResolvedCurve(BaseModel):
    """A curve after the assembler has loaded / collapsed the ref-vs-inline branches.

    Mirrors the swap_ir / swaption shape so the walker logic between
    products stays a name-only difference. Quote-ID substitution
    happens downstream in
    :mod:`quantra_orchestrator.pricing.bonds.md_resolution`.
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


class ResolvedIndex(BaseModel):
    """A floating-rate index after ref/inline collapsing.

    Not MD-resolved — the body is pure config (calendar, fixing days,
    day counter, period / time unit). Kept as a typed shape so the
    assembled-request echo on engine failures carries which
    index the orchestrator would have asked the engine to bootstrap.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = Field(
        default=None,
        description=(
            "The originating ``app.indices`` row when the index came "
            "from a ref. ``None`` for inline-only definitions."
        ),
    )
    name: str = Field(min_length=1)
    kind: str = Field(
        min_length=1,
        description=("Mirrors ``app.indices.kind`` (``IborIndex`` / ``OvernightIndex`` / …)."),
    )
    currency: str | None = None
    calendar: str | None = None
    day_counter: str | None = None
    body: dict[str, Any] = Field(default_factory=dict)


class ResolvedQuoteValue(BaseModel):
    """One quote ID resolved to its value at the request's ``as_of``.

    Mirrors the swap_ir / swaption shape verbatim. ``from_snapshot``
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


class FixedBondTrade(BaseModel):
    """One fixed-rate bond trade flowing through the concurrency seam.

    Carries the saved-or-inline bond body verbatim plus the optional
    ``bond_id`` / ``name`` so structured logs + the pricing-history
    hook can refer back to the originating row. The seam's ``T``
    type parameter is bound to this class so per-call fan-out can
    eventually group bonds by curve set / discount curve once a
    smarter policy lands (today the ``OneTradePerCall`` default
    keeps it at one trade per gRPC call).
    """

    model_config = ConfigDict(extra="forbid")

    bond_id: UUID | None = None
    name: str | None = None
    bond: dict[str, Any] = Field(default_factory=dict)


class FloatingBondTrade(BaseModel):
    """One floating-rate bond trade flowing through the concurrency seam.

    Same shape as :class:`FixedBondTrade` minus the discriminator —
    the floating variant lands on ``PRICE_FLOATING_RATE_BOND`` and
    its assembled-request payload carries a projection curve + index
    in addition to the discount curve.
    """

    model_config = ConfigDict(extra="forbid")

    bond_id: UUID | None = None
    name: str | None = None
    bond: dict[str, Any] = Field(default_factory=dict)


class AssembledFixedBondRequest(BaseModel):
    """The full resolved pricing request the orchestrator would hand the engine.

    Echoed back in the response body (and in the ``details`` of the
    error envelope on engine failure,) so operators can confirm
    the end-to-end resolution path completed before the engine call
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
            "Logical curve-set identifier for the concurrency seam: "
            "trades sharing this value can be grouped onto one engine "
            "call once the reserved ``group_by_curve_set`` policy "
            "lands. ``None`` for purely-inline pricing requests."
        ),
    )
    discount_curve_id: UUID | None = Field(
        default=None,
        description=(
            "Logical discount-curve identifier surfaced for log "
            "correlation and for the future ``group_by_curve_set`` "
            "policy. ``None`` for inline-only discount curves."
        ),
    )
    trade: FixedBondTrade
    discount_curve: ResolvedCurve
    resolved_quotes: list[ResolvedQuoteValue] = Field(default_factory=list)


class AssembledFloatingBondRequest(BaseModel):
    """Assembled request for the floating-rate bond endpoint.

    Adds a separate projection-curve + index alongside the discount
    curve. The two curves are surfaced as distinct fields (rather
    than a list) so the engine-side payload can label each role
    without re-walking the assembled curves and so the assembled-
    request echo on engine failures reads naturally.
    """

    model_config = ConfigDict(extra="forbid")

    as_of: date
    snapshot_id: UUID | None = None
    curve_set_id: UUID | None = Field(
        default=None,
        description=(
            "Logical curve-set identifier for the concurrency seam. "
            "Same reserved future policy as the fixed-bond / swap_ir "
            "side (``group_by_curve_set``)."
        ),
    )
    discount_curve_id: UUID | None = Field(
        default=None,
        description=("Logical discount-curve identifier (``None`` for inline discount curves)."),
    )
    projection_curve_id: UUID | None = Field(
        default=None,
        description=(
            "Logical projection-curve identifier (``None`` for inline projection curves)."
        ),
    )
    index_id: UUID | None = Field(
        default=None,
        description=("Logical ``app.indices`` identifier (``None`` for inline indices)."),
    )
    trade: FloatingBondTrade
    discount_curve: ResolvedCurve
    projection_curve: ResolvedCurve
    index: ResolvedIndex
    resolved_quotes: list[ResolvedQuoteValue] = Field(default_factory=list)


class FixedBondResult(BaseModel):
    """Engine response for one fixed-rate bond, decoded from FlatBuffers.

    Today's stub backend never produces one of these (every call
    raises ``NotImplementedError``); the shape is reified now so the
    route handler can stay agnostic to "stub vs. real" once Phase
    6/7 lands the engine bindings. The inner fields are deliberately
    loose because the engine's wire shape is still in flux upstream;
    the engine's own ``.fbs`` is the source of truth once the
    FlatBuffers bindings are vendored in.
    """

    model_config = ConfigDict(extra="forbid")

    npv: float | None = None
    clean_price: float | None = None
    dirty_price: float | None = None
    accrued: float | None = None
    yield_to_maturity: float | None = None
    cashflows: list[dict[str, Any]] = Field(default_factory=list)
    extras: dict[str, Any] = Field(default_factory=dict)


class FloatingBondResult(BaseModel):
    """Engine response for one floating-rate bond, decoded from FlatBuffers.

    Same loose shape as :class:`FixedBondResult`.
    """

    model_config = ConfigDict(extra="forbid")

    npv: float | None = None
    clean_price: float | None = None
    dirty_price: float | None = None
    accrued: float | None = None
    cashflows: list[dict[str, Any]] = Field(default_factory=list)
    extras: dict[str, Any] = Field(default_factory=dict)


class FixedBondPriceRequest(BaseModel):
    """Endpoint input for ``POST /v1/price/bonds/fixed``.

    Either by-reference or fully inline. Validation rules (enforced
    by the post-init validator below):

    * Exactly one of ``bond_id`` or ``bond`` is set.
    * Inline mode (``bond`` set) must also supply ``curves`` so the
      orchestrator can resolve quote IDs without consulting any saved
      curve set.
    * By-reference mode (``bond_id`` set) MAY supply ``curves`` to
      override the saved bond's discount-curve reference.

    ``as_of`` is the resolution date for every quote ID; pinning to
    a particular ``snapshot_id`` makes the call reproducible.
    """

    model_config = ConfigDict(extra="forbid")

    bond_id: UUID | None = Field(
        default=None,
        description=(
            "ID of an ``app.bonds_fixed`` row owned by the caller. The "
            "orchestrator loads it via ``app_ro`` and uses its "
            "``request`` JSONB as the trade body."
        ),
    )
    bond: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Inline fixed-rate bond body. The shape matches what the "
            "portal would have saved into ``bonds_fixed.request`` — "
            "i.e. the full pricing-request payload. Mutually exclusive "
            "with ``bond_id``."
        ),
    )
    curves: list[CurveRef] | None = Field(
        default=None,
        description=(
            "Optional discount-curve override. Each entry may be a "
            'ref into ``app.curves`` (``{"id": "..."}``) or a '
            "fully inline definition. Required for inline mode; "
            "optional for by-reference mode."
        ),
    )
    as_of: date = Field(
        description=("Resolution date for every quote ID in the resolved curves."),
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
        if self.bond_id is None and self.bond is None:
            msg = "Either ``bond_id`` (by reference) or ``bond`` (inline) must be set."
            raise ValueError(msg)
        if self.bond_id is not None and self.bond is not None:
            msg = "``bond_id`` and ``bond`` are mutually exclusive; pick one."
            raise ValueError(msg)
        if self.bond is not None and (self.curves is None or len(self.curves) == 0):
            msg = "Inline mode (``bond`` set) requires a non-empty ``curves`` list."
            raise ValueError(msg)
        return self


class FloatingBondPriceRequest(BaseModel):
    """Endpoint input for ``POST /v1/price/bonds/floating``.

    Same shape as :class:`FixedBondPriceRequest` plus an optional
    ``index`` ref-or-inline override so callers can swap projection
    indices ad-hoc. Inline mode additionally requires the ``index``
    override because there's no saved-bond fallback to consult.

    The ``curves`` list must carry **at least one** curve for inline
    mode; how the assembler maps that list onto discount-vs-projection
    roles is documented in :mod:`bonds.assembler`.
    """

    model_config = ConfigDict(extra="forbid")

    bond_id: UUID | None = Field(
        default=None,
        description=(
            "ID of an ``app.bonds_floating`` row owned by the caller. "
            "The orchestrator loads it via ``app_ro`` and uses its "
            "``request`` JSONB as the trade body."
        ),
    )
    bond: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Inline floating-rate bond body. The shape matches what "
            "the portal would have saved into ``bonds_floating.request"
            "`` — i.e. the full pricing-request payload. Mutually "
            "exclusive with ``bond_id``."
        ),
    )
    curves: list[CurveRef] | None = Field(
        default=None,
        description=(
            "Optional curve overrides. For inline mode the list must "
            "contain at least one entry; if the discount and projection "
            "curves are distinct, both must be present (the assembler "
            "consults the inline ``bond`` body's role markers to map "
            "list entries onto roles)."
        ),
    )
    index: IndexRef | None = Field(
        default=None,
        description=(
            "Optional ``app.indices`` override. Required for inline "
            "mode; optional for by-reference mode (defaults to the "
            "saved bond's ``index_id`` / inline index)."
        ),
    )
    as_of: date = Field(
        description=("Resolution date for every quote ID in the resolved curves."),
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
        if self.bond_id is None and self.bond is None:
            msg = "Either ``bond_id`` (by reference) or ``bond`` (inline) must be set."
            raise ValueError(msg)
        if self.bond_id is not None and self.bond is not None:
            msg = "``bond_id`` and ``bond`` are mutually exclusive; pick one."
            raise ValueError(msg)
        if self.bond is not None:
            if self.curves is None or len(self.curves) == 0:
                msg = "Inline mode (``bond`` set) requires a non-empty ``curves`` list."
                raise ValueError(msg)
            if self.index is None:
                msg = "Inline mode (``bond`` set) requires an ``index`` override."
                raise ValueError(msg)
        return self


class FixedBondPriceResponse(BaseModel):
    """``POST /v1/price/bonds/fixed`` success-path response.

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
    assembled_request: AssembledFixedBondRequest = Field(
        description=(
            "The fully resolved request the orchestrator handed (or "
            "would have handed) the engine. Echoed on every response "
            "so callers can verify the resolution path."
        ),
    )
    result: FixedBondResult


class FloatingBondPriceResponse(BaseModel):
    """``POST /v1/price/bonds/floating`` success-path response."""

    model_config = ConfigDict(extra="forbid")

    pricing_history_id: str | None = Field(
        default=None,
        description=(
            "Best-effort ``app.pricing_history`` row id recorded for "
            "this call. ``None`` when history capture is disabled or "
            "the write did not complete."
        ),
    )
    assembled_request: AssembledFloatingBondRequest = Field(
        description=(
            "The fully resolved request the orchestrator handed (or would have handed) the engine."
        ),
    )
    result: FloatingBondResult


__all__ = [
    "AssembledFixedBondRequest",
    "AssembledFloatingBondRequest",
    "CurveRef",
    "FixedBondPriceRequest",
    "FixedBondPriceResponse",
    "FixedBondResult",
    "FixedBondTrade",
    "FloatingBondPriceRequest",
    "FloatingBondPriceResponse",
    "FloatingBondResult",
    "FloatingBondTrade",
    "IndexRef",
    "ResolvedCurve",
    "ResolvedIndex",
    "ResolvedQuoteValue",
]
