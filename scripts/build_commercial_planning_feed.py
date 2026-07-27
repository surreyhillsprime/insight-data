#!/usr/bin/env python3
"""Build the GitHub planning-history feed from an explicitly licensed source."""

import argparse
import hashlib
import json
import os
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from enrich_planning_history import enrich, read_source
from insight_data_utils import DEFAULT_INPUT_JS, property_record_id, read_js, utc_now


def property_cache_key(item):
    return property_record_id(item)


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sha256_json(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def base_feed_fingerprint(transactions):
    identities = sorted(
        [str(item.get("id") or ""), str(item.get("propertyRecordId") or "")]
        for item in transactions
    )
    return sha256_json(identities)


def canonical_history(item, history, key, checked_at):
    """Return one explicit full-history coverage record for a property."""

    record = dict(history) if isinstance(history, dict) else {}
    applications = record.get("applications")
    if not isinstance(applications, list):
        applications = []
    try:
        declared_total = int(record.get("totalApplications") or 0)
    except (TypeError, ValueError):
        declared_total = 0
    has_history = bool(
        applications
        or declared_total > 0
        or record.get("latestApplication")
        or record.get("coverageStatus") == "complete"
    )
    record.update({
        "propertyRecordId": key,
        "source": record.get("source") or "Licensed local-authority planning data",
        "updatedAt": record.get("updatedAt") or checked_at,
        "authority": record.get("authority") or item.get("district", ""),
        "totalApplications": len(applications) if has_history else 0,
        "latestApplication": record.get("latestApplication") if has_history else None,
        "applications": applications if has_history else [],
        "matchMethod": record.get("matchMethod") or "postcode-and-address",
        "matchConfidence": record.get("matchConfidence") or 0,
        "coverageMode": "full-available-history",
        "coverageStatus": "complete" if has_history else "checked_none",
    })
    return record


def build_histories(transactions, enriched, checked_at=None):
    """Build canonical property records plus every transaction lookup alias."""

    if len(transactions) != len(enriched):
        raise ValueError("Planning enrichment did not return one row per transaction")

    checked_at = checked_at or utc_now()
    canonical = {}
    transaction_properties = {}
    for item, enriched_item in zip(transactions, enriched):
        key = property_cache_key(item)
        if not key.startswith("property:"):
            raise ValueError(
                f"Transaction {item.get('id') or '<missing>'} has no canonical propertyRecordId"
            )
        candidate = canonical_history(
            item,
            enriched_item.get("planningHistory"),
            key,
            checked_at,
        )
        existing = canonical.get(key)
        if existing is None or (
            existing["coverageStatus"] == "checked_none"
            and candidate["coverageStatus"] == "complete"
        ):
            canonical[key] = candidate
        transaction_id = str(item.get("id") or "")
        if not transaction_id:
            raise ValueError("Every transaction requires an id for planning-feed parity")
        if transaction_id in transaction_properties:
            raise ValueError(f"Duplicate transaction id in base feed: {transaction_id}")
        transaction_properties[transaction_id] = key

    histories = dict(canonical)
    for transaction_id, key in transaction_properties.items():
        histories[transaction_id] = canonical[key]

    authority_counts = defaultdict(Counter)
    for record in canonical.values():
        authority = record.get("authority") or "Unknown"
        authority_counts[authority]["propertiesChecked"] += 1
        if record["coverageStatus"] == "complete":
            authority_counts[authority]["propertiesWithHistory"] += 1
            authority_counts[authority]["applicationsFound"] += len(record["applications"])
        else:
            authority_counts[authority]["propertiesCheckedNone"] += 1

    coverage = [
        {
            "authority": authority,
            "propertiesChecked": counts["propertiesChecked"],
            "propertiesWithHistory": counts["propertiesWithHistory"],
            "propertiesCheckedNone": counts["propertiesCheckedNone"],
            "applicationsFound": counts["applicationsFound"],
            "status": "licensed-source",
            "coverageMode": "full-available-history",
        }
        for authority, counts in sorted(authority_counts.items())
    ]
    return histories, canonical, coverage


def write_planning_js(
    path,
    histories,
    stats,
    coverage,
    *,
    source_name="INSIGHT licensed commercial planning feed",
    source_licence_url="https://example.invalid/licence",
    source_fingerprint=None,
    base_fingerprint=None,
    updated_at=None,
):
    path = Path(path)
    updated_at = updated_at or utc_now()
    canonical_count = sum(key.startswith("property:") for key in histories)
    transaction_aliases = len(histories) - canonical_count
    metadata = {
        "schemaVersion": 1,
        "deploymentMode": "commercial",
        "publicationStatus": "complete",
        "source": source_name,
        "sourceLicenceUrl": source_licence_url,
        "redistributionRights": "licensed-for-product-redistribution",
        "updatedAt": updated_at,
        "propertiesRequested": stats["propertiesChecked"],
        "propertiesChecked": stats["propertiesChecked"],
        "propertiesWithHistory": stats["propertiesWithHistory"],
        "propertiesCheckedNone": stats["propertiesCheckedNone"],
        "propertiesUnavailable": 0,
        "applicationsFound": stats["applicationsFound"],
        "lookupKeys": len(histories),
        "canonicalPropertyRecords": canonical_count,
        "transactionAliases": transaction_aliases,
        "coverageMode": "full-available-history",
        "coverageStatus": "complete",
        "earliestApplicationYear": stats.get("earliestApplicationYear") or None,
        "latestApplicationYear": stats.get("latestApplicationYear") or None,
        "authorities": [item["authority"] for item in coverage],
        "authorityCoverage": coverage,
        "baseFeedFingerprint": base_fingerprint or ("0" * 64),
        "sourceFingerprint": source_fingerprint or ("0" * 64),
        "historyFingerprint": sha256_json(histories),
    }
    content = "\n".join([
        "window.SURREY_PLANNING_HISTORY = " + json.dumps(histories, separators=(",", ":")) + ";",
        "window.SURREY_PLANNING_HISTORY_META = " + json.dumps(metadata, separators=(",", ":")) + ";",
        "",
    ])
    if len(content.encode("utf-8")) > 50 * 1024 * 1024:
        raise ValueError("Planning feed exceeds the native 50 MiB safety limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        handle.write(content)
        temporary_path = Path(handle.name)
    os.replace(temporary_path, path)


def parse_args():
    parser = argparse.ArgumentParser(description="Build the licensed INSIGHT planning feed.")
    parser.add_argument("--source", required=True, help="Licensed CSV/JSON file or HTTPS URL.")
    parser.add_argument("--source-name", required=True, help="Human-readable licensed source name.")
    parser.add_argument(
        "--source-licence-url",
        required=True,
        help="HTTPS licence/contract URL explicitly permitting product redistribution.",
    )
    parser.add_argument("--input-js", default=str(DEFAULT_INPUT_JS), help="INSIGHT transactions feed.")
    parser.add_argument("--write-js", required=True, help="Output planning-history.js.")
    parser.add_argument("--minimum-address-score", type=float, default=0.72)
    return parser.parse_args()


def main():
    args = parse_args()
    if not args.source_licence_url.startswith("https://"):
        raise ValueError("The licensed planning source must provide an HTTPS licence URL")
    transactions, _summary, _meta = read_js(args.input_js)
    source_rows = read_source(args.source)
    checked_at = utc_now()
    enriched, source_stats = enrich(
        transactions,
        source_rows,
        args.minimum_address_score,
    )
    histories, canonical, coverage = build_histories(
        transactions,
        enriched,
        checked_at=checked_at,
    )
    complete_records = [
        record
        for record in canonical.values()
        if record["coverageStatus"] == "complete"
    ]
    stats = Counter(
        propertiesChecked=len(canonical),
        propertiesWithHistory=len(complete_records),
        propertiesCheckedNone=len(canonical) - len(complete_records),
        applicationsFound=sum(len(record["applications"]) for record in complete_records),
        earliestApplicationYear=source_stats["earliestApplicationYear"],
        latestApplicationYear=source_stats["latestApplicationYear"],
    )
    write_planning_js(
        args.write_js,
        histories,
        stats,
        coverage,
        source_name=args.source_name,
        source_licence_url=args.source_licence_url,
        source_fingerprint=sha256_json(source_rows),
        base_fingerprint=base_feed_fingerprint(transactions),
        updated_at=checked_at,
    )
    print(json.dumps(dict(stats), sort_keys=True))


if __name__ == "__main__":
    main()
