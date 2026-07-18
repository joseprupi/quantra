"""Roll the real MD-backed demo curves' ``reference_date`` forward.

After a real-vendor ingest tick (``boe_ois`` / ``treasury``), the freshest
business day present in ``md.quote_points`` advances (the BoE "latest" OIS
workbook lands yesterday's fixing). The seeded "always current" demo curves
must track it: their ``app.curves.reference_date`` is bumped to the latest
``as_of`` of the series they reference, so a fresh price on the
auto-defaulted As-Of resolves those quotes at an exact-match date
(the resolver keys on the latest quote with ``as_of <= as_of``).

Read/write split (pool isolation):

* the latest date is READ from ``md.quote_points`` via the ``md_rw`` engine
  (the ingester already owns it — no new DSN needed for the read);
* the bump is WRITTEN to ``app.curves`` via a dedicated ``app_rw`` engine
  (``POSTGRES_DSN_APP_RW`` — the one extra DSN the roll job carries).

Safety properties:

* **marker-scoped** — only curves whose ``body->>'local_id'`` matches a
  configured real-curve marker are touched, so synthetic / user curves are
  never rewritten;
* **owner-scoped** — bounded to a single ``owner_uid`` (the bundle's implicit
  ``dev-user``);
* **idempotent** — the UPDATE has ``reference_date IS DISTINCT FROM:ref_date``
  so a re-run on an unchanged feed writes nothing (and reports zero updated).

All SQL is unqualified and relies on the pinned per-role ``search_path``
exactly like :mod:`quantra_md_ingester.writer` / :mod:`quantra_md_ingester.series`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_common.db import make_app_engine
from quantra_common.logging import get_logger
from quantra_common.settings.base import Settings

logger = get_logger(__name__)

# The bundle's implicit single user (dev-auth bypass); the seed script
# creates every demo entity under this uid.
DEFAULT_ROLL_OWNER_UID: Final[str] = "dev-user"


@dataclass(frozen=True, slots=True)
class RollTarget:
    """One real MD-backed curve to keep current.

    ``curve_local_id`` matches the seed script's ``body.local_id`` marker so
    only the intended real curve is rewritten; ``series_prefix`` selects the
    ``md.quote_points`` rows whose latest ``as_of`` defines "today's" curve
    date (a ``canonical_id LIKE 'prefix%'`` match).
    """

    label: str
    curve_local_id: str
    series_prefix: str


# The real curves the seed script plants (``scripts/seed_demo_entities.py``).
# ``md-gbp-boe-ois`` is the flagship: its pillars reference the BoE SONIA OIS
# par strip. ``md-usd-treasury`` is defensive — the UST curve is not seeded
# today, so this target simply finds no matching curve and is a no-op (the
# ``treasury`` series still land so it activates automatically if a UST curve
# is seeded later).
DEFAULT_ROLL_TARGETS: Final[tuple[RollTarget, ...]] = (
    RollTarget(
        label="GBP SONIA OIS (BoE)",
        curve_local_id="md-gbp-boe-ois",
        series_prefix="GBP.RATES.BOE.OIS.",
    ),
    RollTarget(
        label="USD Treasury (UST official)",
        curve_local_id="md-usd-treasury",
        series_prefix="USD.RATES.UST.OFFICIAL.",
    ),
)


@dataclass(slots=True)
class RollResult:
    """Per-target outcome of a roll."""

    label: str
    curve_local_id: str
    series_prefix: str
    latest_date: date | None
    curves_updated: int

    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "curve_local_id": self.curve_local_id,
            "series_prefix": self.series_prefix,
            "latest_date": self.latest_date.isoformat() if self.latest_date else None,
            "curves_updated": self.curves_updated,
        }


# Latest ingested business day for the target's series family. ``::date`` so
# the value maps cleanly onto ``app.curves.reference_date`` (a DATE column).
_LATEST_DATE_SQL: Final[str] = (
    "SELECT max(as_of)::date AS latest_date FROM quote_points WHERE canonical_id LIKE :prefix"
)

# Marker + owner scoped, idempotent bump. RETURNING id so the count of
# actually-changed rows is exact (a no-op re-run returns zero rows).
_ROLL_CURVE_SQL: Final[str] = """
    UPDATE curves
    SET reference_date = :ref_date
    WHERE owner_uid = :owner_uid
      AND body->>'local_id' = :local_id
      AND deleted_at IS NULL
      AND reference_date IS DISTINCT FROM :ref_date
    RETURNING id
"""


async def _latest_date_for_prefix(md_engine: AsyncEngine, prefix: str) -> date | None:
    async with md_engine.connect() as conn:
        result = await conn.execute(text(_LATEST_DATE_SQL), {"prefix": f"{prefix}%"})
        row = result.mappings().one_or_none()
    if row is None:
        return None
    latest: date | None = row["latest_date"]
    return latest


async def _bump_curve(
    app_engine: AsyncEngine, *, owner_uid: str, local_id: str, ref_date: date
) -> int:
    async with app_engine.begin() as conn:
        result = await conn.execute(
            text(_ROLL_CURVE_SQL),
            {"ref_date": ref_date, "owner_uid": owner_uid, "local_id": local_id},
        )
        return len(result.mappings().all())


async def roll_curve_dates(
    *,
    app_engine: AsyncEngine,
    md_engine: AsyncEngine,
    owner_uid: str = DEFAULT_ROLL_OWNER_UID,
    targets: Sequence[RollTarget] = DEFAULT_ROLL_TARGETS,
) -> list[RollResult]:
    """Bump each real MD-backed curve's ``reference_date`` to its latest date.

    Returns one :class:`RollResult` per target. A target whose series has no
    ``md.quote_points`` yet (fresh volume, feed never ran) is reported with
    ``latest_date=None`` / ``curves_updated=0`` and left untouched — the
    seeded ``reference_date`` (or the last successful roll) persists.
    """

    results: list[RollResult] = []
    for target in targets:
        latest = await _latest_date_for_prefix(md_engine, target.series_prefix)
        updated = 0
        if latest is not None:
            updated = await _bump_curve(
                app_engine,
                owner_uid=owner_uid,
                local_id=target.curve_local_id,
                ref_date=latest,
            )
        logger.info(
            "md_ingester.roll_curve_dates.target",
            label=target.label,
            curve_local_id=target.curve_local_id,
            series_prefix=target.series_prefix,
            owner_uid=owner_uid,
            latest_date=latest.isoformat() if latest else None,
            curves_updated=updated,
        )
        results.append(
            RollResult(
                label=target.label,
                curve_local_id=target.curve_local_id,
                series_prefix=target.series_prefix,
                latest_date=latest,
                curves_updated=updated,
            )
        )
    return results


def build_app_rw_engine(settings: Settings | None = None) -> AsyncEngine:
    """Return an ``app_rw`` async engine for the ``reference_date`` bump."""

    return make_app_engine("rw", settings=settings)


__all__ = [
    "DEFAULT_ROLL_OWNER_UID",
    "DEFAULT_ROLL_TARGETS",
    "RollResult",
    "RollTarget",
    "build_app_rw_engine",
    "roll_curve_dates",
]
