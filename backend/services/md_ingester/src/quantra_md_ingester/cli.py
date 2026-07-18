"""``argparse`` CLI for the MD ingester.

Subcommands are routed through a clean stdlib ``argparse`` parser (no
Click/Typer dependency) because every subcommand has the same handful of
flags and the added libraries would be the largest dependency in the
service.

Subcommand semantics:

* ``ingest``      — fetch quotes for the given window and upsert them.
                    No snapshot. ``--start-date`` defaults to ``--as-of``
                    so the default is a one-day incremental.
* ``backfill``    — alias for ``ingest`` that requires ``--start-date``
                    explicitly so a multi-day pull is intentional.
* ``build-snapshot`` — rebuild the named snapshot from data already in
                       ``quote_points``. Fires the trigger.
* ``roll-curve-dates`` — bump the MD-backed demo curves' reference_date
                         to the latest ingested business day.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncEngine

from quantra_common.logging import configure_logging, get_logger
from quantra_md_ingester.pipeline import (
    ALL_SOURCES,
    DEFAULT_SNAPSHOT_NAME,
    DEFAULT_SOURCES,
    SnapshotBuildResult,
    build_engine,
    build_snapshot,
    ingest_quotes,
)
from quantra_md_ingester.roll import (
    DEFAULT_ROLL_OWNER_UID,
    build_app_rw_engine,
    roll_curve_dates,
)
from quantra_md_ingester.settings import MdIngesterSettings, get_md_ingester_settings

logger = get_logger(__name__)

# Bare invocations default to the real-vendor set; ``synthetic`` is
# opt-in via an explicit ``--source synthetic`` so demo data never silently
# mixes into a real-vendor run. The allow-list (validated against) still
# includes it.
_DEFAULT_SOURCES_STR: Final[str] = ",".join(sorted(DEFAULT_SOURCES))
_ALLOWED_SOURCES_STR: Final[str] = ",".join(sorted(ALL_SOURCES))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quantra-md-ingester",
        description=(
            "Scheduled-worker entry point for vendor market-data ingestion. "
            "Writes only to md.* via the md_rw pool (quantra_common.db)."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest_p = subparsers.add_parser(
        "ingest",
        help="Fetch and upsert quotes for the given window (no snapshot).",
    )
    _add_window_args(ingest_p)
    _add_sources_arg(ingest_p)

    backfill_p = subparsers.add_parser(
        "backfill",
        help=("Fetch and upsert quotes for an explicit historical window. Requires --start-date."),
    )
    _add_window_args(backfill_p, require_start=True)
    _add_sources_arg(backfill_p)

    snap_p = subparsers.add_parser(
        "build-snapshot",
        help="Rebuild a named snapshot from data already in quote_points.",
    )
    # ``--as-of`` defaults to today's UTC date so a scheduled job can
    # declare a bare ``build-snapshot`` command (no per-tick date
    # injection). Historical rebuilds still pass ``--as-of`` explicitly.
    snap_p.add_argument("--as-of", default=None, type=_parse_date)
    snap_p.add_argument("--snapshot-name", default=DEFAULT_SNAPSHOT_NAME)

    roll_p = subparsers.add_parser(
        "roll-curve-dates",
        help=(
            "Bump the real MD-backed demo curves' reference_date to the latest "
            "ingested business day for the series they reference (run AFTER the "
            "real ingests). Marker- and owner-scoped; idempotent."
        ),
    )
    roll_p.add_argument(
        "--owner-uid",
        default=None,
        help=(
            "Owner of the curves to roll. Defaults to QUANTRA_ROLL_OWNER_UID, "
            f"then {DEFAULT_ROLL_OWNER_UID!r} (the bundle's implicit dev-user)."
        ),
    )

    return parser


def _add_window_args(parser: argparse.ArgumentParser, *, require_start: bool = False) -> None:
    parser.add_argument(
        "--as-of",
        default=None,
        type=_parse_date,
        help="End date of the window (UTC, YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--start-date",
        required=require_start,
        default=None,
        type=_parse_date,
        help=(
            "Start date of the window (UTC, YYYY-MM-DD). Defaults to --as-of "
            "for a one-day incremental."
        ),
    )
    parser.add_argument(
        "--since-month-start",
        action="store_true",
        help=(
            "Set the window start to the first of --as-of's month when "
            "--start-date is not given. Used for the real curve feeds (e.g. "
            "boe_ois): a current-month window pulls the fresher 'latest' "
            "workbook and captures the most recent published fixing rather "
            "than an empty one-day window on a date the feed has no data for."
        ),
    )


def _add_sources_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--source",
        "--sources",
        dest="sources",
        default=_DEFAULT_SOURCES_STR,
        help=(
            "Comma-separated vendor identifiers. Allowed values: "
            f"{_ALLOWED_SOURCES_STR} (default: {_DEFAULT_SOURCES_STR}). "
            "'synthetic' generates demo standing data and must be "
            "requested explicitly. Aliases: --source (singular)."
        ),
    )


def _parse_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        msg = f"Invalid date '{raw}'. Expected YYYY-MM-DD."
        raise argparse.ArgumentTypeError(msg) from exc


def _parse_sources(raw: str) -> set[str]:
    parsed = {item.strip().lower() for item in raw.split(",") if item.strip()}
    if not parsed:
        msg = "At least one source must be provided via --source / --sources."
        raise RuntimeError(msg)
    unknown = parsed.difference(ALL_SOURCES)
    if unknown:
        msg = f"Unsupported source(s): {', '.join(sorted(unknown))}"
        raise RuntimeError(msg)
    return parsed


def _as_of_or_today(value: date | None) -> date:
    return value if value is not None else datetime.now(UTC).date()


def _window_start(args: argparse.Namespace, as_of: date) -> date | None:
    """Resolve the ingest window start.

    An explicit ``--start-date`` always wins; otherwise ``--since-month-start``
    anchors the window at the first of ``as_of``'s month (so the real feeds
    pull the fresher current-month workbook); otherwise ``None`` → a one-day
    incremental (``ingest_quotes`` defaults start to ``as_of``).
    """

    if args.start_date is not None:
        return args.start_date  # type: ignore[no-any-return]
    if getattr(args, "since_month_start", False):
        return as_of.replace(day=1)
    return None


async def _dispatch(
    args: argparse.Namespace,
    engine: AsyncEngine,
    settings: MdIngesterSettings,
) -> dict[str, object]:
    if args.command == "ingest":
        as_of = _as_of_or_today(args.as_of)
        start = _window_start(args, as_of)
        result = await ingest_quotes(
            engine=engine,
            as_of=as_of,
            start_date=start,
            sources=_parse_sources(args.sources),
        )
        return {
            "command": "ingest",
            "as_of": as_of.isoformat(),
            "start_date": start.isoformat() if start else as_of.isoformat(),
            **asdict(result),
        }

    if args.command == "backfill":
        as_of = _as_of_or_today(args.as_of)
        start = _window_start(args, as_of)
        result = await ingest_quotes(
            engine=engine,
            as_of=as_of,
            start_date=start,
            sources=_parse_sources(args.sources),
        )
        return {
            "command": "backfill",
            "as_of": as_of.isoformat(),
            "start_date": start.isoformat() if start else None,
            **asdict(result),
        }

    if args.command == "roll-curve-dates":
        owner_uid = (
            args.owner_uid or os.environ.get("QUANTRA_ROLL_OWNER_UID") or DEFAULT_ROLL_OWNER_UID
        )
        app_engine = build_app_rw_engine(settings=settings)
        try:
            rolled = await roll_curve_dates(
                app_engine=app_engine,
                md_engine=engine,
                owner_uid=owner_uid,
            )
        finally:
            await app_engine.dispose()
        return {
            "command": "roll-curve-dates",
            "owner_uid": owner_uid,
            "rolled": [r.as_dict() for r in rolled],
        }

    if args.command == "build-snapshot":
        as_of = _as_of_or_today(args.as_of)
        snap = await build_snapshot(
            engine=engine,
            as_of=as_of,
            snapshot_name=args.snapshot_name,
        )
        return {"command": "build-snapshot", **_snapshot_to_dict(snap)}

    msg = f"Unknown command: {args.command!r}"
    raise RuntimeError(msg)


def _snapshot_to_dict(snap: SnapshotBuildResult) -> dict[str, object]:
    """Render a snapshot result for JSON output.

    ``asdict`` would emit a ``UUID`` and a ``datetime`` which json.dumps
    cannot serialise; we coerce here so the CLI's printed payload stays
    cleanly stringifiable.
    """

    return {
        "snapshot_id": str(snap.snapshot_id),
        "name": snap.name,
        "as_of": snap.as_of.isoformat(),
        "quotes_written": snap.quotes_written,
        "quotes_changed": snap.quotes_changed,
        "version_etag_before": snap.version_etag_before,
        "version_etag_after": snap.version_etag_after,
    }


async def _main_async(argv: Sequence[str] | None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    settings: MdIngesterSettings = get_md_ingester_settings()
    configure_logging(settings)

    engine = build_engine(settings=settings)
    try:
        result = await _dispatch(args, engine, settings)
    finally:
        await engine.dispose()

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_main_async(argv))


__all__ = ["build_parser", "main"]


# Keep the module importable for direct calls but also runnable.
if __name__ == "__main__":  # pragma: no cover - exercised by __main__.py
    sys.exit(main())
