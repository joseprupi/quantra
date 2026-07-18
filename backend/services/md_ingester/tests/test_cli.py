"""CLI parser tests.

The CLI is the user-visible surface; argparse mistakes here are the
hardest kind to spot in a scheduled-job context (nothing wakes up
unless an operator inspects the exit message). Pin the parse-shape
explicitly.
"""

from __future__ import annotations

from datetime import date

import pytest

from quantra_md_ingester.cli import _window_start, build_parser


def test_build_parser_requires_a_subcommand() -> None:
    parser = build_parser()
    # argparse raises SystemExit on a missing subcommand.
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_ingest_defaults_to_one_day_window() -> None:
    parser = build_parser()
    args = parser.parse_args(["ingest"])
    assert args.command == "ingest"
    assert args.as_of is None
    assert args.start_date is None


def test_ingest_accepts_explicit_window_and_sources() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "ingest",
            "--as-of",
            "2026-05-13",
            "--start-date",
            "2026-05-07",
            "--source",
            "fred",
        ]
    )
    assert args.as_of.isoformat() == "2026-05-13"
    assert args.start_date.isoformat() == "2026-05-07"
    assert args.sources == "fred"


def test_backfill_requires_start_date() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["backfill", "--as-of", "2026-05-13"])


def test_build_snapshot_defaults_as_of_to_today() -> None:
    # bare ``build-snapshot`` defaults ``--as-of`` to today's UTC date so a
    # scheduled cron tick doesn't need per-invocation date injection.
    # Historical rebuilds still pass ``--as-of`` explicitly.
    parser = build_parser()
    args = parser.parse_args(["build-snapshot"])
    assert args.command == "build-snapshot"
    assert args.as_of is None  # ``None`` here ⇒ defaulted later via _as_of_or_today.


def test_build_snapshot_accepts_custom_name() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["build-snapshot", "--as-of", "2026-05-13", "--snapshot-name", "TEST_SNAP"]
    )
    assert args.command == "build-snapshot"
    assert args.snapshot_name == "TEST_SNAP"


def test_ingest_since_month_start_flag() -> None:
    parser = build_parser()
    plain = parser.parse_args(["ingest", "--source", "boe_ois"])
    assert plain.since_month_start is False
    flagged = parser.parse_args(["ingest", "--source", "boe_ois", "--since-month-start"])
    assert flagged.since_month_start is True


def test_window_start_resolution() -> None:
    parser = build_parser()
    as_of = date(2026, 7, 15)

    # Explicit --start-date always wins.
    explicit = parser.parse_args(["ingest", "--source", "boe_ois", "--start-date", "2026-07-01"])
    assert _window_start(explicit, as_of) == date(2026, 7, 1)

    # --since-month-start anchors at the first of as_of's month.
    monthly = parser.parse_args(["ingest", "--source", "boe_ois", "--since-month-start"])
    assert _window_start(monthly, as_of) == date(2026, 7, 1)

    # Neither ⇒ None (one-day incremental; ingest_quotes defaults start=as_of).
    plain = parser.parse_args(["ingest", "--source", "boe_ois"])
    assert _window_start(plain, as_of) is None


def test_roll_curve_dates_subcommand() -> None:
    parser = build_parser()
    args = parser.parse_args(["roll-curve-dates"])
    assert args.command == "roll-curve-dates"
    assert args.owner_uid is None  # ⇒ env / DEFAULT_ROLL_OWNER_UID at dispatch.
    scoped = parser.parse_args(["roll-curve-dates", "--owner-uid", "someone"])
    assert scoped.owner_uid == "someone"
