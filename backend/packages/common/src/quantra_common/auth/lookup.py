"""API-key verification.

The actual storage of API keys lives in ``app.api_keys`` (Postgres).
To keep this module self-contained, ``verify_api_key`` takes an
injectable ``ApiKeyLookup`` callable so services can wire whatever
store they have; the orchestrator provides ``SqlApiKeyLookup`` backed
by ``app.api_keys``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from quantra_common.auth.context import ApiKeyRecord, AuthContext, AuthType

_logger = logging.getLogger(__name__)

type ApiKeyLookup = Callable[[str], Awaitable[ApiKeyRecord | None]]
"""Resolve a raw API key string into an `ApiKeyRecord` or None if unknown."""


async def verify_api_key(
    key: str,
    lookup: ApiKeyLookup,
) -> AuthContext | None:
    """Resolve a raw API key, returning `None` for unknown / inactive keys.

    The lookup is awaited; any exception bubbles up (the caller's middleware
    decides how to render a 5xx). Inactive keys deliberately collapse to the
    same ``None`` as unknown keys so callers don't accidentally leak which
    keys have ever existed.
    """

    if not key:
        return None

    record = await lookup(key)
    if record is None:
        _logger.debug("api key lookup miss")
        return None
    if not record.active:
        _logger.debug("api key lookup hit but inactive", extra={"api_key_id": record.api_key_id})
        return None

    return AuthContext(
        auth_type=AuthType.API_KEY,
        uid=record.owner_uid,
        email=record.email,
        name=record.name,
        tier=record.tier,
        api_key_id=record.api_key_id,
    )
