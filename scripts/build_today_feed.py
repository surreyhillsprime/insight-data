#!/usr/bin/env python3
"""Build the canonical INSIGHT Today feed from published local evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from property_records import read_property_records_js
from today_feed import build_today_feed, write_today_feed


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROPERTY_RECORDS = ROOT / "outputs" / "property-records.js"
DEFAULT_OUTPUT = ROOT / "outputs" / "today-feed.js"


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--property-records", type=Path, default=DEFAULT_PROPERTY_RECORDS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--as-of", help="Evidence snapshot date in YYYY-MM-DD form.")
    parser.add_argument("--generated-at", help="Reproducible ISO-8601 generation timestamp.")
    parser.add_argument("--epc-lookback-days", type=int, default=30)
    parser.add_argument("--planning-lookback-days", type=int, default=45)
    parser.add_argument("--sale-age-crossing-window-days", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    records, property_metadata = read_property_records_js(args.property_records)
    feed, metadata = build_today_feed(
        records,
        property_metadata,
        as_of=args.as_of,
        generated_at=args.generated_at,
        epc_lookback_days=args.epc_lookback_days,
        planning_lookback_days=args.planning_lookback_days,
        sale_age_crossing_window_days=args.sale_age_crossing_window_days,
    )
    if not args.dry_run:
        write_today_feed(args.output, feed, metadata)
    print(json.dumps({
        "output": None if args.dry_run else str(args.output),
        "asOf": feed["asOf"],
        "counts": metadata["counts"],
        "datasetFingerprint": metadata["datasetFingerprint"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as error:
        print(f"Today feed build failed: {error}", file=sys.stderr)
        raise SystemExit(1)
