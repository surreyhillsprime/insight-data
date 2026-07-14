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
    args = parser.parse_args()
    text = Path(args.path).read_text(encoding="utf-8")
    histories = assignment(text, "SURREY_SALES_HISTORY")
    metadata = assignment(text, "SURREY_SALES_HISTORY_META")
    expected_mode = {"commercial", "local"} if args.allow_local else {"commercial"}
    if metadata.get("schemaVersion") != 1 or metadata.get("deploymentMode") not in expected_mode:
        raise ValueError("Sales history metadata has an invalid schemaVersion or deploymentMode")
    if len(histories) > 200_000:
        raise ValueError("Sales history exceeds the app safety limit")
    print(f"Valid sales history feed: {len(histories):,} lookup keys")


if __name__ == "__main__":
    main()
