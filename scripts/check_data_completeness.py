#!/usr/bin/env python3
"""Fail safely when the shared INSIGHT feed loses expected data coverage."""

import argparse
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from insight_data_utils import (
    DEFAULT_INPUT_JS,
    FEED_SCHEMA_VERSION,
    PROPERTY_RECORD_SCHEMA_VERSION,
    clean,
    property_record_id,
    publication_contract_failures,
    read_js,
)
from private_estates import classify_estate, load_compiled_registry


EXPECTED_PRICE_FLOOR = 2_000_000
MINIMUM_COVERAGE = {
    "Postcodes": 99.0,
    "Coordinates": 99.0,
    "EPC matches": 75.0,
    "Fresh flood status": 90.0,
    "UPRN matches": 3.0,
    "School lookups": 80.0,
    "Planning query responses": 95.0,
}

MAX_DYNAMIC_AGE_HOURS = 30
PLANNING_COVERAGE_STATUSES = {"observed", "unknown", "unavailable"}

ESTATE_CLASSIFICATION_FIELDS = (
    "estateId",
    "estate",
    "estateClassification",
    "estateType",
    "estateRuleId",
    "estateEvidenceStatus",
    "estateReviewStatus",
)
STRUCTURED_ADDRESS_FIELDS = ("paon", "saon", "street", "locality", "town", "district", "postcode")


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


def timestamp_is_fresh(value, max_age_hours=MAX_DYNAMIC_AGE_HOURS, now=None):
    try:
        observed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    checked_at = now or datetime.now(timezone.utc)
    age_hours = (checked_at - observed).total_seconds() / 3600
    return 0 <= age_hours <= max_age_hours


def flood_status_is_fresh(item, now=None):
    context = item.get("environmentAgency")
    if not isinstance(context, dict):
        return False
    return timestamp_is_fresh(
        context.get("observedAt") or context.get("updatedAt"),
        now=now,
    )


def planning_context_is_truthful(context):
    if not isinstance(context, dict):
        return False
    status = clean(context.get("coverageStatus")).lower()
    applications = context.get("recentApplications")
    applications = applications if isinstance(applications, list) else []
    latest = clean(context.get("latestApplication"))
    if status == "observed":
        return (
            context.get("coverageMode") == "positive-results-only"
            and bool(applications)
            and bool(latest)
            and not latest.lower().startswith("no recent")
        )
    if status in PLANNING_COVERAGE_STATUSES - {"observed"}:
        return (
            context.get("coverageMode") == "no-authoritative-negative-coverage"
            and not applications
            and not latest
            and "recentApplicationCount" not in context
        )
    return False


def planning_response_is_current(item, now=None):
    context = item.get("planning")
    status = clean(context.get("coverageStatus")).lower() if isinstance(context, dict) else ""
    return status in {"observed", "unknown"} and planning_context_is_truthful(context) and timestamp_is_fresh(
        context.get("updatedAt"),
        now=now,
    )


def coverage_rows(items, now=None):
    total = len(items)
    checks = {
        "Postcodes": lambda x: present(x.get("postcode")),
        "Coordinates": lambda x: isinstance(x.get("latitude"), (int, float)) and isinstance(x.get("longitude"), (int, float)),
        "EPC matches": lambda x: x.get("epcMatched") is True,
        "Fresh flood status": lambda x: flood_status_is_fresh(x, now=now),
        "UPRN matches": lambda x: present(x.get("uprn")) or present(nested(x, "ordnanceSurvey", "uprn")),
        "School lookups": lambda x: present(x.get("ofsted")),
        "Planning query responses": lambda x: planning_response_is_current(x, now=now),
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


def dynamic_context_failures(items, meta, now=None):
    failures = []
    flood_meta = nested(meta, "propertyContext", "environmentAgency")
    flood_meta = flood_meta if isinstance(flood_meta, dict) else {}
    flood_counts = Counter()
    for item in items:
        context = item.get("environmentAgency")
        if not isinstance(context, dict):
            flood_counts["missing"] += 1
        elif flood_status_is_fresh(item, now=now):
            flood_counts["fresh"] += 1
        else:
            flood_counts["stale"] += 1
    expected_flood_meta = {
        "freshRecords": flood_counts["fresh"],
        "staleRecords": flood_counts["stale"],
        "missingRecords": flood_counts["missing"],
    }
    for field, expected in expected_flood_meta.items():
        if flood_meta.get(field) != expected:
            failures.append(
                f"Flood freshness metadata: {field} reports {flood_meta.get(field, 'missing')}, expected {expected}"
            )
    if flood_meta.get("maximumAgeHours") != MAX_DYNAMIC_AGE_HOURS:
        failures.append(
            f"Flood freshness metadata: maximumAgeHours must be {MAX_DYNAMIC_AGE_HOURS}"
        )

    planning_rows = [item.get("planning") for item in items if item.get("planning") is not None]
    invalid_planning = [context for context in planning_rows if not planning_context_is_truthful(context)]
    if invalid_planning:
        failures.append(
            f"Planning truthfulness: {len(invalid_planning):,} rows have unproved or contradictory coverage claims"
        )

    planning_meta = nested(meta, "dailyIntelligence", "planning")
    planning_meta = planning_meta if isinstance(planning_meta, dict) else {}
    truthful_planning = [context for context in planning_rows if planning_context_is_truthful(context)]
    observed = sum(1 for context in truthful_planning if context.get("coverageStatus") == "observed")
    unknown = sum(1 for context in truthful_planning if context.get("coverageStatus") == "unknown")
    unavailable = sum(1 for context in truthful_planning if context.get("coverageStatus") == "unavailable")
    expected_planning_meta = {
        "records": len(truthful_planning),
        "observedRecords": observed,
        "unknownRecords": unknown,
        "unavailableRecords": unavailable,
        "successfulResponses": observed + unknown,
    }
    for field, expected in expected_planning_meta.items():
        if planning_meta.get(field) != expected:
            failures.append(
                f"Planning coverage metadata: {field} reports {planning_meta.get(field, 'missing')}, expected {expected}"
            )
    if planning_meta.get("coverageMode") != "positive-observations-only":
        failures.append("Planning coverage metadata: expected positive-observations-only mode")
    return failures


def estate_failures(items, meta):
    """Replay the exact classifier over every transaction and verify feed metadata."""

    failures = []
    registry = load_compiled_registry()
    registry_version = clean(registry.get("registryVersion"))
    metadata_version = clean(meta.get("estateRegistryVersion"))
    if metadata_version != registry_version:
        failures.append(
            f"Estate registry version: feed reports {metadata_version or 'missing'}, expected {registry_version}"
        )

    missing_structured = [
        index for index, item in enumerate(items, start=1)
        if any(field not in item for field in STRUCTURED_ADDRESS_FIELDS)
    ]
    if missing_structured:
        failures.append(
            f"Structured estate addresses: {len(missing_structured):,} rows lack required fields "
            f"(first row {missing_structured[0]:,})"
        )

    stale_versions = [
        item for item in items if clean(item.get("estateRegistryVersion")) != registry_version
    ]
    if stale_versions:
        failures.append(
            f"Estate row versions: {len(stale_versions):,} rows do not carry {registry_version}"
        )

    mismatches = []
    for item in items:
        expected = classify_estate(item, compiled=registry)
        for field in ESTATE_CLASSIFICATION_FIELDS:
            actual_value = clean(item.get(field))
            expected_value = clean(expected.get(field))
            if actual_value != expected_value:
                mismatches.append((clean(item.get("id")) or clean(item.get("address")), field))
                break
    if mismatches:
        first_id, first_field = mismatches[0]
        failures.append(
            f"Estate classifier replay: {len(mismatches):,} rows differ from the exact road matrix "
            f"(first {first_id or 'unknown row'} at {first_field})"
        )

    estate_ids = Counter(clean(item.get("estateId")) for item in items if clean(item.get("estateId")))
    estate_names = Counter(clean(item.get("estate")) for item in items if clean(item.get("estate")))
    if meta.get("estateIdSummary") != dict(estate_ids):
        failures.append("Estate ID summary: metadata does not match classified transaction rows")
    if meta.get("estateSummary") != dict(estate_names):
        failures.append("Estate name summary: metadata does not match classified transaction rows")

    structured_meta = meta.get("estateStructuredFieldCoverage")
    structured_meta = structured_meta if isinstance(structured_meta, dict) else {}
    if structured_meta.get("rows") != len(items):
        failures.append("Estate structured coverage: metadata row count does not match the feed")
    if structured_meta.get("rowsEvaluatedAgainstRegistry") != len(items):
        failures.append("Estate classifier coverage: not every row is recorded as evaluated")
    if meta.get("estateClassifierMode") != "structured-exact-fail-closed":
        failures.append("Estate classifier mode: expected structured-exact-fail-closed")
    if meta.get("estateClassificationMode") != "audited-road-matrix":
        failures.append("Estate classification mode: expected audited-road-matrix")
    if meta.get("estateActiveDefinitionCount") != registry.get("metadata", {}).get("activeDefinitionCount"):
        failures.append("Estate definition count: feed metadata differs from the active registry")
    if meta.get("estateActiveRuleCount") != registry.get("metadata", {}).get("activeRuleCount"):
        failures.append("Estate rule count: feed metadata differs from the active registry")

    pre_2010 = [item for item in items if clean(item.get("date")) < "2010-01-01"]
    from_2010 = [item for item in items if clean(item.get("date")) >= "2010-01-01"]
    if not pre_2010 or not from_2010:
        failures.append("Historic partition: expected both 1995-2009 and 2010+ transaction rows")
    else:
        if not any(clean(item.get("estateId")) for item in pre_2010):
            failures.append("Historic estate classifications: 1995-2009 contains no estate IDs")
        if not any(clean(item.get("estateId")) for item in from_2010):
            failures.append("Current estate classifications: 2010+ contains no estate IDs")

    identifiers = [clean(item.get("id")) for item in items]
    if any(not value for value in identifiers):
        failures.append("Transaction identifiers: one or more rows have no stable ID")
    elif len(identifiers) != len(set(identifiers)):
        failures.append("Transaction identifiers: duplicate stable IDs detected")
    return failures


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
    parser.add_argument("--minimum-records", type=int, default=4500)
    parser.add_argument("--strict-metadata", action="store_true")
    parser.add_argument(
        "--base-only",
        action="store_true",
        help="Validate the expanded HMLR/property/estate contract before dependent enrichment completes.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    items, _summary, meta = read_js(args.input)
    rows = coverage_rows(items)
    failures = []
    warnings = []

    if len(items) < args.minimum_records:
        failures.append(f"Record count: {len(items):,} is below {args.minimum_records:,}")
    if str(meta.get("from", "")) != "1995-01-01":
        failures.append(f"Historic coverage: expected 1995-01-01, found {meta.get('from', 'missing')}")
    if meta.get("schemaVersion") != FEED_SCHEMA_VERSION:
        failures.append(
            f"Schema version: expected {FEED_SCHEMA_VERSION}, found {meta.get('schemaVersion', 'missing')}"
        )
    if meta.get("priceFloor") != EXPECTED_PRICE_FLOOR:
        failures.append(
            f"Price floor: expected {EXPECTED_PRICE_FLOOR:,}, found {meta.get('priceFloor', 'missing')}"
        )
    below_floor = [
        item
        for item in items
        if not isinstance(item.get("price"), (int, float))
        or item["price"] < EXPECTED_PRICE_FLOOR
    ]
    if below_floor:
        failures.append(f"Price floor rows: {len(below_floor):,} records are invalid or below £2m")
    if items and not any(
        EXPECTED_PRICE_FLOOR <= item.get("price", 0) < 3_000_000
        for item in items
    ):
        failures.append("Price cohort: feed contains no £2m-£3m transactions")
    if meta.get("propertyRecordSchemaVersion") != PROPERTY_RECORD_SCHEMA_VERSION:
        failures.append("Property identity: schema version is missing or unsupported")
    canonical_property_ids = [property_record_id(item) for item in items]
    if any(item.get("propertyRecordId") != expected for item, expected in zip(items, canonical_property_ids)):
        failures.append("Property identity: one or more rows do not use the canonical full-address ID")
    if meta.get("canonicalPropertyRecords") != len(set(canonical_property_ids)):
        failures.append("Property identity: canonical property count is stale")
    if meta.get("propertyIdentityMode") != "full-normalised-address-plus-postcode-fail-closed":
        failures.append("Property identity: feed does not declare the fail-closed identity mode")

    transaction_dates = sorted(clean(item.get("date")) for item in items if clean(item.get("date")))
    if not transaction_dates or not transaction_dates[0].startswith("1995-"):
        failures.append(
            f"Historic rows: earliest transaction is {transaction_dates[0] if transaction_dates else 'missing'}, expected 1995"
        )
    for field in ("residentialRows", "mappedTransactions"):
        if meta.get(field) != len(items):
            failures.append(f"{field}: metadata reports {meta.get(field, 'missing')}, feed contains {len(items):,}")
    historical_meta = meta.get("historicalExpansion")
    historical_meta = historical_meta if isinstance(historical_meta, dict) else {}
    actual_pre_2010 = sum(1 for item in items if clean(item.get("date")) < "2010-01-01")
    if historical_meta.get("pre2010Transactions") != actual_pre_2010:
        failures.append("Historical expansion: pre-2010 metadata does not match transaction rows")

    failures.extend(estate_failures(items, meta))
    if not args.base_only:
        failures.extend(dynamic_context_failures(items, meta))
    failures.extend(publication_contract_failures(items))

    for row in rows:
        if not args.base_only and row["coverage"] < row["minimum"]:
            failures.append(
                f"{row['name']}: {row['coverage']:.1f}% is below {row['minimum']:.1f}%"
            )

    epc_meta = meta.get("epcEnrichment") if isinstance(meta.get("epcEnrichment"), dict) else {}
    epc_actual = next(row["coverage"] for row in rows if row["name"] == "EPC matches")
    if args.strict_metadata and clean(epc_meta.get("status")).lower() != "complete":
        failures.append("EPC metadata: enrichment has not completed across the full transaction universe")
    if args.strict_metadata:
        epc_requested = epc_meta.get("requested")
        epc_resolved = epc_meta.get("resolved")
        epc_pending = epc_meta.get("pending")
        epc_errors = epc_meta.get("errors")
        if epc_requested != len(items):
            failures.append(
                f"EPC metadata: requested count is {epc_requested!r}, expected {len(items):,}"
            )
        if not all(isinstance(value, int) for value in (epc_resolved, epc_pending, epc_errors)):
            failures.append("EPC metadata: terminal accounting is missing")
        else:
            if epc_resolved + epc_pending != len(items):
                failures.append("EPC metadata: resolved and pending counts do not reconcile")
            if epc_pending != 0 or epc_errors != 0:
                failures.append(
                    f"EPC metadata: {epc_pending} lookups remain pending with {epc_errors} errors"
                )
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
