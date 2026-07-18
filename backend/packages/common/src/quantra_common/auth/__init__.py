"""Authentication primitives for Quantra services.

Two auth modes are supported:

- **Firebase ID tokens** (``Authorization: Bearer <token>``) — used by the
  portal and any user-facing client.
- **API keys** (``X-API-Key: <key>``) — used by Python/external clients.

This module exposes typed verifiers and an ``AuthContext`` carried through
FastAPI's request scope. The API-key *lookup* is pluggable (see
``ApiKeyLookup``): the orchestrator provides a database-backed lookup over
its ``app.api_keys`` table.
"""

from __future__ import annotations

from quantra_common.auth.context import ApiKeyRecord, AuthContext, AuthType
from quantra_common.auth.firebase import (
    FirebaseTokenVerifier,
    default_firebase_verifier,
    verify_firebase_id_token,
)
from quantra_common.auth.lookup import ApiKeyLookup, verify_api_key

__all__ = [
    "ApiKeyLookup",
    "ApiKeyRecord",
    "AuthContext",
    "AuthType",
    "FirebaseTokenVerifier",
    "default_firebase_verifier",
    "verify_api_key",
    "verify_firebase_id_token",
]
