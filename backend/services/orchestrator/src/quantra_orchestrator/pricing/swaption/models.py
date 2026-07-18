"""Pydantic v2 request / response models for ``POST /v1/price/swaption``.

Three layers, structurally analogous to
:mod:`quantra_orchestrator.pricing.swap_ir.models` (the Phase-3 template
locked by convention). Differences vs. the IR-swap product:

* The trade carries a swaption body (exercise type / underlying swap /
  ATM offsets / etc.); the shape is left as ``dict[str, Any]`` because
  the portal's saved-request schema (``swaptions.request`` JSONB)
  is still in flux and the engine's FlatBuffers shape — not pydantic —
  is the source of truth for the final wire form.
* Two extra ``shared_inputs`` ride along (vol surface + swaption model)
  in addition to the curve set that IR swaps already carry. Both are
  reified as ref-or-inline shapes so the same endpoint serves saved
  rows and ad-hoc payloads.
* :class:`ResolvedVolSurface` mirrors :class:`ResolvedCurve` so the
  MD-resolution walker can traverse the vol-surface body with the same
  ``quote_id``-collection contract. Vol surfaces' ``payload`` JSONB
  carries the (per-strike / per-tenor) grid with optional
  ``{"quoteId": "..."}`` cells — the walker substitutes those exactly
  the way it substitutes curve-point ``quote_id`` leaves.
* :class:`ResolvedSwaptionModel` is the only resolved object **not**
  MD-resolved — its body is pure config (model class, calibration
  knobs) and never references a quote ID; the assembler still loads
  ``app.swaption_models`` so the endpoint can fail fast on a missing
  reference rather than surfacing a confusing engine-side error.

The duplication of :class:`CurveRef` / :class:`ResolvedCurve` /
:class:`ResolvedQuoteValue` between this module and
``pricing.swap_ir.models`` is **intentional**: factoring a shared
module before the shapes stop moving would couple the products
prematurely. A shared ``pricing/_resolved/`` module is a candidate
future refactor.
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
    client-side. Mirrors the swap_ir shape on purpose so a future
    shared module is a name-only refactor.
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


class VolSurfaceRef(BaseModel):
    """A vol-surface reference *or* a fully inline vol-surface definition.

    Either ``id`` points at an ``app.vol_surfaces`` row owned by the
    caller (the orchestrator loads + projects it) or the inline
    fields (``payload_type`` + ``payload`` at minimum) carry a full
    definition the caller built client-side. The latter is what the
    portal sends today when a user is editing an unsaved surface.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = Field(
        default=None,
        description=(
            "Soft reference into ``app.vol_surfaces``. When set, the "
            "orchestrator loads the row from the database and "
            "ignores any inline fields below."
        ),
    )
    name: str | None = None
    kind: str | None = Field(
        default=None,
        description=(
            "One of ``SwaptionVolSpec`` / ``OptionletVolSpec`` / "
            "``BlackVolSpec`` (mirrors ``app.vol_surfaces.kind``). "
            "Required for inline mode so the engine knows which "
            "wire shape to bootstrap."
        ),
    )
    payload: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Inline vol-surface body. Same JSONB shape ``app.vol_"
            "surfaces.payload`` carries (axes, grids, SABR params, "
            "etc.) so an inline + a saved surface look identical "
            "downstream of the assembler."
        ),
    )

    @model_validator(mode="after")
    def _validate_branches(self) -> Self:
        if self.id is None and self.payload is None:
            msg = (
                "VolSurfaceRef must provide either ``id`` (reference "
                "into app.vol_surfaces) or an inline ``payload`` "
                "definition."
            )
            raise ValueError(msg)
        if self.id is None and self.kind is None:
            msg = (
                "Inline VolSurfaceRef must include ``kind`` (one of "
                "``SwaptionVolSpec`` / ``OptionletVolSpec`` / "
                "``BlackVolSpec``)."
            )
            raise ValueError(msg)
        return self


class SwaptionModelRef(BaseModel):
    """A swaption-model reference *or* a fully inline model spec.

    Either ``id`` points at an ``app.swaption_models`` row owned by
    the caller (the orchestrator loads + projects it) or the inline
    fields (``kind`` + ``payload``) carry a full definition. Unlike
    curves and vol surfaces, the model body is **not** MD-resolved —
    it carries calibration knobs (HW lattice parameters, SABR
    seeds, etc.) that are pure config, not quotes.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = Field(
        default=None,
        description=(
            "Soft reference into ``app.swaption_models``. When set, "
            "the orchestrator loads the row from the database "
            "and ignores any inline fields below."
        ),
    )
    name: str | None = None
    kind: str | None = Field(
        default=None,
        description=(
            "Model discriminator (``HullWhiteLattice`` today; future "
            "short-rate models slot in here). Mirrors "
            "``app.swaption_models.kind``."
        ),
    )
    payload: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Inline swaption-model body. Same JSONB shape ``app.swaption_models.payload`` carries."
        ),
    )

    @model_validator(mode="after")
    def _validate_branches(self) -> Self:
        if self.id is None and self.payload is None:
            msg = (
                "SwaptionModelRef must provide either ``id`` "
                "(reference into app.swaption_models) or an inline "
                "``payload`` definition."
            )
            raise ValueError(msg)
        if self.id is None and self.kind is None:
            msg = "Inline SwaptionModelRef must include ``kind`` (e.g. ``HullWhiteLattice``)."
            raise ValueError(msg)
        return self


class IndexRef(BaseModel):
    """The underlying swap's float-leg index: a reference *or* inline spec.

    Either ``id`` points at an ``app.indices`` row owned by the caller
    (the orchestrator loads + projects it) or the inline fields
    (``kind`` + ``body``) carry a full definition. Mirrors the
    bonds-side :class:`~quantra_orchestrator.pricing.bonds.models.IndexRef`
    shape verbatim — the duplication is intentional and temporary (the
    same shared-``pricing/_resolved`` follow-up that folds the curve
    shapes will fold this).

    The index body is **not** MD-resolved — its tenor / calendar /
    day-counter / fixing-days knobs are pure config — but the assembler
    still loads ``app.indices`` for the ref case so the endpoint fails
    fast on a stale reference rather than surfacing a confusing
    engine-side error. When this field is omitted the swaption's
    underlying float leg keeps exactly today's behaviour (the interim
    default forwarding index + Semiannual coupon frequency).
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
            "carries (tenor, fixing days, business-day convention, …). "
            "The tenor drives the underlying float-leg coupon frequency "
            "— a 3M index pays quarterly, a 6M index semi-annually."
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

    Mirrors the swap_ir shape so the walker logic between products
    stays a name-only difference. Quote-ID substitution happens
    downstream in :mod:`quantra_orchestrator.pricing.swaption.md_resolution`.
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


class ResolvedVolSurface(BaseModel):
    """A vol surface after the assembler has loaded / collapsed the ref-vs-inline branches.

    The ``payload`` JSONB carries whatever the portal saved
    (`SwaptionVolSpec` / `OptionletVolSpec` / `BlackVolSpec`). The
    MD-resolution walker traverses this body looking for `quote_id`
    leaves and `{"quoteId": "..."}` cells inside grids; see
    :mod:`pricing.swaption.md_resolution` for the exact shape contract.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = Field(
        default=None,
        description=(
            "The originating ``app.vol_surfaces`` row when the surface "
            "came from a ref. ``None`` for inline-only definitions."
        ),
    )
    name: str = Field(min_length=1)
    kind: str = Field(
        min_length=1,
        description=(
            "Mirrors ``app.vol_surfaces.kind`` "
            "(``SwaptionVolSpec`` / ``OptionletVolSpec`` / ``BlackVolSpec``)."
        ),
    )
    payload: dict[str, Any] = Field(default_factory=dict)


class ResolvedSwaptionModel(BaseModel):
    """A swaption model after the assembler has loaded / collapsed the ref-vs-inline branches.

    Not MD-resolved — the body is pure config (model class,
    calibration knobs). Kept as a typed shape so the assembled-request
    echo on engine failures carries which model the orchestrator
    would have asked the engine to bootstrap.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = Field(
        default=None,
        description=(
            "The originating ``app.swaption_models`` row when the model "
            "came from a ref. ``None`` for inline-only definitions."
        ),
    )
    name: str = Field(min_length=1)
    kind: str = Field(
        min_length=1,
        description=("Mirrors ``app.swaption_models.kind`` (``HullWhiteLattice`` today)."),
    )
    payload: dict[str, Any] = Field(default_factory=dict)


class ResolvedIndex(BaseModel):
    """The underlying swap's float-leg index after ref/inline collapsing.

    An internal intermediate (not a request/response model): the assembler
    projects the supplied :class:`IndexRef` (inline body or a loaded
    ``app.indices`` row) into this shape so the translator can build a
    faithful ``IndexDef`` for the swaption's underlying float leg —
    and, crucially, so ``float_leg_frequency`` follows the index tenor.
    Not MD-resolved — the body is pure config (tenor / fixing days / calendar
    / day counter). Mirrors the swap_ir / bonds shape (duplication intentional
    + temporary; a shared ``pricing/_resolved`` folds all three).
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

    Mirrors the swap_ir shape verbatim. ``from_snapshot`` carries
    whether the value came from a user-pinned ``app.snapshots`` row
    (True) or the live MD service (False); the pricing-history hook
    uses this for provenance.
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


class SwaptionTrade(BaseModel):
    """One swaption trade flowing through the concurrency seam.

    Carries the swaption body shape verbatim (saved or inline) plus
    the optional ``swaption_id`` / ``name`` so structured logs + the
    pricing-history hook can refer back to the originating row. The
    seam's ``T`` type parameter is bound to this class so per-call
    fan-out groups trades by vol surface / curve set via the
    ``EngineBatch.shared_inputs`` map (today every batch is one
    trade per the ``OneTradePerCall`` default).
    """

    model_config = ConfigDict(extra="forbid")

    swaption_id: UUID | None = None
    name: str | None = None
    swaption: dict[str, Any] = Field(default_factory=dict)


class AssembledSwaptionRequest(BaseModel):
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
            "Logical curve-set identifier for the concurrency "
            "seam: trades sharing this value can be grouped onto one "
            "engine call once the ``GroupByCurveSet`` policy lands. "
            "``None`` for purely-inline pricing requests."
        ),
    )
    vol_surface_id: UUID | None = Field(
        default=None,
        description=(
            "Logical vol-surface identifier for the concurrency "
            "seam: trades sharing this value can be grouped onto one "
            "engine call once the future ``GroupByVolSurface`` policy "
            "lands (more impactful than curve grouping because "
            "surface bootstrap is heavier). "
            "``None`` for purely-inline vol-surface payloads."
        ),
    )
    swaption_model_id: UUID | None = Field(
        default=None,
        description=(
            "Logical swaption-model identifier for the concurrency "
            "seam. ``None`` for inline model payloads."
        ),
    )
    trade: SwaptionTrade
    curves: list[ResolvedCurve] = Field(default_factory=list)
    vol_surface: ResolvedVolSurface
    swaption_model: ResolvedSwaptionModel
    resolved_quotes: list[ResolvedQuoteValue] = Field(default_factory=list)


class SwaptionResult(BaseModel):
    """Engine response for one swaption, decoded from FlatBuffers.

    The shape is deliberately loose so the route handler stays
    agnostic to "stub vs. real" backends; the engine's own ``.fbs``
    schema (vendored FlatBuffers bindings) is the source of truth
    for the wire shape.
    """

    model_config = ConfigDict(extra="forbid")

    npv: float | None = None
    vega: float | None = None
    delta: float | None = None
    leg_npvs: list[dict[str, Any]] = Field(default_factory=list)
    extras: dict[str, Any] = Field(default_factory=dict)


class SwaptionPriceRequest(BaseModel):
    """Endpoint input: either by-reference or fully inline.

    Validation rules (enforced by the post-init validator below):

    * Exactly one of ``swaption_id`` or ``swaption`` is set.
    * Inline mode (``swaption`` set) must also supply ``curves``,
      ``vol_surface`` and ``swaption_model`` so the orchestrator can
      resolve every reference without consulting saved entities.
    * By-reference mode (``swaption_id`` set) MAY supply any of those
      overrides to take precedence over the swaption's saved refs.

    ``as_of`` is the resolution date for every quote ID (in curves
    and vol surfaces); pinning to a particular ``snapshot_id`` makes
    the call reproducible (the pinned snapshot's content overrides
    the live MD lookup for canonical IDs it contains).
    """

    model_config = ConfigDict(extra="forbid")

    swaption_id: UUID | None = Field(
        default=None,
        description=(
            "ID of an ``app.swaptions`` row owned by the caller. The "
            "orchestrator loads it via ``app_ro`` and uses its "
            "``request`` JSONB as the trade body."
        ),
    )
    swaption: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Inline swaption body. The shape matches what the portal "
            "would have saved into ``swaptions.request`` — i.e. the "
            "full pricing-request payload. Mutually exclusive with "
            "``swaption_id``."
        ),
    )
    curves: list[CurveRef] | None = Field(
        default=None,
        description=(
            "Optional curve overrides. Each entry may be a ref into "
            '``app.curves`` (``{"id": "..."}``) or a fully inline '
            "definition. Required for inline mode; optional for "
            "by-reference mode (defaults to whatever the saved "
            "swaption references)."
        ),
    )
    vol_surface: VolSurfaceRef | None = Field(
        default=None,
        description=(
            "Optional vol-surface override. Same ref-or-inline shape "
            "as ``curves``; required for inline mode; optional for "
            "by-reference mode."
        ),
    )
    swaption_model: SwaptionModelRef | None = Field(
        default=None,
        description=(
            "Optional swaption-model override. Same ref-or-inline "
            "shape as ``curves``; required for inline mode; "
            "optional for by-reference mode."
        ),
    )
    index: IndexRef | None = Field(
        default=None,
        description=(
            "Optional underlying-swap float-leg index. A ref into "
            '``app.indices`` (``{"id": "..."}``) or a fully inline '
            "spec (``kind`` + ``body``). When supplied, the resolved "
            "index drives the underlying float leg's projection index and "
            "coupon frequency (a 3M index → quarterly, a 6M index → "
            "semi-annual). Fully OPTIONAL and back-compatible: when "
            "omitted the underlying float leg keeps exactly today's "
            "behaviour (interim default forwarding index + Semiannual). "
            "Not required even in inline mode."
        ),
    )
    as_of: date = Field(
        description=(
            "Resolution date for every quote ID in the resolved "
            "curves + vol surface. The orchestrator passes this "
            "through to the MD service via "
            "``MdClient.resolve_quotes(..., as_of)``."
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
        if self.swaption_id is None and self.swaption is None:
            msg = "Either ``swaption_id`` (by reference) or ``swaption`` (inline) must be set."
            raise ValueError(msg)
        if self.swaption_id is not None and self.swaption is not None:
            msg = "``swaption_id`` and ``swaption`` are mutually exclusive; pick one."
            raise ValueError(msg)
        if self.swaption is not None:
            if self.curves is None or len(self.curves) == 0:
                msg = "Inline mode (``swaption`` set) requires a non-empty ``curves`` list."
                raise ValueError(msg)
            if self.vol_surface is None:
                msg = "Inline mode (``swaption`` set) requires a ``vol_surface`` override."
                raise ValueError(msg)
            if self.swaption_model is None:
                msg = "Inline mode (``swaption`` set) requires a ``swaption_model`` override."
                raise ValueError(msg)
        return self


class SwaptionPriceResponse(BaseModel):
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
    assembled_request: AssembledSwaptionRequest = Field(
        description=(
            "The fully resolved request the orchestrator handed (or "
            "would have handed) the engine. Echoed on every response "
            "so callers can verify the resolution path."
        ),
    )
    result: SwaptionResult


__all__ = [
    "AssembledSwaptionRequest",
    "CurveRef",
    "IndexRef",
    "ResolvedCurve",
    "ResolvedIndex",
    "ResolvedQuoteValue",
    "ResolvedSwaptionModel",
    "ResolvedVolSurface",
    "SwaptionModelRef",
    "SwaptionPriceRequest",
    "SwaptionPriceResponse",
    "SwaptionResult",
    "SwaptionTrade",
    "VolSurfaceRef",
]
