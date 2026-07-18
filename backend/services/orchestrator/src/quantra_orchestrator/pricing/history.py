"""``app.pricing_history`` write hook (plan).

Every product pricing endpoint calls :func:`record_pricing_call`
exactly once per request (on the success path *and* on the failure
path), persisting the call into the immutable
``app.pricing_history`` table. The function is the single
seam every per-product endpoint shares so the same audit shape lands
for every product without per-product SQL.

Hard constraints:

* **Write goes through ``app_rw``** (the only request-path
  write surface outside the CRUD layer).
* **JSONB binds use ``CAST(:col AS jsonb)``** (the
  ``:col::jsonb`` shorthand collides with SQLAlchemy's ``text``
  parameter parser and silently corrupts the bind).
* **Fire-and-forget.** A failure here (engine missing, DB
  unreachable, etc.) MUST NOT turn a successful price into a
  500. The function never raises; it logs structurally with the
  ``pricing_history_write_failed`` error token (logged-only — never
  surfaced in a client response) and returns ``None``. Callers
  populate ``IrSwapPriceResponse.pricing_history_id`` (and the
  analogous field on every other product's response model) from
  the return value — ``None`` is a documented success shape on
  the response.
* **Schema column names.** The migration
  (``migrations/alembic/versions/app/0007_pricing_history.py``)
  defines the JSONB columns as ``request`` / ``response`` (not
  ``request_payload`` / ``response_payload`` — the plan text uses
  the longer names interchangeably). The migration is the source
  of truth.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_log = structlog.get_logger(__name__)

PRICING_HISTORY_WRITE_FAILED_CODE: str = "pricing_history_write_failed"

# The valid ``product_kind`` values are pinned by the migration's
# CHECK constraint (``pricing_history_product_kind_valid``). Plans
# 06-11 each pick their own value; keep this tuple in sync with the
# CHECK list so a typo at a call site fails fast in unit tests
# rather than at runtime against Postgres.
VALID_PRODUCT_KINDS: tuple[str, ...] = (
    "swaps_ir",
    "swaps_inflation",
    "swaptions",
    "bonds_fixed",
    "bonds_floating",
    "cds",
    "equity_options",
)

# When the call prices a SAVED product (``product_id`` set), the row
# additionally records the entity's head version in the append-only
# audit trail (``app.entity_versions``) so the number is
# forever tied to the exact trade state that produced it. The head
# version is resolved by the inline scalar subquery — same transaction,
# no extra round-trip. ``trade_entity_type`` equals ``product_kind``
# because the product tables' entity-versions type key IS the table
# name; inline calls leave all three columns NULL.
_INSERT_SQL = text(
    "INSERT INTO app.pricing_history ("
    "owner_uid, product_kind, product_id, as_of, request, response, "
    "trade_entity_type, trade_entity_id, trade_version"
    ") VALUES ("
    ":owner_uid, :product_kind, :product_id, :as_of, "
    "CAST(:request AS jsonb), CAST(:response AS jsonb), "
    ":trade_entity_type, CAST(:trade_entity_id AS uuid), "
    "(SELECT MAX(v.version_no) FROM app.entity_versions AS v "
    " WHERE v.entity_type = :product_kind "
    "   AND v.entity_id = CAST(:trade_entity_id AS uuid) "
    "   AND v.owner_uid = :owner_uid)"
    ") RETURNING id::text AS id"
)


async def record_pricing_call(
    *,
    rw_engine: AsyncEngine | None,
    owner_uid: str,
    product_kind: str,
    product_id: UUID | None,
    as_of: date | None,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any],
) -> UUID | None:
    """Insert one ``app.pricing_history`` row. Never raises.

    Parameters
    ----------
    rw_engine:
        The lifespan-owned ``app_rw`` engine. ``None`` is accepted as
        a deploy-time-disabled signal (e.g. a scaffold deploy without
        ``POSTGRES_DSN_APP_RW``) and short-circuits to a structured
        log line + ``None`` return so pricing endpoints stay
        serviceable.
    owner_uid:
        The authenticated principal from ``AuthContext.uid``.
    product_kind:
        Stable product label matching the migration's CHECK list (see
        :data:`VALID_PRODUCT_KINDS`).
    product_id:
        Soft reference into the matching product table. ``None``
        for inline-only pricing calls that don't reference a saved
        row.
    as_of:
        Pricing ``as_of`` date. ``None`` is admitted by the migration
        for callers that don't carry one.
    request_payload:
        The post-validation API request body (envelope shape on
        failures isn't this — callers wrap failures in ``response``).
        Must be a JSON object (the migration's
        ``pricing_history_request_object`` CHECK enforces it).
    response_payload:
        On success: the decoded engine response (typed Pydantic model
        ``.model_dump(mode="json")``). On failure: the envelope
        wrapped under ``{"error": {...}}`` so a successful pricing
        call is distinguishable from a recorded failure on the same
        row shape. Must be a JSON object.

    Returns
    -------
    UUID | None:
        The newly-inserted row's id, or ``None`` on any failure.
        Callers surface ``None`` as ``pricing_history_id`` in the
        response and continue serving normally.
    """

    if rw_engine is None:
        _log.warning(
            "orchestrator.pricing_history.write_failed",
            code=PRICING_HISTORY_WRITE_FAILED_CODE,
            owner_uid=owner_uid,
            product_kind=product_kind,
            reason="app_rw_engine_unavailable",
        )
        return None

    binds: dict[str, Any] = {
        "owner_uid": owner_uid,
        "product_kind": product_kind,
        "product_id": str(product_id) if product_id is not None else None,
        "as_of": as_of,
        # By-reference calls link the priced entity + its audit-trail
        # head version (resolved by the INSERT's scalar subquery);
        # inline calls leave the trio NULL.
        "trade_entity_type": product_kind if product_id is not None else None,
        "trade_entity_id": str(product_id) if product_id is not None else None,
        # JSONB binds go in as JSON strings; the ``CAST(... AS jsonb)``
        # wrappers in ``_INSERT_SQL`` parse them server-side. Using
        # ``json.dumps`` matches the data-layer repository's posture
        # (``_bind_value``) so the same serialiser quirks apply
        # uniformly across the app.
        "request": json.dumps(request_payload),
        "response": json.dumps(response_payload),
    }

    try:
        async with rw_engine.begin() as conn:
            result = await conn.execute(_INSERT_SQL, binds)
            row = result.mappings().one()
    except Exception as exc:
        # Catch-all is intentional: any failure here (asyncpg
        # connectivity, IntegrityError from a stale CHECK list, the
        # engine being disposed mid-request, etc.) is non-blocking.
        # The ``pricing_history_write_failed`` token is
        # log-only — clients never see it.
        _log.warning(
            "orchestrator.pricing_history.write_failed",
            code=PRICING_HISTORY_WRITE_FAILED_CODE,
            owner_uid=owner_uid,
            product_kind=product_kind,
            product_id=str(product_id) if product_id is not None else None,
            exc_type=type(exc).__name__,
        )
        return None

    raw_id = row["id"]
    if isinstance(raw_id, UUID):
        return raw_id
    # ``id::text AS id`` in the SQL means asyncpg returns a ``str``;
    # branch defensively so a future repository tweak that drops the
    # cast (returning ``uuid.UUID``) doesn't break this seam.
    return UUID(str(raw_id))


__all__ = [
    "PRICING_HISTORY_WRITE_FAILED_CODE",
    "VALID_PRODUCT_KINDS",
    "record_pricing_call",
]
