"""Pydantic v2 request / response models for ``POST /v1/price/swaps/inflation``.

Three layers, structurally analogous to
:mod:`quantra_orchestrator.pricing.equity_options.models` (the
closest multi-bundle reference: discount + dividend curves + vol
surface + spot). Differences vs. equity options:

* The two curves are both **rates-shaped** — no ``BlackVolSpec``-
  style payload check, just discount- and inflation-role curves
  loaded from the same ``app.curves`` table. Per per-entity-family error-code convention,
  the per-curve role lands in ``details`` on the shared
  ``swap_inflation_curve_resolution_failed`` 422 code.
* A new :class:`InflationIndexRef` carries the inflation index
  reference. The index sits in ``app.indices`` (kind="Inflation")
  and its body holds the historical CPI fixings + the engine-side
  conventions (calendar, day counter, frequency, observation /
  availability lags). The index is **not** MD-resolved — its
  ``fixings`` are historical CPI values, not vendor quotes (the no-MD-fallback rule
  applies: inflation **curves** flow through MD, but the
  inflation **index** body is consumed verbatim by the engine).
* The trade body discriminates between **zero-coupon** and
  **year-on-year** swaps via a ``swap_kind`` field (defaults to
  ``"zero_coupon"``). The endpoint dispatches to either
  :attr:`EngineRpc.PRICE_ZERO_COUPON_INFLATION_SWAP` or
  :attr:`EngineRpc.PRICE_YEAR_ON_YEAR_INFLATION_SWAP` (an early
  design used a single ``PRICE_SWAP_INFLATION`` placeholder, but the
  canonical enum has two RPCs and this module honors the enum).

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

_VALID_SWAP_KINDS: frozenset[str] = frozenset({"zero_coupon", "year_on_year"})

_REQUIRED_INLINE_CURVE_COUNT: int = 2
"""Inline mode requires both nominal + inflation curves."""


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
            'Optional role tag — ``"nominal"`` or '
            '``"inflation"``. When unset the assembler infers role '
            "by position (first → nominal, second → inflation) but "
            "routes that want the role pinned in ``details`` on a "
            "422 should set it explicitly."
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


class InflationIndexRef(BaseModel):
    """An inflation-index reference *or* a fully inline definition.

    The inflation index sits in ``app.indices`` with ``kind =
    'Inflation'`` and its body carries the engine-side conventions
    (calendar / day counter / frequency / lags) plus the historical
    fixings. Inline mode supplies ``name`` / ``id`` / ``currency`` /
    ``body`` directly.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = Field(
        default=None,
        description=(
            "Soft reference into ``app.indices``. When set, the "
            "orchestrator loads the row from the database and "
            "ignores any inline fields below. The orchestrator "
            "rejects rows with ``kind != 'Inflation'``."
        ),
    )
    name: str | None = None
    index_id: str | None = Field(
        default=None,
        description=(
            'Engine-side string id (e.g. ``"EUHICP"``) used in '
            "``InflationIndexSpec.id`` and referenced from the swap "
            "body's ``inflation_index_id``. Defaults to ``name`` "
            "when not pinned explicitly."
        ),
    )
    currency: str | None = None
    day_counter: str | None = None
    body: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _validate_branches(self) -> Self:
        if self.id is None and (self.name is None and self.index_id is None):
            msg = (
                "InflationIndexRef must provide either ``id`` "
                "(reference into app.indices) or an inline "
                "definition with at least ``name`` or "
                "``index_id``."
            )
            raise ValueError(msg)
        return self


class ResolvedCurve(BaseModel):
    """A curve after the assembler has loaded / collapsed the ref-vs-inline branches."""

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
            '``"nominal"`` or ``"inflation"`` — pinned by the '
            "assembler so 422 ``swap_inflation_curve_resolution_failed`` "
            "errors can carry the role in their ``details``."
        ),
    )
    currency: str | None = None
    day_counter: str | None = None
    helper_kind: str | None = None
    reference_date: date | None = None
    points: list[dict[str, Any]] = Field(default_factory=list)
    body: dict[str, Any] = Field(default_factory=dict)


class ResolvedInflationIndex(BaseModel):
    """An inflation index after the assembler has loaded / collapsed both branches.

    Mirrors the structure of :class:`ResolvedCurve` — ``id`` carries
    the originating ``app.indices`` row when applicable, ``name`` is
    the human-readable family name, ``index_id`` is the string id
    the engine wire shape uses (``InflationIndexSpec.id``), and
    ``body`` carries the JSONB block the assembler hands to
    ``engine_io.py`` to build :class:`InflationIndexSpecT`.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = Field(
        default=None,
        description=(
            "The originating ``app.indices`` row. ``None`` for purely-inline definitions."
        ),
    )
    name: str = Field(min_length=1)
    index_id: str = Field(
        min_length=1,
        description=(
            "Engine-side string id used in ``InflationIndexSpec.id`` "
            "and referenced from the swap body's "
            "``inflation_index_id``."
        ),
    )
    currency: str | None = None
    day_counter: str | None = None
    body: dict[str, Any] = Field(default_factory=dict)


class ResolvedQuoteValue(BaseModel):
    """One quote ID resolved to its value at the request's ``as_of``."""

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
            "a live MD service call."
        ),
    )


class InflationSwapTrade(BaseModel):
    """One inflation swap flowing through the concurrency seam.

    Carries the swap body shape verbatim (saved or inline) plus the
    optional ``swap_id`` / ``name`` so structured logs + the
    pricing-history hook can refer back to the originating row. The
    seam's ``T`` type parameter is bound to this class. Today every
    batch is one trade per the :class:`OneTradePerCall` default.

    The trade body's ``swap_kind`` discriminator (``"zero_coupon"``
    or ``"year_on_year"``) decides which engine RPC the encoder
    targets. The canonical enum has one RPC per kind and this model
    honors it.
    """

    model_config = ConfigDict(extra="forbid")

    swap_id: UUID | None = None
    name: str | None = None
    swap: dict[str, Any] = Field(default_factory=dict)


class AssembledInflationSwapRequest(BaseModel):
    """The full resolved pricing request the orchestrator would hand the engine.

    Echoed back in the response body and on the ``details`` of the
    error envelope on engine failure so operators can verify
    the resolution path completed before the engine call failed.
    """

    model_config = ConfigDict(extra="forbid")

    as_of: date
    snapshot_id: UUID | None = None
    nominal_curve_id: UUID | None = Field(
        default=None,
        description=(
            "Logical nominal-discount-curve identifier. ``None`` for purely-inline curves."
        ),
    )
    inflation_curve_id: UUID | None = Field(
        default=None,
        description=("Logical inflation-curve identifier. ``None`` for purely-inline curves."),
    )
    inflation_index_id: UUID | None = Field(
        default=None,
        description=(
            "Logical inflation-index identifier (the originating "
            "``app.indices`` row). ``None`` for purely-inline indices."
        ),
    )
    trade: InflationSwapTrade
    nominal_curve: ResolvedCurve
    inflation_curve: ResolvedCurve
    inflation_index: ResolvedInflationIndex
    resolved_quotes: list[ResolvedQuoteValue] = Field(default_factory=list)


class InflationSwapResult(BaseModel):
    """Engine response for one inflation swap, decoded from FlatBuffers.

    Mirrors :class:`ZeroCouponInflationSwapResponse` (ZCIIS) /
    :class:`YearOnYearInflationSwapResponse` (YYIIS):

    * ZCIIS — ``npv``, ``fair_rate``, ``fixed_leg_npv``,
      ``inflation_leg_npv``, ``fixed_leg_bps``.
    * YYIIS — same plus ``fair_spread`` and ``yoy_leg_*`` /
      ``yoy_leg_bps`` instead of ``inflation_leg_*``.

    The orchestrator-side typed model exposes the union of both
    fields (any field absent in a given response is ``None``);
    ``extras`` mirrors the engine's wire-naming verbatim so a future
    engine-side fix surfaces without a decoder change.
    """

    model_config = ConfigDict(extra="forbid")

    npv: float | None = None
    fair_rate: float | None = None
    fair_spread: float | None = None
    fixed_leg_bps: float | None = None
    fixed_leg_npv: float | None = None
    inflation_leg_npv: float | None = None
    yoy_leg_bps: float | None = None
    yoy_leg_npv: float | None = None
    swap_kind: str = Field(
        default="zero_coupon",
        description=(
            "Echoes the trade body's ``swap_kind`` discriminator so "
            "callers can branch on which leg shape the result "
            "carries without inspecting null fields."
        ),
    )
    extras: dict[str, Any] = Field(default_factory=dict)


class InflationSwapPriceRequest(BaseModel):
    """Endpoint input: either by-reference or fully inline.

    Validation rules:

    * Exactly one of ``swap_id`` or ``swap`` is set.
    * Inline mode (``swap`` set) must also supply ``curves`` (with
      at least two entries — nominal + inflation) and an
      ``inflation_index`` ref so the orchestrator can resolve every
      reference without consulting saved entities.
    * By-reference mode (``swap_id`` set) MAY supply any of those
      overrides.
    """

    model_config = ConfigDict(extra="forbid")

    swap_id: UUID | None = Field(
        default=None,
        description=(
            "ID of an ``app.swaps_inflation`` row owned by the "
            "caller. The orchestrator loads it via ``app_ro`` and "
            "uses its ``request`` JSONB as the trade body."
        ),
    )
    swap: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Inline inflation-swap body. The shape matches what "
            "the portal would have saved into "
            "``swaps_inflation.request`` — the full pricing-request "
            "payload. Mutually exclusive with ``swap_id``."
        ),
    )
    swap_kind: str | None = Field(
        default=None,
        description=(
            "Optional override for the trade body's ``swap_kind`` "
            'discriminator (``"zero_coupon"`` or '
            '``"year_on_year"``). When unset the assembler reads '
            "it off the trade body, falling back to "
            '``"zero_coupon"``.'
        ),
    )
    curves: list[CurveRef] | None = Field(
        default=None,
        description=(
            "Optional curve overrides. The first entry is treated "
            "as the nominal discount curve; the second is the "
            "inflation curve. Each entry may also pin a ``role`` "
            "explicitly. Required for inline mode."
        ),
    )
    inflation_index: InflationIndexRef | None = Field(
        default=None,
        description=(
            "Optional inflation-index override. Required for "
            "inline mode (the orchestrator will not invent an "
            "index); optional for by-reference mode (defaults to "
            "whatever the saved swap references)."
        ),
    )
    as_of: date = Field(
        description=(
            "Resolution date for every quote ID in the resolved "
            "curves. The orchestrator passes this through to the "
            "MD service via ``MdClient.resolve_quotes(..., as_of)``."
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
        if self.swap is not None:
            if self.curves is None or len(self.curves) < _REQUIRED_INLINE_CURVE_COUNT:
                msg = (
                    "Inline mode (``swap`` set) requires a "
                    "``curves`` list with at least two entries "
                    "(nominal discount curve first; inflation "
                    "curve second)."
                )
                raise ValueError(msg)
            if self.inflation_index is None:
                msg = "Inline mode (``swap`` set) requires an ``inflation_index`` override."
                raise ValueError(msg)
        if self.swap_kind is not None and self.swap_kind not in _VALID_SWAP_KINDS:
            msg = (
                f"``swap_kind`` must be one of {sorted(_VALID_SWAP_KINDS)}, got {self.swap_kind!r}."
            )
            raise ValueError(msg)
        return self


class InflationSwapPriceResponse(BaseModel):
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
    assembled_request: AssembledInflationSwapRequest = Field(
        description=(
            "The fully resolved request the orchestrator handed "
            "(or would have handed) the engine. Echoed on every "
            "response so callers can verify the resolution path."
        ),
    )
    result: InflationSwapResult


__all__ = [
    "AssembledInflationSwapRequest",
    "CurveRef",
    "InflationIndexRef",
    "InflationSwapPriceRequest",
    "InflationSwapPriceResponse",
    "InflationSwapResult",
    "InflationSwapTrade",
    "ResolvedCurve",
    "ResolvedInflationIndex",
    "ResolvedQuoteValue",
]
