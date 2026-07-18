"""Unit tests for ``pricing/cds/assembler.py``.

The assembler is the first to walk two distinct entity families in
one resolution pass (``app.curves`` for the discount curve,
``app.credit_curves`` for the credit curve). These tests pin the
contracts the 09-plan calls out:

* Inline mode (``cds=...``) skips ``app.cds`` entirely.
* By-reference mode (``cds_id=...``) hits ``app.cds`` once with the
  right WHERE clause (owner_uid + deleted_at IS NULL).
* Soft-deleted / cross-tenant / missing CDS rows surface as
  ``cds_not_found`` (404).
* ``request.curves`` / ``request.credit_curve_id`` /
  ``request.credit_curve`` overrides win over the saved CDS's
  pricing block.
* Discount-curve fallback chain (request → saved
  ``pricing.discount_curve_id`` → saved ``pricing.curves[0]``).
* Credit-curve fallback chain (request id → request inline →
  saved ``pricing.credit_curve_id``).
* Per-bundle-type 404 codes: ``cds_discount_curve_not_found`` vs
  ``cds_credit_curve_not_found`` (refinement — distinct
  entity families get distinct codes).
* Per-bundle-type 422 codes: ``cds_curve_resolution_failed`` (rates)
  vs ``cds_credit_curve_resolution_failed`` (credit), the latter
  for vendor-quote-ref bodies / missing hazard inputs / unsupported
  source markers.
* ``snapshot_id`` loads ``app.snapshots`` and projects into the
  snapshot's ``pins`` map; missing snapshots surface as
  ``cds_snapshot_not_found`` (404).
* batching hook: assembler output carries ``credit_curve_id`` /
  ``discount_curve_id`` so the route handler can pack them into
  ``shared_inputs``.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_orchestrator.pricing.cds.assembler import (
    CdsAssemblerOutput,
    assemble,
)
from quantra_orchestrator.pricing.cds.errors import (
    CdsCreditCurveNotFoundError,
    CdsCreditCurveResolutionFailedError,
    CdsCurveResolutionFailedError,
    CdsDiscountCurveNotFoundError,
    CdsNotFoundError,
    CdsSnapshotNotFoundError,
)
from quantra_orchestrator.pricing.cds.models import (
    CdsPriceRequest,
    CreditCurveRef,
    CurveRef,
)

from .conftest import FakeEngine

OWNER = "user-test"


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------


def _curve_row(
    *,
    curve_id: uuid.UUID,
    name: str = "USD-OIS",
    quote_id: str = "USD.IRS.1Y",
) -> dict[str, Any]:
    return {
        "id": str(curve_id),
        "name": name,
        "currency": "USD",
        "day_counter": "Actual/360",
        "helper_kind": "Discount",
        "reference_date": date(2026, 5, 13),
        "points": [{"tenor": "1Y", "quote_id": quote_id}],
        "body": {"interpolator": "Cubic"},
    }


def _credit_curve_row(
    *,
    credit_curve_id: uuid.UUID,
    name: str = "ACME-SR",
    source: str = "flat",
    recovery_rate: float = 0.4,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": str(credit_curve_id),
        "name": name,
        "reference_entity": "ACME",
        "currency": "USD",
        "seniority": "Senior Unsecured",
        "source": source,
        "recovery_rate": recovery_rate,
        "body": body if body is not None else {"flat_hazard_rate": 0.02},
    }


def _cds_row(
    *,
    cds_id: uuid.UUID,
    request_json: dict[str, Any],
    name: str = "demo-cds",
) -> dict[str, Any]:
    return {"id": str(cds_id), "name": name, "request": request_json}


def _snapshot_row(*, snapshot_id: uuid.UUID, content: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(snapshot_id), "name": "EOD-USD", "content": content}


def _ro(engine: FakeEngine) -> AsyncEngine:
    return cast(AsyncEngine, engine)


# ---------------------------------------------------------------------------
# Inline + by-reference: trade loading
# ---------------------------------------------------------------------------


async def test_inline_does_not_touch_cds_table(
    fake_ro_engine: FakeEngine,
) -> None:
    """Inline mode (``cds=...``) never reads ``app.cds`` — only the curves."""

    curve_id = uuid.uuid4()
    credit_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.credit_curves" in sql:
            return [_credit_curve_row(credit_curve_id=credit_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    request = CdsPriceRequest(
        cds_id=None,
        cds={"notional": 10_000_000.0},
        curves=[CurveRef(id=curve_id)],
        credit_curve_id=credit_id,
        as_of=date(2026, 5, 13),
    )
    out = await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))

    assert isinstance(out, CdsAssemblerOutput)
    assert out.trade.cds_id is None
    assert out.trade.cds == {"notional": 10_000_000.0}
    assert out.discount_curve.id == curve_id
    assert out.credit_curve.id == credit_id
    assert out.discount_curve_id == curve_id
    assert out.credit_curve_id == credit_id
    assert out.snapshot is None
    assert all("FROM app.cds" not in rec.sql for rec in fake_ro_engine.recordings)


async def test_by_reference_loads_cds_and_both_curves(
    fake_ro_engine: FakeEngine,
) -> None:
    cds_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    credit_id = uuid.uuid4()
    cds = _cds_row(
        cds_id=cds_id,
        request_json={
            "pricing": {
                "discount_curve_id": str(curve_id),
                "credit_curve_id": str(credit_id),
            }
        },
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.cds" in sql:
            return [cds]
        if "FROM app.credit_curves" in sql:
            return [_credit_curve_row(credit_curve_id=credit_id)]
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    out = await assemble(
        CdsPriceRequest(cds_id=cds_id, as_of=date(2026, 5, 13)),
        owner_uid=OWNER,
        ro_engine=_ro(fake_ro_engine),
    )

    assert out.trade.cds_id == cds_id
    assert out.discount_curve.id == curve_id
    assert out.credit_curve.id == credit_id
    assert out.discount_curve_id == curve_id
    assert out.credit_curve_id == credit_id
    cds_sqls = [rec for rec in fake_ro_engine.recordings if "FROM app.cds" in rec.sql]
    assert len(cds_sqls) == 1
    assert "owner_uid" in cds_sqls[0].sql
    assert "deleted_at IS NULL" in cds_sqls[0].sql
    assert cds_sqls[0].params["owner_uid"] == OWNER


async def test_cds_id_missing_returns_cds_not_found(
    fake_ro_engine: FakeEngine,
) -> None:
    fake_ro_engine.set_handler(lambda _sql, _params: [])
    request = CdsPriceRequest(cds_id=uuid.uuid4(), as_of=date(2026, 5, 13))

    with pytest.raises(CdsNotFoundError) as excinfo:
        await assemble(request, owner_uid=OWNER, ro_engine=_ro(fake_ro_engine))
    assert excinfo.value.code == "cds_not_found"
    assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# Discount-curve resolution (rates family)
# ---------------------------------------------------------------------------


async def test_discount_curve_missing_returns_404(
    fake_ro_engine: FakeEngine,
) -> None:
    cds_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    credit_id = uuid.uuid4()
    cds = _cds_row(
        cds_id=cds_id,
        request_json={
            "pricing": {
                "discount_curve_id": str(curve_id),
                "credit_curve_id": str(credit_id),
            }
        },
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.cds" in sql:
            return [cds]
        if "FROM app.curves" in sql:
            return []
        if "FROM app.credit_curves" in sql:
            return [_credit_curve_row(credit_curve_id=credit_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    with pytest.raises(CdsDiscountCurveNotFoundError) as excinfo:
        await assemble(
            CdsPriceRequest(cds_id=cds_id, as_of=date(2026, 5, 13)),
            owner_uid=OWNER,
            ro_engine=_ro(fake_ro_engine),
        )
    assert excinfo.value.code == "cds_discount_curve_not_found"


async def test_missing_pricing_block_raises_curve_resolution_failed(
    fake_ro_engine: FakeEngine,
) -> None:
    """No pricing block → 422 ``cds_curve_resolution_failed`` (rates stage)."""

    cds_id = uuid.uuid4()
    cds = _cds_row(cds_id=cds_id, request_json={"notional": 10_000_000.0})
    fake_ro_engine.set_handler(lambda _sql, _params: [cds])

    with pytest.raises(CdsCurveResolutionFailedError) as excinfo:
        await assemble(
            CdsPriceRequest(cds_id=cds_id, as_of=date(2026, 5, 13)),
            owner_uid=OWNER,
            ro_engine=_ro(fake_ro_engine),
        )
    assert excinfo.value.code == "cds_curve_resolution_failed"
    assert excinfo.value.status_code == 422


async def test_missing_discount_curve_id_raises_curve_resolution_failed(
    fake_ro_engine: FakeEngine,
) -> None:
    """Pricing block without discount curve id / curves → 422 ``cds_curve_resolution_failed``."""

    cds_id = uuid.uuid4()
    credit_id = uuid.uuid4()
    cds = _cds_row(
        cds_id=cds_id,
        request_json={"pricing": {"credit_curve_id": str(credit_id)}},
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.cds" in sql:
            return [cds]
        if "FROM app.credit_curves" in sql:
            return [_credit_curve_row(credit_curve_id=credit_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    with pytest.raises(CdsCurveResolutionFailedError) as excinfo:
        await assemble(
            CdsPriceRequest(cds_id=cds_id, as_of=date(2026, 5, 13)),
            owner_uid=OWNER,
            ro_engine=_ro(fake_ro_engine),
        )
    assert excinfo.value.code == "cds_curve_resolution_failed"


async def test_pricing_curves_fallback_is_consumed(
    fake_ro_engine: FakeEngine,
) -> None:
    """Saved-CDS ``pricing.curves[0]`` is the final fallback for the discount curve."""

    cds_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    credit_id = uuid.uuid4()
    cds = _cds_row(
        cds_id=cds_id,
        request_json={
            "pricing": {
                "credit_curve_id": str(credit_id),
                "curves": [{"id": str(curve_id)}],
            }
        },
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.cds" in sql:
            return [cds]
        if "FROM app.credit_curves" in sql:
            return [_credit_curve_row(credit_curve_id=credit_id)]
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    out = await assemble(
        CdsPriceRequest(cds_id=cds_id, as_of=date(2026, 5, 13)),
        owner_uid=OWNER,
        ro_engine=_ro(fake_ro_engine),
    )
    assert out.discount_curve.id == curve_id
    assert out.discount_curve_id == curve_id


# ---------------------------------------------------------------------------
# Credit-curve resolution (credit family)
# ---------------------------------------------------------------------------


async def test_credit_curve_missing_returns_404(
    fake_ro_engine: FakeEngine,
) -> None:
    """Missing credit curve → ``cds_credit_curve_not_found`` (distinct from discount 404).

    the error-code convention refinement: credit curves and discount curves are different
    entity families so the 404 codes are intentionally distinct
    rather than collapsed under a ``role`` marker.
    """

    cds_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    credit_id = uuid.uuid4()
    cds = _cds_row(
        cds_id=cds_id,
        request_json={
            "pricing": {
                "discount_curve_id": str(curve_id),
                "credit_curve_id": str(credit_id),
            }
        },
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.cds" in sql:
            return [cds]
        if "FROM app.credit_curves" in sql:
            return []
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    with pytest.raises(CdsCreditCurveNotFoundError) as excinfo:
        await assemble(
            CdsPriceRequest(cds_id=cds_id, as_of=date(2026, 5, 13)),
            owner_uid=OWNER,
            ro_engine=_ro(fake_ro_engine),
        )
    assert excinfo.value.code == "cds_credit_curve_not_found"
    assert excinfo.value.status_code == 404


async def test_request_credit_curve_id_override_wins(
    fake_ro_engine: FakeEngine,
) -> None:
    """``request.credit_curve_id`` overrides the saved CDS's pricing block."""

    cds_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    saved_credit_id = uuid.uuid4()
    override_credit_id = uuid.uuid4()
    cds = _cds_row(
        cds_id=cds_id,
        request_json={
            "pricing": {
                "discount_curve_id": str(curve_id),
                "credit_curve_id": str(saved_credit_id),
            }
        },
    )

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.cds" in sql:
            return [cds]
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.credit_curves" in sql:
            # Only the override id should ever be queried.
            assert params["credit_curve_id"] == str(override_credit_id)
            return [_credit_curve_row(credit_curve_id=override_credit_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    out = await assemble(
        CdsPriceRequest(
            cds_id=cds_id,
            credit_curve_id=override_credit_id,
            as_of=date(2026, 5, 13),
        ),
        owner_uid=OWNER,
        ro_engine=_ro(fake_ro_engine),
    )
    assert out.credit_curve.id == override_credit_id
    assert out.credit_curve_id == override_credit_id


async def test_inline_credit_curve_override_does_not_hit_db(
    fake_ro_engine: FakeEngine,
) -> None:
    """``request.credit_curve`` inline override skips ``app.credit_curves``."""

    curve_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    out = await assemble(
        CdsPriceRequest(
            cds_id=None,
            cds={"notional": 10_000_000.0},
            curves=[CurveRef(id=curve_id)],
            credit_curve=CreditCurveRef(
                name="ACME inline",
                recovery_rate=0.4,
                source="flat",
                body={"flat_hazard_rate": 0.025},
            ),
            as_of=date(2026, 5, 13),
        ),
        owner_uid=OWNER,
        ro_engine=_ro(fake_ro_engine),
    )
    assert out.credit_curve.name == "ACME inline"
    assert out.credit_curve.id is None
    assert out.credit_curve_id is None
    assert all("FROM app.credit_curves" not in rec.sql for rec in fake_ro_engine.recordings)


async def test_credit_curve_with_quote_book_source_raises_422(
    fake_ro_engine: FakeEngine,
) -> None:
    """``source="quote_book"`` rejected — would require MD walker extension.

    Per the plan: "credit curves carry their own hazard/probability
    inputs (not vendor quotes)". The orchestrator's MD walker is
    discount-curve only; a ``quote_book``-sourced credit curve would
    need an MD walker extension that's an explicit scope-expansion
    decision (escalate before extending). The assembler raises the
    per-bundle-stage 422 here rather than silently bypassing the
    constraint.
    """

    cds_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    credit_id = uuid.uuid4()
    cds = _cds_row(
        cds_id=cds_id,
        request_json={
            "pricing": {
                "discount_curve_id": str(curve_id),
                "credit_curve_id": str(credit_id),
            }
        },
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.cds" in sql:
            return [cds]
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.credit_curves" in sql:
            return [
                _credit_curve_row(
                    credit_curve_id=credit_id,
                    source="quote_book",
                    body={"points": [{"quote_id": "ACME.5Y", "tenor": "5Y"}]},
                )
            ]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    with pytest.raises(CdsCreditCurveResolutionFailedError) as excinfo:
        await assemble(
            CdsPriceRequest(cds_id=cds_id, as_of=date(2026, 5, 13)),
            owner_uid=OWNER,
            ro_engine=_ro(fake_ro_engine),
        )
    assert excinfo.value.code == "cds_credit_curve_resolution_failed"
    assert excinfo.value.status_code == 422


async def test_credit_curve_with_inline_quote_id_raises_422(
    fake_ro_engine: FakeEngine,
) -> None:
    """A ``points[i].quote_id`` reference (even on a non-``quote_book`` source) is rejected.

    The plan defers MD resolution for credit curves; any quote_id
    reference in the body would require the walker extension that's
    out of scope. The 422 surfaces the offending index so the
    operator can see exactly which point needs an inline value.
    """

    cds_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    credit_id = uuid.uuid4()
    cds = _cds_row(
        cds_id=cds_id,
        request_json={
            "pricing": {
                "discount_curve_id": str(curve_id),
                "credit_curve_id": str(credit_id),
            }
        },
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.cds" in sql:
            return [cds]
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.credit_curves" in sql:
            return [
                _credit_curve_row(
                    credit_curve_id=credit_id,
                    source="manual",
                    body={
                        "points": [
                            {
                                "quote_id": "ACME.5Y",
                                "tenor": "5Y",
                                "quote_type": "ParSpread",
                            }
                        ]
                    },
                )
            ]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    with pytest.raises(CdsCreditCurveResolutionFailedError) as excinfo:
        await assemble(
            CdsPriceRequest(cds_id=cds_id, as_of=date(2026, 5, 13)),
            owner_uid=OWNER,
            ro_engine=_ro(fake_ro_engine),
        )
    assert excinfo.value.code == "cds_credit_curve_resolution_failed"


async def test_credit_curve_with_no_hazard_inputs_raises_422(
    fake_ro_engine: FakeEngine,
) -> None:
    """Empty body / no flat hazard / no points → 422 ``cds_credit_curve_resolution_failed``."""

    cds_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    credit_id = uuid.uuid4()
    cds = _cds_row(
        cds_id=cds_id,
        request_json={
            "pricing": {
                "discount_curve_id": str(curve_id),
                "credit_curve_id": str(credit_id),
            }
        },
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.cds" in sql:
            return [cds]
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.credit_curves" in sql:
            return [_credit_curve_row(credit_curve_id=credit_id, source="manual", body={})]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    with pytest.raises(CdsCreditCurveResolutionFailedError) as excinfo:
        await assemble(
            CdsPriceRequest(cds_id=cds_id, as_of=date(2026, 5, 13)),
            owner_uid=OWNER,
            ro_engine=_ro(fake_ro_engine),
        )
    assert excinfo.value.code == "cds_credit_curve_resolution_failed"


async def test_credit_curve_with_inline_par_spread_points_is_accepted(
    fake_ro_engine: FakeEngine,
) -> None:
    """Inline-value-only quotes (no ``quote_id``) are accepted as hazard inputs."""

    cds_id = uuid.uuid4()
    curve_id = uuid.uuid4()
    credit_id = uuid.uuid4()
    cds = _cds_row(
        cds_id=cds_id,
        request_json={
            "pricing": {
                "discount_curve_id": str(curve_id),
                "credit_curve_id": str(credit_id),
            }
        },
    )

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.cds" in sql:
            return [cds]
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.credit_curves" in sql:
            return [
                _credit_curve_row(
                    credit_curve_id=credit_id,
                    source="manual",
                    body={
                        "points": [
                            {
                                "tenor": "5Y",
                                "quote_type": "ParSpread",
                                "quoted_par_spread": 0.012,
                            }
                        ]
                    },
                )
            ]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    out = await assemble(
        CdsPriceRequest(cds_id=cds_id, as_of=date(2026, 5, 13)),
        owner_uid=OWNER,
        ro_engine=_ro(fake_ro_engine),
    )
    assert out.credit_curve.body["points"][0]["quoted_par_spread"] == 0.012


# ---------------------------------------------------------------------------
# Snapshot pinning (shared shape with swap_ir / bonds)
# ---------------------------------------------------------------------------


async def test_snapshot_loaded_and_pinned(
    fake_ro_engine: FakeEngine,
) -> None:
    curve_id = uuid.uuid4()
    credit_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.credit_curves" in sql:
            return [_credit_curve_row(credit_curve_id=credit_id)]
        if "FROM app.snapshots" in sql:
            return [_snapshot_row(snapshot_id=snapshot_id, content={"USD.IRS.1Y": 4.25})]
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    out = await assemble(
        CdsPriceRequest(
            cds_id=None,
            cds={"notional": 10_000_000.0},
            curves=[CurveRef(id=curve_id)],
            credit_curve_id=credit_id,
            as_of=date(2026, 5, 13),
            snapshot_id=snapshot_id,
        ),
        owner_uid=OWNER,
        ro_engine=_ro(fake_ro_engine),
    )
    assert out.snapshot is not None
    assert out.snapshot.pins == {"USD.IRS.1Y": {"value": 4.25, "source": None}}


async def test_snapshot_missing_returns_snapshot_not_found(
    fake_ro_engine: FakeEngine,
) -> None:
    curve_id = uuid.uuid4()
    credit_id = uuid.uuid4()

    def handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM app.curves" in sql:
            return [_curve_row(curve_id=curve_id)]
        if "FROM app.credit_curves" in sql:
            return [_credit_curve_row(credit_curve_id=credit_id)]
        if "FROM app.snapshots" in sql:
            return []
        msg = f"unexpected SQL: {sql!r}"
        raise AssertionError(msg)

    fake_ro_engine.set_handler(handler)

    with pytest.raises(CdsSnapshotNotFoundError) as excinfo:
        await assemble(
            CdsPriceRequest(
                cds_id=None,
                cds={"notional": 10_000_000.0},
                curves=[CurveRef(id=curve_id)],
                credit_curve_id=credit_id,
                as_of=date(2026, 5, 13),
                snapshot_id=uuid.uuid4(),
            ),
            owner_uid=OWNER,
            ro_engine=_ro(fake_ro_engine),
        )
    assert excinfo.value.code == "cds_snapshot_not_found"


# ---------------------------------------------------------------------------
# Assembler output — keying field surface
# ---------------------------------------------------------------------------


async def test_inline_credit_curve_surfaces_none_credit_curve_id(
    fake_ro_engine: FakeEngine,
) -> None:
    """Inline credit-curve override yields ``credit_curve_id=None`` on the assembler.

    The route handler must still emit
    ``credit_curve_id`` into ``shared_inputs`` (with ``None`` here);
    pinning the assembler-side ``None`` is the first half of that
    contract.
    """

    curve_id = uuid.uuid4()
    fake_ro_engine.set_handler(lambda _sql, _params: [_curve_row(curve_id=curve_id)])

    out = await assemble(
        CdsPriceRequest(
            cds_id=None,
            cds={"notional": 10_000_000.0},
            curves=[CurveRef(id=curve_id)],
            credit_curve=CreditCurveRef(
                name="inline",
                recovery_rate=0.4,
                source="flat",
                body={"flat_hazard_rate": 0.02},
            ),
            as_of=date(2026, 5, 13),
        ),
        owner_uid=OWNER,
        ro_engine=_ro(fake_ro_engine),
    )
    assert out.credit_curve_id is None
    assert out.discount_curve_id == curve_id
