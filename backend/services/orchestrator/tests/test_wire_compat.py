"""Tests for the engine wire-format compat seam (``wire_compat``).

Engine 0.4.0 inserted ``notionals`` mid-table into four FlatBuffers tables,
shifting their slot ids — a binary wire break vs the pinned engine 0.2.0.
``wire_compat`` packs/unpacks those four tables in the layout selected by
``ENGINE_WIRE_COMPAT`` (default ``0.2`` = the compose engine pin). These tests
pin both layouts at the byte/slot level and the decode round-trip in each
mode, so a regen or a default flip that changes the wire fails loudly here.
"""

from __future__ import annotations

from typing import Any

import flatbuffers
import pytest

from quantra_common.engine_client._generated.quantra.FixedRateBond import (
    FixedRateBond as CanonFixedRateBondReader,
)
from quantra_common.engine_client._generated.quantra.Schedule import ScheduleT
from quantra_common.engine_client._generated.quantra.SwapFixedLeg import (
    SwapFixedLeg as CanonSwapFixedLegReader,
)
from quantra_common.engine_client._legacy_v020 import (
    fixed_rate_bond as legacy_fixed_rate_bond,
)
from quantra_common.engine_client._legacy_v020 import (
    swap_fixed_leg as legacy_swap_fixed_leg,
)
from quantra_common.engine_client.wire_compat import (
    FixedRateBondT,
    SwapFixedLegT,
    engine_wire_target,
)


def _pack(obj: object) -> bytes:
    builder = flatbuffers.Builder(256)
    builder.ForceDefaults(True)
    builder.Finish(obj.Pack(builder))  # type: ignore[attr-defined]
    return bytes(builder.Output())


def _fixed_leg() -> Any:
    leg = SwapFixedLegT()
    schedule = ScheduleT()
    schedule.effectiveDate = "2026-06-15"
    schedule.terminationDate = "2031-06-15"
    leg.schedule = schedule
    leg.notional = 1_000_000.0
    leg.rate = 0.035
    leg.dayCounter = 5
    leg.paymentConvention = 1
    return leg


def _fixed_bond() -> Any:
    bond = FixedRateBondT()
    schedule = ScheduleT()
    schedule.effectiveDate = "2026-06-15"
    schedule.terminationDate = "2031-06-15"
    bond.schedule = schedule
    bond.settlementDays = 2
    bond.faceAmount = 100.0
    bond.rate = 0.05
    bond.accrualDayCounter = 3
    bond.paymentConvention = 1
    bond.redemption = 100.0
    bond.issueDate = "2026-06-15"
    return bond


def test_default_target_is_the_pinned_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENGINE_WIRE_COMPAT", raising=False)
    assert engine_wire_target() == "0.2"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0.2", "0.2"), ("0.2.0", "0.2"), ("0.5", "0.5"), ("0.5.0", "0.5"), ("master", "0.5")],
)
def test_target_value_normalisation(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
) -> None:
    monkeypatch.setenv("ENGINE_WIRE_COMPAT", raw)
    assert engine_wire_target() == expected


def test_unknown_target_refuses_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENGINE_WIRE_COMPAT", "0.4-ish")
    with pytest.raises(ValueError, match="ENGINE_WIRE_COMPAT"):
        engine_wire_target()


def test_legacy_pack_writes_the_v020_slot_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    """In 0.2 mode the legacy reader finds the rate; the canonical one must not."""

    monkeypatch.setenv("ENGINE_WIRE_COMPAT", "0.2")
    buf = _pack(_fixed_leg())

    legacy_reader = legacy_swap_fixed_leg.SwapFixedLeg.GetRootAs(bytearray(buf), 0)
    assert legacy_reader.Rate() == pytest.approx(0.035)
    assert legacy_reader.DayCounter() == 5

    canonical_reader = CanonSwapFixedLegReader.GetRootAs(bytearray(buf), 0)
    # The canonical reader looks for ``rate`` in the slot the legacy layout
    # never wrote (it belongs to ``notionals``'s successor there): the value
    # must NOT round-trip, proving the layouts genuinely differ on the wire.
    assert canonical_reader.Rate() != pytest.approx(0.035)


def test_canonical_pack_writes_the_master_slot_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENGINE_WIRE_COMPAT", "0.5")
    buf = _pack(_fixed_leg())
    canonical_reader = CanonSwapFixedLegReader.GetRootAs(bytearray(buf), 0)
    assert canonical_reader.Rate() == pytest.approx(0.035)
    assert canonical_reader.DayCounter() == 5


@pytest.mark.parametrize("mode", ["0.2", "0.5"])
def test_swap_fixed_leg_object_api_round_trips_per_mode(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("ENGINE_WIRE_COMPAT", mode)
    buf = _pack(_fixed_leg())
    reader = CanonSwapFixedLegReader.GetRootAs(bytearray(buf), 0)
    decoded = SwapFixedLegT.InitFromObj(reader)  # type: ignore[attr-defined]
    assert decoded.rate == pytest.approx(0.035)
    assert decoded.notional == pytest.approx(1_000_000.0)
    assert decoded.dayCounter == 5
    assert decoded.paymentConvention == 1
    assert decoded.schedule is not None
    assert decoded.schedule.effectiveDate in ("2026-06-15", b"2026-06-15")


@pytest.mark.parametrize("mode", ["0.2", "0.5"])
def test_fixed_rate_bond_object_api_round_trips_per_mode(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("ENGINE_WIRE_COMPAT", mode)
    buf = _pack(_fixed_bond())
    reader = CanonFixedRateBondReader.GetRootAs(bytearray(buf), 0)
    decoded = FixedRateBondT.InitFromObj(reader)  # type: ignore[attr-defined]
    assert decoded.rate == pytest.approx(0.05)
    assert decoded.faceAmount == pytest.approx(100.0)
    assert decoded.settlementDays == 2
    assert decoded.accrualDayCounter == 3
    assert decoded.redemption == pytest.approx(100.0)

    if mode == "0.2":
        legacy_reader = legacy_fixed_rate_bond.FixedRateBond.GetRootAs(bytearray(buf), 0)
        assert legacy_reader.Rate() == pytest.approx(0.05)


def test_legacy_mode_refuses_notionals(monkeypatch: pytest.MonkeyPatch) -> None:
    """``notionals`` does not exist on the 0.2.0 wire — packing must refuse."""

    monkeypatch.setenv("ENGINE_WIRE_COMPAT", "0.2")
    leg = _fixed_leg()
    leg.notionals = [1_000_000.0, 900_000.0]
    with pytest.raises(ValueError, match="notionals"):
        _pack(leg)


def test_canonical_mode_packs_notionals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENGINE_WIRE_COMPAT", "0.5")
    leg = _fixed_leg()
    leg.notionals = [1_000_000.0, 900_000.0]
    buf = _pack(leg)
    reader = CanonSwapFixedLegReader.GetRootAs(bytearray(buf), 0)
    assert reader.NotionalsLength() == 2
    assert reader.Notionals(0) == pytest.approx(1_000_000.0)
    assert reader.Rate() == pytest.approx(0.035)
