#!/usr/bin/env python3
"""Fail safely when the shared INSIGHT feed loses expected data coverage."""

import argparse
import os
from pathlib import Path

from insight_data_utils import DEFAULT_INPUT_JS, clean, read_js


MINIMUM_COVERAGE = {
    "Postcodes": 99.0,
    "Coordinates": 99.0,
    "EPC matches": 75.0,
    "Flood lookups": 90.0,
    "OSM amenities": 10.0,
    "UPRN matches": 3.0,
    "School lookups": 80.0,
    "Planning lookups": 95.0,
}


def present(value):
    return bool(clean(value)) if isinstance(value, str) else value is not None


def percent(found, total):
    return round(found / total * 100, 1) if total else 0.0


def nested(item, *keys):
    value = item
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def coverage_rows(items):
    total = len(items)
    checks = {
        "Postcodes": lambda x: present(x.get("postcode")),
        "Coordinates": lambda x: isinstance(x.get("latitude"), (int, float)) and isinstance(x.get("longitude"), (int, float)),
        "EPC matches": lambda x: x.get("epcMatched") is True,
        "Flood lookups": lambda x: present(x.get("environmentAgency")),
        "OSM amenities": lambda x: present(x.get("openStreetMap")),
        "UPRN matches": lambda x: present(x.get("uprn")) or present(nested(x, "ordnanceSurvey", "uprn")),
        "School lookups": lambda x: present(x.get("ofsted")),
        "Planning lookups": lambda x: present(x.get("planning")),
    }
    return [
        {
            "name": name,
            "found": sum(1 for item in items if predicate(item)),
            "total": total,
            "coverage": percent(sum(1 for item in items if predicate(item)), total),
            "minimum": MINIMUM_COVERAGE[name],
        }
        for name, predicate in checks.items()
    ]


def render(rows, failures, warnings, meta):
    lines = [
        "# INSIGHT data completeness",
        "",
        f"Feed coverage: `{meta.get('from', 'unknown')}` to `{meta.get('to', 'unknown')}`.",
        "",
        "| Check | Found | Total | Coverage | Minimum | Result |",
        "|---|---:|---:|---:|---:|---|",
    ]
    failed_names = {item.split(":", 1)[0] for item in failures}
    for row in rows:
        result = "FAIL" if row["name"] in failed_names else "PASS"
        lines.append(
            f"| {row['name']} | {row['found']:,} | {row['total']:,} | "
            f"{row['coverage']:.1f}% | {row['minimum']:.1f}% | {result} |"
        )
    if warnings:
        lines.extend(["", "## Warnings", "", *[f"- {item}" for item in warnings]])
    if failures:
        lines.extend(["", "## Failures", "", *[f"- {item}" for item in failures]])
    return "\n".join(lines) + "\n"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(DEFAULT_INPUT_JS))
    parser.add_argument("--minimum-records", type=int, default=1500)
    parser.add_argument("--strict-metadata", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    items, _summary, meta = read_js(args.input)
    rows = coverage_rows(items)
    failures = []
    warnings = []

    if len(items) < args.minimum_records:
        failures.append(f"Record count: {len(items):,} is below {args.minimum_records:,}")
    if str(meta.get("from", "9999")) > "1995-01-01":
        failures.append(f"Historic coverage: expected 1995-01-01, found {meta.get('from', 'missing')}")
    if meta.get("schemaVersion") != 2:
        failures.append(f"Schema version: expected 2, found {meta.get('schemaVersion', 'missing')}")

    for row in rows:
        if row["coverage"] < row["minimum"]:
            failures.append(
                f"{row['name']}: {row['coverage']:.1f}% is below {row['minimum']:.1f}%"
            )

    epc_meta = meta.get("epcEnrichment") if isinstance(meta.get("epcEnrichment"), dict) else {}
    epc_actual = next(row["coverage"] for row in rows if row["name"] == "EPC matches")
    epc_reported = epc_meta.get("coveragePercent")
    if isinstance(epc_reported, (int, float)) and abs(epc_reported - epc_actual) > 2.0:
        message = f"EPC metadata: reports {epc_reported:.1f}% but actual coverage is {epc_actual:.1f}%"
        (failures if args.strict_metadata else warnings).append(message)

    report = render(rows, failures, warnings, meta)
    print(report, end="")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as handle:
            handle.write(report)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
