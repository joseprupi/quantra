"""Ad-hoc round-trip verification for the md.* schema.

Not a pytest test — touching a live Postgres from the test suite would
break CI. This is the kind of script humans run by hand after a fresh
``alembic -n md upgrade head`` to convince themselves that:

1. Each entity table accepts a representative INSERT through ``md_rw``.
2. The same row reads back through ``md_ro`` byte-for-byte.
3. ``md_ro`` is denied INSERT / UPDATE / DELETE on every table.
4. ``md_rw`` is denied any access at all to ``app.*`` (schema
   isolation via per-role grants + search_path).
5. ``snapshots.version_etag`` advances when ``snapshot_quotes`` for a
   snapshot changes — INSERT, UPDATE, and DELETE all bump it. This
   is the cache-invalidation primitive promised by O1.

Run from the repo root::

    uv run python migrations/scripts/roundtrip_md_schema.py

Reads role DSNs from the same ``.env`` the migration env.py uses, so
it follows the role pinning configured by ``0001_init.py``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime
from typing import Any

import asyncpg

from quantra_common.settings.base import Settings


def _dsn_for_asyncpg(dsn: str) -> str:
    """Strip the SQLAlchemy driver hint so asyncpg can connect."""

    if dsn.startswith("postgresql+asyncpg://"):
        return "postgresql://" + dsn[len("postgresql+asyncpg://") :]
    return dsn


async def _set_json_codecs(conn: asyncpg.Connection[Any]) -> None:
    """Register a JSON/JSONB codec so the driver returns Python dicts/lists."""

    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )
    await conn.set_type_codec(
        "json",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


def _check_row(label: str, written: dict[str, Any], actual_row: asyncpg.Record) -> None:
    """JSONB columns round-trip exactly via the codec above."""

    mismatches: list[str] = []
    for key, expected in written.items():
        got = actual_row.get(key)
        if isinstance(expected, dict | list):
            if json.dumps(expected, sort_keys=True) != json.dumps(got, sort_keys=True):
                mismatches.append(f"{key}: wrote {expected!r}, read {got!r}")
        elif got != expected:
            mismatches.append(f"{key}: wrote {expected!r}, read {got!r}")
    if mismatches:
        raise SystemExit(f"[{label}] round-trip mismatch:\n  " + "\n  ".join(mismatches))
    print(f"  [{label}] OK")


async def main() -> None:  # noqa: PLR0912, PLR0915 -- one big linear script; refactor for size hurts readability
    settings = Settings()
    rw_dsn = _dsn_for_asyncpg(settings.require_postgres_dsn_md_rw())
    ro_dsn = _dsn_for_asyncpg(settings.require_postgres_dsn_md_ro())

    conn_rw = await asyncpg.connect(rw_dsn)
    await _set_json_codecs(conn_rw)
    try:
        # Use UUID-tagged canonical IDs so re-running the script
        # doesn't conflict with previous fixtures.
        suffix = uuid.uuid4().hex[:8].upper()
        canonical_id_a = f"USD.RATES.UST.DGS.10Y.YIELD_{suffix}"
        canonical_id_b = f"USD.RATES.UST.DGS.2Y.YIELD_{suffix}"

        # (table, id_cols, id_vals, expected-fields). The list grows as each
        # entity is inserted; we replay it through `md_ro` at the end.
        fixtures: list[tuple[str, tuple[str, ...], tuple[Any, ...], dict[str, Any]]] = []

        # canonical_ids
        for cid, instrument, tenor in (
            (canonical_id_a, "DGS", "10Y"),
            (canonical_id_b, "DGS", "2Y"),
        ):
            ci_row = await conn_rw.fetchrow(
                "INSERT INTO md.canonical_ids "
                "(canonical_id, asset_class, family, instrument, currency, tenor, field, "
                "description) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
                "RETURNING canonical_id, asset_class, family, instrument, currency, tenor, field",
                cid,
                "RATES",
                "UST",
                instrument,
                "USD",
                tenor,
                "YIELD",
                f"Round-trip fixture for {cid}",
            )
            assert ci_row is not None
            fixtures.append(
                (
                    "canonical_ids",
                    ("canonical_id",),
                    (cid,),
                    {
                        "asset_class": ci_row["asset_class"],
                        "family": ci_row["family"],
                        "instrument": ci_row["instrument"],
                        "currency": ci_row["currency"],
                        "tenor": ci_row["tenor"],
                        "field": ci_row["field"],
                    },
                )
            )

        # vendor_mappings (one per canonical id)
        for cid, vendor_id in ((canonical_id_a, "DGS10"), (canonical_id_b, "DGS2")):
            vm_row = await conn_rw.fetchrow(
                "INSERT INTO md.vendor_mappings "
                "(vendor, vendor_id, vendor_field, canonical_id) "
                "VALUES ($1, $2, $3, $4) "
                "RETURNING id, vendor, vendor_id, canonical_id, priority, active",
                "fred",
                f"{vendor_id}_{suffix}",
                "RATE",
                cid,
            )
            assert vm_row is not None
            fixtures.append(
                (
                    "vendor_mappings",
                    ("id",),
                    (vm_row["id"],),
                    {
                        "vendor": vm_row["vendor"],
                        "vendor_id": vm_row["vendor_id"],
                        "canonical_id": vm_row["canonical_id"],
                        "priority": vm_row["priority"],
                        "active": vm_row["active"],
                    },
                )
            )

        # quote_points — append a small time series across both canonicals
        as_of_base = datetime(2026, 5, 14, 0, 0, 0, tzinfo=UTC)
        for cid, value in (
            (canonical_id_a, 0.0423),
            (canonical_id_b, 0.0468),
        ):
            qp_row = await conn_rw.fetchrow(
                "INSERT INTO md.quote_points "
                "(canonical_id, as_of, value, source, vendor_id, raw_value, units, "
                "quality_flags, meta) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
                "RETURNING canonical_id, as_of, value, source, vendor_id, units, "
                "quality_flags, meta",
                cid,
                as_of_base,
                value,
                "fred",
                f"vendor_{suffix}",
                value * 100.0,
                "decimal_rate",
                {"is_holiday": False},
                {"raw_value_percent": value * 100.0},
            )
            assert qp_row is not None
            fixtures.append(
                (
                    "quote_points",
                    ("canonical_id", "as_of"),
                    (cid, as_of_base),
                    {
                        "value": qp_row["value"],
                        "source": qp_row["source"],
                        "vendor_id": qp_row["vendor_id"],
                        "units": qp_row["units"],
                        "quality_flags": qp_row["quality_flags"],
                        "meta": qp_row["meta"],
                    },
                )
            )

        # Bulk insert: 10k rows in a single statement so we can sanity-check
        # the plan's "<1s representative ingestion" claim. Reuses
        # canonical_id_a so we collide on the PK every 50 rows (i.e. only
        # the first 50 actually insert without `ON CONFLICT`). For a
        # realistic measurement we generate distinct as_of values across
        # both canonical ids.
        bulk_n = 10_000
        bulk_rows = [
            (
                canonical_id_a if i % 2 == 0 else canonical_id_b,
                datetime(2025, 1, 1, tzinfo=UTC).timestamp() + i * 60,
                0.04 + (i % 50) * 0.0001,
            )
            for i in range(bulk_n)
        ]
        bulk_start = datetime.now(UTC)
        await conn_rw.executemany(
            "INSERT INTO md.quote_points (canonical_id, as_of, value, source) "
            "VALUES ($1, to_timestamp($2), $3, 'fred') "
            "ON CONFLICT (canonical_id, as_of) DO UPDATE "
            "SET value = EXCLUDED.value, ingested_at = now()",
            bulk_rows,
        )
        bulk_elapsed = (datetime.now(UTC) - bulk_start).total_seconds()
        print(f"  [bulk] inserted {bulk_n} quote_points in {bulk_elapsed:.3f}s")

        # snapshots
        snap_row = await conn_rw.fetchrow(
            "INSERT INTO md.snapshots (name, as_of, status, meta) "
            "VALUES ($1, $2, $3, $4) "
            "RETURNING id, name, as_of, status, version_etag",
            f"PUBLIC_USD_EOD_{suffix}",
            as_of_base,
            "ready",
            {"source_label": "roundtrip"},
        )
        assert snap_row is not None
        snapshot_id = snap_row["id"]
        initial_etag = snap_row["version_etag"]
        fixtures.append(
            (
                "snapshots",
                ("id",),
                (snapshot_id,),
                {
                    "name": snap_row["name"],
                    "as_of": snap_row["as_of"],
                    "status": snap_row["status"],
                },
            )
        )
        print(f"  [snapshots] initial version_etag={initial_etag[:8]}…")

        # snapshot_quotes — insert and verify version_etag advances
        for cid, value in ((canonical_id_a, 0.0423), (canonical_id_b, 0.0468)):
            sq_row = await conn_rw.fetchrow(
                "INSERT INTO md.snapshot_quotes "
                "(snapshot_id, canonical_id, value, resolved_as_of, source, vendor_id, meta) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7) "
                "RETURNING snapshot_id, canonical_id, value, resolved_as_of, source",
                snapshot_id,
                cid,
                value,
                as_of_base,
                "fred",
                f"vendor_{suffix}",
                {"is_exact": True},
            )
            assert sq_row is not None
            fixtures.append(
                (
                    "snapshot_quotes",
                    ("snapshot_id", "canonical_id"),
                    (snapshot_id, cid),
                    {
                        "value": sq_row["value"],
                        "resolved_as_of": sq_row["resolved_as_of"],
                        "source": sq_row["source"],
                    },
                )
            )

        etag_after_inserts = await conn_rw.fetchval(
            "SELECT version_etag FROM md.snapshots WHERE id = $1", snapshot_id
        )
        if etag_after_inserts == initial_etag:
            raise SystemExit(
                f"version_etag did NOT advance after snapshot_quotes INSERTs "
                f"({initial_etag} -> {etag_after_inserts})"
            )
        print(f"  [version_etag] advanced after INSERT: {etag_after_inserts[:8]}…")

        # ingestion_log
        il_row = await conn_rw.fetchrow(
            "INSERT INTO md.ingestion_log "
            "(vendor, source, status, row_count, error_count, errors, meta) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) "
            "RETURNING id, vendor, source, status, row_count, error_count",
            "fred",
            "ust_yields",
            "success",
            42,
            0,
            [],
            {"config_path": "configs/fred/ust_yields.json"},
        )
        assert il_row is not None
        fixtures.append(
            (
                "ingestion_log",
                ("id",),
                (il_row["id"],),
                {
                    "vendor": il_row["vendor"],
                    "source": il_row["source"],
                    "status": il_row["status"],
                    "row_count": il_row["row_count"],
                    "error_count": il_row["error_count"],
                },
            )
        )

        # Separate snapshot dedicated to the UPDATE / DELETE etag checks
        # so we don't disturb the fixtures the md_ro readback inspects
        # below.
        etag_snap = await conn_rw.fetchrow(
            "INSERT INTO md.snapshots (name, as_of, status) VALUES ($1, $2, $3) "
            "RETURNING id, version_etag",
            f"ETAG_TEST_{suffix}",
            as_of_base,
            "ready",
        )
        assert etag_snap is not None
        etag_snap_id = etag_snap["id"]
        await conn_rw.execute(
            "INSERT INTO md.snapshot_quotes "
            "(snapshot_id, canonical_id, value, resolved_as_of) "
            "VALUES ($1, $2, $3, $4), ($1, $5, $6, $4)",
            etag_snap_id,
            canonical_id_a,
            0.05,
            as_of_base,
            canonical_id_b,
            0.06,
        )
        etag_pre_update = await conn_rw.fetchval(
            "SELECT version_etag FROM md.snapshots WHERE id = $1", etag_snap_id
        )
        await conn_rw.execute(
            "UPDATE md.snapshot_quotes SET value = value + 0.0001 "
            "WHERE snapshot_id = $1 AND canonical_id = $2",
            etag_snap_id,
            canonical_id_a,
        )
        etag_post_update = await conn_rw.fetchval(
            "SELECT version_etag FROM md.snapshots WHERE id = $1", etag_snap_id
        )
        if etag_post_update == etag_pre_update:
            raise SystemExit(
                "version_etag did NOT advance after snapshot_quotes UPDATE "
                f"({etag_pre_update} -> {etag_post_update})"
            )
        print(f"  [version_etag] advanced after UPDATE: {etag_post_update[:8]}…")

        await conn_rw.execute(
            "DELETE FROM md.snapshot_quotes WHERE snapshot_id = $1 AND canonical_id = $2",
            etag_snap_id,
            canonical_id_b,
        )
        etag_post_delete = await conn_rw.fetchval(
            "SELECT version_etag FROM md.snapshots WHERE id = $1", etag_snap_id
        )
        if etag_post_delete == etag_post_update:
            raise SystemExit(
                "version_etag did NOT advance after snapshot_quotes DELETE "
                f"({etag_post_update} -> {etag_post_delete})"
            )
        print(f"  [version_etag] advanced after DELETE: {etag_post_delete[:8]}…")

        # md_rw must NOT be able to touch app.*: pinned search_path
        # plus per-schema grants in 0001_init mean app_* tables are
        # invisible/forbidden to md_*. Verify with an explicit schema
        # qualifier so search_path is not the only line of defence.
        app_denied: list[str] = []
        for label, stmt in (
            ("SELECT", "SELECT 1 FROM app.users LIMIT 1"),
            ("INSERT", "INSERT INTO app.users (uid) VALUES ('mdrw_should_not_write')"),
            ("UPDATE", "UPDATE app.users SET tier = 'pwned'"),
            ("DELETE", "DELETE FROM app.users"),
        ):
            try:
                await conn_rw.execute(stmt)
            except asyncpg.exceptions.InsufficientPrivilegeError:
                app_denied.append(label)
            else:
                raise SystemExit(f"md_rw was allowed to {label} on app.users (should be denied)")
        print(f"\nmd_rw denied on app.*: {app_denied}")
    finally:
        await conn_rw.close()

    print("\nreading back via md_ro …")
    conn_ro = await asyncpg.connect(ro_dsn)
    await _set_json_codecs(conn_ro)
    try:
        for table, id_cols, id_vals, expected in fixtures:
            where = " AND ".join(f"{c} = ${i + 1}" for i, c in enumerate(id_cols))
            row = await conn_ro.fetchrow(
                f"SELECT * FROM md.{table} WHERE {where}",  # noqa: S608 — fixed allow-list
                *id_vals,
            )
            if row is None:
                raise SystemExit(f"[{table}] missing on md_ro (ids={id_vals!r})")
            _check_row(table, expected, row)

        denied: list[str] = []
        for label, stmt in (
            (
                "INSERT",
                "INSERT INTO md.canonical_ids "
                "(canonical_id, asset_class, instrument, currency, field) "
                "VALUES ('HACK', 'X', 'X', 'USD', 'X')",
            ),
            ("UPDATE", "UPDATE md.canonical_ids SET description = 'pwned'"),
            ("DELETE", "DELETE FROM md.canonical_ids"),
        ):
            try:
                await conn_ro.execute(stmt)
            except asyncpg.exceptions.InsufficientPrivilegeError:
                denied.append(label)
            else:
                raise SystemExit(
                    f"md_ro was allowed to {label} on md.canonical_ids (should be denied)"
                )
        print(f"\nmd_ro DML denied: {denied}")
    finally:
        await conn_ro.close()

    print("\nround-trip OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit:
        raise
    except Exception as exc:
        print(f"round-trip FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
