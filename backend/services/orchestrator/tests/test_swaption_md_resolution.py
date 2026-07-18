"""Unit tests for ``pricing/swaption/md_resolution.py``.

The walker has three responsibilities:

* Collect every distinct ``quote_id`` from the resolved curves
  AND the resolved vol surface (the swaption-only extension over
  the swap_ir walker: vol-surface bodies carry quote IDs across
  ``MatrixCell`` grids, flat ID lists, nested ID lists, ``base``
  scalars, and per-date ``series`` snapshots).
* Substitute snapshot pins before falling through to live MD.
* Surface per-MD-error envelopes verbatim
  (``quote_not_found`` / ``md_unreachable`` / ``md_upstream_error``)
  and collapse per-item ``found=False`` into one
  ``swaption_quote_resolution_failed`` (422).

Tests use a hand-rolled ``MdClient`` substitute so the walker can
be exercised without provisioning the cache + httpx transport.
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
from quantra_orchestrator.pricing.swaption.assembler import ResolvedSnapshot
from quantra_orchestrator.pricing.swaption.errors import (
    SwaptionQuoteResolutionFailedError,
)
from quantra_orchestrator.pricing.swaption.md_resolution import (
    collect_quote_ids,
    resolve,
)
from quantra_orchestrator.pricing.swaption.models import (
    ResolvedCurve,
    ResolvedVolSurface,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeMdClient:
    """Minimum ``MdClient`` surface the walker exercises."""

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


def _vol_surface(
    *,
    name: str = "USD-ATM",
    kind: str = "SwaptionVolSpec",
    payload: dict[str, Any] | None = None,
) -> ResolvedVolSurface:
    return ResolvedVolSurface(
        id=None,
        name=name,
        kind=kind,
        payload=payload or {},
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
# collect_quote_ids — curves (same coverage as swap_ir)
# ---------------------------------------------------------------------------


def test_collect_quote_ids_extracts_flat_and_nested_curve_points() -> None:
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
    assert collect_quote_ids(curves, None) == ["USD.IRS.1Y", "USD.IRS.5Y"]


def test_collect_quote_ids_dedupes_across_curves_preserving_order() -> None:
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
    assert collect_quote_ids(curves, None) == [
        "USD.IRS.1Y",
        "USD.IRS.5Y",
        "USD.IRS.10Y",
    ]


def test_collect_quote_ids_empty_for_no_inputs() -> None:
    assert collect_quote_ids([], None) == []


# ---------------------------------------------------------------------------
# collect_quote_ids — vol surfaces (the swaption-only extension)
# ---------------------------------------------------------------------------


def test_collect_from_vol_surface_base_quote_id() -> None:
    surface = _vol_surface(payload={"base": {"quote_id": "USD.SWPTN.ATM.5Y10Y.VOL"}})
    assert collect_quote_ids([], surface) == ["USD.SWPTN.ATM.5Y10Y.VOL"]


def test_collect_from_vol_surface_spot_quote_id() -> None:
    surface = _vol_surface(
        kind="BlackVolSpec",
        payload={"spot_quote_id": "SPX.SPOT"},
    )
    assert collect_quote_ids([], surface) == ["SPX.SPOT"]


def test_collect_from_vol_surface_matrix2d_grid() -> None:
    """``MatrixCell`` grids: ``{"quoteId": "..."}`` cells get picked up."""

    surface = _vol_surface(
        payload={
            "grid": [
                [0.20, {"quoteId": "USD.SWPTN.ATM.1Y1Y.VOL"}],
                [{"quote_id": "USD.SWPTN.ATM.5Y5Y.VOL"}, 0.18],
            ]
        }
    )
    assert collect_quote_ids([], surface) == [
        "USD.SWPTN.ATM.1Y1Y.VOL",
        "USD.SWPTN.ATM.5Y5Y.VOL",
    ]


def test_collect_from_vol_surface_sabr_grids() -> None:
    surface = _vol_surface(
        payload={
            "sabr_alpha": [[{"quoteId": "SABR.ALPHA"}]],
            "sabr_rho": [[{"quoteId": "SABR.RHO"}]],
        }
    )
    assert set(collect_quote_ids([], surface)) == {"SABR.ALPHA", "SABR.RHO"}


def test_collect_from_vol_surface_sabr_market_vols_cube() -> None:
    surface = _vol_surface(
        payload={
            "sabr_market_vols": [
                [
                    [0.18, {"quoteId": "SABR.MKT.1Y1Y.K0"}],
                    [{"quoteId": "SABR.MKT.1Y2Y.K0"}, 0.19],
                ]
            ]
        }
    )
    assert collect_quote_ids([], surface) == [
        "SABR.MKT.1Y1Y.K0",
        "SABR.MKT.1Y2Y.K0",
    ]


def test_collect_from_vol_surface_surface_price_quote_ids_flat_list() -> None:
    surface = _vol_surface(
        kind="BlackVolSpec",
        payload={"surface_price_quote_ids": ["SPX.OPT.1", "SPX.OPT.2", "SPX.OPT.3"]},
    )
    assert collect_quote_ids([], surface) == [
        "SPX.OPT.1",
        "SPX.OPT.2",
        "SPX.OPT.3",
    ]


def test_collect_from_vol_surface_quote_ids_nested_list() -> None:
    surface = _vol_surface(payload={"quote_ids": [["A.1Y", "A.2Y"], ["B.1Y", "B.2Y"]]})
    assert collect_quote_ids([], surface) == [
        "A.1Y",
        "A.2Y",
        "B.1Y",
        "B.2Y",
    ]


def test_collect_from_vol_surface_series_entries() -> None:
    surface = _vol_surface(
        payload={
            "series": [
                {
                    "date": "2026-05-13",
                    "base": {"quote_id": "USD.SWPTN.ATM.SERIES"},
                    "grid": [[{"quoteId": "SERIES.GRID"}]],
                }
            ]
        }
    )
    assert collect_quote_ids([], surface) == [
        "USD.SWPTN.ATM.SERIES",
        "SERIES.GRID",
    ]


def test_collect_curves_first_then_vol_surface_dedupes_across_both() -> None:
    """Curve IDs come first; surface IDs follow; overlap dedupes."""

    curves = [_curve(points=[{"quote_id": "USD.IRS.5Y"}])]
    surface = _vol_surface(
        payload={
            "base": {"quote_id": "USD.SWPTN.5Y5Y"},
            "grid": [[{"quoteId": "USD.IRS.5Y"}]],  # already in curves
        }
    )
    assert collect_quote_ids(curves, surface) == [
        "USD.IRS.5Y",
        "USD.SWPTN.5Y5Y",
    ]


def test_collect_ignores_unknown_keys_silently() -> None:
    """Unknown payload keys are not over-collected — they're skipped."""

    surface = _vol_surface(
        payload={
            "axes_expiries": ["1Y", "5Y"],  # not a quote-bearing field
            "axes_tenors": ["2Y", "10Y"],
            "unknown_blob": {"quote_id": "WOULD.BE.MISS"},  # not in our walker
        }
    )
    assert collect_quote_ids([], surface) == []


# ---------------------------------------------------------------------------
# resolve — happy paths
# ---------------------------------------------------------------------------


async def test_resolve_returns_empty_when_no_quote_ids() -> None:
    md = _FakeMdClient()
    curves = [_curve(points=[{"tenor": "1Y", "rate": 0.04}])]
    surface = _vol_surface(payload={"axes_expiries": ["1Y"]})

    out = await resolve(
        curves=curves,
        vol_surface=surface,
        as_of=date(2026, 5, 13),
        snapshot=None,
        md_client=md,  # type: ignore[arg-type]
    )

    assert out == []
    assert md.calls == []


async def test_resolve_combines_curve_and_surface_ids_in_one_md_call() -> None:
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="USD.IRS.1Y", value=4.25),
            _resolved_quote(canonical_id="USD.SWPTN.5Y5Y.VOL", value=0.65),
        ]
    )
    curves = [_curve(points=[{"quote_id": "USD.IRS.1Y"}])]
    surface = _vol_surface(payload={"base": {"quote_id": "USD.SWPTN.5Y5Y.VOL"}})

    out = await resolve(
        curves=curves,
        vol_surface=surface,
        as_of=date(2026, 5, 13),
        snapshot=None,
        md_client=md,  # type: ignore[arg-type]
    )

    assert [q.canonical_id for q in out] == [
        "USD.IRS.1Y",
        "USD.SWPTN.5Y5Y.VOL",
    ]
    # One batched MD call carries both IDs.
    assert len(md.calls) == 1
    assert md.calls[0][0] == ["USD.IRS.1Y", "USD.SWPTN.5Y5Y.VOL"]


async def test_resolve_pins_from_snapshot_short_circuit_md_for_surface_ids() -> None:
    """Snapshot pins cover surface IDs the same way they cover curve IDs."""

    md = _FakeMdClient()
    snapshot = ResolvedSnapshot(
        id=uuid.uuid4(),
        name="EOD-USD",
        pins={
            "USD.SWPTN.5Y5Y.VOL": {"value": 0.65, "source": "vendor_x"},
        },
    )
    surface = _vol_surface(payload={"base": {"quote_id": "USD.SWPTN.5Y5Y.VOL"}})

    out = await resolve(
        curves=[],
        vol_surface=surface,
        as_of=date(2026, 5, 13),
        snapshot=snapshot,
        md_client=md,  # type: ignore[arg-type]
    )

    assert len(out) == 1
    assert out[0].from_snapshot is True
    assert out[0].value == pytest.approx(0.65)
    assert md.calls == []


# ---------------------------------------------------------------------------
# resolve — error surfaces
# ---------------------------------------------------------------------------


async def test_resolve_per_item_not_found_raises_swaption_quote_resolution_failed() -> None:
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="USD.IRS.1Y", value=4.25),
            _resolved_quote(canonical_id="USD.SWPTN.5Y5Y.VOL", value=0.0, found=False),
        ]
    )
    curves = [_curve(points=[{"quote_id": "USD.IRS.1Y"}])]
    surface = _vol_surface(payload={"base": {"quote_id": "USD.SWPTN.5Y5Y.VOL"}})

    with pytest.raises(SwaptionQuoteResolutionFailedError) as excinfo:
        await resolve(
            curves=curves,
            vol_surface=surface,
            as_of=date(2026, 5, 13),
            snapshot=None,
            md_client=md,  # type: ignore[arg-type]
        )

    err = excinfo.value
    assert err.code == "swaption_quote_resolution_failed"
    assert err.status_code == 422
    assert err.details is not None
    missing_ids = [d["canonical_id"] for d in err.details]
    assert missing_ids == ["USD.SWPTN.5Y5Y.VOL"]


async def test_resolve_md_transport_error_maps_to_md_unreachable() -> None:
    md = _FakeMdClient(raises=MdTransportError("connection refused"))
    curves = [_curve(points=[{"quote_id": "USD.IRS.1Y"}])]

    with pytest.raises(MdUnreachableError) as excinfo:
        await resolve(
            curves=curves,
            vol_surface=None,
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
            vol_surface=None,
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
            vol_surface=None,
            as_of=date(2026, 5, 13),
            snapshot=None,
            md_client=md,  # type: ignore[arg-type]
        )
    assert excinfo.value.code == "md_upstream_error"
