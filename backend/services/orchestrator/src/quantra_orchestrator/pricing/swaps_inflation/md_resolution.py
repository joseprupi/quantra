"""Product-local MD-resolution walker for inflation swaps.

First per-product walker that coordinates two curves in a single
pass. Per the no-MD-fallback rule (inflation curves are
**rates-shaped** and DO flow through the MD walker — they are not
credit-shaped), every canonical-id leaf in BOTH the nominal
discount curve and the inflation curve is collected into one set
and resolved via a **single** batched
``MdClient.resolve_quotes(...)`` call. Two sequential resolve
calls would defeat cache amortization and split correlated logs;
the batched call is the contract pinned by the test suite.

The inflation **index** body is consumed verbatim by the engine —
its ``fixings`` are historical CPI values, not vendor quotes — so
the index does **not** flow through this walker (distinction:
inflation **curves** are MD-resolved, but the inflation **index**
spec is not). Extending MD coverage to historical CPI fixings
would be an explicit future scope expansion; this walker stays
curve-only.

Failure semantics (mirror swap_ir / equity_options):

* :class:`MdClientError` raised by the MD client maps via the
  per-MD-error MD error envelope (``quote_not_found`` /
  ``md_unreachable`` / ``md_upstream_error``) — bubbled directly.
* Per-item ``found=False`` markers from
  :meth:`MdClient.resolve_quotes` are collected into a single
  ``swap_inflation_quote_resolution_failed`` (422) with every
  missing canonical ID listed in ``details``.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any

import structlog

from quantra_common.md_client import MdClient, MdClientError
from quantra_orchestrator.md import map_md_client_error
from quantra_orchestrator.pricing.swaps_inflation.assembler import (
    ResolvedSnapshot,
)
from quantra_orchestrator.pricing.swaps_inflation.errors import (
    SwapInflationQuoteResolutionFailedError,
)
from quantra_orchestrator.pricing.swaps_inflation.models import (
    ResolvedCurve,
    ResolvedQuoteValue,
)

_log = structlog.get_logger(__name__)

# Top-level keys inside one curve-point object that carry a quote ID.
# Mirrors the swap_ir walker — both flat ``{"quote_id": "..."}`` and
# discriminated-helper ``{"point_type": "...", "point": {"quote_id": "..."}}``
# shapes are accepted.
_POINT_QUOTE_KEYS: tuple[str, ...] = ("quote_id", "quoteId")

# How many missing canonical IDs to surface in the ``error``
# string when MD blows up. Same constant as the swap_ir walker.
_BREADCRUMB_ID_LIMIT: int = 5


def collect_quote_ids(curves: Iterable[ResolvedCurve]) -> list[str]:
    """Return every distinct quote ID across both curves.

    Order-preserving so the resolved-quotes echo in the response
    body is stable. Per the single-walker contract the same
    iterator passes every leaf from both curves through the same
    de-dup pass — a quote that appears on both curves is resolved
    once and echoed once.
    """

    seen: dict[str, None] = {}
    for curve in curves:
        for point in curve.points:
            quote_id = _extract_point_quote_id(point)
            if quote_id is not None and quote_id not in seen:
                seen[quote_id] = None
    return list(seen.keys())


async def resolve(
    *,
    nominal_curve: ResolvedCurve,
    inflation_curve: ResolvedCurve,
    as_of: date,
    snapshot: ResolvedSnapshot | None,
    md_client: MdClient,
    snapshot_version: str | None = None,
) -> list[ResolvedQuoteValue]:
    """Resolve every quote ID across BOTH curves to a value in ONE batched call.

    Hard constraint from the plan: emit a single batched call to
    ``MdClient.resolve_quotes`` covering both curves' canonical
    ids, not two sequential calls. The walker collects all
    canonical ids first, applies any snapshot pin, then issues
    the single batched MD round-trip for the misses.
    """

    quote_ids = collect_quote_ids([nominal_curve, inflation_curve])
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
                "orchestrator.pricing.swaps_inflation.md_resolution_error",
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
            raise SwapInflationQuoteResolutionFailedError(
                missing_canonical_ids=missing_after_live,
                as_of=as_of.isoformat(),
            )

    by_cid = {p.canonical_id: p for p in pinned}
    by_cid.update({r.canonical_id: r for r in live_resolved})
    return [by_cid[cid] for cid in quote_ids if cid in by_cid]


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
