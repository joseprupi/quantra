"""Resolved-input → FlatBuffers ``Pricing`` translator.

This module is the faithful request-assembly layer: it consumes the
products' already-resolved Pydantic shapes (curves with quote ids still
attached, plus the ``canonical_id → value`` map market-data resolution
produced) and builds the engine's strongly-typed FlatBuffers ``Pricing``
graph from them — substituting quote values server-side so the engine
never sees a quote id.

Ground rules:

* Production request assembly builds ``Pricing`` from a typed
  :class:`ResolvedMarketData`; the ``build_canonical_*`` helpers survive
  only as test fixtures / defaults.
* :class:`ResolvedMarketData` reaches ``price_<product>_batch`` by
  route-closure capture, not via ``EngineBatch.shared_inputs``. See the
  "route-scope capture" note below.
* Per-trade curve references honor the resolved entity ids
  (``app.curves`` UUID as text, or the inline name), never ``CANONICAL_*``.
  :func:`resolved_curve_id` is the single id-derivation site.
* ``md_resolution`` resolves (``canonical_id → value``); the translator
  substitutes into the FB graph and drops ``quote_id``. A ``quote_id``
  with no resolved value raises :class:`QuoteResolutionError` (mapped to
  the product's ``*_quote_resolution_failed`` 422 by the caller); the
  translator never leaves a ``quote_id`` in the wire payload nor
  substitutes a default for an unresolved quote.
* Storage is loose; translation is strict on the helper / surface *kind*
  (an unknown kind rejects rather than falling back) and on the value
  source. The rates-curve and swaption vol-surface *minimal translatable
  contracts* below spell this out.

Route-scope capture (future-proofing, do not refactor now)
----------------------------------------------------------
:class:`ResolvedMarketData` is captured **once per request** at route scope
and passed into ``build_<product>_request(..., resolved=...)``. That is
correct for the ``OneTradePerCall`` policy and for the reserved
``group_by_<shared-bundle>`` policies, where every trade in a batch
shares one resolved bundle. A wave-two *portfolio* policy that batches trades
across **different** curve sets would need ``ResolvedMarketData`` keyed per
:class:`~quantra_orchestrator.pricing.concurrency.EngineBatch`, not per route.
Do not bake in a route-only assumption that blocks that — but do not build for
it here either.

Rates-curve minimal translatable contract
-----------------------------------------
Per curve (read off the product's ``ResolvedCurve``):

* ``id`` / ``name`` — the per-trade reference id. ``ResolvedCurve``
  always carries a ``name`` so an id is always derivable; never required to
  reject.
* ``reference_date`` — the curve's bootstrap reference date. Falls back to the
  request ``as_of`` when absent.
* ``day_counter`` (curve metadata) / ``body.interpolator`` /
  ``body.bootstrap_trait`` — read by FB enum *member name* when present and
  recognised; otherwise the documented default
  (``Actual365Fixed`` / ``LogLinear`` / ``Discount``). These are loose
  metadata; they never reject.

Per point (the strict part):

* ``point_type`` (**required**) — the helper kind discriminator. Missing or
  not on :data:`_HELPER_SPECS` → :class:`UnknownHelperKindError` (→ 422). No
  canonical fallback. A kind seen in the wild that is not mapped
  is an *operator escalation*, not a worker-invented translation.
* a **value source** (**required**) — either a ``quote_id`` that resolves
  against the MD map, or an inline value (``rate`` / ``price`` /
  ``futures_price``). An unresolved ``quote_id`` → :class:`QuoteResolutionError`
  (→ 422); neither present → :class:`CurveTranslationError` (→ 422).
* the structural fields each kind needs to bootstrap (tenor / dates) —
  **required**; missing → :class:`CurveTranslationError` (→ 422).
* the convention fields (calendar / day-counter / BDC / fixing days /
  frequencies) — read by FB enum member name when present, documented default
  otherwise. The live portal always sends these; the defaults are a
  robustness fallback for loose storage.
* any **extra** field is preserved-but-ignored (forward-compat).

Helper-kind map (the kinds the portal actually writes)
------------------------------------------------------
Enumerated from the portal's ``CurvePoint`` union and its
quote-substitution target fields:

================  ================  ===================================
portal point_type FB ``Point`` kind quote-substitution target field
================  ================  ===================================
DepositHelper     DepositHelper     ``rate``
SwapHelper        SwapHelper        ``rate``
FRAHelper         FRAHelper         ``rate``
FutureHelper      FutureHelper      ``futures_price`` (``rate`` if present)
BondHelper        BondHelper        ``price`` (``rate`` if present)
OISHelper         OISHelper         ``rate``
DatedOISHelper    DatedOISHelper    ``rate``
================  ================  ===================================

The FB ``Point`` union additionally defines ``ZeroRatePoint`` /
``TenorBasisSwapHelper`` / ``FxSwapHelper`` / ``CrossCcyBasisHelper``; the
portal does not emit them for rates curves, so they are intentionally **not**
mapped — they reject as unknown kinds until support is added deliberately.
Inflation helpers (``ZeroCouponInflationSwapHelper`` /
``YearOnYearInflationSwapHelper``) live on inflation curves and are handled
in the inflation section below.
"""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from math import isfinite
from typing import Any, Final, Protocol
from uuid import UUID

from quantra_common.engine_client._generated.quantra.BlackVolBaseSpec import (
    BlackVolBaseSpecT,
)
from quantra_common.engine_client._generated.quantra.BlackVolSpec import BlackVolSpecT
from quantra_common.engine_client._generated.quantra.BondHelper import BondHelperT
from quantra_common.engine_client._generated.quantra.CdsHelperConventions import (
    CdsHelperConventionsT,
)
from quantra_common.engine_client._generated.quantra.CdsQuote import CdsQuoteT
from quantra_common.engine_client._generated.quantra.CreditCurveSpec import (
    CreditCurveSpecT,
)
from quantra_common.engine_client._generated.quantra.CreditMarketData import (
    CreditMarketDataT,
)
from quantra_common.engine_client._generated.quantra.DatedOISHelper import (
    DatedOISHelperT,
)
from quantra_common.engine_client._generated.quantra.DepositHelper import DepositHelperT
from quantra_common.engine_client._generated.quantra.DiscountFactorPoint import (
    DiscountFactorPointT,
)
from quantra_common.engine_client._generated.quantra.enums.BootstrapTrait import (
    BootstrapTrait,
)
from quantra_common.engine_client._generated.quantra.enums.BusinessDayConvention import (
    BusinessDayConvention,
)
from quantra_common.engine_client._generated.quantra.enums.Calendar import Calendar
from quantra_common.engine_client._generated.quantra.enums.CdsHelperModel import (
    CdsHelperModel,
)
from quantra_common.engine_client._generated.quantra.enums.CdsQuoteType import (
    CdsQuoteType,
)
from quantra_common.engine_client._generated.quantra.enums.Compounding import (
    Compounding,
)
from quantra_common.engine_client._generated.quantra.enums.CPIInterpolationType import (
    CPIInterpolationType,
)
from quantra_common.engine_client._generated.quantra.enums.DateGenerationRule import (
    DateGenerationRule,
)
from quantra_common.engine_client._generated.quantra.enums.DayCounter import DayCounter
from quantra_common.engine_client._generated.quantra.enums.Frequency import Frequency
from quantra_common.engine_client._generated.quantra.enums.InflationCurveKind import (
    InflationCurveKind,
)
from quantra_common.engine_client._generated.quantra.enums.Interpolator import (
    Interpolator,
)
from quantra_common.engine_client._generated.quantra.enums.IrModelType import (
    IrModelType,
)
from quantra_common.engine_client._generated.quantra.enums.RateAveragingType import (
    RateAveragingType,
)
from quantra_common.engine_client._generated.quantra.enums.TimeUnit import TimeUnit
from quantra_common.engine_client._generated.quantra.enums.VolatilityType import (
    VolatilityType,
)
from quantra_common.engine_client._generated.quantra.enums.VolSurfaceShape import (
    VolSurfaceShape,
)
from quantra_common.engine_client._generated.quantra.EquityMarketData import (
    EquityMarketDataT,
)
from quantra_common.engine_client._generated.quantra.EquityUnderlyingSpec import (
    EquityUnderlyingSpecT,
)
from quantra_common.engine_client._generated.quantra.Fixing import FixingT
from quantra_common.engine_client._generated.quantra.ForwardRatePoint import (
    ForwardRatePointT,
)
from quantra_common.engine_client._generated.quantra.FRAHelper import FRAHelperT
from quantra_common.engine_client._generated.quantra.FutureHelper import FutureHelperT
from quantra_common.engine_client._generated.quantra.IndexDef import IndexDefT
from quantra_common.engine_client._generated.quantra.IndexRef import IndexRefT
from quantra_common.engine_client._generated.quantra.IndexType import IndexType
from quantra_common.engine_client._generated.quantra.InflationCurveSpec import (
    InflationCurveSpecT,
)
from quantra_common.engine_client._generated.quantra.InflationIndexSpec import (
    InflationIndexSpecT,
)
from quantra_common.engine_client._generated.quantra.InflationMarketData import (
    InflationMarketDataT,
)
from quantra_common.engine_client._generated.quantra.InflationPoint import (
    InflationPoint,
)
from quantra_common.engine_client._generated.quantra.InflationPointWrapper import (
    InflationPointWrapperT,
)
from quantra_common.engine_client._generated.quantra.IrVolBaseSpec import IrVolBaseSpecT
from quantra_common.engine_client._generated.quantra.ModelPayload import ModelPayload
from quantra_common.engine_client._generated.quantra.ModelSpec import ModelSpecT
from quantra_common.engine_client._generated.quantra.OISHelper import OISHelperT
from quantra_common.engine_client._generated.quantra.Period import PeriodT
from quantra_common.engine_client._generated.quantra.Point import Point
from quantra_common.engine_client._generated.quantra.PointsWrapper import PointsWrapperT
from quantra_common.engine_client._generated.quantra.Pricing import PricingT
from quantra_common.engine_client._generated.quantra.PricingOptions import (
    PricingOptionsT,
)
from quantra_common.engine_client._generated.quantra.QuoteKind import QuoteKind
from quantra_common.engine_client._generated.quantra.QuoteMatrix2D import (
    QuoteMatrix2DT,
)
from quantra_common.engine_client._generated.quantra.QuoteSpec import QuoteSpecT
from quantra_common.engine_client._generated.quantra.QuoteTensor3D import (
    QuoteTensor3DT,
)
from quantra_common.engine_client._generated.quantra.QuoteType import QuoteType
from quantra_common.engine_client._generated.quantra.RatesMarketData import (
    RatesMarketDataT,
)
from quantra_common.engine_client._generated.quantra.Schedule import ScheduleT
from quantra_common.engine_client._generated.quantra.SwapHelper import SwapHelperT
from quantra_common.engine_client._generated.quantra.SwaptionModelSpec import (
    SwaptionModelSpecT,
)
from quantra_common.engine_client._generated.quantra.SwaptionSabrCalibrateSpec import (
    SwaptionSabrCalibrateSpecT,
)
from quantra_common.engine_client._generated.quantra.SwaptionVolAtmMatrixSpec import (
    SwaptionVolAtmMatrixSpecT,
)
from quantra_common.engine_client._generated.quantra.SwaptionVolConstantSpec import (
    SwaptionVolConstantSpecT,
)
from quantra_common.engine_client._generated.quantra.SwaptionVolPayload import (
    SwaptionVolPayload,
)
from quantra_common.engine_client._generated.quantra.SwaptionVolSpec import (
    SwaptionVolSpecT,
)
from quantra_common.engine_client._generated.quantra.TermStructure import (
    TermStructureT,
)
from quantra_common.engine_client._generated.quantra.VolatilityMarketData import (
    VolatilityMarketDataT,
)
from quantra_common.engine_client._generated.quantra.VolPayload import VolPayload
from quantra_common.engine_client._generated.quantra.VolSurfaceSpec import (
    VolSurfaceSpecT,
)
from quantra_common.engine_client._generated.quantra.YearOnYearInflationSwapHelper import (
    YearOnYearInflationSwapHelperT,
)
from quantra_common.engine_client._generated.quantra.ZeroCouponInflationSwapHelper import (
    ZeroCouponInflationSwapHelperT,
)
from quantra_common.engine_client._generated.quantra.ZeroRatePoint import ZeroRatePointT
from quantra_orchestrator.pricing._fb_helpers import (
    DEFAULT_CDS_MODEL_ID,
    DEFAULT_EQUITY_MODEL_ID,
    build_canonical_cds_model,
    build_canonical_coupon_pricer,
    build_canonical_equity_model,
    make_period,
)

# ---------------------------------------------------------------------------
# Structural typing — the resolved shapes the translator reads
# ---------------------------------------------------------------------------
# The translator is shared across the six Phase-3 products, each of which
# defines its own ``ResolvedCurve`` / ``ResolvedQuoteValue`` Pydantic models.
# Rather than import any one product's models (which would couple the shared
# translator to swap_ir), we type against read-only Protocols. Pydantic model
# instances satisfy them structurally.


class CurveRole(StrEnum):
    """The role a resolved curve plays in a per-trade reference.

    The assemblers tag each resolved curve with its role rather than handing
    the route a flat positional list; :class:`ResolvedMarketData` carries the
    ``role → resolved-curve-id`` map and each ``_build_one_<product>`` reads the
    reference slot it needs by role. This replaces the interim positional
    convention used previously (``curve[0]=discount, curve[1]=forwarding``).

    A ``str`` enum so a role doubles as a stable dict key and reads naturally in
    structured logs / 422 details.
    """

    DISCOUNT = "discount"
    FORWARDING = "forwarding"
    PROJECTION = "projection"
    NOMINAL = "nominal"
    INFLATION = "inflation"
    DIVIDEND = "dividend"


class ResolvedCurveLike(Protocol):
    """The read surface :func:`translate_curve` needs off a product ``ResolvedCurve``."""

    @property
    def id(self) -> UUID | None: ...

    @property
    def name(self) -> str: ...

    @property
    def day_counter(self) -> str | None: ...

    @property
    def reference_date(self) -> date | None: ...

    @property
    def points(self) -> Sequence[Mapping[str, Any]]: ...

    @property
    def body(self) -> Mapping[str, Any]: ...


class ResolvedQuoteValueLike(Protocol):
    """The read surface the quote-substitution map needs off a ``ResolvedQuoteValue``."""

    @property
    def canonical_id(self) -> str: ...

    @property
    def value(self) -> float: ...


class ResolvedVolSurfaceLike(Protocol):
    """The read surface :func:`translate_vol_surface` needs off a product ``ResolvedVolSurface``.

    Defined for the swaption vertical; the equity surface satisfies the
    same shape structurally — both carry ``kind`` (the ``app.vol_surfaces.kind``
    discriminator) and a loose ``payload`` body.
    """

    @property
    def id(self) -> UUID | None: ...

    @property
    def name(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def payload(self) -> Mapping[str, Any]: ...


class ResolvedModelLike(Protocol):
    """The read surface :func:`translate_model` needs off a product resolved model.

    Defined for the swaption ``ResolvedSwaptionModel``; the cds / equity model
    shapes satisfy it structurally — ``kind`` is the model
    discriminator and ``payload`` carries the (non-MD-resolved) config knobs.
    """

    @property
    def id(self) -> UUID | None: ...

    @property
    def name(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def payload(self) -> Mapping[str, Any]: ...


class ResolvedCreditCurveLike(Protocol):
    """The read surface :func:`translate_credit_curve` needs off a ``ResolvedCreditCurve``.

    The credit curve is **not** MD-resolved: its hazard / par-spread
    inputs are inline literals the assembler already validated, never
    ``quote_id``s. The translator reads the body to build a faithful
    :class:`CreditCurveSpec` whose id honours the resolved entity and
    whose hazard inputs are the supplied ``flat_hazard_rate`` (flat path) or
    inline ``par-spread`` / ``upfront`` quotes (engine bootstrap path), never
    the canonical 2 % / 40 % fixture.
    """

    @property
    def id(self) -> UUID | None: ...

    @property
    def name(self) -> str: ...

    @property
    def recovery_rate(self) -> float: ...

    @property
    def body(self) -> Mapping[str, Any]: ...


class ResolvedIndexLike(Protocol):
    """The read surface :func:`translate_index` needs off a product ``ResolvedIndex``.

    The float-leg / projection index a swap_ir float leg and a bonds-floating
    projection reference. The body is **not** MD-resolved (its tenor / calendar
    / fixing-days knobs are pure config); the translator reads it to build
    a faithful :class:`IndexDef` whose id honours the resolved entity,
    replacing the interim hardcoded Euribor-6M placeholder.
    """

    @property
    def id(self) -> UUID | None: ...

    @property
    def name(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def currency(self) -> str | None: ...

    @property
    def calendar(self) -> str | None: ...

    @property
    def day_counter(self) -> str | None: ...

    @property
    def body(self) -> Mapping[str, Any]: ...


class ResolvedSpotQuoteLike(Protocol):
    """The read surface :func:`translate_spot_quote` needs off a ``ResolvedSpotQuote``.

    The equity underlier spot. Two value sources mirror the product's
    ``SpotQuoteRef`` branches: an inline ``value`` short-circuits MD, or a
    ``canonical_id`` that resolves against the MD map (equity options does
    not bypass MD). The translator writes the resolved value into the
    underlier-spot :class:`QuoteSpec` and honours the resolved id; a
    ``canonical_id`` with no resolved value is a 422, never a silent default
    .
    """

    @property
    def canonical_id(self) -> str | None: ...

    @property
    def value(self) -> float | None: ...


class ResolvedInflationIndexLike(Protocol):
    """The read surface :func:`translate_inflation_index` needs off a ``ResolvedInflationIndex``.

    The inflation index is **not** MD-resolved: its body carries the
    historical CPI fixings + engine conventions (calendar / frequency / lags)
    consumed verbatim, never ``quote_id``s. ``index_id`` is the engine-facing
    string id (e.g. ``"EUHICP"``) the swap body's ``inflation_index_id`` and the
    ``InflationCurveSpec.indexId`` both reference; the inflation **curve**
    that bootstraps over it still flows through MD (its helper ``quoteValue``s
    substitute).
    """

    @property
    def id(self) -> UUID | None: ...

    @property
    def name(self) -> str: ...

    @property
    def index_id(self) -> str: ...

    @property
    def currency(self) -> str | None: ...

    @property
    def day_counter(self) -> str | None: ...

    @property
    def body(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ResolvedMarketData:
    """Everything the translator needs to build one request's ``Pricing`` graph.

    Captured once per request at route scope and passed into the
    widened ``build_<product>_request(..., resolved=...)``. Frozen + slotted so
    it is a cheap, immutable value safe to close over in the route's
    ``price_batch`` lambda.

    Every request populates :attr:`as_of`, :attr:`curves` and :attr:`quotes`.
    The remaining fields are per-product: each product fills the field(s) it
    resolves and the translator branches on what is present, so the container
    is extensible *without widening the seam*.
    """

    as_of: str
    curves: tuple[ResolvedCurveLike, ...]
    quotes: tuple[ResolvedQuoteValueLike, ...]
    # ``role → resolved-curve-id`` map. The translator builds one
    # ``rates.curves[]`` entry per curve in :attr:`curves` (faithful, no drop);
    # each ``_build_one_<product>`` reads the reference slot it needs by role
    # (:meth:`curve_id_for_role`), replacing the older positional convention.
    curve_roles: Mapping[CurveRole, str] = field(default_factory=dict)
    # The resolved float-leg / projection index. When present its faithful
    # ``IndexDef`` replaces the interim hardcoded Euribor-6M placeholder under
    # ``rates.indices`` and the per-trade index ref honours its id.
    index: ResolvedIndexLike | None = None
    # The resolved vol surfaces + models (swaption / equity).
    vol_surfaces: tuple[ResolvedVolSurfaceLike, ...] = ()
    models: tuple[ResolvedModelLike, ...] = ()
    # The resolved credit curve (cds). When present its faithful
    # ``CreditCurveSpec`` lands under ``credit.credit_curves`` (flat-hazard or
    # inline par-spread bootstrap) and the per-trade ``creditCurveId`` honours
    # its resolved id, never the canonical 2 % / 40 % fixture.
    credit_curve: ResolvedCreditCurveLike | None = None
    # The resolved inflation index (swaps_inflation). When present its
    # faithful ``InflationIndexSpec`` lands under
    # ``Pricing.inflation.inflationIndices`` (body consumed verbatim) and the
    # per-trade ``inflationIndexId`` + the inflation curve's ``indexId`` honour its
    # resolved ``index_id``, never ``CANONICAL_INFLATION_INDEX_ID``. Index
    # presence is the inflation-bundle discriminator.
    inflation_index: ResolvedInflationIndexLike | None = None
    # The resolved inflation curve (rates-shaped, ZCIIS/YYIIS helper points).
    # Translated into ``InflationCurveSpec`` under
    # ``Pricing.inflation.inflationCurves`` with quote substitution; kept
    # **separate** from :attr:`curves` (which feed ``rates.curves``) because its
    # helper points are inflation-swap helpers, not rates helpers. The ZCIIS-vs-
    # YYIIS helper kind is chosen by the ``swap_kind`` discriminator the engine_io
    # reads off ``shared_inputs`` and passes to
    # :func:`build_pricing_from_resolved`, never carried in this container.
    inflation_curve: ResolvedCurveLike | None = None
    # The resolved equity underlier spot (equity_options). When present the
    # translator emits the underlier-spot ``QuoteSpec`` under ``Pricing.quotes``
    # (faithful value), the default equity model under ``volatility.models``
    # and the ``EquityUnderlyingSpec`` under ``EquityMarketData``; the per-trade
    # spot / underlier ids honour the resolved entity. Spot presence is the
    # equity-bundle discriminator (spot is equity-only today).
    spot: ResolvedSpotQuoteLike | None = None

    def curve_id_for_role(self, role: CurveRole) -> str | None:
        """Return the resolved engine-facing curve id assigned to ``role``.

        ``None`` when the assembler did not tag that role (the caller decides
        the fallback — e.g. swap_ir reuses the discount curve for forwarding).
        """

        return self.curve_roles.get(role)

    @property
    def forwarding_index_id(self) -> str:
        """The id the float-leg / projection index ref points at.

        The resolved index's id when one was supplied, else the interim default
        forwarding-index id (the documented placeholder for the not-yet-resolved
        case; see :data:`DEFAULT_FORWARDING_INDEX_ID`).
        """

        return (
            resolved_index_id(self.index) if self.index is not None else DEFAULT_FORWARDING_INDEX_ID
        )


# ---------------------------------------------------------------------------
# Neutral translation errors
# ---------------------------------------------------------------------------
# The translator is product-agnostic, so it raises *neutral* exceptions and
# lets each product's ``engine_io.py`` map them onto that product's existing
# 422 codes (``<product>_curve_resolution_failed`` /
# ``<product>_quote_resolution_failed``). This keeps the catalog unchanged
# (no new codes) while the shared translator stays free of FastAPI / product
# imports.


class TranslationError(Exception):
    """Base for every faithful-translation failure (always maps to a 422)."""


class CurveTranslationError(TranslationError):
    """A resolved curve cannot be translated into a bootstrappable FB graph.

    Maps to the product's ``<product>_curve_resolution_failed`` (422). The
    optional ``details`` mirror the structured-context shape the product 422s
    already carry.
    """

    def __init__(self, reason: str, *, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


class UnknownHelperKindError(CurveTranslationError):
    """A curve point's ``point_type`` is missing or not on the translator's map.

    A :class:`CurveTranslationError` subclass (so callers that only catch the
    base still map it to ``*_curve_resolution_failed``) — but distinct so a
    caller / test can recognise the "unknown kind, escalate" case.
    """

    def __init__(self, kind: object) -> None:
        super().__init__(
            f"Unknown curve helper kind {kind!r}; the translator maps only "
            f"{', '.join(sorted(_HELPER_SPECS))}. An unmapped kind is an "
            "operator escalation; there is no default fallback.",
            details=[{"helper_kind": kind}],
        )
        self.kind = kind


class IndexRegistrationError(CurveTranslationError):
    """A curve helper references an index id with no registrable ``IndexDef``.

    The engine's ``IndexRegistry`` is keyed by ``IndexDef.id`` and resolves every
    helper ref (``OISHelper.overnightIndex`` / ``SwapHelper.floatIndex``). When a
    helper references an id that is neither the resolved instrument index, the
    default forwarding index, nor a known overnight index in the orchestrator's
    catalog (:data:`_KNOWN_OVERNIGHT_INDICES`), the orchestrator cannot emit a
    faithful ``IndexDef`` for it — and sending a *bogus* placeholder would mis-
    price silently (the failure mode). So it raises this actionable 422
    instead of letting the request reach the engine and fail with an opaque
    ``NOT_FOUND: Unknown index id: <id>`` (the error-quality defect this
    closes). A :class:`CurveTranslationError` subclass so a base-only catch maps
    it to the product's ``<product>_curve_resolution_failed`` 422 (no new error
    code needed; the ``details`` name the unregistered id + the known catalog).
    """

    def __init__(self, index_id: str, known_index_ids: Sequence[str]) -> None:
        known = list(known_index_ids)
        super().__init__(
            f"Curve helper references index id {index_id!r} but no IndexDef "
            "could be registered for it: it is neither the resolved instrument "
            "index, the default forwarding index, nor a known overnight or IBOR "
            f"forwarding index ({', '.join(known) or 'none'}). Resolve the index "
            "on the request or add it to the orchestrator's known-index catalog; "
            "the "
            "engine cannot bootstrap a curve against an unregistered index "
            "(it would otherwise surface as an opaque engine NOT_FOUND).",
            details=[{"unregistered_index_id": index_id, "known_index_ids": known}],
        )
        self.index_id = index_id


class QuoteResolutionError(TranslationError):
    """One or more ``quote_id``s on the curves had no resolved MD value.

    Maps to the product's ``<product>_quote_resolution_failed`` (422). Collects
    every missing canonical id so the client patches them in one pass (mirrors
    ``md_resolution``'s batch semantics).
    """

    def __init__(self, missing_canonical_ids: Sequence[str]) -> None:
        self.missing_canonical_ids = list(missing_canonical_ids)
        super().__init__(
            f"{len(self.missing_canonical_ids)} quote id(s) could not be "
            "substituted into the curve / surface points."
        )


class VolSurfaceTranslationError(TranslationError):
    """A resolved vol surface cannot be translated into a bootstrappable FB spec.

    Maps to the product's ``<product>_surface_resolution_failed`` (422) — the
    surface-side analogue of :class:`CurveTranslationError`, kept distinct so a
    caller / test can branch on which resolved input failed. The optional
    ``details`` mirror the structured-context shape the product 422s carry.
    """

    def __init__(self, reason: str, *, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


class UnknownVolSurfaceKindError(VolSurfaceTranslationError):
    """A vol surface's ``kind`` is not one the translator maps.

    A :class:`VolSurfaceTranslationError` subclass (so a base-only catch still
    maps it to ``*_surface_resolution_failed``) — but distinct so a caller /
    test can recognise the "unknown kind, escalate" case. No
    canonical fallback: an unmapped surface kind is an operator escalation.
    """

    def __init__(self, kind: object) -> None:
        super().__init__(
            f"Unknown vol-surface kind {kind!r}; the translator maps only "
            f"{', '.join(sorted(_VOL_SURFACE_KINDS))}. An unmapped kind is an "
            "operator escalation; there is no default fallback.",
            details=[{"vol_surface_kind": kind}],
        )
        self.kind = kind


class SpotTranslationError(TranslationError):
    """A resolved equity spot cannot be translated into an underlier ``QuoteSpec``.

    Maps to the product's ``equity_option_spot_resolution_failed`` (422) — the
    spot-side analogue of :class:`CurveTranslationError`, kept distinct so a
    caller / test can branch on which resolved input failed. Fires when the
    resolved spot carries neither an inline ``value`` nor a ``canonical_id``
    (the product's ``SpotQuoteRef`` validator already rejects that upstream —
    this is the belt-and-braces translator copy). An unresolved ``canonical_id``
    is a :class:`QuoteResolutionError`, not this. The optional ``details`` mirror
    the structured-context shape the equity 422s carry.
    """

    def __init__(self, reason: str, *, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


class CreditCurveTranslationError(TranslationError):
    """A resolved credit curve cannot be translated into a bootstrappable FB spec.

    Maps to the product's ``cds_credit_curve_resolution_failed`` (422) — the
    credit-side analogue of :class:`CurveTranslationError`, kept distinct so a
    caller / test can branch on which resolved input failed. Credit curves
    bypass MD, so this never wraps a quote-resolution miss; it fires when
    the body carries neither a usable ``flat_hazard_rate`` nor inline
    par-spread / upfront quotes (the assembler already rejects those upstream —
    this is the belt-and-braces translator copy). The optional ``details``
    mirror the structured-context shape the cds 422s carry.
    """

    def __init__(self, reason: str, *, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


class CreditCurveInvariantError(CreditCurveTranslationError):
    """A credit curve pairs a non-zero ``flat_hazard_rate`` with inline quotes.

    The HARD invariant: the engine's ``PriceCDS`` bootstraps a
    non-flat curve from the inline par-spread / upfront quotes **only** when
    ``flat_hazard_rate == 0``; a non-zero ``flat_hazard_rate`` silently
    overrides the quotes and the engine prices a flat curve instead. Emitting
    both would therefore discard the caller's term structure with no error —
    exactly the silent-fixture failure mode this module exists to kill. The
    translator
    never produces this pairing (the bootstrap branch pins
    ``flat_hazard_rate = 0``); this guard is the belt-and-braces assertion that
    a future refactor can't reintroduce it. A
    :class:`CreditCurveTranslationError` subclass so a base-only catch still
    maps it to ``cds_credit_curve_resolution_failed``.
    """

    def __init__(self, flat_hazard_rate: float, quote_count: int) -> None:
        super().__init__(
            f"Credit curve supplies {quote_count} inline quote(s) with a "
            f"non-zero flat_hazard_rate={flat_hazard_rate!r}; the engine would "
            "silently discard the quotes and bootstrap a flat curve. "
            "An inline-quote credit curve MUST emit flat_hazard_rate=0 "
            "so the engine bootstraps the supplied term structure.",
            details=[{"flat_hazard_rate": flat_hazard_rate, "quote_count": quote_count}],
        )
        self.flat_hazard_rate = flat_hazard_rate
        self.quote_count = quote_count


class InflationIndexTranslationError(TranslationError):
    """A resolved inflation index cannot be translated into an ``InflationIndexSpec``.

    Maps to the product's ``swap_inflation_index_resolution_failed`` (422) — the
    index-side analogue of :class:`CurveTranslationError`, kept distinct so a
    caller / test can branch on which resolved input failed. The inflation index
    bypasses MD, so this never wraps a quote-resolution miss; it fires when
    the index body carries a malformed ``fixings`` block (not a list, or an entry
    missing a ``date`` / numeric ``value``). The optional ``details`` mirror the
    structured-context shape the swaps_inflation 422s carry. No canonical fallback
    : a malformed index is an operator fix, not a silent default.
    """

    def __init__(self, reason: str, *, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


# ---------------------------------------------------------------------------
# Float-leg / projection index (resolved; interim default otherwise)
# ---------------------------------------------------------------------------
# A vanilla swap's floating leg and a floating bond's projection both reference
# an ``IndexDef`` that must be present in ``Pricing.rates.indices``: when the
# request
# resolves an index (``ResolvedMarketData.index``), :func:`translate_index`
# builds it faithfully and the per-trade ref honours its id. When no
# index was resolved the translator still emits the one documented default
# Euribor-6M forwarding index below — a structural placeholder for a
# not-yet-resolved entity (clearly flagged), *not* a silent canonical fallback
# for a resolvable input. Curves / quotes / the resolved index are always
# translated faithfully.
# index minimal translatable contract
# ------------------------------------------
# Per resolved index (read off the product's ``ResolvedIndex``):
# * ``id`` / ``name`` — the per-trade reference id and the engine index
#   family name. ``ResolvedIndex`` always carries a ``name``; never rejects.
# * ``kind`` (loose) — ``IborIndex`` / ``OvernightIndex`` mapped to the FB
#   ``IndexType`` member; an unrecognised kind defaults to ``Ibor`` (loose
#   metadata, not a strict helper kind — index kinds are a small closed set the
#   engine validates downstream, so the translator does not 422 on them).
# * ``body`` convention fields (tenor / fixing-days / calendar / BDC /
#   day-counter / end-of-month / currency) — read by FB enum member name /
#   value when present, documented default otherwise. The live portal always
#   sends these; the defaults are a robustness fallback for loose storage.
# * any **extra** field is preserved-but-ignored (forward-compat).

DEFAULT_FORWARDING_INDEX_ID: Final[str] = "forwarding_index"
"""``IndexDef.id`` the interim default forwarding index is published under."""

_INDEX_TYPE_KINDS: Final[dict[str, int]] = {
    "IborIndex": IndexType.Ibor,
    "Ibor": IndexType.Ibor,
    "OvernightIndex": IndexType.Overnight,
    "Overnight": IndexType.Overnight,
}

_INDEX_NAME_KEYS: Final[tuple[str, ...]] = ("name", "family_name", "index_name")


def _build_default_forwarding_index() -> IndexDefT:
    """Build the interim default Euribor-6M forwarding index (used when unresolved)."""

    idx = IndexDefT()
    idx.id = DEFAULT_FORWARDING_INDEX_ID
    idx.name = "Euribor"
    idx.indexType = IndexType.Ibor
    idx.tenor = make_period(6, TimeUnit.Months)
    idx.fixingDays = 2
    idx.calendar = Calendar.TARGET
    idx.businessDayConvention = BusinessDayConvention.ModifiedFollowing
    idx.dayCounter = DayCounter.Actual360
    idx.endOfMonth = False
    idx.currency = "EUR"
    return idx


@dataclass(frozen=True, slots=True)
class _KnownOvernightIndex:
    """One catalog entry: the conventions a known overnight index registers with.

    Keyed by the engine-facing id string a curve helper references (e.g.
    ``"ESTR"``). The conventions mirror the engine's contract fixture
    (``tests/contract/ql_reference.py``) so the orchestrator-built ``IndexDef``
    matches what the engine expects when it constructs the QuantLib
    ``OvernightIndex`` dynamically from ``rates.indices``.
    """

    name: str
    calendar: int
    day_counter: int
    currency: str
    fixing_days: int = 0


# known overnight indices a curve helper may reference by id
# (``OISHelper.overnightIndex`` / ``DatedOISHelper.overnightIndex``). When a
# helper references one of these and no resolved/explicit IndexDef already
# carries that id, the translator registers a faithful IndexDef from this
# catalog so the engine can resolve it (instead of failing with an opaque
# ``NOT_FOUND: Unknown index id: ESTR``). ESTR conventions match the engine's
# ``EUR_ESTR`` contract fixture; SOFR / SONIA follow the same standard pattern.
_KNOWN_OVERNIGHT_INDICES: Final[dict[str, _KnownOvernightIndex]] = {
    "ESTR": _KnownOvernightIndex(
        name="ESTR",
        calendar=Calendar.TARGET,
        day_counter=DayCounter.Actual360,
        currency="EUR",
        fixing_days=0,
    ),
    "SOFR": _KnownOvernightIndex(
        name="SOFR",
        calendar=Calendar.UnitedStates,
        day_counter=DayCounter.Actual360,
        currency="USD",
        fixing_days=0,
    ),
    "SONIA": _KnownOvernightIndex(
        name="SONIA",
        calendar=Calendar.UnitedKingdom,
        day_counter=DayCounter.Actual365Fixed,
        currency="GBP",
        fixing_days=0,
    ),
}


@dataclass(frozen=True, slots=True)
class _KnownIborIndex:
    """One catalog entry: the conventions a known IBOR forwarding index registers with.

    Keyed by the engine-facing id string a ``SwapHelper.floatIndex`` references
    (e.g. ``"EURIBOR_6M"``). Mirrors :class:`_KnownOvernightIndex` but for a
    term IBOR-style projection index, so it carries the extra fields an
    :class:`IndexType.Ibor` ``IndexDef`` needs that an overnight index does not:
    an explicit ``tenor`` (an overnight index is always 1D) and a
    ``business_day_convention``.  The EURIBOR conventions are the exact ones the
    default forwarding index (:func:`_build_default_forwarding_index`) carries;
    the USD/GBP LIBOR conventions are QuantLib's standard money-market family
    conventions (not invented).
    """

    name: str
    tenor_number: int
    tenor_unit: int
    calendar: int
    day_counter: int
    currency: str
    business_day_convention: int = BusinessDayConvention.ModifiedFollowing
    fixing_days: int = 2


# known IBOR *forwarding* indices a curve helper may reference by id
# (``SwapHelper.floatIndex``). Symmetric to :data:`_KNOWN_OVERNIGHT_INDICES` but
# for term IBOR projection indices: before this a EUR (or USD/GBP) discount /
# projection curve whose ``SwapHelper`` points carried ``float_index:{id:
# "EURIBOR_6M"}`` failed translation with :class:`IndexRegistrationError`
# ("no IndexDef could be registered … nor a known overnight index"), because the
# id is neither the resolved instrument index, the default forwarding index, nor
# an overnight index — so swaption / vol-surface / curve-preview flows that bind a
# EUR curve aborted before ever reaching the engine. The EURIBOR conventions are
# byte-identical to the demo ``app.indices`` rows + the default forwarding index
# (TARGET, Actual360, ModifiedFollowing, 2 fixing days, EUR); the USD/GBP LIBOR
# conventions are QuantLib's standard family conventions.
_KNOWN_IBOR_INDICES: Final[dict[str, _KnownIborIndex]] = {
    "EURIBOR_1M": _KnownIborIndex(
        name="Euribor",
        tenor_number=1,
        tenor_unit=TimeUnit.Months,
        calendar=Calendar.TARGET,
        day_counter=DayCounter.Actual360,
        currency="EUR",
    ),
    "EURIBOR_3M": _KnownIborIndex(
        name="Euribor",
        tenor_number=3,
        tenor_unit=TimeUnit.Months,
        calendar=Calendar.TARGET,
        day_counter=DayCounter.Actual360,
        currency="EUR",
    ),
    "EURIBOR_6M": _KnownIborIndex(
        name="Euribor",
        tenor_number=6,
        tenor_unit=TimeUnit.Months,
        calendar=Calendar.TARGET,
        day_counter=DayCounter.Actual360,
        currency="EUR",
    ),
    "EURIBOR_12M": _KnownIborIndex(
        name="Euribor",
        tenor_number=12,
        tenor_unit=TimeUnit.Months,
        calendar=Calendar.TARGET,
        day_counter=DayCounter.Actual360,
        currency="EUR",
    ),
    "USD_LIBOR_3M": _KnownIborIndex(
        name="USDLibor",
        tenor_number=3,
        tenor_unit=TimeUnit.Months,
        calendar=Calendar.UnitedStates,
        day_counter=DayCounter.Actual360,
        currency="USD",
    ),
    "USD_LIBOR_6M": _KnownIborIndex(
        name="USDLibor",
        tenor_number=6,
        tenor_unit=TimeUnit.Months,
        calendar=Calendar.UnitedStates,
        day_counter=DayCounter.Actual360,
        currency="USD",
    ),
    "GBP_LIBOR_3M": _KnownIborIndex(
        name="GBPLibor",
        tenor_number=3,
        tenor_unit=TimeUnit.Months,
        calendar=Calendar.UnitedKingdom,
        day_counter=DayCounter.Actual365Fixed,
        currency="GBP",
        fixing_days=0,
    ),
    "GBP_LIBOR_6M": _KnownIborIndex(
        name="GBPLibor",
        tenor_number=6,
        tenor_unit=TimeUnit.Months,
        calendar=Calendar.UnitedKingdom,
        day_counter=DayCounter.Actual365Fixed,
        currency="GBP",
        fixing_days=0,
    ),
}

# The helper kinds that carry an index ref + the body field each reads (mirrors
# the per-kind builders: ``_build_swap`` reads ``float_index``; ``_build_ois`` /
# ``_build_dated_ois`` read ``overnight_index``). Used to collect every index id
# the curve helpers reference so each is registered in ``rates.indices``.
_HELPER_INDEX_REF_FIELDS: Final[dict[str, str]] = {
    "SwapHelper": "float_index",
    "OISHelper": "overnight_index",
    "DatedOISHelper": "overnight_index",
}


def _build_known_overnight_index(index_id: str, known: _KnownOvernightIndex) -> IndexDefT:
    """Build a faithful overnight ``IndexDef`` from a catalog entry.

    Reuses the same ``IndexDefT`` assembly the resolved-index / default-index
    paths use (rather than hand-rolling a second encoder), under the *same id
    string the helper references* so the engine's id-keyed registry resolves it.
    """

    idx = IndexDefT()
    idx.id = index_id
    idx.name = known.name
    idx.indexType = IndexType.Overnight
    idx.tenor = make_period(1, TimeUnit.Days)
    idx.fixingDays = known.fixing_days
    idx.calendar = known.calendar
    idx.businessDayConvention = BusinessDayConvention.ModifiedFollowing
    idx.dayCounter = known.day_counter
    idx.endOfMonth = False
    idx.currency = known.currency
    return idx


def _build_known_ibor_index(index_id: str, known: _KnownIborIndex) -> IndexDefT:
    """Build a faithful term-IBOR ``IndexDef`` from a catalog entry.

    Symmetric to :func:`_build_known_overnight_index` but emits an
    :data:`IndexType.Ibor` def with the catalog's explicit tenor + business-day
    convention, under the *same id string the helper references* so the engine's
    id-keyed registry resolves it. The engine's ``build_ibor_index`` constructs a
    generic ``ql.IborIndex`` from exactly these fields (name, tenor, fixingDays,
    currency, calendar, bdc, eom, dayCounter).
    """

    idx = IndexDefT()
    idx.id = index_id
    idx.name = known.name
    idx.indexType = IndexType.Ibor
    idx.tenor = make_period(known.tenor_number, known.tenor_unit)
    idx.fixingDays = known.fixing_days
    idx.calendar = known.calendar
    idx.businessDayConvention = known.business_day_convention
    idx.dayCounter = known.day_counter
    idx.endOfMonth = False
    idx.currency = known.currency
    return idx


def _collect_helper_index_ids(
    curves: Sequence[ResolvedCurveLike],
) -> list[str]:
    """Collect every index id the curves' helper points reference.

    Walks all curves' points, and for each helper kind that carries an index ref
    (:data:`_HELPER_INDEX_REF_FIELDS`) derives the id via the same rule the
    builder uses (:func:`_index_ref_id`). Returns a sorted, de-duplicated list so
    the registry build is deterministic (byte-stable packed ``Pricing``).
    """

    ids: set[str] = set()
    for curve in curves:
        for raw in curve.points:
            if not isinstance(raw, Mapping):
                continue
            kind, inner = _split_point(raw)
            if not isinstance(kind, str):
                continue
            field = _HELPER_INDEX_REF_FIELDS.get(kind)
            if field is None:
                continue
            ids.add(_index_ref_id(inner.get(field)))
    return sorted(ids)


def _build_rates_indices(resolved: ResolvedMarketData) -> list[IndexDefT]:
    """Build the ``rates.indices`` registry: default + resolved + helper refs.

    The engine's ``IndexRegistry`` (``parser/index_registry.h``) is keyed by
    ``IndexDef.id`` and resolves *both* the curve-helper refs
    (``SwapHelper.floatIndex`` / ``OISHelper.overnightIndex``, which default to
    :data:`DEFAULT_FORWARDING_INDEX_ID` via :func:`_index_ref`) and the per-trade
    instrument ref (the resolved index id). The registry must therefore
    carry *every* referenced id:

    * the default forwarding index the curve helpers bootstrap against is always
      emitted;
    * the resolved instrument index is registered alongside it when supplied
      (a derived-id collision with the default keeps the resolved def — it wins);
    * **and every index id the curve *helpers* themselves reference** — e.g. an
      EUR OIS curve whose ``OISHelper`` points carry ``overnight_index:{id:
      "ESTR"}`` — is registered from the known-overnight-index catalog
      (:data:`_KNOWN_OVERNIGHT_INDICES`). Before this those
      helper ids landed in the helper structs but were never registered, so the
      engine rejected the curve with the opaque ``NOT_FOUND: Unknown index id:
      ESTR``. A helper id that is already registered (resolved/explicit wins) is
      not duplicated; a helper id that is neither known nor otherwise registered
      raises an actionable :class:`IndexRegistrationError` (422) rather than
      sending a bogus placeholder the engine would mis-price against.
    """

    default_index = _build_default_forwarding_index()
    indices_by_id: dict[str, IndexDefT] = {default_index.id: default_index}
    if resolved.index is not None:
        # A resolved index whose derived id collides with the default id wins
        # (its faithful definition replaces the placeholder, single entry).
        resolved_index = translate_index(resolved.index)
        indices_by_id[resolved_index.id] = resolved_index
    # register every index id the curve helpers reference that is
    # not already covered by the default / resolved indices above. Resolved and
    # explicit ids take precedence (already in ``indices_by_id``, so skipped);
    # an unknown id surfaces an actionable 422 instead of an opaque engine
    # NOT_FOUND.
    for index_id in _collect_helper_index_ids(resolved.curves):
        if index_id in indices_by_id:
            continue
        known_on = _KNOWN_OVERNIGHT_INDICES.get(index_id)
        if known_on is not None:
            indices_by_id[index_id] = _build_known_overnight_index(index_id, known_on)
            continue
        # a helper referencing a standard term-IBOR forwarding index
        # (e.g. ``EURIBOR_6M``) registers a faithful Ibor IndexDef from the
        # catalog, symmetric to the overnight path above.
        known_ibor = _KNOWN_IBOR_INDICES.get(index_id)
        if known_ibor is not None:
            indices_by_id[index_id] = _build_known_ibor_index(index_id, known_ibor)
            continue
        raise IndexRegistrationError(
            index_id,
            sorted(set(indices_by_id) | set(_KNOWN_OVERNIGHT_INDICES) | set(_KNOWN_IBOR_INDICES)),
        )
    return list(indices_by_id.values())


def resolved_index_id(index: ResolvedIndexLike) -> str:
    """Derive the stable engine-facing index id from a resolved entity.

    The ``app.indices`` UUID as text when the index came from a row, else the
    inline name. ``ResolvedIndex`` guarantees a non-empty name, so this never
    falls back to a canonical id.
    """

    return str(index.id) if index.id is not None else index.name


def _index_tenor(body: Mapping[str, Any]) -> PeriodT:
    """Read the index tenor off the body, defaulting to 6M (loose metadata)."""

    tenor = body.get("tenor")
    if isinstance(tenor, Mapping) and tenor.get("n") is not None:
        return make_period(
            _int(tenor.get("n"), 6),
            _enum(TimeUnit, tenor.get("unit"), TimeUnit.Months),
        )
    if body.get("tenor_number") is not None:
        return make_period(
            _int(body.get("tenor_number"), 6),
            _enum(TimeUnit, body.get("tenor_time_unit"), TimeUnit.Months),
        )
    return make_period(6, TimeUnit.Months)


def translate_index(index: ResolvedIndexLike) -> IndexDefT:
    """Translate one resolved index → a faithful ``IndexDef``.

    The id honours the resolved entity (never a canonical id); the convention
    fields are read off the index body / metadata with documented defaults for
    loose storage. Replaces the swap_ir Euribor-6M hardcoded placeholder.
    """

    body = index.body or {}
    idx = IndexDefT()
    idx.id = resolved_index_id(index)
    idx.name = _first_str(body, _INDEX_NAME_KEYS) or index.name
    idx.indexType = _INDEX_TYPE_KINDS.get(index.kind, IndexType.Ibor)
    idx.tenor = _index_tenor(body)
    idx.fixingDays = _int(body.get("fixing_days"), 2)
    idx.calendar = _enum(Calendar, index.calendar or body.get("calendar"), Calendar.TARGET)
    idx.businessDayConvention = _enum(
        BusinessDayConvention,
        body.get("business_day_convention"),
        BusinessDayConvention.ModifiedFollowing,
    )
    idx.dayCounter = _enum(
        DayCounter, index.day_counter or body.get("day_counter"), DayCounter.Actual360
    )
    idx.endOfMonth = bool(body.get("end_of_month", False))
    idx.currency = index.currency or _first_str(body, ("currency",)) or "EUR"
    return idx


# ---------------------------------------------------------------------------
# float-leg coupon frequency follows the resolved index tenor
# ---------------------------------------------------------------------------

# The float-leg coupon frequency MUST follow the resolved index tenor (a 3M
# index pays quarterly float coupons, a 6M index semi-annually, …); a hardcoded
# Semiannual made the index tenor invisible to the engine, so on a multi-curve
# (discount ≠ forwarding) setup the 3M-vs-6M choice moved nothing. When no index
# is resolved we keep this documented default so the no-index fallback prices
# exactly as before. This started swap_ir-only and was lifted here so the
# swaption underlying float leg and the floating-rate bond schedule share one
# source of truth and can never disagree with swap_ir's mapping.
DEFAULT_FLOAT_FREQUENCY: Final[int] = Frequency.Semiannual

# Coupon periods per year → FB ``Frequency`` for the tenors QuantLib can express
# as an integer annual frequency. Keyed by the tenor's length in *months* so a
# ``Years`` tenor normalises through the same table (1Y → 12 → Annual).
_MONTHS_TO_FREQUENCY: Final[dict[int, int]] = {
    1: Frequency.Monthly,
    2: Frequency.Bimonthly,
    3: Frequency.Quarterly,
    4: Frequency.EveryFourthMonth,
    6: Frequency.Semiannual,
    12: Frequency.Annual,
}
_WEEKS_TO_FREQUENCY: Final[dict[int, int]] = {
    1: Frequency.Weekly,
    2: Frequency.Biweekly,
    4: Frequency.EveryFourthWeek,
}


def _frequency_from_period(period: PeriodT) -> int | None:
    """Map a QuantLib-style ``Period`` (n, unit) → the matching coupon ``Frequency``.

    Mirrors ``QuantLib::Period::frequency`` for the tenors that have an exact
    annual frequency: whole-year / whole-month tenors normalise through
    :data:`_MONTHS_TO_FREQUENCY`; weekly / daily tenors map directly. Returns
    ``None`` for a tenor with no clean integer frequency (e.g. 5M, 3W) so the
    caller can fall back to the documented default rather than pick a wrong
    schedule.
    """

    n = int(period.n)
    unit = int(period.unit)
    if n <= 0:
        return None
    if unit == TimeUnit.Years:
        return _MONTHS_TO_FREQUENCY.get(n * 12)
    if unit == TimeUnit.Months:
        return _MONTHS_TO_FREQUENCY.get(n)
    if unit == TimeUnit.Weeks:
        return _WEEKS_TO_FREQUENCY.get(n)
    if unit == TimeUnit.Days:
        return Frequency.Daily if n == 1 else None
    return None


def float_leg_frequency(resolved: ResolvedMarketData) -> int:
    """Derive the floating-leg coupon frequency from the resolved index tenor.

    Reads the tenor off the *same* faithful ``IndexDef`` the engine receives
    (:func:`translate_index`), so the float schedule and the projection index can
    never disagree. Falls back to :data:`DEFAULT_FLOAT_FREQUENCY` when no index
    is resolved (single flat-default path, unchanged) or when the tenor has no
    clean integer frequency (the non-standard-tenor case is preserved, not
    solved). Shared by swap_ir, swaption and floating-rate bonds so all three
    honour one mapping.
    """

    if resolved.index is None:
        return DEFAULT_FLOAT_FREQUENCY
    tenor = translate_index(resolved.index).tenor
    if tenor is None:
        return DEFAULT_FLOAT_FREQUENCY
    frequency = _frequency_from_period(tenor)
    # ``Frequency.Annual`` is 0 (falsy) — must test ``is None`` explicitly, not
    # truthiness, or an annual index would silently fall back to the default.
    if frequency is None:
        return DEFAULT_FLOAT_FREQUENCY
    return frequency


# ---------------------------------------------------------------------------
# Small read helpers
# ---------------------------------------------------------------------------


def _enum(enum_cls: type, value: object, default: int) -> int:
    """Map a portal enum *member name* (string) to its FB integer value.

    Convention / curve-metadata enums are loose: an absent or
    unrecognised value falls back to ``default``. Only the helper *kind* is
    strict. An already-int value passes through (forward-compat
    with a future caller that pre-resolves).
    """

    if value is None:
        return default
    if isinstance(value, bool):  # bool is an int subclass — never an enum value
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        member = getattr(enum_cls, value, None)
        if isinstance(member, int):
            return member
    return default


def _int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    return default


def _float(value: object, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _first_str(inner: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = inner.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _require_str(inner: Mapping[str, Any], key: str, kind: str) -> str:
    value = inner.get(key)
    if isinstance(value, str) and value:
        return value
    raise CurveTranslationError(
        f"{kind} point is missing the required string field {key!r}.",
        details=[{"helper_kind": kind, "missing_field": key}],
    )


def _require_int(inner: Mapping[str, Any], key: str, kind: str) -> int:
    value = inner.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CurveTranslationError(
            f"{kind} point is missing the required numeric field {key!r}.",
            details=[{"helper_kind": kind, "missing_field": key}],
        )
    return int(value)


def _require_tenor(inner: Mapping[str, Any], kind: str) -> PeriodT:
    """Build a ``PeriodT`` from ``tenor: {n, unit}`` or ``tenor_number``/``tenor_time_unit``.

    Accepts both the normalised API shape (``tenor`` object) and the raw portal
    storage shape (split ``tenor_number`` + ``tenor_time_unit``) — the "the
    assembler is the compat layer" applies to point shapes too. Missing → 422.
    """

    tenor = inner.get("tenor")
    if isinstance(tenor, Mapping) and tenor.get("n") is not None:
        return make_period(
            _int(tenor.get("n"), 0), _enum(TimeUnit, tenor.get("unit"), TimeUnit.Years)
        )
    if inner.get("tenor_number") is not None:
        return make_period(
            _int(inner.get("tenor_number"), 0),
            _enum(TimeUnit, inner.get("tenor_time_unit"), TimeUnit.Years),
        )
    raise CurveTranslationError(
        f"{kind} point is missing the required tenor "
        "(``tenor: {n, unit}`` or ``tenor_number``/``tenor_time_unit``).",
        details=[{"helper_kind": kind, "missing_field": "tenor"}],
    )


def _index_ref_id(raw: object) -> str:
    """Derive the effective index id a helper ``{"id": ...}`` ref points at.

    Mirrors :func:`_index_ref`'s id rule exactly so the index *collector*
    (:func:`_collect_helper_index_ids`) registers precisely the ids
    the helper builders actually emit: an explicit non-empty ``id`` wins,
    otherwise the helper falls back to :data:`DEFAULT_FORWARDING_INDEX_ID`.
    """

    candidate = raw.get("id") if isinstance(raw, Mapping) else None
    return candidate if isinstance(candidate, str) and candidate else DEFAULT_FORWARDING_INDEX_ID


def _index_ref(raw: object) -> IndexRefT:
    """Build an ``IndexRef`` from a ``{"id": ...}`` ref, defaulting to the forwarding index."""

    ref = IndexRefT()
    ref.id = _index_ref_id(raw)
    return ref


# ---------------------------------------------------------------------------
# Per-kind structural builders (value field is applied separately)
# ---------------------------------------------------------------------------


def _build_deposit(inner: Mapping[str, Any]) -> DepositHelperT:
    h = DepositHelperT()
    h.tenor = _require_tenor(inner, "DepositHelper")
    h.fixingDays = _int(inner.get("fixing_days"), 2)
    h.calendar = _enum(Calendar, inner.get("calendar"), Calendar.TARGET)
    h.businessDayConvention = _enum(
        BusinessDayConvention,
        inner.get("business_day_convention"),
        BusinessDayConvention.ModifiedFollowing,
    )
    h.dayCounter = _enum(DayCounter, inner.get("day_counter"), DayCounter.Actual365Fixed)
    return h


def _build_swap(inner: Mapping[str, Any]) -> SwapHelperT:
    h = SwapHelperT()
    h.tenor = _require_tenor(inner, "SwapHelper")
    h.calendar = _enum(Calendar, inner.get("calendar"), Calendar.TARGET)
    h.swFixedLegFrequency = _enum(Frequency, inner.get("sw_fixed_leg_frequency"), Frequency.Annual)
    h.swFixedLegConvention = _enum(
        BusinessDayConvention,
        inner.get("sw_fixed_leg_convention"),
        BusinessDayConvention.ModifiedFollowing,
    )
    h.swFixedLegDayCounter = _enum(
        DayCounter, inner.get("sw_fixed_leg_day_counter"), DayCounter.Thirty360
    )
    h.floatIndex = _index_ref(inner.get("float_index"))
    h.spread = _float(inner.get("spread"), 0.0)
    h.fwdStartDays = _int(inner.get("fwd_start_days"), 0)
    return h


def _build_fra(inner: Mapping[str, Any]) -> FRAHelperT:
    h = FRAHelperT()
    h.monthsToStart = _require_int(inner, "months_to_start", "FRAHelper")
    h.monthsToEnd = _require_int(inner, "months_to_end", "FRAHelper")
    h.fixingDays = _int(inner.get("fixing_days"), 2)
    h.calendar = _enum(Calendar, inner.get("calendar"), Calendar.TARGET)
    h.businessDayConvention = _enum(
        BusinessDayConvention,
        inner.get("business_day_convention"),
        BusinessDayConvention.ModifiedFollowing,
    )
    h.dayCounter = _enum(DayCounter, inner.get("day_counter"), DayCounter.Actual365Fixed)
    return h


def _build_future(inner: Mapping[str, Any]) -> FutureHelperT:
    h = FutureHelperT()
    h.futureStartDate = _require_str(inner, "future_start_date", "FutureHelper")
    h.futureMonths = _int(inner.get("future_months"), 3)
    h.calendar = _enum(Calendar, inner.get("calendar"), Calendar.TARGET)
    h.businessDayConvention = _enum(
        BusinessDayConvention,
        inner.get("business_day_convention"),
        BusinessDayConvention.ModifiedFollowing,
    )
    h.dayCounter = _enum(DayCounter, inner.get("day_counter"), DayCounter.Actual365Fixed)
    h.convexityAdjustment = _float(inner.get("convexity_adjustment"), 0.0)
    return h


def _build_bond(inner: Mapping[str, Any]) -> BondHelperT:
    schedule_raw = inner.get("schedule")
    schedule: Mapping[str, Any] = schedule_raw if isinstance(schedule_raw, Mapping) else {}
    sched = ScheduleT()
    sched.effectiveDate = _require_str(schedule, "effective_date", "BondHelper.schedule")
    sched.terminationDate = _require_str(schedule, "termination_date", "BondHelper.schedule")
    sched.calendar = _enum(Calendar, schedule.get("calendar"), Calendar.TARGET)
    sched.frequency = _enum(Frequency, schedule.get("frequency"), Frequency.Semiannual)
    sched.convention = _enum(
        BusinessDayConvention,
        schedule.get("convention"),
        BusinessDayConvention.ModifiedFollowing,
    )
    sched.terminationDateConvention = _enum(
        BusinessDayConvention,
        schedule.get("termination_date_convention"),
        BusinessDayConvention.ModifiedFollowing,
    )
    sched.dateGenerationRule = _enum(
        DateGenerationRule, schedule.get("date_generation_rule"), DateGenerationRule.Forward
    )
    # Presence-required since engine 0.5.0 (#118); False == the engine-0.2.0
    # default (the field was never emitted before, so the engine always read
    # False). An explicit user value is honored like every other schedule field.
    sched.endOfMonth = bool(schedule.get("end_of_month", False))

    h = BondHelperT()
    h.schedule = sched
    h.settlementDays = _int(inner.get("settlement_days"), 2)
    h.faceAmount = _float(inner.get("face_amount"), 100.0)
    h.couponRate = _float(inner.get("coupon_rate"), 0.0)
    h.dayCounter = _enum(DayCounter, inner.get("day_counter"), DayCounter.Actual365Fixed)
    h.businessDayConvention = _enum(
        BusinessDayConvention,
        inner.get("business_day_convention"),
        BusinessDayConvention.ModifiedFollowing,
    )
    h.redemption = _float(inner.get("redemption"), 100.0)
    h.issueDate = _require_str(inner, "issue_date", "BondHelper")
    return h


def _ois_non_negative_int(inner: Mapping[str, Any], key: str, kind: str) -> int:
    """Read an optional non-negative int helper field (absent → 0, negative → 422).

    Absent means "the stored point predates engine 0.6" and 0 is the exact
    legacy value for every field this reads (see :func:`_apply_ois_overnight_params`).
    A negative value can never translate (the engine wraps these into unsigned
    QuantLib ``Natural``s and 400s), so it is a cheap pre-flight typed 422.
    """

    raw = inner.get(key)
    value = _int(raw, 0)
    if value < 0:
        raise CurveTranslationError(
            f"{kind} point field {key!r} must be >= 0 (got {value}).",
            details=[{"helper_kind": kind, "field": key, "value": value}],
        )
    return value


def _ois_averaging_method(inner: Mapping[str, Any], kind: str) -> int:
    """Read ``averaging_method`` strictly: Compound | Simple, absent → Compound.

    Unlike the loose convention enums, an unrecognised averaging method is a
    typed 422 rather than a silent fallback: Compound-vs-Simple changes the
    bootstrapped curve, so a typo must not silently price as Compound.
    Absent → Compound (the exact <=0.5.0 engine behavior; see
    :func:`_apply_ois_overnight_params`).
    """

    raw = inner.get("averaging_method")
    if raw is None:
        return int(RateAveragingType.Compound)
    if not isinstance(raw, bool) and isinstance(raw, int):
        if raw in (RateAveragingType.Compound, RateAveragingType.Simple):
            return raw
    elif isinstance(raw, str):
        member = getattr(RateAveragingType, raw, None)
        if isinstance(member, int):
            return member
    raise CurveTranslationError(
        f"{kind} point field 'averaging_method' must be 'Compound' or 'Simple' (got {raw!r}).",
        details=[{"helper_kind": kind, "field": "averaging_method", "value": raw}],
    )


def _apply_ois_overnight_params(
    inner: Mapping[str, Any], h: OISHelperT | DatedOISHelperT, kind: str
) -> None:
    """Emit the five engine-0.6 overnight params, REQUIRED on the wire.

    ``payment_lag`` / ``averaging_method`` / ``lookback_days`` /
    ``lockout_days`` / ``apply_observation_shift`` became required helper
    fields in engine 0.6 (an omitted convention is an error, never a silent
    default). All five are always assigned here — the presence-based bindings
    then serialize each one explicitly even at its zero/false value.

    LEGACY-COMPAT: a stored curve whose OIS points predate engine 0.6 lacks
    these keys; we emit the exact values the <=0.5.0 engine applied silently
    (its 5-arg QuantLib ``OISRateHelper`` call → QuantLib defaults):
    ``payment_lag=0``, ``averaging_method=Compound``, ``lookback_days=0``
    (wire 0 = "no lookback" → QuantLib ``Null``, the engine maps it),
    ``lockout_days=0``, ``apply_observation_shift=False``. Do not change
    these — they are the 0.5.0-parity contract for stored curves.
    """

    h.paymentLag = _ois_non_negative_int(inner, "payment_lag", kind)
    h.averagingMethod = _ois_averaging_method(inner, kind)
    h.lookbackDays = _ois_non_negative_int(inner, "lookback_days", kind)
    h.lockoutDays = _ois_non_negative_int(inner, "lockout_days", kind)
    # bool(...) mirrors the Schedule.end_of_month presence pattern.
    h.applyObservationShift = bool(inner.get("apply_observation_shift", False))


def _build_ois(inner: Mapping[str, Any]) -> OISHelperT:
    h = OISHelperT()
    h.tenor = _require_tenor(inner, "OISHelper")
    h.overnightIndex = _index_ref(inner.get("overnight_index"))
    h.settlementDays = _int(inner.get("settlement_days"), 2)
    h.calendar = _enum(Calendar, inner.get("calendar"), Calendar.TARGET)
    h.fixedLegFrequency = _enum(Frequency, inner.get("fixed_leg_frequency"), Frequency.Annual)
    h.fixedLegConvention = _enum(
        BusinessDayConvention,
        inner.get("fixed_leg_convention"),
        BusinessDayConvention.ModifiedFollowing,
    )
    # Deprecated/accepted-but-unused on engine >=0.6 (no QuantLib overload
    # takes it); still accepted from the stored point and emitted for
    # backward compatibility — the engine ignores it.
    h.fixedLegDayCounter = _enum(
        DayCounter, inner.get("fixed_leg_day_counter"), DayCounter.Actual365Fixed
    )
    _apply_ois_overnight_params(inner, h, "OISHelper")
    return h


def _build_dated_ois(inner: Mapping[str, Any]) -> DatedOISHelperT:
    h = DatedOISHelperT()
    h.startDate = _require_str(inner, "start_date", "DatedOISHelper")
    h.endDate = _require_str(inner, "end_date", "DatedOISHelper")
    h.overnightIndex = _index_ref(inner.get("overnight_index"))
    h.settlementDays = _int(inner.get("settlement_days"), 2)
    h.calendar = _enum(Calendar, inner.get("calendar"), Calendar.TARGET)
    # Required since engine 0.6 (the dated helper's payment frequency was
    # previously hardcoded Annual engine-side); Annual == the exact legacy
    # behavior for stored points that lack the key.
    h.fixedLegFrequency = _enum(Frequency, inner.get("fixed_leg_frequency"), Frequency.Annual)
    h.fixedLegConvention = _enum(
        BusinessDayConvention,
        inner.get("fixed_leg_convention"),
        BusinessDayConvention.ModifiedFollowing,
    )
    # Deprecated/accepted-but-unused on engine >=0.6 — see _build_ois.
    h.fixedLegDayCounter = _enum(
        DayCounter, inner.get("fixed_leg_day_counter"), DayCounter.Actual365Fixed
    )
    _apply_ois_overnight_params(inner, h, "DatedOISHelper")
    return h


@dataclass(frozen=True)
class _HelperSpec:
    """One row of the helper-kind map: the FB union tag + the structural builder."""

    union: int
    build: Callable[[Mapping[str, Any]], object]


_HELPER_SPECS: Final[dict[str, _HelperSpec]] = {
    "DepositHelper": _HelperSpec(Point.DepositHelper, _build_deposit),
    "SwapHelper": _HelperSpec(Point.SwapHelper, _build_swap),
    "FRAHelper": _HelperSpec(Point.FRAHelper, _build_fra),
    "FutureHelper": _HelperSpec(Point.FutureHelper, _build_future),
    "BondHelper": _HelperSpec(Point.BondHelper, _build_bond),
    "OISHelper": _HelperSpec(Point.OISHelper, _build_ois),
    "DatedOISHelper": _HelperSpec(Point.DatedOISHelper, _build_dated_ois),
}

# Logical value field → FB Object-API attribute name.
_FB_VALUE_FIELD: Final[dict[str, str]] = {
    "rate": "rate",
    "price": "price",
    "futures_price": "futuresPrice",
}

_QUOTE_KEYS: Final[tuple[str, ...]] = ("quote_id", "quoteId")


def _value_target(kind: str, inner: Mapping[str, Any]) -> str:
    """Pick the FB value field a substituted quote writes into (mirrors the portal).

    See ``curve-quote-resolution.ts``: a BondHelper writes ``price`` and a
    FutureHelper writes ``futures_price`` *unless* the point already carries a
    ``rate`` key, in which case the value is a rate.
    """

    if kind == "BondHelper" and "rate" not in inner:
        return "price"
    if kind == "FutureHelper" and "rate" not in inner:
        return "futures_price"
    return "rate"


def _split_point(raw: Mapping[str, Any]) -> tuple[object, Mapping[str, Any]]:
    """Return ``(kind, inner)`` for a curve point, wrapped or flat.

    Wrapped: ``{"point_type": "...", "point": {...}}`` (the portal storage
    shape). Flat: the helper fields at the top level alongside ``point_type``.
    """

    kind = raw.get("point_type") or raw.get("pointType")
    nested = raw.get("point")
    if isinstance(nested, Mapping):
        return kind, nested
    return kind, {k: v for k, v in raw.items() if k not in ("point_type", "pointType")}


def _apply_value(
    kind: str,
    inner: Mapping[str, Any],
    point_t: object,
    quote_map: Mapping[str, float],
    missing_quotes: list[str],
) -> None:
    """Write the point's value into the right FB field; drop ``quote_id``.

    The FB helper carries a native ``quoteId`` field, but per invariant #8 the
    engine must never see one — so we only ever set the *value* field and leave
    ``quoteId`` unset. An unresolved ``quote_id`` is appended to
    ``missing_quotes`` (batched into one 422 by the caller); a point with no
    value source at all is a curve-translation error.
    """

    quote_id = _first_str(inner, _QUOTE_KEYS)
    if quote_id is not None:
        if quote_id in quote_map:
            target = _value_target(kind, inner)
            setattr(point_t, _FB_VALUE_FIELD[target], float(quote_map[quote_id]))
        else:
            missing_quotes.append(quote_id)
        return

    for logical in ("rate", "price", "futures_price"):
        candidate = inner.get(logical)
        if not isinstance(candidate, bool) and isinstance(candidate, (int, float)):
            setattr(point_t, _FB_VALUE_FIELD[logical], float(candidate))
            return

    raise CurveTranslationError(
        f"{kind} point has neither a ``quote_id`` nor an inline value "
        "(``rate`` / ``price`` / ``futures_price``).",
        details=[{"helper_kind": kind}],
    )


def _translate_point(
    raw: Mapping[str, Any],
    quote_map: Mapping[str, float],
    missing_quotes: list[str],
) -> tuple[int, object]:
    """Translate one curve point → ``(Point union tag, helper *T)``."""

    kind, inner = _split_point(raw)
    if not isinstance(kind, str) or kind not in _HELPER_SPECS:
        raise UnknownHelperKindError(kind)
    spec = _HELPER_SPECS[kind]
    point_t = spec.build(inner)
    _apply_value(kind, inner, point_t, quote_map, missing_quotes)
    return spec.union, point_t


# ---------------------------------------------------------------------------
# Value-based curve points (engine 0.5.0 direct-data curves)
# ---------------------------------------------------------------------------
# ``bootstrap_trait`` ``InterpolatedZero`` / ``InterpolatedDiscount`` /
# ``InterpolatedFwd`` curves carry explicit VALUE points (``ZeroRatePoint`` /
# ``DiscountFactorPoint`` / ``ForwardRatePoint``) instead of rate helpers: the
# engine interpolates the values directly (QuantLib ``InterpolatedZeroCurve``
# / ``InterpolatedDiscountCurve`` / ``InterpolatedForwardCurve``) with no
# bootstrap. The app-level point shape mirrors the wire PLUS an optional
# ``quote_id`` (exactly one of inline value | ``quote_id`` per point); the
# quote is resolved through the SAME MD walker as helper quotes and the
# resolved number is emitted inline — the engine never sees a quote id
# (invariant #8).
#
# Validation mirrors the engine's rules pre-flight (typed 422s instead of an
# engine 400/masked QuantLib error): trait/point family coherence (no
# auto-dispatch, no mixing), a date or a positive tenor per point, finite
# values, uniform compounding/frequency across zero points, discount factors
# in (0, 1] with the first point exactly 1.0 at the curve reference date, and
# strictly-increasing unique pillars (computed on the unadjusted tenor date —
# the engine applies the calendar/BDC adjustment; the pre-flight ordering
# check uses the raw pillar spacing, which real curves clear by weeks).
# Value curves reference no indices, so index registration skips them
# (they carry no entry in ``_HELPER_INDEX_REF_FIELDS``).


@dataclass(frozen=True)
class _ValuePointSpec:
    """One value-point family: FB union tag + required trait + value field."""

    kind: str
    union: int
    trait: int
    value_keys: tuple[str, ...]
    fb_attr: str
    build: Callable[[Mapping[str, Any]], object]


def _value_point_conventions(inner: Mapping[str, Any], point_t: object) -> None:
    """Apply the calendar / BDC shared by all three value-point kinds."""

    point_t.calendar = _enum(Calendar, inner.get("calendar"), Calendar.TARGET)  # type: ignore[attr-defined]
    point_t.businessDayConvention = _enum(  # type: ignore[attr-defined]
        BusinessDayConvention,
        inner.get("business_day_convention"),
        BusinessDayConvention.ModifiedFollowing,
    )


def _build_zero_rate_point(inner: Mapping[str, Any]) -> ZeroRatePointT:
    p = ZeroRatePointT()
    _value_point_conventions(inner, p)
    p.compounding = _enum(Compounding, inner.get("compounding"), Compounding.Continuous)
    p.frequency = _enum(Frequency, inner.get("frequency"), Frequency.Annual)
    return p


def _build_discount_factor_point(inner: Mapping[str, Any]) -> DiscountFactorPointT:
    p = DiscountFactorPointT()
    _value_point_conventions(inner, p)
    return p


def _build_forward_rate_point(inner: Mapping[str, Any]) -> ForwardRatePointT:
    p = ForwardRatePointT()
    _value_point_conventions(inner, p)
    return p


_VALUE_POINT_SPECS: Final[dict[str, _ValuePointSpec]] = {
    "ZeroRatePoint": _ValuePointSpec(
        kind="ZeroRatePoint",
        union=Point.ZeroRatePoint,
        trait=BootstrapTrait.InterpolatedZero,
        value_keys=("zero_rate", "zeroRate"),
        fb_attr="zeroRate",
        build=_build_zero_rate_point,
    ),
    "DiscountFactorPoint": _ValuePointSpec(
        kind="DiscountFactorPoint",
        union=Point.DiscountFactorPoint,
        trait=BootstrapTrait.InterpolatedDiscount,
        value_keys=("discount_factor", "discountFactor"),
        fb_attr="discountFactor",
        build=_build_discount_factor_point,
    ),
    "ForwardRatePoint": _ValuePointSpec(
        kind="ForwardRatePoint",
        union=Point.ForwardRatePoint,
        trait=BootstrapTrait.InterpolatedFwd,
        value_keys=("forward_rate", "forwardRate"),
        fb_attr="forwardRate",
        build=_build_forward_rate_point,
    ),
}

_VALUE_SPEC_BY_TRAIT: Final[dict[int, _ValuePointSpec]] = {
    spec.trait: spec for spec in _VALUE_POINT_SPECS.values()
}

_TRAIT_NAMES: Final[dict[int, str]] = {
    getattr(BootstrapTrait, name): name for name in dir(BootstrapTrait) if not name.startswith("_")
}


def _value_curve_error(
    reason: str, *, curve: str, point_index: int | None, rule: str, **extra: object
) -> CurveTranslationError:
    detail: dict[str, Any] = {"curve": curve, "rule": rule, **extra}
    if point_index is not None:
        detail["point_index"] = point_index
    return CurveTranslationError(reason, details=[detail])


def _value_point_pillar(
    inner: Mapping[str, Any],
    *,
    kind: str,
    curve_id: str,
    point_index: int,
    reference_date: date,
) -> tuple[date, str | None, PeriodT | None]:
    """Resolve one value point's pillar: explicit ISO date, or ref + tenor.

    Mirrors the engine's precedence (``date`` wins; else a positive ``tenor``
    is required). Returns ``(pillar, explicit_date, period)`` — exactly one of
    ``explicit_date`` / ``period`` is non-``None`` and is what the caller puts
    on the wire. The tenor pillar is the UNADJUSTED anchor used only for the
    pre-flight ordering/uniqueness check — the engine applies the point's
    calendar/BDC adjustment when it builds the curve.
    """

    raw_date = inner.get("date")
    if isinstance(raw_date, str) and raw_date:
        try:
            return date.fromisoformat(raw_date[:10]), raw_date[:10], None
        except ValueError:
            raise _value_curve_error(
                f"{kind} point {point_index} of curve {curve_id} has an invalid "
                f"``date`` {raw_date!r} (expected YYYY-MM-DD).",
                curve=curve_id,
                point_index=point_index,
                rule="invalid_date",
            ) from None
    try:
        period = _require_tenor(inner, kind)
    except CurveTranslationError as exc:
        raise _value_curve_error(
            f"{kind} point {point_index} of curve {curve_id} requires a "
            "``date`` or a positive ``tenor``.",
            curve=curve_id,
            point_index=point_index,
            rule="missing_pillar",
        ) from exc
    n = int(period.n or 0)
    if n <= 0:
        raise _value_curve_error(
            f"{kind} point {point_index} of curve {curve_id} requires a "
            "``date`` or a positive ``tenor``.",
            curve=curve_id,
            point_index=point_index,
            rule="missing_pillar",
        )
    unit = int(period.unit or 0)
    if unit == TimeUnit.Days:
        return reference_date + timedelta(days=n), None, period
    if unit == TimeUnit.Weeks:
        return reference_date + timedelta(weeks=n), None, period
    if unit in (TimeUnit.Months, TimeUnit.Years):
        months = n * 12 if unit == TimeUnit.Years else n
        total = reference_date.month - 1 + months
        year = reference_date.year + total // 12
        month = total % 12 + 1
        day = min(reference_date.day, monthrange(year, month)[1])
        return date(year, month, day), None, period
    raise _value_curve_error(
        f"{kind} point {point_index} of curve {curve_id} has an unsupported "
        "tenor unit (use Days / Weeks / Months / Years).",
        curve=curve_id,
        point_index=point_index,
        rule="unsupported_tenor_unit",
    )


def _value_point_value(
    spec: _ValuePointSpec,
    inner: Mapping[str, Any],
    quote_map: Mapping[str, float],
    missing_quotes: list[str],
    *,
    curve_id: str,
    point_index: int,
) -> float | None:
    """Resolve one value point's number: exactly one of inline value | quote.

    Returns ``None`` when the point references a quote the MD walker could not
    resolve — the id is appended to ``missing_quotes`` and the caller's
    existing batched quote-resolution 422 fires (family-level numeric checks
    skip the point).
    """

    quote_id = _first_str(inner, _QUOTE_KEYS)
    raw_value = None
    for key in spec.value_keys:
        candidate = inner.get(key)
        if candidate is not None:
            raw_value = candidate
            break
    if quote_id is not None and raw_value is not None:
        raise _value_curve_error(
            f"{spec.kind} point {point_index} of curve {curve_id} carries BOTH "
            f"an inline ``{spec.value_keys[0]}`` and a ``quote_id`` — exactly "
            "one value source is allowed per point.",
            curve=curve_id,
            point_index=point_index,
            rule="ambiguous_value_source",
            quote_id=quote_id,
        )
    if quote_id is not None:
        if quote_id in quote_map:
            return float(quote_map[quote_id])
        missing_quotes.append(quote_id)
        return None
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        raise _value_curve_error(
            f"{spec.kind} point {point_index} of curve {curve_id} has neither "
            f"a numeric ``{spec.value_keys[0]}`` nor a ``quote_id``.",
            curve=curve_id,
            point_index=point_index,
            rule="missing_value_source",
        )
    value = float(raw_value)
    if not isfinite(value):
        raise _value_curve_error(
            f"{spec.kind} point {point_index} of curve {curve_id}: "
            f"``{spec.value_keys[0]}`` must be finite.",
            curve=curve_id,
            point_index=point_index,
            rule="non_finite_value",
        )
    return value


def _validate_discount_factor(
    value: float, *, curve_id: str, point_index: int, reference_iso: str, explicit_date: str | None
) -> None:
    """Mirror the engine's DF rules: (0, 1]; first point exactly 1.0 at ref."""

    if value <= 0.0 or value > 1.0:
        raise _value_curve_error(
            f"DiscountFactorPoint {point_index} of curve {curve_id}: "
            f"``discount_factor`` must be in (0, 1], got {value!r}.",
            curve=curve_id,
            point_index=point_index,
            rule="discount_factor_out_of_range",
        )
    if point_index == 0:
        if value != 1.0:
            raise _value_curve_error(
                f"DiscountFactorPoint 0 of curve {curve_id}: the first "
                "(reference-date) point must carry a ``discount_factor`` of "
                f"exactly 1.0, got {value!r}.",
                curve=curve_id,
                point_index=0,
                rule="first_discount_factor_not_one",
            )
        if explicit_date != reference_iso:
            raise _value_curve_error(
                f"DiscountFactorPoint 0 of curve {curve_id} must sit AT the "
                f"curve reference date ({reference_iso}): give it "
                f'``date: "{reference_iso}"`` (got {explicit_date!r}).',
                curve=curve_id,
                point_index=0,
                rule="first_discount_point_not_at_reference",
            )


def _check_zero_convention(
    point_t: object,
    zero_convention: tuple[int, int] | None,
    *,
    curve_id: str,
    point_index: int,
) -> tuple[int, int]:
    """Enforce ONE compounding/frequency convention across the zero points."""

    convention = (int(point_t.compounding), int(point_t.frequency))  # type: ignore[attr-defined]
    if zero_convention is not None and convention != zero_convention:
        raise _value_curve_error(
            f"ZeroRatePoint {point_index} of curve {curve_id}: ``compounding`` "
            "and ``frequency`` must be identical across all points (a single "
            "interpolated zero curve carries ONE quoting convention).",
            curve=curve_id,
            point_index=point_index,
            rule="mixed_zero_conventions",
        )
    return convention


def _translate_value_curve_points(
    curve: ResolvedCurveLike,
    ts: TermStructureT,
    spec: _ValuePointSpec,
    quote_map: Mapping[str, float],
    missing_quotes: list[str],
) -> list[PointsWrapperT]:
    """Translate + validate a value curve's points (family already selected)."""

    curve_id = str(ts.id)
    reference_iso = str(ts.referenceDate)
    reference = date.fromisoformat(reference_iso)
    trait_name = _TRAIT_NAMES.get(spec.trait, str(spec.trait))

    if not curve.points:
        raise _value_curve_error(
            f"Curve {curve_id}: bootstrap_trait {trait_name} requires at least one {spec.kind}.",
            curve=curve_id,
            point_index=None,
            rule="empty_points",
        )

    wrappers: list[PointsWrapperT] = []
    pillars: list[date] = []
    zero_convention: tuple[int, int] | None = None
    for index, raw in enumerate(curve.points):
        if not isinstance(raw, Mapping):
            raise CurveTranslationError(
                f"Curve {curve_id} point {index} is not an object.",
                details=[{"curve": curve_id, "point_index": index}],
            )
        kind, inner = _split_point(raw)
        if kind != spec.kind:
            raise _value_curve_error(
                f"Curve {curve_id}: bootstrap_trait {trait_name} builds from "
                f"{spec.kind}s but point {index} is a {kind!r} — value curves "
                "cannot mix point families.",
                curve=curve_id,
                point_index=index,
                rule="point_family_mismatch",
                expected_kind=spec.kind,
                actual_kind=kind,
            )
        point_t = spec.build(inner)

        pillar, explicit_date, period = _value_point_pillar(
            inner,
            kind=spec.kind,
            curve_id=curve_id,
            point_index=index,
            reference_date=reference,
        )
        if explicit_date is not None:
            point_t.date = explicit_date  # type: ignore[attr-defined]
        else:
            point_t.tenor = period  # type: ignore[attr-defined]
        pillars.append(pillar)

        value = _value_point_value(
            spec,
            inner,
            quote_map,
            missing_quotes,
            curve_id=curve_id,
            point_index=index,
        )
        if value is not None:
            setattr(point_t, spec.fb_attr, value)
            if spec.kind == "DiscountFactorPoint":
                _validate_discount_factor(
                    value,
                    curve_id=curve_id,
                    point_index=index,
                    reference_iso=reference_iso,
                    explicit_date=explicit_date,
                )

        if spec.kind == "ZeroRatePoint":
            zero_convention = _check_zero_convention(
                point_t, zero_convention, curve_id=curve_id, point_index=index
            )

        wrapper = PointsWrapperT()
        wrapper.pointType = spec.union
        wrapper.point = point_t
        wrappers.append(wrapper)

    for i in range(1, len(pillars)):
        if pillars[i] <= pillars[i - 1]:
            raise _value_curve_error(
                f"Curve {curve_id}: value-curve pillars must be strictly "
                f"increasing and unique; point {i} ({pillars[i].isoformat()}) "
                f"does not follow point {i - 1} ({pillars[i - 1].isoformat()}).",
                curve=curve_id,
                point_index=i,
                rule="pillars_not_sorted",
            )

    return wrappers


def resolved_curve_id(curve: ResolvedCurveLike) -> str:
    """Derive the stable engine-facing curve id from a resolved entity.

    The ``app.curves`` UUID as text when the curve came from a row, else the
    inline name. ``ResolvedCurve`` guarantees a non-empty name, so this never
    falls back to a canonical id.
    """

    return str(curve.id) if curve.id is not None else curve.name


def translate_curve(
    curve: ResolvedCurveLike,
    quote_map: Mapping[str, float],
    *,
    default_reference_date: str,
    missing_quotes: list[str],
) -> TermStructureT:
    """Translate one resolved curve → a ``TermStructure`` with substituted points."""

    ts = TermStructureT()
    ts.id = resolved_curve_id(curve)
    ref = curve.reference_date
    ts.referenceDate = ref.isoformat() if ref is not None else default_reference_date
    ts.dayCounter = _enum(DayCounter, curve.day_counter, DayCounter.Actual365Fixed)
    body = curve.body or {}
    ts.interpolator = _enum(Interpolator, body.get("interpolator"), Interpolator.LogLinear)
    ts.bootstrapTrait = _enum(
        BootstrapTrait,
        body.get("bootstrap_trait") or body.get("bootstrapTrait"),
        BootstrapTrait.Discount,
    )

    # Value-based curves (InterpolatedZero / InterpolatedDiscount /
    # InterpolatedFwd): the trait selects the family explicitly — every point
    # must be the matching value point (mirrors the engine's family selector;
    # no auto-dispatch, no mixing).
    value_spec = _VALUE_SPEC_BY_TRAIT.get(int(ts.bootstrapTrait))
    if value_spec is not None:
        ts.points = _translate_value_curve_points(curve, ts, value_spec, quote_map, missing_quotes)
        return ts

    points: list[PointsWrapperT] = []
    for index, raw in enumerate(curve.points):
        if not isinstance(raw, Mapping):
            raise CurveTranslationError(
                f"Curve {ts.id} point {index} is not an object.",
                details=[{"curve": ts.id, "point_index": index}],
            )
        kind, _ = _split_point(raw)
        if isinstance(kind, str) and kind in _VALUE_POINT_SPECS:
            trait_name = _TRAIT_NAMES.get(int(ts.bootstrapTrait), str(ts.bootstrapTrait))
            raise _value_curve_error(
                f"Curve {ts.id}: bootstrap_trait {trait_name} builds from rate "
                f"helpers but point {index} is a {kind} — set bootstrap_trait "
                f"{_TRAIT_NAMES.get(_VALUE_POINT_SPECS[kind].trait)} for a "
                "value-based curve.",
                curve=str(ts.id),
                point_index=index,
                rule="point_family_mismatch",
                actual_kind=kind,
            )
        try:
            union, point_t = _translate_point(raw, quote_map, missing_quotes)
        except CurveTranslationError as exc:
            # Augment in place so the subclass (UnknownHelperKindError) and its
            # "escalate" semantics survive while the 422 detail gains location.
            exc.details = [
                {"curve": ts.id, "point_index": index, **detail} for detail in (exc.details or [{}])
            ]
            raise
        wrapper = PointsWrapperT()
        wrapper.pointType = union
        wrapper.point = point_t
        points.append(wrapper)
    ts.points = points
    return ts


# ---------------------------------------------------------------------------
# Vol-surface + model translation (swaption)
# ---------------------------------------------------------------------------
# swaption vol-surface minimal translatable contract
# ---------------------------------------------------------
# Per resolved vol surface (read off the product's ``ResolvedVolSurface``):
# * ``kind`` (**required**, strict) — the surface discriminator. The translator
#   maps ``SwaptionVolSpec`` (this section) and ``BlackVolSpec`` (the
#   equity section below); anything else (``OptionletVolSpec`` is cap/floor) →
#   :class:`UnknownVolSurfaceKindError` (→ 422). No canonical fallback
# * ``id`` / ``name`` — the per-trade reference id. ``ResolvedVolSurface``
#   always carries a ``name`` so an id is always derivable; never rejects.
# * ``payload.base`` (**required**) — the constant-vol envelope. Missing →
#   :class:`VolSurfaceTranslationError` (→ 422). Only the constant-vol
#   ``SwaptionVolConstantSpec`` path ships: the value source is read off ``base`` and
#   the structural ``swap_index_id`` / convention fields are read loosely. A
#   richer matrix / cube / SABR sub-shape carried alongside ``base`` is an
#   *extra* (preserved-ignored, forward-compat) — its full translation is a
#   follow-on; this is a minimal-contract read, not a canonical fallback.
# * a **value source** on ``base`` (**required**) — either a ``quote_id`` that
#   resolves against the MD map, or an inline value (``constant_vol``). An
#   unresolved ``quote_id`` → :class:`QuoteResolutionError` (→ 422, batched with
#   the curve quotes); neither present → :class:`VolSurfaceTranslationError`
#   (→ 422). The resolved value is written into
#   ``constantVol`` and ``quoteId`` is **never** left on the wire spec.
# * ``base`` convention fields (calendar / BDC / day-counter / volatility-type /
#   displacement / reference-date) and the surface ``swap_index_id`` — read by
#   FB enum member name / string when present, documented default otherwise
# (loose metadata). The live portal always sends these.
# * any **extra** field is preserved-but-ignored (forward-compat).
# The swaption *model* (``ResolvedSwaptionModel`` → ``ModelSpecT`` /
# ``SwaptionModelSpec``) is pure config (never MD-resolved): ``kind`` →
# ``IrModelType`` by member name (loose; default ``HullWhiteLattice``), the
# Hull-White knobs read off the payload, and the per-trade ``model`` ref honours
# the resolved entity id, never ``CANONICAL_MODEL_ID``.

DEFAULT_SWAP_INDEX_ID: Final[str] = "EUR_SWAP_6M"
"""Swap-index convention anchor a ``SwaptionVolSpec`` references when the
resolved surface body omits ``swap_index_id`` (matches the engine's known
swap index; loose metadata)."""

_VOL_SURFACE_KINDS: Final[frozenset[str]] = frozenset({"SwaptionVolSpec", "BlackVolSpec"})
"""Vol-surface ``kind`` discriminators the translator maps (swaption
``SwaptionVolSpec`` + equity ``BlackVolSpec``)."""

_SWAP_INDEX_KEYS: Final[tuple[str, ...]] = ("swap_index_id", "swapIndexId")
_VOL_VALUE_KEYS: Final[tuple[str, ...]] = ("constant_vol", "constantVol")
_PAYLOAD_TYPE_KEYS: Final[tuple[str, ...]] = ("payload_type", "payloadType")
_SWAPTION_MATRIX_PAYLOAD_TYPE: Final[str] = "SwaptionVolAtmMatrixSpec"
_SWAPTION_SABR_CALIBRATE_PAYLOAD_TYPE: Final[str] = "SwaptionSabrCalibrateSpec"


def resolved_vol_surface_id(vol_surface: ResolvedVolSurfaceLike) -> str:
    """Derive the stable engine-facing vol-surface id from a resolved entity.

    The ``app.vol_surfaces`` UUID as text when the surface came from a row, else
    the inline name. ``ResolvedVolSurface`` guarantees a non-empty name, so this
    never falls back to a canonical id.
    """

    return str(vol_surface.id) if vol_surface.id is not None else vol_surface.name


def resolved_model_id(model: ResolvedModelLike) -> str:
    """Derive the stable engine-facing model id from a resolved entity.

    The ``app.swaption_models`` UUID as text when the model came from a row,
    else the inline name. Never falls back to a canonical id.
    """

    return str(model.id) if model.id is not None else model.name


def _apply_vol_value(
    vol_surface: ResolvedVolSurfaceLike,
    base: IrVolBaseSpecT,
    base_raw: Mapping[str, Any],
    quote_map: Mapping[str, float],
    missing_quotes: list[str],
) -> None:
    """Write the surface's constant vol into ``base.constantVol``; drop ``quote_id``.

    The FB base carries a native ``quoteId`` field, but per invariant #8 the
    engine must never see one — so we only ever set ``constantVol`` and leave
    ``quoteId`` unset. An unresolved ``quote_id`` is appended to
    ``missing_quotes`` (batched into one 422 by the caller); a base with no
    value source at all is a surface-translation error.
    """

    quote_id = _first_str(base_raw, _QUOTE_KEYS)
    if quote_id is not None:
        if quote_id in quote_map:
            base.constantVol = float(quote_map[quote_id])
        else:
            missing_quotes.append(quote_id)
        return

    for key in _VOL_VALUE_KEYS:
        candidate = base_raw.get(key)
        if not isinstance(candidate, bool) and isinstance(candidate, (int, float)):
            base.constantVol = float(candidate)
            return

    raise VolSurfaceTranslationError(
        "SwaptionVolSpec base has neither a ``quote_id`` nor an inline ``constant_vol`` value.",
        details=[{"vol_surface": resolved_vol_surface_id(vol_surface)}],
    )


def _swaption_base_raw(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Locate a swaption surface's ``base`` envelope in either wire shape.

    The base may sit at the top level (``payload.base`` — the legacy / flat
    shape) or one level deeper under the inner payload (``payload.payload.base``
    — the shape the portal emits, where the outer ``payload`` carries
    ``swap_index_id`` + ``payload_type`` and the inner ``payload`` carries the
    concrete spec). Returns ``None`` when neither is a mapping.
    """

    base = payload.get("base")
    if isinstance(base, Mapping):
        return base
    inner = payload.get("payload")
    if isinstance(inner, Mapping):
        inner_base = inner.get("base")
        if isinstance(inner_base, Mapping):
            return inner_base
    return None


def _swaption_vol_base(
    base_raw: Mapping[str, Any],
    *,
    default_reference_date: str,
    default_shape: int,
) -> IrVolBaseSpecT:
    """Build the shared ``IrVolBaseSpec`` envelope common to every swaption surface.

    Convention fields are read off ``base`` with documented defaults; the
    ``shape`` defaults to the caller's discriminant (Constant vs AtmMatrix2D) so
    the engine knows how to interpret the payload. The value source
    (``constantVol`` / quote) is applied by the per-payload caller, not here.
    """

    base = IrVolBaseSpecT()
    ref = base_raw.get("reference_date")
    base.referenceDate = ref if isinstance(ref, str) and ref else default_reference_date
    base.calendar = _enum(Calendar, base_raw.get("calendar"), Calendar.TARGET)
    base.businessDayConvention = _enum(
        BusinessDayConvention,
        base_raw.get("business_day_convention"),
        BusinessDayConvention.ModifiedFollowing,
    )
    base.dayCounter = _enum(DayCounter, base_raw.get("day_counter"), DayCounter.Actual365Fixed)
    base.shape = _enum(VolSurfaceShape, base_raw.get("shape"), default_shape)
    base.volatilityType = _enum(
        VolatilityType, base_raw.get("volatility_type"), VolatilityType.Lognormal
    )
    base.displacement = _float(base_raw.get("displacement"), 0.0)
    return base


def _matrix_periods(
    raw: object,
    *,
    field: str,
    vol_surface: ResolvedVolSurfaceLike,
    spec_name: str = _SWAPTION_MATRIX_PAYLOAD_TYPE,
) -> list[PeriodT]:
    """Translate a matrix axis (``[{n, unit}, …]``) into a list of ``Period``."""

    if not isinstance(raw, list) or not raw:
        raise VolSurfaceTranslationError(
            f"{spec_name} surface is missing the required ``{field}`` axis.",
            details=[{"vol_surface": resolved_vol_surface_id(vol_surface), "missing_field": field}],
        )
    periods: list[PeriodT] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise VolSurfaceTranslationError(
                f"{spec_name} ``{field}`` axis entry is not a ``{{n, unit}}`` tenor.",
                details=[{"vol_surface": resolved_vol_surface_id(vol_surface), "field": field}],
            )
        periods.append(
            make_period(
                _int(item.get("n"), 0),
                _enum(TimeUnit, item.get("unit"), TimeUnit.Years),
            )
        )
    return periods


def _quote_matrix_2d(raw: object, *, vol_surface: ResolvedVolSurfaceLike) -> QuoteMatrix2DT:
    """Translate a ``{n_rows, n_cols, values}`` grid into a ``QuoteMatrix2D``.

    Inline values only: per invariant #8 the engine never sees a ``quote_id``,
    so ``quoteIds`` is left absent (an empty list would be encoded as a
    present-but-empty vector, which the engine rejects as a length mismatch).
    """

    if not isinstance(raw, Mapping):
        raise VolSurfaceTranslationError(
            "SwaptionVolAtmMatrixSpec surface is missing the required "
            "``vols`` grid (``{n_rows, n_cols, values}``).",
            details=[
                {"vol_surface": resolved_vol_surface_id(vol_surface), "missing_field": "vols"}
            ],
        )
    values_raw = raw.get("values")
    if not isinstance(values_raw, list) or not values_raw:
        raise VolSurfaceTranslationError(
            "SwaptionVolAtmMatrixSpec ``vols.values`` is empty.",
            details=[{"vol_surface": resolved_vol_surface_id(vol_surface)}],
        )
    values = [_float(v, 0.0) for v in values_raw]
    n_rows = _int(raw.get("n_rows") if raw.get("n_rows") is not None else raw.get("nRows"), 0)
    n_cols = _int(raw.get("n_cols") if raw.get("n_cols") is not None else raw.get("nCols"), 0)
    if n_rows * n_cols != len(values):
        raise VolSurfaceTranslationError(
            f"SwaptionVolAtmMatrixSpec ``vols`` grid is {n_rows}x{n_cols} "
            f"({n_rows * n_cols} cells) but carries {len(values)} values.",
            details=[{"vol_surface": resolved_vol_surface_id(vol_surface)}],
        )
    matrix = QuoteMatrix2DT()
    matrix.nRows = n_rows
    matrix.nCols = n_cols
    matrix.values = values
    matrix.quoteIds = None
    return matrix


def _translate_swaption_atm_matrix(
    vol_surface: ResolvedVolSurfaceLike,
    payload: Mapping[str, Any],
    *,
    default_reference_date: str,
) -> SwaptionVolAtmMatrixSpecT:
    """Translate a ``SwaptionVolAtmMatrixSpec`` surface (expiry x tenor ATM matrix).

    The concrete spec sits under the inner ``payload`` envelope:
    ``{base, expiries:[{n,unit}], tenors:[{n,unit}], vols:{n_rows,n_cols,values}}``.
    The grid values are inline (already-resolved) so no quote resolution is
    needed; the ``base`` supplies the conventions + volatility type + shape.
    """

    inner = payload.get("payload")
    if not isinstance(inner, Mapping):
        raise VolSurfaceTranslationError(
            "SwaptionVolAtmMatrixSpec surface is missing the required inner "
            "``payload`` envelope (``{base, expiries, tenors, vols}``).",
            details=[
                {"vol_surface": resolved_vol_surface_id(vol_surface), "missing_field": "payload"}
            ],
        )
    base_raw = inner.get("base")
    if not isinstance(base_raw, Mapping):
        raise VolSurfaceTranslationError(
            "SwaptionVolAtmMatrixSpec surface is missing the required ``base`` "
            "envelope (conventions + volatility type).",
            details=[
                {"vol_surface": resolved_vol_surface_id(vol_surface), "missing_field": "base"}
            ],
        )

    base = _swaption_vol_base(
        base_raw,
        default_reference_date=default_reference_date,
        default_shape=VolSurfaceShape.AtmMatrix2D,
    )
    # A matrix carries its vols in the grid; an optional inline ``constant_vol``
    # on the base is harmless (the engine reads the grid for AtmMatrix2D) but is
    # forwarded for fidelity if present. It is NOT required here (unlike the
    # constant path).
    for key in _VOL_VALUE_KEYS:
        candidate = base_raw.get(key)
        if not isinstance(candidate, bool) and isinstance(candidate, (int, float)):
            base.constantVol = float(candidate)
            break

    expiries = _matrix_periods(inner.get("expiries"), field="expiries", vol_surface=vol_surface)
    tenors = _matrix_periods(inner.get("tenors"), field="tenors", vol_surface=vol_surface)
    vols = _quote_matrix_2d(inner.get("vols"), vol_surface=vol_surface)
    if vols.nRows != len(expiries) or vols.nCols != len(tenors):
        raise VolSurfaceTranslationError(
            f"SwaptionVolAtmMatrixSpec grid {vols.nRows}x{vols.nCols} does not "
            f"match the {len(expiries)} expiries x {len(tenors)} tenors axes.",
            details=[{"vol_surface": resolved_vol_surface_id(vol_surface)}],
        )

    matrix = SwaptionVolAtmMatrixSpecT()
    matrix.base = base
    matrix.expiries = expiries
    matrix.tenors = tenors
    matrix.vols = vols
    return matrix


def _sabr_strikes(raw: object, *, vol_surface: ResolvedVolSurfaceLike) -> list[float]:
    """Translate the SABR strike-spread axis (``[double]``, rate units).

    Strikes are SPREADS from the per-node ATM forward (e.g. ``-0.02`` = -200bp);
    the same vector applies at every (expiry, tenor) node. Ordering/finiteness
    rules beyond non-emptiness are enforced engine-side (strictly-increasing,
    finite) — the translator only rejects a missing/empty/non-numeric axis.
    """

    if not isinstance(raw, list) or not raw:
        raise VolSurfaceTranslationError(
            "SwaptionSabrCalibrateSpec surface is missing the required "
            "``strikes`` axis (spreads from the per-node ATM forward).",
            details=[
                {"vol_surface": resolved_vol_surface_id(vol_surface), "missing_field": "strikes"}
            ],
        )
    strikes: list[float] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise VolSurfaceTranslationError(
                "SwaptionSabrCalibrateSpec ``strikes`` axis entry is not a number.",
                details=[{"vol_surface": resolved_vol_surface_id(vol_surface), "field": "strikes"}],
            )
        strikes.append(float(item))
    return strikes


def _quote_tensor_3d(
    raw: object,
    *,
    n_expiries: int,
    n_tenors: int,
    n_strikes: int,
    vol_surface: ResolvedVolSurfaceLike,
) -> QuoteTensor3DT:
    """Translate a ``{n_1, n_2, n_3, values}`` cube into a ``QuoteTensor3D``.

    Row-major ``n_expiries * n_tenors * n_strikes`` inline market vols. Mirrors
    :func:`_quote_matrix_2d`: inline values only — per invariant #8 the engine
    never sees a ``quote_id``, so ``quoteIds`` is left absent. Declared dims (if
    present) must agree with the axes; the flat ``values`` length must equal the
    axes product.
    """

    if not isinstance(raw, Mapping):
        raise VolSurfaceTranslationError(
            "SwaptionSabrCalibrateSpec surface is missing the required "
            "``vols`` tensor (``{n_1, n_2, n_3, values}``).",
            details=[
                {"vol_surface": resolved_vol_surface_id(vol_surface), "missing_field": "vols"}
            ],
        )
    values_raw = raw.get("values")
    if not isinstance(values_raw, list) or not values_raw:
        raise VolSurfaceTranslationError(
            "SwaptionSabrCalibrateSpec ``vols.values`` is empty.",
            details=[{"vol_surface": resolved_vol_surface_id(vol_surface)}],
        )
    values = [_float(v, 0.0) for v in values_raw]

    def _declared(*keys: str) -> int | None:
        for key in keys:
            if raw.get(key) is not None:
                return _int(raw.get(key), 0)
        return None

    declared = (_declared("n_1", "n1"), _declared("n_2", "n2"), _declared("n_3", "n3"))
    axes = (n_expiries, n_tenors, n_strikes)
    for dim_declared, dim_axis, label in zip(declared, axes, ("n_1", "n_2", "n_3"), strict=True):
        if dim_declared is not None and dim_declared != dim_axis:
            raise VolSurfaceTranslationError(
                f"SwaptionSabrCalibrateSpec ``vols.{label}`` is {dim_declared} but the "
                f"axes declare {n_expiries} expiries x {n_tenors} tenors x "
                f"{n_strikes} strikes.",
                details=[{"vol_surface": resolved_vol_surface_id(vol_surface)}],
            )
    expected = n_expiries * n_tenors * n_strikes
    if len(values) != expected:
        raise VolSurfaceTranslationError(
            f"SwaptionSabrCalibrateSpec ``vols`` tensor must carry "
            f"{n_expiries}x{n_tenors}x{n_strikes} = {expected} row-major values "
            f"but carries {len(values)}.",
            details=[{"vol_surface": resolved_vol_surface_id(vol_surface)}],
        )

    tensor = QuoteTensor3DT()
    tensor.n1 = n_expiries
    tensor.n2 = n_tenors
    tensor.n3 = n_strikes
    tensor.values = values
    tensor.quoteIds = None
    return tensor


def _translate_swaption_sabr_calibrate(
    vol_surface: ResolvedVolSurfaceLike,
    payload: Mapping[str, Any],
    *,
    default_reference_date: str,
) -> SwaptionSabrCalibrateSpecT:
    """Translate a ``SwaptionSabrCalibrateSpec`` surface (SABR smile calibration).

    The concrete spec sits under the inner ``payload`` envelope:
    ``{base, expiries:[{n,unit}], tenors:[{n,unit}], strikes:[double],
    vols:{n_1,n_2,n_3,values}, beta_fixed, beta_value, vega_weighted_smile_fit}``.
    The vols are inline (already-resolved) absolute market vols, row-major
    nExp*nTen*nStrikes; strikes are spreads from the per-node ATM forward.
    ``weights`` is a v1 parse-time rejection engine-side (per-strike weights are
    not exposed by QuantLib 1.41's calibrator) — the translator rejects it here
    with a typed 422 and never emits the field.
    """

    inner = payload.get("payload")
    if not isinstance(inner, Mapping):
        raise VolSurfaceTranslationError(
            "SwaptionSabrCalibrateSpec surface is missing the required inner "
            "``payload`` envelope (``{base, expiries, tenors, strikes, vols}``).",
            details=[
                {"vol_surface": resolved_vol_surface_id(vol_surface), "missing_field": "payload"}
            ],
        )
    base_raw = inner.get("base")
    if not isinstance(base_raw, Mapping):
        raise VolSurfaceTranslationError(
            "SwaptionSabrCalibrateSpec surface is missing the required ``base`` "
            "envelope (conventions + volatility type).",
            details=[
                {"vol_surface": resolved_vol_surface_id(vol_surface), "missing_field": "base"}
            ],
        )

    weights_raw = inner.get("weights")
    if isinstance(weights_raw, Mapping) and weights_raw.get("values"):
        raise VolSurfaceTranslationError(
            "SwaptionSabrCalibrateSpec ``weights`` are not supported in v1 — "
            "per-strike weights are not exposed by the engine's calibrator; "
            "use ``vega_weighted_smile_fit`` instead.",
            details=[{"vol_surface": resolved_vol_surface_id(vol_surface), "field": "weights"}],
        )

    base = _swaption_vol_base(
        base_raw,
        default_reference_date=default_reference_date,
        default_shape=VolSurfaceShape.SabrCalibrate,
    )

    expiries = _matrix_periods(
        inner.get("expiries"),
        field="expiries",
        vol_surface=vol_surface,
        spec_name=_SWAPTION_SABR_CALIBRATE_PAYLOAD_TYPE,
    )
    tenors = _matrix_periods(
        inner.get("tenors"),
        field="tenors",
        vol_surface=vol_surface,
        spec_name=_SWAPTION_SABR_CALIBRATE_PAYLOAD_TYPE,
    )
    strikes = _sabr_strikes(inner.get("strikes"), vol_surface=vol_surface)
    vols = _quote_tensor_3d(
        inner.get("vols"),
        n_expiries=len(expiries),
        n_tenors=len(tenors),
        n_strikes=len(strikes),
        vol_surface=vol_surface,
    )

    beta_fixed_raw = inner.get("beta_fixed", inner.get("betaFixed"))
    vega_weighted_raw = inner.get("vega_weighted_smile_fit", inner.get("vegaWeightedSmileFit"))
    beta_value_raw = inner.get("beta_value", inner.get("betaValue"))

    spec = SwaptionSabrCalibrateSpecT()
    spec.base = base
    spec.expiries = expiries
    spec.tenors = tenors
    spec.strikes = strikes
    spec.vols = vols
    spec.betaFixed = beta_fixed_raw if isinstance(beta_fixed_raw, bool) else True
    spec.betaValue = _float(beta_value_raw, 0.5)
    spec.vegaWeightedSmileFit = vega_weighted_raw if isinstance(vega_weighted_raw, bool) else False
    # Never emit ``weights`` (engine v1 parse-time rejection; see above).
    spec.weights = None
    return spec


def _translate_swaption_constant(
    vol_surface: ResolvedVolSurfaceLike,
    payload: Mapping[str, Any],
    quote_map: Mapping[str, float],
    *,
    default_reference_date: str,
    missing_quotes: list[str],
) -> SwaptionVolConstantSpecT:
    """Translate a constant-vol swaption surface → ``SwaptionVolConstantSpec``.

    The ``base`` may be top-level (legacy) or nested under the inner payload
    (portal wire); either way the constant vol value source is required.
    """

    base_raw = _swaption_base_raw(payload)
    if base_raw is None:
        raise VolSurfaceTranslationError(
            "SwaptionVolSpec surface is missing the required ``base`` envelope "
            "(constant-vol value source).",
            details=[
                {
                    "vol_surface": resolved_vol_surface_id(vol_surface),
                    "missing_field": "base",
                }
            ],
        )
    base = _swaption_vol_base(
        base_raw,
        default_reference_date=default_reference_date,
        default_shape=VolSurfaceShape.Constant,
    )
    _apply_vol_value(vol_surface, base, base_raw, quote_map, missing_quotes)
    constant_spec = SwaptionVolConstantSpecT()
    constant_spec.base = base
    return constant_spec


def _translate_swaption_vol_surface(
    vol_surface: ResolvedVolSurfaceLike,
    quote_map: Mapping[str, float],
    *,
    default_reference_date: str,
    missing_quotes: list[str],
) -> VolSurfaceSpecT:
    """Translate a resolved ``SwaptionVolSpec`` surface → ``VolSurfaceSpec``.

    Dispatches on the surface ``payload_type``:

    * ``SwaptionVolAtmMatrixSpec`` → the ATM expiry x tenor vol matrix so a
      real term-structured surface reaches the engine (was a hard 422 before).
    * ``SwaptionSabrCalibrateSpec`` → the SABR smile-calibration cube
      (expiry x tenor x strike-spread market vols + beta policy).
    * ``SwaptionVolConstantSpec`` / base-at-top-level → the constant-vol path,
      unchanged.
    """

    payload = vol_surface.payload or {}
    payload_type = _first_str(payload, _PAYLOAD_TYPE_KEYS)

    swaption_vol = SwaptionVolSpecT()
    swaption_vol.swapIndexId = _first_str(payload, _SWAP_INDEX_KEYS) or DEFAULT_SWAP_INDEX_ID
    if payload_type == _SWAPTION_MATRIX_PAYLOAD_TYPE:
        swaption_vol.payloadType = SwaptionVolPayload.SwaptionVolAtmMatrixSpec
        swaption_vol.payload = _translate_swaption_atm_matrix(
            vol_surface, payload, default_reference_date=default_reference_date
        )
    elif payload_type == _SWAPTION_SABR_CALIBRATE_PAYLOAD_TYPE:
        swaption_vol.payloadType = SwaptionVolPayload.SwaptionSabrCalibrateSpec
        swaption_vol.payload = _translate_swaption_sabr_calibrate(
            vol_surface, payload, default_reference_date=default_reference_date
        )
    else:
        swaption_vol.payloadType = SwaptionVolPayload.SwaptionVolConstantSpec
        swaption_vol.payload = _translate_swaption_constant(
            vol_surface,
            payload,
            quote_map,
            default_reference_date=default_reference_date,
            missing_quotes=missing_quotes,
        )

    spec = VolSurfaceSpecT()
    spec.id = resolved_vol_surface_id(vol_surface)
    spec.payloadType = VolPayload.SwaptionVolSpec
    spec.payload = swaption_vol
    return spec


def _apply_black_vol_value(
    vol_surface: ResolvedVolSurfaceLike,
    base: BlackVolBaseSpecT,
    base_raw: Mapping[str, Any],
    quote_map: Mapping[str, float],
    missing_quotes: list[str],
) -> None:
    """Write the surface's constant vol into ``base.constantVol``; drop ``quote_id``.

    The equity-side analogue of :func:`_apply_vol_value`: the FB
    :class:`BlackVolBaseSpec` carries a native ``quoteId`` field, but per
    invariant #8 the engine must never see one — so we only ever set
    ``constantVol`` and leave ``quoteId`` unset. An unresolved ``quote_id`` is
    appended to ``missing_quotes`` (batched into one 422 by the caller); a base
    with no value source at all is a surface-translation error.
    """

    quote_id = _first_str(base_raw, _QUOTE_KEYS)
    if quote_id is not None:
        if quote_id in quote_map:
            base.constantVol = float(quote_map[quote_id])
        else:
            missing_quotes.append(quote_id)
        return

    for key in _VOL_VALUE_KEYS:
        candidate = base_raw.get(key)
        if not isinstance(candidate, bool) and isinstance(candidate, (int, float)):
            base.constantVol = float(candidate)
            return

    raise VolSurfaceTranslationError(
        "BlackVolSpec base has neither a ``quote_id`` nor an inline ``constant_vol`` value.",
        details=[{"vol_surface": resolved_vol_surface_id(vol_surface)}],
    )


def _translate_black_vol_surface(
    vol_surface: ResolvedVolSurfaceLike,
    quote_map: Mapping[str, float],
    *,
    default_reference_date: str,
    missing_quotes: list[str],
) -> VolSurfaceSpecT:
    """Translate a resolved equity ``BlackVolSpec`` surface → ``VolSurfaceSpec`` (constant path).

    Lossy-translation caveat: only the **base-value** (constant-vol) path
    ships here. The value source is read off ``payload.base``
    (quote-substituted) and the surface ``shape`` is pinned to
    :attr:`VolSurfaceShape.Constant`. A richer payload carried alongside ``base``
    (an ``AtmMatrix2D`` term/strike grid, a ``SmileCube3D`` cube, a
    ``SurfaceFromPrices`` price grid, or SABR params) is an *extra*
    (preserved-ignored, forward-compat) — the engine consumes the base value
    faithfully but the richer surface is **not** translated here. Full
    matrix / cube / SABR payload translation is deferred (same caveat the
    swaption side flags); this is a minimal-contract read, not a canonical
    fallback. Mirrors :func:`_translate_swaption_vol_surface`.
    """

    payload = vol_surface.payload or {}
    base_raw = payload.get("base")
    if not isinstance(base_raw, Mapping):
        raise VolSurfaceTranslationError(
            "BlackVolSpec surface is missing the required ``base`` envelope "
            "(constant-vol value source).",
            details=[
                {
                    "vol_surface": resolved_vol_surface_id(vol_surface),
                    "missing_field": "base",
                }
            ],
        )

    base = BlackVolBaseSpecT()
    ref = base_raw.get("reference_date")
    base.referenceDate = ref if isinstance(ref, str) and ref else default_reference_date
    base.calendar = _enum(Calendar, base_raw.get("calendar"), Calendar.TARGET)
    base.businessDayConvention = _enum(
        BusinessDayConvention,
        base_raw.get("business_day_convention"),
        BusinessDayConvention.ModifiedFollowing,
    )
    base.dayCounter = _enum(DayCounter, base_raw.get("day_counter"), DayCounter.Actual365Fixed)
    # base-value path — the engine reads the constant vol off the base.
    base.shape = VolSurfaceShape.Constant
    _apply_black_vol_value(vol_surface, base, base_raw, quote_map, missing_quotes)

    black_vol = BlackVolSpecT()
    black_vol.base = base

    spec = VolSurfaceSpecT()
    spec.id = resolved_vol_surface_id(vol_surface)
    spec.payloadType = VolPayload.BlackVolSpec
    spec.payload = black_vol
    return spec


def translate_vol_surface(
    vol_surface: ResolvedVolSurfaceLike,
    quote_map: Mapping[str, float],
    *,
    default_reference_date: str,
    missing_quotes: list[str],
) -> VolSurfaceSpecT:
    """Translate one resolved vol surface → a ``VolSurfaceSpec`` (kind dispatch).

    Maps ``SwaptionVolSpec`` (swaption) and ``BlackVolSpec`` (equity).
    An unmapped kind rejects (:class:`UnknownVolSurfaceKindError`) rather than
    falling back to a canonical surface.
    """

    if vol_surface.kind == "SwaptionVolSpec":
        return _translate_swaption_vol_surface(
            vol_surface,
            quote_map,
            default_reference_date=default_reference_date,
            missing_quotes=missing_quotes,
        )
    if vol_surface.kind == "BlackVolSpec":
        return _translate_black_vol_surface(
            vol_surface,
            quote_map,
            default_reference_date=default_reference_date,
            missing_quotes=missing_quotes,
        )
    raise UnknownVolSurfaceKindError(vol_surface.kind)


def translate_model(model: ResolvedModelLike) -> ModelSpecT:
    """Translate one resolved model → a ``ModelSpec``.

    Only the swaption ``SwaptionModelSpec`` translation ships here; the cds /
    equity model branches add their kind dispatch here. The swaption
    model type is read off ``kind`` by ``IrModelType`` member name (loose —
    defaults to ``HullWhiteLattice``, the production swaption model); the
    Hull-White knobs (``hw_a`` / ``hw_sigma`` / ``lattice_steps``) are read off
    the payload when present. The per-trade ``model`` ref honours the resolved
    entity id, never ``CANONICAL_MODEL_ID``.
    """

    config = model.payload or {}
    swaption_model = SwaptionModelSpecT()
    swaption_model.modelType = _enum(IrModelType, model.kind, IrModelType.HullWhiteLattice)
    hw_a = config.get("hw_a")
    if not isinstance(hw_a, bool) and isinstance(hw_a, (int, float)):
        swaption_model.hwA = float(hw_a)
    hw_sigma = config.get("hw_sigma")
    if not isinstance(hw_sigma, bool) and isinstance(hw_sigma, (int, float)):
        swaption_model.hwSigma = float(hw_sigma)
    lattice_steps = config.get("lattice_steps")
    if not isinstance(lattice_steps, bool) and isinstance(lattice_steps, (int, float)):
        swaption_model.latticeSteps = int(lattice_steps)

    spec = ModelSpecT()
    spec.id = resolved_model_id(model)
    spec.payloadType = ModelPayload.SwaptionModelSpec
    spec.payload = swaption_model
    return spec


# ---------------------------------------------------------------------------
# Credit-curve translation (cds)
# ---------------------------------------------------------------------------
# credit-curve minimal translatable contract
# -------------------------------------------------
# Per resolved credit curve (read off the product's ``ResolvedCreditCurve``,
# already validated by the cds assembler — credit curves bypass MD):
# * ``id`` / ``name`` — the per-trade ``creditCurveId`` reference.
#   ``ResolvedCreditCurve`` always carries a ``name`` so an id is always
#   derivable; never rejects.
# * ``recovery_rate`` (**required**, top-level on the resolved entity) — the
#   engine bootstrap and the flat path both consume it; the assembler pins it in
#   ``[0, 1]``. Honoured verbatim, never the canonical 40 %.
# * ``body`` — the hazard inputs. **Exactly one** of two shapes drives the spec:
#     - a positive ``flat_hazard_rate`` → the flat path. The engine prices a
#       flat curve and **ignores** any inline quotes, so
#       the translator emits ``flatHazardRate`` and **no** quotes.
#     - inline ``points`` (par-spread / upfront, no ``quote_id``) →
#       the bootstrap path. The translator emits ``quotes[]`` and pins
#       ``flatHazardRate = 0`` so the engine bootstraps the supplied term
#       structure. This is the HARD invariant the
#       :class:`CreditCurveInvariantError` guard defends.
#   A ``flat_hazard_rate`` of exactly ``0`` with no quotes is a faithful (if
#   degenerate) flat-zero curve, emitted as-is. A body with neither is a
#   :class:`CreditCurveTranslationError` (→ 422; the assembler already rejects
#   it upstream — this is the belt-and-braces copy). No canonical fallback
# * ``body`` convention fields (calendar / day-counter / interpolator /
#   reference-date / ``helper_conventions``) — read by FB enum member name when
# present, documented default otherwise (loose metadata). ``calendar`` is
#   always non-Null (defaults to ``TARGET``). The live portal sends these; the
#   defaults are a robustness fallback for loose storage.
# * any **extra** field is preserved-but-ignored (forward-compat).
# The cds *model* (``MidPoint`` engine) is pure config — not an ``app.*``
# resolved entity and never MD-resolved — so it survives as a default
# (:data:`DEFAULT_CDS_MODEL_ID`), the credit analogue of the bonds coupon pricer
# . The per-trade ``model`` ref points at that id; it is not a
# ``CANONICAL_*`` discarded-input leak.

# ``DEFAULT_CDS_MODEL_ID`` (imported from ``_fb_helpers``) is the
# ``ModelSpec.id`` the default MidPoint cds model is published under; the
# per-trade ``PriceCDS.model`` ref points at it.

_CDS_QUOTE_KEYS_PAR_SPREAD: Final[tuple[str, ...]] = (
    "quoted_par_spread",
    "quotedParSpread",
)
_CDS_QUOTE_KEYS_UPFRONT: Final[tuple[str, ...]] = ("quoted_upfront", "quotedUpfront")
_CDS_QUOTE_KEYS_RUNNING: Final[tuple[str, ...]] = ("running_coupon", "runningCoupon")
_TENOR_UNIT_SUFFIX: Final[dict[str, int]] = {
    "D": TimeUnit.Days,
    "W": TimeUnit.Weeks,
    "M": TimeUnit.Months,
    "Y": TimeUnit.Years,
}


def resolved_credit_curve_id(credit_curve: ResolvedCreditCurveLike) -> str:
    """Derive the stable engine-facing credit-curve id from a resolved entity.

    The ``app.credit_curves`` UUID as text when the curve came from a row, else
    the inline name. ``ResolvedCreditCurve`` guarantees a non-empty name, so
    this never falls back to the canonical ``credit`` id.
    """

    return str(credit_curve.id) if credit_curve.id is not None else credit_curve.name


def _build_cds_helper_conventions(raw: object) -> CdsHelperConventionsT:
    """Build the CDS helper-conventions block from the body, with ISDA defaults.

    Reads the ``helper_conventions`` sub-body when present; the defaults match
    the engine's ``cds_request.json`` fixture (Quarterly / Following /
    TwentiethIMM / Actual365Fixed / MidPoint). Loose metadata — never
    rejects.
    """

    body: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    conv = CdsHelperConventionsT()
    conv.settlementDays = _int(body.get("settlement_days"), 0)
    conv.frequency = _enum(Frequency, body.get("frequency"), Frequency.Quarterly)
    conv.businessDayConvention = _enum(
        BusinessDayConvention,
        body.get("business_day_convention"),
        BusinessDayConvention.Following,
    )
    conv.dateGenerationRule = _enum(
        DateGenerationRule,
        body.get("date_generation_rule"),
        DateGenerationRule.TwentiethIMM,
    )
    conv.lastPeriodDayCounter = _enum(
        DayCounter, body.get("last_period_day_counter"), DayCounter.Actual365Fixed
    )
    conv.settlesAccrual = bool(body.get("settles_accrual", True))
    conv.paysAtDefaultTime = bool(body.get("pays_at_default_time", True))
    conv.rebatesAccrual = bool(body.get("rebates_accrual", True))
    conv.helperModel = _enum(CdsHelperModel, body.get("helper_model"), CdsHelperModel.MidPoint)
    return conv


def _parse_tenor_string(value: str) -> PeriodT | None:
    """Parse a compact tenor string (``"5Y"`` / ``"6M"`` / ``"3W"`` / ``"30D"``).

    Returns ``None`` for an unrecognised shape so the caller can fall through to
    its other tenor encodings or reject.
    """

    text = value.strip().upper()
    if not text:
        return None
    unit = _TENOR_UNIT_SUFFIX.get(text[-1])
    digits = text[:-1]
    if unit is None or not digits:
        return None
    try:
        n = int(digits)
    except ValueError:
        return None
    return make_period(n, unit)


def _credit_quote_tenor(point: Mapping[str, Any]) -> PeriodT:
    """Read a CDS quote tenor: ``"5Y"`` string, ``{n, unit}`` object, or split keys.

    Missing / unparseable → :class:`CreditCurveTranslationError` (→ 422).
    """

    tenor = point.get("tenor")
    if isinstance(tenor, str) and tenor:
        parsed = _parse_tenor_string(tenor)
        if parsed is not None:
            return parsed
    if isinstance(tenor, Mapping) and tenor.get("n") is not None:
        return make_period(
            _int(tenor.get("n"), 0), _enum(TimeUnit, tenor.get("unit"), TimeUnit.Years)
        )
    if point.get("tenor_number") is not None:
        return make_period(
            _int(point.get("tenor_number"), 0),
            _enum(TimeUnit, point.get("tenor_time_unit"), TimeUnit.Years),
        )
    raise CreditCurveTranslationError(
        "CdsQuote point is missing a usable tenor "
        '(``"5Y"`` / ``{n, unit}`` / ``tenor_number``+``tenor_time_unit``).',
        details=[{"role": "credit", "missing_field": "tenor"}],
    )


def _build_credit_curve_quote(index: int, raw: Mapping[str, Any]) -> CdsQuoteT:
    """Translate one inline credit-curve point → a ``CdsQuote`` (par-spread / upfront).

    The value is **inline** (credit curves never carry ``quote_id``s); a
    point that nonetheless references one, or carries neither a par-spread nor
    an upfront, is a :class:`CreditCurveTranslationError` (→ 422).
    """

    if _first_str(raw, _QUOTE_KEYS) is not None:
        raise CreditCurveTranslationError(
            f"Credit-curve point {index} references a vendor quote id; credit "
            "curves carry inline hazard inputs only.",
            details=[{"role": "credit", "point_index": index}],
        )

    quote = CdsQuoteT()
    quote.tenor = _credit_quote_tenor(raw)

    declared = raw.get("quote_type") or raw.get("quoteType")
    par = _opt_number(raw, _CDS_QUOTE_KEYS_PAR_SPREAD)
    upfront = _opt_number(raw, _CDS_QUOTE_KEYS_UPFRONT)

    want_upfront = declared == "Upfront" or (par is None and upfront is not None)
    if want_upfront:
        if upfront is None:
            raise CreditCurveTranslationError(
                f"Credit-curve point {index} is an Upfront quote but carries no "
                "``quoted_upfront`` value.",
                details=[{"role": "credit", "point_index": index}],
            )
        quote.quoteType = CdsQuoteType.Upfront
        quote.quotedUpfront = upfront
        running = _opt_number(raw, _CDS_QUOTE_KEYS_RUNNING)
        quote.runningCoupon = running if running is not None else 0.0
        return quote

    if par is None:
        raise CreditCurveTranslationError(
            f"Credit-curve point {index} has neither an inline "
            "``quoted_par_spread`` nor a ``quoted_upfront`` value.",
            details=[{"role": "credit", "point_index": index}],
        )
    quote.quoteType = CdsQuoteType.ParSpread
    quote.quotedParSpread = par
    return quote


def _opt_number(inner: Mapping[str, Any], keys: tuple[str, ...]) -> float | None:
    """Return the first key whose value is a real (non-bool) number, else ``None``."""

    for key in keys:
        value = inner.get(key)
        if not isinstance(value, bool) and isinstance(value, (int, float)):
            return float(value)
    return None


def _guard_credit_curve_invariant(spec: CreditCurveSpecT) -> None:
    """Reject a spec that pairs inline quotes with a non-zero flat hazard.

    Belt-and-braces: the translator never builds this pairing, but a future
    refactor that did would silently lose the term structure to the engine's
    flat-curve override in the engine. See :class:`CreditCurveInvariantError`.
    """

    # the bootstrap path now leaves flatHazardRate None (absent on the
    # wire, 0.2.0 presence semantics); only a genuine POSITIVE flat hazard
    # paired with quotes is the illegal pairing this guards.
    if spec.quotes and spec.flatHazardRate not in (None, 0.0):
        raise CreditCurveInvariantError(spec.flatHazardRate, len(spec.quotes))


def translate_credit_curve(
    credit_curve: ResolvedCreditCurveLike,
    *,
    default_reference_date: str,
) -> CreditCurveSpecT:
    """Translate one resolved credit curve → a faithful ``CreditCurveSpec``.

    Flat path (positive ``flat_hazard_rate``) emits ``flatHazardRate`` and no
    quotes; bootstrap path (inline par-spread / upfront ``points``) emits
    ``quotes[]`` and pins ``flatHazardRate = 0`` (the HARD invariant). Never the
    canonical 2 % / 40 % fixture. Credit curves bypass MD, so there
    is no quote substitution here.
    """

    body = credit_curve.body or {}
    spec = CreditCurveSpecT()
    spec.id = resolved_credit_curve_id(credit_curve)
    ref = body.get("reference_date")
    spec.referenceDate = ref if isinstance(ref, str) and ref else default_reference_date
    spec.calendar = _enum(Calendar, body.get("calendar"), Calendar.TARGET)
    spec.dayCounter = _enum(DayCounter, body.get("day_counter"), DayCounter.Actual365Fixed)
    spec.recoveryRate = float(credit_curve.recovery_rate)
    spec.curveInterpolator = _enum(
        Interpolator,
        body.get("curve_interpolator") or body.get("interpolator"),
        Interpolator.LogLinear,
    )
    spec.helperConventions = _build_cds_helper_conventions(body.get("helper_conventions"))

    flat = _opt_number(body, ("flat_hazard_rate", "flatHazardRate"))
    if flat is not None and flat > 0.0:
        # Flat path: the engine ignores quotes when flat_hazard_rate > 0,
        # so we emit the hazard rate and no quotes.
        spec.flatHazardRate = flat
    else:
        points = body.get("points") or body.get("quotes")
        quotes: list[CdsQuoteT] = []
        if isinstance(points, list):
            for index, raw in enumerate(points):
                if not isinstance(raw, Mapping):
                    raise CreditCurveTranslationError(
                        f"Credit-curve point {index} is not an object.",
                        details=[{"role": "credit", "point_index": index}],
                    )
                quotes.append(_build_credit_curve_quote(index, raw))
        if quotes:
            # Bootstrap path: the term structure is bootstrapped from the
            # quotes. The engine's credit-curve construction is presence-
            # based: a PRESENT flat_hazard_rate (incl. a genuine 0) pins a
            # flat-hazard curve and IGNORES the quotes; only an ABSENT
            # flat_hazard_rate bootstraps from them. Because the request
            # builder forces defaults, the historical ``flatHazardRate = 0.0``
            # would land as a present 0 and silently zero out the credit curve
            # (protection leg → 0). Leave it None so it is OMITTED from the
            # wire → the quotes drive the bootstrap.
            spec.quotes = quotes
            spec.flatHazardRate = None
        elif flat is not None:
            # A flat-zero curve with no quotes — faithful (if degenerate).
            spec.flatHazardRate = flat
        else:
            raise CreditCurveTranslationError(
                "Credit-curve body has neither a positive ``flat_hazard_rate`` "
                "nor inline par-spread / upfront quotes; cannot bootstrap.",
                details=[
                    {"role": "credit", "credit_curve": resolved_credit_curve_id(credit_curve)}
                ],
            )

    _guard_credit_curve_invariant(spec)
    return spec


# ---------------------------------------------------------------------------
# Equity spot + underlier translation (equity_options)
# ---------------------------------------------------------------------------
# equity underlier minimal translatable contract
# -----------------------------------------------------
# Per resolved spot (read off the product's ``ResolvedSpotQuote``, already
# validated by the equity assembler):
# * a **value source** (**required**) — either an inline ``value`` (the
#   ``SpotQuoteRef`` inline branch, which short-circuits MD) or a ``canonical_id``
# that resolves against the MD map (equity options does NOT bypass MD).
#   An unresolved ``canonical_id`` → :class:`QuoteResolutionError` (→ 422, batched
#   with the curve / surface quotes); neither present →
#   :class:`SpotTranslationError` (→ 422). The resolved
#   value is written into the underlier-spot ``QuoteSpec.value`` and no quote id
#   reaches the engine.
# * ``canonical_id`` — the resolved spot quote id: the ``QuoteSpec.id`` the
#   ``EquityUnderlyingSpec.spotQuoteId`` references, and the underlier id the
#   per-trade ``EquityOption.underlyingId`` references. An inline-only spot (no
#   canonical id) falls back to the documented default ids below — a structural
#   placeholder for a not-yet-identified underlier, not a discarded resolved
#   input.
# The equity *model* (analytic Black-Scholes engine) is pure config — not an
# ``app.*`` resolved entity and never MD-resolved — so it survives as a default
# (:data:`DEFAULT_EQUITY_MODEL_ID`), the equity analogue of the cds MidPoint
# model. The per-trade ``model`` ref points at that id; it is not a
# ``CANONICAL_*`` discarded-input leak.

# ``DEFAULT_EQUITY_MODEL_ID`` (imported from ``_fb_helpers``) is the
# ``ModelSpec.id`` the default analytic Black-Scholes equity model is
# published under; the per-trade ``PriceEquityOption.model`` ref points at it.

DEFAULT_EQUITY_SPOT_QUOTE_ID: Final[str] = "equity_spot"
"""``QuoteSpec.id`` an inline-only spot (no ``canonical_id``) is published under;
the ``EquityUnderlyingSpec.spotQuoteId`` references the same id."""

DEFAULT_EQUITY_UNDERLYING_ID: Final[str] = "equity_underlying"
"""``EquityUnderlyingSpec.id`` an inline-only spot (no ``canonical_id``) is
published under; the per-trade ``EquityOption.underlyingId`` references it."""

DEFAULT_EQUITY_CURRENCY: Final[str] = "USD"
"""Currency the ``EquityUnderlyingSpec`` carries (loose engine metadata —
the resolved shapes do not expose the underlier currency to the translator)."""


def resolved_spot_quote_id(spot: ResolvedSpotQuoteLike) -> str:
    """Derive the stable engine-facing spot quote id from a resolved entity.

    The spot's vendor ``canonical_id`` when one was supplied, else the documented
    inline-spot default. The ``EquityUnderlyingSpec.spotQuoteId`` references the
    same id.
    """

    return spot.canonical_id or DEFAULT_EQUITY_SPOT_QUOTE_ID


def resolved_underlier_id(spot: ResolvedSpotQuoteLike) -> str:
    """Derive the stable engine-facing underlier id from a resolved entity.

    The underlier is identified by its spot's market identity, so the resolved
    ``canonical_id`` is the faithful id when supplied; an inline-only spot falls
    back to the documented default. The per-trade ``EquityOption.underlyingId``
    references the same id, never ``CANONICAL_EQUITY_UNDERLYING_ID``.
    """

    return spot.canonical_id or DEFAULT_EQUITY_UNDERLYING_ID


def dividend_curve_id(resolved: ResolvedMarketData) -> str:
    """Return the resolved dividend-yield curve id the underlier references.

    Reads the dividend role off the role map; falls back to the second resolved
    curve (the equity assembler hands the route discount-first, dividend-second)
    and finally the first, so the path stays robust for a role-less caller.
    ``build_pricing_from_resolved`` has already rejected the empty-curve case.
    """

    by_role = resolved.curve_id_for_role(CurveRole.DIVIDEND)
    if by_role is not None:
        return by_role
    curves = resolved.curves
    return resolved_curve_id(curves[1] if len(curves) > 1 else curves[0])


def translate_spot_quote(
    spot: ResolvedSpotQuoteLike,
    quote_map: Mapping[str, float],
    *,
    missing_quotes: list[str],
) -> QuoteSpecT:
    """Translate the resolved equity spot → the underlier-spot ``QuoteSpec``.

    The id honours the resolved entity (:func:`resolved_spot_quote_id`); the
    value is the inline ``value`` when supplied (the MD short-circuit branch),
    else the MD-resolved value for the spot's ``canonical_id``. An unresolved
    ``canonical_id`` is appended to ``missing_quotes`` (batched into the request's
    single :class:`QuoteResolutionError`); a spot with no value source at all is a
    :class:`SpotTranslationError`. The engine never sees a quote id (invariant #8).
    """

    quote = QuoteSpecT()
    quote.id = resolved_spot_quote_id(spot)
    quote.kind = QuoteKind.Price
    quote.quoteType = QuoteType.Curve

    if spot.value is not None:
        quote.value = float(spot.value)
        return quote
    if spot.canonical_id:
        if spot.canonical_id in quote_map:
            quote.value = float(quote_map[spot.canonical_id])
        else:
            missing_quotes.append(spot.canonical_id)
        return quote

    raise SpotTranslationError(
        "Equity spot has neither an inline ``value`` nor a ``canonical_id``.",
        details=[{"role": "spot"}],
    )


def _build_equity_underlying(resolved: ResolvedMarketData) -> EquityUnderlyingSpecT:
    """Build the ``EquityUnderlyingSpec`` from the resolved spot + dividend curve.

    The id / spot-quote id honour the resolved spot entity and the
    ``dividendYieldCurveId`` honours the resolved dividend curve id — never the
    canonical underlier / dividend ids. ``resolved.spot`` is non-``None`` at the
    one call site.
    """

    spot = resolved.spot
    assert spot is not None  # guarded by the caller (spot-present branch)
    underlying = EquityUnderlyingSpecT()
    underlying.id = resolved_underlier_id(spot)
    underlying.currency = DEFAULT_EQUITY_CURRENCY
    underlying.spotQuoteId = resolved_spot_quote_id(spot)
    underlying.dividendYieldCurveId = dividend_curve_id(resolved)
    return underlying


# ---------------------------------------------------------------------------
# Inflation index + curve translation (swaps_inflation)
# ---------------------------------------------------------------------------
# inflation minimal translatable contract
# ----------------------------------------------
# An inflation swap bundles three resolved inputs:
# * the **nominal** discount curve — a plain rates ``ResolvedCurve`` whose helper
#   points translate through :func:`translate_curve` into ``rates.curves`` (the
#   per-trade ``discountingCurve`` and the inflation curve's ``discountCurveId``
# both reference its resolved id). It rides in :attr:`ResolvedMarketData.curves`.
# * the **inflation** ``ResolvedCurve`` — rates-shaped storage (it flows
#   through MD) but its helper points are ZCIIS / YYIIS inflation-swap helpers,
#   so it is translated by :func:`translate_inflation_curve` into an
#   ``InflationCurveSpec`` under ``Pricing.inflation.inflationCurves`` (not
# ``rates.curves``). Each point's ``quoteValue`` is quote-substituted
#   exactly like a rates point; the engine never sees a ``quote_id`` (invariant #8).
# * the ``ResolvedInflationIndex`` — **not** MD-resolved: its body's
#   historical fixings + conventions are consumed verbatim by
#   :func:`translate_inflation_index` into an ``InflationIndexSpec`` under
#   ``Pricing.inflation.inflationIndices``. The per-trade ``inflationIndexId`` and
#   the inflation curve's ``indexId`` both reference its resolved ``index_id``
# , never ``CANONICAL_INFLATION_INDEX_ID``.
# The **ZCIIS-vs-YYIIS discriminator** is read off
# ``shared_inputs["swap_kind"]`` by the engine_io and threaded in as the
# ``inflation_swap_kind`` argument to :func:`build_pricing_from_resolved`; it
# selects the ``InflationCurveKind`` (ZeroInflation / YoYInflation) for both the
# index and the curve, and the helper kind for each curve point. It is **never**
# moved into :class:`ResolvedMarketData` (keeping the seam intact).

DEFAULT_INFLATION_CURRENCY: Final[str] = "EUR"
"""Currency an ``InflationIndexSpec`` carries when neither the resolved entity
nor its body names one (loose engine metadata; matches the canonical
EU-HICP fixture's currency)."""

_SWAP_KIND_ZERO_COUPON: Final[str] = "zero_coupon"
_SWAP_KIND_YEAR_ON_YEAR: Final[str] = "year_on_year"

_INFLATION_FAMILY_NAME_KEYS: Final[tuple[str, ...]] = (
    "family_name",
    "familyName",
    "name",
)
_INFLATION_VALUE_KEYS: Final[tuple[str, ...]] = ("quote_value", "quoteValue", "rate")
_OBSERVATION_LAG_KEYS: Final[tuple[str, ...]] = (
    "swap_observation_lag",
    "swapObservationLag",
    "observation_lag",
    "observationLag",
)
_AVAILABILITY_LAG_KEYS: Final[tuple[str, ...]] = (
    "availability_lag",
    "availabilityLag",
)


def resolved_inflation_index_id(index: ResolvedInflationIndexLike) -> str:
    """Derive the engine-facing inflation-index id from a resolved entity.

    Unlike curves (which the engine keys by ``app.*`` UUID), the engine references
    an inflation index by its **string** ``index_id`` (e.g. ``"EUHICP"``): the
    swap body's ``inflation_index_id`` and the ``InflationCurveSpec.indexId`` both
    point at it. ``ResolvedInflationIndex`` guarantees a non-empty ``index_id``,
    so this never falls back to ``CANONICAL_INFLATION_INDEX_ID``.
    """

    return index.index_id


def _period_from(
    body: Mapping[str, Any],
    keys: tuple[str, ...],
    *,
    default_n: int,
    default_unit: int,
) -> PeriodT:
    """Read a ``{"n", "unit"}`` period off the first present key, else the default."""

    for key in keys:
        raw = body.get(key)
        if isinstance(raw, Mapping) and raw.get("n") is not None:
            return make_period(
                _int(raw.get("n"), default_n),
                _enum(TimeUnit, raw.get("unit"), default_unit),
            )
    return make_period(default_n, default_unit)


def _build_inflation_fixings(
    index: ResolvedInflationIndexLike, body: Mapping[str, Any]
) -> list[FixingT]:
    """Translate the index body's historical CPI fixings verbatim.

    Each ``fixings[]`` entry is a ``{"date", "value"}`` pair (the historical CPI
    level, not a vendor quote). Absent → an empty list (faithful to a body that
    supplied none; no canonical fallback). A malformed block (not a list, or an
    entry missing a ``date`` / numeric ``value``) →
    :class:`InflationIndexTranslationError` (→ 422).
    """

    raw = body.get("fixings")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise InflationIndexTranslationError(
            "Inflation index ``fixings`` must be a list of ``{date, value}`` objects.",
            details=[{"inflation_index": resolved_inflation_index_id(index)}],
        )
    fixings: list[FixingT] = []
    for position, entry in enumerate(raw):
        date_value = entry.get("date") if isinstance(entry, Mapping) else None
        level = entry.get("value") if isinstance(entry, Mapping) else None
        if (
            not isinstance(date_value, str)
            or not date_value
            or isinstance(level, bool)
            or not isinstance(level, (int, float))
        ):
            raise InflationIndexTranslationError(
                f"Inflation index fixing {position} is missing a ``date`` "
                "string or a numeric ``value``.",
                details=[
                    {
                        "inflation_index": resolved_inflation_index_id(index),
                        "fixing_index": position,
                    }
                ],
            )
        fixing = FixingT()
        fixing.date = date_value
        fixing.value = float(level)
        fixings.append(fixing)
    return fixings


def translate_inflation_index(
    index: ResolvedInflationIndexLike, *, swap_kind: str
) -> InflationIndexSpecT:
    """Translate one resolved inflation index → a faithful ``InflationIndexSpec``.

    The id honours the resolved entity's ``index_id``; the family name /
    conventions / fixings are read off the body with documented defaults for
    loose storage, never the canonical EU-HICP fixture. The
    ``kind`` follows the ``swap_kind`` discriminator (ZeroInflation for ZCIIS,
    YoYInflation for YYIIS) unless the body pins it explicitly. The index body
    bypasses MD — there is no quote substitution here.
    """

    body = index.body or {}
    spec = InflationIndexSpecT()
    spec.id = resolved_inflation_index_id(index)
    spec.familyName = _first_str(body, _INFLATION_FAMILY_NAME_KEYS) or index.name
    spec.currency = index.currency or _first_str(body, ("currency",)) or DEFAULT_INFLATION_CURRENCY
    spec.calendar = _enum(Calendar, body.get("calendar"), Calendar.TARGET)
    spec.dayCounter = _enum(
        DayCounter,
        index.day_counter or body.get("day_counter"),
        DayCounter.Actual365Fixed,
    )
    spec.frequency = _enum(Frequency, body.get("frequency"), Frequency.Monthly)
    spec.availabilityLag = _period_from(
        body, _AVAILABILITY_LAG_KEYS, default_n=2, default_unit=TimeUnit.Months
    )
    spec.observationLag = _period_from(
        body, _OBSERVATION_LAG_KEYS, default_n=3, default_unit=TimeUnit.Months
    )
    spec.interpolated = bool(body.get("interpolated", True))
    spec.revised = bool(body.get("revised", False))
    spec.kind = _enum(
        InflationCurveKind,
        body.get("kind"),
        InflationCurveKind.YoYInflation
        if swap_kind == _SWAP_KIND_YEAR_ON_YEAR
        else InflationCurveKind.ZeroInflation,
    )
    spec.fixings = _build_inflation_fixings(index, body)
    return spec


def _inflation_curve_tenor(point: Mapping[str, Any]) -> PeriodT:
    """Read an inflation helper tenor: ``"5Y"`` string, ``{n, unit}``, or split keys.

    Missing / unparseable → :class:`CurveTranslationError` (→ 422; the inflation
    curve shares the product's ``swap_inflation_curve_resolution_failed`` code).
    """

    tenor = point.get("tenor")
    if isinstance(tenor, str) and tenor:
        parsed = _parse_tenor_string(tenor)
        if parsed is not None:
            return parsed
    if isinstance(tenor, Mapping) and tenor.get("n") is not None:
        return make_period(
            _int(tenor.get("n"), 0), _enum(TimeUnit, tenor.get("unit"), TimeUnit.Years)
        )
    if point.get("tenor_number") is not None:
        return make_period(
            _int(point.get("tenor_number"), 0),
            _enum(TimeUnit, point.get("tenor_time_unit"), TimeUnit.Years),
        )
    raise CurveTranslationError(
        "Inflation curve point is missing a usable tenor "
        '(``"5Y"`` / ``{n, unit}`` / ``tenor_number``+``tenor_time_unit``).',
        details=[{"role": "inflation", "missing_field": "tenor"}],
    )


def _apply_inflation_quote_value(
    point: Mapping[str, Any],
    helper: ZeroCouponInflationSwapHelperT | YearOnYearInflationSwapHelperT,
    quote_map: Mapping[str, float],
    missing_quotes: list[str],
    curve_id: str,
) -> None:
    """Write the helper's ``quoteValue``; drop ``quote_id``.

    The FB helper carries a native ``quoteId`` field, but the engine must never
    see one — so we only set ``quoteValue`` and leave ``quoteId`` unset. An
    unresolved ``quote_id`` is appended to ``missing_quotes`` (batched into one
    422 by the caller); a point with no value source is a curve-translation error.
    """

    quote_id = _first_str(point, _QUOTE_KEYS)
    if quote_id is not None:
        if quote_id in quote_map:
            helper.quoteValue = float(quote_map[quote_id])
        else:
            missing_quotes.append(quote_id)
        return

    for key in _INFLATION_VALUE_KEYS:
        candidate = point.get(key)
        if not isinstance(candidate, bool) and isinstance(candidate, (int, float)):
            helper.quoteValue = float(candidate)
            return

    raise CurveTranslationError(
        "Inflation curve point has neither a ``quote_id`` nor an inline value "
        "(``quote_value`` / ``rate``).",
        details=[{"role": "inflation", "curve": curve_id}],
    )


def _build_inflation_helper(
    point: Mapping[str, Any], *, is_yoy: bool, nominal_curve_id: str
) -> ZeroCouponInflationSwapHelperT | YearOnYearInflationSwapHelperT:
    """Build one ZCIIS / YYIIS helper's structural fields (value applied separately).

    The YYIIS helper additionally carries the ``nominalCurveId`` link QuantLib's
    YoY bootstrap requires; both read tenor / lag / conventions off the point with
    documented defaults (loose metadata).
    """

    helper: ZeroCouponInflationSwapHelperT | YearOnYearInflationSwapHelperT
    if is_yoy:
        helper = YearOnYearInflationSwapHelperT()
        helper.nominalCurveId = nominal_curve_id
    else:
        helper = ZeroCouponInflationSwapHelperT()
    helper.tenor = _inflation_curve_tenor(point)
    helper.swapObservationLag = _period_from(
        point, _OBSERVATION_LAG_KEYS, default_n=3, default_unit=TimeUnit.Months
    )
    helper.calendar = _enum(Calendar, point.get("calendar"), Calendar.TARGET)
    helper.paymentConvention = _enum(
        BusinessDayConvention,
        point.get("payment_convention"),
        BusinessDayConvention.ModifiedFollowing,
    )
    helper.dayCounter = _enum(DayCounter, point.get("day_counter"), DayCounter.Actual365Fixed)
    helper.observationInterpolation = _enum(
        CPIInterpolationType,
        point.get("observation_interpolation"),
        CPIInterpolationType.Linear,
    )
    return helper


def translate_inflation_curve(
    curve: ResolvedCurveLike,
    quote_map: Mapping[str, float],
    *,
    swap_kind: str,
    index_id: str,
    discount_curve_id: str,
    nominal_curve_id: str,
    default_reference_date: str,
    missing_quotes: list[str],
) -> InflationCurveSpecT:
    """Translate one resolved inflation curve → a faithful ``InflationCurveSpec``.

    Points are ZCIIS or YYIIS helpers chosen by ``swap_kind``; each helper's
    ``quoteValue`` is quote-substituted. The id honours the resolved
    entity, ``indexId`` the resolved inflation index id, and ``discountCurveId``
    the resolved nominal curve id — never ``CANONICAL_*``. Convention
    fields are read off the body with documented defaults for loose storage.
    """

    body = curve.body or {}
    is_yoy = swap_kind == _SWAP_KIND_YEAR_ON_YEAR
    spec = InflationCurveSpecT()
    spec.id = resolved_curve_id(curve)
    ref = curve.reference_date
    spec.referenceDate = ref.isoformat() if ref is not None else default_reference_date
    spec.calendar = _enum(Calendar, body.get("calendar"), Calendar.TARGET)
    spec.businessDayConvention = _enum(
        BusinessDayConvention,
        body.get("business_day_convention"),
        BusinessDayConvention.ModifiedFollowing,
    )
    spec.dayCounter = _enum(
        DayCounter, curve.day_counter or body.get("day_counter"), DayCounter.Actual365Fixed
    )
    spec.interpolator = _enum(Interpolator, body.get("interpolator"), Interpolator.Linear)
    spec.bootstrapAccuracy = _float(body.get("bootstrap_accuracy"), 1.0e-12)
    spec.kind = InflationCurveKind.YoYInflation if is_yoy else InflationCurveKind.ZeroInflation
    spec.indexId = index_id
    spec.discountCurveId = discount_curve_id
    spec.allowExtrapolation = bool(body.get("allow_extrapolation", True))

    points: list[InflationPointWrapperT] = []
    for position, raw in enumerate(curve.points):
        if not isinstance(raw, Mapping):
            raise CurveTranslationError(
                f"Inflation curve {spec.id} point {position} is not an object.",
                details=[{"role": "inflation", "curve": spec.id, "point_index": position}],
            )
        nested = raw.get("point")
        inner: Mapping[str, Any] = nested if isinstance(nested, Mapping) else raw
        try:
            helper = _build_inflation_helper(
                inner, is_yoy=is_yoy, nominal_curve_id=nominal_curve_id
            )
            _apply_inflation_quote_value(inner, helper, quote_map, missing_quotes, spec.id)
        except CurveTranslationError as exc:
            exc.details = [
                {"curve": spec.id, "point_index": position, **detail}
                for detail in (exc.details or [{}])
            ]
            raise
        wrapper = InflationPointWrapperT()
        wrapper.pointType = (
            InflationPoint.YearOnYearInflationSwapHelper
            if is_yoy
            else InflationPoint.ZeroCouponInflationSwapHelper
        )
        wrapper.point = helper
        points.append(wrapper)
    spec.points = points
    return spec


def _build_inflation_market_data(
    resolved: ResolvedMarketData,
    quote_map: Mapping[str, float],
    *,
    swap_kind: str,
    missing_quotes: list[str],
) -> InflationMarketDataT:
    """Build the ``InflationMarketData`` block from the resolved index + curve.

    The inflation index id / nominal-discount-curve id are derived from the
    resolved entities and threaded into the curve so the engine links the
    bootstrap. ``resolved.inflation_index`` is non-``None`` at the one call site.
    """

    index = resolved.inflation_index
    assert index is not None  # guarded by the caller (inflation-present branch)
    nominal_id = resolved.curve_id_for_role(CurveRole.NOMINAL) or resolved_curve_id(
        resolved.curves[0]
    )
    index_id = resolved_inflation_index_id(index)

    inflation = InflationMarketDataT()
    inflation.inflationIndices = [translate_inflation_index(index, swap_kind=swap_kind)]
    if resolved.inflation_curve is not None:
        inflation.inflationCurves = [
            translate_inflation_curve(
                resolved.inflation_curve,
                quote_map,
                swap_kind=swap_kind,
                index_id=index_id,
                discount_curve_id=nominal_id,
                nominal_curve_id=nominal_id,
                default_reference_date=resolved.as_of,
                missing_quotes=missing_quotes,
            )
        ]
    return inflation


def _dedup_curves(curves: Sequence[ResolvedCurveLike]) -> list[ResolvedCurveLike]:
    """Drop repeated resolved-curve ids, first-wins.

    Engine 0.5.0 (#119) REJECTS duplicate ids in ``rates.curves``; engines
    <= 0.2.0 silently kept the first. The same entity can legitimately land
    twice in ``resolved.curves`` (e.g. one curve serving both the discount
    and dividend roles, or a curve set repeating a ref) — first-wins
    reproduces the old engines' semantics exactly.
    """

    seen: set[str] = set()
    out: list[ResolvedCurveLike] = []
    for curve in curves:
        curve_id = resolved_curve_id(curve)
        if curve_id in seen:
            continue
        seen.add(curve_id)
        out.append(curve)
    return out


def build_pricing_from_resolved(
    resolved: ResolvedMarketData,
    *,
    bond_pricing_details: bool = False,
    include_coupon_pricer: bool = False,
    inflation_swap_kind: str | None = None,
) -> PricingT:
    """Build the engine's ``Pricing`` graph faithfully from resolved inputs.

    The shared surface: ``rates.curves`` from the resolved curves with quote
    substitution and resolved-id honoring. ``rates.indices``
    always carries the interim default forwarding index (the id the curve
    helpers reference) and, when a float-leg / projection index was resolved,
    that index too (:func:`_build_rates_indices`) so neither the
    curve-helper refs nor the per-trade instrument ref orphan against the
    engine's id-keyed index registry. For swaptions: when
    ``resolved.vol_surfaces`` / ``resolved.models`` are present they are
    translated into :class:`VolatilityMarketData` (vol surfaces + models).
    For cds: when ``resolved.credit_curve`` is present it is translated into
    :class:`CreditMarketData` (flat-hazard or inline par-spread bootstrap)
    and the default MidPoint cds model is added under ``volatility.models``.
    For equity options: when ``resolved.spot`` is present the underlier
    spot is emitted under ``Pricing.quotes`` (faithful value), the BlackVol
    surface rides under ``volatility.volSurfaces``, the default analytic
    Black-Scholes equity model under ``volatility.models``, and the
    :class:`EquityUnderlyingSpec` (resolved spot / dividend ids) under
    :class:`EquityMarketData`. For inflation swaps: when
    ``resolved.inflation_index`` is present the resolved index +
    inflation curve are translated into :class:`InflationMarketData`
    (``InflationIndexSpec`` + ``InflationCurveSpec``) — the index body
    consumed verbatim, the inflation-curve helpers quote-substituted,
    and the per-trade / cross-spec references honouring the resolved ids;
    ``inflation_swap_kind`` (read off ``shared_inputs`` by engine_io)
    selects the ZCIIS vs YYIIS helper / curve kind.

    The keyword options carry the per-product ``PricingOptions`` levers and the
    floating-bond coupon pricer. ``include_coupon_pricer`` adds the
    default zero-vol Black-Ibor coupon pricer under ``rates.couponPricers`` — it
    is pure config the engine's floating-bond pricer requires, not a discarded
    resolved input (a vol-surface-derived pricer is a later plan's scope), so it
    survives as a default. ``inflation_swap_kind`` selects the inflation
    helper kind (``"zero_coupon"`` default / ``"year_on_year"``) and only matters
    when ``resolved.inflation_index`` is present. Products that need none of these
    (swap_ir / swaption) leave the options at their defaults, so their ``Pricing``
    graph is unchanged.

    Raises a neutral :class:`TranslationError` subclass on any faithful-
    translation failure (the caller maps it to the product's 422); never falls
    back to a canonical fixture for a resolvable input.
    """

    if not resolved.curves:
        raise CurveTranslationError(
            "No resolved curves to translate into the engine request.",
            details=[{"resolved_curve_count": 0}],
        )

    quote_map: dict[str, float] = {q.canonical_id: float(q.value) for q in resolved.quotes}
    missing_quotes: list[str] = []

    pricing = PricingT()
    pricing.asOfDate = resolved.as_of
    pricing.settlementDate = resolved.as_of

    rates = RatesMarketDataT()
    rates.indices = _build_rates_indices(resolved)
    rates.curves = [
        translate_curve(
            curve,
            quote_map,
            default_reference_date=resolved.as_of,
            missing_quotes=missing_quotes,
        )
        for curve in _dedup_curves(resolved.curves)
    ]
    if include_coupon_pricer:
        rates.couponPricers = [build_canonical_coupon_pricer()]
    pricing.rates = rates

    # The underlier spot under Pricing.quotes (equity_options). The value is
    # the inline spot or the MD-resolved one; a spot quote miss joins the
    # same ``missing_quotes`` batch so one QuoteResolutionError covers curve /
    # surface / spot misses together. Spot presence is the equity discriminator.
    if resolved.spot is not None:
        pricing.quotes = [
            translate_spot_quote(resolved.spot, quote_map, missing_quotes=missing_quotes)
        ]

    # Credit curve under CreditMarketData (cds). Built faithfully from the
    # resolved hazard inputs (flat or inline par-spread bootstrap), never the
    # canonical 2 % / 40 % fixture. Credit curves bypass MD,
    # so the build does not touch ``missing_quotes``.
    if resolved.credit_curve is not None:
        credit = CreditMarketDataT()
        credit.creditCurves = [
            translate_credit_curve(resolved.credit_curve, default_reference_date=resolved.as_of)
        ]
        pricing.credit = credit

    # Inflation index + curve under InflationMarketData (swaps_inflation).
    # The index body is consumed verbatim; the inflation curve's helper
    # quote misses join the same ``missing_quotes`` batch so one
    # QuoteResolutionError covers curve + inflation-curve misses together.
    # The ZCIIS-vs-YYIIS helper / kind is selected by ``inflation_swap_kind``
    # (read off ``shared_inputs`` by engine_io) — never carried in
    # ``ResolvedMarketData``. Index presence is the inflation discriminator.
    if resolved.inflation_index is not None:
        pricing.inflation = _build_inflation_market_data(
            resolved,
            quote_map,
            swap_kind=inflation_swap_kind or _SWAP_KIND_ZERO_COUPON,
            missing_quotes=missing_quotes,
        )

    # Vol surfaces + models under VolatilityMarketData. The vol-surface
    # quote misses are collected into the same ``missing_quotes`` list so a
    # single QuoteResolutionError batches the curve and surface misses.
    vol_surface_specs = [
        translate_vol_surface(
            vol_surface,
            quote_map,
            default_reference_date=resolved.as_of,
            missing_quotes=missing_quotes,
        )
        for vol_surface in resolved.vol_surfaces
    ]
    model_specs = [translate_model(model) for model in resolved.models]
    # The cds model (MidPoint engine) is pure config, not a resolved
    # entity; it rides under ``volatility.models`` at the default id the
    # per-trade ``PriceCDS.model`` ref points at, the credit analogue of the
    # bonds coupon pricer.
    if resolved.credit_curve is not None:
        model_specs.append(build_canonical_cds_model(DEFAULT_CDS_MODEL_ID))
    # The equity model (analytic Black-Scholes engine) is pure config, not a
    # resolved entity; it rides under ``volatility.models`` at the default
    # id the per-trade ``PriceEquityOption.model`` ref points at, the equity
    # analogue of the cds MidPoint model. The BlackVol surface itself is a resolved
    # entity and was already translated into ``vol_surface_specs`` above.
    if resolved.spot is not None:
        model_specs.append(build_canonical_equity_model(DEFAULT_EQUITY_MODEL_ID))

    if missing_quotes:
        # De-dup while preserving first-seen order for a stable 422 detail list.
        raise QuoteResolutionError(list(dict.fromkeys(missing_quotes)))

    if vol_surface_specs or model_specs:
        volatility = VolatilityMarketDataT()
        if vol_surface_specs:
            volatility.volSurfaces = vol_surface_specs
        if model_specs:
            volatility.models = model_specs
        pricing.volatility = volatility

    # The equity underlier under EquityMarketData (equity_options). The
    # underlier id / spot-quote id honour the resolved spot and the dividend-curve
    # ref honours the resolved dividend curve id, never the canonical
    # underlier / dividend ids.
    if resolved.spot is not None:
        equity = EquityMarketDataT()
        equity.equityUnderlyings = [_build_equity_underlying(resolved)]
        pricing.equity = equity

    options = PricingOptionsT()
    options.bondPricingDetails = bond_pricing_details
    options.bondPricingFlows = False
    # Swaption diagnostics ride on the same flag the canonical builder set when a
    # swaption vol surface is present (preserves the decode surface). Keyed on the
    # SwaptionVolSpec kind so an equity BlackVolSpec surface does not flip it
    # (the canonical equity builder left it False).
    options.swaptionPricingDetails = any(
        vs.kind == "SwaptionVolSpec" for vs in resolved.vol_surfaces
    )
    pricing.options = options
    return pricing


__all__ = [
    "DEFAULT_CDS_MODEL_ID",
    "DEFAULT_EQUITY_MODEL_ID",
    "DEFAULT_EQUITY_SPOT_QUOTE_ID",
    "DEFAULT_EQUITY_UNDERLYING_ID",
    "DEFAULT_FORWARDING_INDEX_ID",
    "DEFAULT_SWAP_INDEX_ID",
    "CreditCurveInvariantError",
    "CreditCurveTranslationError",
    "CurveRole",
    "CurveTranslationError",
    "IndexRegistrationError",
    "InflationIndexTranslationError",
    "QuoteResolutionError",
    "ResolvedMarketData",
    "SpotTranslationError",
    "TranslationError",
    "UnknownHelperKindError",
    "UnknownVolSurfaceKindError",
    "VolSurfaceTranslationError",
    "build_pricing_from_resolved",
    "dividend_curve_id",
    "float_leg_frequency",
    "resolved_credit_curve_id",
    "resolved_curve_id",
    "resolved_index_id",
    "resolved_inflation_index_id",
    "resolved_model_id",
    "resolved_spot_quote_id",
    "resolved_underlier_id",
    "resolved_vol_surface_id",
    "translate_credit_curve",
    "translate_curve",
    "translate_index",
    "translate_inflation_curve",
    "translate_inflation_index",
    "translate_spot_quote",
]
