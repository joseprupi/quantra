"""Generic CRUD repository for ``app.*`` named entities + immutable views.

The orchestrator's data layer follows a single template: every mutable user-data entity carries

* ``id UUID PRIMARY KEY``
* ``owner_uid TEXT NOT NULL REFERENCES app.users(uid)``
* one or more scalar columns (``name``, ``currency``, ``kind``, …)
* one or more ``JSONB`` columns (``body``, ``payload``, ``request``,
  ``points``, ``content``, ``entries``)
* ``created_at`` / ``updated_at`` (trigger-maintained) and
  ``deleted_at`` (NULL when live).

``CrudRepository`` is the single SQL-emitter every entity router calls
into. It is parameterised by ``EntitySpec`` so the same instance shape
serves all sixteen mutable entities; the alternative — sixteen
hand-written copies — would be a privilege-bug factory the first time
someone forgot the ``WHERE owner_uid =:uid`` clause.

``ImmutableRepository`` covers ``app.pricing_history``: no
updates, no soft delete, no trigger, only ``list`` and ``get``.

Both repositories return ``dict[str, Any]`` rows (asyncpg JSONB ↦ dict;
timestamps ↦ ``datetime``); per-entity routers translate to / from
Pydantic models. We intentionally stop at the raw row shape here so the
repository stays the privilege boundary and doesn't grow per-entity
type knowledge.
"""

from __future__ import annotations

import builtins
import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_orchestrator.data.errors import NameConflictError, NotFoundError


def _is_uuid_column(column: str) -> bool:
    return column == "id" or column.endswith("_id")


_SCALAR_TYPES: tuple[type, ...] = (
    str,
    int,
    float,
    bool,
    uuid.UUID,
    date,
    datetime,
    type(None),
)


@dataclass(frozen=True)
class EntitySpec:
    """Per-table layout descriptor that drives the generic repository.

    Attributes
    ----------
    entity:
        Human label (e.g. ``"curve"``). Used in 404 / 409 messages.
    table:
        Bare table name. Always lives under the ``app`` schema; the
        repository hard-codes that.
    scalar_columns:
        Top-level non-JSONB columns that participate in writes / reads
        (e.g. ``("name", "currency", "kind", "reference_date")``).
        ``id`` / ``owner_uid`` / timestamps are added by the repository.
    jsonb_columns:
        JSONB columns the repository serialises on write and projects
        on read. Order matters only for INSERT statement readability.
    has_soft_delete:
        ``True`` for the standard named-entity template; ``False`` for
        tables without a ``deleted_at`` column (``pricing_history``).
        Soft-delete-less tables still go through ``CrudRepository`` but
        reject ``soft_delete`` / ``restore``.
    name_column:
        Column whose value is surfaced in 409 ``name_conflict`` detail
        strings (typically ``"name"``; a business-key table may point it
        at another column). ``None`` disables conflict detection
        entirely.
    unique_constraint:
        Substring matched against the IntegrityError message to decide
        whether a unique violation is a 409 ``name_conflict`` (vs. some
        other constraint we shouldn't swallow).
    immutable_after_create:
        Columns that participate in projection but not in PATCH
        mutability — e.g. a business key within an owner that the user
        shouldn't be able to rename in place.
    wrapper_graph_cascade:
        ``True`` for the seven product tables. A
        portal "Save" persists TWO rows in the same product table: the
        wrapper row the user sees (``request->>'__wrapper__' = 'true'``)
        and the by-reference save-graph row it points at via
        ``request->>'appId'``. Deleting the wrapper alone orphaned the
        graph row; when this flag is set, ``soft_delete``
        cascades the same owner-scoped soft delete to the ``appId``
        sibling. Reference-data rows (curves / curve_sets) named in the
        wrapper's ``appGraph`` live in OTHER tables, may be shared, and
        are intentionally NOT cascaded here (see the residual).
    versioned:
        ``True`` (the default) enrolls the entity in the append-only
        ``app.entity_versions`` audit trail: every create / patch /
        soft-delete / restore inserts a version row (full post-change
        snapshot + actor + reason + request id) in the SAME transaction
        as the head-row write. ``False`` opts a spec out (e.g. the
        immutable ``pricing_history`` view, synthetic test specs whose
        tables never migrate the audit trail).
    """

    entity: str
    table: str
    scalar_columns: tuple[str, ...]
    jsonb_columns: tuple[str, ...] = ()
    has_soft_delete: bool = True
    has_updated_at: bool = True
    name_column: str | None = "name"
    unique_constraint: str | None = None
    immutable_after_create: tuple[str, ...] = field(default_factory=tuple)
    wrapper_graph_cascade: bool = False
    versioned: bool = True

    def __post_init__(self) -> None:
        if self.unique_constraint is None and self.name_column == "name":
            # Default convention from migrations 0003/0005/0006: the
            # partial-unique index for the named-entity template is
            # ``uq_<table>_owner_name_active``.
            object.__setattr__(
                self,
                "unique_constraint",
                f"uq_{self.table}_owner_name_active",
            )

    @property
    def projection_columns(self) -> tuple[str, ...]:
        """All columns the repository returns to callers, in stable order."""

        base: list[str] = ["id", *self.scalar_columns, *self.jsonb_columns]
        base.append("created_at")
        if self.has_updated_at:
            base.append("updated_at")
        if self.has_soft_delete:
            base.append("deleted_at")
        return tuple(base)

    @property
    def mutable_columns(self) -> tuple[str, ...]:
        """Columns that ``patch()`` may rewrite.

        Excludes the per-entity-immutable list so callers can't change
        a business-key column through PATCH.
        """

        return tuple(
            col
            for col in (*self.scalar_columns, *self.jsonb_columns)
            if col not in self.immutable_after_create
        )


def _format_projection(prefix: str, columns: tuple[str, ...]) -> str:
    """Render ``SELECT`` projection. UUID columns are cast to text.

    Asyncpg returns UUID columns as ``uuid.UUID`` instances; casting to
    text inside SQL keeps the wire shape JSON-serialisable without a
    second pass through Python.
    """

    parts: list[str] = []
    for column in columns:
        if _is_uuid_column(column):
            parts.append(f"{prefix}.{column}::text AS {column}")
        else:
            parts.append(f"{prefix}.{column}")
    return ", ".join(parts)


def _bind_value(column: str, value: Any) -> Any:  # noqa: ANN401 -- sql-binding edge
    """Serialise a Python value for a JSONB / scalar bind.

    JSONB columns receive ``json.dumps(...)`` strings; the SQL casts
    them via ``::jsonb``. Scalars pass through unchanged because the
    asyncpg dialect handles UUID / date / datetime natively.
    """

    if isinstance(value, (dict, list)):
        return json.dumps(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, _SCALAR_TYPES):
        return value
    msg = f"Unsupported bind value for column {column!r}: {type(value).__name__}"
    raise TypeError(msg)


# ---------------------------------------------------------------------------
# Append-only entity versioning (the audit trail)
# ---------------------------------------------------------------------------


def _current_request_id() -> str | None:
    """Read the request id ``RequestIdMiddleware`` bound for this request.

    The middleware binds ``request_id`` on ``structlog.contextvars`` for
    every HTTP request; repository calls made outside a request context
    (scripts, seeds, tests) see ``None`` and the version row's
    ``request_id`` column stays NULL.
    """

    bound = structlog.contextvars.get_contextvars()
    request_id = bound.get("request_id")
    return request_id if isinstance(request_id, str) and request_id else None


def _snapshot_json_default(value: Any) -> Any:  # noqa: ANN401 -- json.dumps default hook
    """Serialise row-projection values (timestamps, UUIDs, numerics) for JSONB."""

    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    msg = f"Unsupported snapshot value: {type(value).__name__}"
    raise TypeError(msg)


# Internal helper keys the wrapper-graph cascade adds to the delete
# RETURNING; they are plumbing, not row state, and must not leak into
# the persisted snapshot.
_SNAPSHOT_EXCLUDED_KEYS: frozenset[str] = frozenset({"_wrapper_flag", "_linked_app_id"})


def _snapshot_payload(row: dict[str, Any]) -> str:
    """Render the full-row snapshot stored in ``entity_versions.payload``."""

    cleaned = {k: v for k, v in row.items() if k not in _SNAPSHOT_EXCLUDED_KEYS}
    return json.dumps(cleaned, default=_snapshot_json_default)


# Version rows are appended with ``version_no = max + 1`` computed in the
# same transaction as the head-row write. The head-row UPDATE/INSERT takes
# the row lock first, so two writers amending the same entity serialise on
# the head row before reaching this INSERT; the UNIQUE
# (entity_type, entity_id, version_no) constraint is the belt-and-braces
# backstop.
_VERSION_INSERT_SQL = text(
    "INSERT INTO app.entity_versions ("
    "entity_type, entity_id, version_no, payload, change_type, "
    "change_reason, changed_by_uid, changed_by_email, request_id, owner_uid"
    ") "
    "SELECT :entity_type, CAST(:entity_id AS uuid), "
    "COALESCE(MAX(v.version_no), 0) + 1, "
    "CAST(:payload AS jsonb), :change_type, "
    ":change_reason, :changed_by_uid, :changed_by_email, :request_id, :owner_uid "
    "FROM app.entity_versions AS v "
    "WHERE v.entity_type = :entity_type AND v.entity_id = CAST(:entity_id AS uuid)"
)


class CrudRepository:
    """Owner-scoped CRUD for a single ``app.*`` named entity.

    All public methods take an ``owner_uid`` explicitly and embed it in
    every ``WHERE`` clause. Callers (per-entity routers) pass the
    authenticated principal's UID; there is no "list all entities, any
    owner" path on this class.
    """

    def __init__(
        self,
        spec: EntitySpec,
        *,
        rw_engine: AsyncEngine | None = None,
        ro_engine: AsyncEngine | None = None,
    ) -> None:
        self._spec = spec
        self._rw = rw_engine
        self._ro = ro_engine

    @property
    def spec(self) -> EntitySpec:
        return self._spec

    @property
    def rw_engine(self) -> AsyncEngine:
        if self._rw is None:
            msg = f"{self._spec.entity!r} repository has no rw engine bound."
            raise RuntimeError(msg)
        return self._rw

    @property
    def ro_engine(self) -> AsyncEngine:
        if self._ro is None:
            msg = f"{self._spec.entity!r} repository has no ro engine bound."
            raise RuntimeError(msg)
        return self._ro

    def _version_binds(
        self,
        *,
        owner_uid: str,
        entity_id: str,
        snapshot: dict[str, Any],
        change_type: str,
        actor_uid: str | None,
        actor_email: str | None,
        change_reason: str | None,
    ) -> dict[str, Any]:
        return {
            "entity_type": self._spec.table,
            "entity_id": entity_id,
            "payload": _snapshot_payload(snapshot),
            "change_type": change_type,
            "change_reason": change_reason,
            "changed_by_uid": actor_uid or owner_uid,
            "changed_by_email": actor_email,
            "request_id": _current_request_id(),
            "owner_uid": owner_uid,
        }

    async def _write_version(
        self,
        conn: Any,  # noqa: ANN401 -- SQLAlchemy AsyncConnection inside the open txn
        *,
        owner_uid: str,
        snapshot: dict[str, Any],
        change_type: str,
        actor_uid: str | None,
        actor_email: str | None,
        change_reason: str | None,
    ) -> None:
        """Append one ``app.entity_versions`` row inside the caller's transaction.

        Runs on the SAME connection as the head-row write so the version
        row commits (or rolls back) atomically with it: a failed head
        write leaves no version row, and a failed version write undoes
        the head write.
        """

        if not self._spec.versioned:
            return
        entity_id = str(snapshot.get("id", ""))
        if not entity_id:
            return
        await conn.execute(
            _VERSION_INSERT_SQL,
            self._version_binds(
                owner_uid=owner_uid,
                entity_id=entity_id,
                snapshot=snapshot,
                change_type=change_type,
                actor_uid=actor_uid,
                actor_email=actor_email,
                change_reason=change_reason,
            ),
        )

    async def create(
        self,
        *,
        owner_uid: str,
        values: dict[str, Any],
        actor_uid: str | None = None,
        actor_email: str | None = None,
        change_reason: str | None = None,
    ) -> dict[str, Any]:
        """Insert one row owned by ``owner_uid`` and return the canonical projection."""

        spec = self._spec
        column_names = ["owner_uid", *spec.scalar_columns, *spec.jsonb_columns]
        binds = {"owner_uid": owner_uid}
        for column in (*spec.scalar_columns, *spec.jsonb_columns):
            if column not in values:
                msg = f"Missing required column {column!r} for create({spec.entity})."
                raise KeyError(msg)
            binds[column] = _bind_value(column, values[column])

        placeholders: list[str] = []
        for column in column_names:
            if column in spec.jsonb_columns:
                # SQLAlchemy ``text()``'s parameter parser treats
                # ``:name::type`` as ``name`` + a ``::type`` type-hint,
                # consuming the last char of ``name`` as part of the
                # hint. Using ``CAST(... AS jsonb)`` sidesteps the
                # collision so any column name binds cleanly.
                placeholders.append(f"CAST(:{column} AS jsonb)")
            else:
                placeholders.append(f":{column}")

        projection = _format_projection("t", spec.projection_columns)
        sql = text(
            f"INSERT INTO app.{spec.table} ({', '.join(column_names)}) "  # noqa: S608
            f"VALUES ({', '.join(placeholders)}) "
            f"RETURNING {projection.replace('t.', '')}"
        )

        try:
            async with self.rw_engine.begin() as conn:
                result = await conn.execute(sql, binds)
                row = result.mappings().one()
                await self._write_version(
                    conn,
                    owner_uid=owner_uid,
                    snapshot=dict(row),
                    change_type="create",
                    actor_uid=actor_uid,
                    actor_email=actor_email,
                    change_reason=change_reason,
                )
        except IntegrityError as exc:
            self._maybe_translate_name_conflict(exc, values)
            raise

        return dict(row)

    async def list(
        self,
        *,
        owner_uid: str,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        """Return up to ``limit`` live rows owned by ``owner_uid``.

        Excludes soft-deleted rows when the table has the ``deleted_at``
        column; ordered by ``created_at DESC, id ASC`` for deterministic
        pagination (UUID v4 ``id`` is the tiebreaker for rows inserted
        in the same millisecond — unlikely in practice but the
        deterministic order matters for offset pagination's correctness).
        """

        spec = self._spec
        projection = _format_projection("t", spec.projection_columns)
        where_clauses = ["t.owner_uid = :owner_uid"]
        if spec.has_soft_delete:
            where_clauses.append("t.deleted_at IS NULL")
        sql = text(
            f"SELECT {projection} FROM app.{spec.table} AS t "  # noqa: S608
            f"WHERE {' AND '.join(where_clauses)} "
            "ORDER BY t.created_at DESC, t.id ASC "
            "LIMIT :limit OFFSET :offset"
        )
        binds = {"owner_uid": owner_uid, "limit": limit, "offset": offset}
        async with self.ro_engine.connect() as conn:
            result = await conn.execute(sql, binds)
            rows = result.mappings().all()
        return [dict(row) for row in rows]

    async def get(
        self,
        *,
        owner_uid: str,
        resource_id: uuid.UUID,
    ) -> dict[str, Any]:
        """Return one live row by id, scoped to ``owner_uid``.

        Raises ``NotFoundError`` if the row doesn't exist, has a
        different owner, or has been soft-deleted. The three cases
        collapse to the same response so we don't leak the existence
        of a row another tenant owns.
        """

        row = await self._fetch_visible(owner_uid=owner_uid, resource_id=resource_id)
        if row is None:
            raise NotFoundError(entity=self._spec.entity, resource_id=str(resource_id))
        return row

    async def patch(
        self,
        *,
        owner_uid: str,
        resource_id: uuid.UUID,
        updates: dict[str, Any],
        actor_uid: str | None = None,
        actor_email: str | None = None,
        change_reason: str | None = None,
    ) -> dict[str, Any]:
        """Apply a partial update; return the post-update projection.

        ``updates`` keys must be a subset of ``EntitySpec.mutable_columns``.
        ``updated_at`` is trigger-maintained; we don't touch it
        here. The row visibility check (``deleted_at IS NULL``) lives
        in the ``WHERE`` clause so a stale PATCH against a soft-deleted
        row returns ``NotFoundError`` instead of silently un-deleting it.
        """

        spec = self._spec
        if not updates:
            return await self.get(owner_uid=owner_uid, resource_id=resource_id)

        allowed = set(spec.mutable_columns)
        unknown = set(updates) - allowed
        if unknown:
            msg = f"Unknown PATCH fields for {spec.entity}: {sorted(unknown)!r}"
            raise KeyError(msg)

        binds: dict[str, Any] = {
            "owner_uid": owner_uid,
            "resource_id": str(resource_id),
        }
        set_fragments: list[str] = []
        for column, value in updates.items():
            binds[column] = _bind_value(column, value)
            if column in spec.jsonb_columns:
                # See create() above — ``CAST(... AS jsonb)`` works
                # around SQLAlchemy's ``:name::type`` parser quirk.
                set_fragments.append(f"{column} = CAST(:{column} AS jsonb)")
            else:
                set_fragments.append(f"{column} = :{column}")

        where_clauses = ["id = :resource_id", "owner_uid = :owner_uid"]
        if spec.has_soft_delete:
            where_clauses.append("deleted_at IS NULL")
        projection = _format_projection("t", spec.projection_columns).replace("t.", "")
        sql = text(
            f"UPDATE app.{spec.table} SET {', '.join(set_fragments)} "  # noqa: S608
            f"WHERE {' AND '.join(where_clauses)} "
            f"RETURNING {projection}"
        )

        try:
            async with self.rw_engine.begin() as conn:
                result = await conn.execute(sql, binds)
                row = result.mappings().one_or_none()
                if row is not None:
                    await self._write_version(
                        conn,
                        owner_uid=owner_uid,
                        snapshot=dict(row),
                        change_type="amend",
                        actor_uid=actor_uid,
                        actor_email=actor_email,
                        change_reason=change_reason,
                    )
        except IntegrityError as exc:
            self._maybe_translate_name_conflict(exc, updates)
            raise

        if row is None:
            raise NotFoundError(entity=spec.entity, resource_id=str(resource_id))
        return dict(row)

    async def soft_delete(
        self,
        *,
        owner_uid: str,
        resource_id: uuid.UUID,
        actor_uid: str | None = None,
        actor_email: str | None = None,
        change_reason: str | None = None,
    ) -> None:
        """Set ``deleted_at = now()`` on the row. Idempotent.

        If the row is already soft-deleted, the call is a no-op success
        (the ``deleted_at`` timestamp is not refreshed — the original
        deletion stamp wins) and NO version row is appended. If the row
        doesn't exist or has a different owner, raises ``NotFoundError``.
        """

        spec = self._spec
        if not spec.has_soft_delete:
            msg = f"{spec.entity!r} does not support soft delete."
            raise RuntimeError(msg)

        # Full projection so the audit trail captures the row state at
        # deletion (``deleted_at`` freshly set) — not just the id.
        returning = _format_projection("t", spec.projection_columns).replace("t.", "")
        if spec.wrapper_graph_cascade:
            # Capture the wrapper marker + the sibling save-graph row's
            # UUID so the same transaction can cascade the delete to it
            # . ``request`` is always a JSONB column on the
            # product tables this flag is set for.
            returning += (
                ", (request->>'__wrapper__') AS _wrapper_flag"
                ", (request->>'appId') AS _linked_app_id"
            )

        sql = text(
            f"UPDATE app.{spec.table} "  # noqa: S608
            "SET deleted_at = now() "
            "WHERE id = :resource_id "
            "  AND owner_uid = :owner_uid "
            "  AND deleted_at IS NULL "
            f"RETURNING {returning}"
        )
        binds = {"owner_uid": owner_uid, "resource_id": str(resource_id)}
        async with self.rw_engine.begin() as conn:
            result = await conn.execute(sql, binds)
            row = result.mappings().one_or_none()
            if row is not None:
                await self._write_version(
                    conn,
                    owner_uid=owner_uid,
                    snapshot=dict(row),
                    change_type="delete",
                    actor_uid=actor_uid,
                    actor_email=actor_email,
                    change_reason=change_reason,
                )
            if row is not None and spec.wrapper_graph_cascade:
                await self._cascade_wrapper_graph(
                    conn,
                    owner_uid=owner_uid,
                    deleted_row=dict(row),
                    actor_uid=actor_uid,
                    actor_email=actor_email,
                    change_reason=change_reason,
                )

        if row is not None:
            return

        # The row may have already been soft-deleted; ``DELETE`` is
        # idempotent by design, so we treat "row exists but deleted"
        # as success. "Row missing entirely / wrong owner" stays 404.
        existing = await self._fetch_any(owner_uid=owner_uid, resource_id=resource_id)
        if existing is None:
            raise NotFoundError(entity=spec.entity, resource_id=str(resource_id))

    async def _cascade_wrapper_graph(
        self,
        conn: Any,  # noqa: ANN401 -- SQLAlchemy AsyncConnection inside an open txn
        *,
        owner_uid: str,
        deleted_row: dict[str, Any],
        actor_uid: str | None = None,
        actor_email: str | None = None,
        change_reason: str | None = None,
    ) -> None:
        """Soft-delete the save-graph sibling a wrapper row points at.

        A portal "Save" persists TWO rows in the same
        product table — the wrapper row the user sees
        (``request.__wrapper__ = true``) and the by-reference save-graph
        row it targets via ``request.appId``. Deleting the wrapper alone
        left the graph row orphaned. We cascade the same
        owner-scoped soft delete to the ``appId`` sibling inside the
        caller's transaction so both rows disappear atomically.

        Only wrapper rows cascade (graph rows are not wrapper-marked, so
        there is no recursion). A missing / malformed ``appId`` is a
        no-op. Reference-data rows (curves / curve_sets) named in the
        wrapper's ``appGraph`` live in other tables, may be shared across
        products, and are intentionally left untouched — cascading them
        safely needs reference-counting (tracked as the residual).
        """

        if deleted_row.get("_wrapper_flag") != "true":
            return
        raw_app_id = deleted_row.get("_linked_app_id")
        if not raw_app_id:
            return
        try:
            app_id = str(uuid.UUID(str(raw_app_id)))
        except (ValueError, TypeError):
            return

        projection = _format_projection("t", self._spec.projection_columns).replace("t.", "")
        sql = text(
            f"UPDATE app.{self._spec.table} "  # noqa: S608
            "SET deleted_at = now() "
            "WHERE id = CAST(:app_id AS uuid) "
            "  AND owner_uid = :owner_uid "
            "  AND deleted_at IS NULL "
            f"RETURNING {projection}"
        )
        result = await conn.execute(sql, {"app_id": app_id, "owner_uid": owner_uid})
        cascaded = result.mappings().one_or_none()
        if cascaded is not None:
            # The cascaded sibling's delete is part of the same user
            # action: same transaction, same actor, same request_id —
            # the audit trail groups the pair naturally.
            await self._write_version(
                conn,
                owner_uid=owner_uid,
                snapshot=dict(cascaded),
                change_type="delete",
                actor_uid=actor_uid,
                actor_email=actor_email,
                change_reason=change_reason,
            )

    async def restore(
        self,
        *,
        owner_uid: str,
        resource_id: uuid.UUID,
        actor_uid: str | None = None,
        actor_email: str | None = None,
        change_reason: str | None = None,
    ) -> dict[str, Any]:
        """Clear ``deleted_at``. Idempotent. 409 on name conflict.

        Idempotent: restoring a row that is already live returns it
        unchanged. If a different live row already owns the same
        ``(owner_uid, name)``, the partial unique index raises and we
        translate to ``NameConflictError``.
        """

        spec = self._spec
        if not spec.has_soft_delete:
            msg = f"{spec.entity!r} does not support restore."
            raise RuntimeError(msg)

        existing = await self._fetch_any(owner_uid=owner_uid, resource_id=resource_id)
        if existing is None:
            raise NotFoundError(entity=spec.entity, resource_id=str(resource_id))
        if existing.get("deleted_at") is None:
            return existing

        projection = _format_projection("t", spec.projection_columns).replace("t.", "")
        sql = text(
            f"UPDATE app.{spec.table} "  # noqa: S608
            "SET deleted_at = NULL "
            "WHERE id = :resource_id "
            "  AND owner_uid = :owner_uid "
            "  AND deleted_at IS NOT NULL "
            f"RETURNING {projection}"
        )
        binds = {"owner_uid": owner_uid, "resource_id": str(resource_id)}
        try:
            async with self.rw_engine.begin() as conn:
                result = await conn.execute(sql, binds)
                row = result.mappings().one_or_none()
                if row is not None:
                    await self._write_version(
                        conn,
                        owner_uid=owner_uid,
                        snapshot=dict(row),
                        change_type="restore",
                        actor_uid=actor_uid,
                        actor_email=actor_email,
                        change_reason=change_reason,
                    )
        except IntegrityError as exc:
            # The partial unique on (owner_uid, name) WHERE deleted_at
            # IS NULL is the only constraint restore can violate.
            if spec.name_column is not None:
                raise NameConflictError(
                    entity=spec.entity,
                    name=str(existing.get(spec.name_column, "")),
                ) from exc
            raise

        if row is None:
            # Raced with another writer; treat as "already restored" by
            # re-fetching the visible row.
            return await self.get(owner_uid=owner_uid, resource_id=resource_id)
        return dict(row)

    async def list_versions(
        self,
        *,
        owner_uid: str,
        resource_id: uuid.UUID,
        # NB: ``builtins.list`` because inside this class body the bare
        # name ``list`` resolves to the ``CrudRepository.list`` method.
    ) -> builtins.list[dict[str, Any]]:
        """Return the entity's version history (metadata only), newest first.

        Owner-scoped with the 404-not-403 tenancy rule: an entity owned
        by someone else (or one that never existed) raises
        ``NotFoundError``. Soft-deleted entities DO list their history —
        the audit trail is precisely how a delete is inspected.
        """

        spec = self._spec
        sql = text(
            "SELECT version_no, change_type, change_reason, changed_by_uid, "
            "changed_by_email, changed_at, request_id "
            "FROM app.entity_versions AS v "
            "WHERE v.entity_type = :entity_type "
            "  AND v.entity_id = CAST(:resource_id AS uuid) "
            "  AND v.owner_uid = :owner_uid "
            "ORDER BY v.version_no DESC"
        )
        binds = {
            "entity_type": spec.table,
            "resource_id": str(resource_id),
            "owner_uid": owner_uid,
        }
        async with self.ro_engine.connect() as conn:
            result = await conn.execute(sql, binds)
            rows = result.mappings().all()
        if not rows:
            # Post-backfill every existing row has at least v1, so "no
            # versions" normally means "no such entity for this owner"
            # → 404. The head-row check keeps the edge case (a row
            # created before the audit migration ran) from 404ing a
            # real entity.
            existing = await self._fetch_any(owner_uid=owner_uid, resource_id=resource_id)
            if existing is None:
                raise NotFoundError(entity=spec.entity, resource_id=str(resource_id))
        return [dict(row) for row in rows]

    async def get_version(
        self,
        *,
        owner_uid: str,
        resource_id: uuid.UUID,
        version_no: int,
    ) -> dict[str, Any]:
        """Return one version row including the full ``payload`` snapshot.

        Same owner-scoped 404-not-403 rule as :meth:`list_versions`.
        """

        spec = self._spec
        sql = text(
            "SELECT version_no, change_type, change_reason, changed_by_uid, "
            "changed_by_email, changed_at, request_id, payload "
            "FROM app.entity_versions AS v "
            "WHERE v.entity_type = :entity_type "
            "  AND v.entity_id = CAST(:resource_id AS uuid) "
            "  AND v.owner_uid = :owner_uid "
            "  AND v.version_no = :version_no"
        )
        binds = {
            "entity_type": spec.table,
            "resource_id": str(resource_id),
            "owner_uid": owner_uid,
            "version_no": version_no,
        }
        async with self.ro_engine.connect() as conn:
            result = await conn.execute(sql, binds)
            row = result.mappings().one_or_none()
        if row is None:
            raise NotFoundError(
                entity=spec.entity,
                resource_id=f"{resource_id} version {version_no}",
            )
        return dict(row)

    async def _fetch_visible(
        self,
        *,
        owner_uid: str,
        resource_id: uuid.UUID,
    ) -> dict[str, Any] | None:
        spec = self._spec
        projection = _format_projection("t", spec.projection_columns)
        where_clauses = ["t.id = :resource_id", "t.owner_uid = :owner_uid"]
        if spec.has_soft_delete:
            where_clauses.append("t.deleted_at IS NULL")
        sql = text(
            f"SELECT {projection} FROM app.{spec.table} AS t "  # noqa: S608
            f"WHERE {' AND '.join(where_clauses)}"
        )
        binds = {"owner_uid": owner_uid, "resource_id": str(resource_id)}
        async with self.ro_engine.connect() as conn:
            result = await conn.execute(sql, binds)
            row = result.mappings().one_or_none()
        return dict(row) if row is not None else None

    async def _fetch_any(
        self,
        *,
        owner_uid: str,
        resource_id: uuid.UUID,
    ) -> dict[str, Any] | None:
        """Read a row regardless of ``deleted_at``. Used by delete/restore."""

        spec = self._spec
        projection = _format_projection("t", spec.projection_columns)
        sql = text(
            f"SELECT {projection} FROM app.{spec.table} AS t "  # noqa: S608
            "WHERE t.id = :resource_id AND t.owner_uid = :owner_uid"
        )
        binds = {"owner_uid": owner_uid, "resource_id": str(resource_id)}
        async with self.ro_engine.connect() as conn:
            result = await conn.execute(sql, binds)
            row = result.mappings().one_or_none()
        return dict(row) if row is not None else None

    def _maybe_translate_name_conflict(
        self,
        exc: IntegrityError,
        values: dict[str, Any],
    ) -> None:
        """Convert a partial-unique violation into a 409 ``name_conflict``."""

        spec = self._spec
        if spec.name_column is None or spec.unique_constraint is None:
            return
        text_blob = str(exc.orig) if exc.orig is not None else str(exc)
        if spec.unique_constraint in text_blob:
            raise NameConflictError(
                entity=spec.entity,
                name=str(values.get(spec.name_column, "")),
            ) from exc


class ImmutableRepository:
    """Read-only view of ``app.pricing_history``.

    Exposes only ``list`` / ``get``: no creates from the data layer,
    no soft delete, no trigger, no restore. Writes are produced by
    the pricing flow (a future plan), not by this CRUD surface.
    """

    def __init__(
        self,
        spec: EntitySpec,
        *,
        ro_engine: AsyncEngine | None = None,
    ) -> None:
        self._spec = spec
        self._ro = ro_engine

    @property
    def spec(self) -> EntitySpec:
        return self._spec

    @property
    def ro_engine(self) -> AsyncEngine:
        if self._ro is None:
            msg = f"{self._spec.entity!r} repository has no ro engine bound."
            raise RuntimeError(msg)
        return self._ro

    async def list(
        self,
        *,
        owner_uid: str,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        spec = self._spec
        projection = _format_projection("t", spec.projection_columns)
        sql = text(
            f"SELECT {projection} FROM app.{spec.table} AS t "  # noqa: S608
            "WHERE t.owner_uid = :owner_uid "
            "ORDER BY t.created_at DESC, t.id ASC "
            "LIMIT :limit OFFSET :offset"
        )
        binds = {"owner_uid": owner_uid, "limit": limit, "offset": offset}
        async with self.ro_engine.connect() as conn:
            result = await conn.execute(sql, binds)
            rows = result.mappings().all()
        return [dict(row) for row in rows]

    async def get(
        self,
        *,
        owner_uid: str,
        resource_id: uuid.UUID,
    ) -> dict[str, Any]:
        spec = self._spec
        projection = _format_projection("t", spec.projection_columns)
        sql = text(
            f"SELECT {projection} FROM app.{spec.table} AS t "  # noqa: S608
            "WHERE t.id = :resource_id AND t.owner_uid = :owner_uid"
        )
        binds = {"owner_uid": owner_uid, "resource_id": str(resource_id)}
        async with self.ro_engine.connect() as conn:
            result = await conn.execute(sql, binds)
            row = result.mappings().one_or_none()
        if row is None:
            raise NotFoundError(entity=spec.entity, resource_id=str(resource_id))
        return dict(row)


__all__ = [
    "CrudRepository",
    "EntitySpec",
    "ImmutableRepository",
]
