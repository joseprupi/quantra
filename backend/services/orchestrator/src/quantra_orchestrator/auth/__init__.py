"""Orchestrator-local auth wiring.

The abstract ``ApiKeyLookup`` protocol and the cross-service helpers
(``AuthContext``, Firebase verifier, ``verify_api_key``) live in
``quantra_common.auth``. This subpackage hosts the orchestrator's:

* ``SqlApiKeyLookup`` — concrete ``ApiKeyLookup`` backed by the
  ``app.api_keys`` table. Service-local because no second
  consumer exists.
* ``get_auth_context`` — FastAPI dependency that resolves either a
  ``Bearer`` token or an ``X-API-Key`` header into an ``AuthContext``,
  raising ``UnauthenticatedError`` (rendered as a structured-error 401
  envelope) on failure.
"""

from __future__ import annotations

from quantra_orchestrator.auth.api_keys import SqlApiKeyLookup, hash_api_key
from quantra_orchestrator.auth.dependencies import (
    AUTH_FAILURE_CODE,
    UnauthenticatedError,
    get_api_key_lookup,
    get_auth_context,
    get_firebase_verifier,
)

__all__ = [
    "AUTH_FAILURE_CODE",
    "SqlApiKeyLookup",
    "UnauthenticatedError",
    "get_api_key_lookup",
    "get_auth_context",
    "get_firebase_verifier",
    "hash_api_key",
]
