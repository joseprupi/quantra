"""`MdClient` — async wrapper around the read-only MD service HTTP surface."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from types import TracebackType
from typing import Any, Self

import httpx
from pydantic import BaseModel, Field, ValidationError

from quantra_common.md_client.cache import NullQuoteCache, QuoteCache, quote_cache_key
from quantra_common.md_client.errors import (
    MdClientError,
    MdHttpStatusError,
    MdNotFoundError,
    MdResponseError,
    MdTimeoutError,
    MdTransportError,
)
from quantra_common.settings.base import Settings
from quantra_common.types import ResolvedQuote

_logger = logging.getLogger(__name__)


class MdClientConfig(BaseModel):
    """Static configuration for an `MdClient` instance."""

    base_url: str
    timeout_s: float = Field(default=10.0, gt=0)
    max_retries: int = Field(default=3, ge=0)
    backoff_base_s: float = Field(default=0.1, gt=0)
    backoff_cap_s: float = Field(default=2.0, gt=0)

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            base_url=settings.require_md_service_url(),
            timeout_s=settings.md_service_timeout_s,
        )


class MdClient:
    """Async HTTP client for the MD service.

    Use as an async context manager so the underlying ``httpx.AsyncClient``
    is closed cleanly::

        async with MdClient.from_settings(get_settings()) as md:
            quotes = await md.resolve_quotes(["USD.IRS.5Y.PAR"], as_of)
    """

    def __init__(
        self,
        config: MdClientConfig,
        *,
        cache: QuoteCache | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        # Don't use `cache or NullQuoteCache()` — `__len__`-based truthiness
        # would replace an empty real cache with the no-op one.
        self._cache: QuoteCache = NullQuoteCache() if cache is None else cache
        self._client = (
            client
            if client is not None
            else httpx.AsyncClient(
                base_url=config.base_url.rstrip("/"),
                timeout=config.timeout_s,
            )
        )
        self._owns_client = client is None

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        cache: QuoteCache | None = None,
    ) -> MdClient:
        return cls(MdClientConfig.from_settings(settings), cache=cache)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ----- public API --------------------------------------------------------

    async def resolve_quotes(
        self,
        canonical_ids: list[str],
        as_of: date | datetime,
        *,
        snapshot_version: str | None = None,
    ) -> list[ResolvedQuote]:
        """Resolve a batch of canonical IDs at a point in time.

        Cache hits are returned without a network call; cache misses are
        batched into a single ``POST /quotes/resolved`` request and the
        results are written back to the cache before returning.

        ``snapshot_version`` is forwarded to the MD service in the request
        body. The MD service may not yet honor it (open question O1) but
        sending it from day one keeps callers from having to migrate later.
        """

        as_of_dt = _to_datetime(as_of)

        result_by_id: dict[str, ResolvedQuote] = {}
        misses: list[str] = []
        for cid in canonical_ids:
            key = quote_cache_key(cid, as_of_dt, snapshot_version)
            cached = await self._cache.get(key)
            if cached is not None:
                result_by_id[cid] = cached
            else:
                misses.append(cid)

        if misses:
            payload: dict[str, Any] = {
                "canonical_ids": misses,
                "as_of": as_of_dt.isoformat(),
            }
            if snapshot_version is not None:
                payload["snapshot_version"] = snapshot_version

            body = await self._post_json("/quotes/resolved", json=payload)
            try:
                items_raw = body.get("items", []) if isinstance(body, dict) else []
                fetched = [ResolvedQuote.model_validate(item) for item in items_raw]
            except ValidationError as e:
                raise MdResponseError(f"unparseable /quotes/resolved body: {e}") from e

            for rq in fetched:
                key = quote_cache_key(rq.canonical_id, as_of_dt, snapshot_version)
                await self._cache.put(key, rq)
                result_by_id[rq.canonical_id] = rq

        # Preserve caller order; fill misses with explicit not-found markers.
        out: list[ResolvedQuote] = []
        for cid in canonical_ids:
            if cid in result_by_id:
                out.append(result_by_id[cid])
            else:
                out.append(
                    ResolvedQuote(
                        canonical_id=cid,
                        requested_as_of=as_of_dt,
                        found=False,
                    )
                )
        return out

    # ----- low-level helpers -------------------------------------------------

    async def _post_json(self, path: str, *, json: dict[str, Any]) -> object:
        return await self._request("POST", path, params=None, json=json)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        json: dict[str, Any] | None,
    ) -> object:
        attempt = 0
        last_exc: MdClientError | None = None
        while True:
            try:
                response = await self._client.request(
                    method,
                    path,
                    params=params,
                    json=json,
                )
            except httpx.TimeoutException as e:
                last_exc = MdTimeoutError(f"timeout calling {method} {path}: {e}")
            except httpx.TransportError as e:
                last_exc = MdTransportError(f"transport error calling {method} {path}: {e}")
            else:
                if response.status_code == httpx.codes.NOT_FOUND:
                    raise MdNotFoundError(response.status_code, response.text)
                if response.status_code >= httpx.codes.BAD_REQUEST:
                    last_exc = MdHttpStatusError(response.status_code, response.text)
                    if response.status_code < httpx.codes.INTERNAL_SERVER_ERROR:
                        raise last_exc
                else:
                    try:
                        return response.json()
                    except ValueError as e:
                        raise MdResponseError(f"non-JSON body from {path}: {e}") from e

            attempt += 1
            if attempt > self._config.max_retries:
                assert last_exc is not None
                raise last_exc
            sleep_s = min(
                self._config.backoff_cap_s,
                self._config.backoff_base_s * (2 ** (attempt - 1)),
            )
            _logger.debug(
                "md request retrying",
                extra={
                    "attempt": attempt,
                    "method": method,
                    "path": path,
                    "sleep_s": sleep_s,
                },
            )
            await asyncio.sleep(sleep_s)


def _to_datetime(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime(value.year, value.month, value.day)
