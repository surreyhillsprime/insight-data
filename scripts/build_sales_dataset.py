#!/usr/bin/env python3
"""Publish one HMLR-only data snapshot for the immutable signed INSIGHT app.

This offline producer consumes an already verified base acquisition and its
matching complete-accounted property histories. It never retrieves EPC/context
or exports their fields. The content digest excludes outer acquisition and
publication clocks, while retaining each history's actual evidence timestamp.
"""

import argparse
import hashlib
import json
import math
import re
from datetime import date, datetime, timezone
from pathlib import Path

from insight_data_utils import FEED_SCHEMA_VERSION, read_js
from sweep_land_registry import velocity_cutoff_date
from validate_sales_history_feed import (
    ADDRESS_DATA_USE, ATTRIBUTION, REDISTRIBUTION_RIGHTS, SOURCE_NAME,
    assignment, base_feed_identity, canonical_json, parse_timestamp, sha256_json,
    validate as validate_history,
)

ROOT = Path(__file__).resolve().parents[1]
SCOPE = "surrey-residential-2m-1995"
MAX_BYTES = 25 * 1024 * 1024
TRANSACTION_FIELDS = frozenset({
    "id", "propertyRecordId", "market", "district", "address", "paon", "saon", "street", "locality", "town",
    "postcode", "price", "priceText", "date", "propertyType", "estateId", "estate", "estateClassification",
    "estateType", "estateRuleId", "estateRegistryVersion", "estateEvidenceStatus", "estateReviewStatus",
    "source", "kind", "category",
})
METADATA_FIELDS = frozenset({
    "schemaVersion", "propertyRecordSchemaVersion", "propertyIdentityMode",
    "rawRows", "residentialRows", "mappedTransactions", "canonicalPropertyRecords",
    "from", "to", "latestObservedSaleDate", "velocityMaturityLagMonths",
    "velocityCutoffDate", "velocityMethodVersion", "priceFloor", "estateRegistryVersion", "source",
})
HISTORY_SALE_FIELDS = frozenset({"price", "priceText", "date", "propertyType", "category", "source"})
HISTORY_META_FIELDS = frozenset({
    "schemaVersion", "deploymentMode", "publicationStatus", "coverageMode", "coverageStatus",
    "source", "redistributionRights", "addressDataUse", "attribution", "coverageFrom",
    "freshnessWindowDays", "propertiesRequested", "propertiesChecked", "propertiesUnavailable",
    "propertiesNotChecked", "propertiesWithHistory", "propertiesCheckedNoHistory",
    "transactionsFound", "lookupKeys", "canonicalPropertyRecords", "transactionAliases",
    "baseFeedFingerprint", "historyFingerprint",
    "updatedAt", "sourceCheckedAt",
})
REFRESH_MODES = frozenset({"current-sparql", "annual-archives-rolling", "annual-archives-all-years"})


def scalar_projection(source, allowed):
    if not isinstance(source, dict):
        raise ValueError("Sales dataset source must be an object")
    result = {key: source[key] for key in sorted(allowed) if key in source}
    for value in result.values():
        if not isinstance(value, (str, int, float, bool)) and value is not None:
            raise ValueError("Sales dataset allowlist requires scalar fields")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Sales dataset numbers must be finite")
        if isinstance(value, str) and ("http://" in value.lower() or "https://" in value.lower()):
            raise ValueError("Sales dataset contains a provider URL in a public field")
    return result


def verified_base_acquisition(metadata, *, now=None):
    """Check genuine acquisition provenance, never infer it from sale dates."""
    now = now or datetime.now(timezone.utc)
    if metadata.get("sourceFetchStatus") != "verified" or metadata.get("sourceRefreshMode") not in REFRESH_MODES:
        raise ValueError("Sales dataset requires a verified official HMLR acquisition")
    checked = parse_timestamp(metadata.get("sourceCheckedAt"), "HMLR sourceCheckedAt").replace(microsecond=0)
    age = (now - checked).total_seconds()
    if not -300 <= age <= 45 * 86400:
        raise ValueError("Verified HMLR acquisition is stale or in the future")
    refresh_from = metadata.get("sourceRefreshFrom")
    try:
        refresh_date = date.fromisoformat(refresh_from)
    except (TypeError, ValueError) as error:
        raise ValueError("HMLR sourceRefreshFrom must be an ISO date") from error
    if refresh_date < date(1995, 1, 1) or refresh_date > checked.date():
        raise ValueError("HMLR sourceRefreshFrom is outside the source coverage")
    expected_from = {
        "current-sparql": "2010-01-01",
        "annual-archives-rolling": f"{max(2010, checked.year - 1)}-01-01",
        "annual-archives-all-years": "1995-01-01",
    }[metadata["sourceRefreshMode"]]
    if refresh_from != expected_from:
        raise ValueError("HMLR sourceRefreshFrom does not match the acquisition mode")
    return checked, refresh_from


def checked_sale(source, *, floor=1, source_name=SOURCE_NAME, as_of=None):
    sale = scalar_projection(source, HISTORY_SALE_FIELDS)
    if type(sale.get("price")) is not int or sale["price"] < floor:
        raise ValueError("Sales dataset has an invalid sale price")
    try:
        when = date.fromisoformat(sale.get("date", ""))
    except (TypeError, ValueError) as error:
        raise ValueError("Sales dataset has an invalid sale date") from error
    if when < date(1995, 1, 1) or when > (as_of or datetime.now(timezone.utc).date()):
        raise ValueError("Sales dataset sale date is outside the available history")
    if sale.get("source") != source_name:
        raise ValueError("Sales dataset sale is not attributed to HMLR")
    if sale.get("propertyType") not in {"Detached", "Semi Detached", "Terraced", "Flat Maisonette"} or sale.get("category") not in {"A", "B"}:
        raise ValueError("Sales dataset sale type or category is invalid")
    return sale


def build_dataset(rows, metadata, histories, history_metadata, *, now=None, published_at=None):
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    base_checked, refresh_from = verified_base_acquisition(metadata, now=now)
    history_checked = parse_timestamp(history_metadata.get("sourceCheckedAt"), "History sourceCheckedAt").replace(microsecond=0)
    history_updated = parse_timestamp(history_metadata.get("updatedAt"), "History updatedAt").replace(microsecond=0)
    if not -300 <= (now - history_checked).total_seconds() <= 45 * 86400:
        raise ValueError("Sales history source check is stale or in the future")
    if history_updated < history_checked or (history_updated - now).total_seconds() > 300:
        raise ValueError("Sales history publication timestamp is invalid")
    properties, ids, identity_map, base_fingerprint = base_feed_identity(rows)
    if len(rows) > 100_000:
        raise ValueError("Sales dataset transaction count exceeds its bound")
    if (type(metadata.get("schemaVersion")) is not int or metadata["schemaVersion"] != FEED_SCHEMA_VERSION
            or type(metadata.get("propertyRecordSchemaVersion")) is not int or metadata["propertyRecordSchemaVersion"] != 1):
        raise ValueError("Sales dataset requires supported canonical metadata")
    if metadata.get("propertyIdentityMode") != "full-normalised-address-plus-postcode-fail-closed":
        raise ValueError("Sales dataset requires fail-closed canonical property identities")
    if metadata.get("priceFloor") != 2_000_000 or metadata.get("from") != "1995-01-01":
        raise ValueError("Sales dataset scope differs from Surrey residential £2m+ since 1995")
    for field, expected in (("mappedTransactions", len(rows)), ("residentialRows", len(rows)),
                            ("canonicalPropertyRecords", len(properties))):
        if type(metadata.get(field)) is not int or metadata[field] != expected:
            raise ValueError(f"Sales dataset {field} does not reconcile")
    latest = max(row.get("date", "") for row in rows)
    if metadata.get("to") != latest or metadata.get("latestObservedSaleDate") != latest:
        raise ValueError("Sales dataset latest observed sale date does not reconcile")
    if metadata.get("velocityMaturityLagMonths") != 2 or metadata.get("velocityCutoffDate") != velocity_cutoff_date(latest):
        raise ValueError("Sales dataset velocity cutoff does not reconcile")
    projected_rows = []
    for row in sorted(rows, key=lambda item: item["id"]):
        checked_sale(row, floor=2_000_000, source_name="HM Land Registry", as_of=now.date())
        if row["date"] > base_checked.date().isoformat():
            raise ValueError("Base sale date is after its official source check")
        if not re.fullmatch(r"lr-[0-9a-f]{20}", row["id"]):
            raise ValueError("Sales dataset transaction id is not a canonical INSIGHT id")
        projected_rows.append(scalar_projection(row, TRANSACTION_FIELDS))
    if history_metadata.get("baseFeedFingerprint") != base_fingerprint:
        raise ValueError("Sales dataset histories do not match the base identity fingerprint")
    for field, expected in {
        "schemaVersion": 1, "deploymentMode": "commercial", "publicationStatus": "complete",
        "coverageMode": "full-available-price-paid-history", "coverageStatus": "complete-accounted",
        "source": SOURCE_NAME, "redistributionRights": REDISTRIBUTION_RIGHTS,
        "addressDataUse": ADDRESS_DATA_USE, "attribution": ATTRIBUTION, "coverageFrom": "1995",
    }.items():
        if history_metadata.get(field) != expected:
            raise ValueError(f"Sales dataset history {field} is invalid")
    if type(history_metadata.get("schemaVersion")) is not int:
        raise ValueError("Sales dataset history schemaVersion must be an integer")
    canonical = {key: value for key, value in histories.items() if key.startswith("property:")}
    if set(canonical) != properties:
        raise ValueError("Sales dataset history property set differs from the base feed")
    history_by_property = {}
    complete_times = []
    for property_id, record in sorted(canonical.items()):
        if record.get("propertyRecordId") != property_id:
            raise ValueError("Sales dataset history identity disagrees with its key")
        status = record.get("coverageStatus")
        if status not in {"complete", "unavailable"}:
            raise ValueError("Sales dataset histories are not completely accounted")
        if record.get("source") != SOURCE_NAME or record.get("coverageFrom") != "1995":
            raise ValueError("Sales dataset history source or coverage is invalid")
        raw_sales = record.get("transactions")
        if not isinstance(raw_sales, list) or len(raw_sales) > 1_000:
            raise ValueError("Sales dataset history transactions exceed their bound")
        sales = sorted((checked_sale(sale, as_of=now.date()) for sale in raw_sales),
                       key=lambda sale: (sale["date"], sale["price"], canonical_json(sale)), reverse=True)
        record_checked = parse_timestamp(record.get("updatedAt"), "History record updatedAt").replace(microsecond=0)
        if record_checked > history_updated or any(sale["date"] > record_checked.date().isoformat() for sale in sales):
            raise ValueError("History sale or record timestamp is after its actual source check")
        if status == "unavailable" and sales:
            raise ValueError("Unavailable sales histories contain sales")
        if status == "complete":
            complete_times.append(record_checked)
            signatures = {(sale["date"], sale["price"], sale["propertyType"], sale["category"]) for sale in sales}
            if any((row["date"], row["price"], row["propertyType"], row["category"]) not in signatures
                   for row in rows if row["propertyRecordId"] == property_id):
                raise ValueError("Complete sales history omits a canonical base sale")
        history_by_property[property_id] = {
            "propertyRecordId": property_id, "coverageStatus": status, "coverageFrom": "1995",
            "source": SOURCE_NAME, "totalTransactions": len(sales),
            "updatedAt": record_checked.isoformat().replace("+00:00", "Z"),
            "latestTransaction": sales[0] if sales else None, "transactions": sales,
        }
    if not complete_times or min(complete_times) != history_checked:
        raise ValueError("History sourceCheckedAt does not equal the oldest complete lookup")
    projected_history_meta = scalar_projection(history_metadata, HISTORY_META_FIELDS)
    complete_count = sum(record["coverageStatus"] == "complete" for record in history_by_property.values())
    with_history = sum(bool(record["transactions"]) for record in history_by_property.values())
    projected_history_meta.update(
        historyFingerprint=sha256_json(history_by_property), lookupKeys=len(properties), transactionAliases=0,
        canonicalPropertyRecords=len(properties), propertiesRequested=len(properties), propertiesChecked=complete_count,
        propertiesUnavailable=len(properties) - complete_count, propertiesNotChecked=0,
        propertiesWithHistory=with_history, propertiesCheckedNoHistory=complete_count - with_history,
        transactionsFound=sum(len(record["transactions"]) for record in history_by_property.values()),
        sourceCheckedAt=history_checked.isoformat().replace("+00:00", "Z"),
        updatedAt=history_updated.isoformat().replace("+00:00", "Z"),
    )
    payload = canonical_json({
        "schemaVersion": 1, "scope": SCOPE, "metadata": scalar_projection(metadata, METADATA_FIELDS),
        "transactions": projected_rows, "historyByProperty": history_by_property,
        "historyMetadata": projected_history_meta,
    })
    published = parse_timestamp(published_at, "publishedAt").replace(microsecond=0) if published_at else now
    if published < max(base_checked, history_updated) or (published - now).total_seconds() > 300:
        raise ValueError("Publication time predates acquisition or is in the future")
    envelope = {
        "schemaVersion": 1, "sourceCheckedAt": base_checked.isoformat().replace("+00:00", "Z"),
        "sourceRefreshFrom": refresh_from, "publishedAt": published.isoformat().replace("+00:00", "Z"),
        "contentSha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(), "payload": payload,
    }
    if len(canonical_json(envelope).encode("utf-8")) > MAX_BYTES:
        raise ValueError("Sales dataset exceeds the 25 MiB native bound")
    return envelope


def validate_envelope(envelope, *, now=None):
    """Validate an independently published snapshot without legacy feed files."""
    if not isinstance(envelope, dict) or set(envelope) != {
        "schemaVersion", "sourceCheckedAt", "sourceRefreshFrom", "publishedAt", "contentSha256", "payload",
    } or type(envelope.get("schemaVersion")) is not int or envelope["schemaVersion"] != 1 or not isinstance(envelope.get("payload"), str):
        raise ValueError("Sales dataset envelope schema is invalid")
    if len(canonical_json(envelope).encode("utf-8")) > MAX_BYTES:
        raise ValueError("Sales dataset exceeds the native bound")
    if hashlib.sha256(envelope["payload"].encode("utf-8")).hexdigest() != envelope.get("contentSha256"):
        raise ValueError("Sales dataset content digest is invalid")
    payload = json.loads(envelope["payload"])
    if not isinstance(payload, dict) or set(payload) != {
        "schemaVersion", "scope", "metadata", "transactions", "historyByProperty", "historyMetadata",
    } or type(payload.get("schemaVersion")) is not int or payload["schemaVersion"] != 1 or payload.get("scope") != SCOPE:
        raise ValueError("Sales dataset payload schema is invalid")
    refresh_from = envelope.get("sourceRefreshFrom")
    acquisition_mode = {"1995-01-01": "annual-archives-all-years", "2010-01-01": "current-sparql"}.get(
        refresh_from, "annual-archives-rolling")
    metadata = dict(payload["metadata"])
    metadata.update(sourceCheckedAt=envelope["sourceCheckedAt"], sourceRefreshFrom=refresh_from,
                    sourceFetchStatus="verified", sourceRefreshMode=acquisition_mode)
    rebuilt = build_dataset(payload["transactions"], metadata, payload["historyByProperty"],
                            payload["historyMetadata"], now=now, published_at=envelope["publishedAt"])
    if rebuilt != envelope:
        raise ValueError("Sales dataset contains noncanonical fields or inconsistent accounting")
    return payload


def load_prior_dataset(path):
    """Read a verified prior generation as a seed, without claiming it is fresh."""
    if not path or not Path(path).exists():
        return None
    source = Path(path)
    if source.stat().st_size > MAX_BYTES:
        raise ValueError("Prior native sales dataset exceeds the source bound")
    envelope = json.loads(source.read_text(encoding="utf-8"))
    published = parse_timestamp(envelope.get("publishedAt"), "Prior publishedAt")
    # Integrity is checked at publication time. Current per-property freshness
    # and a new official base acquisition remain mandatory before republishing.
    return validate_envelope(envelope, now=min(datetime.now(timezone.utc), published))


def build_from_files(transactions_path, history_path, *, published_at=None):
    validate_history(history_path, base_feed=transactions_path, minimum_property_coverage_percent=99,
                     minimum_transactions=6735, maximum_properties_unavailable=4, maximum_age_days=45)
    rows, _summary, metadata = read_js(transactions_path)
    history_text = Path(history_path).read_text(encoding="utf-8")
    return build_dataset(rows, metadata, assignment(history_text, "SURREY_SALES_HISTORY"),
                         assignment(history_text, "SURREY_SALES_HISTORY_META"), published_at=published_at)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transactions", type=Path, default=ROOT / "outputs/surrey-transactions.js")
    parser.add_argument("--sales-history", type=Path, default=ROOT / "outputs/sales-history.js")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/sales-dataset.json")
    parser.add_argument("--published-at")
    parser.add_argument("--check", action="store_true", help="Verify existing content against the exact current feeds.")
    parser.add_argument("--validate", action="store_true", help="Validate the self-contained published snapshot.")
    parser.add_argument("--if-verified", action="store_true", help="Preserve the prior native snapshot when legacy base provenance is unavailable.")
    args = parser.parse_args()
    if args.check and args.validate:
        parser.error("Choose either --check or --validate")
    if args.if_verified and not (args.check or args.validate):
        _rows, _summary, source_metadata = read_js(args.transactions)
        if source_metadata.get("sourceFetchStatus") != "verified":
            print("Native sales snapshot retained: legacy base has no verified official acquisition; use the HMLR-only refresh workflow.")
            return
    if args.validate:
        if args.output.stat().st_size > MAX_BYTES:
            raise ValueError("Sales dataset exceeds the native bound")
        envelope = json.loads(args.output.read_text(encoding="utf-8"))
        validate_envelope(envelope)
        print(f"Sales dataset verified: {envelope['contentSha256']}; source checked {envelope['sourceCheckedAt']}")
        return
    existing = json.loads(args.output.read_text(encoding="utf-8")) if args.check else None
    envelope = build_from_files(args.transactions, args.sales_history,
                                published_at=existing.get("publishedAt") if existing else args.published_at)
    if args.check:
        if existing != envelope:
            raise ValueError("Published sales dataset differs from its coherent source feeds")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(args.output.name + ".tmp")
        temporary.write_text(canonical_json(envelope) + "\n", encoding="utf-8")
        temporary.replace(args.output)
    print(f"Sales dataset {'verified' if args.check else 'built'}: {envelope['contentSha256']}; "
          f"source checked {envelope['sourceCheckedAt']}; {len(canonical_json(envelope).encode('utf-8')):,} bytes")


if __name__ == "__main__":
    main()
