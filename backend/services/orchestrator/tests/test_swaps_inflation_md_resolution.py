"""Unit tests for ``pricing/swaps_inflation/md_resolution.py``.

The walker has four responsibilities:

* Collect every distinct ``quote_id`` from BOTH the nominal and
  the inflation curves (helper-wrapped *and* flat shapes, no
  duplicates, input-order preserved).
* Issue a SINGLE batched ``MdClient.resolve_quotes`` call covering
  every canonical id from both curves (correlated logs + cache
  amortization — the plan's hard constraint, **not** two
  sequential resolve calls).
* Substitute snapshot pins before falling through to live MD.
* Surface per-MD-error envelopes verbatim
  (``quote_not_found`` / ``md_unreachable`` / ``md_upstream_error``)
  and collapse per-item ``found=False`` into one
  ``swap_inflation_quote_resolution_failed`` (422).

**the no-MD-fallback rule bypass-check pin (the heart of this file).** Inflation
curves are rates-shaped (discount-curve helpers / ZCIIS /
YYIIS swap helpers carry ``quote_id`` leaves the same way the
nominal curve's deposit / swap helpers do). They are **NOT**
bypassed. Every canonical id leaf in either curve flows through
the MD walker. We pin this with explicit assertions that:

(a) inflation-curve quote ids appear in the
    :meth:`MdClient.resolve_quotes` call,
(b) the nominal-curve quote ids appear in the same call (one
    batched MD round-trip, not two), and
(c) the order is stable (nominal first, inflation second).

If a future plan finds a bundle type that should bypass the MD
walker, that's a new D# entry — not a silent omission here.

The inflation **index** body (CPI fixings) is consumed verbatim
by the engine and does NOT flow through the walker. That's the
distinction the assembler enforces; this walker only sees the two
curves.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest

from quantra_common.md_client import (
    MdHttpStatusError,
    MdNotFoundError,
    MdTransportError,
)
from quantra_common.types.market_data import ResolvedQuote
from quantra_orchestrator.md.errors import (
    MdUnreachableError,
    MdUpstreamError,
    QuoteNotFoundError,
)
from quantra_orchestrator.pricing.swaps_inflation.assembler import (
    ResolvedSnapshot,
)
from quantra_orchestrator.pricing.swaps_inflation.errors import (
    SwapInflationQuoteResolutionFailedError,
)
from quantra_orchestrator.pricing.swaps_inflation.md_resolution import (
    collect_quote_ids,
    resolve,
)
from quantra_orchestrator.pricing.swaps_inflation.models import ResolvedCurve


class _FakeMdClient:
    """Minimum ``MdClient`` surface the walker exercises (``resolve_quotes`` only)."""

    def __init__(
        self,
        *,
        results: list[ResolvedQuote] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[list[str], date | datetime]] = []
        self._results = results or []
        self._raises = raises

    async def resolve_quotes(
        self,
        canonical_ids: list[str],
        as_of: date | datetime,
        *,
        snapshot_version: str | None = None,
    ) -> list[ResolvedQuote]:
        self.calls.append((list(canonical_ids), as_of))
        if self._raises is not None:
            raise self._raises
        return list(self._results)


def _curve(
    *,
    name: str,
    role: str,
    points: list[dict[str, Any]],
) -> ResolvedCurve:
    return ResolvedCurve(
        id=None,
        name=name,
        role=role,
        currency="USD",
        day_counter="Actual/365",
        helper_kind="Discount",
        reference_date=date(2025, 1, 15),
        points=points,
        body={"interpolator": "Cubic"},
    )


def _resolved_quote(
    *,
    canonical_id: str,
    value: float,
    found: bool = True,
    source: str | None = "vendor_x",
) -> ResolvedQuote:
    return ResolvedQuote(
        canonical_id=canonical_id,
        requested_as_of=datetime(2025, 1, 15, 0, 0),
        resolved_as_of=datetime(2025, 1, 15, 0, 0),
        found=found,
        is_exact=True,
        value=value if found else None,
        source=source,
        vendor_id="vendor",
    )


# ---------------------------------------------------------------------------
# collect_quote_ids
# ---------------------------------------------------------------------------


def test_collect_quote_ids_extracts_flat_and_nested_points() -> None:
    nominal = _curve(
        name="DISC",
        role="nominal",
        points=[
            {"tenor": "1Y", "quote_id": "USD.IRS.1Y"},
            {
                "point_type": "swap",
                "point": {"tenor": "5Y", "quote_id": "USD.IRS.5Y"},
            },
        ],
    )
    inflation = _curve(
        name="HICP_ZC",
        role="inflation",
        points=[
            {
                "point_type": "ZeroCouponInflationSwapHelper",
                "point": {"tenor": "1Y", "quote_id": "EUR.HICP.1Y"},
            },
            {"tenor": "5Y", "quote_id": "EUR.HICP.5Y"},
        ],
    )
    assert collect_quote_ids([nominal, inflation]) == [
        "USD.IRS.1Y",
        "USD.IRS.5Y",
        "EUR.HICP.1Y",
        "EUR.HICP.5Y",
    ]


def test_collect_quote_ids_dedupes_across_curves_preserving_order() -> None:
    nominal = _curve(
        name="DISC",
        role="nominal",
        points=[{"quote_id": "EUR.IRS.1Y"}, {"quote_id": "EUR.IRS.5Y"}],
    )
    inflation = _curve(
        name="HICP_ZC",
        role="inflation",
        points=[{"quote_id": "EUR.IRS.1Y"}, {"quote_id": "EUR.HICP.1Y"}],
    )
    assert collect_quote_ids([nominal, inflation]) == [
        "EUR.IRS.1Y",
        "EUR.IRS.5Y",
        "EUR.HICP.1Y",
    ]


def test_collect_quote_ids_handles_quoteid_camelcase() -> None:
    inflation = _curve(
        name="HICP",
        role="inflation",
        points=[{"quoteId": "EUR.HICP.1Y"}],
    )
    assert collect_quote_ids([inflation]) == ["EUR.HICP.1Y"]


def test_collect_quote_ids_empty_for_no_curves() -> None:
    assert collect_quote_ids([]) == []


# ---------------------------------------------------------------------------
# MD-bypass check — the heart of the file
# ---------------------------------------------------------------------------


async def test_inflation_curve_quote_ids_flow_through_md_walker() -> None:
    """the no-MD-fallback rule pin: inflation-curve canonical ids hit MdClient.resolve_quotes.

    Critical invariant. If a future refactor accidentally drops
    the inflation curve from the walker (so its leaves bypass MD
    silently), this test fails — operators get the regression
    signal before production. The orchestrator NEVER bypasses MD
    for a bundle type without a matching D# entry; the no-MD-fallback rule reserves
    that escape hatch for explicit decisions.
    """

    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="EUR.IRS.1Y", value=0.03),
            _resolved_quote(canonical_id="EUR.HICP.1Y", value=0.02),
        ]
    )
    nominal = _curve(
        name="DISC",
        role="nominal",
        points=[{"quote_id": "EUR.IRS.1Y"}],
    )
    inflation = _curve(
        name="HICP_ZC",
        role="inflation",
        points=[{"quote_id": "EUR.HICP.1Y"}],
    )

    out = await resolve(
        nominal_curve=nominal,
        inflation_curve=inflation,
        as_of=date(2025, 1, 15),
        snapshot=None,
        md_client=md,  # type: ignore[arg-type]
    )

    assert [q.canonical_id for q in out] == ["EUR.IRS.1Y", "EUR.HICP.1Y"]
    # The inflation-curve canonical id is in the call.
    assert "EUR.HICP.1Y" in md.calls[0][0]
    # Both curves' ids are in the SAME call (single batched walker).
    assert "EUR.IRS.1Y" in md.calls[0][0]


async def test_walker_emits_single_batched_md_call_for_both_curves() -> None:
    """The plan's hard constraint: ONE batched ``resolve_quotes`` call, not two.

    ``MdClient.resolve_quotes`` is invoked exactly once with the
    union of both curves' canonical ids in the (de-duplicated,
    nominal-first) input order. Two sequential calls would defeat
    the cache amortization + correlated-logs intent.
    """

    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="EUR.IRS.1Y", value=0.03),
            _resolved_quote(canonical_id="EUR.HICP.1Y", value=0.02),
            _resolved_quote(canonical_id="EUR.HICP.5Y", value=0.022),
        ]
    )
    nominal = _curve(
        name="DISC",
        role="nominal",
        points=[{"quote_id": "EUR.IRS.1Y"}],
    )
    inflation = _curve(
        name="HICP_ZC",
        role="inflation",
        points=[{"quote_id": "EUR.HICP.1Y"}, {"quote_id": "EUR.HICP.5Y"}],
    )

    out = await resolve(
        nominal_curve=nominal,
        inflation_curve=inflation,
        as_of=date(2025, 1, 15),
        snapshot=None,
        md_client=md,  # type: ignore[arg-type]
    )

    assert len(md.calls) == 1
    canonical_ids, as_of = md.calls[0]
    assert canonical_ids == ["EUR.IRS.1Y", "EUR.HICP.1Y", "EUR.HICP.5Y"]
    assert as_of == date(2025, 1, 15)
    assert [q.canonical_id for q in out] == [
        "EUR.IRS.1Y",
        "EUR.HICP.1Y",
        "EUR.HICP.5Y",
    ]


# ---------------------------------------------------------------------------
# resolve - happy paths
# ---------------------------------------------------------------------------


async def test_resolve_returns_empty_when_no_quote_ids() -> None:
    md = _FakeMdClient()
    nominal = _curve(name="DISC", role="nominal", points=[{"rate": 0.03}])
    inflation = _curve(name="HICP", role="inflation", points=[{"rate": 0.02}])

    out = await resolve(
        nominal_curve=nominal,
        inflation_curve=inflation,
        as_of=date(2025, 1, 15),
        snapshot=None,
        md_client=md,  # type: ignore[arg-type]
    )

    assert out == []
    assert md.calls == []


async def test_resolve_pins_from_snapshot_short_circuit_md() -> None:
    md = _FakeMdClient()
    snapshot = ResolvedSnapshot(
        id=__import__("uuid").uuid4(),
        name="EOD-EUR",
        pins={
            "EUR.IRS.1Y": {"value": 0.03, "source": "vendor_x"},
            "EUR.HICP.1Y": {"value": 0.02, "source": None},
        },
    )
    nominal = _curve(name="DISC", role="nominal", points=[{"quote_id": "EUR.IRS.1Y"}])
    inflation = _curve(name="HICP_ZC", role="inflation", points=[{"quote_id": "EUR.HICP.1Y"}])

    out = await resolve(
        nominal_curve=nominal,
        inflation_curve=inflation,
        as_of=date(2025, 1, 15),
        snapshot=snapshot,
        md_client=md,  # type: ignore[arg-type]
    )

    assert [q.canonical_id for q in out] == ["EUR.IRS.1Y", "EUR.HICP.1Y"]
    assert all(q.from_snapshot is True for q in out)
    assert out[0].source == "vendor_x"
    assert out[1].source == "EOD-EUR"
    assert md.calls == []


async def test_resolve_mix_of_snapshot_pins_and_live_md() -> None:
    md = _FakeMdClient(results=[_resolved_quote(canonical_id="EUR.HICP.5Y", value=0.022)])
    snapshot = ResolvedSnapshot(
        id=__import__("uuid").uuid4(),
        name="EOD-EUR",
        pins={"EUR.IRS.1Y": {"value": 0.03, "source": "vendor_x"}},
    )
    nominal = _curve(name="DISC", role="nominal", points=[{"quote_id": "EUR.IRS.1Y"}])
    inflation = _curve(
        name="HICP_ZC",
        role="inflation",
        points=[{"quote_id": "EUR.HICP.5Y"}],
    )

    out = await resolve(
        nominal_curve=nominal,
        inflation_curve=inflation,
        as_of=date(2025, 1, 15),
        snapshot=snapshot,
        md_client=md,  # type: ignore[arg-type]
    )

    assert [q.canonical_id for q in out] == ["EUR.IRS.1Y", "EUR.HICP.5Y"]
    assert out[0].from_snapshot is True
    assert out[1].from_snapshot is False
    assert md.calls == [(["EUR.HICP.5Y"], date(2025, 1, 15))]


# ---------------------------------------------------------------------------
# resolve - error surfaces
# ---------------------------------------------------------------------------


async def test_resolve_per_item_not_found_raises_quote_resolution_failed() -> None:
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="EUR.IRS.1Y", value=0.03),
            _resolved_quote(canonical_id="EUR.HICP.1Y", value=0.0, found=False),
            _resolved_quote(canonical_id="EUR.HICP.5Y", value=0.0, found=False),
        ]
    )
    nominal = _curve(name="DISC", role="nominal", points=[{"quote_id": "EUR.IRS.1Y"}])
    inflation = _curve(
        name="HICP_ZC",
        role="inflation",
        points=[{"quote_id": "EUR.HICP.1Y"}, {"quote_id": "EUR.HICP.5Y"}],
    )

    with pytest.raises(SwapInflationQuoteResolutionFailedError) as excinfo:
        await resolve(
            nominal_curve=nominal,
            inflation_curve=inflation,
            as_of=date(2025, 1, 15),
            snapshot=None,
            md_client=md,  # type: ignore[arg-type]
        )

    err = excinfo.value
    assert err.code == "swap_inflation_quote_resolution_failed"
    assert err.status_code == 422
    assert err.details is not None
    missing_ids = [d["canonical_id"] for d in err.details]
    assert missing_ids == ["EUR.HICP.1Y", "EUR.HICP.5Y"]


async def test_resolve_md_transport_error_maps_to_md_unreachable() -> None:
    md = _FakeMdClient(raises=MdTransportError("connection refused"))
    nominal = _curve(name="DISC", role="nominal", points=[{"quote_id": "EUR.IRS.1Y"}])
    inflation = _curve(
        name="HICP_ZC",
        role="inflation",
        points=[{"quote_id": "EUR.HICP.1Y"}],
    )

    with pytest.raises(MdUnreachableError) as excinfo:
        await resolve(
            nominal_curve=nominal,
            inflation_curve=inflation,
            as_of=date(2025, 1, 15),
            snapshot=None,
            md_client=md,  # type: ignore[arg-type]
        )
    assert excinfo.value.code == "md_unreachable"


async def test_resolve_md_not_found_maps_to_quote_not_found() -> None:
    md = _FakeMdClient(raises=MdNotFoundError(404, "EUR.HICP.1Y not in MD upstream"))
    nominal = _curve(name="DISC", role="nominal", points=[{"quote_id": "EUR.IRS.1Y"}])
    inflation = _curve(
        name="HICP_ZC",
        role="inflation",
        points=[{"quote_id": "EUR.HICP.1Y"}],
    )

    with pytest.raises(QuoteNotFoundError) as excinfo:
        await resolve(
            nominal_curve=nominal,
            inflation_curve=inflation,
            as_of=date(2025, 1, 15),
            snapshot=None,
            md_client=md,  # type: ignore[arg-type]
        )
    assert excinfo.value.code == "quote_not_found"


async def test_resolve_md_http_status_error_maps_to_md_upstream_error() -> None:
    md = _FakeMdClient(raises=MdHttpStatusError(503, "upstream 503"))
    nominal = _curve(name="DISC", role="nominal", points=[{"quote_id": "EUR.IRS.1Y"}])
    inflation = _curve(
        name="HICP_ZC",
        role="inflation",
        points=[{"quote_id": "EUR.HICP.1Y"}],
    )

    with pytest.raises(MdUpstreamError) as excinfo:
        await resolve(
            nominal_curve=nominal,
            inflation_curve=inflation,
            as_of=date(2025, 1, 15),
            snapshot=None,
            md_client=md,  # type: ignore[arg-type]
        )
    assert excinfo.value.code == "md_upstream_error"
