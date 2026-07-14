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
    args = parser.parse_args()
    text = Path(args.path).read_text(encoding="utf-8")
    histories = assignment(text, "SURREY_PLANNING_HISTORY")
    metadata = assignment(text, "SURREY_PLANNING_HISTORY_META")
    if metadata.get("schemaVersion") != 1 or metadata.get("deploymentMode") != "commercial":
        raise ValueError("Commercial planning metadata is missing schemaVersion 1 or deploymentMode commercial")
    if len(histories) > 200_000:
        raise ValueError("Planning history exceeds the app safety limit")
    print(f"Valid commercial planning feed: {len(histories):,} lookup keys")


if __name__ == "__main__":
    main()
