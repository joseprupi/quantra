"""``/auth/*`` identity routes for the orchestrator.

* ``GET /auth/whoami`` — a client-facing echo of the resolved
  ``AuthContext`` so portal / CLI / API callers can verify their
  credentials wire up end-to-end.
* ``POST /auth/provision`` — idempotent upsert of the caller's
  ``app.users`` row. Every entity table FKs
  ``owner_uid -> app.users.uid`` but nothing used to create the row, so
  a fresh identity's first write 500'd on the FK. The portal calls this
  once after login; the demo seed script calls it before seeding.

Both routes require auth (401 with the ``code="unauthenticated"``
envelope); ``/health`` remains the only anonymous endpoint.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_common.auth.context import AuthContext
from quantra_orchestrator.auth.dependencies import get_auth_context
from quantra_orchestrator.data.engines import get_app_rw_engine
from quantra_orchestrator.schemas import ProvisionResponse, WhoamiResponse

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get(
    "/whoami",
    response_model=WhoamiResponse,
    responses={401: {"description": "Missing or invalid credentials."}},
)
async def whoami(
    ctx: AuthContext = Depends(get_auth_context),
) -> WhoamiResponse:
    """Echo the resolved identity for the authenticated caller.

    Useful for confirming that a Firebase ID token or X-API-Key reaches
    the orchestrator's auth layer and resolves to the expected user.
    """

    return WhoamiResponse(
        uid=ctx.uid,
        email=ctx.email,
        auth_method=ctx.auth_type.value,
    )


@router.post(
    "/provision",
    response_model=ProvisionResponse,
    responses={
        401: {"description": "Missing or invalid credentials."},
        503: {"description": "Persistent storage is not configured."},
    },
)
async def provision(
    ctx: AuthContext = Depends(get_auth_context),
    rw_engine: AsyncEngine = Depends(get_app_rw_engine),
) -> ProvisionResponse:
    """Ensure the caller's ``app.users`` row exists (idempotent upsert).

    On conflict the row is kept; email / display_name are refreshed from
    the credential only when the credential actually carries them
    (``COALESCE`` keeps existing values otherwise). ``tier`` is never
    touched here — it belongs to account management, not auth.
    """

    sql = text(
        "INSERT INTO app.users (uid, email, display_name) "
        "VALUES (:uid, :email, :display_name) "
        "ON CONFLICT (uid) DO UPDATE SET "
        "  email = COALESCE(EXCLUDED.email, app.users.email), "
        "  display_name = COALESCE(EXCLUDED.display_name, app.users.display_name) "
        "RETURNING uid, email, display_name, tier"
    )
    binds = {"uid": ctx.uid, "email": ctx.email, "display_name": ctx.name}
    async with rw_engine.begin() as conn:
        result = await conn.execute(sql, binds)
        row = result.mappings().one()
    return ProvisionResponse.model_validate(dict(row))
