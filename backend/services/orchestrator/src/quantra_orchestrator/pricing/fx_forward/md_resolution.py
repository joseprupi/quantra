"""Product-local MD-resolution walker for FX forwards.

Collects every ``quote_id`` across the resolved curves (same walk as swap_ir:
helper-wrapped or flat points) AND the resolved FX spots (``quote_id`` on a
spot carried through ``pricing.quotes`` engine-side; here it is resolved to
its value so the engine never sees the id — invariant #8). Snapshot pins
first, live MD second; per-item misses roll into one
``fx_forward_quote_resolution_failed`` (422).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

import structlog

from quantra_common.md_client import MdClient, MdClientError
from quantra_orchestrator.md import map_md_client_error
from quantra_orchestrator.pricing.fx_forward.errors import (
    FxForwardQuoteResolutionFailedError,
)
from quantra_orchestrator.pricing.fx_forward.models import (
    ResolvedCurve,
    ResolvedFxSpot,
    ResolvedQuoteValue,
)
from quantra_orchestrator.pricing.swap_ir.assembler import ResolvedSnapshot
from quantra_orchestrator.pricing.swap_ir.md_resolution import (
    collect_quote_ids as _collect_curve_quote_ids,
)

_log = structlog.get_logger(__name__)
_BREADCRUMB_ID_LIMIT: int = 5


def collect_quote_ids(
    curves: Iterable[ResolvedCurve], fx_spots: Iterable[ResolvedFxSpot] = ()
) -> list[str]:
    """Every distinct quote id across the curves' points and the FX spots (stable order)."""

    seen: dict[str, None] = dict.fromkeys(_collect_curve_quote_ids(curves))
    for spot in fx_spots:
        if isinstance(spot.quote_id, str) and spot.quote_id and spot.quote_id not in seen:
            seen[spot.quote_id] = None
    return list(seen)


async def resolve(
    *,
    curves: Iterable[ResolvedCurve],
    fx_spots: Iterable[ResolvedFxSpot],
    as_of: date,
    snapshot: ResolvedSnapshot | None,
    md_client: MdClient,
    snapshot_version: str | None = None,
) -> list[ResolvedQuoteValue]:
    quote_ids = collect_quote_ids(curves, fx_spots)
    if not quote_ids:
        return []

    pinned: list[ResolvedQuoteValue] = []
    misses: list[str] = []
    if snapshot is not None:
        for cid in quote_ids:
            pin = snapshot.pins.get(cid)
            if pin is not None:
                source = pin.get("source")
                pinned.append(
                    ResolvedQuoteValue(
                        canonical_id=cid,
                        as_of=as_of,
                        value=float(pin["value"]),
                        source=source if isinstance(source, str) and source else snapshot.name,
                        from_snapshot=True,
                    )
                )
                continue
            misses.append(cid)
    else:
        misses = list(quote_ids)

    live: list[ResolvedQuoteValue] = []
    if misses:
        try:
            md_results = await md_client.resolve_quotes(
                misses, as_of, snapshot_version=snapshot_version
            )
        except MdClientError as exc:
            _log.warning(
                "orchestrator.pricing.fx_forward.md_resolution_error",
                exc_type=type(exc).__name__,
                missing_count=len(misses),
            )
            head = misses[:_BREADCRUMB_ID_LIMIT]
            suffix = "..." if len(misses) > _BREADCRUMB_ID_LIMIT else ""
            raise map_md_client_error(
                exc, canonical_id=",".join(head) + suffix, as_of=as_of.isoformat()
            ) from exc
        still_missing: list[str] = []
        for result in md_results:
            if not result.found or result.value is None:
                still_missing.append(result.canonical_id)
                continue
            resolved_dt = result.resolved_as_of or result.requested_as_of
            live.append(
                ResolvedQuoteValue(
                    canonical_id=result.canonical_id,
                    as_of=resolved_dt.date() if resolved_dt is not None else as_of,
                    value=float(result.value),
                    source=result.source or result.vendor_id,
                    from_snapshot=False,
                )
            )
        if still_missing:
            raise FxForwardQuoteResolutionFailedError(
                missing_canonical_ids=still_missing, as_of=as_of.isoformat()
            )

    by_cid = {p.canonical_id: p for p in pinned}
    by_cid.update({r.canonical_id: r for r in live})
    return [by_cid[cid] for cid in quote_ids if cid in by_cid]


__all__ = ["collect_quote_ids", "resolve"]
