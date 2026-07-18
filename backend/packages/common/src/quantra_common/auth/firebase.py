"""Firebase ID token verification.

`firebase_admin` is sync and uses thread-local state for its app cache. We
wrap the verify call in ``asyncio.to_thread`` so it doesn't block the
event loop. The default verifier lazily initializes a default
``firebase_admin`` app the first time it's called; tests usually inject a
custom ``FirebaseTokenVerifier`` instead.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, cast

import firebase_admin  # type: ignore[import-untyped]
from firebase_admin import auth as firebase_auth

from quantra_common.auth.context import AuthContext, AuthType
from quantra_common.settings.base import Settings

_logger = logging.getLogger(__name__)

type FirebaseTokenVerifier = Callable[[str], dict[str, Any]]
"""Sync callable that verifies an ID token and returns the decoded claims.

Matches the signature of :func:`firebase_admin.auth.verify_id_token`.
"""


def default_firebase_verifier(settings: Settings | None = None) -> FirebaseTokenVerifier:
    """Build a verifier that lazily initializes the default firebase_admin app.

    ``settings.firebase_project_id`` is honored if present (helps in
    environments where ``GOOGLE_APPLICATION_CREDENTIALS`` doesn't pin a
    project). Subsequent calls are cheap because firebase_admin caches.
    """

    cfg = settings or Settings()
    initialized = False

    def _verify(token: str) -> dict[str, Any]:
        nonlocal initialized
        if not initialized:
            try:
                opts = {"projectId": cfg.firebase_project_id} if cfg.firebase_project_id else None
                firebase_admin.initialize_app(options=opts)
            except ValueError:
                pass
            initialized = True
        return cast(dict[str, Any], firebase_auth.verify_id_token(token))

    return _verify


async def verify_firebase_id_token(
    token: str,
    *,
    verifier: FirebaseTokenVerifier | None = None,
    settings: Settings | None = None,
) -> AuthContext | None:
    """Verify an ID token and return an `AuthContext`, or `None` if invalid.

    Returns `None` (rather than raising) for any verifier exception so the
    caller can decide whether to fall back to API-key auth or 401.
    """

    if not token:
        return None

    verify = verifier or default_firebase_verifier(settings)

    try:
        claims = await asyncio.to_thread(verify, token)
    except Exception:
        _logger.debug("firebase token verification failed", exc_info=True)
        return None

    uid = claims.get("uid") or claims.get("user_id")
    if not uid:
        return None

    return AuthContext(
        auth_type=AuthType.FIREBASE,
        uid=str(uid),
        email=_optional_str(claims.get("email")),
        name=_optional_str(claims.get("name")),
        tier=str(claims.get("tier", "free")),
    )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
