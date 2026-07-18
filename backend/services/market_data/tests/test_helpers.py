"""Behavior-equivalence tests for the lifted ``_helpers`` module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from quantra_market_data._helpers import downsample_rows, iso


def test_iso_renders_aware_datetime_in_utc() -> None:
    dt = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    assert iso(dt) == "2026-05-14T12:00:00+00:00"


def test_iso_converts_non_utc_to_utc() -> None:
    eastern = datetime(2026, 5, 14, 7, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert iso(eastern) == "2026-05-14T12:00:00+00:00"


def test_iso_falls_back_to_str() -> None:
    assert iso("2026-05-14") == "2026-05-14"
    assert iso(42) == "42"


def test_downsample_rows_returns_input_when_under_cap() -> None:
    rows = list(range(5))
    assert downsample_rows(rows, 10) is rows


def test_downsample_rows_returns_input_when_max_is_none() -> None:
    rows = list(range(5))
    assert downsample_rows(rows, None) is rows


def test_downsample_rows_collapses_to_last_when_one() -> None:
    rows = list(range(5))
    assert downsample_rows(rows, 1) == [4]


def test_downsample_rows_preserves_endpoints_and_caps_count() -> None:
    rows = list(range(100))
    sampled = downsample_rows(rows, 10)
    assert sampled[0] == 0
    assert sampled[-1] == 99
    assert len(sampled) <= 10


def test_downsample_rows_is_idempotent_when_max_equals_len() -> None:
    rows = list(range(7))
    sampled = downsample_rows(rows, 7)
    assert sampled is rows


def test_downsample_rows_returns_input_when_max_zero() -> None:
    rows = list(range(5))
    assert downsample_rows(rows, 0) is rows


def test_downsample_rows_is_deterministic() -> None:
    rows = list(range(50))
    a = downsample_rows(rows, 7)
    b = downsample_rows(list(rows), 7)
    assert a == b
