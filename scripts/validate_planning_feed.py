#!/usr/bin/env python3
"""Validate the standalone, licensed planning-history.js publication contract."""

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path


MAX_FEED_BYTES = 50 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def assignment(text, name):
    matches = re.findall(rf"^window\.{re.escape(name)}\s*=\s*(.*);$", text, flags=re.M)
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one window.{name} assignment")
    value = json.loads(matches[0])
    if not isinstance(value, dict):
        raise ValueError(f"window.{name} must be an object")
    return value


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sha256_json(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def parse_timestamp(value, label):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def exact_nonnegative_integer(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def base_feed_identity(base_rows):
    property_ids = set()
    transaction_ids = set()
    pairs = []
    for item in base_rows:
        property_id = str(item.get("propertyRecordId") or "")
        transaction_id = str(item.get("id") or "")
        if not property_id.startswith("property:") or not transaction_id:
            raise ValueError("Base feed contains a row without canonical property and transaction ids")
        if transaction_id in transaction_ids:
            raise ValueError(f"Base feed contains duplicate transaction id {transaction_id}")
        property_ids.add(property_id)
        transaction_ids.add(transaction_id)
        pairs.append([transaction_id, property_id])
    return property_ids, transaction_ids, sha256_json(sorted(pairs))


def validate(
    path,
    *,
    base_feed=None,
    minimum_properties_with_history=0,
    minimum_property_coverage_percent=0,
    minimum_applications=0,
    maximum_age_days=45,
    allow_blocked=False,
):
    path = Path(path)
    if path.stat().st_size > MAX_FEED_BYTES:
        raise ValueError("Planning history exceeds the native 50 MiB safety limit")
    text = path.read_text(encoding="utf-8")
    histories = assignment(text, "SURREY_PLANNING_HISTORY")
    metadata = assignment(text, "SURREY_PLANNING_HISTORY_META")

    if metadata.get("schemaVersion") != 1 or metadata.get("deploymentMode") != "commercial":
        raise ValueError("Planning metadata is missing schemaVersion 1 or deploymentMode commercial")
    if metadata.get("publicationStatus") == "blocked-missing-licensed-source":
        if not allow_blocked:
            raise ValueError("Commercial planning publication is not complete")
        if histories:
            raise ValueError("Blocked planning publication must not contain history records")
        expected_strings = {
            "source": "Commercial planning feed not enabled",
            "sourceLicenceUrl": "",
            "redistributionRights": "not-authorised-for-publication",
            "updatedAt": "",
            "coverageMode": "unavailable",
            "coverageStatus": "unavailable",
        }
        for field, expected in expected_strings.items():
            if metadata.get(field) != expected:
                raise ValueError(
                    f"Blocked planning publication has invalid {field}"
                )
        for field in (
            "propertiesRequested",
            "propertiesChecked",
            "propertiesWithHistory",
            "propertiesCheckedNone",
            "propertiesUnavailable",
            "applicationsFound",
            "lookupKeys",
            "canonicalPropertyRecords",
            "transactionAliases",
        ):
            if metadata.get(field) != 0:
                raise ValueError(
                    f"Blocked planning publication must report zero {field}"
                )
        if metadata.get("authorities") != [] or metadata.get("authorityCoverage") != []:
            raise ValueError(
                "Blocked planning publication must not claim authority coverage"
            )
        return {
            "publicationStatus": "blocked-missing-licensed-source",
            "lookupKeys": 0,
            "canonicalPropertyRecords": 0,
            "transactionAliases": 0,
            "propertiesWithHistory": 0,
            "applicationsFound": 0,
            "updatedAt": "",
        }
    if metadata.get("publicationStatus") != "complete":
        raise ValueError("Commercial planning publication is not complete")
    if metadata.get("coverageMode") != "full-available-history" or metadata.get("coverageStatus") != "complete":
        raise ValueError("Commercial planning publication must declare complete full-history coverage")
    if metadata.get("redistributionRights") != "licensed-for-product-redistribution":
        raise ValueError("Commercial planning publication lacks explicit redistribution rights")
    if not isinstance(metadata.get("source"), str) or not metadata["source"].strip():
        raise ValueError("Commercial planning publication source is missing")
    licence_url = metadata.get("sourceLicenceUrl")
    if not isinstance(licence_url, str) or not licence_url.startswith("https://"):
        raise ValueError("Commercial planning publication requires an HTTPS source licence URL")

    updated_at = parse_timestamp(metadata.get("updatedAt"), "Planning updatedAt")
    now = datetime.now(timezone.utc)
    age_seconds = (now - updated_at).total_seconds()
    if age_seconds < -300:
        raise ValueError("Planning updatedAt is implausibly in the future")
    if maximum_age_days > 0 and age_seconds > maximum_age_days * 86400:
        raise ValueError(
            f"Planning publication is stale: updatedAt exceeds {maximum_age_days} days"
        )

    canonical = {key: value for key, value in histories.items() if key.startswith("property:")}
    aliases = {key: value for key, value in histories.items() if not key.startswith("property:")}
    status_counts = {"complete": 0, "checked_none": 0}
    applications_found = 0
    for key, record in canonical.items():
        if not isinstance(record, dict) or record.get("propertyRecordId") != key:
            raise ValueError("Planning history canonical record is missing its propertyRecordId")
        status = record.get("coverageStatus")
        if status not in status_counts:
            raise ValueError("Commercial planning history contains an incomplete/unavailable property")
        if record.get("coverageMode") != "full-available-history":
            raise ValueError("Planning history record is missing full-available-history coverage")
        applications = record.get("applications")
        if not isinstance(applications, list):
            raise ValueError("Planning history applications must be an array")
        total = exact_nonnegative_integer(
            record.get("totalApplications"),
            f"{key} totalApplications",
        )
        if total != len(applications):
            raise ValueError("Planning history totalApplications disagrees with its applications array")
        if status == "complete" and total == 0:
            raise ValueError("Complete planning history record contains no applications")
        if status == "checked_none" and total != 0:
            raise ValueError("Checked-none planning history record contains applications")
        if status == "complete" and not isinstance(record.get("latestApplication"), dict):
            raise ValueError("Complete planning history record has no latestApplication")
        if status == "checked_none" and record.get("latestApplication") not in (None, {}):
            raise ValueError("Checked-none planning history record has a latestApplication")
        status_counts[status] += 1
        applications_found += total

    for transaction_id, record in aliases.items():
        if not isinstance(record, dict):
            raise ValueError("Planning transaction alias is not an object")
        property_id = record.get("propertyRecordId")
        if property_id not in canonical or record != canonical[property_id]:
            raise ValueError(
                f"Planning transaction alias {transaction_id} does not equal its canonical record"
            )

    properties_checked = exact_nonnegative_integer(
        metadata.get("propertiesChecked"),
        "propertiesChecked",
    )
    properties_requested = exact_nonnegative_integer(
        metadata.get("propertiesRequested"),
        "propertiesRequested",
    )
    properties_with_history = exact_nonnegative_integer(
        metadata.get("propertiesWithHistory"),
        "propertiesWithHistory",
    )
    properties_checked_none = exact_nonnegative_integer(
        metadata.get("propertiesCheckedNone"),
        "propertiesCheckedNone",
    )
    properties_unavailable = exact_nonnegative_integer(
        metadata.get("propertiesUnavailable"),
        "propertiesUnavailable",
    )
    declared_applications = exact_nonnegative_integer(
        metadata.get("applicationsFound"),
        "applicationsFound",
    )
    if (
        properties_requested != len(canonical)
        or properties_checked != len(canonical)
        or properties_unavailable != 0
        or properties_with_history != status_counts["complete"]
        or properties_checked_none != status_counts["checked_none"]
        or properties_with_history + properties_checked_none != properties_checked
    ):
        raise ValueError("Planning property coverage metadata does not reconcile")
    if declared_applications != applications_found:
        raise ValueError("Planning applicationsFound does not reconcile with canonical records")
    if properties_with_history < minimum_properties_with_history:
        raise ValueError(
            "Planning publication regressed below the reviewed property-history floor: "
            f"{properties_with_history:,} < {minimum_properties_with_history:,}"
        )
    if not 0 <= minimum_property_coverage_percent <= 100:
        raise ValueError(
            "Planning minimum property coverage percent must be between 0 and 100"
        )
    if (
        canonical
        and properties_with_history * 100
        < len(canonical) * minimum_property_coverage_percent
    ):
        actual_percent = properties_with_history * 100 / len(canonical)
        raise ValueError(
            "Planning publication regressed below the reviewed property-denominator "
            f"coverage floor: {actual_percent:.1f}% < {minimum_property_coverage_percent:g}%"
        )
    if declared_applications < minimum_applications:
        raise ValueError(
            "Planning publication regressed below the reviewed application floor: "
            f"{declared_applications:,} < {minimum_applications:,}"
        )

    count_contract = {
        "lookupKeys": len(histories),
        "canonicalPropertyRecords": len(canonical),
        "transactionAliases": len(aliases),
    }
    for field, expected in count_contract.items():
        if exact_nonnegative_integer(metadata.get(field), field) != expected:
            raise ValueError(f"Planning {field} metadata does not reconcile")

    authority_coverage = metadata.get("authorityCoverage")
    authorities = metadata.get("authorities")
    if not isinstance(authority_coverage, list) or not isinstance(authorities, list):
        raise ValueError("Planning authority coverage metadata is missing")
    if authorities != [row.get("authority") for row in authority_coverage]:
        raise ValueError("Planning authority list does not match authorityCoverage")
    authority_totals = {
        field: sum(
            exact_nonnegative_integer(row.get(field), f"authorityCoverage {field}")
            for row in authority_coverage
            if isinstance(row, dict)
        )
        for field in (
            "propertiesChecked",
            "propertiesWithHistory",
            "propertiesCheckedNone",
            "applicationsFound",
        )
    }
    expected_authority_totals = {
        "propertiesChecked": len(canonical),
        "propertiesWithHistory": status_counts["complete"],
        "propertiesCheckedNone": status_counts["checked_none"],
        "applicationsFound": applications_found,
    }
    if authority_totals != expected_authority_totals:
        raise ValueError("Planning authority coverage totals do not reconcile")

    for field in ("baseFeedFingerprint", "sourceFingerprint", "historyFingerprint"):
        if not SHA256_RE.fullmatch(str(metadata.get(field) or "")):
            raise ValueError(f"Planning {field} is not a SHA-256 digest")
    if metadata["historyFingerprint"] != sha256_json(histories):
        raise ValueError("Planning historyFingerprint does not match the published records")

    if base_feed:
        base_text = Path(base_feed).read_text(encoding="utf-8")
        matches = re.findall(
            r"^window\.SURREY_LAND_REG_TRANSACTIONS\s*=\s*(.*);$",
            base_text,
            flags=re.M,
        )
        if len(matches) != 1:
            raise ValueError("Expected exactly one base transaction assignment")
        base_rows = json.loads(matches[0])
        expected_properties, expected_transactions, expected_fingerprint = base_feed_identity(base_rows)
        if set(canonical) != expected_properties or set(aliases) != expected_transactions:
            raise ValueError(
                "Planning history canonical/transaction coverage is stale or outside the base feed"
            )
        if metadata["baseFeedFingerprint"] != expected_fingerprint:
            raise ValueError("Planning baseFeedFingerprint does not match the base feed")

    return {
        "lookupKeys": len(histories),
        "canonicalPropertyRecords": len(canonical),
        "transactionAliases": len(aliases),
        "propertiesWithHistory": properties_with_history,
        "applicationsFound": applications_found,
        "updatedAt": metadata["updatedAt"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default="outputs/planning-history.js")
    parser.add_argument("--base-feed", default="")
    parser.add_argument("--minimum-properties-with-history", type=int, default=0)
    parser.add_argument("--minimum-property-coverage-percent", type=float, default=0)
    parser.add_argument("--minimum-applications", type=int, default=0)
    parser.add_argument("--maximum-age-days", type=int, default=45)
    parser.add_argument(
        "--allow-blocked",
        action="store_true",
        help=(
            "Accept only the explicit zero-record missing-licensed-source "
            "publication; never relax validation of a populated feed."
        ),
    )
    args = parser.parse_args()
    result = validate(
        args.path,
        base_feed=args.base_feed or None,
        minimum_properties_with_history=args.minimum_properties_with_history,
        minimum_property_coverage_percent=args.minimum_property_coverage_percent,
        minimum_applications=args.minimum_applications,
        maximum_age_days=args.maximum_age_days,
        allow_blocked=args.allow_blocked,
    )
    if result.get("publicationStatus") == "blocked-missing-licensed-source":
        print(
            "Valid blocked planning publication: licensed redistribution "
            "source is not configured."
        )
        return
    print(
        "Valid commercial planning feed: "
        f"{result['lookupKeys']:,} lookup keys, "
        f"{result['canonicalPropertyRecords']:,} properties, "
        f"{result['applicationsFound']:,} applications"
    )


if __name__ == "__main__":
    main()
