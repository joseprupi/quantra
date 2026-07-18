"""Ad-hoc round-trip verification for the app.* schema.

Not a pytest test — touching a live Postgres from the test suite would
break CI. This is the kind of script humans run by hand after a fresh
``alembic -n app upgrade head`` to convince themselves that:

1. Each entity table accepts a representative INSERT through ``app_rw``.
2. The same row reads back through ``app_ro`` byte-for-byte.
3. ``app_ro`` is denied INSERT / UPDATE / DELETE.
4. The ``app.set_updated_at()`` trigger bumps ``updated_at`` on UPDATE.

Run from the repo root:

    uv run python migrations/scripts/roundtrip_app_schema.py

Reads role DSNs from the same ``.env`` the migration env.py uses, so it
follows the role pinning configured by ``0001_init.py``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
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


async def main() -> None:  # noqa: PLR0915 -- one big linear script; refactor for size hurts readability
    settings = Settings()
    rw_dsn = _dsn_for_asyncpg(settings.require_postgres_dsn_app_rw())
    ro_dsn = _dsn_for_asyncpg(settings.require_postgres_dsn_app_ro())

    conn_rw = await asyncpg.connect(rw_dsn)
    await _set_json_codecs(conn_rw)
    try:
        owner_uid = f"firebase_{uuid.uuid4().hex[:8]}"
        await conn_rw.execute(
            "INSERT INTO app.users (uid, email, display_name, tier) VALUES ($1, $2, $3, $4)",
            owner_uid,
            f"{owner_uid}@example.test",
            "Test User",
            "free",
        )
        print(f"seeded owner_uid={owner_uid}")

        # (table, id_col, id_val, expected-fields). The list grows as each
        # entity is inserted; we replay it through `app_ro` at the end.
        fixtures: list[tuple[str, str, Any, dict[str, Any]]] = []

        # api_keys
        api_key_row = await conn_rw.fetchrow(
            "INSERT INTO app.api_keys (owner_uid, name, key_hash) "
            "VALUES ($1, $2, $3) RETURNING id, name, key_hash, active",
            owner_uid,
            "primary",
            "sha256:" + uuid.uuid4().hex,
        )
        assert api_key_row is not None
        fixtures.append(
            (
                "api_keys",
                "id",
                api_key_row["id"],
                {
                    "name": api_key_row["name"],
                    "key_hash": api_key_row["key_hash"],
                    "active": api_key_row["active"],
                },
            )
        )

        # indices
        idx_row = await conn_rw.fetchrow(
            "INSERT INTO app.indices (owner_uid, name, kind, currency, calendar, "
            "day_counter, body) VALUES ($1, $2, $3, $4, $5, $6, $7) "
            "RETURNING id, name, kind, body",
            owner_uid,
            "Euribor 3M",
            "IBOR",
            "EUR",
            "TARGET",
            "Actual360",
            {"family": "Euribor", "tenor_number": 3, "tenor_time_unit": "Months"},
        )
        assert idx_row is not None
        fixtures.append(
            (
                "indices",
                "id",
                idx_row["id"],
                {"name": idx_row["name"], "kind": idx_row["kind"], "body": idx_row["body"]},
            )
        )

        # curves
        curve_row = await conn_rw.fetchrow(
            "INSERT INTO app.curves (owner_uid, name, currency, day_counter, helper_kind, "
            "reference_date, points, body) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
            "RETURNING id, name, currency, points, body",
            owner_uid,
            "EUR Discount",
            "EUR",
            "Actual365Fixed",
            "discount",
            __import__("datetime").date(2026, 5, 14),
            [{"tenor": "1Y", "rate": 0.025}, {"tenor": "5Y", "rate": 0.028}],
            {"interpolator": "LogLinear", "bootstrap_trait": "Discount"},
        )
        assert curve_row is not None
        fixtures.append(
            (
                "curves",
                "id",
                curve_row["id"],
                {
                    "name": curve_row["name"],
                    "currency": curve_row["currency"],
                    "points": curve_row["points"],
                    "body": curve_row["body"],
                },
            )
        )

        # curve_sets — soft refs the curve we just inserted.
        cset_row = await conn_rw.fetchrow(
            "INSERT INTO app.curve_sets (owner_uid, name, currency, body) "
            "VALUES ($1, $2, $3, $4) RETURNING id, body",
            owner_uid,
            "EUR Standard",
            "EUR",
            {
                "curve_refs": [
                    {"id": "csref_1", "curve_id": str(curve_row["id"]), "role": "discount"}
                ],
                "credit_curve_ids": [],
                "quote_ids": [],
            },
        )
        assert cset_row is not None
        fixtures.append(("curve_sets", "id", cset_row["id"], {"body": cset_row["body"]}))

        # credit_curves
        cc_row = await conn_rw.fetchrow(
            "INSERT INTO app.credit_curves (owner_uid, name, reference_entity, currency, "
            "seniority, source, recovery_rate, body) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
            "RETURNING id, source, recovery_rate",
            owner_uid,
            "ACME SR EUR",
            "ACME",
            "EUR",
            "SR_UNSECURED",
            "flat",
            __import__("decimal").Decimal("0.4000"),
            {"flat_hazard_rate": 0.02},
        )
        assert cc_row is not None
        fixtures.append(
            (
                "credit_curves",
                "id",
                cc_row["id"],
                {"source": cc_row["source"], "recovery_rate": cc_row["recovery_rate"]},
            )
        )

        # snapshots
        snap_row = await conn_rw.fetchrow(
            "INSERT INTO app.snapshots (owner_uid, name, as_of, content) "
            "VALUES ($1, $2, $3, $4) RETURNING id, as_of, content",
            owner_uid,
            "EOD 2026-05-14",
            __import__("datetime").date(2026, 5, 14),
            {"quotes": {"EURIBOR_3M": 0.0325}},
        )
        assert snap_row is not None
        fixtures.append(
            (
                "snapshots",
                "id",
                snap_row["id"],
                {"as_of": snap_row["as_of"], "content": snap_row["content"]},
            )
        )

        # quote_book
        qb_row = await conn_rw.fetchrow(
            "INSERT INTO app.quote_book (owner_uid, resolution_mode, entries) "
            "VALUES ($1, $2, $3) RETURNING id, resolution_mode, entries",
            owner_uid,
            "previous",
            [
                {
                    "id": "EURIBOR_3M",
                    "kind": "Rate",
                    "series": [{"date": "2026-05-14", "value": 0.0325}],
                }
            ],
        )
        assert qb_row is not None
        fixtures.append(
            (
                "quote_book",
                "id",
                qb_row["id"],
                {"resolution_mode": qb_row["resolution_mode"], "entries": qb_row["entries"]},
            )
        )

        # vol_surfaces
        vs_row = await conn_rw.fetchrow(
            "INSERT INTO app.vol_surfaces (owner_uid, name, kind, payload) "
            "VALUES ($1, $2, $3, $4) RETURNING id, kind, payload",
            owner_uid,
            "EUR Swaption ATM",
            "SwaptionVolSpec",
            {
                "payload_type": "SwaptionVolSpec",
                "base": {"volatility_type": "Normal", "day_counter": "Actual365Fixed"},
                "expiries": [1.0, 5.0],
                "tenors": [1.0, 5.0],
                "grid": [[0.005, 0.006], [0.0065, 0.007]],
            },
        )
        assert vs_row is not None
        fixtures.append(
            (
                "vol_surfaces",
                "id",
                vs_row["id"],
                {"kind": vs_row["kind"], "payload": vs_row["payload"]},
            )
        )

        # swaption_models
        sm_row = await conn_rw.fetchrow(
            "INSERT INTO app.swaption_models (owner_uid, name, kind, payload) "
            "VALUES ($1, $2, $3, $4) RETURNING id, kind, payload",
            owner_uid,
            "HW EUR 2026-05-14",
            "HullWhiteLattice",
            {
                "hw_a": 0.03,
                "hw_sigma": 0.01,
                "vol_surface_id": str(vs_row["id"]),
                "rmse": 0.0008,
            },
        )
        assert sm_row is not None
        fixtures.append(
            (
                "swaption_models",
                "id",
                sm_row["id"],
                {"kind": sm_row["kind"], "payload": sm_row["payload"]},
            )
        )

        # All seven product tables share the request-body shape, so loop.
        product_request: dict[str, Any] = {
            "pricing": {"as_of_date": "2026-05-14"},
            "items": [{"id": "X1"}],
        }
        product_table_to_id: dict[str, uuid.UUID] = {}
        for table in (
            "swaps_ir",
            "swaps_inflation",
            "swaptions",
            "bonds_fixed",
            "bonds_floating",
            "cds",
            "equity_options",
        ):
            prod_row = await conn_rw.fetchrow(
                f"INSERT INTO app.{table} (owner_uid, name, request) "  # noqa: S608 — fixed allow-list
                f"VALUES ($1, $2, $3) RETURNING id, name, request",
                owner_uid,
                f"{table} demo",
                product_request,
            )
            assert prod_row is not None
            product_table_to_id[table] = prod_row["id"]
            fixtures.append(
                (
                    table,
                    "id",
                    prod_row["id"],
                    {"name": prod_row["name"], "request": prod_row["request"]},
                )
            )

        # pricing_history — soft ref to the swaps_ir row we just inserted.
        ph_row = await conn_rw.fetchrow(
            "INSERT INTO app.pricing_history "
            "(owner_uid, product_kind, product_id, as_of, request, response) "
            "VALUES ($1, $2, $3, $4, $5, $6) "
            "RETURNING id, product_kind, product_id, response",
            owner_uid,
            "swaps_ir",
            product_table_to_id["swaps_ir"],
            __import__("datetime").date(2026, 5, 14),
            product_request,
            {"npv": 12345.67, "currency": "EUR"},
        )
        assert ph_row is not None
        fixtures.append(
            (
                "pricing_history",
                "id",
                ph_row["id"],
                {
                    "product_kind": ph_row["product_kind"],
                    "product_id": ph_row["product_id"],
                    "response": ph_row["response"],
                },
            )
        )

        # `app.set_updated_at()` trigger sanity check.
        prev_ts = await conn_rw.fetchval(
            "SELECT updated_at FROM app.users WHERE uid = $1", owner_uid
        )
        # `pg_sleep(0.01)` keeps the comparison meaningful when the wall clock
        # advances faster than `now()`'s resolution between back-to-back
        # statements in the same transaction.
        await conn_rw.execute("SELECT pg_sleep(0.01)")
        await conn_rw.execute("UPDATE app.users SET tier = 'pro' WHERE uid = $1", owner_uid)
        new_ts = await conn_rw.fetchval(
            "SELECT updated_at FROM app.users WHERE uid = $1", owner_uid
        )
        if not new_ts > prev_ts:
            raise SystemExit(f"updated_at trigger did not advance ({prev_ts!r} -> {new_ts!r})")
        print("  [trigger] updated_at advances on UPDATE: OK")
    finally:
        await conn_rw.close()

    print("\nreading back via app_ro …")
    conn_ro = await asyncpg.connect(ro_dsn)
    await _set_json_codecs(conn_ro)
    try:
        for table, id_col, id_val, expected in fixtures:
            row = await conn_ro.fetchrow(
                f"SELECT * FROM app.{table} WHERE {id_col} = $1",  # noqa: S608 — fixed allow-list
                id_val,
            )
            if row is None:
                raise SystemExit(f"[{table}] missing on app_ro (id={id_val!r})")
            _check_row(table, expected, row)

        denied: list[str] = []
        for label, stmt in (
            ("INSERT", "INSERT INTO app.users (uid) VALUES ('hacker')"),
            ("UPDATE", "UPDATE app.users SET tier = 'pwned'"),
            ("DELETE", "DELETE FROM app.users"),
        ):
            try:
                await conn_ro.execute(stmt)
            except asyncpg.exceptions.InsufficientPrivilegeError:
                denied.append(label)
            else:
                raise SystemExit(f"app_ro was allowed to {label} on app.users (should be denied)")
        print(f"\napp_ro DML denied: {denied}")
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
