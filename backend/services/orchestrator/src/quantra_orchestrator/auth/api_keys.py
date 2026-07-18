"""SQL-backed ``ApiKeyLookup`` against ``app.api_keys``.

The abstract callable Protocol lives in ``quantra_common.auth.lookup`` so
the verifier can stay storage-agnostic. This module ships the
orchestrator's concrete adapter — service-local because no other consumer
of ``app.*`` exists yet (same instinct).

Lookups go through the ``app_ro`` engine: key verification is a pure
read, and routing it through the read-only role keeps the request path
honest about its privilege envelope. Hashing matches the
``0002_users_and_api_keys`` migration — SHA-256 hex digest. We
re-compare the stored hex against the recomputed hex with
``hmac.compare_digest`` for constant-time semantics, even though the
unique index has already done the equality check; this defends against a
future migration that swaps the storage format without revisiting the
comparison.

The lookup additionally joins ``app.users`` so the returned
``ApiKeyRecord`` carries the owner's email and tier. Without the join we
would have to do a second query (or punt user metadata onto a later
plan) and we would not catch the dangling-FK case if one ever slipped
past the ``ON DELETE RESTRICT`` — joining now keeps us
fail-closed.
"""

from __future__ import annotations

import hashlib
import hmac

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_common.auth.context import ApiKeyRecord

_log = structlog.get_logger(__name__)


def hash_api_key(raw_key: str) -> str:
    """Return the lowercase hex SHA-256 digest of ``raw_key``.

    The orchestrator never persists raw keys; the digest produced here is
    what the ``app.api_keys.key_hash`` column stores. Same algorithm and
    encoding the legacy auth proxy used.
    """

    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


_LOOKUP_SQL = text(
    """
    SELECT k.id::text       AS api_key_id,
           k.owner_uid      AS owner_uid,
           k.name           AS key_name,
           k.key_hash       AS key_hash,
           k.active         AS active,
           k.revoked_at     AS revoked_at,
           u.email          AS owner_email,
           u.tier           AS owner_tier
      FROM app.api_keys AS k
      JOIN app.users    AS u ON u.uid = k.owner_uid
     WHERE k.key_hash = :digest
    """
)


class SqlApiKeyLookup:
    """Resolve an X-API-Key value against ``app.api_keys`` + ``app.users``.

    Conforms to the ``ApiKeyLookup`` callable Protocol from
    ``quantra_common.auth``. Construct with a ``make_app_engine("ro")``
    engine; the orchestrator's lifespan owns disposal.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def __call__(self, key: str) -> ApiKeyRecord | None:
        if not key:
            return None

        digest = hash_api_key(key)

        async with self._engine.connect() as conn:
            result = await conn.execute(_LOOKUP_SQL, {"digest": digest})
            row = result.mappings().one_or_none()

        if row is None:
            return None

        stored_hash = str(row["key_hash"])
        if not hmac.compare_digest(stored_hash, digest):
            _log.warning(
                "orchestrator.auth.api_key.hash_mismatch",
                api_key_id=str(row["api_key_id"]),
            )
            return None

        active = bool(row["active"]) and row["revoked_at"] is None

        return ApiKeyRecord(
            api_key_id=str(row["api_key_id"]),
            owner_uid=str(row["owner_uid"]),
            name=str(row["key_name"]) if row["key_name"] else "API Key",
            email=_optional_str(row["owner_email"]),
            tier=str(row["owner_tier"]) if row["owner_tier"] else "free",
            active=active,
        )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


__all__ = ["SqlApiKeyLookup", "hash_api_key"]
