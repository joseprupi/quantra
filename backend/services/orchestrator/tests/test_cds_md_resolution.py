"""Unit tests for ``pricing/cds/md_resolution.py``.

The CDS walker is the swap_ir walker minus the projection curve:
**discount-curve-only**.
These tests pin:

* ``collect_quote_ids`` walks the same shape as swap_ir
  (helper-wrapped + flat, dedup, order-preserving, ``quoteId``
  camel-case alias).
* The walker accepts a ``credit_curve`` shape **but never traverses
  it** — even if a credit curve carries a ``quote_id`` body, the
  walker only looks at the curves it's handed; the assembler is
  the gatekeeper that rejects credit curves with quote refs.
* Snapshot pins short-circuit MD; misses fall through.
* Per-item ``found=False`` markers roll into a single
  ``cds_quote_resolution_failed`` (422 — distinct from
  ``cds_curve_resolution_failed``).
* Per-MD-error MD-error envelopes (``quote_not_found`` /
  ``md_unreachable`` / ``md_upstream_error``) surface verbatim.
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
from quantra_orchestrator.pricing.cds.assembler import ResolvedSnapshot
from quantra_orchestrator.pricing.cds.errors import (
    CdsQuoteResolutionFailedError,
)
from quantra_orchestrator.pricing.cds.md_resolution import (
    collect_quote_ids,
    resolve,
)
from quantra_orchestrator.pricing.cds.models import ResolvedCurve


class _FakeMdClient:
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
                {"tenor": "1Y", "quote_id": "USD.IRS.1Y"},
                {
                    "point_type": "swap",
                    "point": {"tenor": "5Y", "quote_id": "USD.IRS.5Y"},
                },
            ]
        )
    ]
    assert collect_quote_ids(curves) == ["USD.IRS.1Y", "USD.IRS.5Y"]


def test_collect_quote_ids_dedupes_preserving_order() -> None:
    curves = [
        _curve(
            name="a",
            points=[{"quote_id": "USD.IRS.1Y"}, {"quote_id": "USD.IRS.5Y"}],
        ),
        _curve(
            name="b",
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


async def test_resolve_via_live_md_when_no_snapshot() -> None:
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


async def test_resolve_mix_of_snapshot_pins_and_live_md() -> None:
    md = _FakeMdClient(results=[_resolved_quote(canonical_id="USD.IRS.10Y", value=4.05)])
    snapshot = ResolvedSnapshot(
        id=uuid.uuid4(),
        name="EOD-USD",
        pins={"USD.IRS.1Y": {"value": 4.25, "source": "vendor_x"}},
    )
    curves = [
        _curve(
            points=[
                {"quote_id": "USD.IRS.1Y"},
                {"quote_id": "USD.IRS.10Y"},
            ]
        )
    ]

    out = await resolve(
        curves=curves,
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


async def test_resolve_per_item_not_found_raises_cds_quote_resolution_failed() -> None:
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

    with pytest.raises(CdsQuoteResolutionFailedError) as excinfo:
        await resolve(
            curves=curves,
            as_of=date(2026, 5, 13),
            snapshot=None,
            md_client=md,  # type: ignore[arg-type]
        )
    err = excinfo.value
    assert err.code == "cds_quote_resolution_failed"
    assert err.status_code == 422
    assert err.details is not None
    missing = [d["canonical_id"] for d in err.details]
    assert missing == ["USD.IRS.5Y", "USD.IRS.10Y"]


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
