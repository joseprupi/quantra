"""Unit tests for the pricing-concurrency seam.

The seam ships scaffolding only — no endpoint wires it yet — so these
tests exercise it in complete isolation. They cover:

* :class:`OneTradePerCall.plan` produces N batches of size 1 for N
  trades, in input order, with empty ``shared_inputs``.
* :func:`execute` with a stub ``price_batch`` that returns the batch
  verbatim yields the original trade list back, in order.
* :func:`execute` propagates the first concurrent failure (no
  swallowing) — the documented :func:`asyncio.gather` default.
* :func:`resolve_policy` resolves ``"one_trade_per_call"`` →
  :class:`OneTradePerCall`; an unknown name raises ``ValueError``.
* :func:`execute` actually fans calls out concurrently — 10 batches of
  ``await asyncio.sleep(0.05)`` complete in well under the
  serialised-floor of 0.5 s.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import FrozenInstanceError

import pytest

from quantra_common.engine_client import (
    EngineClient,
    EngineClientConfig,
    StubEngineClient,
)
from quantra_orchestrator.pricing.concurrency import (
    POLICY_REGISTRY,
    EngineBatch,
    OneTradePerCall,
    execute,
    resolve_policy,
)

# A "trade" type for the tests is just a string id; the seam is generic
# so the actual T doesn't matter, only that order is preserved.
Trade = str


@pytest.fixture
def engine() -> EngineClient:
    """A throwaway ``EngineClient`` instance.

    ``execute`` only forwards the engine to ``price_batch``; the test
    ``price_batch`` callables never actually invoke ``.call(...)``. The
    stub satisfies the type without spinning up a real channel.
    """

    return StubEngineClient(EngineClientConfig(address="stub"))


def test_one_trade_per_call_plan_preserves_order_and_size() -> None:
    policy = OneTradePerCall()
    trades: list[Trade] = ["t0", "t1", "t2", "t3"]

    batches = policy.plan(trades)

    assert len(batches) == len(trades)
    for index, batch in enumerate(batches):
        assert batch.trades == (trades[index],)
        assert batch.shared_inputs == {}


def test_one_trade_per_call_plan_handles_empty() -> None:
    policy = OneTradePerCall()

    assert policy.plan([]) == []


def test_engine_batch_is_frozen() -> None:
    """``EngineBatch`` is a frozen dataclass so it can travel as a value."""

    batch: EngineBatch[Trade] = EngineBatch(trades=("t0",), shared_inputs={})

    with pytest.raises(FrozenInstanceError):
        batch.trades = ("t1",)  # type: ignore[misc]


def test_engine_batch_has_slots() -> None:
    """``slots=True`` keeps ``EngineBatch`` cheap as a per-call value object."""

    assert EngineBatch.__slots__ == ("trades", "shared_inputs")
    batch: EngineBatch[Trade] = EngineBatch(trades=("t0",), shared_inputs={})
    assert not hasattr(batch, "__dict__")


async def test_execute_returns_results_in_input_order(engine: EngineClient) -> None:
    """The flattened output lines up 1:1 with the input ``trades`` sequence."""

    trades: list[Trade] = [f"t{i}" for i in range(5)]

    async def price_batch(_engine: EngineClient, batch: EngineBatch[Trade]) -> Sequence[Trade]:
        return list(batch.trades)

    results = await execute(
        OneTradePerCall(),
        engine,
        trades,
        price_batch=price_batch,
    )

    assert results == trades


async def test_execute_with_empty_trades_short_circuits(engine: EngineClient) -> None:
    """No trades → no ``price_batch`` calls, empty result."""

    calls = 0

    async def price_batch(_engine: EngineClient, _batch: EngineBatch[Trade]) -> Sequence[Trade]:
        nonlocal calls
        calls += 1
        return []

    results = await execute(
        OneTradePerCall(),
        engine,
        [],
        price_batch=price_batch,
    )

    assert results == []
    assert calls == 0


async def test_execute_propagates_failure_from_price_batch(engine: EngineClient) -> None:
    """One failing batch surfaces; remaining work is cancelled (gather default)."""

    trades: list[Trade] = [f"t{i}" for i in range(5)]
    bad_trade = "t2"

    class BatchPricingError(RuntimeError):
        pass

    async def price_batch(_engine: EngineClient, batch: EngineBatch[Trade]) -> Sequence[Trade]:
        # Give every coroutine a chance to start before any returns,
        # so the failure races against in-flight peers (mirrors how
        # the real fan-out behaves under load).
        await asyncio.sleep(0)
        if batch.trades[0] == bad_trade:
            msg = f"engine refused trade {batch.trades[0]!r}"
            raise BatchPricingError(msg)
        return list(batch.trades)

    with pytest.raises(BatchPricingError, match="t2"):
        await execute(
            OneTradePerCall(),
            engine,
            trades,
            price_batch=price_batch,
        )


def test_registry_contains_one_trade_per_call() -> None:
    assert POLICY_REGISTRY["one_trade_per_call"] is OneTradePerCall


def test_resolve_policy_returns_class() -> None:
    cls = resolve_policy("one_trade_per_call")

    assert cls is OneTradePerCall


def test_resolve_policy_unknown_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Unknown concurrency policy 'does-not-exist'"):
        resolve_policy("does-not-exist")


async def test_execute_threads_shared_inputs_into_every_batch(
    engine: EngineClient,
) -> None:
    """Shape B: ``execute(shared_inputs=...)`` populates every batch.

    Pins the seam-level threading mechanism: the route hands
    ``execute`` a route-level constants dict; the runner merges it
    into every batch the policy emits *before* the per-product
    ``price_batch`` translator sees the batch. The merge uses
    route-wins semantics (mirrors the previous bonds-only
    ``_with_shared_inputs`` wrapper) so a future
    ``GroupByCurveSet`` policy that sets per-batch keys still works,
    while today's ``OneTradePerCall`` (empty batch shared_inputs)
    sees the route dict verbatim.
    """

    captured: list[EngineBatch[Trade]] = []

    async def price_batch(_engine: EngineClient, batch: EngineBatch[Trade]) -> Sequence[Trade]:
        captured.append(batch)
        return list(batch.trades)

    trades: list[Trade] = ["a", "b", "c"]
    shared = {"curve_set_id": "abc", "as_of": "2026-05-13"}

    await execute(
        OneTradePerCall(),
        engine,
        trades,
        price_batch=price_batch,
        shared_inputs=shared,
    )

    assert len(captured) == 3
    for batch in captured:
        assert dict(batch.shared_inputs) == shared


async def test_execute_route_shared_inputs_win_over_policy_set_keys(
    engine: EngineClient,
) -> None:
    """Route keys override policy-set keys on the same name.

    Today's ``OneTradePerCall`` sets ``shared_inputs={}`` so this
    only matters for future grouping policies. A toy policy below
    pre-loads a ``policy_marker`` key plus a colliding ``curve_set_id``
    so the merge semantics are observable on the same trade list.
    """

    class _MarkingPolicy:
        def plan(self, trades_: Sequence[Trade]) -> list[EngineBatch[Trade]]:
            return [
                EngineBatch(
                    trades=(trade,),
                    shared_inputs={
                        "policy_marker": "set",
                        "curve_set_id": "policy-set",
                    },
                )
                for trade in trades_
            ]

    captured: list[EngineBatch[Trade]] = []

    async def price_batch(_engine: EngineClient, batch: EngineBatch[Trade]) -> Sequence[Trade]:
        captured.append(batch)
        return list(batch.trades)

    await execute(
        _MarkingPolicy(),
        engine,
        ["a"],
        price_batch=price_batch,
        shared_inputs={"curve_set_id": "route-wins", "as_of": "2026-05-13"},
    )

    assert len(captured) == 1
    assert dict(captured[0].shared_inputs) == {
        "policy_marker": "set",
        "curve_set_id": "route-wins",
        "as_of": "2026-05-13",
    }


async def test_execute_shared_inputs_default_none_is_a_no_op(
    engine: EngineClient,
) -> None:
    """Backwards-compat: omitting ``shared_inputs`` leaves batches untouched.

    Plans 09-11 land before they migrate to the kwarg; this
    pins that ``execute`` without ``shared_inputs`` is bit-identical
    to the legacy shape (empty ``shared_inputs`` for the trivial
    policy, no surprise mutation of policy-set keys).
    """

    captured: list[EngineBatch[Trade]] = []

    async def price_batch(_engine: EngineClient, batch: EngineBatch[Trade]) -> Sequence[Trade]:
        captured.append(batch)
        return list(batch.trades)

    await execute(
        OneTradePerCall(),
        engine,
        ["a", "b"],
        price_batch=price_batch,
    )

    assert [dict(batch.shared_inputs) for batch in captured] == [{}, {}]


async def test_execute_runs_batches_concurrently(engine: EngineClient) -> None:
    """10 batches x 50ms sleep complete in well under the serialised 500ms floor.

    A serial implementation would take ~500ms (10 * 0.05s); a fully
    concurrent fan-out completes near ~0.05s. The 0.2s threshold is
    generous to absorb event-loop scheduling jitter on slow CI while
    still catching a regression that drops the fan-out (which would
    push wall clock back over 0.5s).
    """

    trades: list[Trade] = [f"t{i}" for i in range(10)]
    sleep_per_batch_s = 0.05
    serialised_floor_s = sleep_per_batch_s * len(trades)
    threshold_s = 0.2

    async def price_batch(_engine: EngineClient, batch: EngineBatch[Trade]) -> Sequence[Trade]:
        await asyncio.sleep(sleep_per_batch_s)
        return list(batch.trades)

    started = time.monotonic()
    results = await execute(
        OneTradePerCall(),
        engine,
        trades,
        price_batch=price_batch,
    )
    elapsed = time.monotonic() - started

    assert results == trades
    assert elapsed < threshold_s, (
        f"execute took {elapsed:.3f}s; concurrent fan-out should be well "
        f"under {threshold_s}s (serialised floor: {serialised_floor_s}s)"
    )
