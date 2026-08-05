#!/usr/bin/env python3
"""Migrate an existing INSIGHT feed without discarding enrichment fields."""

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from insight_data_utils import (
    DEFAULT_INPUT_JS,
    FEED_SCHEMA_VERSION,
    PROPERTY_RECORD_SCHEMA_VERSION,
    canonicalise_property_addresses,
    clean,
    property_record_id,
    read_js,
    write_js,
)
from sweep_land_registry import (
    CURRENT_CSV,
    CURRENT_START_DATE,
    DEFAULT_CSV,
    HISTORICAL_CSV,
    stable_transaction_id,
    write_processed_csv,
)


HERITAGE_STATUS_PRIORITY = {
    "unknown": 0,
    "no_direct_match": 1,
    "candidate_review": 2,
    "confirmed_listed": 3,
}
HISTORIC_ENGLAND_SOURCE = "Historic England NHLE"
HISTORIC_ENGLAND_COORDINATE_PRECISION = "confirmed-nhle-designation-location"


def stable_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(value):
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def reconcile_heritage_projection(transactions, metadata):
    """Re-key a validated property-grain NHLE projection after identity repair.

    This does not perform new Historic England matching.  It carries the
    existing reviewed evidence onto the corrected canonical property, applies
    one identical projection to every transaction and recalculates the
    property-grain denominators and semantic fingerprints.
    """

    heritage_meta = metadata.get("heritageSync")
    if not isinstance(heritage_meta, dict):
        return transactions, metadata

    grouped = defaultdict(list)
    for row in transactions:
        grouped[property_record_id(row)].append(row)

    reconciled = []
    property_contexts = {}
    for record_id, rows in grouped.items():
        contexts = [
            row.get("historicEngland")
            for row in rows
            if isinstance(row.get("historicEngland"), dict)
        ]
        if len(contexts) != len(rows):
            raise ValueError(f"Heritage identity migration found incomplete evidence for {record_id}")
        chosen = max(
            contexts,
            key=lambda context: (
                HERITAGE_STATUS_PRIORITY.get(context.get("status"), -1),
                stable_json(context),
            ),
        )
        chosen = dict(chosen)
        if chosen.get("status") in {"confirmed_listed", "candidate_review"}:
            matching_contexts = [
                context
                for context in contexts
                if context.get("status") == chosen.get("status")
            ]
            entries = {}
            for context in matching_contexts:
                for entry in context.get("entries") or []:
                    number = str(entry.get("listEntryNumber") or "")
                    if number:
                        entries[number] = entry
            chosen["entries"] = [entries[number] for number in sorted(entries)]
        property_contexts[record_id] = chosen

        refined_rows = [
            row for row in rows
            if row.get("coordinateSource") == HISTORIC_ENGLAND_SOURCE
            and row.get("coordinatePrecision") == HISTORIC_ENGLAND_COORDINATE_PRECISION
        ]
        coordinate_donor = None
        if chosen.get("status") == "confirmed_listed" and refined_rows:
            coordinate_views = {
                stable_json({
                    "latitude": row.get("latitude"),
                    "longitude": row.get("longitude"),
                    "coordinateSource": row.get("coordinateSource"),
                    "coordinatePrecision": row.get("coordinatePrecision"),
                    "geocode": row.get("geocode"),
                })
                for row in refined_rows
            }
            if len(coordinate_views) != 1:
                raise ValueError(f"Heritage identity migration found conflicting NHLE coordinates for {record_id}")
            coordinate_donor = refined_rows[0]

        for row in rows:
            migrated = dict(row)
            migrated["historicEngland"] = chosen
            if coordinate_donor is not None:
                for field in ("latitude", "longitude", "coordinateSource", "coordinatePrecision"):
                    migrated[field] = coordinate_donor.get(field)
                migrated["geocode"] = dict(coordinate_donor.get("geocode") or {})
            reconciled.append(migrated)

    reconciled.sort(
        key=lambda row: (str(row.get("date") or ""), row.get("price") or 0, str(row.get("address") or "")),
        reverse=True,
    )
    states = Counter(context.get("status") for context in property_contexts.values())
    confirmed_relationships = []
    grades_by_entry = {}
    grade_counts = Counter()
    for record_id, context in property_contexts.items():
        if context.get("status") != "confirmed_listed":
            continue
        for entry in context.get("entries") or []:
            number = str(entry.get("listEntryNumber") or "")
            grade = str(entry.get("grade") or "")
            confirmed_relationships.append((record_id, number, grade))
            grade_counts[grade] += 1
            if number:
                prior_grade = grades_by_entry.setdefault(number, grade)
                if prior_grade != grade:
                    raise ValueError(f"Heritage identity migration found conflicting grades for NHLE {number}")

    representatives = {
        record_id: max(rows, key=lambda row: (str(row.get("date") or ""), str(row.get("id") or "")))
        for record_id, rows in grouped.items()
    }
    input_projection = []
    output_projection = {}
    for record_id, representative in sorted(representatives.items()):
        geocode = representative.get("geocode")
        geocode = geocode if isinstance(geocode, dict) else {}
        input_projection.append({
            "propertyRecordId": record_id,
            "address": representative.get("address"),
            "paon": representative.get("paon"),
            "saon": representative.get("saon"),
            "street": representative.get("street"),
            "locality": representative.get("locality"),
            "town": representative.get("town"),
            "postcode": representative.get("postcode"),
            "latitude": representative.get("latitude"),
            "longitude": representative.get("longitude"),
            "coordinateSource": representative.get("coordinateSource") or geocode.get("source"),
            "coordinatePrecision": representative.get("coordinatePrecision") or geocode.get("precision"),
        })
        context = property_contexts[record_id]
        output_projection[record_id] = {
            "historicEngland": {
                "status": context.get("status"),
                "entries": context.get("entries"),
            },
            "latitude": representative.get("latitude"),
            "longitude": representative.get("longitude"),
            "coordinateSource": representative.get("coordinateSource") or "",
            "coordinatePrecision": representative.get("coordinatePrecision") or "",
            "postcodeCentroidLatitude": geocode.get("postcodeCentroidLatitude"),
            "postcodeCentroidLongitude": geocode.get("postcodeCentroidLongitude"),
        }

    decisions = [
        {
            "propertyRecordId": record_id,
            "status": context.get("status"),
            "listEntryNumbers": sorted(
                str(entry.get("listEntryNumber") or "")
                for entry in context.get("entries") or []
                if str(entry.get("listEntryNumber") or "")
            ),
        }
        for record_id, context in sorted(property_contexts.items())
    ]
    refreshed_meta = dict(metadata)
    refreshed_heritage = dict(heritage_meta)
    refreshed_heritage.update({
        "propertiesAccountedFor": len(property_contexts),
        "confirmedListed": states["confirmed_listed"],
        "candidateReview": states["candidate_review"],
        "noDirectMatch": states["no_direct_match"],
        "unknown": states["unknown"],
        "overridesApplied": sum(
            any(entry.get("matchMethod") == "reviewed_override" for entry in context.get("entries") or [])
            for context in property_contexts.values()
        ),
        "confirmedEntries": len(confirmed_relationships),
        "confirmedUniqueListEntries": len(grades_by_entry),
        "confirmedEntryGradeCounts": {
            grade: grade_counts[grade] for grade in ("I", "II*", "II")
        },
        "confirmedLocationsApplied": sum(
            representative.get("coordinateSource") == HISTORIC_ENGLAND_SOURCE
            and representative.get("coordinatePrecision") == HISTORIC_ENGLAND_COORDINATE_PRECISION
            for representative in representatives.values()
        ),
        "inputFingerprint": fingerprint(input_projection),
        "overrideFingerprint": fingerprint(decisions),
        "outputFingerprint": fingerprint(output_projection),
    })
    refreshed_meta["heritageSync"] = refreshed_heritage
    return reconciled, refreshed_meta


def bind_reviewed_heritage_ledger(transactions, metadata, overrides_path):
    """Bind a migrated projection to the exact reviewed ledger without rematching."""

    from enrich_listed_buildings import (
        build_properties,
        input_fingerprint,
        load_overrides,
        publication_fingerprint,
    )

    properties = build_properties(transactions)
    property_ids = set(properties)
    overrides, override_fingerprint = load_overrides(
        overrides_path,
        property_ids=property_ids,
    )
    if set(overrides) != property_ids:
        raise ValueError(
            "Reviewed heritage ledger does not cover the migrated property universe"
        )

    contexts = {}
    for item in transactions:
        record_id = property_record_id(item)
        context = item.get("historicEngland")
        if not isinstance(context, dict):
            raise ValueError(
                f"Migrated heritage projection is missing for {record_id}"
            )
        prior = contexts.setdefault(record_id, context)
        if stable_json(prior) != stable_json(context):
            raise ValueError(
                f"Migrated heritage projection is inconsistent for {record_id}"
            )

    status_counts = Counter()
    for record_id, reviewed in overrides.items():
        context = contexts[record_id]
        reviewed_status = clean(reviewed.get("status"))
        if context.get("status") != reviewed_status:
            raise ValueError(
                f"Migrated heritage projection disagrees with reviewed status for {record_id}"
            )
        reviewed_entries = sorted(reviewed.get("listEntryNumbers") or [])
        projected_entries = sorted(
            clean(entry.get("listEntryNumber"))
            for entry in context.get("entries") or []
            if clean(entry.get("listEntryNumber"))
        )
        if projected_entries != reviewed_entries:
            raise ValueError(
                f"Migrated heritage projection disagrees with reviewed entries for {record_id}"
            )
        status_counts[reviewed_status] += 1

    heritage_meta = metadata.get("heritageSync")
    if not isinstance(heritage_meta, dict):
        raise ValueError("Migrated heritage metadata is missing")
    expected_counts = {
        "confirmedListed": status_counts["confirmed_listed"],
        "candidateReview": status_counts["candidate_review"],
        "noDirectMatch": status_counts["no_direct_match"],
        "unknown": status_counts["unknown"],
    }
    for field, expected in expected_counts.items():
        if heritage_meta.get(field) != expected:
            raise ValueError(
                f"Migrated heritage metadata {field} does not match the reviewed ledger"
            )
    output = dict(metadata)
    output_heritage = dict(heritage_meta)
    output_heritage["inputFingerprint"] = input_fingerprint(properties)
    output_heritage["overrideFingerprint"] = override_fingerprint
    output_heritage["outputFingerprint"] = publication_fingerprint(transactions)
    output["heritageSync"] = output_heritage
    return output


def migrate_transaction(item):
    migrated = dict(item)
    prior_id = str(item.get("id") or "")
    migrated["id"] = (
        prior_id
        if re.fullmatch(r"lr-[0-9a-f]{20}", prior_id)
        else stable_transaction_id(
            item.get("address"),
            item.get("postcode"),
            item.get("price"),
            item.get("date"),
            item.get("propertyType"),
            item.get("category"),
        )
    )
    migrated["propertyRecordId"] = property_record_id(migrated)
    geocode = item.get("geocode") if isinstance(item.get("geocode"), dict) else {}
    precision = str(geocode.get("precision") or "").lower()
    confirmed_nhle_location = (
        item.get("coordinateSource") == "Historic England NHLE"
        and item.get("coordinatePrecision")
        == "confirmed-nhle-designation-location"
    )
    if (
        ("postcode" in precision or "centroid" in precision)
        and not confirmed_nhle_location
    ):
        migrated["coordinateSource"] = geocode.get("source") or "Postcodes.io"
        migrated["coordinatePrecision"] = "postcode-centroid"
    return migrated


def main():
    parser = argparse.ArgumentParser(description="Migrate an INSIGHT feed to the current schema.")
    parser.add_argument("--input-js", default=str(DEFAULT_INPUT_JS))
    parser.add_argument("--write-js", default=str(DEFAULT_INPUT_JS))
    parser.add_argument("--write-csv", default="")
    parser.add_argument(
        "--heritage-overrides",
        default="",
        help=(
            "Bind migrated Historic England projections to this exact reviewed "
            "ledger without performing new source matching."
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    transactions, _summary, meta = read_js(args.input_js)
    canonical_transactions, address_stats = canonicalise_property_addresses(transactions)
    migrated = [migrate_transaction(item) for item in canonical_transactions]
    meta = dict(meta)
    meta["schemaVersion"] = FEED_SCHEMA_VERSION
    meta["propertyRecordSchemaVersion"] = PROPERTY_RECORD_SCHEMA_VERSION
    meta["canonicalPropertyRecords"] = len({item["propertyRecordId"] for item in migrated})
    meta["propertyIdentityMode"] = "full-normalised-address-plus-postcode-fail-closed"
    migrated, meta = reconcile_heritage_projection(migrated, meta)
    if args.heritage_overrides:
        meta = bind_reviewed_heritage_ledger(
            migrated,
            meta,
            args.heritage_overrides,
        )
    ids = [item["id"] for item in migrated]
    if len(ids) != len(set(ids)):
        raise RuntimeError("Stable transaction ID collision detected")
    print(f"Migrated {len(migrated)} transactions to schema {FEED_SCHEMA_VERSION}.")
    if not args.dry_run:
        write_js(Path(args.write_js), migrated, meta, address_stats=address_stats)
        if args.write_csv:
            csv_path = Path(args.write_csv)
            write_processed_csv(csv_path, migrated)
            if csv_path.resolve() == DEFAULT_CSV.resolve():
                write_processed_csv(
                    HISTORICAL_CSV,
                    [item for item in migrated if item.get("date", "") < CURRENT_START_DATE],
                )
                write_processed_csv(
                    CURRENT_CSV,
                    [item for item in migrated if item.get("date", "") >= CURRENT_START_DATE],
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
