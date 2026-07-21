#!/usr/bin/env python3
"""Validate the standalone commercial sales-history.js contract."""

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
    parser.add_argument("path", nargs="?", default="outputs/sales-history.js")
    parser.add_argument("--allow-local", action="store_true")
    parser.add_argument("--base-feed", default="")
    args = parser.parse_args()
    text = Path(args.path).read_text(encoding="utf-8")
    histories = assignment(text, "SURREY_SALES_HISTORY")
    metadata = assignment(text, "SURREY_SALES_HISTORY_META")
    expected_mode = {"commercial", "local"} if args.allow_local else {"commercial"}
    if metadata.get("schemaVersion") != 1 or metadata.get("deploymentMode") not in expected_mode:
        raise ValueError("Sales history metadata has an invalid schemaVersion or deploymentMode")
    if len(histories) > 200_000:
        raise ValueError("Sales history exceeds the app safety limit")
    property_records = {
        key: value
        for key, value in histories.items()
        if key.startswith("property:")
    }
    for key, record in property_records.items():
        if not isinstance(record, dict) or record.get("propertyRecordId") not in (None, key):
            raise ValueError("Sales-history record points at a different canonical property")
        if record.get("coverageStatus") not in (
            None,
            "complete",
            "partial",
            "unavailable",
            "not_checked",
        ):
            raise ValueError("Sales-history record has an invalid coverage status")
    if args.base_feed:
        base_text = Path(args.base_feed).read_text(encoding="utf-8")
        base_rows = json.loads(re.findall(r"^window\.SURREY_LAND_REG_TRANSACTIONS\s*=\s*(.*);$", base_text, flags=re.M)[0])
        expected = {str(item.get("propertyRecordId") or "") for item in base_rows}
        expected.discard("")
        actual = set(property_records)
        requested = metadata.get("propertiesRequested", metadata.get("propertiesChecked"))
        accounted = sum(
            int(metadata.get(field) or 0)
            for field in ("propertiesChecked", "propertiesUnavailable", "propertiesNotChecked")
        )
        if actual != expected or requested != len(expected) or accounted != len(expected):
            raise ValueError(
                f"Sales history property coverage is stale: expected {len(expected):,}, "
                f"found {len(actual):,} keys / {requested} requested / {accounted} accounted"
            )
    print(f"Valid sales history feed: {len(histories):,} lookup keys")


if __name__ == "__main__":
    main()
