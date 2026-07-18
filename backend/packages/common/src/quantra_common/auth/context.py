"""`AuthContext` carried through every authenticated request."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class AuthType(StrEnum):
    FIREBASE = "firebase"
    API_KEY = "api_key"


class AuthContext(BaseModel):
    """Identity + tier of the calling principal.

    Returned by the verifiers and injected into request handlers via
    ``require_auth()``. Treat fields like ``email`` / ``name`` as optional —
    legacy API keys may not have them populated, and Firebase users may sign
    up without an email if they used a phone provider.
    """

    model_config = ConfigDict(frozen=True)

    auth_type: AuthType
    uid: str = Field(
        min_length=1,
        description="Stable user identifier (Firebase UID or API-key owner UID).",
    )
    email: str | None = None
    name: str | None = None
    tier: str = Field(default="free", description="Subscription/feature tier label.")
    api_key_id: str | None = Field(
        default=None,
        description="Set when auth_type is API_KEY; identifies the key document/row.",
    )


class ApiKeyRecord(BaseModel):
    """Result of an `ApiKeyLookup` call.

    Decoupled from any specific storage so the orchestrator can ship its own
    lookup (Postgres `app.api_keys` row → ApiKeyRecord) without depending on
    a database schema this plan deliberately does not add.
    """

    model_config = ConfigDict(frozen=True)

    api_key_id: str
    owner_uid: str
    name: str = "API Key"
    email: str | None = None
    tier: str = "free"
    active: bool = True
