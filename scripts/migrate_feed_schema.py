#!/usr/bin/env python3
"""Migrate an existing INSIGHT feed without discarding approved enrichment."""

import argparse
from pathlib import Path

from insight_data_utils import (
    DEFAULT_INPUT_JS,
    FEED_SCHEMA_VERSION,
    PROPERTY_RECORD_SCHEMA_VERSION,
    property_record_id,
    read_js,
    write_js,
)
from sweep_land_registry import stable_transaction_id


def migrate_transaction(item):
    migrated = dict(item)
    migrated["id"] = stable_transaction_id(
        item.get("address"),
        item.get("postcode"),
        item.get("price"),
        item.get("date"),
        item.get("propertyType"),
        item.get("category"),
    )
    migrated["propertyRecordId"] = property_record_id(migrated)
    geocode = item.get("geocode") if isinstance(item.get("geocode"), dict) else {}
    precision = str(geocode.get("precision") or "").lower()
    if "postcode" in precision or "centroid" in precision:
        migrated["coordinateSource"] = geocode.get("source") or "Postcodes.io"
        migrated["coordinatePrecision"] = "postcode-centroid"
    return migrated


def main():
    parser = argparse.ArgumentParser(description="Migrate an INSIGHT feed to the current schema.")
    parser.add_argument("--input-js", default=str(DEFAULT_INPUT_JS))
    parser.add_argument("--write-js", default=str(DEFAULT_INPUT_JS))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    transactions, _summary, meta = read_js(args.input_js)
    migrated = [migrate_transaction(item) for item in transactions]
    meta = dict(meta)
    meta["schemaVersion"] = FEED_SCHEMA_VERSION
    meta["propertyRecordSchemaVersion"] = PROPERTY_RECORD_SCHEMA_VERSION
    meta["canonicalPropertyRecords"] = len({item["propertyRecordId"] for item in migrated})
    meta["propertyIdentityMode"] = "full-normalised-address-plus-postcode-fail-closed"
    ids = [item["id"] for item in migrated]
    if len(ids) != len(set(ids)):
        raise RuntimeError("Stable transaction ID collision detected")
    print(f"Migrated {len(migrated)} transactions to schema {FEED_SCHEMA_VERSION}.")
    if not args.dry_run:
        write_js(Path(args.write_js), migrated, meta)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
