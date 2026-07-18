"""Product-local MD-resolution walker for swaptions.

Extends the :mod:`pricing.swap_ir.md_resolution` shape to traverse
vol-surface bodies in addition to curve point lists. Both kinds of
JSONB carry ``{"quote_id": "..."}`` or ``{"quoteId": "..."}`` leaves
(/ portal's ``MatrixCell`` shape); the walker collects every
distinct ID across both, substitutes pinned values via the request's
optional :class:`ResolvedSnapshot`, and falls through to the MD client
for the rest.

This module is **product-local on purpose** — a shared walker is
deliberately deferred: "do not prematurely factor a shared
walker — wait until at least three products land (you'll have the
data after ``08_bonds.md``)."

Vol-surface body shapes the walker recognises (any combination may
appear; unknown leaves are silently ignored so the walker
under-collects rather than over-collects):

* ``base.quote_id`` / ``base.quoteId`` — single optional ID.
* ``spot_quote_id`` / ``spotQuoteId`` — top-level ID for equity surfaces.
* ``grid: MatrixCell[][]`` — cells may be ``{"quoteId": "..."}`` or
  ``{"quote_id": "..."}``.
* ``sabr_alpha`` / ``sabr_beta`` / ``sabr_rho`` / ``sabr_nu`` —
  ``MatrixCell[][]``, same cell shape.
* ``sabr_market_vols`` — ``MatrixCell[][][]``, same cell shape.
* ``surface_price_quote_ids: string[]`` — row-major ID list for
  ``BlackVolSpec`` price-grid surfaces.
* ``quote_ids: string[][]`` — explicit ID grid (legacy
  ``SwaptionVolSpec`` snapshots).
* ``series: VolSurfaceSnapshot[]`` — recursed into per-entry
  (``grid``, ``base.quote_id``).

Failure semantics:

* Any ``MdClientError`` raised by the MD client is mapped via the
  per-MD-error MD error envelope (``quote_not_found`` / ``md_unreachable``
  / ``md_upstream_error``) — bubbled directly. No product wrapping;
  the MD layer's tokens are already stable enough for clients.
* Per-item ``found=False`` markers from ``MdClient.resolve_quotes``
  are collected and rolled into a single
  ``swaption_quote_resolution_failed`` (422) with every missing
  canonical ID listed in ``details`` (one error, complete miss list).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any

import structlog

from quantra_common.md_client import MdClient, MdClientError
from quantra_orchestrator.md import map_md_client_error
from quantra_orchestrator.pricing.swaption.assembler import ResolvedSnapshot
from quantra_orchestrator.pricing.swaption.errors import (
    SwaptionQuoteResolutionFailedError,
)
from quantra_orchestrator.pricing.swaption.models import (
    ResolvedCurve,
    ResolvedQuoteValue,
    ResolvedVolSurface,
)

_log = structlog.get_logger(__name__)

_POINT_QUOTE_KEYS: tuple[str, ...] = ("quote_id", "quoteId")
_SPOT_QUOTE_KEYS: tuple[str, ...] = ("spot_quote_id", "spotQuoteId")

# Vol-surface payload keys that carry MatrixCell grids of varying rank.
_SURFACE_MATRIX2D_KEYS: tuple[str, ...] = (
    "grid",
    "sabr_alpha",
    "sabr_beta",
    "sabr_rho",
    "sabr_nu",
)
_SURFACE_MATRIX3D_KEYS: tuple[str, ...] = ("sabr_market_vols",)
_SURFACE_FLAT_ID_LIST_KEYS: tuple[str, ...] = ("surface_price_quote_ids",)
_SURFACE_NESTED_ID_LIST_KEYS: tuple[str, ...] = ("quote_ids",)

# How many missing canonical IDs to surface in the ``error`` string
# when MD blows up. The full miss list still lands in ``details``; the
# breadcrumb just keeps a 1k-quote batch from rendering a multi-KB top-
# level error message. Same constant as the swap_ir walker.
_BREADCRUMB_ID_LIMIT: int = 5


def collect_quote_ids(
    curves: Iterable[ResolvedCurve],
    vol_surface: ResolvedVolSurface | None,
) -> list[str]:
    """Return every distinct quote ID referenced across curves + vol surface.

    Order-preserving so the resolved-quotes echo in the response body
    is stable (helps debugging via diff'ing two runs). Curve IDs come
    first (matches the swap_ir-side convention), surface IDs second.
    """

    seen: dict[str, None] = {}
    for curve in curves:
        for point in curve.points:
            quote_id = _extract_point_quote_id(point)
            if quote_id is not None and quote_id not in seen:
                seen[quote_id] = None
    if vol_surface is not None:
        for quote_id in _walk_vol_surface_payload(vol_surface.payload):
            if quote_id not in seen:
                seen[quote_id] = None
    return list(seen.keys())


async def resolve(
    *,
    curves: Iterable[ResolvedCurve],
    vol_surface: ResolvedVolSurface | None,
    as_of: date,
    snapshot: ResolvedSnapshot | None,
    md_client: MdClient,
    snapshot_version: str | None = None,
) -> list[ResolvedQuoteValue]:
    """Resolve every quote ID in ``curves`` + ``vol_surface`` into a value.

    Snapshot-pinned IDs short-circuit the MD round-trip; the
    remainder go through ``md_client.resolve_quotes(...)`` in a
    single batched call. ``found=False`` markers from MD are
    collected into a single 422.
    """

    quote_ids = collect_quote_ids(curves, vol_surface)
    if not quote_ids:
        return []

    pinned: list[ResolvedQuoteValue] = []
    misses: list[str] = []
    if snapshot is not None:
        for cid in quote_ids:
            pin = snapshot.pins.get(cid)
            if pin is not None:
                pinned.append(
                    ResolvedQuoteValue(
                        canonical_id=cid,
                        as_of=as_of,
                        value=float(pin["value"]),
                        source=_optional_str(pin.get("source")) or snapshot.name,
                        from_snapshot=True,
                    )
                )
                continue
            misses.append(cid)
    else:
        misses = list(quote_ids)

    live_resolved: list[ResolvedQuoteValue] = []
    if misses:
        try:
            md_results = await md_client.resolve_quotes(
                misses, as_of, snapshot_version=snapshot_version
            )
        except MdClientError as exc:
            _log.warning(
                "orchestrator.pricing.swaption.md_resolution_error",
                exc_type=type(exc).__name__,
                missing_count=len(misses),
            )
            head = misses[:_BREADCRUMB_ID_LIMIT]
            suffix = "..." if len(misses) > _BREADCRUMB_ID_LIMIT else ""
            raise map_md_client_error(
                exc,
                canonical_id=",".join(head) + suffix,
                as_of=as_of.isoformat(),
            ) from exc

        missing_after_live: list[str] = []
        for result in md_results:
            if not result.found or result.value is None:
                missing_after_live.append(result.canonical_id)
                continue
            resolved_dt = result.resolved_as_of or result.requested_as_of
            live_resolved.append(
                ResolvedQuoteValue(
                    canonical_id=result.canonical_id,
                    as_of=resolved_dt.date() if resolved_dt is not None else as_of,
                    value=float(result.value),
                    source=result.source or result.vendor_id,
                    from_snapshot=False,
                )
            )
        if missing_after_live:
            raise SwaptionQuoteResolutionFailedError(
                missing_canonical_ids=missing_after_live,
                as_of=as_of.isoformat(),
            )

    by_cid = {p.canonical_id: p for p in pinned}
    by_cid.update({r.canonical_id: r for r in live_resolved})
    return [by_cid[cid] for cid in quote_ids if cid in by_cid]


# ---------------------------------------------------------------------------
# Vol-surface walker
# ---------------------------------------------------------------------------


def _walk_vol_surface_payload(payload: dict[str, Any]) -> list[str]:
    """Walk a vol-surface payload and yield every quote ID it references.

    Order matters for the deduped echo, so we collect into a list and
    let :func:`collect_quote_ids` dedupe across the union of curve +
    surface IDs. The walker delegates per-shape collection to small
    helpers so the top-level dispatch stays linear in the number of
    recognised shapes (one helper per shape).
    """

    out: list[str] = []
    out.extend(_walk_payload_scalars(payload))
    out.extend(_walk_payload_grids(payload))
    out.extend(_walk_payload_id_lists(payload))
    out.extend(_walk_payload_series(payload))
    return out


def _walk_payload_scalars(payload: dict[str, Any]) -> list[str]:
    """Collect top-level scalar IDs: ``base.quote_id`` + ``spot_quote_id``."""

    out: list[str] = []
    base = payload.get("base")
    if isinstance(base, dict):
        base_id = _first_str(base, _POINT_QUOTE_KEYS)
        if base_id is not None:
            out.append(base_id)
    for key in _SPOT_QUOTE_KEYS:
        spot_id = payload.get(key)
        if isinstance(spot_id, str) and spot_id:
            out.append(spot_id)
    return out


def _walk_payload_grids(payload: dict[str, Any]) -> list[str]:
    """Collect IDs from 2D and 3D ``MatrixCell``-shaped grids."""

    out: list[str] = []
    for key in _SURFACE_MATRIX2D_KEYS:
        grid = payload.get(key)
        if isinstance(grid, list):
            out.extend(_walk_matrix_2d(grid))
    for key in _SURFACE_MATRIX3D_KEYS:
        cube = payload.get(key)
        if isinstance(cube, list):
            out.extend(_walk_matrix_3d(cube))
    return out


def _walk_payload_id_lists(payload: dict[str, Any]) -> list[str]:
    """Collect IDs from flat (``str[]``) and nested (``str[][]``) ID arrays."""

    out: list[str] = []
    for key in _SURFACE_FLAT_ID_LIST_KEYS:
        flat = payload.get(key)
        if isinstance(flat, list):
            out.extend(item for item in flat if isinstance(item, str) and item)
    for key in _SURFACE_NESTED_ID_LIST_KEYS:
        nested = payload.get(key)
        if isinstance(nested, list):
            for row in nested:
                if isinstance(row, list):
                    out.extend(item for item in row if isinstance(item, str) and item)
    return out


def _walk_payload_series(payload: dict[str, Any]) -> list[str]:
    """Recurse into per-date ``series`` snapshots (legacy + current portal shape)."""

    out: list[str] = []
    series = payload.get("series")
    if not isinstance(series, list):
        return out
    for entry in series:
        if not isinstance(entry, dict):
            continue
        entry_base = entry.get("base")
        if isinstance(entry_base, dict):
            base_id = _first_str(entry_base, _POINT_QUOTE_KEYS)
            if base_id is not None:
                out.append(base_id)
        entry_grid = entry.get("grid")
        if isinstance(entry_grid, list):
            out.extend(_walk_matrix_2d(entry_grid))
    return out


def _walk_matrix_2d(grid: list[Any]) -> list[str]:
    out: list[str] = []
    for row in grid:
        if not isinstance(row, list):
            continue
        for cell in row:
            cid = _matrix_cell_quote_id(cell)
            if cid is not None:
                out.append(cid)
    return out


def _walk_matrix_3d(cube: list[Any]) -> list[str]:
    out: list[str] = []
    for plane in cube:
        if isinstance(plane, list):
            out.extend(_walk_matrix_2d(plane))
    return out


def _matrix_cell_quote_id(cell: Any) -> str | None:  # noqa: ANN401 -- runtime branch
    """Pull a quote ID out of a ``MatrixCell``-shaped value, if any.

    Cells are either a literal number (no quote) or an object
    ``{"quoteId": "..."}`` / ``{"quote_id": "..."}``. Anything else
    is treated as a non-quote cell.
    """

    if not isinstance(cell, dict):
        return None
    return _first_str(cell, _POINT_QUOTE_KEYS)


# ---------------------------------------------------------------------------
# Curve walker (same shape as swap_ir; duplicated per the "Key
# product-specific additions")
# ---------------------------------------------------------------------------


def _extract_point_quote_id(point: dict[str, Any]) -> str | None:
    """Return the quote ID from a curve point (helper-wrapped or flat)."""

    nested = point.get("point")
    if isinstance(nested, dict):
        candidate = _first_str(nested, _POINT_QUOTE_KEYS)
        if candidate is not None:
            return candidate
    return _first_str(point, _POINT_QUOTE_KEYS)


def _first_str(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _optional_str(value: Any) -> str | None:  # noqa: ANN401 -- runtime branch
    return value if isinstance(value, str) and value else None


__all__ = [
    "collect_quote_ids",
    "resolve",
]
