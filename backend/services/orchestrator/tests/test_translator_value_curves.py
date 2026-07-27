"""Unit tests for value-based curve translation (engine 0.5.0 direct-data curves).

``bootstrap_trait`` ``InterpolatedZero`` / ``InterpolatedDiscount`` /
``InterpolatedFwd`` curves carry explicit value points instead of rate
helpers. These tests pin:

* the wire emission per family (union tag, value field, conventions,
  compounding/frequency on zero points);
* the app-level ``quote_id`` resolution through the shared quote map with the
  resolved number emitted inline (invariant #8) and the batched missing-quote
  flow;
* every pre-flight validation that mirrors an engine rule (family coherence,
  date-or-tenor, finiteness, uniform zero conventions, DF range + the
  first-point-1.0-at-reference rule, sorted unique pillars, exactly one of
  inline value | ``quote_id``).
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest

from quantra_common.engine_client._generated.quantra.enums.BootstrapTrait import (
    BootstrapTrait,
)
from quantra_common.engine_client._generated.quantra.enums.BusinessDayConvention import (
    BusinessDayConvention,
)
from quantra_common.engine_client._generated.quantra.enums.Calendar import Calendar
from quantra_common.engine_client._generated.quantra.enums.Compounding import (
    Compounding,
)
from quantra_common.engine_client._generated.quantra.enums.Frequency import Frequency
from quantra_common.engine_client._generated.quantra.Point import Point
from quantra_orchestrator.pricing._translator import (
    CurveTranslationError,
    translate_curve,
)
from quantra_orchestrator.pricing.swap_ir.models import ResolvedCurve

_REF = "2026-05-13"


def _zero_point(
    *,
    tenor: dict[str, Any] | None = None,
    pillar_date: str | None = None,
    zero_rate: float | None = 0.03,
    quote_id: str | None = None,
    compounding: str | None = None,
    frequency: str | None = None,
) -> dict[str, Any]:
    point: dict[str, Any] = {}
    if tenor is not None:
        point["tenor"] = tenor
    if pillar_date is not None:
        point["date"] = pillar_date
    if zero_rate is not None:
        point["zero_rate"] = zero_rate
    if quote_id is not None:
        point["quote_id"] = quote_id
    if compounding is not None:
        point["compounding"] = compounding
    if frequency is not None:
        point["frequency"] = frequency
    return {"point_type": "ZeroRatePoint", "point": point}


def _df_point(*, pillar_date: str, discount_factor: float) -> dict[str, Any]:
    return {
        "point_type": "DiscountFactorPoint",
        "point": {"date": pillar_date, "discount_factor": discount_factor},
    }


def _fwd_point(
    *, tenor: dict[str, Any] | None = None, pillar_date: str | None = None, forward_rate: float
) -> dict[str, Any]:
    point: dict[str, Any] = {"forward_rate": forward_rate}
    if tenor is not None:
        point["tenor"] = tenor
    if pillar_date is not None:
        point["date"] = pillar_date
    return {"point_type": "ForwardRatePoint", "point": point}


def _curve(
    points: list[dict[str, Any]],
    *,
    trait: str,
    name: str = "VALUE-CURVE",
    curve_id: uuid.UUID | None = None,
) -> ResolvedCurve:
    return ResolvedCurve(
        id=curve_id,
        name=name,
        currency="EUR",
        day_counter="Actual365Fixed",
        reference_date=date.fromisoformat(_REF),
        points=points,
        body={"interpolator": "Linear", "bootstrap_trait": trait},
    )


def _translate(
    curve: ResolvedCurve,
    quote_map: dict[str, float] | None = None,
    missing: list[str] | None = None,
) -> Any:
    return translate_curve(
        curve,
        quote_map or {},
        default_reference_date=_REF,
        missing_quotes=missing if missing is not None else [],
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_interpolated_zero_curve_emits_zero_rate_points() -> None:
    ts = _translate(
        _curve(
            [
                _zero_point(pillar_date=_REF, zero_rate=0.02),
                _zero_point(tenor={"n": 1, "unit": "Years"}, zero_rate=0.025),
                _zero_point(tenor={"n": 5, "unit": "Years"}, zero_rate=0.03),
            ],
            trait="InterpolatedZero",
        )
    )
    assert ts.bootstrapTrait == BootstrapTrait.InterpolatedZero
    assert [w.pointType for w in ts.points] == [Point.ZeroRatePoint] * 3
    first, one_y, five_y = (w.point for w in ts.points)
    assert first.date == _REF
    assert first.zeroRate == pytest.approx(0.02)
    assert first.calendar == Calendar.TARGET
    assert first.businessDayConvention == BusinessDayConvention.ModifiedFollowing
    assert first.compounding == Compounding.Continuous
    assert first.frequency == Frequency.Annual
    assert one_y.tenor.n == 1
    assert five_y.zeroRate == pytest.approx(0.03)


def test_interpolated_discount_curve_emits_discount_factor_points() -> None:
    ts = _translate(
        _curve(
            [
                _df_point(pillar_date=_REF, discount_factor=1.0),
                _df_point(pillar_date="2027-05-13", discount_factor=0.97),
                _df_point(pillar_date="2031-05-13", discount_factor=0.86),
            ],
            trait="InterpolatedDiscount",
        )
    )
    assert ts.bootstrapTrait == BootstrapTrait.InterpolatedDiscount
    assert [w.pointType for w in ts.points] == [Point.DiscountFactorPoint] * 3
    assert ts.points[0].point.discountFactor == pytest.approx(1.0)
    assert ts.points[2].point.discountFactor == pytest.approx(0.86)


def test_interpolated_fwd_curve_emits_forward_rate_points_negative_allowed() -> None:
    ts = _translate(
        _curve(
            [
                _fwd_point(pillar_date=_REF, forward_rate=-0.005),
                _fwd_point(tenor={"n": 2, "unit": "Years"}, forward_rate=0.031),
            ],
            trait="InterpolatedFwd",
        )
    )
    assert ts.bootstrapTrait == BootstrapTrait.InterpolatedFwd
    assert [w.pointType for w in ts.points] == [Point.ForwardRatePoint] * 2
    assert ts.points[0].point.forwardRate == pytest.approx(-0.005)


def test_zero_point_quote_id_resolves_inline_via_the_shared_map() -> None:
    missing: list[str] = []
    ts = _translate(
        _curve(
            [
                _zero_point(pillar_date=_REF, zero_rate=0.02),
                _zero_point(
                    tenor={"n": 1, "unit": "Years"}, zero_rate=None, quote_id="EUR.ZERO.1Y"
                ),
            ],
            trait="InterpolatedZero",
        ),
        quote_map={"EUR.ZERO.1Y": 0.0271},
        missing=missing,
    )
    assert missing == []
    resolved = ts.points[1].point
    # Invariant #8: the resolved number rides inline; no quote id on the wire
    # (the wire point types have no quote_id field at all).
    assert resolved.zeroRate == pytest.approx(0.0271)


def test_unresolved_quote_id_lands_in_missing_quotes() -> None:
    missing: list[str] = []
    _translate(
        _curve(
            [
                _zero_point(pillar_date=_REF, zero_rate=0.02),
                _zero_point(tenor={"n": 1, "unit": "Years"}, zero_rate=None, quote_id="EUR.NOPE"),
            ],
            trait="InterpolatedZero",
        ),
        quote_map={},
        missing=missing,
    )
    assert missing == ["EUR.NOPE"]


# ---------------------------------------------------------------------------
# Family coherence
# ---------------------------------------------------------------------------


def test_value_trait_rejects_helper_points() -> None:
    helper = {"point_type": "DepositHelper", "point": {"tenor": {"n": 1, "unit": "Years"}}}
    with pytest.raises(CurveTranslationError, match="cannot mix point families"):
        _translate(_curve([helper], trait="InterpolatedZero"))


def test_value_trait_rejects_the_wrong_value_family() -> None:
    with pytest.raises(CurveTranslationError, match="InterpolatedZero builds from ZeroRatePoint"):
        _translate(
            _curve([_df_point(pillar_date=_REF, discount_factor=1.0)], trait="InterpolatedZero")
        )


def test_helper_trait_rejects_value_points() -> None:
    with pytest.raises(CurveTranslationError, match="builds from rate helpers"):
        _translate(
            _curve([_zero_point(pillar_date=_REF, zero_rate=0.02)], trait="Discount"),
        )


def test_value_trait_rejects_empty_points() -> None:
    with pytest.raises(CurveTranslationError, match="at least one"):
        _translate(_curve([], trait="InterpolatedFwd"))


# ---------------------------------------------------------------------------
# Per-point validation
# ---------------------------------------------------------------------------


def test_exactly_one_of_value_or_quote_id() -> None:
    with pytest.raises(CurveTranslationError, match="exactly one value source"):
        _translate(
            _curve(
                [_zero_point(pillar_date=_REF, zero_rate=0.02, quote_id="EUR.ZERO.0D")],
                trait="InterpolatedZero",
            ),
            quote_map={"EUR.ZERO.0D": 0.02},
        )


def test_point_without_any_value_source_rejects() -> None:
    with pytest.raises(CurveTranslationError, match="neither"):
        _translate(
            _curve([_zero_point(pillar_date=_REF, zero_rate=None)], trait="InterpolatedZero")
        )


def test_point_without_date_or_tenor_rejects() -> None:
    with pytest.raises(CurveTranslationError, match="``date`` or a positive ``tenor``"):
        _translate(_curve([_zero_point(zero_rate=0.02)], trait="InterpolatedZero"))


def test_non_finite_value_rejects() -> None:
    with pytest.raises(CurveTranslationError, match="finite"):
        _translate(
            _curve(
                [_zero_point(pillar_date=_REF, zero_rate=float("nan"))], trait="InterpolatedZero"
            )
        )


def test_mixed_zero_conventions_reject() -> None:
    with pytest.raises(CurveTranslationError, match="identical"):
        _translate(
            _curve(
                [
                    _zero_point(pillar_date=_REF, zero_rate=0.02, compounding="Continuous"),
                    _zero_point(
                        tenor={"n": 1, "unit": "Years"}, zero_rate=0.025, compounding="Simple"
                    ),
                ],
                trait="InterpolatedZero",
            )
        )


def test_discount_factor_out_of_range_rejects() -> None:
    with pytest.raises(CurveTranslationError, match=r"in \(0, 1\]"):
        _translate(
            _curve(
                [
                    _df_point(pillar_date=_REF, discount_factor=1.0),
                    _df_point(pillar_date="2027-05-13", discount_factor=1.02),
                ],
                trait="InterpolatedDiscount",
            )
        )


def test_first_discount_factor_must_be_exactly_one() -> None:
    with pytest.raises(CurveTranslationError, match=r"exactly 1\.0"):
        _translate(
            _curve(
                [_df_point(pillar_date=_REF, discount_factor=0.999)],
                trait="InterpolatedDiscount",
            )
        )


def test_first_discount_point_must_sit_at_the_reference_date() -> None:
    with pytest.raises(CurveTranslationError, match="reference date"):
        _translate(
            _curve(
                [_df_point(pillar_date="2026-06-13", discount_factor=1.0)],
                trait="InterpolatedDiscount",
            )
        )


def test_unsorted_pillars_reject() -> None:
    with pytest.raises(CurveTranslationError, match="strictly increasing"):
        _translate(
            _curve(
                [
                    _zero_point(pillar_date=_REF, zero_rate=0.02),
                    _zero_point(tenor={"n": 5, "unit": "Years"}, zero_rate=0.03),
                    _zero_point(tenor={"n": 1, "unit": "Years"}, zero_rate=0.025),
                ],
                trait="InterpolatedZero",
            )
        )


def test_duplicate_pillars_reject() -> None:
    with pytest.raises(CurveTranslationError, match="strictly increasing"):
        _translate(
            _curve(
                [
                    _zero_point(pillar_date=_REF, zero_rate=0.02),
                    _zero_point(tenor={"n": 12, "unit": "Months"}, zero_rate=0.025),
                    _zero_point(tenor={"n": 1, "unit": "Years"}, zero_rate=0.026),
                ],
                trait="InterpolatedZero",
            )
        )


def test_invalid_date_rejects() -> None:
    with pytest.raises(CurveTranslationError, match="invalid"):
        _translate(
            _curve(
                [_zero_point(pillar_date="not-a-date", zero_rate=0.02)],
                trait="InterpolatedZero",
            )
        )
