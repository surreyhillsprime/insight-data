#!/usr/bin/env python3
"""Validate the standalone planning-history.js publication contract."""

import argparse
import json
import re
from pathlib import Path


def assignment(text, name):
    matches = re.findall(rf"^window\.{re.escape(name)}\s*=\s*(.*);$", text, flags=re.M)
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one window.{name} assignment")
    value = json.loads(matches[0])
    if not isinstance(value, dict):
        raise ValueError(f"window.{name} must be an object")
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default="outputs/planning-history.js")
    parser.add_argument("--base-feed", default="")
    args = parser.parse_args()
    text = Path(args.path).read_text(encoding="utf-8")
    histories = assignment(text, "SURREY_PLANNING_HISTORY")
    metadata = assignment(text, "SURREY_PLANNING_HISTORY_META")
    if metadata.get("schemaVersion") != 1 or metadata.get("deploymentMode") != "commercial":
        raise ValueError("Commercial planning metadata is missing schemaVersion 1 or deploymentMode commercial")
    if len(histories) > 200_000:
        raise ValueError("Planning history exceeds the app safety limit")
    if args.base_feed:
        base_text = Path(args.base_feed).read_text(encoding="utf-8")
        base_rows = json.loads(re.findall(r"^window\.SURREY_LAND_REG_TRANSACTIONS\s*=\s*(.*);$", base_text, flags=re.M)[0])
        expected = {str(item.get("propertyRecordId") or "") for item in base_rows}
        expected.discard("")
        expected_transactions = {str(item.get("id") or "") for item in base_rows}
        expected_transactions.discard("")
        actual_properties = {key for key in histories if key.startswith("property:")}
        actual_transactions = set(histories) - actual_properties
        unexpected_properties = actual_properties - expected
        unexpected_transactions = actual_transactions - expected_transactions
        if metadata.get("propertiesChecked") != len(expected):
            raise ValueError(
                f"Planning history property coverage is stale: expected {len(expected):,}, "
                f"found {metadata.get('propertiesChecked', 'missing')} checked"
            )
        if unexpected_properties or unexpected_transactions:
            raise ValueError(
                "Planning history contains lookup keys outside the canonical base feed: "
                f"{len(unexpected_properties):,} property keys / "
                f"{len(unexpected_transactions):,} transaction keys"
            )
        if metadata.get("propertiesWithHistory") != len(actual_properties):
            raise ValueError(
                "Planning history metadata disagrees with emitted canonical property keys: "
                f"declared {metadata.get('propertiesWithHistory', 'missing')}, "
                f"found {len(actual_properties):,}"
            )
    print(f"Valid commercial planning feed: {len(histories):,} lookup keys")


if __name__ == "__main__":
    main()
