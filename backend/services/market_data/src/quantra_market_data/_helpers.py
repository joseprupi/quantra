"""Tiny helpers used across the MD route modules."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def iso(value: Any) -> str:  # noqa: ANN401 — callers pass arbitrary DB row values
    """Format a value as an ISO-8601 string.

    Matches the legacy ``_iso`` helper byte-for-byte: aware datetimes are
    converted to UTC; everything else falls through to ``str()``.
    """

    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


def downsample_rows[T](rows: list[T], max_points: int | None) -> list[T]:
    """Evenly sample ``rows`` to at most ``max_points``, preserving endpoints.

    Lifted verbatim from the legacy service. Sampling is deterministic and
    stable: indices are computed by linear interpolation, the first and last
    rows are always included, and duplicates from rounding are dropped.
    """

    if not max_points or max_points <= 0 or len(rows) <= max_points:
        return rows
    if max_points == 1:
        return [rows[-1]]

    last_index = len(rows) - 1
    step = last_index / float(max_points - 1)
    sampled: list[T] = []
    used: set[int] = set()
    for i in range(max_points):
        idx = round(i * step)
        idx = max(0, min(last_index, idx))
        if idx in used:
            continue
        used.add(idx)
        sampled.append(rows[idx])
    if sampled[0] is not rows[0]:
        sampled[0] = rows[0]
    if sampled[-1] is not rows[-1]:
        sampled[-1] = rows[-1]
    return sampled
