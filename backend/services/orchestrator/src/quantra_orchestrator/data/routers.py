"""FastAPI router factories for the data layer.

``build_crud_router`` turns an ``EntitySpec`` + (Create / Update /
Response) Pydantic triple into the six canonical endpoints documented
 (create / list / read / patch /
soft-delete / restore). The fourteen named entities share this builder.

``build_immutable_router`` covers ``pricing_history`` (list + get only).

All endpoints unconditionally call ``Depends(get_auth_context)`` and
forward the resolved ``owner_uid`` into the repository — there is no
"list all entities, any owner" path on this surface.

Note: this module deliberately omits ``from __future__ import annotations``.
FastAPI / pydantic v2 need the Pydantic class objects bound to the
parameter-annotation names at function-definition time so that
``payload: create_cls`` resolves to the concrete request model rather
than the literal string ``'create_cls'``. With PEP-563 deferred
evaluation enabled, the closure-captured names are unreachable to the
introspection code (it would have to walk the function's free
variables, which neither library does). The annotations the file does
use (``list[X]``, ``dict[X, Y]``, ``X | Y``) all work natively in
Python 3.12 without the future import.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from pydantic import create_model as pydantic_create_model
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_common.auth.context import AuthContext
from quantra_orchestrator.auth.dependencies import get_auth_context
from quantra_orchestrator.data.engines import (
    get_app_ro_engine,
    get_app_rw_engine,
)
from quantra_orchestrator.data.repository import (
    CrudRepository,
    EntitySpec,
    ImmutableRepository,
)
from quantra_orchestrator.data.schemas import (
    EntityVersionDetail,
    EntityVersionList,
    EntityVersionSummary,
    PageMeta,
)

# Pagination caps —. ``DEFAULT_LIMIT`` keeps a fresh list call
# under a single TCP packet's worth of JSON for the typical entity
# size; ``MAX_LIMIT`` is a hard wall to prevent unbounded scans.
DEFAULT_LIMIT: int = 50
MAX_LIMIT: int = 200


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------


def _common_responses() -> dict[int | str, dict[str, Any]]:
    """Document the structured-error error responses every protected route emits."""

    return {
        401: {"description": "Missing or invalid credentials."},
        503: {"description": "Persistent storage is not configured."},
    }


# The optional ``X-Change-Reason`` header every mutating route accepts.
# The value is recorded verbatim as ``change_reason`` on the
# ``app.entity_versions`` row the write appends, so an auditor can see
# WHY an entity changed, not just what changed. (Documented in OpenAPI
# via the Header parameter's description on each mutating route.)
_CHANGE_REASON_DESCRIPTION: str = (
    "Optional human-readable reason for this change. Recorded "
    "immutably in the entity's audit trail (see "
    "``GET .../{resource_id}/versions``)."
)


def _make_page_model(item_model: type[BaseModel]) -> type[BaseModel]:
    """Build a concrete ``ListPage[X]`` subclass.

    pydantic v2's PEP-695 generic ``class ListPage[T]`` confuses FastAPI's
    response-model evaluator when ``T`` is a local function parameter
    (the forward-ref's enclosing scope no longer has ``T`` bound). We
    sidestep that by minting a concrete model per entity instead — same
    JSON shape, no generic resolution at request time.
    """

    return pydantic_create_model(
        f"ListPage_{item_model.__name__}",
        __config__=ConfigDict(extra="forbid"),
        items=(list[item_model], Field(default_factory=list)),  # type: ignore[valid-type]
        page=(PageMeta, ...),
    )


# ---------------------------------------------------------------------------
# Named-entity (standard template) factory
# ---------------------------------------------------------------------------


def build_crud_router(
    *,
    spec: EntitySpec,
    prefix: str,
    tag: str,
    create_model: type[BaseModel],
    update_model: type[BaseModel],
    response_model: type[BaseModel],
) -> APIRouter:
    """Build the six-endpoint CRUD router for one named entity.

    All routes:

    * Require auth (``Depends(get_auth_context)``).
    * Scope every query to ``ctx.uid`` — there is no path that lists or
      reads rows owned by a different user.
    * Translate ``NotFoundError`` / ``NameConflictError`` into the structured-error
      envelope automatically via the shared HTTPException handler.
    """

    router = APIRouter(prefix=prefix, tags=[tag], responses=_common_responses())
    page_model = _make_page_model(response_model)
    # FastAPI inspects the function's type annotations at registration
    # time; binding the model classes to local names with these aliases
    # is what makes the forward refs resolve in the closure's scope.
    create_cls = create_model
    update_cls = update_model

    @router.post(
        "",
        status_code=status.HTTP_201_CREATED,
        response_model=response_model,
        summary=f"Create a {spec.entity}",
    )
    async def _create(
        payload: create_cls,  # type: ignore[valid-type]
        ctx: AuthContext = Depends(get_auth_context),
        rw_engine: AsyncEngine = Depends(get_app_rw_engine),
        ro_engine: AsyncEngine = Depends(get_app_ro_engine),
        change_reason: str | None = Header(
            default=None, alias="X-Change-Reason", description=_CHANGE_REASON_DESCRIPTION
        ),
    ) -> Any:  # noqa: ANN401 -- response_model handles serialisation
        repo = CrudRepository(spec, rw_engine=rw_engine, ro_engine=ro_engine)
        values = payload.model_dump()  # type: ignore[attr-defined]
        row = await repo.create(
            owner_uid=ctx.uid,
            values=values,
            actor_uid=ctx.uid,
            actor_email=ctx.email,
            change_reason=change_reason,
        )
        return response_model.model_validate(row)

    @router.get(
        "",
        response_model=page_model,
        summary=f"List {spec.entity} rows owned by the caller",
    )
    async def _list(
        ctx: AuthContext = Depends(get_auth_context),
        ro_engine: AsyncEngine = Depends(get_app_ro_engine),
        limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
        offset: int = Query(default=0, ge=0),
    ) -> Any:  # noqa: ANN401
        repo = CrudRepository(spec, ro_engine=ro_engine)
        rows = await repo.list(owner_uid=ctx.uid, limit=limit, offset=offset)
        items = [response_model.model_validate(row) for row in rows]
        page = PageMeta(limit=limit, offset=offset, has_more=len(items) == limit)
        return page_model(items=items, page=page)

    @router.get(
        "/{resource_id}",
        response_model=response_model,
        responses={404: {"description": "Not found / not owned / soft-deleted."}},
        summary=f"Fetch one {spec.entity}",
    )
    async def _read(
        resource_id: UUID,
        ctx: AuthContext = Depends(get_auth_context),
        ro_engine: AsyncEngine = Depends(get_app_ro_engine),
    ) -> Any:  # noqa: ANN401
        repo = CrudRepository(spec, ro_engine=ro_engine)
        row = await repo.get(owner_uid=ctx.uid, resource_id=resource_id)
        return response_model.model_validate(row)

    @router.patch(
        "/{resource_id}",
        response_model=response_model,
        responses={
            404: {"description": "Not found / not owned / soft-deleted."},
            409: {"description": "Name conflict with a live row owned by the caller."},
        },
        summary=f"Partial update of a {spec.entity}",
    )
    async def _patch(
        resource_id: UUID,
        payload: update_cls,  # type: ignore[valid-type]
        ctx: AuthContext = Depends(get_auth_context),
        rw_engine: AsyncEngine = Depends(get_app_rw_engine),
        ro_engine: AsyncEngine = Depends(get_app_ro_engine),
        change_reason: str | None = Header(
            default=None, alias="X-Change-Reason", description=_CHANGE_REASON_DESCRIPTION
        ),
    ) -> Any:  # noqa: ANN401
        repo = CrudRepository(spec, rw_engine=rw_engine, ro_engine=ro_engine)
        updates = payload.model_dump(exclude_unset=True)  # type: ignore[attr-defined]
        row = await repo.patch(
            owner_uid=ctx.uid,
            resource_id=resource_id,
            updates=updates,
            actor_uid=ctx.uid,
            actor_email=ctx.email,
            change_reason=change_reason,
        )
        return response_model.model_validate(row)

    @router.delete(
        "/{resource_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        responses={404: {"description": "Not found / not owned."}},
        summary=f"Soft-delete a {spec.entity}",
    )
    async def _delete(
        resource_id: UUID,
        ctx: AuthContext = Depends(get_auth_context),
        rw_engine: AsyncEngine = Depends(get_app_rw_engine),
        ro_engine: AsyncEngine = Depends(get_app_ro_engine),
        change_reason: str | None = Header(
            default=None, alias="X-Change-Reason", description=_CHANGE_REASON_DESCRIPTION
        ),
    ) -> Response:
        repo = CrudRepository(spec, rw_engine=rw_engine, ro_engine=ro_engine)
        await repo.soft_delete(
            owner_uid=ctx.uid,
            resource_id=resource_id,
            actor_uid=ctx.uid,
            actor_email=ctx.email,
            change_reason=change_reason,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post(
        "/{resource_id}:restore",
        response_model=response_model,
        responses={
            404: {"description": "Not found / not owned."},
            409: {"description": "Name conflict with a live row owned by the caller."},
        },
        summary=f"Restore a soft-deleted {spec.entity}",
    )
    async def _restore(
        resource_id: UUID,
        ctx: AuthContext = Depends(get_auth_context),
        rw_engine: AsyncEngine = Depends(get_app_rw_engine),
        ro_engine: AsyncEngine = Depends(get_app_ro_engine),
        change_reason: str | None = Header(
            default=None, alias="X-Change-Reason", description=_CHANGE_REASON_DESCRIPTION
        ),
    ) -> Any:  # noqa: ANN401
        repo = CrudRepository(spec, rw_engine=rw_engine, ro_engine=ro_engine)
        row = await repo.restore(
            owner_uid=ctx.uid,
            resource_id=resource_id,
            actor_uid=ctx.uid,
            actor_email=ctx.email,
            change_reason=change_reason,
        )
        return response_model.model_validate(row)

    # Audit trail (append-only ``app.entity_versions``) -----------------------

    @router.get(
        "/{resource_id}/versions",
        response_model=EntityVersionList,
        responses={404: {"description": "Not found / not owned."}},
        summary=f"List the version history of a {spec.entity}",
        description=(
            "Full amendment history of the entity, newest first: who "
            "changed it, when, what kind of change (create / amend / "
            "delete / restore), and the optional ``X-Change-Reason`` "
            "supplied with the write. Snapshots are fetched per version "
            "via ``GET .../versions/{version_no}``. Soft-deleted "
            "entities still list their history."
        ),
    )
    async def _list_versions(
        resource_id: UUID,
        ctx: AuthContext = Depends(get_auth_context),
        ro_engine: AsyncEngine = Depends(get_app_ro_engine),
    ) -> Any:  # noqa: ANN401
        repo = CrudRepository(spec, ro_engine=ro_engine)
        rows = await repo.list_versions(owner_uid=ctx.uid, resource_id=resource_id)
        return EntityVersionList(items=[EntityVersionSummary.model_validate(row) for row in rows])

    @router.get(
        "/{resource_id}/versions/{version_no}",
        response_model=EntityVersionDetail,
        responses={404: {"description": "Not found / not owned / no such version."}},
        summary=f"Fetch one version of a {spec.entity} (with full snapshot)",
        description=(
            "Returns the version's metadata plus ``payload`` — the full "
            "row snapshot after the change (for deletes: the row state "
            "at deletion). Diffs between versions are computed client-"
            "side."
        ),
    )
    async def _get_version(
        resource_id: UUID,
        version_no: int,
        ctx: AuthContext = Depends(get_auth_context),
        ro_engine: AsyncEngine = Depends(get_app_ro_engine),
    ) -> Any:  # noqa: ANN401
        repo = CrudRepository(spec, ro_engine=ro_engine)
        row = await repo.get_version(
            owner_uid=ctx.uid, resource_id=resource_id, version_no=version_no
        )
        return EntityVersionDetail.model_validate(row)

    return router


# ---------------------------------------------------------------------------
# Immutable view (pricing_history)
# ---------------------------------------------------------------------------


def build_immutable_router(
    *,
    spec: EntitySpec,
    prefix: str,
    tag: str,
    response_model: type[BaseModel],
) -> APIRouter:
    """Read-only router for ``app.pricing_history``.

    No POST / PATCH / DELETE / restore. Writes land via the future
    pricing flow; this surface only lists / reads.
    """

    router = APIRouter(prefix=prefix, tags=[tag], responses=_common_responses())
    page_model = _make_page_model(response_model)

    @router.get(
        "",
        response_model=page_model,
        summary=f"List {spec.entity} rows owned by the caller",
    )
    async def _list(
        ctx: AuthContext = Depends(get_auth_context),
        ro_engine: AsyncEngine = Depends(get_app_ro_engine),
        limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
        offset: int = Query(default=0, ge=0),
    ) -> Any:  # noqa: ANN401
        repo = ImmutableRepository(spec, ro_engine=ro_engine)
        rows = await repo.list(owner_uid=ctx.uid, limit=limit, offset=offset)
        items = [response_model.model_validate(row) for row in rows]
        page = PageMeta(limit=limit, offset=offset, has_more=len(items) == limit)
        return page_model(items=items, page=page)

    @router.get(
        "/{resource_id}",
        response_model=response_model,
        responses={404: {"description": "Not found / not owned."}},
        summary=f"Fetch one {spec.entity}",
    )
    async def _read(
        resource_id: UUID,
        ctx: AuthContext = Depends(get_auth_context),
        ro_engine: AsyncEngine = Depends(get_app_ro_engine),
    ) -> Any:  # noqa: ANN401
        repo = ImmutableRepository(spec, ro_engine=ro_engine)
        row = await repo.get(owner_uid=ctx.uid, resource_id=resource_id)
        return response_model.model_validate(row)

    return router


__all__ = [
    "build_crud_router",
    "build_immutable_router",
]
