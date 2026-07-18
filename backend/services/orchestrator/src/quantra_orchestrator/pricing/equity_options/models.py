"""Pydantic v2 request / response models for ``POST /v1/price/equity-option``.

Three layers, structurally analogous to
:mod:`quantra_orchestrator.pricing.swaption.models` (the closest
multi-bundle reference: curves + vol surface + model + the new
underlier-spot bundle). Differences vs. the swaption product:

* The vol-surface :attr:`kind` is constrained to ``"BlackVolSpec"`` —
  the equity-side payload type ``app.vol_surfaces.kind`` carries
  for an equity surface. The assembler enforces this at load time and
  raises :class:`equity_options.errors.EquityOptionVolSurfaceWrongKindError`
  when the referenced surface is a rates one
  (``SwaptionVolSpec`` / ``OptionletVolSpec``).
* A separate :class:`SpotQuoteRef` rides alongside the curves /
  surface so the underlier spot can be supplied either via an
  inline value (``{"value": 100.0}``), a canonical-id pointer for
  MD resolution (``{"canonical_id": "AAPL.SPOT"}``), or both
  (with the inline value used as the immediate result). Spot data
  is the only equity-options market-data leaf that does *not*
  flow through ``app.curves`` / ``app.vol_surfaces``, hence its own
  ref shape — but the walker still calls into the MD client for
  the canonical-id branch (equity options does NOT bypass
  MD; see ``equity_options.md_resolution`` for the contract).
* The trade body (``EquityOptionTrade.equity_option``) carries the
  payoff (Call / Put / cash-or-nothing / asset-or-nothing), exercise
  style (European / American / Bermudan), strike, quantity, and
  optional barrier feature. Loose ``dict[str, Any]`` —
  the engine's :class:`EquityOption` FlatBuffers shape is the
  source of truth; the orchestrator's typed model just gates
  request-shape validation past pydantic.

The duplication of :class:`CurveRef` / :class:`ResolvedCurve` /
:class:`ResolvedQuoteValue` between this module and the other
per-product packages is intentional — a shared ``pricing/_resolved/``
module is a candidate future refactor.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CurveRef(BaseModel):
    """A curve reference *or* a fully inline curve definition."""

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
    role: str | None = Field(
        default=None,
        description=(
            'Optional role tag — ``"discount"`` or ``"dividend"``. '
            "When unset the assembler infers role by position "
            "(first → discount, second → dividend) but routes that "
            "want the role pinned in ``details`` on a 422 should "
            "set it explicitly."
        ),
    )

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

    Mirrors the swaption-side shape but constrains :attr:`kind` to
    the equity-payload discriminator. Inline mode requires
    ``payload`` + ``kind``; saved mode loads the row via
    ``app_ro`` and the assembler enforces ``kind == "BlackVolSpec"``.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = Field(
        default=None,
        description=(
            "Soft reference into ``app.vol_surfaces``. When set, the "
            "orchestrator loads the row from the database and "
            "rejects the surface unless ``kind = 'BlackVolSpec'`` "
            "(see ``equity_options.errors.EquityOptionVolSurfaceWrongKindError``)."
        ),
    )
    name: str | None = None
    kind: str | None = Field(
        default=None,
        description=(
            "Equity options consume the ``BlackVolSpec`` payload "
            "shape — the value of ``app.vol_surfaces.kind`` for "
            "the row this ref points at, when supplied inline. "
            "Required when the surface is fully inline so the "
            "orchestrator can dispatch the correct engine bootstrap."
        ),
    )
    payload: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Inline vol-surface body. Same JSONB shape "
            "``app.vol_surfaces.payload`` carries for "
            "``BlackVolSpec`` — ``base.constant_vol`` for a flat "
            "surface, ``term_vols`` / ``surface_vols`` / "
            "``surface_prices`` for richer shapes."
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
                "Inline VolSurfaceRef must include ``kind`` "
                "(``BlackVolSpec`` for an equity surface)."
            )
            raise ValueError(msg)
        return self


class SpotQuoteRef(BaseModel):
    """Spot reference for the equity underlier.

    Two branches:

    * Inline literal — ``{"value": 102.5}``. The orchestrator
      resolves the spot to that value with no MD round-trip.
    * MD-resolved canonical id — ``{"canonical_id": "AAPL.SPOT"}``.
      The orchestrator hands the canonical id to the MD client
      (or the snapshot pin) just like a curve-point quote and
      uses the resolved value as the underlier spot.

    Both branches may be supplied together; the inline ``value``
    short-circuits the MD lookup but the canonical id is still
    propagated into the resolved-quotes echo (audit) so
    operators can confirm the shape that would have flowed live.
    """

    model_config = ConfigDict(extra="forbid")

    canonical_id: str | None = Field(
        default=None,
        description=(
            "Vendor-canonical id for the spot quote. When set, the "
            "orchestrator's MD walker resolves it to a value at the "
            "request's ``as_of`` (snapshot first, MD client second)."
        ),
    )
    value: float | None = Field(
        default=None,
        description=(
            "Inline spot value. When set, no MD lookup is "
            "performed; the canonical id (if any) is still echoed "
            "in the assembled-request response."
        ),
    )

    @model_validator(mode="after")
    def _validate_branches(self) -> Self:
        if self.canonical_id is None and self.value is None:
            msg = (
                "SpotQuoteRef must provide either ``canonical_id`` "
                "(MD-resolved) or an inline ``value``."
            )
            raise ValueError(msg)
        return self


class ResolvedCurve(BaseModel):
    """A curve after the assembler has loaded / collapsed the ref-vs-inline branches.

    Mirrors the swaption shape so the walker logic stays a name-only
    difference. ``role`` is populated for every curve so the per-entity-family
    refinement (same-bundle-type / different-role on one code with
    role in ``details``) has a stable contract.
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
    role: str = Field(
        min_length=1,
        description=(
            '``"discount"`` or ``"dividend"`` — pinned by the '
            "assembler so 422 ``equity_option_curve_resolution_failed`` "
            "errors can carry the role in their ``details``."
        ),
    )
    currency: str | None = None
    day_counter: str | None = None
    helper_kind: str | None = None
    reference_date: date | None = None
    points: list[dict[str, Any]] = Field(default_factory=list)
    body: dict[str, Any] = Field(default_factory=dict)


class ResolvedVolSurface(BaseModel):
    """A vol surface after the assembler has loaded / collapsed the ref-vs-inline branches.

    The :attr:`kind` invariant — ``"BlackVolSpec"`` for equity
    options — is enforced by the assembler at load time; reaching
    this point with any other kind is a programmer error.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = Field(
        default=None,
        description=(
            "The originating ``app.vol_surfaces`` row when the "
            "surface came from a ref. ``None`` for inline-only "
            "definitions."
        ),
    )
    name: str = Field(min_length=1)
    kind: str = Field(
        min_length=1,
        description=(
            "Mirrors ``app.vol_surfaces.kind``. The assembler "
            "rejects anything other than ``BlackVolSpec`` for the "
            "equity-options endpoint."
        ),
    )
    payload: dict[str, Any] = Field(default_factory=dict)


class ResolvedSpotQuote(BaseModel):
    """The spot reference after the assembler has collapsed both branches.

    ``canonical_id`` and ``value`` mirror the input ref's branches
    one-for-one (no walker side-effects yet — that happens in
    :mod:`equity_options.md_resolution`). Carrying both on the
    resolved object lets the assembled-request echo show the
    operator both what was asked for (canonical id) and what the
    immediate value is (when inline).
    """

    model_config = ConfigDict(extra="forbid")

    canonical_id: str | None = None
    value: float | None = None


class ResolvedQuoteValue(BaseModel):
    """One quote ID resolved to its value at the request's ``as_of``.

    Mirrors the swaption / swap_ir shape verbatim. ``from_snapshot``
    flags whether the value came from a user-pinned ``app.snapshots``
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
            "(reproducible across reruns); False when it came from "
            "a live MD service call (subject to upstream "
            "corrections)."
        ),
    )


class EquityOptionTrade(BaseModel):
    """One equity option flowing through the concurrency seam.

    Carries the option body shape verbatim (saved or inline) plus
    the optional ``equity_option_id`` / ``name`` so structured logs
    + the pricing-history hook can refer back to the originating
    row. The seam's ``T`` type parameter is bound to this class so
    per-call fan-out groups trades by equity surface via
    ``EngineBatch.shared_inputs[equity_surface_id]`` (keying
    field). Today every batch is one trade per the
    :class:`OneTradePerCall` default.
    """

    model_config = ConfigDict(extra="forbid")

    equity_option_id: UUID | None = None
    name: str | None = None
    equity_option: dict[str, Any] = Field(default_factory=dict)


class AssembledEquityOptionRequest(BaseModel):
    """The full resolved pricing request the orchestrator would hand the engine.

    Echoed back in the response body and on the ``details`` of the
    error envelope on engine failure so operators can verify
    the resolution path completed before the engine call failed.
    """

    model_config = ConfigDict(extra="forbid")

    as_of: date
    snapshot_id: UUID | None = None
    discount_curve_id: UUID | None = Field(
        default=None,
        description=(
            "Logical discount-curve identifier. ``None`` for "
            "purely-inline curves. Echoed for parity with the "
            "swap_ir / bonds shapes."
        ),
    )
    dividend_curve_id: UUID | None = Field(
        default=None,
        description=("Logical dividend-curve identifier. ``None`` for purely-inline curves."),
    )
    equity_surface_id: UUID | None = Field(
        default=None,
        description=(
            "Logical equity vol-surface identifier (the keying "
            "field for the reserved ``group_by_equity_surface`` "
            "policy). ``None`` for purely-inline surfaces."
        ),
    )
    trade: EquityOptionTrade
    discount_curve: ResolvedCurve
    dividend_curve: ResolvedCurve
    vol_surface: ResolvedVolSurface
    spot: ResolvedSpotQuote
    resolved_quotes: list[ResolvedQuoteValue] = Field(default_factory=list)


class EquityOptionResult(BaseModel):
    """Engine response for one equity option, decoded from FlatBuffers.

    Mirrors :class:`EquityOptionResponse` per
    ``equity_option_response.fbs`` — NPV, full Greek surface
    (delta, gamma, vega, theta, rho), implied volatility and the
    used spot / strike / settlement so callers can verify the
    engine's decision on the pricing inputs.
    """

    model_config = ConfigDict(extra="forbid")

    npv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    vega: float | None = None
    theta: float | None = None
    rho: float | None = None
    implied_volatility: float | None = None
    used_spot: float | None = None
    used_strike: float | None = None
    used_settlement: str | None = Field(
        default=None,
        description=(
            'Engine-side settlement (``"Physical"`` / ``"Cash"``) '
            "actually used; pinned alongside the request's settlement "
            "so callers can detect when the engine rewrote it (e.g. "
            "for an American option that the engine clamped to "
            "Cash)."
        ),
    )
    extras: dict[str, Any] = Field(default_factory=dict)


class EquityOptionPriceRequest(BaseModel):
    """Endpoint input: either by-reference or fully inline.

    Validation rules (enforced by the post-init validator below):

    * Exactly one of ``equity_option_id`` or ``equity_option`` is
      set.
    * Inline mode (``equity_option`` set) must also supply
      ``curves`` (with at least one entry), ``vol_surface`` and
      ``spot`` so the orchestrator can resolve every reference
      without consulting saved entities. Inline mode without a
      dividend curve is fine — the orchestrator falls back to the
      canonical ``flat_dividend_rate`` baked into the engine's
      :class:`BlackVolSpec`.
    * By-reference mode (``equity_option_id`` set) MAY supply any
      of those overrides to take precedence over the saved option's
      pricing block.
    """

    model_config = ConfigDict(extra="forbid")

    equity_option_id: UUID | None = Field(
        default=None,
        description=(
            "ID of an ``app.equity_options`` row owned by the "
            "caller. The orchestrator loads it via ``app_ro`` "
            "and uses its ``request`` JSONB as the trade body."
        ),
    )
    equity_option: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Inline equity-option body. The shape matches what "
            "the portal would have saved into "
            "``equity_options.request`` — the full pricing-request "
            "payload, including the ``option`` block (payoff / "
            "strike / exercise / quantity). Mutually exclusive "
            "with ``equity_option_id``."
        ),
    )
    curves: list[CurveRef] | None = Field(
        default=None,
        description=(
            "Optional curve overrides. The first entry is treated "
            "as the discount (risk-free) curve; the second (if "
            "provided) is the dividend-yield curve. Each entry may "
            "also pin a ``role`` explicitly. Required for inline "
            "mode; optional for by-reference mode."
        ),
    )
    vol_surface: VolSurfaceRef | None = Field(
        default=None,
        description=(
            "Optional vol-surface override (``BlackVolSpec``). "
            "Required for inline mode; optional for by-reference "
            "mode."
        ),
    )
    spot: SpotQuoteRef | None = Field(
        default=None,
        description=(
            "Optional spot override. Required for inline mode "
            "(the orchestrator will not invent a spot); optional "
            "for by-reference mode (defaults to whatever the "
            "saved option references)."
        ),
    )
    as_of: date = Field(
        description=(
            "Resolution date for every quote ID in the resolved "
            "curves + vol surface + spot. The orchestrator passes "
            "this through to the MD service via "
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
        if self.equity_option_id is None and self.equity_option is None:
            msg = (
                "Either ``equity_option_id`` (by reference) or "
                "``equity_option`` (inline) must be set."
            )
            raise ValueError(msg)
        if self.equity_option_id is not None and self.equity_option is not None:
            msg = "``equity_option_id`` and ``equity_option`` are mutually exclusive; pick one."
            raise ValueError(msg)
        if self.equity_option is not None:
            if self.curves is None or len(self.curves) == 0:
                msg = (
                    "Inline mode (``equity_option`` set) requires "
                    "a non-empty ``curves`` list (discount curve "
                    "first; optional dividend curve second)."
                )
                raise ValueError(msg)
            if self.vol_surface is None:
                msg = (
                    "Inline mode (``equity_option`` set) requires "
                    "a ``vol_surface`` override (``BlackVolSpec``)."
                )
                raise ValueError(msg)
            if self.spot is None:
                msg = (
                    "Inline mode (``equity_option`` set) requires "
                    "a ``spot`` override (inline value or "
                    "``canonical_id``)."
                )
                raise ValueError(msg)
        return self


class EquityOptionPriceResponse(BaseModel):
    """Endpoint output. Success path; failure paths emit the envelope."""

    model_config = ConfigDict(extra="forbid")

    pricing_history_id: str | None = Field(
        default=None,
        description=(
            "Best-effort ``app.pricing_history`` row id recorded "
            "for this call. ``None`` when history capture is "
            "disabled or the write did not complete."
        ),
    )
    assembled_request: AssembledEquityOptionRequest = Field(
        description=(
            "The fully resolved request the orchestrator handed "
            "(or would have handed) the engine. Echoed on every "
            "response so callers can verify the resolution path."
        ),
    )
    result: EquityOptionResult


__all__ = [
    "AssembledEquityOptionRequest",
    "CurveRef",
    "EquityOptionPriceRequest",
    "EquityOptionPriceResponse",
    "EquityOptionResult",
    "EquityOptionTrade",
    "ResolvedCurve",
    "ResolvedQuoteValue",
    "ResolvedSpotQuote",
    "ResolvedVolSurface",
    "SpotQuoteRef",
    "VolSurfaceRef",
]
