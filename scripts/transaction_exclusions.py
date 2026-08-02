#!/usr/bin/env python3
"""Strict reviewed exclusions for known-bad HMLR Price Paid transactions."""

import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = ROOT / "config" / "transaction-exclusions.json"
SCHEMA_VERSION = 1
MATCH_POLICY = "reviewed-hmlr-source-id-or-exact-tuple-fail-closed"
REQUIRED_ENTRY_FIELDS = {
    "id",
    "disposition",
    "source",
    "sourceTransactionId",
    "transactionUuid",
    "address",
    "postcode",
    "price",
    "date",
    "propertyType",
    "category",
    "reason",
    "evidence",
    "reviewedAt",
    "reviewedBy",
}
PROPERTY_TYPES = {"Detached", "Semi Detached", "Terraced", "Flat Maisonette"}
SOURCE_TRANSACTION_RE = re.compile(
    r"^https?://landregistry\.data\.gov\.uk/data/ppi/transaction/([A-F0-9-]+)/current$",
    re.IGNORECASE,
)
TRANSACTION_UUID_RE = re.compile(r"^[A-F0-9-]+$", re.IGNORECASE)
LONDON_TIMEZONE = ZoneInfo("Europe/London")


def london_today(now=None):
    current = now or datetime.now(LONDON_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=LONDON_TIMEZONE)
    return current.astimezone(LONDON_TIMEZONE).date()


def clean(value):
    return " ".join(str(value or "").replace("\xa0", " ").split()).strip()


def canonical_address(value):
    return re.sub(r"[^A-Z0-9]+", " ", clean(value).upper()).strip()


def canonical_postcode(value):
    return re.sub(r"[^A-Z0-9]", "", clean(value).upper())


def canonical_source_id(value):
    value = clean(value).upper().rstrip("/")
    if "/TRANSACTION/" in value:
        value = value.split("/TRANSACTION/", 1)[1]
    if value.endswith("/CURRENT"):
        value = value[:-8]
    return value.strip("/{} ")


def transaction_signature(record):
    try:
        price = int(float(str(record.get("price", "")).replace(",", "")))
    except (TypeError, ValueError):
        price = None
    return (
        canonical_address(record.get("address")),
        canonical_postcode(record.get("postcode")),
        price,
        clean(record.get("date"))[:10],
        clean(record.get("propertyType") or record.get("property_type")).title(),
        clean(record.get("category") or record.get("transaction_category")).upper().replace("CATEGORY-", ""),
    )


def _validate_entry(entry, index):
    if not isinstance(entry, dict):
        raise ValueError(f"Transaction exclusion {index} must be an object")
    missing = REQUIRED_ENTRY_FIELDS - set(entry)
    extra = set(entry) - REQUIRED_ENTRY_FIELDS
    if missing or extra:
        raise ValueError(
            f"Transaction exclusion {index} has invalid fields; missing={sorted(missing)}, extra={sorted(extra)}"
        )
    if entry["disposition"] != "exclude":
        raise ValueError(f"Transaction exclusion {index} disposition must be exclude")
    if entry["source"] != "HM Land Registry Price Paid Data":
        raise ValueError(f"Transaction exclusion {index} source is unsupported")
    if entry["propertyType"] not in PROPERTY_TYPES or entry["category"] not in {"A", "B"}:
        raise ValueError(f"Transaction exclusion {index} has an unsupported property type or category")
    if not isinstance(entry["price"], int) or entry["price"] < 2_000_000:
        raise ValueError(f"Transaction exclusion {index} price must be an integer at or above the product floor")
    for field in ("id", "address", "postcode", "reason", "evidence", "reviewedBy"):
        if not clean(entry[field]):
            raise ValueError(f"Transaction exclusion {index} field {field} cannot be empty")
    if len(clean(entry["reason"])) < 20 or len(clean(entry["evidence"])) < 20:
        raise ValueError(f"Transaction exclusion {index} reason and evidence must be substantive")
    try:
        transaction_date = date.fromisoformat(entry["date"])
        reviewed_at = date.fromisoformat(entry["reviewedAt"])
    except (TypeError, ValueError) as error:
        raise ValueError(f"Transaction exclusion {index} dates must use YYYY-MM-DD") from error
    if reviewed_at < transaction_date or reviewed_at > london_today():
        raise ValueError(f"Transaction exclusion {index} review date is inconsistent")
    source_match = SOURCE_TRANSACTION_RE.fullmatch(clean(entry["sourceTransactionId"]))
    transaction_uuid = canonical_source_id(entry["transactionUuid"])
    source_uuid = canonical_source_id(entry["sourceTransactionId"])
    if (
        not source_match
        or not TRANSACTION_UUID_RE.fullmatch(clean(entry["transactionUuid"]))
        or not transaction_uuid
        or transaction_uuid != source_uuid
    ):
        raise ValueError(f"Transaction exclusion {index} source transaction identity is inconsistent")


def load_transaction_exclusion_ledger(path=DEFAULT_LEDGER):
    path = Path(path)
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Transaction exclusion ledger is unreadable: {path}") from error
    if not isinstance(ledger, dict):
        raise ValueError("Transaction exclusion ledger must be an object")
    if set(ledger) != {"schemaVersion", "matchPolicy", "description", "exclusions"}:
        raise ValueError("Transaction exclusion ledger top-level fields are invalid")
    if ledger.get("schemaVersion") != SCHEMA_VERSION or ledger.get("matchPolicy") != MATCH_POLICY:
        raise ValueError("Transaction exclusion ledger schema or match policy is unsupported")
    if not clean(ledger.get("description")):
        raise ValueError("Transaction exclusion ledger description cannot be empty")
    exclusions = ledger.get("exclusions")
    if not isinstance(exclusions, list) or not exclusions:
        raise ValueError("Transaction exclusion ledger must contain at least one reviewed exclusion")
    for index, entry in enumerate(exclusions):
        _validate_entry(entry, index)
    ids = [entry["id"] for entry in exclusions]
    source_ids = [canonical_source_id(entry["sourceTransactionId"]) for entry in exclusions]
    signatures = [transaction_signature(entry) for entry in exclusions]
    if len(ids) != len(set(ids)) or len(source_ids) != len(set(source_ids)) or len(signatures) != len(set(signatures)):
        raise ValueError("Transaction exclusion ledger contains duplicate identities")
    return ledger


def find_transaction_exclusion(record, ledger=None):
    ledger = ledger or load_transaction_exclusion_ledger()
    record_signature = transaction_signature(record)
    record_source_id = canonical_source_id(
        record.get("tx") or record.get("sourceTransactionId") or record.get("source_transaction_id")
    )
    for entry in ledger["exclusions"]:
        expected_source_id = canonical_source_id(entry["sourceTransactionId"])
        if record_source_id:
            if record_source_id == expected_source_id:
                return entry
            continue
        if record_signature == transaction_signature(entry):
            return entry
    return None


def transaction_exclusion_metadata(ledger=None):
    ledger = ledger or load_transaction_exclusion_ledger()
    canonical = json.dumps(ledger, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "schemaVersion": ledger["schemaVersion"],
        "matchPolicy": ledger["matchPolicy"],
        "reviewedExclusionCount": len(ledger["exclusions"]),
        "ledgerFingerprint": hashlib.sha256(canonical).hexdigest(),
    }


def excluded_transaction_failures(records, ledger=None):
    ledger = ledger or load_transaction_exclusion_ledger()
    failures = []
    for record in records:
        exclusion = find_transaction_exclusion(record, ledger)
        if exclusion:
            failures.append(
                f"Reviewed transaction exclusion {exclusion['id']} is present in the published feed"
            )
    return failures
