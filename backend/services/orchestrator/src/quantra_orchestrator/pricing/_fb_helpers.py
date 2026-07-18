"""Shared FlatBuffers building blocks for pricing request assembly.

Historically this module carried the full ``build_canonical_pricing``
fixture subtree (a hardcoded flat-3 % canonical ``Pricing`` graph used by
the Phase-3 smoke path). Production request assembly moved onto
:mod:`quantra_orchestrator.pricing._translator`, which
builds the ``Pricing`` graph faithfully from the caller's resolved curves
and quotes; the unused canonical-fixture builders have been deleted.

What remains is the small set of live helpers the translator and the
per-product ``engine_io`` modules share:

* :func:`make_period` / :func:`build_index_ref` — generic FB plumbing.
* :func:`build_canonical_yield` — the yield-convention block bond pricing
  reports duration / convexity / yield in.
* :func:`build_canonical_coupon_pricer` — the zero-vol Black-Ibor coupon
  pricer floating-rate bonds reference.
* :func:`build_canonical_cds_model` / :func:`build_canonical_equity_model`
  — the default model specs the translator attaches when the caller does
  not supply one.
"""

from __future__ import annotations

from quantra_common.engine_client._generated.quantra.BlackIborCouponPricer import (
    BlackIborCouponPricerT,
)
from quantra_common.engine_client._generated.quantra.CdsModelSpec import CdsModelSpecT
from quantra_common.engine_client._generated.quantra.ConstantOptionletVolatility import (
    ConstantOptionletVolatilityT,
)
from quantra_common.engine_client._generated.quantra.CouponPricer import CouponPricerT
from quantra_common.engine_client._generated.quantra.enums.BusinessDayConvention import (
    BusinessDayConvention,
)
from quantra_common.engine_client._generated.quantra.enums.Calendar import Calendar
from quantra_common.engine_client._generated.quantra.enums.CdsEngineType import (
    CdsEngineType,
)
from quantra_common.engine_client._generated.quantra.enums.Compounding import (
    Compounding,
)
from quantra_common.engine_client._generated.quantra.enums.DayCounter import DayCounter
from quantra_common.engine_client._generated.quantra.enums.EquityModelType import (
    EquityModelType,
)
from quantra_common.engine_client._generated.quantra.enums.Frequency import Frequency
from quantra_common.engine_client._generated.quantra.EquityVanillaModelSpec import (
    EquityVanillaModelSpecT,
)
from quantra_common.engine_client._generated.quantra.IndexRef import IndexRefT
from quantra_common.engine_client._generated.quantra.ModelPayload import ModelPayload
from quantra_common.engine_client._generated.quantra.ModelSpec import ModelSpecT
from quantra_common.engine_client._generated.quantra.Period import PeriodT
from quantra_common.engine_client._generated.quantra.Yield import YieldT

CANONICAL_COUPON_PRICER_ID = "iborpricer"
"""Coupon-pricer id floating-rate bonds reference."""

DEFAULT_CDS_MODEL_ID = "cds_midpoint"
"""ModelSpec id every CDS request references in ``PriceCDS.model``."""

DEFAULT_EQUITY_MODEL_ID = "equity_model"
"""``ModelSpec.id`` every equity option references in
``PriceEquityOption.model``."""

DEFAULT_EQUITY_BINOMIAL_STEPS = 500
"""Fallback binomial-tree step count for the default equity model
(matches the engine's :class:`EquityVanillaModelSpec` default)."""


def make_period(n: int, unit: int) -> PeriodT:
    """Construct a :class:`PeriodT`. Used by every helper / leg builder."""

    p = PeriodT()
    p.n = n
    p.unit = unit
    return p


def build_index_ref(index_id: str) -> IndexRefT:
    """Return an ``IndexRef`` pointing at ``index_id``."""

    ref = IndexRefT()
    ref.id = index_id
    return ref


def build_canonical_coupon_pricer(
    pricer_id: str = CANONICAL_COUPON_PRICER_ID,
) -> CouponPricerT:
    """Build a Black-Ibor coupon pricer with a zero-vol optionlet structure.

    Floating-rate bond pricing requires a coupon pricer reference; a
    zero-vol pricer is the default attached when the caller does not
    supply a vol surface to derive one from.
    """

    optionlet = ConstantOptionletVolatilityT()
    optionlet.settlementDays = 2
    optionlet.calendar = Calendar.TARGET
    optionlet.businessDayConvention = BusinessDayConvention.ModifiedFollowing
    optionlet.volatility = 0.0
    optionlet.dayCounter = DayCounter.Actual365Fixed

    black_pricer = BlackIborCouponPricerT()
    black_pricer.optionletVolatility = optionlet

    pricer = CouponPricerT()
    pricer.id = pricer_id
    pricer.blackIborCouponPricer = black_pricer
    return pricer


def build_canonical_cds_model(
    model_id: str = DEFAULT_CDS_MODEL_ID,
    *,
    engine_type: int = CdsEngineType.MidPoint,
) -> ModelSpecT:
    """Build a CDS :class:`ModelSpec` (defaults to the MidPoint engine).

    Wraps a :class:`CdsModelSpecT` inside the polymorphic
    :class:`ModelSpec` carrier the engine expects under
    ``Pricing.volatility.models``. The MidPoint engine is the canonical
    default in the engine's reference fixture; the ISDA engine is also
    available via ``CdsEngineType.ISDA``.
    """

    cds_model = CdsModelSpecT()
    cds_model.engineType = engine_type

    spec = ModelSpecT()
    spec.id = model_id
    spec.payloadType = ModelPayload.CdsModelSpec
    spec.payload = cds_model
    return spec


def build_canonical_equity_model(
    model_id: str = DEFAULT_EQUITY_MODEL_ID,
    *,
    model_type: int = EquityModelType.BlackScholesAnalytic,
    binomial_steps: int = DEFAULT_EQUITY_BINOMIAL_STEPS,
) -> ModelSpecT:
    """Build the default equity vanilla pricing :class:`ModelSpec`.

    Defaults to the analytic Black-Scholes engine (matches the engine's
    reference fixture). ``BinomialCRR`` is reachable via the
    ``model_type`` arg for callers that want the tree path.
    """

    eq_model = EquityVanillaModelSpecT()
    eq_model.modelType = model_type
    eq_model.binomialSteps = binomial_steps

    spec = ModelSpecT()
    spec.id = model_id
    spec.payloadType = ModelPayload.EquityVanillaModelSpec
    spec.payload = eq_model
    return spec


def build_canonical_yield(
    *,
    day_counter: int = DayCounter.Actual360,
    compounding: int = Compounding.Compounded,
    frequency: int = Frequency.Annual,
) -> YieldT:
    """Build a :class:`YieldT` block for fixed/floating bond pricing.

    Required by ``PriceFixedRateBond`` / ``PriceFloatingRateBond``
    so the engine knows which yield convention to report
    duration / convexity / yield in.
    """

    spec = YieldT()
    spec.dayCounter = day_counter
    spec.compounding = compounding
    spec.frequency = frequency
    return spec


__all__ = [
    "CANONICAL_COUPON_PRICER_ID",
    "DEFAULT_CDS_MODEL_ID",
    "DEFAULT_EQUITY_MODEL_ID",
    "build_canonical_cds_model",
    "build_canonical_coupon_pricer",
    "build_canonical_equity_model",
    "build_canonical_yield",
    "build_index_ref",
    "make_period",
]
