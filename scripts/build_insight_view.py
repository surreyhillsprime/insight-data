#!/usr/bin/env python3
"""Build the canonical daily INSIGHT View JavaScript asset."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from insight_view import (
    TIME_ZONE,
    InsightViewValidationError,
    build_insight_view,
    load_snapshot,
    write_insight_view,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = ROOT / "config" / "insight-view-snapshot.json"
DEFAULT_OUTPUT = ROOT / "outputs" / "insight-view.js"


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--as-of", help="Europe/London briefing date in YYYY-MM-DD form.")
    parser.add_argument("--generated-at", help="Reproducible ISO-8601 generation timestamp.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    as_of = args.as_of or now.astimezone(ZoneInfo(TIME_ZONE)).date().isoformat()
    generated_at = args.generated_at or now.isoformat().replace("+00:00", "Z")
    view = build_insight_view(
        load_snapshot(args.snapshot),
        briefing_date=as_of,
        generated_at=generated_at,
    )
    if not args.dry_run:
        write_insight_view(args.output, view)
    print(
        json.dumps(
            {
                "output": None if args.dry_run else str(args.output),
                "asOf": view["briefingDate"],
                "generatedAt": view["generatedAt"],
                "bankRate": view["policy"]["bankRate"],
                "nextDecision": view["policy"]["nextDecisionDate"],
                "mortgageRate": view["mortgage"]["rate"],
                "fingerprint": view["fingerprint"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError, InsightViewValidationError) as error:
        print(f"INSIGHT View build failed: {error}", file=sys.stderr)
        raise SystemExit(1)
