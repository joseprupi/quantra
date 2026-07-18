"""Unit tests for :func:`record_pricing_call`.

The function is the cross-product write hook every product pricing
endpoint calls (06-11 add the call sites; this suite covers the
helper in isolation). Coverage focuses on:

1. The SQL shape — INSERT INTO ``app.pricing_history`` with ``RETURNING
   id::text AS id``, JSONB columns bound via ``CAST(:col AS jsonb)``
 (the ``:col::jsonb`` shorthand breaks SQLAlchemy's
   ``text()`` parser).
2. Bind correctness — owner_uid, product_kind, product_id (string for
   UUIDs, ``None`` for inline-only calls), as_of, and JSON-serialised
   request / response payloads. The migration's
   ``pricing_history_product_kind_valid`` CHECK pins ``product_kind``
   to the seven products;:data:`VALID_PRODUCT_KINDS` keeps
   the orchestrator side in sync with that list.
3. Fire-and-forget semantics — a missing ``app_rw`` engine returns
   ``None`` without raising; an exception inside ``begin()`` /
   ``execute()`` also returns ``None`` without raising (plan hard
   constraint: history-write failures must NEVER turn a successful
   price into a 500).

The "in-process DB" is the per-test :class:`conftest.FakeEngine` —
the same recording engine the data-layer unit tests use — so this
suite stays hermetic (no Postgres, no asyncpg) while exercising the
exact ``.begin() / .execute()`` shape the production AsyncEngine
provides. The DB-backed integration coverage lives in
``test_data_db_integration.py`` (already gated behind
``orchestrator_db``).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_orchestrator.pricing.history import (
    PRICING_HISTORY_WRITE_FAILED_CODE,
    VALID_PRODUCT_KINDS,
    record_pricing_call,
)

from .conftest import FakeEngine, Recording


def _inserted_row_handler(row_id: uuid.UUID) -> Any:
    """Build a FakeEngine handler that mimics ``INSERT ... RETURNING id``."""

    def _handler(sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if "INSERT INTO app.pricing_history" in sql:
            return [{"id": str(row_id)}]
        msg = f"unexpected SQL hit history helper: {sql!r}"
        raise AssertionError(msg)

    return _handler


# ---------------------------------------------------------------------------
# Happy path — SQL shape + bind correctness
# ---------------------------------------------------------------------------


async def test_record_pricing_call_inserts_and_returns_row_id() -> None:
    rw = FakeEngine()
    expected_id = uuid.uuid4()
    rw.set_handler(_inserted_row_handler(expected_id))

    swap_id = uuid.uuid4()
    request_payload = {"swap_id": str(swap_id), "as_of": "2026-05-13"}
    response_payload = {"npv": 123.45, "leg_npvs": [], "extras": {"fair_rate": 0.03}}

    history_id = await record_pricing_call(
        rw_engine=cast(AsyncEngine, rw),
        owner_uid="uid-1",
        product_kind="swaps_ir",
        product_id=swap_id,
        as_of=date(2026, 5, 13),
        request_payload=request_payload,
        response_payload=response_payload,
    )

    assert history_id == expected_id
    assert len(rw.recordings) == 1
    recording: Recording = rw.recordings[0]
    assert recording.mode == "write"
    assert "INSERT INTO app.pricing_history" in recording.sql
    # JSONB binds use ``CAST(:col AS jsonb)``, never
    # ``:col::jsonb``. We assert both forms (positive + negative) so a
    # future refactor that "simplifies" the SQL with the shorthand
    # trips this test instead of silently corrupting binds.
    assert "CAST(:request AS jsonb)" in recording.sql
    assert "CAST(:response AS jsonb)" in recording.sql
    assert "::jsonb" not in recording.sql
    assert "RETURNING id::text AS id" in recording.sql

    params = recording.params
    assert params["owner_uid"] == "uid-1"
    assert params["product_kind"] == "swaps_ir"
    assert params["product_id"] == str(swap_id)
    assert params["as_of"] == date(2026, 5, 13)
    # JSONB binds land as JSON strings so SQLAlchemy + asyncpg pass
    # them through the ``CAST(... AS jsonb)`` wrapper unchanged.
    assert json.loads(params["request"]) == request_payload
    assert json.loads(params["response"]) == response_payload


async def test_record_pricing_call_handles_inline_only_call_with_no_product_id() -> None:
    """Inline-only call (no saved row): ``product_id`` is ``None``."""

    rw = FakeEngine()
    rw.set_handler(_inserted_row_handler(uuid.uuid4()))

    history_id = await record_pricing_call(
        rw_engine=cast(AsyncEngine, rw),
        owner_uid="uid-inline",
        product_kind="swaps_ir",
        product_id=None,
        as_of=None,
        request_payload={"swap": {"notional": 1.0}},
        response_payload={"npv": 0.0},
    )

    assert history_id is not None
    assert rw.recordings[0].params["product_id"] is None
    assert rw.recordings[0].params["as_of"] is None


async def test_record_pricing_call_persists_failure_envelope_under_error_key() -> None:
    """A recorded failure carries the envelope under ``{"error":...}``."""

    rw = FakeEngine()
    rw.set_handler(_inserted_row_handler(uuid.uuid4()))

    failure_response: dict[str, Any] = {
        "error": {
            "status_code": 502,
            "error": "Pricing engine client is the stub (real gRPC backend not configured).",
            "code": "engine_unavailable",
            "details": [{"assembled_request": {"as_of": "2026-05-13"}}],
        }
    }
    history_id = await record_pricing_call(
        rw_engine=cast(AsyncEngine, rw),
        owner_uid="uid-failure",
        product_kind="swaps_ir",
        product_id=None,
        as_of=date(2026, 5, 13),
        request_payload={"swap": {"notional": 1.0}},
        response_payload=failure_response,
    )

    assert history_id is not None
    persisted = json.loads(rw.recordings[0].params["response"])
    assert persisted == failure_response
    assert persisted["error"]["code"] == "engine_unavailable"


async def test_record_pricing_call_links_trade_version_for_saved_product() -> None:
    """By-reference calls record the priced entity + its head audit version.

    ``trade_entity_type`` / ``trade_entity_id`` bind to the product kind
    and id; ``trade_version`` is resolved by the INSERT's scalar
    subquery over ``app.entity_versions`` (same transaction, no extra
    round-trip).
    """

    rw = FakeEngine()
    rw.set_handler(_inserted_row_handler(uuid.uuid4()))
    swap_id = uuid.uuid4()

    await record_pricing_call(
        rw_engine=cast(AsyncEngine, rw),
        owner_uid="uid-1",
        product_kind="swaps_ir",
        product_id=swap_id,
        as_of=date(2026, 7, 19),
        request_payload={"swap_id": str(swap_id)},
        response_payload={"npv": 1.0},
    )

    rec = rw.recordings[0]
    assert "trade_entity_type" in rec.sql
    assert "SELECT MAX(v.version_no) FROM app.entity_versions" in rec.sql
    assert "v.entity_type = :product_kind" in rec.sql
    assert "v.owner_uid = :owner_uid" in rec.sql
    assert rec.params["trade_entity_type"] == "swaps_ir"
    assert rec.params["trade_entity_id"] == str(swap_id)


async def test_record_pricing_call_leaves_trade_link_null_for_inline_calls() -> None:
    rw = FakeEngine()
    rw.set_handler(_inserted_row_handler(uuid.uuid4()))

    await record_pricing_call(
        rw_engine=cast(AsyncEngine, rw),
        owner_uid="uid-1",
        product_kind="swaps_ir",
        product_id=None,
        as_of=None,
        request_payload={"swap": {"notional": 1.0}},
        response_payload={"npv": 0.0},
    )

    rec = rw.recordings[0]
    assert rec.params["trade_entity_type"] is None
    assert rec.params["trade_entity_id"] is None


def test_valid_product_kinds_matches_migration_check_list() -> None:
    """Catch drift between the helper's allow-list and the SQL CHECK constraint.

    The Alembic migration ``0007_pricing_history.py`` pins the seven
    valid ``product_kind`` values. Drift here means a new product
    would silently bypass the orchestrator-side guard while still
    being rejected by Postgres at runtime — a much worse failure
    mode than tripping a unit test.
    """

    assert set(VALID_PRODUCT_KINDS) == {
        "swaps_ir",
        "swaps_inflation",
        "swaptions",
        "bonds_fixed",
        "bonds_floating",
        "cds",
        "equity_options",
    }


# ---------------------------------------------------------------------------
# Fire-and-forget semantics — every failure path returns ``None``
# ---------------------------------------------------------------------------


async def test_record_pricing_call_returns_none_when_rw_engine_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A None engine (scaffold deploy / missing DSN) short-circuits silently."""

    with caplog.at_level("WARNING"):
        result = await record_pricing_call(
            rw_engine=None,
            owner_uid="uid-1",
            product_kind="swaps_ir",
            product_id=None,
            as_of=None,
            request_payload={},
            response_payload={},
        )

    assert result is None
    # The ``pricing_history_write_failed`` token is log-only;
    # assert it lands in the structured warning so the audit trail
    # for missing-engine deploys is recoverable.
    assert PRICING_HISTORY_WRITE_FAILED_CODE in caplog.text


class _RaisingEngine:
    """``AsyncEngine`` stand-in whose ``begin()`` raises on entry."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[Any]:
        raise self._exc
        yield  # pragma: no cover -- unreachable; keeps the asynccontextmanager shape


async def test_record_pricing_call_returns_none_when_engine_begin_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A DB-side failure (asyncpg down, etc.) must NEVER propagate."""

    engine = cast(AsyncEngine, _RaisingEngine(ConnectionError("postgres unreachable")))

    with caplog.at_level("WARNING"):
        result = await record_pricing_call(
            rw_engine=engine,
            owner_uid="uid-1",
            product_kind="swaps_ir",
            product_id=None,
            as_of=None,
            request_payload={"x": 1},
            response_payload={"y": 2},
        )

    assert result is None
    assert PRICING_HISTORY_WRITE_FAILED_CODE in caplog.text


async def test_record_pricing_call_returns_none_when_handler_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An IntegrityError / unexpected SQL error inside execute() is swallowed."""

    rw = FakeEngine()

    def _boom(_sql: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        msg = "synthetic asyncpg failure"
        raise RuntimeError(msg)

    rw.set_handler(_boom)

    with caplog.at_level("WARNING"):
        result = await record_pricing_call(
            rw_engine=cast(AsyncEngine, rw),
            owner_uid="uid-1",
            product_kind="swaps_ir",
            product_id=uuid.uuid4(),
            as_of=date(2026, 5, 13),
            request_payload={"x": 1},
            response_payload={"y": 2},
        )

    assert result is None
    assert PRICING_HISTORY_WRITE_FAILED_CODE in caplog.text
