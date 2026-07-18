"""Tests for `quantra_common.engine_client`."""

from __future__ import annotations

from typing import Any

import pytest

from quantra_common.engine_client import (
    EngineClient,
    EngineClientConfig,
    EngineRetryableError,
    EngineRpc,
    EngineRpcError,
    EngineTimeoutError,
    RetryingEngineClient,
    StubEngineClient,
)
from quantra_common.settings.base import Settings


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


# ----- enum / config --------------------------------------------------------


def test_engine_rpc_values_match_engine_fbs() -> None:
    assert EngineRpc.PRICE_VANILLA_SWAP.value == "PriceVanillaSwap"
    assert EngineRpc.BOOTSTRAP_CURVES.value == "BootstrapCurves"
    assert EngineRpc.PRICE_FRA.value == "PriceFRA"


def test_engine_client_config_from_settings() -> None:
    s = _settings(engine_grpc_addr="engine:50051", engine_grpc_timeout_s=12.0)
    cfg = EngineClientConfig.from_settings(s)
    assert cfg.address == "engine:50051"
    assert cfg.timeout_s == 12.0


def test_engine_client_config_requires_addr() -> None:
    s = _settings()
    with pytest.raises(RuntimeError, match="ENGINE_GRPC_ADDR"):
        EngineClientConfig.from_settings(s)


# ----- StubEngineClient -----------------------------------------------------


@pytest.mark.asyncio
async def test_stub_engine_client_raises_not_implemented() -> None:
    async with StubEngineClient() as client:
        with pytest.raises(NotImplementedError, match="PriceVanillaSwap"):
            await client.call(EngineRpc.PRICE_VANILLA_SWAP, b"\x00")


# ----- RetryingEngineClient -------------------------------------------------


class _ScriptedClient(EngineClient):
    """Test double that replays a list of outcomes (bytes or exceptions)."""

    def __init__(self, outcomes: list[bytes | Exception]) -> None:
        self._outcomes = list(outcomes)
        self.call_count = 0
        self.closed = False

    async def call(self, rpc: EngineRpc, request_bytes: bytes) -> bytes:
        del rpc, request_bytes
        self.call_count += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def close(self) -> None:
        self.closed = True


def _config(**overrides: Any) -> EngineClientConfig:
    base: dict[str, Any] = {
        "address": "engine:50051",
        "max_retries": 2,
        "backoff_base_s": 0.001,
        "backoff_cap_s": 0.01,
    }
    base.update(overrides)
    return EngineClientConfig(**base)


@pytest.mark.asyncio
async def test_retrying_client_passes_through_on_success() -> None:
    inner = _ScriptedClient([b"\x42"])
    client = RetryingEngineClient(inner, _config())

    result = await client.call(EngineRpc.PRICE_VANILLA_SWAP, b"req")
    assert result == b"\x42"
    assert inner.call_count == 1


@pytest.mark.asyncio
async def test_retrying_client_retries_retryable_errors() -> None:
    inner = _ScriptedClient(
        [
            EngineRetryableError("blip"),
            EngineRetryableError("blip2"),
            b"\x99",
        ]
    )
    client = RetryingEngineClient(inner, _config(max_retries=3))

    result = await client.call(EngineRpc.BOOTSTRAP_CURVES, b"req")
    assert result == b"\x99"
    assert inner.call_count == 3


@pytest.mark.asyncio
async def test_retrying_client_retries_timeouts() -> None:
    inner = _ScriptedClient(
        [
            EngineTimeoutError("slow"),
            b"\x55",
        ]
    )
    client = RetryingEngineClient(inner, _config(max_retries=1))

    result = await client.call(EngineRpc.PRICE_FRA, b"req")
    assert result == b"\x55"
    assert inner.call_count == 2


@pytest.mark.asyncio
async def test_retrying_client_does_not_retry_non_retryable() -> None:
    inner = _ScriptedClient(
        [
            EngineRpcError("invalid arg", code="INVALID_ARGUMENT"),
        ]
    )
    client = RetryingEngineClient(inner, _config(max_retries=5))

    with pytest.raises(EngineRpcError) as exc:
        await client.call(EngineRpc.PRICE_SWAPTION, b"req")
    assert exc.value.code == "INVALID_ARGUMENT"
    assert inner.call_count == 1


@pytest.mark.asyncio
async def test_retrying_client_gives_up_after_max_retries() -> None:
    inner = _ScriptedClient(
        [
            EngineRetryableError("nope-1"),
            EngineRetryableError("nope-2"),
            EngineRetryableError("nope-3"),
        ]
    )
    client = RetryingEngineClient(inner, _config(max_retries=2))

    with pytest.raises(EngineRetryableError, match="nope-3"):
        await client.call(EngineRpc.PRICE_VANILLA_SWAP, b"req")
    assert inner.call_count == 3  # initial + 2 retries


@pytest.mark.asyncio
async def test_retrying_client_close_propagates() -> None:
    inner = _ScriptedClient([b""])
    client = RetryingEngineClient(inner, _config())

    async with client:
        pass

    assert inner.closed is True
