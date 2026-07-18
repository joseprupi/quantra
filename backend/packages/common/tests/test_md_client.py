"""Tests for `quantra_common.md_client`."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest

from quantra_common.md_client import (
    MdClient,
    MdClientConfig,
    MdHttpStatusError,
    MdNotFoundError,
    MdResponseError,
    MdTimeoutError,
    MdTransportError,
    NullQuoteCache,
    quote_cache_key,
)
from quantra_common.types import ResolvedQuote


class _DictQuoteCache:
    """Minimal dict-backed `QuoteCache` implementation for tests."""

    def __init__(self) -> None:
        self._store: dict[str, ResolvedQuote] = {}

    async def get(self, key: str) -> ResolvedQuote | None:
        return self._store.get(key)

    async def put(self, key: str, value: ResolvedQuote) -> None:
        self._store[key] = value

    async def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


def _make_client(
    handler: httpx.MockTransport,
    *,
    cache: Any = None,
    max_retries: int = 0,
    backoff_base_s: float = 0.001,
) -> MdClient:
    config = MdClientConfig(
        base_url="http://md.test",
        timeout_s=1.0,
        max_retries=max_retries,
        backoff_base_s=backoff_base_s,
    )
    transport = httpx.AsyncClient(base_url=config.base_url, transport=handler)
    return MdClient(config, cache=cache, client=transport)


# ----- cache primitives -----------------------------------------------------


def test_quote_cache_key_is_stable() -> None:
    a = quote_cache_key("X", datetime(2026, 5, 1, tzinfo=UTC), "v1")
    b = quote_cache_key("X", datetime(2026, 5, 1, tzinfo=UTC), "v1")
    assert a == b
    assert a != quote_cache_key("X", datetime(2026, 5, 1, tzinfo=UTC), "v2")


@pytest.mark.asyncio
async def test_null_cache_returns_none() -> None:
    cache = NullQuoteCache()
    rq = ResolvedQuote(canonical_id="A", found=True, value=1.0)
    await cache.put("k", rq)
    assert await cache.get("k") is None


# ----- resolve_quotes -------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_quotes_uses_cache_when_available() -> None:
    cache = _DictQuoteCache()
    cached = ResolvedQuote(
        canonical_id="X",
        requested_as_of=datetime(2026, 5, 1, tzinfo=UTC),
        found=True,
        value=99.0,
    )
    await cache.put(
        quote_cache_key("X", datetime(2026, 5, 1, tzinfo=UTC), None),
        cached,
    )

    call_count = 0

    def handler(_req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"items": []})

    async with _make_client(httpx.MockTransport(handler), cache=cache) as md:
        out = await md.resolve_quotes(["X"], datetime(2026, 5, 1, tzinfo=UTC))

    assert out[0].value == 99.0
    assert call_count == 0  # fully served from cache


@pytest.mark.asyncio
async def test_resolve_quotes_fetches_misses_and_writes_cache() -> None:
    cache = _DictQuoteCache()
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        captured["body"] = body
        captured["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "canonical_id": "A",
                        "requested_as_of": "2026-05-01T00:00:00+00:00",
                        "found": True,
                        "is_exact": True,
                        "resolved_as_of": "2026-05-01T00:00:00+00:00",
                        "value": 1.0,
                    },
                    {
                        "canonical_id": "B",
                        "requested_as_of": "2026-05-01T00:00:00+00:00",
                        "found": False,
                    },
                ]
            },
        )

    async with _make_client(httpx.MockTransport(handler), cache=cache) as md:
        out = await md.resolve_quotes(
            ["A", "B"],
            date(2026, 5, 1),
            snapshot_version="v42",
        )

    assert [r.canonical_id for r in out] == ["A", "B"]
    assert out[0].value == 1.0
    assert not out[1].found
    assert b'"snapshot_version":"v42"' in captured["body"]
    assert captured["path"] == "/quotes/resolved"
    assert len(cache) == 2  # cache should hold both A and B

    second_call_count = 0

    def again_handler(_req: httpx.Request) -> httpx.Response:
        nonlocal second_call_count
        second_call_count += 1
        return httpx.Response(500, text="should not be called")

    async with _make_client(httpx.MockTransport(again_handler), cache=cache) as md:
        cached = await md.resolve_quotes(["A"], date(2026, 5, 1), snapshot_version="v42")
    assert cached[0].value == 1.0
    assert second_call_count == 0


@pytest.mark.asyncio
async def test_resolve_quotes_invalidates_cache_across_snapshot_versions() -> None:
    """an etag bump must invalidate the per-(cid, as_of) cache.

    The cache is keyed on ``(canonical_id, as_of, snapshot_version)``;
    a fetch under ``v1`` populates one key, and the same
    ``(canonical_id, as_of)`` queried under ``v2`` MUST round-trip to
    the MD service (different key) and return ``v2``'s value, not
    the stale ``v1`` value. This is the correctness story.
    """

    cache = _DictQuoteCache()
    served_value = {"current": 1.0}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "canonical_id": "A",
                        "requested_as_of": "2026-05-01T00:00:00+00:00",
                        "found": True,
                        "is_exact": True,
                        "resolved_as_of": "2026-05-01T00:00:00+00:00",
                        "value": served_value["current"],
                    }
                ]
            },
        )

    # First call: snapshot v1, server returns value A = 1.0; cache it.
    async with _make_client(httpx.MockTransport(handler), cache=cache) as md:
        out_v1 = await md.resolve_quotes(["A"], date(2026, 5, 1), snapshot_version="v1")
    assert out_v1[0].value == 1.0
    assert len(cache) == 1

    # Etag bump: same (cid, as_of) but a new snapshot_version. The
    # server side now returns value B = 2.0 to prove we did NOT serve
    # the stale v1 cache entry.
    served_value["current"] = 2.0
    async with _make_client(httpx.MockTransport(handler), cache=cache) as md:
        out_v2 = await md.resolve_quotes(["A"], date(2026, 5, 1), snapshot_version="v2")
    assert out_v2[0].value == 2.0
    # Both entries coexist (different keys); old v1 row was not evicted.
    assert len(cache) == 2


# ----- retry / error mapping ------------------------------------------------


@pytest.mark.asyncio
async def test_5xx_is_retried_then_raised() -> None:
    calls = 0

    def handler(_req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="upstream down")

    async with _make_client(httpx.MockTransport(handler), max_retries=2) as md:
        with pytest.raises(MdHttpStatusError) as exc:
            await md.resolve_quotes(["A"], date(2026, 5, 1))
    assert exc.value.status_code == 503
    assert calls == 3  # initial + 2 retries


@pytest.mark.asyncio
async def test_4xx_is_not_retried() -> None:
    calls = 0

    def handler(_req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, text="bad request")

    async with _make_client(httpx.MockTransport(handler), max_retries=5) as md:
        with pytest.raises(MdHttpStatusError) as exc:
            await md.resolve_quotes(["A"], date(2026, 5, 1))
    assert exc.value.status_code == 400
    assert calls == 1


@pytest.mark.asyncio
async def test_404_raises_not_found() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    async with _make_client(httpx.MockTransport(handler)) as md:
        with pytest.raises(MdNotFoundError):
            await md.resolve_quotes(["A"], date(2026, 5, 1))


@pytest.mark.asyncio
async def test_invalid_body_raises_response_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{"unexpected": "shape"}]})

    async with _make_client(httpx.MockTransport(handler)) as md:
        with pytest.raises(MdResponseError):
            await md.resolve_quotes(["A"], date(2026, 5, 1))


@pytest.mark.asyncio
async def test_timeout_is_retried_and_then_raises() -> None:
    calls = 0

    def handler(_req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectTimeout("nope")

    async with _make_client(httpx.MockTransport(handler), max_retries=1) as md:
        with pytest.raises(MdTimeoutError):
            await md.resolve_quotes(["A"], date(2026, 5, 1))
    assert calls == 2


@pytest.mark.asyncio
async def test_transport_error_is_classified() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns")

    async with _make_client(httpx.MockTransport(handler), max_retries=0) as md:
        with pytest.raises(MdTransportError):
            await md.resolve_quotes(["A"], date(2026, 5, 1))
