"""Tests for `quantra_common.auth`."""

from __future__ import annotations

from typing import Any

import pytest

from quantra_common.auth import (
    ApiKeyLookup,
    ApiKeyRecord,
    AuthContext,
    AuthType,
    verify_api_key,
    verify_firebase_id_token,
)

# ----- verify_firebase_id_token ---------------------------------------------


@pytest.mark.asyncio
async def test_verify_firebase_id_token_happy_path() -> None:
    sentinel = "fake-firebase-id-token"

    def fake_verifier(token: str) -> dict[str, Any]:
        assert token == sentinel
        return {"uid": "u-1", "email": "a@b.c", "name": "Alice", "tier": "pro"}

    ctx = await verify_firebase_id_token(sentinel, verifier=fake_verifier)

    assert isinstance(ctx, AuthContext)
    assert ctx.auth_type is AuthType.FIREBASE
    assert ctx.uid == "u-1"
    assert ctx.email == "a@b.c"
    assert ctx.name == "Alice"
    assert ctx.tier == "pro"


@pytest.mark.asyncio
async def test_verify_firebase_id_token_returns_none_on_verifier_exception() -> None:
    def boom(_token: str) -> dict[str, Any]:
        raise RuntimeError("revoked")

    assert await verify_firebase_id_token("x", verifier=boom) is None


@pytest.mark.asyncio
async def test_verify_firebase_id_token_handles_missing_uid() -> None:
    def no_uid(_token: str) -> dict[str, Any]:
        return {"email": "x@y.z"}

    assert await verify_firebase_id_token("x", verifier=no_uid) is None


@pytest.mark.asyncio
async def test_verify_firebase_id_token_empty_token_short_circuits() -> None:
    called = False

    def verifier(_token: str) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    assert await verify_firebase_id_token("", verifier=verifier) is None
    assert not called


# ----- verify_api_key --------------------------------------------------------


def _lookup_returning(record: ApiKeyRecord | None) -> ApiKeyLookup:
    async def _lookup(_key: str) -> ApiKeyRecord | None:
        return record

    return _lookup


@pytest.mark.asyncio
async def test_verify_api_key_happy_path() -> None:
    rec = ApiKeyRecord(
        api_key_id="k1",
        owner_uid="u-7",
        email="bob@x.io",
        tier="enterprise",
    )
    ctx = await verify_api_key("raw-key", _lookup_returning(rec))
    assert ctx is not None
    assert ctx.auth_type is AuthType.API_KEY
    assert ctx.uid == "u-7"
    assert ctx.api_key_id == "k1"
    assert ctx.tier == "enterprise"


@pytest.mark.asyncio
async def test_verify_api_key_unknown() -> None:
    assert await verify_api_key("raw", _lookup_returning(None)) is None


@pytest.mark.asyncio
async def test_verify_api_key_inactive() -> None:
    rec = ApiKeyRecord(api_key_id="k1", owner_uid="u-7", active=False)
    assert await verify_api_key("raw", _lookup_returning(rec)) is None


@pytest.mark.asyncio
async def test_verify_api_key_empty_key() -> None:
    called = False

    async def _lookup(_key: str) -> ApiKeyRecord | None:
        nonlocal called
        called = True
        return None

    assert await verify_api_key("", _lookup) is None
    assert not called
