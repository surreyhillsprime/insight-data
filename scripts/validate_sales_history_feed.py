#!/usr/bin/env python3
"""Validate the standalone HM Land Registry sales-history publication contract."""

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path


MAX_FEED_BYTES = 50 * 1024 * 1024
MAX_LOOKUP_KEYS = 200_000
MAX_FRESHNESS_WINDOW_DAYS = 45
SOURCE_NAME = "HM Land Registry Price Paid Data"
SOURCE_LICENCE_URL = (
    "https://www.gov.uk/government/statistical-data-sets/price-paid-data-downloads"
)
REDISTRIBUTION_RIGHTS = "open-government-licence-v3.0"
ADDRESS_DATA_USE = "residential-property-price-information-display"
ATTRIBUTION = (
    "Contains HM Land Registry data © Crown copyright and database right 2021. "
    "This data is licensed under the Open Government Licence v3.0."
)
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
    if not isinstance(base_rows, list) or not base_rows:
        raise ValueError("Base feed must contain transaction rows")
    property_ids = set()
    transaction_ids = set()
    transaction_properties = {}
    pairs = []
    for item in base_rows:
        if not isinstance(item, dict):
            raise ValueError("Base feed contains a non-object transaction row")
        property_id = str(item.get("propertyRecordId") or "")
        transaction_id = str(item.get("id") or "")
        if not property_id.startswith("property:") or not transaction_id:
            raise ValueError(
                "Base feed contains a row without canonical property and transaction ids"
            )
        if transaction_id in transaction_ids:
            raise ValueError(f"Base feed contains duplicate transaction id {transaction_id}")
        if transaction_id.startswith("property:"):
            raise ValueError("Base transaction ids must not collide with canonical property ids")
        property_ids.add(property_id)
        transaction_ids.add(transaction_id)
        transaction_properties[transaction_id] = property_id
        pairs.append([transaction_id, property_id])
    return (
        property_ids,
        transaction_ids,
        transaction_properties,
        sha256_json(sorted(pairs)),
    )


def read_base_feed(path):
    text = Path(path).read_text(encoding="utf-8")
    matches = re.findall(
        r"^window\.SURREY_LAND_REG_TRANSACTIONS\s*=\s*(.*);$",
        text,
        flags=re.M,
    )
    if len(matches) != 1:
        raise ValueError("Expected exactly one base transaction assignment")
    return json.loads(matches[0])


def validate(
    path,
    *,
    base_feed=None,
    allow_local=False,
    minimum_properties_with_history=0,
    minimum_property_coverage_percent=0,
    minimum_transactions=0,
    maximum_properties_unavailable=None,
    maximum_age_days=MAX_FRESHNESS_WINDOW_DAYS,
    allow_unbound_commercial=False,
    allow_stale=False,
):
    path = Path(path)
    if path.stat().st_size > MAX_FEED_BYTES:
        raise ValueError("Sales history exceeds the native 50 MiB safety limit")
    text = path.read_text(encoding="utf-8")
    histories = assignment(text, "SURREY_SALES_HISTORY")
    metadata = assignment(text, "SURREY_SALES_HISTORY_META")
    if len(histories) > MAX_LOOKUP_KEYS:
        raise ValueError("Sales history exceeds the native lookup-key safety limit")

    mode = metadata.get("deploymentMode")
    expected_modes = {"commercial", "local"} if allow_local else {"commercial"}
    if metadata.get("schemaVersion") != 1 or mode not in expected_modes:
        raise ValueError("Sales history metadata has an invalid schemaVersion or deploymentMode")

    canonical = {
        key: value
        for key, value in histories.items()
        if key.startswith("property:")
    }
    aliases = {
        key: value
        for key, value in histories.items()
        if not key.startswith("property:")
    }
    if mode == "commercial" and not canonical:
        raise ValueError("Commercial sales-history publication cannot be empty")

    allowed_statuses = (
        {"complete", "unavailable"}
        if mode == "commercial"
        else {"complete", "partial", "unavailable", "not_checked"}
    )
    status_counts = {status: 0 for status in allowed_statuses}
    properties_with_history = 0
    transactions_found = 0
    published_transaction_ids = set()
    complete_check_times = []
    record_update_times = []
    for key, record in canonical.items():
        if not isinstance(record, dict) or record.get("propertyRecordId") != key:
            raise ValueError("Sales-history canonical record is missing its propertyRecordId")
        status = record.get("coverageStatus")
        if status not in allowed_statuses:
            raise ValueError("Sales-history record has an invalid coverage status")
        transactions = record.get("transactions")
        if not isinstance(transactions, list):
            raise ValueError("Sales-history transactions must be an array")
        declared_total = record.get("totalTransactions")
        if mode == "commercial" and declared_total is None:
            raise ValueError("Commercial sales-history record has no totalTransactions")
        if declared_total is not None:
            total = exact_nonnegative_integer(
                declared_total,
                f"{key} totalTransactions",
            )
            if total != len(transactions):
                raise ValueError(
                    "Sales-history totalTransactions disagrees with its transactions array"
                )
        if record.get("latestTransaction") != (transactions[0] if transactions else None):
            raise ValueError(
                "Sales-history latestTransaction does not match its transactions array"
            )
        if status in {"unavailable", "not_checked"} and transactions:
            raise ValueError("Unavailable/not-checked sales-history record contains transactions")
        if status == "unavailable" and mode == "commercial" and not str(
            record.get("coverageReason") or ""
        ).strip():
            raise ValueError("Unavailable commercial sales-history record lacks a reason")
        if mode == "commercial" and record.get("source") != SOURCE_NAME:
            raise ValueError("Commercial sales-history record has an unexpected source")
        if mode == "commercial":
            record_updated_at = parse_timestamp(
                record.get("updatedAt"),
                f"{key} updatedAt",
            )
            record_update_times.append(record_updated_at)
            if status == "complete":
                complete_check_times.append(record_updated_at)
        for transaction in transactions:
            if not isinstance(transaction, dict):
                raise ValueError("Sales-history transaction is not an object")
            transaction_id = str(transaction.get("id") or "")
            if not transaction_id:
                raise ValueError("Sales-history transaction has no source id")
            if transaction_id in published_transaction_ids:
                raise ValueError(
                    f"Sales-history transaction id is duplicated: {transaction_id}"
                )
            published_transaction_ids.add(transaction_id)
            if mode == "commercial" and transaction.get("source") != SOURCE_NAME:
                raise ValueError("Commercial sales-history transaction has an unexpected source")
        status_counts[status] += 1
        properties_with_history += int(bool(transactions))
        transactions_found += len(transactions)

    for transaction_id, record in aliases.items():
        if not isinstance(record, dict):
            raise ValueError("Sales-history transaction alias is not an object")
        property_id = record.get("propertyRecordId")
        if property_id not in canonical or record != canonical[property_id]:
            raise ValueError(
                f"Sales-history transaction alias {transaction_id} "
                "does not equal its canonical record"
            )

    if mode == "commercial":
        if metadata.get("publicationStatus") != "complete":
            raise ValueError("Commercial sales-history publication is not complete")
        if (
            metadata.get("coverageMode") != "full-available-price-paid-history"
            or metadata.get("coverageStatus") != "complete-accounted"
        ):
            raise ValueError(
                "Commercial sales-history publication must declare complete accounted coverage"
            )
        if metadata.get("source") != SOURCE_NAME:
            raise ValueError("Commercial sales-history publication source is incorrect")
        if metadata.get("sourceLicenceUrl") != SOURCE_LICENCE_URL:
            raise ValueError("Commercial sales-history publication licence URL is incorrect")
        if metadata.get("redistributionRights") != REDISTRIBUTION_RIGHTS:
            raise ValueError(
                "Commercial sales-history publication lacks explicit OGL v3 rights"
            )
        if metadata.get("addressDataUse") != ADDRESS_DATA_USE:
            raise ValueError(
                "Commercial sales-history publication lacks the HMLR address-display purpose"
            )
        if metadata.get("attribution") != ATTRIBUTION:
            raise ValueError("Commercial sales-history publication attribution is incorrect")
        if metadata.get("coverageFrom") != "1995":
            raise ValueError("Commercial sales-history publication must declare coverage from 1995")

        freshness_window = exact_nonnegative_integer(
            metadata.get("freshnessWindowDays"),
            "freshnessWindowDays",
        )
        if not 1 <= freshness_window <= MAX_FRESHNESS_WINDOW_DAYS:
            raise ValueError("Sales-history freshnessWindowDays must be between 1 and 45")
        updated_at = parse_timestamp(metadata.get("updatedAt"), "Sales-history updatedAt")
        source_checked_at = parse_timestamp(
            metadata.get("sourceCheckedAt"),
            "Sales-history sourceCheckedAt",
        )
        if updated_at < source_checked_at:
            raise ValueError(
                "Sales-history updatedAt predates its sourceCheckedAt"
            )
        now = datetime.now(timezone.utc)
        if (now - updated_at).total_seconds() < -300:
            raise ValueError("Sales-history updatedAt is implausibly in the future")
        if any((now - value).total_seconds() < -300 for value in record_update_times):
            raise ValueError("Sales-history record updatedAt is implausibly in the future")
        expected_source_checked_at = (
            min(complete_check_times)
            if complete_check_times
            else updated_at
        )
        if source_checked_at != expected_source_checked_at:
            raise ValueError(
                "Sales-history sourceCheckedAt does not match the oldest complete lookup"
            )
        age_seconds = (now - source_checked_at).total_seconds()
        if age_seconds < -300:
            raise ValueError("Sales-history sourceCheckedAt is implausibly in the future")
        effective_maximum_age = freshness_window
        if maximum_age_days > 0:
            effective_maximum_age = min(effective_maximum_age, maximum_age_days)
        # A structurally valid stale publication may be loaded only to
        # bootstrap its own refresh. All provenance, identity, reconciliation,
        # and future-timestamp checks above still apply.
        if not allow_stale and age_seconds > effective_maximum_age * 86400:
            raise ValueError(
                "Sales-history publication is stale: sourceCheckedAt exceeds "
                f"{effective_maximum_age} days"
            )

        checked = status_counts["complete"]
        unavailable = status_counts["unavailable"]
        checked_no_history = checked - properties_with_history
        count_contract = {
            "propertiesRequested": len(canonical),
            "propertiesChecked": checked,
            "propertiesUnavailable": unavailable,
            "propertiesNotChecked": 0,
            "propertiesWithHistory": properties_with_history,
            "propertiesCheckedNoHistory": checked_no_history,
            "transactionsFound": transactions_found,
            "lookupKeys": len(histories),
            "canonicalPropertyRecords": len(canonical),
            "transactionAliases": len(aliases),
        }
        for field, expected in count_contract.items():
            if exact_nonnegative_integer(metadata.get(field), field) != expected:
                raise ValueError(f"Sales-history {field} metadata does not reconcile")
        if checked + unavailable != len(canonical):
            raise ValueError("Sales-history property coverage metadata does not reconcile")
        if (
            maximum_properties_unavailable is not None
            and maximum_properties_unavailable >= 0
            and unavailable > maximum_properties_unavailable
        ):
            raise ValueError(
                "Sales-history publication exceeded the reviewed unavailable-property "
                f"ceiling: {unavailable:,} > {maximum_properties_unavailable:,}"
            )
        if properties_with_history < minimum_properties_with_history:
            raise ValueError(
                "Sales-history publication regressed below the reviewed property-history "
                f"floor: {properties_with_history:,} < {minimum_properties_with_history:,}"
            )
        if not 0 <= minimum_property_coverage_percent <= 100:
            raise ValueError(
                "Sales-history minimum property coverage percent must be between 0 and 100"
            )
        if (
            canonical
            and properties_with_history * 100
            < len(canonical) * minimum_property_coverage_percent
        ):
            actual_percent = properties_with_history * 100 / len(canonical)
            raise ValueError(
                "Sales-history publication regressed below the reviewed property-denominator "
                f"coverage floor: {actual_percent:.1f}% < {minimum_property_coverage_percent:g}%"
            )
        if transactions_found < minimum_transactions:
            raise ValueError(
                "Sales-history publication regressed below the reviewed transaction floor: "
                f"{transactions_found:,} < {minimum_transactions:,}"
            )

        for field in ("baseFeedFingerprint", "historyFingerprint"):
            if not SHA256_RE.fullmatch(str(metadata.get(field) or "")):
                raise ValueError(f"Sales-history {field} is not a SHA-256 digest")
        if metadata["historyFingerprint"] != sha256_json(histories):
            raise ValueError(
                "Sales-history historyFingerprint does not match the published records"
            )
        alias_base_fingerprint = sha256_json(sorted(
            [transaction_id, str(record.get("propertyRecordId") or "")]
            for transaction_id, record in aliases.items()
        ))
        if metadata["baseFeedFingerprint"] != alias_base_fingerprint:
            raise ValueError(
                "Sales-history baseFeedFingerprint does not match its transaction aliases"
            )

    if base_feed:
        base_rows = read_base_feed(base_feed)
        (
            expected_properties,
            expected_transactions,
            transaction_properties,
            expected_fingerprint,
        ) = base_feed_identity(base_rows)
        if set(canonical) != expected_properties:
            raise ValueError(
                "Sales-history canonical property coverage is stale or outside the base feed"
            )
        if mode == "commercial" and set(aliases) != expected_transactions:
            raise ValueError(
                "Sales-history transaction-alias coverage is stale or outside the base feed"
            )
        if mode == "commercial":
            for transaction_id, property_id in transaction_properties.items():
                if aliases[transaction_id].get("propertyRecordId") != property_id:
                    raise ValueError(
                        "Sales-history transaction alias points at a different base property"
                    )
            published_signatures = {
                property_id: {
                    (
                        str(sale.get("date") or "")[:10],
                        int(float(sale.get("price", 0))),
                    )
                    for sale in record.get("transactions", [])
                }
                for property_id, record in canonical.items()
                if record.get("coverageStatus") == "complete"
            }
            for row in base_rows:
                property_id = str(row.get("propertyRecordId") or "")
                date = str(row.get("date") or "")[:10]
                try:
                    price = int(float(row.get("price", 0)))
                except (TypeError, ValueError):
                    price = 0
                if (
                    date
                    and price > 0
                    and property_id in published_signatures
                    and (date, price) not in published_signatures[property_id]
                ):
                    raise ValueError(
                        "Complete sales-history record omits a sale proven by the "
                        f"canonical base feed: {property_id}"
                    )
            if metadata["baseFeedFingerprint"] != expected_fingerprint:
                raise ValueError(
                    "Sales-history baseFeedFingerprint does not match the base feed"
                )
        else:
            requested = metadata.get(
                "propertiesRequested",
                metadata.get("propertiesChecked"),
            )
            accounted = sum(
                int(metadata.get(field) or 0)
                for field in (
                    "propertiesChecked",
                    "propertiesUnavailable",
                    "propertiesNotChecked",
                )
            )
            if requested != len(expected_properties) or accounted != len(
                expected_properties
            ):
                raise ValueError(
                    "Sales history property coverage is stale: "
                    f"expected {len(expected_properties):,}, "
                    f"found {len(canonical):,} keys / {requested} requested / "
                    f"{accounted} accounted"
                )
    elif mode == "commercial" and not allow_unbound_commercial:
        raise ValueError(
            "Commercial sales-history validation requires the exact base feed"
        )

    return {
        "lookupKeys": len(histories),
        "canonicalPropertyRecords": len(canonical),
        "transactionAliases": len(aliases),
        "propertiesWithHistory": properties_with_history,
        "transactionsFound": transactions_found,
        "updatedAt": metadata.get("updatedAt"),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default="outputs/sales-history.js")
    parser.add_argument("--allow-local", action="store_true")
    parser.add_argument("--base-feed", default="")
    parser.add_argument("--minimum-properties-with-history", type=int, default=0)
    parser.add_argument("--minimum-property-coverage-percent", type=float, default=0)
    parser.add_argument("--minimum-transactions", type=int, default=0)
    parser.add_argument("--maximum-properties-unavailable", type=int)
    parser.add_argument(
        "--maximum-age-days",
        type=int,
        default=MAX_FRESHNESS_WINDOW_DAYS,
    )
    args = parser.parse_args()
    result = validate(
        args.path,
        base_feed=args.base_feed or None,
        allow_local=args.allow_local,
        minimum_properties_with_history=args.minimum_properties_with_history,
        minimum_property_coverage_percent=args.minimum_property_coverage_percent,
        minimum_transactions=args.minimum_transactions,
        maximum_properties_unavailable=args.maximum_properties_unavailable,
        maximum_age_days=args.maximum_age_days,
    )
    print(
        "Valid sales history feed: "
        f"{result['lookupKeys']:,} lookup keys, "
        f"{result['canonicalPropertyRecords']:,} properties, "
        f"{result['transactionAliases']:,} aliases, "
        f"{result['transactionsFound']:,} sales"
    )


if __name__ == "__main__":
    main()
