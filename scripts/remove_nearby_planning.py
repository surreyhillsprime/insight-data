#!/usr/bin/env python3
"""Remove the retired nearby-planning payload from a transaction feed.

The migration is deliberately narrow and idempotent: it removes only the
top-level transaction ``planning`` field and ``dailyIntelligence.planning``
metadata. Static ``planningConstraints`` data and the separate property-level
planning-history feed are outside this script's scope.
"""

import argparse
import copy
import json
import sys
from pathlib import Path

from insight_data_utils import DEFAULT_INPUT_JS, read_js


def strip_nearby_planning(transactions, metadata):
    cleaned_transactions = []
    removed_rows = 0
    for item in transactions:
        cleaned = dict(item)
        if "planning" in cleaned:
            removed_rows += 1
            cleaned.pop("planning", None)
        cleaned_transactions.append(cleaned)

    cleaned_metadata = copy.deepcopy(metadata or {})
    daily = cleaned_metadata.get("dailyIntelligence")
    removed_metadata = isinstance(daily, dict) and "planning" in daily
    if isinstance(daily, dict):
        daily = dict(daily)
        daily.pop("planning", None)
        if any(key != "updatedAt" for key in daily):
            cleaned_metadata["dailyIntelligence"] = daily
        else:
            cleaned_metadata.pop("dailyIntelligence", None)

    return cleaned_transactions, cleaned_metadata, {
        "transactionPlanningFieldsRemoved": removed_rows,
        "planningMetadataRemoved": int(removed_metadata),
    }


def write_migrated_js(path, transactions, summary, metadata):
    """Write the migration without re-normalising unrelated feed fields."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(
        [
            "window.SURREY_LAND_REG_TRANSACTIONS = "
            + json.dumps(transactions, separators=(",", ":"))
            + ";",
            "window.SURREY_LAND_REG_SUMMARY = "
            + json.dumps(summary, separators=(",", ":"))
            + ";",
            "window.SURREY_LAND_REG_META = "
            + json.dumps(metadata, separators=(",", ":"))
            + ";",
            "",
        ]
    )
    destination.write_text(content, encoding="utf-8")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Remove retired nearby-planning data from an INSIGHT transaction feed."
    )
    parser.add_argument("--input-js", default=str(DEFAULT_INPUT_JS))
    parser.add_argument("--write-js", default=str(DEFAULT_INPUT_JS))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    transactions, summary, metadata = read_js(args.input_js)
    cleaned, cleaned_metadata, stats = strip_nearby_planning(
        transactions,
        metadata,
    )
    print(
        "Nearby-planning migration: "
        + ", ".join(f"{key}={value}" for key, value in stats.items())
    )
    if not args.dry_run:
        write_migrated_js(
            args.write_js,
            cleaned,
            summary,
            cleaned_metadata,
        )
        print(f"Updated {args.write_js}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
