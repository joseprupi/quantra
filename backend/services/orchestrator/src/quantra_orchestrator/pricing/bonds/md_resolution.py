"""Product-local MD-resolution walker for bonds (fixed + floating).

Walks the resolved curves, collects every ``quote_id`` field, and
substitutes the resolved value via the snapshot pin (when present) or
the MD client (otherwise). The output is the
``list[ResolvedQuoteValue]`` echoed back in the response envelope
and that the engine-side encoder uses to build the FlatBuffers
payload — quote IDs never leave the orchestrator (north-star
invariant #8).

This module is **product-local on purpose** — a shared walker is
deliberately deferred. The MD-resolution walker is structurally
identical to ``swap_ir``'s for the curve traversal (no new JSONB
shape); factoring a shared walker is a candidate future refactor.

This module differs from the swap_ir-side walker in exactly one
place: it accepts a list of curves (already de-duplicated by the
assembler — see :func:`assembler.collect_curves`) rather than a
single iterable, so the floating-rate route can pass the discount +
projection curve pair without the walker needing to know which is
which. The point-walking, snapshot lookup, MD batch, and error-
surface contract are identical.

Failure semantics:

* Any ``MdClientError`` raised by the MD client is mapped via the
  per-MD-error MD error envelope (``quote_not_found`` / ``md_unreachable``
  / ``md_upstream_error``) — bubbled directly. No product wrapping;
  the MD layer's tokens are already stable enough for clients.
* Per-item ``found=False`` markers from ``MdClient.resolve_quotes``
  are collected and rolled into a single
  ``bond_quote_resolution_failed`` (422) with every missing
  canonical ID listed in ``details``.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any

import structlog

from quantra_common.md_client import MdClient, MdClientError
from quantra_orchestrator.md import map_md_client_error
from quantra_orchestrator.pricing.bonds.assembler import ResolvedSnapshot
from quantra_orchestrator.pricing.bonds.errors import (
    BondQuoteResolutionFailedError,
)
from quantra_orchestrator.pricing.bonds.models import (
    ResolvedCurve,
    ResolvedQuoteValue,
)

_log = structlog.get_logger(__name__)

_POINT_QUOTE_KEYS: tuple[str, ...] = ("quote_id", "quoteId")

# How many missing canonical IDs to surface in the ``error``
# string when MD blows up. The full miss list still lands in
# ``details``; the breadcrumb just keeps a 1k-quote batch from
# rendering a multi-KB top-level error message. Same constant as
# the swap_ir / swaption walkers.
_BREADCRUMB_ID_LIMIT: int = 5


def collect_quote_ids(curves: Iterable[ResolvedCurve]) -> list[str]:
    """Return every distinct quote ID referenced across ``curves``.

    Order-preserving so the resolved-quotes echo in the response
    body is stable (helps debugging via diff'ing two runs).
    """

    seen: dict[str, None] = {}
    for curve in curves:
        for point in curve.points:
            quote_id = _extract_quote_id(point)
            if quote_id is not None and quote_id not in seen:
                seen[quote_id] = None
    return list(seen.keys())


async def resolve(
    *,
    curves: Iterable[ResolvedCurve],
    as_of: date,
    snapshot: ResolvedSnapshot | None,
    md_client: MdClient,
    snapshot_version: str | None = None,
) -> list[ResolvedQuoteValue]:
    """Resolve every quote ID in ``curves`` into a :class:`ResolvedQuoteValue`.

    Snapshot-pinned IDs short-circuit the MD round-trip; the
    remainder go through ``md_client.resolve_quotes(...)`` in a
    single batched call. ``found=False`` markers from MD are
    collected into a single 422.
    """

    quote_ids = collect_quote_ids(curves)
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
                "orchestrator.pricing.bonds.md_resolution_error",
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
            raise BondQuoteResolutionFailedError(
                missing_canonical_ids=missing_after_live,
                as_of=as_of.isoformat(),
            )

    by_cid = {p.canonical_id: p for p in pinned}
    by_cid.update({r.canonical_id: r for r in live_resolved})
    return [by_cid[cid] for cid in quote_ids if cid in by_cid]


def _extract_quote_id(point: dict[str, Any]) -> str | None:
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
