"""Vendored engine-0.2.0 FlatBuffers bindings for the four slot-shifted tables.

Engine 0.4.0 inserted ``notionals:[double]`` mid-table into ``SwapFixedLeg`` /
``SwapFloatingLeg`` / ``FixedRateBond`` / ``FloatingRateBond``, shifting every
subsequent field's vtable slot — a silent binary wire break vs the pinned
engine 0.2.0. Engine 0.5.0 additionally REMOVED ``SwapLegFlow.has_cms_swap_rate``
mid-table (#119), shifting ``cms_swap_rate``/``spread``/``rate`` down one slot
on the response side. These five modules are byte-exact copies of the
pre-regen (0.2.0-layout) generated bindings; ``wire_compat`` selects between
them and the canonical ``_generated`` set via ``ENGINE_WIRE_COMPAT``.

Delete this package together with ``wire_compat`` once the compose engine pin
moves to the released 0.5.0.
"""

from quantra_common.engine_client._legacy_v020 import (
    FixedRateBond as fixed_rate_bond,
)
from quantra_common.engine_client._legacy_v020 import (
    FloatingRateBond as floating_rate_bond,
)
from quantra_common.engine_client._legacy_v020 import (
    SwapFixedLeg as swap_fixed_leg,
)
from quantra_common.engine_client._legacy_v020 import (
    SwapFloatingLeg as swap_floating_leg,
)
from quantra_common.engine_client._legacy_v020 import (
    SwapLegFlow as swap_leg_flow,
)

__all__ = [
    "fixed_rate_bond",
    "floating_rate_bond",
    "swap_fixed_leg",
    "swap_floating_leg",
    "swap_leg_flow",
]
