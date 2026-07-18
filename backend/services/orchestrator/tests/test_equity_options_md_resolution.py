"""Unit tests for ``pricing/equity_options/md_resolution.py``.

The equity-options walker is structurally the swaption walker (a
multi-leaf walker over curves + a vol-surface body) **plus** a third
leaf — the underlier spot. Every leaf with a canonical id
flows through the MD client; the spot just adds one extra canonical
id alongside the curve / surface ones. These tests pin:

* ``collect_quote_ids`` walks curves + the BlackVol surface body
  + the spot leaf, dedupes, and is order-preserving.
* The spot's inline-value branch short-circuits the canonical-id
  collection (we don't ask MD for a value the caller already
  supplied).
* The vol-surface walker recognises both the constant-vol body
  shape (``base.quote_id``) and the matrix-grid bodies
  (``surface_vols`` / ``term_vols`` / ``surface_prices``) as well
  as the flat / nested ID-array shapes
  (``surface_price_quote_ids`` / ``quote_ids``).
* Snapshot pins short-circuit MD; misses fall through.
* Per-item ``found=False`` markers roll into a single
  ``equity_option_quote_resolution_failed`` (422 — distinct from
  ``equity_option_curve_resolution_failed``).
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
from quantra_orchestrator.pricing.equity_options.assembler import (
    ResolvedSnapshot,
)
from quantra_orchestrator.pricing.equity_options.errors import (
    EquityOptionQuoteResolutionFailedError,
)
from quantra_orchestrator.pricing.equity_options.md_resolution import (
    collect_quote_ids,
    resolve,
)
from quantra_orchestrator.pricing.equity_options.models import (
    ResolvedCurve,
    ResolvedSpotQuote,
    ResolvedVolSurface,
)


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
    role: str = "discount",
    points: list[dict[str, Any]] | None = None,
) -> ResolvedCurve:
    return ResolvedCurve(
        id=None,
        name=name,
        role=role,
        currency="USD",
        day_counter="Actual/365",
        helper_kind="Discount",
        reference_date=date(2026, 5, 13),
        points=points or [],
        body={"interpolator": "LogLinear"},
    )


def _vol_surface(payload: dict[str, Any]) -> ResolvedVolSurface:
    return ResolvedVolSurface(
        id=None,
        name="AAPL-vol",
        kind="BlackVolSpec",
        payload=payload,
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


def test_collect_quote_ids_walks_curves_surface_and_spot() -> None:
    curves = [
        _curve(
            role="discount",
            points=[{"quote_id": "USD.IRS.1Y"}],
        ),
        _curve(
            name="dividend-yield",
            role="dividend",
            points=[{"quote_id": "AAPL.DIV.1Y"}],
        ),
    ]
    surface = _vol_surface(
        {
            "base": {"quote_id": "AAPL.VOL.BASE"},
            "term_vols": [
                [{"quote_id": "AAPL.VOL.1M"}, {"quote_id": "AAPL.VOL.3M"}],
            ],
        }
    )
    spot = ResolvedSpotQuote(canonical_id="AAPL.SPOT", value=None)
    assert collect_quote_ids(curves, surface, spot) == [
        "USD.IRS.1Y",
        "AAPL.DIV.1Y",
        "AAPL.VOL.BASE",
        "AAPL.VOL.1M",
        "AAPL.VOL.3M",
        "AAPL.SPOT",
    ]


def test_collect_quote_ids_inline_spot_short_circuits_md_collection() -> None:
    """Inline ``value`` means we don't add the canonical_id to the MD lookup list.

    The canonical id is still propagated in :class:`ResolvedSpotQuote`
    but we already have the value; firing an MD call for it would
    waste a round-trip and risk overriding the operator's pin.
    """

    spot = ResolvedSpotQuote(canonical_id="AAPL.SPOT", value=120.5)
    assert collect_quote_ids([], None, spot) == []


def test_collect_quote_ids_dedupes_preserving_order() -> None:
    curves = [
        _curve(
            role="discount",
            points=[{"quote_id": "USD.IRS.1Y"}, {"quote_id": "USD.IRS.5Y"}],
        ),
    ]
    surface = _vol_surface(
        {
            "base": {"quote_id": "USD.IRS.1Y"},
            "spot_quote_id": "AAPL.SPOT",
        }
    )
    spot = ResolvedSpotQuote(canonical_id="AAPL.SPOT", value=None)
    assert collect_quote_ids(curves, surface, spot) == [
        "USD.IRS.1Y",
        "USD.IRS.5Y",
        "AAPL.SPOT",
    ]


def test_collect_quote_ids_handles_quoteid_camelcase() -> None:
    surface = _vol_surface({"base": {"quoteId": "AAPL.VOL.BASE"}})
    assert collect_quote_ids([], surface, None) == ["AAPL.VOL.BASE"]


def test_collect_quote_ids_walks_surface_price_quote_ids_flat_array() -> None:
    surface = _vol_surface({"surface_price_quote_ids": ["AAPL.PX.1", "AAPL.PX.2"]})
    assert collect_quote_ids([], surface, None) == [
        "AAPL.PX.1",
        "AAPL.PX.2",
    ]


def test_collect_quote_ids_walks_quote_ids_nested_array() -> None:
    surface = _vol_surface(
        {
            "quote_ids": [
                ["AAPL.VOL.1M.95", "AAPL.VOL.1M.100"],
                ["AAPL.VOL.3M.95", "AAPL.VOL.3M.100"],
            ]
        }
    )
    assert collect_quote_ids([], surface, None) == [
        "AAPL.VOL.1M.95",
        "AAPL.VOL.1M.100",
        "AAPL.VOL.3M.95",
        "AAPL.VOL.3M.100",
    ]


def test_collect_quote_ids_skips_constant_value_cells() -> None:
    """A literal float in a matrix grid is a constant value, not a quote ref."""

    surface = _vol_surface(
        {
            "term_vols": [
                [0.20, {"quote_id": "AAPL.VOL.3M"}],
            ]
        }
    )
    assert collect_quote_ids([], surface, None) == ["AAPL.VOL.3M"]


def test_collect_quote_ids_empty_for_no_inputs() -> None:
    assert collect_quote_ids([], None, None) == []


# ---------------------------------------------------------------------------
# resolve - happy paths
# ---------------------------------------------------------------------------


async def test_resolve_returns_empty_when_no_quote_ids() -> None:
    md = _FakeMdClient()
    curves = [_curve(role="discount", points=[{"tenor": "1Y", "rate": 0.04}])]

    out = await resolve(
        curves=curves,
        vol_surface=None,
        spot=None,
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
            _resolved_quote(canonical_id="AAPL.VOL.BASE", value=0.22),
            _resolved_quote(canonical_id="AAPL.SPOT", value=152.30),
        ]
    )
    curves = [_curve(points=[{"quote_id": "USD.IRS.1Y"}])]
    surface = _vol_surface({"base": {"quote_id": "AAPL.VOL.BASE"}})
    spot = ResolvedSpotQuote(canonical_id="AAPL.SPOT", value=None)

    out = await resolve(
        curves=curves,
        vol_surface=surface,
        spot=spot,
        as_of=date(2026, 5, 13),
        snapshot=None,
        md_client=md,  # type: ignore[arg-type]
    )

    assert [q.canonical_id for q in out] == [
        "USD.IRS.1Y",
        "AAPL.VOL.BASE",
        "AAPL.SPOT",
    ]
    assert all(q.from_snapshot is False for q in out)
    assert md.calls == [(["USD.IRS.1Y", "AAPL.VOL.BASE", "AAPL.SPOT"], date(2026, 5, 13))]


async def test_resolve_pins_from_snapshot_short_circuit_md() -> None:
    md = _FakeMdClient()
    snapshot = ResolvedSnapshot(
        id=uuid.uuid4(),
        name="EOD-AAPL",
        pins={
            "USD.IRS.1Y": {"value": 4.25, "source": "vendor_x"},
            "AAPL.SPOT": {"value": 150.0, "source": None},
        },
    )
    curves = [_curve(points=[{"quote_id": "USD.IRS.1Y"}])]
    spot = ResolvedSpotQuote(canonical_id="AAPL.SPOT", value=None)

    out = await resolve(
        curves=curves,
        vol_surface=None,
        spot=spot,
        as_of=date(2026, 5, 13),
        snapshot=snapshot,
        md_client=md,  # type: ignore[arg-type]
    )

    assert [q.canonical_id for q in out] == ["USD.IRS.1Y", "AAPL.SPOT"]
    assert all(q.from_snapshot is True for q in out)
    assert out[0].source == "vendor_x"
    assert out[1].source == "EOD-AAPL"
    assert md.calls == []


async def test_resolve_inline_spot_value_skips_md_completely() -> None:
    """Inline spot ``value`` → no canonical id in the MD lookup list."""

    md = _FakeMdClient()
    spot = ResolvedSpotQuote(canonical_id="AAPL.SPOT", value=152.30)

    out = await resolve(
        curves=[],
        vol_surface=None,
        spot=spot,
        as_of=date(2026, 5, 13),
        snapshot=None,
        md_client=md,  # type: ignore[arg-type]
    )

    assert out == []
    assert md.calls == []


# ---------------------------------------------------------------------------
# resolve - error surfaces
# ---------------------------------------------------------------------------


async def test_resolve_per_item_not_found_raises_quote_resolution_failed() -> None:
    md = _FakeMdClient(
        results=[
            _resolved_quote(canonical_id="USD.IRS.1Y", value=4.25),
            _resolved_quote(canonical_id="AAPL.VOL.BASE", value=0.0, found=False),
        ]
    )
    curves = [_curve(points=[{"quote_id": "USD.IRS.1Y"}])]
    surface = _vol_surface({"base": {"quote_id": "AAPL.VOL.BASE"}})

    with pytest.raises(EquityOptionQuoteResolutionFailedError) as excinfo:
        await resolve(
            curves=curves,
            vol_surface=surface,
            spot=None,
            as_of=date(2026, 5, 13),
            snapshot=None,
            md_client=md,  # type: ignore[arg-type]
        )
    err = excinfo.value
    assert err.code == "equity_option_quote_resolution_failed"
    assert err.status_code == 422
    assert err.details is not None
    missing = [d["canonical_id"] for d in err.details]
    assert missing == ["AAPL.VOL.BASE"]


async def test_resolve_md_transport_error_maps_to_md_unreachable() -> None:
    md = _FakeMdClient(raises=MdTransportError("connection refused"))
    curves = [_curve(points=[{"quote_id": "USD.IRS.1Y"}])]

    with pytest.raises(MdUnreachableError) as excinfo:
        await resolve(
            curves=curves,
            vol_surface=None,
            spot=None,
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
            spot=None,
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
            spot=None,
            as_of=date(2026, 5, 13),
            snapshot=None,
            md_client=md,  # type: ignore[arg-type]
        )
    assert excinfo.value.code == "md_upstream_error"
