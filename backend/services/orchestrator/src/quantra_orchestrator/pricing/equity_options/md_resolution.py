"""Product-local MD-resolution walker for equity options.

Mirrors the swaption-side walker shape — same curve / vol-surface
sub-walkers — but adds a third leaf: the underlier spot quote
(:class:`ResolvedSpotQuote`). The equity-options endpoint
does **not** bypass MD: every leaf with a canonical id flows
through ``MdClient.resolve_quotes`` (or the snapshot pin) the
same way curves and vol surfaces do; the spot is just a single
extra canonical id alongside the curve / surface ones.

Vol-surface body shapes the walker recognises (any combination
may appear; unknown leaves are silently ignored so the walker
under-collects rather than over-collects):

* ``base.quote_id`` / ``base.quoteId`` — single optional ID
  (constant-vol body when the vol value is on the wire as a
  quote rather than an inline ``constant_vol``).
* ``spot_quote_id`` / ``spotQuoteId`` — top-level ID for the
  surface's own implied-vol inversion path. We resolve it the
  same way as the underlier spot above so the engine sees a
  consistent value across both touch-points.
* ``term_vols`` / ``surface_vols`` / ``surface_prices`` —
  :class:`QuoteMatrix2D`-shaped grids whose cells may be
  ``{"quoteId": "..."}`` or ``{"quote_id": "..."}``.
* ``surface_price_quote_ids`` — flat (``str[]``) ID array.
* ``quote_ids`` — nested (``str[][]``) ID grid.

Failure semantics:

* :class:`MdClientError` raised by the MD client maps via the
  per-MD-error MD error envelope (``quote_not_found`` /
  ``md_unreachable`` / ``md_upstream_error``) — bubbled directly.
* Per-item ``found=False`` markers from
  :meth:`MdClient.resolve_quotes` are collected into a single
  ``equity_option_quote_resolution_failed`` (422) with every
  missing canonical ID listed in ``details`` (one error,
  complete miss list).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any

import structlog

from quantra_common.md_client import MdClient, MdClientError
from quantra_orchestrator.md import map_md_client_error
from quantra_orchestrator.pricing.equity_options.assembler import (
    ResolvedSnapshot,
)
from quantra_orchestrator.pricing.equity_options.errors import (
    EquityOptionQuoteResolutionFailedError,
)
from quantra_orchestrator.pricing.equity_options.models import (
    ResolvedCurve,
    ResolvedQuoteValue,
    ResolvedSpotQuote,
    ResolvedVolSurface,
)

_log = structlog.get_logger(__name__)

_POINT_QUOTE_KEYS: tuple[str, ...] = ("quote_id", "quoteId")
_SPOT_QUOTE_KEYS: tuple[str, ...] = ("spot_quote_id", "spotQuoteId")

# Vol-surface payload keys that carry MatrixCell grids of varying rank.
_SURFACE_MATRIX2D_KEYS: tuple[str, ...] = (
    "grid",
    "term_vols",
    "surface_vols",
    "surface_prices",
)
_SURFACE_FLAT_ID_LIST_KEYS: tuple[str, ...] = ("surface_price_quote_ids",)
_SURFACE_NESTED_ID_LIST_KEYS: tuple[str, ...] = ("quote_ids",)

# How many missing canonical IDs to surface in the ``error``
# string when MD blows up. Same constant as the swaption walker.
_BREADCRUMB_ID_LIMIT: int = 5


def collect_quote_ids(
    curves: Iterable[ResolvedCurve],
    vol_surface: ResolvedVolSurface | None,
    spot: ResolvedSpotQuote | None,
) -> list[str]:
    """Return every distinct quote ID across curves, vol surface, and spot.

    Order-preserving so the resolved-quotes echo in the response
    body is stable. Curve IDs come first, surface IDs second, the
    spot id last (same convention as the swaption walker, with
    spot tacked on the end).
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
    if (
        spot is not None
        and spot.canonical_id
        and spot.value is None
        and spot.canonical_id not in seen
    ):
        seen[spot.canonical_id] = None
    return list(seen.keys())


async def resolve(
    *,
    curves: Iterable[ResolvedCurve],
    vol_surface: ResolvedVolSurface | None,
    spot: ResolvedSpotQuote | None,
    as_of: date,
    snapshot: ResolvedSnapshot | None,
    md_client: MdClient,
    snapshot_version: str | None = None,
) -> list[ResolvedQuoteValue]:
    """Resolve every quote ID in curves + vol surface + spot to a value."""

    quote_ids = collect_quote_ids(curves, vol_surface, spot)
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
                "orchestrator.pricing.equity_options.md_resolution_error",
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
            raise EquityOptionQuoteResolutionFailedError(
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
    """Walk a vol-surface payload and yield every quote ID it references."""

    out: list[str] = []
    out.extend(_walk_payload_scalars(payload))
    out.extend(_walk_payload_grids(payload))
    out.extend(_walk_payload_id_lists(payload))
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
    """Collect IDs from 2D ``QuoteMatrix2D``-shaped grids."""

    out: list[str] = []
    for key in _SURFACE_MATRIX2D_KEYS:
        grid = payload.get(key)
        if isinstance(grid, list):
            out.extend(_walk_matrix_2d(grid))
        elif isinstance(grid, dict):
            cells = grid.get("cells")
            if isinstance(cells, list):
                out.extend(_walk_matrix_2d(cells))
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


def _matrix_cell_quote_id(cell: Any) -> str | None:  # noqa: ANN401 -- runtime branch
    """Pull a quote ID out of a ``MatrixCell``-shaped value, if any.

    Cells are either a literal number (no quote) or an object
    ``{"quoteId": "..."}`` / ``{"quote_id": "..."}``. Anything
    else is treated as a non-quote cell.
    """

    if not isinstance(cell, dict):
        return None
    return _first_str(cell, _POINT_QUOTE_KEYS)


# ---------------------------------------------------------------------------
# Curve walker (shape-equivalent to swap_ir / swaption)
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
