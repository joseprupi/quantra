#!/usr/bin/env python3
"""
Generate a 1-year (business days) quote book file from an existing backup.

Usage:
  python3 scripts/generate_market_data_quotes.py \
    --input public/example-backup.json \
    --output public/example-market-data.json \
    --seed 42
"""
import argparse
import json
import math
import random
from datetime import date, datetime, timedelta


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate market data quote book from backup.json")
    parser.add_argument("--input", required=True, help="Path to existing backup JSON")
    parser.add_argument("--output", required=True, help="Path to write market data JSON")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducible data")
    parser.add_argument("--days", type=int, default=252, help="Business days to generate (default 252)")
    return parser.parse_args()


def business_days_ending(end_date: date, count: int) -> list[date]:
    days: list[date] = []
    d = end_date
    while len(days) < count:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def clamp_positive(value: float, floor: float) -> float:
    return max(value, floor)


def generate_series(kind: str, base: float, dates: list[date]) -> list[dict]:
    # Simple random walk with small daily noise
    value = base
    series = []
    for i, d in enumerate(dates):
        if i > 0:
            shock = random.gauss(0, 0.0005)  # daily noise
            value += shock
        if kind in ("Rate", "Spread"):
            value = clamp_positive(value, 0.0001)
        elif kind in ("Price",):
            value = clamp_positive(value, 0.01)
        series.append({"date": d.isoformat(), "value": round(value, 6)})
    return series


def main() -> None:
    args = parse_args()
    random.seed(args.seed)

    with open(args.input, "r", encoding="utf-8") as fh:
        backup = json.load(fh)

    exported_at = backup.get("exportedAt")
    if exported_at:
        try:
            end_dt = datetime.fromisoformat(exported_at.replace("Z", "+00:00")).date()
        except ValueError:
            end_dt = date.today()
    else:
        end_dt = date.today()

    quotes = backup.get("quotes", [])
    if not isinstance(quotes, list) or len(quotes) == 0:
        raise SystemExit("No quotes found in input backup")

    dates = business_days_ending(end_dt, args.days)
    quote_book = []

    for q in quotes:
        qid = q.get("id")
        if not qid:
            continue
        kind = q.get("kind", "Rate")
        base = q.get("value")
        if base is None:
            if kind in ("Rate", "Spread"):
                base = 0.02
            elif kind in ("Price",):
                base = 100.0
            else:
                base = 1.0

        series = generate_series(kind, float(base), dates)
        quote_book.append({
            "id": qid,
            "kind": kind,
            "quote_type": q.get("quote_type"),
            "label": q.get("label"),
            "currency": q.get("currency"),
            "description": q.get("description"),
            "series": series,
        })

    output = {
        "version": backup.get("version", "1.1"),
        "exportedAt": datetime.combine(end_dt, datetime.min.time()).isoformat() + "Z",
        "quotes": quotes,
        "quoteBook": quote_book,
    }

    if "volSurfaces" in backup:
        output["volSurfaces"] = backup.get("volSurfaces")

    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2)
        fh.write("\n")

    print(f"Wrote market data file: {args.output}")


if __name__ == "__main__":
    main()
