#!/usr/bin/env python3
"""Build the GitHub planning-history feed from an explicitly licensed source."""

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from enrich_planning_history import enrich, read_source
from insight_data_utils import DEFAULT_INPUT_JS, clean, normalise_postcode, read_js, utc_now


def property_cache_key(item):
    address = re.sub(r"[^A-Z0-9]+", " ", clean(item.get("address")).upper()).strip()
    return f"property:{address}|{normalise_postcode(item.get('postcode'))}"


def write_planning_js(path, histories, stats, coverage):
    metadata = {
        "schemaVersion": 1,
        "deploymentMode": "commercial",
        "source": "INSIGHT licensed commercial planning feed",
        "updatedAt": utc_now(),
        "propertiesChecked": stats["propertiesChecked"],
        "propertiesWithHistory": stats["propertiesWithHistory"],
        "applicationsFound": stats["applicationsFound"],
        "coverageMode": "full-available-history",
        "earliestApplicationYear": stats.get("earliestApplicationYear") or None,
        "latestApplicationYear": stats.get("latestApplicationYear") or None,
        "authorities": [item["authority"] for item in coverage],
        "authorityCoverage": coverage,
    }
    content = "\n".join([
        "window.SURREY_PLANNING_HISTORY = " + json.dumps(histories, separators=(",", ":")) + ";",
        "window.SURREY_PLANNING_HISTORY_META = " + json.dumps(metadata, separators=(",", ":")) + ";",
        "",
    ])
    Path(path).write_text(content, encoding="utf-8")


def parse_args():
    parser = argparse.ArgumentParser(description="Build the licensed INSIGHT planning feed.")
    parser.add_argument("--source", required=True, help="Licensed CSV/JSON file or HTTPS URL.")
    parser.add_argument("--input-js", default=str(DEFAULT_INPUT_JS), help="INSIGHT transactions feed.")
    parser.add_argument("--write-js", required=True, help="Output planning-history.js.")
    parser.add_argument("--minimum-address-score", type=float, default=0.72)
    return parser.parse_args()


def main():
    args = parse_args()
    transactions, _summary, _meta = read_js(args.input_js)
    enriched, source_stats = enrich(transactions, read_source(args.source), args.minimum_address_score)
    histories = {}
    authorities = Counter()
    for item in enriched:
        history = item.get("planningHistory")
        if not history:
            continue
        histories[str(item["id"])] = history
        histories[property_cache_key(item)] = history
        authorities[history.get("authority") or item.get("district") or "Unknown"] += 1
    stats = Counter(
        propertiesChecked=source_stats["propertiesChecked"],
        propertiesWithHistory=source_stats["propertiesWithHistory"],
        applicationsFound=source_stats["applicationMatches"],
        earliestApplicationYear=source_stats["earliestApplicationYear"],
        latestApplicationYear=source_stats["latestApplicationYear"],
    )
    coverage = [
        {"authority": name, "propertiesWithHistory": count, "status": "licensed-source", "coverageMode": "full-available-history"}
        for name, count in sorted(authorities.items())
    ]
    write_planning_js(args.write_js, histories, stats, coverage)
    print(json.dumps(dict(stats), sort_keys=True))


if __name__ == "__main__":
    main()
