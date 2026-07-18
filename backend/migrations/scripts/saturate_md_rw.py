"""Saturate md_rw and prove app_rw keeps serving.

This is the runbook script for ``database/04_pool_isolation.md``'s
acceptance criterion: "Saturating ``md_rw`` does not cause new
``app_rw`` connections to fail or queue."

What it does, in order:

1. Build an md_rw engine with the production-shaped pool (size +
   overflow), and check out every connection it can hand out.
2. Confirm one more checkout against md_rw raises the SQLAlchemy
   ``TimeoutError`` (a subclass of ``builtins.TimeoutError``) within
   ``pg_pool_timeout_s`` — i.e. saturation surfaces as a fast
   client-side error, not a Postgres-side "too many connections."
3. While md_rw is fully saturated, build an app_rw engine and run a
   trivial query through it. The query must succeed; that is the
   isolation we paid for.
4. Print a small summary of pool stats so the operator can see what
   was checked out where.
5. Tear everything down so re-runs start from a clean state.

Run after a fresh ``alembic -n {app,md} upgrade head`` against the dev
Postgres::

    uv run python migrations/scripts/saturate_md_rw.py

It is deliberately not part of ``pytest`` — touching a live Postgres
from CI would require stand-up infrastructure that is out of scope
for the pool-isolation acceptance bar. The script exits non-zero on any
acceptance failure so it can be wired into a smoke-test pipeline
later without changes.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as SaTimeoutError

from quantra_common.db import (
    DbRole,
    make_app_engine,
    make_md_engine,
    pool_stats,
)
from quantra_common.settings.base import Settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


def _max_md_rw_concurrency(settings: Settings) -> int:
    return settings.pg_pool_size_md_rw + settings.pg_pool_max_overflow_md_rw


async def _hold_n_md_rw_connections(
    engine: AsyncEngine,
    n: int,
    stack: AsyncExitStack,
) -> list[AsyncConnection]:
    """Open and pin ``n`` md_rw connections, returning the live handles.

    Each connection runs a trivial query so we know the underlying socket
    is real (asyncpg is lazy: ``engine.connect()`` alone may not contact
    the server until the first statement). ``stack`` owns the lifetimes
    so the caller can rely on a single ``aclose`` to release everything.
    """

    held: list[AsyncConnection] = []
    for i in range(n):
        conn = await stack.enter_async_context(engine.connect())
        await conn.execute(text("SELECT 1"))
        held.append(conn)
        print(f"  md_rw checkout {i + 1}/{n}: ok")
    return held


async def _expect_md_rw_saturation(engine: AsyncEngine, timeout_s: float) -> None:
    """One more checkout beyond the cap must raise within ``timeout_s``."""

    print(
        f"  attempting one extra md_rw checkout (expecting TimeoutError within ~{timeout_s:.1f}s) …"
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except (SaTimeoutError, TimeoutError) as exc:
        print(f"  md_rw saturated as expected: {type(exc).__name__}: {exc}")
        return
    raise SystemExit(
        "md_rw was NOT saturated — extra checkout succeeded. "
        "pool_size + max_overflow may not match the configured limit."
    )


async def _expect_app_rw_unaffected(engine: AsyncEngine, n_queries: int) -> None:
    """``app_rw`` must continue to serve while md_rw is fully checked out."""

    print(f"  running {n_queries} sequential app_rw queries …")
    for i in range(n_queries):
        async with engine.connect() as conn:
            value = await conn.scalar(text("SELECT 1"))
            assert value == 1, f"app_rw returned unexpected value {value!r}"
        print(f"    app_rw query {i + 1}/{n_queries}: ok")


async def main() -> None:
    settings = Settings()
    n = _max_md_rw_concurrency(settings)
    print(
        "config: "
        f"pool_size_md_rw={settings.pg_pool_size_md_rw}, "
        f"max_overflow_md_rw={settings.pg_pool_max_overflow_md_rw}, "
        f"pool_timeout_s={settings.pg_pool_timeout_s}"
    )
    print(f"target: hold {n} md_rw connections, then keep app_rw responsive\n")

    md_engine = make_md_engine(DbRole.RW, settings=settings)
    app_engine = make_app_engine(DbRole.RW, settings=settings)

    try:
        async with AsyncExitStack() as stack:
            print("step 1: saturate md_rw")
            await _hold_n_md_rw_connections(md_engine, n, stack)
            md_stats = pool_stats(md_engine)
            print(f"  md_rw pool stats: {md_stats}")

            print("\nstep 2: confirm md_rw cannot accept another checkout")
            await _expect_md_rw_saturation(md_engine, settings.pg_pool_timeout_s)

            print("\nstep 3: app_rw must keep serving")
            await _expect_app_rw_unaffected(app_engine, n_queries=5)
            app_stats = pool_stats(app_engine)
            print(f"  app_rw pool stats: {app_stats}")

        # AsyncExitStack cleanup released the held md_rw connections; the
        # next checkout should succeed without raising.
        print("\nstep 4: md_rw recovers after release")
        async with md_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        print("  md_rw post-release checkout: ok")
    finally:
        await md_engine.dispose()
        await app_engine.dispose()

    print("\nsaturation acceptance: PASS")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit:
        raise
    except Exception as exc:
        print(f"saturation FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
