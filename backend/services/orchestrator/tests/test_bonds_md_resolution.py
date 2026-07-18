"""Unit tests for ``pricing/bonds/md_resolution.py``.

The walker mirrors the swap_ir-side walker structurally and is
tested against the same three contracts:

* Collect every distinct ``quote_id`` from the resolved curves
  (helper-wrapped *and* flat shapes, no duplicates, input-order
  preserved across a list of curves).
* Substitute snapshot pins before falling through to live MD.
* Surface per-MD-error envelopes verbatim
  (``quote_not_found`` / ``md_unreachable`` / ``md_upstream_error``)
  and collapse per-item ``found=False`` into one
  ``bond_quote_resolution_failed`` (422).

Additional coverage specific to bonds:

* The walker accepts an ordered list of curves (the floating route
  passes ``[discount, projection]``); ordering matters for the
  resolved-quotes echo.
"""

from __future__ import annotations

import uuid
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
from quantra_orchestrator.pricing.bonds.assembler import ResolvedSnapshot
from quantra_orchestrator.pricing.bonds.errors import (
    BondQuoteResolutionFailedError,
)
from quantra_orchestrator.pricing.bonds.md_resolution import (
    collect_quote_ids,
    resolve,
)
from quantra_orchestrator.pricing.bonds.models import ResolvedCurve


class _FakeMdClient:
    """Minimum ``MdClient`` surface the walker exercises.

    Only ``resolve_quotes`` is called; everything else is omitted so
    the test stays focused. We cast at the call site to keep the
    suite from importing the full client just for a one-method stub.
    """

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
    name: str = "USD-OIS",
    points: list[dict[str, Any]],
) -> ResolvedCurve:
    return ResolvedCurve(
        id=None,
        name=name,
        currency="USD",
        day_counter="Actual/360",
        helper_kind="Discount",
        reference_date=date(2026, 5, 13),
        points=points,
        body={"interpolator": "Cubic"},
    )


def _resolved_quote(
    *,
    canonical_id: str,
    value: float,
    found: bool = True,
    source: str | None = "vendor_x",
    resolved_as_of: datetime | None = None,
) -> ResolvedQuote:
    return ResolvedQuote(
        canonical_id=canonical_id,
        requested_as_of=datetime(2026, 5, 13, 0, 0),
        resolved_as_of=resolved_as_of,
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
    curves = [
        _curve(
            points=[
                {"tenor": "1Y", "quote_id": "USD.GOVT.1Y"},
                {
                    "point_type": "fixed_rate_bond",
                    "point": {"tenor": "5Y", "quote_id": "USD.GOVT.5Y"},
                },
            ]
        )
    ]
    assert collect_quote_ids(curves) == ["USD.GOVT.1Y", "USD.GOVT.5Y"]


def test_collect_quote_ids_dedupes_across_curves_preserving_order() -> None:
    """Two-curve walks: dedupe across the (discount, projection) pair.

    The floating route passes both curves to the walker. When the
    discount and projection curves share quote IDs (common when
    ``use_same_curve`` is set), each ID only resolves once and the
    response echo lists IDs in (discount-first, then projection-only)
    order.
    """

    curves = [
        _curve(
            name="discount",
            points=[{"quote_id": "USD.IRS.1Y"}, {"quote_id": "USD.IRS.5Y"}],
        ),
        _curve(
            name="projection",
            points=[{"quote_id": "USD.IRS.1Y"}, {"quote_id": "USD.IRS.10Y"}],
        ),
    ]
    assert collect_quote_ids(curves) == [
        "USD.IRS.1Y",
        "USD.IRS.5Y",
        "USD.IRS.10Y",
    ]


def test_collect_quote_ids_handles_quoteid_camelcase() -> None:
    curves = [_curve(points=[{"quoteId": "USD.IRS.1Y"}])]
    assert collect_quote_ids(curves) == ["USD.IRS.1Y"]


def test_collect_quote_ids_skips_points_without_a_quote_id() -> None:
    curves = [
        _curve(
            points=[
                {"tenor": "1Y", "rate": 0.0425},
                {"quote_id": "USD.IRS.1Y"},
            ]
        )
    ]
    assert collect_quote_ids(curves) == ["USD.IRS.1Y"]


def test_collect_quote_ids_empty_for_no_curves() -> None:
    assert collect_quote_ids([]) == []


# ---------------------------------------------------------------------------
# resolve - happy paths
# ---------------------------------------------------------------------------


async def test_resolve_returns_empty_when_no_quote_ids() -> None:
    md = _FakeMdClient()
    curves = [_curve(points=[{"tenor": "1Y", "rate": 0.04}])]

    out = await resolve(
        curves=curves,
        as_of=date(2026, 5, 13),
        snapshot=None,
        md_client=md,  # type: ignore[arg-type]
    )

    assert out == []
    assert md.calls == []


async def test_resolve_all_via_live_md_when_no_snapshot() -> None:
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="USD.IRS.1Y", value=4.25),
            _resolved_quote(canonical_id="USD.IRS.5Y", value=4.10),
        ]
    )
    curves = [_curve(points=[{"quote_id": "USD.IRS.1Y"}, {"quote_id": "USD.IRS.5Y"}])]

    out = await resolve(
        curves=curves,
        as_of=date(2026, 5, 13),
        snapshot=None,
        md_client=md,  # type: ignore[arg-type]
    )

    assert [q.canonical_id for q in out] == ["USD.IRS.1Y", "USD.IRS.5Y"]
    assert all(q.from_snapshot is False for q in out)
    assert md.calls == [(["USD.IRS.1Y", "USD.IRS.5Y"], date(2026, 5, 13))]


async def test_resolve_pins_from_snapshot_short_circuit_md() -> None:
    md = _FakeMdClient()
    snapshot = ResolvedSnapshot(
        id=uuid.uuid4(),
        name="EOD-USD",
        pins={
            "USD.IRS.1Y": {"value": 4.25, "source": "vendor_x"},
            "USD.IRS.5Y": {"value": 4.10, "source": None},
        },
    )
    curves = [_curve(points=[{"quote_id": "USD.IRS.1Y"}, {"quote_id": "USD.IRS.5Y"}])]

    out = await resolve(
        curves=curves,
        as_of=date(2026, 5, 13),
        snapshot=snapshot,
        md_client=md,  # type: ignore[arg-type]
    )

    assert [q.canonical_id for q in out] == ["USD.IRS.1Y", "USD.IRS.5Y"]
    assert all(q.from_snapshot is True for q in out)
    assert out[0].source == "vendor_x"
    assert out[1].source == "EOD-USD"
    assert md.calls == []


async def test_resolve_mix_of_snapshot_pins_and_live_md_two_curves() -> None:
    """The walker covers a (discount, projection) pair end-to-end.

    The discount curve's quote is pinned in the snapshot; the
    projection curve's quote falls through to live MD. The echo
    preserves "discount IDs first, then projection-only IDs"
    ordering — same contract as :func:`collect_quote_ids`.
    """

    md = _FakeMdClient(results=[_resolved_quote(canonical_id="USD.IRS.10Y", value=4.05)])
    snapshot = ResolvedSnapshot(
        id=uuid.uuid4(),
        name="EOD-USD",
        pins={"USD.IRS.1Y": {"value": 4.25, "source": "vendor_x"}},
    )
    discount = _curve(name="discount", points=[{"quote_id": "USD.IRS.1Y"}])
    projection = _curve(name="projection", points=[{"quote_id": "USD.IRS.10Y"}])

    out = await resolve(
        curves=[discount, projection],
        as_of=date(2026, 5, 13),
        snapshot=snapshot,
        md_client=md,  # type: ignore[arg-type]
    )

    assert [q.canonical_id for q in out] == ["USD.IRS.1Y", "USD.IRS.10Y"]
    assert out[0].from_snapshot is True
    assert out[1].from_snapshot is False
    assert md.calls == [(["USD.IRS.10Y"], date(2026, 5, 13))]


# ---------------------------------------------------------------------------
# resolve - error surfaces
# ---------------------------------------------------------------------------


async def test_resolve_per_item_not_found_raises_bond_quote_resolution_failed() -> None:
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="USD.IRS.1Y", value=4.25),
            _resolved_quote(canonical_id="USD.IRS.5Y", value=0.0, found=False),
            _resolved_quote(canonical_id="USD.IRS.10Y", value=0.0, found=False),
        ]
    )
    curves = [
        _curve(
            points=[
                {"quote_id": "USD.IRS.1Y"},
                {"quote_id": "USD.IRS.5Y"},
                {"quote_id": "USD.IRS.10Y"},
            ]
        )
    ]

    with pytest.raises(BondQuoteResolutionFailedError) as excinfo:
        await resolve(
            curves=curves,
            as_of=date(2026, 5, 13),
            snapshot=None,
            md_client=md,  # type: ignore[arg-type]
        )

    err = excinfo.value
    assert err.code == "bond_quote_resolution_failed"
    assert err.status_code == 422
    assert err.details is not None
    missing_ids = [d["canonical_id"] for d in err.details]
    assert missing_ids == ["USD.IRS.5Y", "USD.IRS.10Y"]


async def test_resolve_md_transport_error_maps_to_md_unreachable() -> None:
    md = _FakeMdClient(raises=MdTransportError("connection refused"))
    curves = [_curve(points=[{"quote_id": "USD.IRS.1Y"}])]

    with pytest.raises(MdUnreachableError) as excinfo:
        await resolve(
            curves=curves,
            as_of=date(2026, 5, 13),
            snapshot=None,
            md_client=md,  # type: ignore[arg-type]
        )
    assert excinfo.value.code == "md_unreachable"


async def test_resolve_md_not_found_maps_to_quote_not_found() -> None:
    md = _FakeMdClient(raises=MdNotFoundError(404, "USD.IRS.1Y not in MD upstream"))
    curves = [_curve(points=[{"quote_id": "USD.IRS.1Y"}])]

    with pytest.raises(QuoteNotFoundError) as excinfo:
        await resolve(
            curves=curves,
            as_of=date(2026, 5, 13),
            snapshot=None,
            md_client=md,  # type: ignore[arg-type]
        )
    assert excinfo.value.code == "quote_not_found"


async def test_resolve_md_http_status_error_maps_to_md_upstream_error() -> None:
    md = _FakeMdClient(raises=MdHttpStatusError(503, "upstream 503"))
    curves = [_curve(points=[{"quote_id": "USD.IRS.1Y"}])]

    with pytest.raises(MdUpstreamError) as excinfo:
        await resolve(
            curves=curves,
            as_of=date(2026, 5, 13),
            snapshot=None,
            md_client=md,  # type: ignore[arg-type]
        )
    assert excinfo.value.code == "md_upstream_error"


async def test_resolve_truncates_breadcrumb_id_list_in_quote_not_found_detail() -> None:
    """For large miss batches, the breadcrumb canonical-id list is truncated.

    Same contract as the swap_ir-side walker — ensures a 1k-quote
    batch doesn't render a multi-KB ``detail`` string when the MD
    upstream blows up. Full miss list still lands in the
    (separately-surfaced) 422's ``details`` payload.
    """

    md = _FakeMdClient(raises=MdNotFoundError(404, "missing"))
    many_points = [{"quote_id": f"USD.IRS.{i}Y"} for i in range(1, 11)]
    curves = [_curve(points=many_points)]

    with pytest.raises(QuoteNotFoundError) as excinfo:
        await resolve(
            curves=curves,
            as_of=date(2026, 5, 13),
            snapshot=None,
            md_client=md,  # type: ignore[arg-type]
        )
    detail_text = str(excinfo.value.detail)
    assert "..." in detail_text
    assert "USD.IRS.10Y" not in detail_text
