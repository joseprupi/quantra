"""Engine-format JSON document → Quantra entity mapping (``POST /v1/import``).

Pure mapping + validation, no I/O: the route in
:mod:`quantra_orchestrator.api.import_doc` feeds the caller's document
through :func:`map_document` and persists the returned items through the
existing :class:`~quantra_orchestrator.data.repository.CrudRepository`.

Accepted input ("as much engine format as possible"):

* a FULL engine price request (``{"pricing": {...}, "swaps": [...]}`` —
  any ``/price-*`` request body),
* a BARE ``pricing`` object (the value of the ``pricing`` key), or
* a bare FRAGMENT (``{"curves": [...]}`` / ``{"indices": [...]}`` …).

Both the nested engine ≥0.5.0 layout (``pricing.rates.curves`` /
``pricing.credit.credit_curves`` / ``pricing.volatility.vol_surfaces``)
and the legacy FLAT layout (``pricing.curves`` / ``pricing.indices`` /
``pricing.credit_curves`` / …, still written by saved portal products)
are accepted; the merge rule mirrors the portal's
``normalizePricingForApi`` (``frontend/src/lib/api-normalizers.ts``):
the nested domain object wins, a legacy flat key only fills a key the
nested object does not already carry.

v1 entity scope (everything else is REPORTED, never silently dropped):

===============================  ==================  =========================
Engine location                  Quantra entity      Create schema
===============================  ==================  =========================
``rates.indices[]``              index               ``IndexCreate``
``rates.curves[]``               curve               ``CurveCreate``
``credit.credit_curves[]``       credit_curve        ``CreditCurveCreate``
``volatility.vol_surfaces[]``    vol_surface         ``VolSurfaceCreate``
``volatility.models[]``          swaption_model      ``SwaptionModelCreate``
===============================  ==================  =========================

Trades (``swaps`` / ``bonds`` / ``swaptions`` / ``cds_list`` /
``options`` / ``fras`` / ``cap_floors``), ``rates.swap_indices``,
``rates.coupon_pricers``, ``inflation.*`` and ``equity.*`` produce one
``unsupported`` report entry per item (reason ``unsupported_in_v1: …``).

Quote substitution: when the document carries ``pricing.quotes[]`` with
inline values, any point / base / grid cell referencing one of those
``quote_id``s gets the VALUE substituted in (imported entities are
self-contained). A ``quote_id`` with NO matching document quote is kept
verbatim at rest for curve points and vol-surface base envelopes
(invariant #8: it resolves from market data at price time) and produces
a warning — except where the stored shape could never resolve later:
credit-curve quotes (the translator only accepts inline hazard inputs)
and vol-grid parallel ``quote_ids`` arrays (the price-time walker and
the translator both need inline grid values) are per-item ERRORS when a
referenced quote is missing from the document.

Validation = round-trip through the REAL pricing translators
(``translate_index`` / ``translate_curve`` / ``translate_credit_curve``
/ ``translate_vol_surface`` / ``translate_model``): an imported entity
is guaranteed to translate. Curves are additionally checked for index
registration against the document's own indices + the known
overnight/IBOR catalogs (mirrors ``curve_preview._build_preview_indices``).
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final

from pydantic import ValidationError

from quantra_orchestrator.data.schemas import (
    CreditCurveCreate,
    CurveCreate,
    IndexCreate,
    SwaptionModelCreate,
    VolSurfaceCreate,
)
from quantra_orchestrator.pricing._translator import (
    _KNOWN_IBOR_INDICES,
    _KNOWN_OVERNIGHT_INDICES,
    _QUOTE_KEYS,
    _VALUE_POINT_SPECS,
    DEFAULT_FORWARDING_INDEX_ID,
    TranslationError,
    _collect_helper_index_ids,
    _value_target,
    translate_credit_curve,
    translate_curve,
    translate_index,
    translate_model,
    translate_vol_surface,
)
from quantra_orchestrator.pricing.cds.models import ResolvedCreditCurve
from quantra_orchestrator.pricing.swap_ir.models import ResolvedCurve, ResolvedIndex
from quantra_orchestrator.pricing.swaption.models import (
    ResolvedSwaptionModel,
    ResolvedVolSurface,
)

# ---------------------------------------------------------------------------
# Report shapes (dataclasses; the route projects them into response models)
# ---------------------------------------------------------------------------

ENTITY_INDEX: Final[str] = "index"
ENTITY_CURVE: Final[str] = "curve"
ENTITY_CREDIT_CURVE: Final[str] = "credit_curve"
ENTITY_VOL_SURFACE: Final[str] = "vol_surface"
ENTITY_SWAPTION_MODEL: Final[str] = "swaption_model"

# Trade array keys an engine price request may carry at its top level
# (one per /price-* request family). All are out of scope v1 → reported.
_TRADE_KEYS: Final[tuple[str, ...]] = (
    "swaps",
    "bonds",
    "swaptions",
    "cds_list",
    "options",
    "fras",
    "cap_floors",
)

# Legacy flat pricing keys → the nested domain they merge into
# (mirrors the portal's ``normalizePricingForApi`` merge tables).
_RATES_LEGACY_KEYS: Final[tuple[str, ...]] = (
    "indices",
    "swap_indices",
    "curves",
    "coupon_pricers",
)
_CREDIT_LEGACY_KEYS: Final[tuple[str, ...]] = ("credit_curves",)
_VOLATILITY_LEGACY_KEYS: Final[tuple[str, ...]] = ("vol_surfaces", "models")
_INFLATION_LEGACY_KEYS: Final[tuple[str, ...]] = ("inflation_indices", "inflation_curves")
_EQUITY_LEGACY_KEYS: Final[tuple[str, ...]] = ("equity_underlyings",)

# ``app.vol_surfaces.kind`` CHECK list (migration 0005).
_VOL_SURFACE_DB_KINDS: Final[frozenset[str]] = frozenset(
    {"SwaptionVolSpec", "OptionletVolSpec", "BlackVolSpec"}
)

# Engine ``IrModelType`` member names a ``SwaptionModelSpec.model_type``
# may carry; anything else would silently translate to the HullWhiteLattice
# default, so an unknown declared model type is rejected per-item instead.
_KNOWN_MODEL_TYPES: Final[frozenset[str]] = frozenset(
    {"Black", "ShiftedBlack", "Bachelier", "HullWhiteLattice"}
)

_UNRESOLVED_QUOTE_WARNING: Final[str] = (
    "quote_id {quote_id!r} not present in document quotes; left as a "
    "reference (resolved from market data at price time)"
)


@dataclass(frozen=True)
class MappedItem:
    """One entity ready for a ``CrudRepository.create`` (values = all spec columns)."""

    entity_type: str
    source_id: str
    path: str
    values: dict[str, Any]


@dataclass(frozen=True)
class ItemError:
    entity_type: str
    source_id: str
    path: str
    reason: str


@dataclass(frozen=True)
class ItemWarning:
    entity_type: str
    source_id: str
    message: str


@dataclass(frozen=True)
class UnsupportedItem:
    section: str
    source_id: str
    path: str
    reason: str


@dataclass
class MappedDocument:
    """The full mapping result the route persists + reports."""

    items: list[MappedItem] = field(default_factory=list)
    errors: list[ItemError] = field(default_factory=list)
    warnings: list[ItemWarning] = field(default_factory=list)
    unsupported: list[UnsupportedItem] = field(default_factory=list)


class EmptyDocumentError(ValueError):
    """The document carries no recognizable engine-format content at all."""


class _ItemMappingError(ValueError):
    """Internal: one item failed mapping/validation (becomes an ``ItemError``)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Document location + nested/flat merge
# ---------------------------------------------------------------------------


def _merge_domain(current: object, legacy: Mapping[str, Any]) -> dict[str, Any]:
    """Merge a nested domain object with its legacy flat keys (nested wins).

    Mirrors the portal's ``mergeDomainFields``: a legacy flat key only fills
    a key the nested object does not already carry (absent — a present
    ``null`` blocks the fill, exactly like ``undefined`` semantics in the
    portal's TypeScript).
    """

    merged: dict[str, Any] = dict(current) if isinstance(current, Mapping) else {}
    for key, value in legacy.items():
        if key not in merged and value is not None:
            merged[key] = value
    return merged


def _list_of_mappings(section: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    raw = section.get(key)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


@dataclass(frozen=True)
class _ParsedDocument:
    """The document normalized to nested-domain sections + trade arrays."""

    rates: dict[str, Any]
    credit: dict[str, Any]
    volatility: dict[str, Any]
    inflation: dict[str, Any]
    equity: dict[str, Any]
    quotes: list[Mapping[str, Any]]
    trades: dict[str, list[Any]]


def _parse_document(document: Mapping[str, Any]) -> _ParsedDocument:
    """Locate the pricing object + trades and normalize nested/flat layouts."""

    raw_pricing = document.get("pricing")
    pricing: Mapping[str, Any] = raw_pricing if isinstance(raw_pricing, Mapping) else document

    rates = _merge_domain(pricing.get("rates"), {k: pricing.get(k) for k in _RATES_LEGACY_KEYS})
    credit = _merge_domain(pricing.get("credit"), {k: pricing.get(k) for k in _CREDIT_LEGACY_KEYS})
    volatility = _merge_domain(
        pricing.get("volatility"), {k: pricing.get(k) for k in _VOLATILITY_LEGACY_KEYS}
    )
    inflation = _merge_domain(
        pricing.get("inflation"), {k: pricing.get(k) for k in _INFLATION_LEGACY_KEYS}
    )
    equity = _merge_domain(pricing.get("equity"), {k: pricing.get(k) for k in _EQUITY_LEGACY_KEYS})

    quotes_raw = pricing.get("quotes")
    quotes = (
        [q for q in quotes_raw if isinstance(q, Mapping)] if isinstance(quotes_raw, list) else []
    )

    trades: dict[str, list[Any]] = {}
    for key in _TRADE_KEYS:
        raw = document.get(key)
        if isinstance(raw, list) and raw:
            trades[key] = list(raw)

    return _ParsedDocument(
        rates=rates,
        credit=credit,
        volatility=volatility,
        inflation=inflation,
        equity=equity,
        quotes=quotes,
        trades=trades,
    )


def _build_quote_map(quotes: list[Mapping[str, Any]]) -> dict[str, float]:
    """``pricing.quotes[] → {id: value}`` for entries with a numeric value."""

    out: dict[str, float] = {}
    for entry in quotes:
        quote_id = entry.get("id")
        value = entry.get("value")
        if (
            isinstance(quote_id, str)
            and quote_id
            and not isinstance(value, bool)
            and isinstance(value, (int, float))
        ):
            out[quote_id] = float(value)
    return out


# ---------------------------------------------------------------------------
# Shared small helpers
# ---------------------------------------------------------------------------


def _require_engine_id(raw: Mapping[str, Any], *, what: str) -> str:
    engine_id = raw.get("id")
    if not isinstance(engine_id, str) or not engine_id.strip():
        raise _ItemMappingError(f"{what} entry is missing the required string ``id``.")
    return engine_id


def _opt_str(raw: Mapping[str, Any], key: str) -> str | None:
    value = raw.get(key)
    return value if isinstance(value, str) and value else None


def _parse_date(value: object) -> date | None:
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _quote_ref(inner: Mapping[str, Any]) -> str | None:
    for key in _QUOTE_KEYS:
        value = inner.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _strip_quote_keys(inner: dict[str, Any]) -> None:
    for key in _QUOTE_KEYS:
        inner.pop(key, None)


# ---------------------------------------------------------------------------
# Indices
# ---------------------------------------------------------------------------

_INDEX_SCALAR_KEYS: Final[frozenset[str]] = frozenset(
    {"id", "index_type", "currency", "calendar", "day_counter"}
)

_INDEX_KIND_BY_ENGINE_TYPE: Final[dict[str, str]] = {
    "ibor": "IBOR",
    "iborindex": "IBOR",
    "overnight": "Overnight",
    "overnightindex": "Overnight",
    "inflation": "Inflation",
}


def _map_index(raw: Mapping[str, Any], path: str) -> MappedItem:
    """``rates.indices[]`` (engine ``IndexDef``) → ``IndexCreate`` values.

    Engine ``id`` becomes the Quantra ``name`` (it is what curve helpers
    reference); the engine display ``name`` stays in the body under the
    ``name`` key the translator's ``_INDEX_NAME_KEYS`` honours. Scalar
    columns ``currency`` / ``calendar`` / ``day_counter``; everything else
    (tenor, fixing_days, business_day_convention, end_of_month, fixings,
    extras) rides in the body verbatim.
    """

    engine_id = _require_engine_id(raw, what="rates.indices")
    kind = _INDEX_KIND_BY_ENGINE_TYPE.get(str(raw.get("index_type") or "").lower(), "IBOR")
    body = {k: copy.deepcopy(v) for k, v in raw.items() if k not in _INDEX_SCALAR_KEYS}

    try:
        create = IndexCreate(
            name=engine_id,
            kind=kind,
            currency=_opt_str(raw, "currency"),
            calendar=_opt_str(raw, "calendar"),
            day_counter=_opt_str(raw, "day_counter"),
            body=body,
        )
    except ValidationError as exc:
        raise _ItemMappingError(f"index does not fit the create schema: {exc}") from exc

    # Round-trip through the real pricing translator: mapping is only
    # accepted when the stored shape yields a faithful IndexDef.
    resolved = ResolvedIndex(
        id=None,
        name=create.name,
        kind=create.kind,
        currency=create.currency,
        calendar=create.calendar,
        day_counter=create.day_counter,
        body=create.body,
    )
    translate_index(resolved)

    return MappedItem(
        entity_type=ENTITY_INDEX,
        source_id=engine_id,
        path=path,
        values=create.model_dump(),
    )


# ---------------------------------------------------------------------------
# Curves (helper AND value-point)
# ---------------------------------------------------------------------------


def _substitute_curve_point(
    raw: Mapping[str, Any],
    quote_map: Mapping[str, float],
    unresolved: list[str],
) -> dict[str, Any]:
    """Substitute a document quote value into one curve point (wrapped or flat).

    Mirrors ``_apply_value`` / ``_value_target`` semantics: helper points
    write ``rate`` / ``price`` / ``futures_price``; value points write their
    family value key (``zero_rate`` / ``discount_factor`` / ``forward_rate``).
    A ``quote_id`` absent from the document is kept verbatim (invariant #8)
    and recorded in ``unresolved``.
    """

    out = copy.deepcopy(dict(raw))
    kind = out.get("point_type") or out.get("pointType")
    nested = out.get("point")
    inner: dict[str, Any] = nested if isinstance(nested, dict) else out

    quote_id = _quote_ref(inner)
    if quote_id is None or not isinstance(kind, str):
        return out
    if quote_id not in quote_map:
        unresolved.append(quote_id)
        return out

    value_spec = _VALUE_POINT_SPECS.get(kind)
    target = value_spec.value_keys[0] if value_spec is not None else _value_target(kind, inner)
    inner[target] = quote_map[quote_id]
    _strip_quote_keys(inner)
    return out


def _map_curve(
    raw: Mapping[str, Any],
    path: str,
    quote_map: Mapping[str, float],
    allowed_index_ids: frozenset[str],
    default_reference_date: str,
    warnings: list[ItemWarning],
) -> MappedItem:
    """``rates.curves[]`` (engine ``TermStructure``) → ``CurveCreate`` values.

    ``name`` = engine ``id``; points verbatim (quote-substituted); the body
    keeps ``interpolator`` + ``bootstrap_trait`` (NEVER dropped — the
    translator silently defaults LogLinear/Discount otherwise) plus
    ``local_id`` = engine id; ``reference_date`` / ``day_counter`` land on
    their scalar columns; ``currency`` stays NULL (absent from the engine
    ``TermStructure``).
    """

    engine_id = _require_engine_id(raw, what="rates.curves")
    raw_points = raw.get("points")
    points_in = (
        [p for p in raw_points if isinstance(p, Mapping)] if isinstance(raw_points, list) else []
    )

    unresolved: list[str] = []
    points = [_substitute_curve_point(p, quote_map, unresolved) for p in points_in]
    for quote_id in dict.fromkeys(unresolved):
        warnings.append(
            ItemWarning(
                entity_type=ENTITY_CURVE,
                source_id=engine_id,
                message=_UNRESOLVED_QUOTE_WARNING.format(quote_id=quote_id),
            )
        )

    body: dict[str, Any] = {}
    interpolator = _opt_str(raw, "interpolator")
    if interpolator is not None:
        body["interpolator"] = interpolator
    bootstrap_trait = _opt_str(raw, "bootstrap_trait") or _opt_str(raw, "bootstrapTrait")
    if bootstrap_trait is not None:
        body["bootstrap_trait"] = bootstrap_trait
    body["local_id"] = engine_id

    try:
        create = CurveCreate(
            name=engine_id,
            currency=None,
            day_counter=_opt_str(raw, "day_counter"),
            helper_kind=None,
            reference_date=_parse_date(raw.get("reference_date")),
            points=points,
            body=body,
        )
    except ValidationError as exc:
        raise _ItemMappingError(f"curve does not fit the create schema: {exc}") from exc

    # Round-trip through the real pricing translator. The quote map is
    # empty here on purpose: resolvable document quotes were already
    # substituted above, and a leftover reference must NOT fail
    # translation (it resolves from market data at price time) — the
    # translator batches it into ``missing_quotes``, which we ignore
    # because the warning was already emitted.
    resolved = ResolvedCurve(
        id=None,
        name=create.name,
        currency=create.currency,
        day_counter=create.day_counter,
        helper_kind=create.helper_kind,
        reference_date=create.reference_date,
        points=create.points,
        body=create.body,
    )
    missing_quotes: list[str] = []
    ref = create.reference_date
    translate_curve(
        resolved,
        {},
        default_reference_date=ref.isoformat() if ref is not None else default_reference_date,
        missing_quotes=missing_quotes,
    )

    # Index-registration pre-flight (mirrors curve_preview's
    # ``_build_preview_indices``): every helper index ref must resolve to a
    # document index, the default forwarding index, or the known
    # overnight / IBOR catalogs — otherwise pricing this curve later could
    # only ever 422.
    for index_id in _collect_helper_index_ids([resolved]):
        if index_id not in allowed_index_ids:
            raise _ItemMappingError(
                f"curve helper references index id {index_id!r}, which is "
                "neither defined in this document's ``rates.indices`` nor a "
                "known overnight/IBOR index."
            )

    return MappedItem(
        entity_type=ENTITY_CURVE,
        source_id=engine_id,
        path=path,
        values=create.model_dump(),
    )


# ---------------------------------------------------------------------------
# Credit curves
# ---------------------------------------------------------------------------

_CREDIT_SCALAR_KEYS: Final[frozenset[str]] = frozenset({"id", "recovery_rate"})


def _substitute_credit_quote(
    raw: Mapping[str, Any],
    quote_map: Mapping[str, float],
) -> dict[str, Any]:
    """Substitute a document quote into one engine ``CdsQuote`` entry.

    Credit curves are stored with inline hazard inputs ONLY (the
    translator rejects quote ids — they never traverse vendor MD), so a
    ``quote_id`` that does not resolve from the document is a hard
    per-item error, not a keep-verbatim warning.
    """

    out = copy.deepcopy(dict(raw))
    quote_id = _quote_ref(out)
    if quote_id is None:
        return out
    if quote_id not in quote_map:
        raise _ItemMappingError(
            f"credit-curve quote references quote_id {quote_id!r} with no "
            "matching value in ``pricing.quotes``; credit curves are stored "
            "with inline values only — include the quote in the document."
        )
    target = (
        "quoted_upfront"
        if (out.get("quote_type") or out.get("quoteType")) == "Upfront"
        else "quoted_par_spread"
    )
    out[target] = quote_map[quote_id]
    _strip_quote_keys(out)
    return out


def _map_credit_curve(
    raw: Mapping[str, Any],
    path: str,
    quote_map: Mapping[str, float],
    default_reference_date: str,
) -> MappedItem:
    """``credit.credit_curves[]`` (engine ``CreditCurveSpec``) → ``CreditCurveCreate``.

    ``source`` = ``'flat'`` when a ``flat_hazard_rate`` is present, else
    ``'manual'``; ``recovery_rate`` is required (engine 0.5.0 requires it
    too). Everything else (reference_date, calendar, day_counter,
    curve_interpolator, helper_conventions, quotes, flat_hazard_rate)
    rides in the body in exactly the shape ``translate_credit_curve``
    reads.
    """

    engine_id = _require_engine_id(raw, what="credit.credit_curves")
    recovery = raw.get("recovery_rate")
    if isinstance(recovery, bool) or not isinstance(recovery, (int, float)):
        raise _ItemMappingError(
            "credit-curve entry is missing the required numeric ``recovery_rate``."
        )

    flat = raw.get("flat_hazard_rate")
    has_flat = not isinstance(flat, bool) and isinstance(flat, (int, float))

    body = {k: copy.deepcopy(v) for k, v in raw.items() if k not in _CREDIT_SCALAR_KEYS}
    raw_quotes = body.get("quotes")
    if isinstance(raw_quotes, list):
        body["quotes"] = [
            _substitute_credit_quote(q, quote_map) if isinstance(q, Mapping) else q
            for q in raw_quotes
        ]

    try:
        create = CreditCurveCreate(
            name=engine_id,
            reference_entity=None,
            currency=None,
            seniority=None,
            source="flat" if has_flat else "manual",
            recovery_rate=float(recovery),
            body=body,
        )
    except ValidationError as exc:
        raise _ItemMappingError(f"credit curve does not fit the create schema: {exc}") from exc

    resolved = ResolvedCreditCurve(
        id=None,
        name=create.name,
        source=create.source,
        recovery_rate=create.recovery_rate,
        body=create.body,
    )
    translate_credit_curve(resolved, default_reference_date=default_reference_date)

    return MappedItem(
        entity_type=ENTITY_CREDIT_CURVE,
        source_id=engine_id,
        path=path,
        values=create.model_dump(),
    )


# ---------------------------------------------------------------------------
# Vol surfaces
# ---------------------------------------------------------------------------


def _substitute_vol_base(
    payload: dict[str, Any],
    source_id: str,
    quote_map: Mapping[str, float],
    warnings: list[ItemWarning],
) -> None:
    """Substitute the ``base`` envelope's ``quote_id`` → ``constant_vol``.

    Handles both nestings the translator tolerates: ``payload.base`` and
    ``payload.payload.base``. An unresolvable reference is kept verbatim
    (the price-time MD walker resolves it) with a warning.
    """

    candidates: list[Any] = [payload.get("base")]
    inner = payload.get("payload")
    if isinstance(inner, dict):
        candidates.append(inner.get("base"))
    for base in candidates:
        if not isinstance(base, dict):
            continue
        quote_id = _quote_ref(base)
        if quote_id is None:
            continue
        if quote_id in quote_map:
            base["constant_vol"] = quote_map[quote_id]
            _strip_quote_keys(base)
        else:
            warnings.append(
                ItemWarning(
                    entity_type=ENTITY_VOL_SURFACE,
                    source_id=source_id,
                    message=_UNRESOLVED_QUOTE_WARNING.format(quote_id=quote_id),
                )
            )


def _substitute_vol_grids(payload: dict[str, Any], quote_map: Mapping[str, float]) -> None:
    """Substitute parallel ``quote_ids`` arrays in ``vols`` grids into values.

    The engine wire allows a grid to carry ``quote_ids`` instead of
    ``values``; the stored entity needs inline ``values`` (both the
    translator and the price-time resolution consume grid values). Every
    referenced id must therefore resolve from the document — a miss is a
    hard per-item error.
    """

    stack: list[Any] = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            grid = node.get("vols")
            if (
                isinstance(grid, dict)
                and isinstance(grid.get("quote_ids"), list)
                and not grid.get("values")
            ):
                quote_ids = grid["quote_ids"]
                missing = [q for q in quote_ids if not (isinstance(q, str) and q in quote_map)]
                if missing:
                    raise _ItemMappingError(
                        "vol grid references quote_ids with no matching value in "
                        f"``pricing.quotes``: {sorted(str(m) for m in missing)[:10]}; "
                        "grid cells are stored inline — include the quotes in the "
                        "document or supply ``values``."
                    )
                grid["values"] = [quote_map[q] for q in quote_ids]
                del grid["quote_ids"]
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def _map_vol_surface(
    raw: Mapping[str, Any],
    path: str,
    quote_map: Mapping[str, float],
    default_reference_date: str,
    warnings: list[ItemWarning],
) -> MappedItem:
    """``volatility.vol_surfaces[]`` (engine ``VolSurfaceSpec``) → ``VolSurfaceCreate``.

    ``kind`` = the OUTER ``payload_type`` (must be on the DB CHECK list);
    ``payload`` = the engine inner payload verbatim (quote-substituted).
    """

    engine_id = _require_engine_id(raw, what="volatility.vol_surfaces")
    kind = raw.get("payload_type") or raw.get("payloadType")
    if not isinstance(kind, str) or not kind:
        raise _ItemMappingError(
            "vol-surface entry is missing the required ``payload_type`` discriminator."
        )
    if kind not in _VOL_SURFACE_DB_KINDS:
        raise _ItemMappingError(
            f"unsupported_kind: vol-surface payload_type {kind!r} is not "
            f"storable (allowed: {sorted(_VOL_SURFACE_DB_KINDS)})."
        )
    raw_payload = raw.get("payload")
    if not isinstance(raw_payload, Mapping):
        raise _ItemMappingError("vol-surface entry is missing the object ``payload``.")

    payload: dict[str, Any] = copy.deepcopy(dict(raw_payload))
    _substitute_vol_base(payload, engine_id, quote_map, warnings)
    _substitute_vol_grids(payload, quote_map)

    try:
        create = VolSurfaceCreate(name=engine_id, kind=kind, payload=payload)
    except ValidationError as exc:
        raise _ItemMappingError(f"vol surface does not fit the create schema: {exc}") from exc

    resolved = ResolvedVolSurface(id=None, name=create.name, kind=create.kind, payload=payload)
    missing_quotes: list[str] = []
    translate_vol_surface(
        resolved,
        {},
        default_reference_date=default_reference_date,
        missing_quotes=missing_quotes,
    )

    return MappedItem(
        entity_type=ENTITY_VOL_SURFACE,
        source_id=engine_id,
        path=path,
        values=create.model_dump(),
    )


# ---------------------------------------------------------------------------
# Swaption models
# ---------------------------------------------------------------------------


def _map_model(raw: Mapping[str, Any], path: str) -> MappedItem:
    """``volatility.models[]`` (engine ``ModelSpec``) → ``SwaptionModelCreate``.

    Only the ``SwaptionModelSpec`` payload family maps to a Quantra entity
    (``app.swaption_models``); ``kind`` = the payload's ``model_type``
    (validated against the engine ``IrModelType`` member names so an
    unknown type cannot silently import as the HullWhiteLattice default).
    """

    engine_id = _require_engine_id(raw, what="volatility.models")
    payload_type = raw.get("payload_type") or raw.get("payloadType")
    if isinstance(payload_type, str) and payload_type != "SwaptionModelSpec":
        raise _ItemMappingError(
            f"unsupported_kind: model payload_type {payload_type!r} has no "
            "Quantra entity in v1 (only SwaptionModelSpec maps to a "
            "swaption_model)."
        )
    raw_payload = raw.get("payload")
    payload: dict[str, Any] = (
        copy.deepcopy(dict(raw_payload)) if isinstance(raw_payload, Mapping) else {}
    )
    model_type = payload.get("model_type")
    if model_type is not None and (
        not isinstance(model_type, str) or model_type not in _KNOWN_MODEL_TYPES
    ):
        raise _ItemMappingError(
            f"model ``model_type`` {model_type!r} is not a known engine "
            f"IrModelType (allowed: {sorted(_KNOWN_MODEL_TYPES)})."
        )

    kind = model_type if isinstance(model_type, str) else "HullWhiteLattice"
    try:
        create = SwaptionModelCreate(name=engine_id, kind=kind, payload=payload)
    except ValidationError as exc:
        raise _ItemMappingError(f"model does not fit the create schema: {exc}") from exc

    resolved = ResolvedSwaptionModel(
        id=None, name=create.name, kind=create.kind, payload=create.payload
    )
    translate_model(resolved)

    return MappedItem(
        entity_type=ENTITY_SWAPTION_MODEL,
        source_id=engine_id,
        path=path,
        values=create.model_dump(),
    )


# ---------------------------------------------------------------------------
# Unsupported-section reporting
# ---------------------------------------------------------------------------


def _report_unsupported(parsed: _ParsedDocument, out: MappedDocument) -> None:
    """One ``unsupported`` entry per out-of-scope item — never silently dropped."""

    def _entry_id(entry: object) -> str:
        if isinstance(entry, Mapping):
            candidate = entry.get("id")
            if isinstance(candidate, str) and candidate:
                return candidate
        return ""

    for key, entries in parsed.trades.items():
        for i, entry in enumerate(entries):
            out.unsupported.append(
                UnsupportedItem(
                    section=key,
                    source_id=_entry_id(entry),
                    path=f"{key}[{i}]",
                    reason="unsupported_in_v1: trades",
                )
            )
    for key in ("swap_indices", "coupon_pricers"):
        for i, entry in enumerate(_list_of_mappings(parsed.rates, key)):
            out.unsupported.append(
                UnsupportedItem(
                    section=key,
                    source_id=_entry_id(entry),
                    path=f"pricing.rates.{key}[{i}]",
                    reason=f"unsupported_in_v1: {key}",
                )
            )
    for key in _INFLATION_LEGACY_KEYS:
        for i, entry in enumerate(_list_of_mappings(parsed.inflation, key)):
            out.unsupported.append(
                UnsupportedItem(
                    section=key,
                    source_id=_entry_id(entry),
                    path=f"pricing.inflation.{key}[{i}]",
                    reason="unsupported_in_v1: inflation",
                )
            )
    for key in _EQUITY_LEGACY_KEYS:
        for i, entry in enumerate(_list_of_mappings(parsed.equity, key)):
            out.unsupported.append(
                UnsupportedItem(
                    section=key,
                    source_id=_entry_id(entry),
                    path=f"pricing.equity.{key}[{i}]",
                    reason="unsupported_in_v1: equity",
                )
            )


# ---------------------------------------------------------------------------
# Top-level mapping
# ---------------------------------------------------------------------------


def _map_section(
    entries: list[Mapping[str, Any]],
    entity_type: str,
    path_template: str,
    mapper: Any,  # noqa: ANN401 -- per-entity closure, signatures differ
    out: MappedDocument,
) -> None:
    """Run one per-entity mapper over a section with per-item error isolation."""

    for i, raw in enumerate(entries):
        path = path_template.format(i=i)
        raw_id = raw.get("id")
        source_id = raw_id if isinstance(raw_id, str) else ""
        try:
            out.items.append(mapper(raw, path))
        except _ItemMappingError as exc:
            out.errors.append(
                ItemError(
                    entity_type=entity_type, source_id=source_id, path=path, reason=exc.reason
                )
            )
        except (TranslationError, ValidationError, ValueError, KeyError, TypeError) as exc:
            # The translators raise actionable messages — surface them
            # verbatim as the per-item reason (that is the point of the
            # round-trip validation).
            out.errors.append(
                ItemError(entity_type=entity_type, source_id=source_id, path=path, reason=str(exc))
            )


def map_document(
    document: Mapping[str, Any],
    *,
    default_reference_date: str | None = None,
) -> MappedDocument:
    """Map one engine-format document → creatable entities + full report.

    Raises :class:`EmptyDocumentError` when the document carries nothing
    recognizable (no entity sections, no trades). Per-item failures never
    raise — they land in ``errors`` so one bad item cannot abort the rest.
    """

    parsed = _parse_document(document)
    default_ref = default_reference_date or date.today().isoformat()
    quote_map = _build_quote_map(parsed.quotes)

    indices = _list_of_mappings(parsed.rates, "indices")
    curves = _list_of_mappings(parsed.rates, "curves")
    credit_curves = _list_of_mappings(parsed.credit, "credit_curves")
    vol_surfaces = _list_of_mappings(parsed.volatility, "vol_surfaces")
    models = _list_of_mappings(parsed.volatility, "models")

    out = MappedDocument()
    _report_unsupported(parsed, out)

    if not any([indices, curves, credit_curves, vol_surfaces, models, out.unsupported]):
        msg = (
            "Document contains no recognizable engine-format content "
            "(expected a price request, a ``pricing`` object, or a fragment "
            "with ``curves`` / ``indices`` / ``credit_curves`` / "
            "``vol_surfaces`` / ``models``)."
        )
        raise EmptyDocumentError(msg)

    _map_section(indices, ENTITY_INDEX, "pricing.rates.indices[{i}]", _map_index, out)

    # Curves validate their helper index refs against the document's own
    # (successfully mapped) indices + the known catalogs + the default
    # forwarding index — mirrors the pricing-path registration order.
    document_index_ids = frozenset(
        item.values["name"] for item in out.items if item.entity_type == ENTITY_INDEX
    )
    allowed_index_ids = frozenset(
        document_index_ids
        | set(_KNOWN_OVERNIGHT_INDICES)
        | set(_KNOWN_IBOR_INDICES)
        | {DEFAULT_FORWARDING_INDEX_ID}
    )
    _map_section(
        curves,
        ENTITY_CURVE,
        "pricing.rates.curves[{i}]",
        lambda raw, path: _map_curve(
            raw, path, quote_map, allowed_index_ids, default_ref, out.warnings
        ),
        out,
    )
    _map_section(
        credit_curves,
        ENTITY_CREDIT_CURVE,
        "pricing.credit.credit_curves[{i}]",
        lambda raw, path: _map_credit_curve(raw, path, quote_map, default_ref),
        out,
    )
    _map_section(
        vol_surfaces,
        ENTITY_VOL_SURFACE,
        "pricing.volatility.vol_surfaces[{i}]",
        lambda raw, path: _map_vol_surface(raw, path, quote_map, default_ref, out.warnings),
        out,
    )
    _map_section(models, ENTITY_SWAPTION_MODEL, "pricing.volatility.models[{i}]", _map_model, out)

    return out


__all__ = [
    "ENTITY_CREDIT_CURVE",
    "ENTITY_CURVE",
    "ENTITY_INDEX",
    "ENTITY_SWAPTION_MODEL",
    "ENTITY_VOL_SURFACE",
    "EmptyDocumentError",
    "ItemError",
    "ItemWarning",
    "MappedDocument",
    "MappedItem",
    "UnsupportedItem",
    "map_document",
]
