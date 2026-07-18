"""Concurrency seam for orchestrator pricing endpoints.

QuantLib is not threadsafe — the pricing engine therefore fans out
across N single-threaded processes behind Envoy (north-star + D2).
This means the orchestrator can't extract concurrency by handing one
process a list of trades to price in parallel; concurrency lives at
the *gRPC fan-out* layer, where one async ``EngineClient.call(...)``
per batch travels to a different engine process.

This module ships the seam — the value type, the policy Protocol, the
trivial baseline policy, the string-keyed registry, and the runner —
but no endpoint wires it yet. The per-product endpoints are the
consumers; each one provides:

* its own typed ``Trade`` model for ``EngineBatch[Trade]``;
* its own ``price_batch`` callable that maps an ``EngineBatch`` to the
  engine's wire payload (FlatBuffers via ``EngineClient.call``); and
* its own ``shared_inputs`` shape (curve_set_id, vol_surface_id,
  snapshot pin, etc.) that the engine bootstraps once per call.

Hard-pinned dependencies: only ``asyncio`` + stdlib
``typing`` / ``collections.abc`` + the ``EngineClient`` ABC from
``quantra_common.engine_client``. No FastAPI, no DB, no ``httpx``,
no ``grpc``. The seam is unit-testable in complete isolation; the
orchestrator's app-wiring tests don't have to know about it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, Protocol

from quantra_common.engine_client import EngineClient

__all__ = [
    "POLICY_REGISTRY",
    "ConcurrencyPolicy",
    "EngineBatch",
    "OneTradePerCall",
    "execute",
    "resolve_policy",
]


@dataclass(frozen=True, slots=True)
class EngineBatch[T]:
    """One concurrent engine call's payload: a tuple of trades + per-call shared inputs.

    ``trades`` is the slice of the flat input list that travels as one
    gRPC call. Tuples (not lists) so the value is hashable and cheap
    to compare in tests; freezing + slots turns the whole instance
    into an immutable, low-overhead value.

    ``shared_inputs`` is the bag of things the engine bootstraps once
    per call rather than once per trade — typically curve sets, vol
    surfaces, and snapshot pins (e.g. for an IR-swap batch
    ``{"curve_set_id": "abc...", "snapshot_version": "..."}``). The
    map is intentionally typed as ``Mapping[str, Any]``: each
    per-product plan declares its own typed-dict / dataclass for these
    keys and packs the result in here right before the call, so the
    seam doesn't have to widen its type as new products land.
    """

    trades: tuple[T, ...]
    shared_inputs: Mapping[str, Any]


class ConcurrencyPolicy[T](Protocol):
    """Sync planner that turns a flat trade list into engine-call batches.

    ``plan`` is intentionally synchronous — it's pure scheduling work
    (size / group / shard the list) and must never block the event
    loop on I/O. The async fan-out and result aggregation live in
    :func:`execute`, not in the policy.

    Each returned batch becomes exactly one concurrent ``price_batch``
    call. Per-product plans choose the policy: :class:`OneTradePerCall`
    for products that don't share much per-call state; future
    ``GroupByCurveSet`` / ``MaxBatchSize`` policies for
    products where the engine pays a heavy bootstrap per call (IR
    swaps share curve sets, swaptions share vol surfaces).
    """

    def plan(self, trades: Sequence[T]) -> Sequence[EngineBatch[T]]: ...


class OneTradePerCall:
    """Trivial policy: one trade per batch, empty ``shared_inputs``.

    Maximum process-level fan-out, zero curve-cache reuse on the
    engine side. The right default for products where the per-call
    bootstrap is cheap or where each trade carries its own independent
    inputs (e.g. a swaption with an inline vol surface embedded in the
    request). The only policy shipped today; bootstrap-amortising
    policies can be added per product once measured as worth it.

    Stateless — instances are interchangeable. The class is what the
    registry holds, so swapping the wired policy is a settings change
    rather than a code change for the consuming endpoint.
    """

    def plan[T](self, trades: Sequence[T]) -> list[EngineBatch[T]]:
        return [EngineBatch(trades=(trade,), shared_inputs={}) for trade in trades]


# Stable id → policy class. Inline (no sibling ``registry.py``) because
# the surface is intentionally tiny; new policies add entries here. The
# keys are the strings settings hold (e.g.
# ``OrchestratorSettings.concurrency_policy_swap_ir``); renaming a key is
# a config-breaking change.
POLICY_REGISTRY: Final[dict[str, type[ConcurrencyPolicy[Any]]]] = {
    "one_trade_per_call": OneTradePerCall,
}


def resolve_policy(name: str) -> type[ConcurrencyPolicy[Any]]:
    """Look up a policy class by its registry id; raise ``ValueError`` on miss.

    Settings hold strings (the per-product ``concurrency_policy_<product>``
    fields); this is the one place those strings cross over into class
    objects.
    Fail loud on miss so a typo'd policy name never silently
    downgrades to the default.
    """

    try:
        return POLICY_REGISTRY[name]
    except KeyError as exc:
        known = ", ".join(sorted(POLICY_REGISTRY)) or "(none)"
        msg = f"Unknown concurrency policy {name!r}; known: {known}"
        raise ValueError(msg) from exc


async def execute[T, R](
    policy: ConcurrencyPolicy[T],
    engine: EngineClient,
    trades: Sequence[T],
    *,
    price_batch: Callable[[EngineClient, EngineBatch[T]], Awaitable[Sequence[R]]],
    shared_inputs: Mapping[str, Any] | None = None,
) -> list[R]:
    """Plan, fan out, gather, flatten — return per-trade results in input order.

    Steps:

    1. Call ``policy.plan(trades)`` synchronously. The policy may
       return any ``Sequence`` of batches.
    2. If ``shared_inputs`` is provided, merge it into every batch's
       ``EngineBatch.shared_inputs`` via :func:`dataclasses.replace`
       (route keys win over policy-set ones). This is the
       threading mechanism: route-level constants (``as_of``,
       ``curve_set_id``, ``vol_surface_id``, …) reach the engine on
       every batch without each per-product route having to wrap
       ``price_batch`` in a shared-inputs injector. Policies that
       set per-batch keys (e.g. a future ``GroupByCurveSet`` whose
       ``plan`` emits ``shared_inputs={"curve_set_id": <group_id>}``)
       remain compatible — route-wins-over-batch matches the
       previous bonds-only ``_with_shared_inputs`` wrapper.
    3. Issue ``asyncio.gather(*(price_batch(engine, b) for b in
       batches))``. Each ``price_batch`` call is one concurrent gRPC
       round-trip; concurrency is bounded by the policy (which decides
       how many batches there are), not by this runner.
    4. Flatten the list-of-lists back into a single ``list[R]`` in
       batch order. Because each policy preserves the input order of
       its trades within a batch, the flattened result lines up 1:1
       with the input ``trades`` sequence — callers can
       ``zip(trades, results)`` directly.

    Error behaviour follows :func:`asyncio.gather` defaults
    (``return_exceptions=False``): the first concurrent failure
    propagates and the remaining tasks are cancelled. This is
    deliberate — pricing endpoints want fail-fast semantics so a
    bad input doesn't silently waste compute on the rest of the
    batch. Per-product code that wants partial-success handling can
    layer a wrapper on top.

    ``price_batch`` is injected so the runner stays product-agnostic.
    Each product package provides the actual translator
    (``EngineBatch[Trade]`` → ``EngineClient.call(rpc, request_bytes)``
    → response decode → ``Sequence[Result]``) without modifying this
    function. ``shared_inputs`` is keyword-only with a ``None``
    default so callers stay source-compatible.
    """

    batches: Sequence[EngineBatch[T]] = policy.plan(trades)
    if not batches:
        return []
    if shared_inputs is not None:
        route_shared = dict(shared_inputs)
        batches = [
            replace(
                batch,
                shared_inputs={**dict(batch.shared_inputs), **route_shared},
            )
            for batch in batches
        ]
    grouped = await asyncio.gather(*(price_batch(engine, batch) for batch in batches))
    flattened: list[R] = []
    for chunk in grouped:
        flattened.extend(chunk)
    return flattened
