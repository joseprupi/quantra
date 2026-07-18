"""Data-layer module for the orchestrator.

Hosts the generic CRUD repository helpers, per-entity ``EntitySpec``
descriptors, Pydantic request/response shapes, and the FastAPI router
factory that turns an ``EntitySpec`` + schema pair into the six
canonical endpoints documented
(create / list / read / patch / soft-delete / restore).

Read patterns route through ``make_app_engine("ro")`` and writes through
``make_app_engine("rw")`` — engines are owned by the FastAPI lifespan
(``quantra_orchestrator.app.create_app``) and exposed to handlers via
``Depends(get_app_ro_engine)`` / ``Depends(get_app_rw_engine)``.

All queries are unconditionally ``owner_uid``-scoped against
``AuthContext.uid`` — a missing ``WHERE owner_uid = :uid`` clause is a
privilege bug, not a performance one.
"""

from quantra_orchestrator.data.engines import (
    AppDbUnavailableError,
    get_app_ro_engine,
    get_app_rw_engine,
)
from quantra_orchestrator.data.errors import (
    NameConflictError,
    NotFoundError,
)
from quantra_orchestrator.data.repository import (
    CrudRepository,
    EntitySpec,
    ImmutableRepository,
)

__all__ = [
    "AppDbUnavailableError",
    "CrudRepository",
    "EntitySpec",
    "ImmutableRepository",
    "NameConflictError",
    "NotFoundError",
    "get_app_ro_engine",
    "get_app_rw_engine",
]
