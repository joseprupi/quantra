"""FastAPI auth dependency for the orchestrator.

Composes the two cross-service primitives from ``quantra_common.auth``:

* ``verify_firebase_id_token`` for ``Authorization: Bearer <jwt>``.
* ``verify_api_key`` for ``X-API-Key: <key>``, delegating the lookup to
  an injectable ``ApiKeyLookup`` — in production
  ``SqlApiKeyLookup``; in tests, an in-memory fake.

``UnauthenticatedError`` carries a class attribute ``code`` so the
shared HTTPException handler in ``quantra_orchestrator.errors`` can
emit the envelope with ``"code": "unauthenticated"`` instead of the
default ``http_401``. We deliberately keep the dependency missing-
credential and invalid-credential outcomes both behind the same
``unauthenticated`` code: the orchestrator must not leak which credential
type the caller supplied (defense against probing).

``get_auth_context`` is the single dependency endpoints declare via
``Depends``. Two helper dependencies (``get_api_key_lookup`` /
``get_firebase_verifier``) read from ``request.app.state`` so the
factory can swap them per-app without going through the module-level
``Settings`` singleton — the same override seam the settings use.
"""

from __future__ import annotations

from typing import Annotated, Final

import structlog
from fastapi import Depends, Header, HTTPException, Request, status

from quantra_common.auth.context import AuthContext, AuthType
from quantra_common.auth.firebase import (
    FirebaseTokenVerifier,
    verify_firebase_id_token,
)
from quantra_common.auth.lookup import ApiKeyLookup, verify_api_key
from quantra_orchestrator.settings import (
    OrchestratorSettings,
    get_orchestrator_settings,
)

_log = structlog.get_logger(__name__)

AUTH_FAILURE_CODE: Final[str] = "unauthenticated"
"""error envelope ``code`` emitted by the 401 handler for any auth failure."""

DEV_BYPASS_UID: Final[str] = "dev-user"
"""Fixed UID returned for every request when ``DEV_AUTH_BYPASS`` is on."""

DEV_BYPASS_EMAIL: Final[str] = "dev@quantra.local"
"""Fixed email returned for every request when ``DEV_AUTH_BYPASS`` is on."""


def _dev_bypass_context() -> AuthContext:
    """Build the fixed development principal used by the auth bypass.

    Only ever constructed when ``settings.dev_auth_bypass`` is true — a
    flag that defaults OFF and must be explicitly set via the
    ``DEV_AUTH_BYPASS`` env var. The principal is typed as a Firebase
    identity so ``/auth/whoami`` and any downstream owner_uid scoping
    behaves exactly as it would for a real signed-in user.
    """

    return AuthContext(
        auth_type=AuthType.FIREBASE,
        uid=DEV_BYPASS_UID,
        email=DEV_BYPASS_EMAIL,
        name="Dev User",
    )


class UnauthenticatedError(HTTPException):
    """401 raised when no valid Firebase token / API key is present.

    The shared HTTPException handler reads the ``code`` class attribute
    and surfaces it in the envelope. Keeping the exception a plain
    ``HTTPException`` subclass means the existing dispatch path lights it
    up without a second handler registration.
    """

    code: Final[str] = AUTH_FAILURE_CODE

    def __init__(self, *, detail: str = "Authentication required.") -> None:
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_api_key_lookup(request: Request) -> ApiKeyLookup | None:
    """Resolve the per-app ``ApiKeyLookup`` from app state, or ``None``.

    Returning ``None`` is a deliberate fail-closed signal: the
    dependency converts it into the same 401 a missing credential would
    produce, so a misconfigured deploy can't accidentally allow API-key
    auth to succeed without a backing store.
    """

    return getattr(request.app.state, "api_key_lookup", None)


def get_firebase_verifier(request: Request) -> FirebaseTokenVerifier | None:
    """Resolve the per-app Firebase verifier from app state, or ``None``.

    ``None`` means "use the default verifier that lazily initializes
    ``firebase_admin``" — see ``quantra_common.auth.firebase``.
    """

    return getattr(request.app.state, "firebase_verifier", None)


async def get_auth_context(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    api_key_lookup: ApiKeyLookup | None = Depends(get_api_key_lookup),
    firebase_verifier: FirebaseTokenVerifier | None = Depends(get_firebase_verifier),
    settings: OrchestratorSettings = Depends(get_orchestrator_settings),
) -> AuthContext:
    """Resolve the calling principal or raise ``UnauthenticatedError``.

    Resolution order:

    1. ``Authorization: Bearer <jwt>`` — verified via Firebase.
    2. ``X-API-Key: <key>`` — verified via the per-app ``ApiKeyLookup``.
    3. Otherwise: 401 with ``code="unauthenticated"``.

    The function logs a structured ``orchestrator.auth.success`` /
    ``orchestrator.auth.failure`` event so the failure reason is
    observable in logs without leaking it to the response envelope.
    """

    if settings.dev_auth_bypass:
        dev_ctx = _dev_bypass_context()
        _log.warning(
            "orchestrator.auth.dev_bypass",
            method=request.method,
            path=request.url.path,
            uid=dev_ctx.uid,
        )
        return dev_ctx

    ctx, reason = await _try_authenticate(
        authorization=authorization,
        x_api_key=x_api_key,
        api_key_lookup=api_key_lookup,
        firebase_verifier=firebase_verifier,
        settings=settings,
    )
    if ctx is not None:
        _log.info(
            "orchestrator.auth.success",
            method=request.method,
            path=request.url.path,
            auth_type=ctx.auth_type.value,
            uid=ctx.uid,
            api_key_id=ctx.api_key_id,
        )
        return ctx

    _log.info(
        "orchestrator.auth.failure",
        method=request.method,
        path=request.url.path,
        reason=reason,
    )
    raise UnauthenticatedError(detail=_DETAILS[reason])


async def _try_authenticate(
    *,
    authorization: str | None,
    x_api_key: str | None,
    api_key_lookup: ApiKeyLookup | None,
    firebase_verifier: FirebaseTokenVerifier | None,
    settings: OrchestratorSettings,
) -> tuple[AuthContext | None, str]:
    if authorization and authorization.startswith("Bearer "):
        return await _try_bearer(authorization, firebase_verifier, settings)
    if x_api_key:
        return await _try_api_key(x_api_key, api_key_lookup)
    return None, "missing_credentials"


async def _try_bearer(
    authorization: str,
    verifier: FirebaseTokenVerifier | None,
    settings: OrchestratorSettings,
) -> tuple[AuthContext | None, str]:
    token = authorization[len("Bearer ") :].strip()
    if not token:
        return None, "invalid_bearer"
    ctx = await verify_firebase_id_token(token, verifier=verifier, settings=settings)
    if ctx is None:
        return None, "invalid_bearer"
    return ctx, "ok"


async def _try_api_key(
    raw_key: str,
    lookup: ApiKeyLookup | None,
) -> tuple[AuthContext | None, str]:
    if lookup is None:
        return None, "api_key_unavailable"
    ctx = await verify_api_key(raw_key.strip(), lookup)
    if ctx is None:
        return None, "invalid_api_key"
    return ctx, "ok"


_DETAILS: Final[dict[str, str]] = {
    "invalid_bearer": "Invalid or expired bearer token.",
    "invalid_api_key": "Invalid or inactive API key.",
    "api_key_unavailable": "Authentication required.",
    "missing_credentials": "Authentication required.",
}


__all__ = [
    "AUTH_FAILURE_CODE",
    "UnauthenticatedError",
    "get_api_key_lookup",
    "get_auth_context",
    "get_firebase_verifier",
]
