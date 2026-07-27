"""Engine wire-format compatibility seam for the 0.2.0 → 0.5.0 transition.

Engine 0.4.0 inserted an optional ``notionals:[double]`` field MID-TABLE into
four FlatBuffers tables (``SwapFixedLeg`` / ``SwapFloatingLeg`` /
``FixedRateBond`` / ``FloatingRateBond``) without explicit field ids, which
SHIFTED every subsequent field's vtable slot. That makes the binary gRPC wire
for those four tables mutually incompatible between engine 0.2.0 (the pinned
release) and engine master/0.5.0: bytes packed with one layout are silently
misread by the other (e.g. ``SwapFixedLeg.rate`` lands in the slot the other
side reads as ``notionals`` → the rate decodes as absent → a fixed leg priced
at 0%). Engine 0.5.0 (#119) additionally REMOVED
``SwapLegFlow.has_cms_swap_rate`` mid-table on the RESPONSE side, shifting
``cms_swap_rate``/``spread``/``rate`` from slots 11/12/13 to 10/11/12 — a
0.2.0 engine's flow bytes decoded with canonical bindings would silently
report the coupon's CMS fixing as its spread and its spread as its rate.
Every other table in the schema is layout-stable — verified by an exhaustive
three-way slot-id diff of all 177 v0.2.0 tables (v0.2.0 / f2360cd / v0.5.0)
at regen time.

This module is the single seam that picks the layout:

* ``ENGINE_WIRE_COMPAT=0.2`` (the DEFAULT — matches the compose engine pin
  ``quantra-server:0.2.0``): the five tables pack/unpack with the legacy
  0.2.0 slot layout vendored under ``_legacy_v020/``.
* ``ENGINE_WIRE_COMPAT=0.5``: the five tables use the canonical regenerated
  v0.5.0 bindings unchanged.

Everything outside these five tables always uses the canonical bindings.

The seam is DELETABLE: when the bundle's engine pin moves to the released
0.5.0, flip the default (or drop the env var), delete ``_legacy_v020/`` and
this module, and point the four imports back at ``_generated``.

Wiring:

* The product ``engine_io`` modules import the four ``*T`` classes from HERE
  instead of ``_generated`` (encode path).
* The regen script rewrites the generated bindings' deferred ``_UnPack``
  imports of the four names to point HERE (decode path — response/trace
  decoding of packed requests must read the same layout it wrote).

An unknown ``ENGINE_WIRE_COMPAT`` value raises immediately (loud beats a
silently mispriced swap).
"""

from __future__ import annotations

import os
from typing import Any

from quantra_common.engine_client import _legacy_v020
from quantra_common.engine_client._generated.quantra import (
    FixedRateBond as _canon_fixed_bond,
)
from quantra_common.engine_client._generated.quantra import (
    FloatingRateBond as _canon_float_bond,
)
from quantra_common.engine_client._generated.quantra import (
    SwapFixedLeg as _canon_fixed_leg,
)
from quantra_common.engine_client._generated.quantra import (
    SwapFloatingLeg as _canon_float_leg,
)
from quantra_common.engine_client._generated.quantra import (
    SwapLegFlow as _canon_leg_flow,
)

ENGINE_WIRE_COMPAT_ENV = "ENGINE_WIRE_COMPAT"

_LEGACY_VALUES = frozenset({"0.2", "0.2.0"})
_CANONICAL_VALUES = frozenset({"0.5", "0.5.0", "master"})

#: Attribute -> legacy scalar default per table (the old bindings initialised
#: scalars to these; the canonical bindings initialise presence-based fields
#: to ``None``, which the legacy ``Pack`` cannot serialise).
_LEGACY_SCALAR_DEFAULTS: dict[str, dict[str, Any]] = {
    "SwapFixedLeg": {"notional": 0.0, "rate": 0.0, "dayCounter": 0, "paymentConvention": 0},
    "SwapFloatingLeg": {
        "notional": 0.0,
        "spread": 0.0,
        "dayCounter": 0,
        "paymentConvention": 0,
        "fixingDays": 0,
        "inArrears": False,
    },
    "FixedRateBond": {
        "settlementDays": 0,
        "faceAmount": 0.0,
        "rate": 0.0,
        "accrualDayCounter": 0,
        "paymentConvention": 0,
        "redemption": 0.0,
    },
    "FloatingRateBond": {
        "settlementDays": 0,
        "faceAmount": 0.0,
        "accrualDayCounter": 0,
        "paymentConvention": 0,
        "fixingDays": 0,
        "inArrears": False,
        "spread": 0.0,
        "redemption": 0.0,
    },
    # Response-side (decode is the real consumer; Pack kept coherent for
    # completeness). ``hasCmsSwapRate`` exists only on the legacy layout —
    # the coercion loop materialises it on the canonical-shaped instance
    # before the legacy Pack reads it.
    "SwapLegFlow": {
        "amount": 0.0,
        "accrualYearFraction": 0.0,
        "gearing": 1.0,
        "discount": 0.0,
        "presentValue": 0.0,
        "indexFixing": 0.0,
        "hasCmsSwapRate": False,
        "cmsSwapRate": 0.0,
        "spread": 0.0,
        "rate": 0.0,
    },
}


def engine_wire_target() -> str:
    """The engine wire layout in force (env ``ENGINE_WIRE_COMPAT``, default 0.2)."""

    raw = os.environ.get(ENGINE_WIRE_COMPAT_ENV, "0.2").strip() or "0.2"
    if raw in _LEGACY_VALUES:
        return "0.2"
    if raw in _CANONICAL_VALUES:
        return "0.5"
    raise ValueError(
        f"Unknown {ENGINE_WIRE_COMPAT_ENV} value {raw!r}: expected one of "
        f"{sorted(_LEGACY_VALUES | _CANONICAL_VALUES)}. Refusing to guess a "
        "wire layout — a wrong pick silently mis-prices swaps and bonds."
    )


def _use_legacy() -> bool:
    return engine_wire_target() == "0.2"


def _make_compat(
    table_name: str,
    canonical_mod: Any,  # noqa: ANN401 -- generated module, no typing surface
    legacy_mod: Any,  # noqa: ANN401 -- generated module, no typing surface
) -> type:
    """Build the mode-dispatching ``<Table>T`` subclass for one shifted table."""

    canonical_t = getattr(canonical_mod, table_name + "T")
    legacy_t = getattr(legacy_mod, table_name + "T")
    legacy_reader = getattr(legacy_mod, table_name)
    defaults = _LEGACY_SCALAR_DEFAULTS[table_name]

    class _CompatT(canonical_t):  # type: ignore[misc,valid-type]
        def Pack(self, builder: Any) -> Any:  # noqa: ANN401 -- FB object API
            if not _use_legacy():
                return super().Pack(builder)
            if getattr(self, "notionals", None):
                raise ValueError(
                    f"{table_name}.notionals is not representable on the engine "
                    "0.2.0 wire (the field does not exist there); unset it or "
                    f"target a 0.5.0 engine via {ENGINE_WIRE_COMPAT_ENV}=0.5."
                )
            # The legacy Pack cannot serialise the canonical bindings'
            # ``None`` presence markers; coerce them to the legacy scalar
            # defaults (which engine 0.2.0 reads for an absent field anyway).
            for attr, default in defaults.items():
                if getattr(self, attr, None) is None:
                    setattr(self, attr, default)
            return legacy_t.Pack(self, builder)

        @classmethod
        def InitFromObj(cls, table: Any) -> Any:  # noqa: ANN401 -- FB object API
            x = cls()
            if table is None:
                return x
            if _use_legacy():
                # Re-read the same table position through the legacy reader so
                # the fields come off the 0.2.0 slot layout they were packed
                # with, then carry them onto the canonical-shaped instance.
                reader = legacy_reader()
                reader.Init(table._tab.Bytes, table._tab.Pos)
                legacy = legacy_t.InitFromObj(reader)
                x.__dict__.update(legacy.__dict__)
                return x
            x._UnPack(table)
            return x

        @classmethod
        def InitFromBuf(cls, buf: Any, pos: Any) -> Any:  # noqa: ANN401 -- FB object API
            reader = getattr(canonical_mod, table_name)()
            reader.Init(buf, pos)
            return cls.InitFromObj(reader)

    _CompatT.__name__ = table_name + "T"
    _CompatT.__qualname__ = table_name + "T"
    return _CompatT


SwapFixedLegT = _make_compat("SwapFixedLeg", _canon_fixed_leg, _legacy_v020.swap_fixed_leg)
SwapFloatingLegT = _make_compat("SwapFloatingLeg", _canon_float_leg, _legacy_v020.swap_floating_leg)
FixedRateBondT = _make_compat("FixedRateBond", _canon_fixed_bond, _legacy_v020.fixed_rate_bond)
FloatingRateBondT = _make_compat(
    "FloatingRateBond", _canon_float_bond, _legacy_v020.floating_rate_bond
)
SwapLegFlowT = _make_compat("SwapLegFlow", _canon_leg_flow, _legacy_v020.swap_leg_flow)


def SwapLegFlow() -> Any:  # noqa: ANN401 -- FB reader factory, generated call shape
    """Layout-dispatching READER for the slot-shifted ``SwapLegFlow`` table.

    Response modules instantiate the flow reader directly
    (``obj = SwapLegFlow(); obj.Init(...)``) in their vector accessors, so the
    decode path needs a mode-aware reader, not just a mode-aware ``*T`` class.
    The regen script redirects those reader imports here.
    """

    if _use_legacy():
        return _legacy_v020.swap_leg_flow.SwapLegFlow()
    return _canon_leg_flow.SwapLegFlow()


__all__ = [
    "ENGINE_WIRE_COMPAT_ENV",
    "FixedRateBondT",
    "FloatingRateBondT",
    "SwapFixedLegT",
    "SwapFloatingLegT",
    "SwapLegFlow",
    "SwapLegFlowT",
    "engine_wire_target",
]
