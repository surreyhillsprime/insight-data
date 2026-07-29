#!/usr/bin/env python3
"""Validate INSIGHT's canonical, evidence-backed property-record feed.

The validator deliberately recomputes every aggregate and fingerprint instead
of trusting generated metadata.  It is suitable both for the local release
gate and for the live-data workflow immediately before publication.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECORDS = ROOT / "outputs" / "property-records.js"
DEFAULT_TRANSACTIONS = ROOT / "outputs" / "surrey-transactions.js"
PROPERTY_RECORD_SCHEMA = ROOT / "config" / "property-record.schema.json"
SCHEMA_VERSION = 1
EVENT_TYPES = {"sale", "planning_application", "epc_certificate"}
COVERAGE_STATUSES = {
    "complete",
    "partial",
    "checked_none",
    "not_checked",
    "unavailable",
    "failed",
}
INCOMPLETE_COVERAGE_STATUSES = {"partial", "not_checked", "failed"}
VOLATILE_FINGERPRINT_FIELDS = {
    "recordVersion",
    "createdAt",
    "updatedAt",
    "generatedAt",
    "checkedAt",
    "searchedAt",
    "fetchedAt",
    "runAt",
}
COVERAGE_ALIASES = {
    "sales": ("sales", "salesHistory", "sales_history"),
    "planning": ("planning", "planningHistory", "planning_history"),
    "epc": ("epc", "epcHistory", "epc_history"),
    "coordinates": ("coordinates",),
    "currentFlood": ("currentFlood",),
    "planningConstraints": ("planningConstraints",),
    "listedBuilding": ("listedBuilding",),
    "schools": ("schools",),
    "osUprn": ("osUprn",),
}
LISTED_BUILDING_STATUSES = {
    "confirmed_listed",
    "candidate_review",
    "no_direct_match",
    "unknown",
}
LISTED_BUILDING_GRADES = {"I", "II*", "II"}
LISTED_BUILDING_MATCH_METHODS = {
    "reviewed_override",
    "genuine_polygon_contains",
    "nearby_nhle_point",
}
CONFIRMED_LISTED_BUILDING_MATCH_METHODS = {
    "reviewed_override",
    "genuine_polygon_contains",
}
LISTED_BUILDING_MATCH_CONFIDENCE = {"confirmed", "review_required"}
HISTORIC_ENGLAND_SOURCE = "Historic England NHLE"
EVENT_COVERAGE_TYPES = {
    "sales": "sale",
    "planning": "planning_application",
    "epc": "epc_certificate",
}
BANNED_CAUSAL_PATTERNS = (
    re.compile(r"\bsolely\s+(?:due\s+to|because\s+of|on)\b", re.I),
    re.compile(r"\bvalue\s+increase\s+is\s+solely\b", re.I),
    re.compile(r"\b(?:was|were)\s+caused\s+by\b", re.I),
    re.compile(r"\bno\s+planning\s+ever\b", re.I),
    re.compile(r"\bnever\s+(?:applied|sought)\s+(?:for\s+)?planning\b", re.I),
    re.compile(r"\b(?:was\s+)?extended\s+(?:to|from|by)\b", re.I),
)
VISIBLE_STORY_DISCLAIMER_PATTERNS = (
    re.compile(r"\bnot proof\b", re.I),
    re.compile(r"\bdoes not establish\b", re.I),
    re.compile(r"\bcannot be treated\b", re.I),
    re.compile(r"\bcoverage is not\b", re.I),
    re.compile(r"\bnot available\b", re.I),
    re.compile(r"\bdoes not infer\b", re.I),
    re.compile(r"\bsource-match result\b", re.I),
)
DATE_RE = re.compile(r"^(?:19|20)\d{2}(?:-\d{2}(?:-\d{2})?)?$")
HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class PropertyRecordValidationError(ValueError):
    """Raised when the property-record publication contract is violated."""


def _is_iso_timestamp(value: Any) -> bool:
    text = str(value or "")
    if not re.fullmatch(
        r"(?:19|20)\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
        r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
        text,
    ):
        return False
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _is_iso_date(value: Any) -> bool:
    text = str(value or "")
    if not re.fullmatch(r"(?:19|20)\d{2}-\d{2}-\d{2}", text):
        return False
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _without_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_volatile(item)
            for key, item in value.items()
            if key != "fingerprint" and key not in VOLATILE_FINGERPRINT_FIELDS
        }
    if isinstance(value, list):
        return [_without_volatile(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def compute_record_fingerprint(record: dict[str, Any]) -> str:
    """Return the stable content digest required in ``record.fingerprint``."""

    payload = _canonical_json(_without_volatile(record)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def compute_dataset_fingerprint(records: dict[str, dict[str, Any]] | list[dict[str, Any]]) -> str:
    """Return a deterministic digest for the complete canonical record set."""

    normalised = normalise_records(records)
    payload = [
        [property_id, compute_record_fingerprint(record)]
        for property_id, record in sorted(normalised.items())
    ]
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _digest(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text.split(":", 1)[-1] if text.startswith("sha256:") else text


def normalise_records(records: Any) -> dict[str, dict[str, Any]]:
    if isinstance(records, dict):
        if not all(isinstance(value, dict) for value in records.values()):
            raise PropertyRecordValidationError("Property records must map ids to objects")
        return dict(records)
    if isinstance(records, list):
        result: dict[str, dict[str, Any]] = {}
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                raise PropertyRecordValidationError(f"Property record {index} is not an object")
            property_id = str(record.get("propertyId") or "")
            if not property_id or property_id in result:
                raise PropertyRecordValidationError("Property record ids must be present and unique")
            result[property_id] = record
        return result
    raise PropertyRecordValidationError("Property records must be an object or array")


def _json_schema_type_matches(value: Any, type_name: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(type_name, True)


def _resolve_local_schema_ref(root_schema: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise PropertyRecordValidationError(f"Unsupported external JSON Schema reference {reference!r}")
    node: Any = root_schema
    for token in reference[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or token not in node:
            raise PropertyRecordValidationError(f"Broken JSON Schema reference {reference!r}")
        node = node[token]
    if not isinstance(node, dict):
        raise PropertyRecordValidationError(f"JSON Schema reference {reference!r} does not resolve to an object")
    return node


def _validate_json_schema(value: Any, schema: dict[str, Any], root_schema: dict[str, Any], path: str) -> None:
    if "$ref" in schema:
        _validate_json_schema(value, _resolve_local_schema_ref(root_schema, str(schema["$ref"])), root_schema, path)
        return
    if "const" in schema and value != schema["const"]:
        raise PropertyRecordValidationError(f"{path}: value does not match JSON Schema const")
    if "enum" in schema and value not in schema["enum"]:
        raise PropertyRecordValidationError(f"{path}: value is outside the JSON Schema enum")
    allowed_types = schema.get("type")
    if allowed_types:
        type_names = [allowed_types] if isinstance(allowed_types, str) else list(allowed_types)
        if not any(_json_schema_type_matches(value, str(type_name)) for type_name in type_names):
            raise PropertyRecordValidationError(f"{path}: value does not match JSON Schema type {type_names}")

    if isinstance(value, dict):
        required = schema.get("required") or []
        missing = sorted(str(field) for field in required if field not in value)
        if missing:
            raise PropertyRecordValidationError(f"{path}: JSON Schema required fields are missing {missing}")
        minimum_properties = schema.get("minProperties")
        if isinstance(minimum_properties, int) and len(value) < minimum_properties:
            raise PropertyRecordValidationError(f"{path}: object has fewer than {minimum_properties} properties")
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        additional = schema.get("additionalProperties", True)
        for key, child in value.items():
            child_schema = properties.get(key)
            if child_schema is None:
                if additional is False:
                    raise PropertyRecordValidationError(f"{path}.{key}: additional property is not permitted")
                child_schema = additional if isinstance(additional, dict) else None
            if isinstance(child_schema, dict):
                _validate_json_schema(child, child_schema, root_schema, f"{path}.{key}")

    if isinstance(value, list):
        minimum_items = schema.get("minItems")
        maximum_items = schema.get("maxItems")
        if isinstance(minimum_items, int) and len(value) < minimum_items:
            raise PropertyRecordValidationError(f"{path}: array has fewer than {minimum_items} items")
        if isinstance(maximum_items, int) and len(value) > maximum_items:
            raise PropertyRecordValidationError(f"{path}: array has more than {maximum_items} items")
        if schema.get("uniqueItems") is True:
            serialised = [_canonical_json(item) for item in value]
            if len(serialised) != len(set(serialised)):
                raise PropertyRecordValidationError(f"{path}: array items are not unique")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, child in enumerate(value):
                _validate_json_schema(child, item_schema, root_schema, f"{path}[{index}]")

    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            raise PropertyRecordValidationError(f"{path}: string is shorter than {minimum_length}")
        pattern = schema.get("pattern")
        if pattern and not re.search(str(pattern), value):
            raise PropertyRecordValidationError(f"{path}: string does not match JSON Schema pattern")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise PropertyRecordValidationError(f"{path}: number is below JSON Schema minimum {minimum}")


def _coverage_entry(coverage: dict[str, Any], source: str) -> dict[str, Any]:
    for key in COVERAGE_ALIASES[source]:
        value = coverage.get(key)
        if value is not None:
            if not isinstance(value, dict):
                raise PropertyRecordValidationError(f"Coverage entry {key} must be an object")
            return value
    raise PropertyRecordValidationError(f"Missing {source} coverage entry")


def _limitations_text(entry: dict[str, Any]) -> str:
    limitations = entry.get("limitations")
    return " ".join(str(value) for value in limitations) if isinstance(limitations, list) else ""


def _validate_historic_england_contract(
    property_id: str,
    value: Any,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PropertyRecordValidationError(
            f"{property_id}: context historicEngland must be an object"
        )
    required = {
        "status",
        "entries",
        "source",
        "checkedAt",
        "sourceUpdatedAt",
        "sourceSnapshot",
    }
    allowed = set(required)
    missing = sorted(required - value.keys())
    extra = sorted(value.keys() - allowed)
    if missing:
        raise PropertyRecordValidationError(
            f"{property_id}: historicEngland is missing fields {missing}"
        )
    if extra:
        raise PropertyRecordValidationError(
            f"{property_id}: historicEngland contains unsupported fields {extra}"
        )

    status = str(value.get("status") or "")
    if status not in LISTED_BUILDING_STATUSES:
        raise PropertyRecordValidationError(
            f"{property_id}: historicEngland has invalid designation status {status!r}"
        )
    if value.get("source") != HISTORIC_ENGLAND_SOURCE:
        raise PropertyRecordValidationError(
            f"{property_id}: historicEngland source must be {HISTORIC_ENGLAND_SOURCE!r}"
        )
    for field in ("checkedAt", "sourceUpdatedAt", "sourceSnapshot"):
        if not str(value.get(field) or "").strip():
            raise PropertyRecordValidationError(
                f"{property_id}: historicEngland {field} is required"
            )
    for field in ("checkedAt", "sourceUpdatedAt"):
        if not _is_iso_timestamp(value[field]):
            raise PropertyRecordValidationError(
                f"{property_id}: historicEngland {field} must be a timezone-qualified ISO timestamp"
            )
    if not re.fullmatch(
        r"nhle-(?:19|20)\d{2}-\d{2}-\d{2}-[0-9a-f]{12}",
        str(value["sourceSnapshot"]),
    ):
        raise PropertyRecordValidationError(
            f"{property_id}: historicEngland sourceSnapshot is invalid"
        )

    entries = value.get("entries")
    if not isinstance(entries, list):
        raise PropertyRecordValidationError(
            f"{property_id}: historicEngland entries must be an array"
        )
    if status in {"confirmed_listed", "candidate_review"} and not entries:
        raise PropertyRecordValidationError(
            f"{property_id}: {status} requires at least one NHLE entry"
        )
    if status in {"no_direct_match", "unknown"} and entries:
        raise PropertyRecordValidationError(
            f"{property_id}: {status} cannot contain NHLE entries"
        )

    seen_entry_numbers: set[str] = set()
    required_entry_fields = {
        "listEntryNumber",
        "grade",
        "name",
        "url",
        "matchMethod",
        "matchConfidence",
    }
    optional_entry_fields = {"listDate", "amendDate", "distanceMetres"}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise PropertyRecordValidationError(
                f"{property_id}: historicEngland entry {index} is not an object"
            )
        missing_entry = sorted(required_entry_fields - entry.keys())
        extra_entry = sorted(entry.keys() - required_entry_fields - optional_entry_fields)
        if missing_entry:
            raise PropertyRecordValidationError(
                f"{property_id}: historicEngland entry {index} is missing fields {missing_entry}"
            )
        if extra_entry:
            raise PropertyRecordValidationError(
                f"{property_id}: historicEngland entry {index} contains unsupported fields {extra_entry}"
            )
        list_entry_number = str(entry.get("listEntryNumber") or "")
        if not re.fullmatch(r"\d{7}", list_entry_number):
            raise PropertyRecordValidationError(
                f"{property_id}: historicEngland entry {index} has an invalid seven-digit List Entry Number"
            )
        if list_entry_number in seen_entry_numbers:
            raise PropertyRecordValidationError(
                f"{property_id}: historicEngland entries repeat List Entry Number {list_entry_number}"
            )
        seen_entry_numbers.add(list_entry_number)
        if entry.get("grade") not in LISTED_BUILDING_GRADES:
            raise PropertyRecordValidationError(
                f"{property_id}: historicEngland entry {list_entry_number} has an invalid grade"
            )
        if not str(entry.get("name") or "").strip():
            raise PropertyRecordValidationError(
                f"{property_id}: historicEngland entry {list_entry_number} has no official name"
            )
        expected_url = (
            "https://historicengland.org.uk/listing/the-list/list-entry/"
            f"{list_entry_number}"
        )
        if entry.get("url") != expected_url:
            raise PropertyRecordValidationError(
                f"{property_id}: historicEngland entry {list_entry_number} has a non-official or mismatched URL"
            )
        match_method = entry.get("matchMethod")
        match_confidence = entry.get("matchConfidence")
        if match_method not in LISTED_BUILDING_MATCH_METHODS:
            raise PropertyRecordValidationError(
                f"{property_id}: historicEngland entry {list_entry_number} has an invalid match method"
            )
        if match_confidence not in LISTED_BUILDING_MATCH_CONFIDENCE:
            raise PropertyRecordValidationError(
                f"{property_id}: historicEngland entry {list_entry_number} has invalid match confidence"
            )
        if status == "confirmed_listed" and (
            match_confidence != "confirmed"
            or match_method not in CONFIRMED_LISTED_BUILDING_MATCH_METHODS
        ):
            raise PropertyRecordValidationError(
                f"{property_id}: confirmed listing requires confirmed reviewed or genuine-polygon evidence"
            )
        if status == "candidate_review" and (
            match_confidence != "review_required"
            or match_method != "nearby_nhle_point"
        ):
            raise PropertyRecordValidationError(
                f"{property_id}: candidate listing evidence must remain a nearby review-required match"
            )
        for field in ("listDate", "amendDate"):
            if field in entry and not _is_iso_date(entry.get(field)):
                raise PropertyRecordValidationError(
                    f"{property_id}: historicEngland entry {list_entry_number} has an invalid {field}"
                )
        if "distanceMetres" in entry and (
            type(entry["distanceMetres"]) is not int
            or entry["distanceMetres"] < 0
        ):
            raise PropertyRecordValidationError(
                f"{property_id}: historicEngland entry {list_entry_number} has invalid distanceMetres"
            )
    return value


def _validate_listed_building_record(
    property_id: str,
    context: dict[str, Any],
    coverage: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> None:
    coverage_entry = _coverage_entry(coverage, "listedBuilding")
    historic_england = context.get("historicEngland")
    designation_evidence = [
        item
        for item in evidence
        if isinstance(item, dict) and item.get("type") == "listed_building"
    ]
    if historic_england is None:
        if (
            coverage_entry.get("designationStatus") != "unknown"
            or coverage_entry.get("status") != "unavailable"
            or coverage_entry.get("recordCount") != 0
        ):
            raise PropertyRecordValidationError(
                f"{property_id}: missing historicEngland context must remain explicit unknown coverage"
            )
        if designation_evidence:
            raise PropertyRecordValidationError(
                f"{property_id}: listed-building evidence exists without historicEngland context"
            )
        return

    contract = _validate_historic_england_contract(property_id, historic_england)
    entries = contract["entries"]
    if coverage_entry.get("designationStatus") != contract["status"]:
        raise PropertyRecordValidationError(
            f"{property_id}: listed-building coverage status does not match historicEngland context"
        )
    if coverage_entry.get("recordCount") != len(entries):
        raise PropertyRecordValidationError(
            f"{property_id}: listed-building coverage count does not match all NHLE entries"
        )
    for field in ("source", "checkedAt", "sourceUpdatedAt", "sourceSnapshot"):
        if coverage_entry.get(field) != contract.get(field):
            raise PropertyRecordValidationError(
                f"{property_id}: listed-building coverage {field} does not match historicEngland context"
            )
    if len(designation_evidence) != 1:
        raise PropertyRecordValidationError(
            f"{property_id}: historicEngland context requires exactly one listed_building evidence record"
        )
    item = designation_evidence[0]
    if (
        item.get("source") != HISTORIC_ENGLAND_SOURCE
        or item.get("sourceId") != contract["sourceSnapshot"]
        or item.get("effectiveDate") != str(contract["sourceUpdatedAt"])[:10]
        or item.get("data") != contract
    ):
        raise PropertyRecordValidationError(
            f"{property_id}: listed_building evidence does not preserve source, date, snapshot and designation data"
        )
    if str(item.get("evidenceId") or "") not in set(_evidence_ids(coverage_entry)):
        raise PropertyRecordValidationError(
            f"{property_id}: listed-building coverage does not link its designation evidence"
        )


def _validate_background_coverage_semantics(
    property_id: str,
    source: str,
    entry: dict[str, Any],
) -> None:
    """Enforce the evidential meaning of each public-context coverage state."""

    status = str(entry.get("status") or "")
    record_count = entry.get("recordCount")
    limitations = _limitations_text(entry).casefold()

    if source == "coordinates":
        if status == "complete" and (
            record_count != 1
            or not isinstance(entry.get("coordinateIsExactProperty"), bool)
            or not str(entry.get("precision") or "").strip()
        ):
            raise PropertyRecordValidationError(
                f"{property_id}: complete coordinate coverage requires one point, declared precision and an exactness flag"
            )
        if entry.get("coverageMode") == "confirmed-nhle-designation-location":
            if (
                entry.get("source") != HISTORIC_ENGLAND_SOURCE
                or entry.get("precision") != "confirmed-nhle-designation-location"
                or entry.get("coordinateIsExactProperty") is not False
                or "designation point" not in limitations
                or "not exact-property" not in limitations
                or "curtilage" not in limitations
            ):
                raise PropertyRecordValidationError(
                    f"{property_id}: NHLE-refined coordinate coverage lacks its designation-point limitations"
                )
        elif entry.get("coordinateIsExactProperty") is False and not (
            "postcode centroid" in limitations and "not exact-property" in limitations
        ):
            raise PropertyRecordValidationError(
                f"{property_id}: approximate coordinate coverage lacks the postcode-centroid exactness limitation"
            )

    elif source == "currentFlood":
        if status == "complete" and record_count != 1:
            raise PropertyRecordValidationError(
                f"{property_id}: complete current-flood coverage requires one dated observation"
            )
        if status == "checked_none":
            raise PropertyRecordValidationError(
                f"{property_id}: current flood-alert coverage must retain a zero-alert observation, not checked_none"
            )
        if "not a long-term property flood-risk assessment" not in limitations:
            raise PropertyRecordValidationError(
                f"{property_id}: current flood coverage must distinguish alerts from long-term property risk"
            )

    elif source == "planningConstraints":
        if status == "checked_none" and (
            entry.get("lookupStatus") != "successful"
            or entry.get("resultStatus") != "no_mapped_constraints"
            or record_count != 0
        ):
            raise PropertyRecordValidationError(
                f"{property_id}: static-constraint checked_none requires explicit successful zero-result evidence"
            )
        if status == "complete" and (entry.get("lookupStatus") != "successful" or record_count < 1):
            raise PropertyRecordValidationError(
                f"{property_id}: complete static-constraint coverage requires an explicit successful positive result"
            )
        if "not proof" not in limitations or "postcode centroid" not in limitations:
            raise PropertyRecordValidationError(
                f"{property_id}: static-constraint coverage lacks zero-result or postcode-centroid limitations"
            )

    elif source == "listedBuilding":
        designation_status = str(entry.get("designationStatus") or "")
        if designation_status not in LISTED_BUILDING_STATUSES:
            raise PropertyRecordValidationError(
                f"{property_id}: listed-building coverage has an invalid designation state"
            )
        expected = {
            "confirmed_listed": ("complete", True),
            "candidate_review": ("complete", True),
            "no_direct_match": ("checked_none", True),
            "unknown": ("unavailable", False),
        }[designation_status]
        if status != expected[0] or entry.get("complete") is not expected[1]:
            raise PropertyRecordValidationError(
                f"{property_id}: listed-building coverage does not reconcile to {designation_status}"
            )
        if entry.get("resultStatus") != designation_status:
            raise PropertyRecordValidationError(
                f"{property_id}: listed-building resultStatus must mirror designationStatus"
            )
        if designation_status in {"confirmed_listed", "candidate_review"} and record_count < 1:
            raise PropertyRecordValidationError(
                f"{property_id}: positive/candidate listed-building coverage requires retained NHLE entries"
            )
        if designation_status in {"no_direct_match", "unknown"} and record_count != 0:
            raise PropertyRecordValidationError(
                f"{property_id}: no-direct/unknown listed-building coverage cannot retain entries"
            )
        if entry.get("source") != HISTORIC_ENGLAND_SOURCE:
            raise PropertyRecordValidationError(
                f"{property_id}: listed-building coverage must cite Historic England NHLE"
            )
        if (
            "not proof" not in limitations
            or "postcode centroid" not in limitations
            or "curtilage" not in limitations
            or "local planning authority" not in limitations
        ):
            raise PropertyRecordValidationError(
                f"{property_id}: listed-building coverage lacks no-match, postcode-centroid, curtilage or legal-verification limitations"
            )

    elif source == "schools":
        if status == "checked_none" and record_count != 0:
            raise PropertyRecordValidationError(
                f"{property_id}: school checked_none coverage must have zero retained records"
            )
        if status == "complete" and record_count < 1:
            raise PropertyRecordValidationError(
                f"{property_id}: complete school coverage must contain a nearby-school result"
            )
        if "postcode-centroid" not in limitations:
            raise PropertyRecordValidationError(
                f"{property_id}: school coverage lacks the approximate-distance limitation"
            )

    elif source == "osUprn":
        result_status = str(entry.get("resultStatus") or "")
        if status == "complete" and result_status not in {
            "confirmed_address_match",
            "nearest_candidate_unconfirmed",
            "no_candidate_within_radius",
        }:
            raise PropertyRecordValidationError(
                f"{property_id}: complete OS UPRN coverage has an invalid candidate result state"
            )
        if result_status == "nearest_candidate_unconfirmed" and (
            record_count != 1 or entry.get("confirmedAddressMatch") is not False
        ):
            raise PropertyRecordValidationError(
                f"{property_id}: nearest OS UPRN candidate must remain explicitly unconfirmed"
            )
        if result_status == "no_candidate_within_radius" and record_count != 0:
            raise PropertyRecordValidationError(
                f"{property_id}: OS no-candidate result must have zero records"
            )
        if "never controls canonical property identity" not in limitations:
            raise PropertyRecordValidationError(
                f"{property_id}: OS UPRN coverage lacks the identity limitation"
            )


def _evidence_ids(value: dict[str, Any]) -> list[str]:
    values: Iterable[Any]
    if isinstance(value.get("evidenceIds"), list):
        values = value["evidenceIds"]
    elif value.get("evidenceId"):
        values = [value["evidenceId"]]
    else:
        values = []
    return [str(item) for item in values if str(item).strip()]


def _date_sort_key(value: Any) -> str:
    """Normalise supported date precision to the generator's comparison day."""

    text = str(value or "").strip()[:10]
    if re.fullmatch(r"(?:19|20)\d{2}-\d{2}-\d{2}", text):
        return text
    if re.fullmatch(r"(?:19|20)\d{2}-\d{2}", text):
        return f"{text}-15"
    if re.fullmatch(r"(?:19|20)\d{2}", text):
        return f"{text}-07-01"
    return ""


def _valuation_round(value: float) -> int:
    if value < 2_000_000:
        step = 25_000
    elif value < 5_000_000:
        step = 50_000
    elif value < 10_000_000:
        step = 100_000
    elif value < 20_000_000:
        step = 250_000
    else:
        step = 500_000
    return int(round(value / step) * step)


def _validate_valuation(
    property_id: str,
    valuation: Any,
    evidence: list[dict[str, Any]],
    expected_universe: tuple[int, str, int] | None,
    expected_as_of: str,
) -> None:
    if not isinstance(valuation, dict):
        raise PropertyRecordValidationError(f"{property_id}: story valuation must be an object")

    required = {
        "modelVersion", "cpihVersion", "asOf", "baseEstimate", "estimatedCurrentValue",
        "planningAdjustment", "confidence", "targetFloorAreaSqft", "targetPropertyType",
        "ownSaleAnchor", "ownSaleDate", "ownSalePrice", "comparableCentre",
        "comparableMedianPricePerSqft", "comparableCount", "selectedComparableCount",
        "comparableChannel", "comparableScope", "comparableWindowYears",
        "effectiveSampleSize", "targetAreaStaleAfterPlanning", "nearFloorPriceFallback",
        "splitSignal", "transactionUniverseCount", "transactionUniverseLatestSaleDate",
        "transactionUniversePriceTotal", "comparables",
    }
    missing = sorted(required - valuation.keys())
    if missing:
        raise PropertyRecordValidationError(f"{property_id}: valuation is missing fields {missing}")

    integer_fields = (
        "baseEstimate", "estimatedCurrentValue", "planningAdjustment", "comparableCount",
        "selectedComparableCount", "comparableWindowYears", "transactionUniverseCount",
        "transactionUniversePriceTotal",
    )
    if any(isinstance(valuation.get(field), bool) or not isinstance(valuation.get(field), int) for field in integer_fields):
        raise PropertyRecordValidationError(f"{property_id}: valuation integer fields are invalid")
    if valuation["baseEstimate"] <= 0 or valuation["estimatedCurrentValue"] <= 0:
        raise PropertyRecordValidationError(f"{property_id}: valuation estimates must be positive")
    if valuation["planningAdjustment"] < 0:
        raise PropertyRecordValidationError(f"{property_id}: planning adjustment cannot be negative")
    if valuation["transactionUniverseCount"] <= 0 or valuation["transactionUniversePriceTotal"] <= 0:
        raise PropertyRecordValidationError(f"{property_id}: valuation transaction universe is invalid")
    if valuation.get("confidence") not in {"low", "medium", "high"}:
        raise PropertyRecordValidationError(f"{property_id}: valuation confidence is invalid")
    if valuation.get("comparableChannel") not in {"price-per-sq-ft", "absolute-price"}:
        raise PropertyRecordValidationError(f"{property_id}: valuation comparable channel is invalid")
    if valuation.get("comparableScope") not in {"estate", "town", "market", "surrey-prime"}:
        raise PropertyRecordValidationError(f"{property_id}: valuation comparable scope is invalid")
    if valuation.get("comparableWindowYears") not in {5, 7, 10, 12}:
        raise PropertyRecordValidationError(f"{property_id}: valuation comparable window is invalid")
    for field in ("targetAreaStaleAfterPlanning", "nearFloorPriceFallback", "splitSignal"):
        if not isinstance(valuation.get(field), bool):
            raise PropertyRecordValidationError(f"{property_id}: valuation {field} must be boolean")
    for field in ("targetFloorAreaSqft", "ownSaleAnchor", "ownSalePrice", "comparableCentre", "comparableMedianPricePerSqft"):
        if valuation.get(field) is not None and (
            isinstance(valuation.get(field), bool) or not isinstance(valuation.get(field), int) or valuation[field] < 0
        ):
            raise PropertyRecordValidationError(f"{property_id}: valuation {field} is invalid")
    if not isinstance(valuation.get("targetPropertyType"), (str, type(None))):
        raise PropertyRecordValidationError(f"{property_id}: valuation targetPropertyType is invalid")
    if isinstance(valuation.get("effectiveSampleSize"), bool) or not isinstance(valuation.get("effectiveSampleSize"), (int, float)) or valuation["effectiveSampleSize"] < 0:
        raise PropertyRecordValidationError(f"{property_id}: valuation effective sample size is invalid")
    for field in ("ownSaleDate", "transactionUniverseLatestSaleDate"):
        if not re.fullmatch(r"(?:19|20)\d{2}-\d{2}-\d{2}", str(valuation.get(field) or "")):
            raise PropertyRecordValidationError(f"{property_id}: valuation {field} is not a full ISO date")
    if str(valuation.get("asOf") or "")[:10] != str(expected_as_of or "")[:10]:
        raise PropertyRecordValidationError(f"{property_id}: valuation date does not match the record snapshot")

    comparables = valuation.get("comparables")
    if not isinstance(comparables, list):
        raise PropertyRecordValidationError(f"{property_id}: valuation comparables must be an array")
    if valuation["selectedComparableCount"] != len(comparables):
        raise PropertyRecordValidationError(f"{property_id}: selectedComparableCount does not match the comparable cohort")
    used = []
    comparable_required = {
        "propertyId", "transactionId", "category", "date", "price", "floorAreaSqft", "score",
        "adjustedPrice", "adjustedPricePerSqft", "trustedPricePerSqft", "areaStaleAtSale",
        "ageYears", "estateId", "town", "market", "usedInChannel",
    }
    for index, comparable in enumerate(comparables):
        if not isinstance(comparable, dict):
            raise PropertyRecordValidationError(f"{property_id}: comparable {index} is not an object")
        if comparable.get("category") != "A":
            raise PropertyRecordValidationError(f"{property_id}: every comparable must be an HM Land Registry Category A sale")
        missing_comparable = sorted(comparable_required - comparable.keys())
        if missing_comparable:
            raise PropertyRecordValidationError(
                f"{property_id}: comparable {index} is missing schema fields {missing_comparable}"
            )
        if not str(comparable.get("propertyId") or "").strip():
            raise PropertyRecordValidationError(f"{property_id}: comparable {index} has no property identity")
        if not re.fullmatch(r"(?:19|20)\d{2}-\d{2}-\d{2}", str(comparable.get("date") or "")):
            raise PropertyRecordValidationError(f"{property_id}: comparable {index} has an invalid sale date")
        if not isinstance(comparable.get("trustedPricePerSqft"), bool) or not isinstance(comparable.get("areaStaleAtSale"), bool):
            raise PropertyRecordValidationError(f"{property_id}: comparable {index} has invalid EPC audit flags")
        if not isinstance(comparable.get("usedInChannel"), bool):
            raise PropertyRecordValidationError(f"{property_id}: comparable {index} has no channel-usage flag")
        if comparable["usedInChannel"]:
            used.append(comparable)
            if valuation["comparableChannel"] == "price-per-sq-ft" and (
                comparable.get("trustedPricePerSqft") is not True
                or comparable.get("areaStaleAtSale") is not False
                or not isinstance(comparable.get("adjustedPricePerSqft"), int)
                or comparable.get("adjustedPricePerSqft", 0) <= 0
            ):
                raise PropertyRecordValidationError(
                    f"{property_id}: price-per-square-foot channel used an untrusted or stale EPC denominator"
                )
    if valuation["comparableCount"] != len(used):
        raise PropertyRecordValidationError(f"{property_id}: comparableCount does not match rows used by the chosen channel")

    expected_final = _valuation_round(valuation["baseEstimate"] + valuation["planningAdjustment"])
    if valuation["estimatedCurrentValue"] != expected_final:
        raise PropertyRecordValidationError(f"{property_id}: final value does not reconcile to base plus planning adjustment")
    cap = valuation["baseEstimate"] * 0.25
    cap_rounding_allowance = abs(_valuation_round(cap) - cap)
    if valuation["planningAdjustment"] > cap + cap_rounding_allowance:
        raise PropertyRecordValidationError(f"{property_id}: planning adjustment exceeds the 25% model cap")

    sale_evidence = [
        item for item in evidence
        if isinstance(item, dict) and item.get("type") == "sale" and isinstance(item.get("data"), dict)
    ]
    latest_sale = max(
        sale_evidence,
        key=lambda item: (
            _date_sort_key(item["data"].get("date") or item.get("effectiveDate")),
            int(item["data"].get("price") or 0),
            str(item["data"].get("sourceTransactionId") or item.get("sourceId") or ""),
        ),
        default=None,
    )
    if not latest_sale:
        raise PropertyRecordValidationError(f"{property_id}: valuation has no supporting sale evidence")
    latest_sale_data = latest_sale["data"]
    if (
        str(valuation.get("ownSaleDate") or "")[:10] != str(latest_sale_data.get("date") or "")[:10]
        or int(valuation.get("ownSalePrice") or 0) != int(latest_sale_data.get("price") or 0)
    ):
        raise PropertyRecordValidationError(f"{property_id}: own-sale valuation anchor does not match latest sale evidence")

    derived = [item for item in evidence if isinstance(item, dict) and item.get("type") == "derived_valuation"]
    if len(derived) != 1 or not isinstance(derived[0].get("data"), dict):
        raise PropertyRecordValidationError(f"{property_id}: valuation must have exactly one derived evidence record")
    for field in ("modelVersion", "asOf", "baseEstimate", "planningAdjustment", "estimatedCurrentValue"):
        if derived[0]["data"].get(field) != valuation.get(field):
            raise PropertyRecordValidationError(f"{property_id}: valuation evidence is stale for {field}")

    planning_signals = valuation.get("planningSignals")
    if valuation["planningAdjustment"] > 0:
        if not isinstance(planning_signals, dict) or int(planning_signals.get("postEvidenceApprovedSchemeCount") or 0) < 1:
            raise PropertyRecordValidationError(f"{property_id}: planning uplift lacks an approved post-evidence scheme")
        references = [str(value) for value in planning_signals.get("references", []) if str(value).strip()]
        if not references:
            raise PropertyRecordValidationError(f"{property_id}: planning uplift lacks auditable scheme references")
        latest_epc_date = max(
            (
                _date_sort_key((item.get("data") or {}).get("date") or item.get("effectiveDate"))
                for item in evidence
                if isinstance(item, dict) and item.get("type") == "epc_certificate"
            ),
            default="",
        )
        evidence_cutoff = max(_date_sort_key(latest_sale_data.get("date")), latest_epc_date)
        planning_by_reference = {
            str((item.get("data") or {}).get("reference") or (item.get("data") or {}).get("signature") or ""): item
            for item in evidence
            if isinstance(item, dict) and item.get("type") == "planning_application" and isinstance(item.get("data"), dict)
        }
        for reference in references:
            application = planning_by_reference.get(reference)
            application_date = _date_sort_key(
                ((application or {}).get("data") or {}).get("date") or (application or {}).get("effectiveDate")
            )
            if not application or not application_date or application_date <= evidence_cutoff:
                raise PropertyRecordValidationError(
                    f"{property_id}: planning uplift reference {reference!r} is not later than both latest sale and EPC evidence"
                )
    elif planning_signals:
        raise PropertyRecordValidationError(f"{property_id}: planning signals cannot exist without a planning adjustment")

    if expected_universe and (
        valuation["transactionUniverseCount"],
        str(valuation.get("transactionUniverseLatestSaleDate") or "")[:10],
        valuation["transactionUniversePriceTotal"],
    ) != expected_universe:
        raise PropertyRecordValidationError(f"{property_id}: valuation transaction-universe signature is stale")


def _validate_story(
    property_id: str,
    story: Any,
    available_evidence: set[str],
) -> None:
    if not isinstance(story, dict):
        raise PropertyRecordValidationError(f"{property_id}: story must be an object")
    text = str(story.get("text") or "").strip()
    if not text:
        raise PropertyRecordValidationError(f"{property_id}: story text is empty")
    paragraphs = story.get("paragraphs")
    if not isinstance(paragraphs, list) or not paragraphs or any(not str(item).strip() for item in paragraphs):
        raise PropertyRecordValidationError(f"{property_id}: story must contain non-empty paragraphs")
    if len(paragraphs) != 2:
        raise PropertyRecordValidationError(f"{property_id}: story must contain a history paragraph and a valuation paragraph")
    if not re.search(r"Estimated current value\s*[—-]\s*£", str(paragraphs[-1]), re.I):
        raise PropertyRecordValidationError(f"{property_id}: final story paragraph must state the estimated current value")
    valuation = story.get("valuation")
    if not isinstance(valuation, dict) or not isinstance(valuation.get("estimatedCurrentValue"), int) or valuation["estimatedCurrentValue"] <= 0:
        raise PropertyRecordValidationError(f"{property_id}: story valuation must contain a positive integer estimate")
    if not str(story.get("generator") or "").strip():
        raise PropertyRecordValidationError(f"{property_id}: story generator/version is missing")
    if not isinstance(story.get("limitations"), list):
        raise PropertyRecordValidationError(f"{property_id}: story limitations must be an array")
    for pattern in BANNED_CAUSAL_PATTERNS:
        if pattern.search(text):
            raise PropertyRecordValidationError(
                f"{property_id}: story contains unsupported causal/absolute language: {pattern.pattern}"
            )
    for pattern in VISIBLE_STORY_DISCLAIMER_PATTERNS:
        if pattern.search(text):
            raise PropertyRecordValidationError(
                f"{property_id}: visible story contains disclaimer copy: {pattern.pattern}"
            )

    claims = story.get("claims")
    if not isinstance(claims, list) or not claims:
        raise PropertyRecordValidationError(f"{property_id}: story must contain evidence-backed claims")
    claim_ids: set[str] = set()
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict):
            raise PropertyRecordValidationError(f"{property_id}: story claim {index} is not an object")
        claim_id = str(claim.get("claimId") or "")
        if not claim_id or claim_id in claim_ids:
            raise PropertyRecordValidationError(f"{property_id}: claim ids must be present and unique")
        claim_ids.add(claim_id)
        if not str(claim.get("text") or "").strip():
            raise PropertyRecordValidationError(f"{property_id}: claim {claim_id} has no text")
        evidence_ids = _evidence_ids(claim)
        if not evidence_ids:
            raise PropertyRecordValidationError(f"{property_id}: claim {claim_id} has no evidence ids")
        missing = sorted(set(evidence_ids) - available_evidence)
        if missing:
            raise PropertyRecordValidationError(
                f"{property_id}: claim {claim_id} references unknown evidence ids {missing}"
            )
        confidence = claim.get("confidence")
        if confidence is None or isinstance(confidence, bool):
            raise PropertyRecordValidationError(f"{property_id}: claim {claim_id} has no confidence")


def validate_property_records(
    records: Any,
    meta: Any,
    transactions: list[dict[str, Any]] | None = None,
    *,
    allow_incomplete: bool = False,
) -> dict[str, Any]:
    """Validate records and return recomputed counts for diagnostics/tests."""

    normalised = normalise_records(records)
    if not normalised:
        raise PropertyRecordValidationError("Property record feed must not be empty")
    schema = json.loads(PROPERTY_RECORD_SCHEMA.read_text(encoding="utf-8"))
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise PropertyRecordValidationError("Property-record JSON Schema must use draft 2020-12")
    if not isinstance(meta, dict):
        raise PropertyRecordValidationError("Property record metadata must be an object")
    if meta.get("schemaVersion") != SCHEMA_VERSION:
        raise PropertyRecordValidationError(f"Property record schemaVersion must be {SCHEMA_VERSION}")
    for field in ("generatedAt", "asOf"):
        if not str(meta.get(field) or "").strip():
            raise PropertyRecordValidationError(f"Property record metadata is missing {field}")

    property_id_for_transaction = None
    expected_universe: tuple[int, str, int] | None = None
    if transactions is not None:
        from insight_data_utils import property_record_id

        property_id_for_transaction = property_record_id
        latest_by_property: dict[str, dict[str, Any]] = {}
        for item in transactions:
            candidate_property_id = property_record_id(item)
            current = latest_by_property.get(candidate_property_id)
            candidate_key = (
                str(item.get("date") or "")[:10],
                str(item.get("id") or ""),
            )
            current_key = (
                str((current or {}).get("date") or "")[:10],
                str((current or {}).get("id") or ""),
            )
            if current is None or candidate_key > current_key:
                latest_by_property[candidate_property_id] = item
        expected_universe = (
            len(latest_by_property),
            max((str(item.get("date") or "")[:10] for item in latest_by_property.values()), default=""),
            sum(int(item.get("price") or 0) for item in latest_by_property.values()),
        )

    all_transaction_ids: list[str] = []
    global_event_ids: set[str] = set()
    narrative_count = 0
    ready_narrative_count = 0
    event_count = 0
    coverage_counts: dict[str, Counter[str]] = {
        source: Counter() for source in COVERAGE_ALIASES
    }
    listed_building_status_counts: Counter[str] = Counter()
    validated_fingerprints: list[list[str]] = []

    for key, record in sorted(normalised.items()):
        property_id = str(record.get("propertyId") or "")
        if not property_id or property_id != str(key):
            raise PropertyRecordValidationError(f"Record key {key!r} does not match propertyId {property_id!r}")
        if record.get("schemaVersion") != SCHEMA_VERSION:
            raise PropertyRecordValidationError(f"{property_id}: schemaVersion must be {SCHEMA_VERSION}")
        if not record.get("recordVersion"):
            raise PropertyRecordValidationError(f"{property_id}: recordVersion is missing")
        if not str(record.get("createdAt") or "").strip() or not str(record.get("updatedAt") or "").strip():
            raise PropertyRecordValidationError(f"{property_id}: createdAt/updatedAt are required")
        if not str(record.get("canonicalAddress") or "").strip() or not str(record.get("postcode") or "").strip():
            raise PropertyRecordValidationError(f"{property_id}: canonical address and postcode are required")

        transaction_ids = record.get("transactionIds")
        if not isinstance(transaction_ids, list) or not transaction_ids:
            raise PropertyRecordValidationError(f"{property_id}: transactionIds must be a non-empty array")
        transaction_ids = [str(value) for value in transaction_ids]
        if any(not value for value in transaction_ids) or len(transaction_ids) != len(set(transaction_ids)):
            raise PropertyRecordValidationError(f"{property_id}: transaction ids must be present and unique")
        all_transaction_ids.extend(transaction_ids)

        evidence = record.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise PropertyRecordValidationError(f"{property_id}: evidence ledger must be a non-empty array")
        evidence_ids: set[str] = set()
        for index, item in enumerate(evidence):
            if not isinstance(item, dict):
                raise PropertyRecordValidationError(f"{property_id}: evidence {index} is not an object")
            evidence_id = str(item.get("evidenceId") or "")
            if not evidence_id or evidence_id in evidence_ids:
                raise PropertyRecordValidationError(f"{property_id}: evidence ids must be present and unique")
            evidence_ids.add(evidence_id)
            if not str(item.get("source") or "").strip():
                raise PropertyRecordValidationError(f"{property_id}: evidence {evidence_id} has no source")

        events = record.get("events")
        if not isinstance(events, list) or not events:
            raise PropertyRecordValidationError(f"{property_id}: events must be a non-empty array")
        event_sort_keys: list[tuple[str, str, str]] = []
        local_event_types: Counter[str] = Counter()
        for index, event in enumerate(events):
            if not isinstance(event, dict):
                raise PropertyRecordValidationError(f"{property_id}: event {index} is not an object")
            event_id = str(event.get("eventId") or "")
            if not event_id or event_id in global_event_ids:
                raise PropertyRecordValidationError(f"{property_id}: event ids must be globally unique")
            global_event_ids.add(event_id)
            if str(event.get("propertyId") or "") != property_id:
                raise PropertyRecordValidationError(f"{property_id}: event {event_id} has the wrong propertyId")
            event_type = str(event.get("type") or "")
            if event_type not in EVENT_TYPES:
                raise PropertyRecordValidationError(f"{property_id}: event {event_id} has unsupported type {event_type!r}")
            local_event_types[event_type] += 1
            date = str(event.get("date") or "")
            if date and not DATE_RE.fullmatch(date):
                raise PropertyRecordValidationError(f"{property_id}: event {event_id} has invalid date {date!r}")
            if not date and event.get("datePrecision") != "unknown":
                raise PropertyRecordValidationError(f"{property_id}: an undated event must declare unknown date precision")
            if not str(event.get("source") or "").strip():
                raise PropertyRecordValidationError(f"{property_id}: event {event_id} has no source")
            linked_evidence = _evidence_ids(event)
            if not linked_evidence:
                raise PropertyRecordValidationError(f"{property_id}: event {event_id} has no evidence ids")
            missing = sorted(set(linked_evidence) - evidence_ids)
            if missing:
                raise PropertyRecordValidationError(
                    f"{property_id}: event {event_id} references unknown evidence ids {missing}"
                )
            event_sort_keys.append((date or "9999", event_type, event_id))
        if event_sort_keys != sorted(event_sort_keys):
            raise PropertyRecordValidationError(f"{property_id}: events are not in deterministic chronological order")
        if local_event_types["sale"] < len(transaction_ids):
            raise PropertyRecordValidationError(f"{property_id}: each transaction must have a sale event")
        event_count += len(events)

        coverage = record.get("coverage")
        if not isinstance(coverage, dict):
            raise PropertyRecordValidationError(f"{property_id}: coverage must be an object")
        for source in COVERAGE_ALIASES:
            entry = _coverage_entry(coverage, source)
            status = str(entry.get("status") or "")
            if status not in COVERAGE_STATUSES:
                raise PropertyRecordValidationError(f"{property_id}: invalid {source} coverage status {status!r}")
            coverage_counts[source][status] += 1
            required_coverage_fields = {
                "complete",
                "coverageMode",
                "source",
                "checkedAt",
                "coverageFrom",
                "coverageTo",
                "recordCount",
                "evidenceIds",
                "basis",
                "limitations",
            }
            missing_coverage_fields = sorted(required_coverage_fields - entry.keys())
            if missing_coverage_fields:
                raise PropertyRecordValidationError(
                    f"{property_id}: {source} coverage is missing fields {missing_coverage_fields}"
                )
            if not str(entry.get("source") or "").strip() or not str(entry.get("basis") or "").strip():
                raise PropertyRecordValidationError(f"{property_id}: {source} coverage lacks source/basis")
            if not isinstance(entry.get("limitations"), list):
                raise PropertyRecordValidationError(f"{property_id}: {source} coverage limitations must be an array")
            if not isinstance(entry.get("recordCount"), int) or entry["recordCount"] < 0:
                raise PropertyRecordValidationError(f"{property_id}: {source} coverage recordCount is invalid")
            linked_evidence = _evidence_ids(entry)
            if not linked_evidence:
                raise PropertyRecordValidationError(f"{property_id}: {source} coverage has no evidence ids")
            missing = sorted(set(linked_evidence) - evidence_ids)
            if missing:
                raise PropertyRecordValidationError(
                    f"{property_id}: {source} coverage references unknown evidence ids {missing}"
                )
            if not allow_incomplete and status in INCOMPLETE_COVERAGE_STATUSES:
                raise PropertyRecordValidationError(f"{property_id}: {source} coverage remains {status}")
            if source == "planning" and status == "checked_none":
                if (
                    entry.get("coverageMode") != "full-available-history"
                    or entry.get("complete") is not True
                    or entry.get("recordCount") != 0
                    or local_event_types["planning_application"]
                ):
                    raise PropertyRecordValidationError(
                        f"{property_id}: planning checked_none requires complete full-available-history coverage"
                    )
            if status in {"complete", "checked_none"} and entry.get("complete") is not True:
                raise PropertyRecordValidationError(f"{property_id}: {source} {status} coverage must be complete")
            if status in INCOMPLETE_COVERAGE_STATUSES | {"unavailable"} and entry.get("complete") is not False:
                raise PropertyRecordValidationError(f"{property_id}: {source} {status} coverage cannot be complete")
            if status == "checked_none" and entry.get("recordCount") != 0:
                raise PropertyRecordValidationError(
                    f"{property_id}: {source} checked_none coverage must have zero records"
                )
            if source not in EVENT_COVERAGE_TYPES:
                _validate_background_coverage_semantics(property_id, source, entry)
            expected_event = EVENT_COVERAGE_TYPES.get(source)
            if expected_event and status == "complete" and entry.get("recordCount", 0) > 0 and not local_event_types[expected_event]:
                raise PropertyRecordValidationError(
                    f"{property_id}: complete {source} coverage has no {expected_event} event"
                )

        context = record.get("context")
        if not isinstance(context, dict):
            raise PropertyRecordValidationError(
                f"{property_id}: context must be an object"
            )
        if {"openStreetMap", "companiesHouse"} & set(context):
            raise PropertyRecordValidationError(
                f"{property_id}: restricted or unactivated context is present in the persistent record"
            )
        _validate_listed_building_record(property_id, context, coverage, evidence)
        listed_building_status_counts[
            str(_coverage_entry(coverage, "listedBuilding").get("designationStatus") or "unknown")
        ] += 1

        if not isinstance(record.get("metrics"), dict) or not record["metrics"]:
            raise PropertyRecordValidationError(f"{property_id}: metrics are missing")
        if not isinstance(record.get("factPacket"), dict) or not record["factPacket"]:
            raise PropertyRecordValidationError(f"{property_id}: factPacket is missing")
        background_facts = [
            fact
            for fact in record["factPacket"].get("facts", [])
            if isinstance(fact, dict) and fact.get("type") == "background_source_coverage"
        ]
        if len(background_facts) != 1:
            raise PropertyRecordValidationError(
                f"{property_id}: fact packet must contain one background-source coverage fact"
            )
        missing_background_evidence = sorted(
            {
                evidence_id
                for source in COVERAGE_ALIASES
                if source not in EVENT_COVERAGE_TYPES
                for evidence_id in _evidence_ids(_coverage_entry(coverage, source))
            }
            - set(_evidence_ids(background_facts[0]))
        )
        if missing_background_evidence:
            raise PropertyRecordValidationError(
                f"{property_id}: background-source fact omits coverage evidence {missing_background_evidence}"
            )
        unknown_background_evidence = sorted(set(_evidence_ids(background_facts[0])) - evidence_ids)
        if unknown_background_evidence:
            raise PropertyRecordValidationError(
                f"{property_id}: background-source fact references unknown evidence {unknown_background_evidence}"
            )
        _validate_story(property_id, record.get("story"), evidence_ids)
        _validate_valuation(
            property_id,
            record.get("story", {}).get("valuation"),
            evidence,
            expected_universe,
            str(meta.get("asOf") or ""),
        )
        _validate_json_schema(record, schema, schema, property_id)
        narrative_count += 1
        ready_narrative_count += record.get("story", {}).get("status") == "ready"

        fingerprint = _digest(record.get("fingerprint"))
        if not HEX_DIGEST_RE.fullmatch(fingerprint):
            raise PropertyRecordValidationError(f"{property_id}: fingerprint must be a SHA-256 digest")
        expected_fingerprint = compute_record_fingerprint(record)
        if fingerprint != expected_fingerprint:
            raise PropertyRecordValidationError(f"{property_id}: fingerprint does not match record content")
        validated_fingerprints.append([property_id, expected_fingerprint])

    duplicates = [value for value, count in Counter(all_transaction_ids).items() if count != 1]
    if duplicates:
        raise PropertyRecordValidationError(f"Transactions must belong to exactly one property: {duplicates[:10]}")

    if transactions is not None:
        expected_ids = [str(item.get("id") or "") for item in transactions]
        if any(not value for value in expected_ids) or len(expected_ids) != len(set(expected_ids)):
            raise PropertyRecordValidationError("Input transactions must have present, unique ids")
        if set(expected_ids) != set(all_transaction_ids):
            missing = sorted(set(expected_ids) - set(all_transaction_ids))
            unknown = sorted(set(all_transaction_ids) - set(expected_ids))
            raise PropertyRecordValidationError(
                f"Property transaction coverage mismatch; missing={missing[:10]}, unknown={unknown[:10]}"
            )
        record_by_transaction = {
            transaction_id: property_id
            for property_id, record in normalised.items()
            for transaction_id in map(str, record.get("transactionIds", []))
        }
        identity_mismatches = [
            (transaction_id, record_by_transaction.get(transaction_id), property_id_for_transaction(item))
            for item in transactions
            for transaction_id in [str(item.get("id") or "")]
            if record_by_transaction.get(transaction_id) != property_id_for_transaction(item)
        ]
        if identity_mismatches:
            raise PropertyRecordValidationError(
                f"Transactions assigned to the wrong canonical property: {identity_mismatches[:10]}"
            )
        transactions_by_property: dict[str, list[dict[str, Any]]] = {}
        for item in transactions:
            transactions_by_property.setdefault(property_id_for_transaction(item), []).append(item)
        for property_id, record in normalised.items():
            rows = transactions_by_property.get(property_id, [])
            transaction_designations = [
                item.get("historicEngland")
                for item in rows
                if item.get("historicEngland") is not None
            ]
            if not transaction_designations:
                continue
            if len(transaction_designations) != len(rows):
                raise PropertyRecordValidationError(
                    f"{property_id}: historicEngland must propagate to every transaction for the canonical property"
                )
            for value in transaction_designations:
                _validate_historic_england_contract(property_id, value)
            serialised = {_canonical_json(value) for value in transaction_designations}
            if len(serialised) != 1:
                raise PropertyRecordValidationError(
                    f"{property_id}: transactions disagree on the property-level historicEngland result"
                )
            if record.get("context", {}).get("historicEngland") != transaction_designations[0]:
                raise PropertyRecordValidationError(
                    f"{property_id}: canonical historicEngland context does not match the transaction projection"
                )

    counts = {
        "propertyCount": len(normalised),
        "transactionCount": len(all_transaction_ids),
        "eventCount": event_count,
        "narrativeCount": narrative_count,
    }
    for key, value in counts.items():
        if meta.get(key) != value:
            raise PropertyRecordValidationError(f"Metadata {key}={meta.get(key)!r}; recomputed value is {value}")
    if narrative_count != len(normalised):
        raise PropertyRecordValidationError("Every property must have exactly one non-empty story")
    if ready_narrative_count != len(normalised) or meta.get("readyNarrativeCount") != ready_narrative_count:
        raise PropertyRecordValidationError(
            f"Every property story must be ready; metadata={meta.get('readyNarrativeCount')!r}, recomputed={ready_narrative_count}"
        )

    expected_coverage_counts = {
        source: dict(sorted(counter.items()))
        for source, counter in coverage_counts.items()
    }
    if meta.get("coverageCounts") != expected_coverage_counts:
        raise PropertyRecordValidationError(
            f"Metadata coverageCounts does not match record coverage; recomputed={expected_coverage_counts}"
        )
    expected_listed_building_status_counts = dict(
        sorted(listed_building_status_counts.items())
    )
    if meta.get("listedBuildingStatusCounts") != expected_listed_building_status_counts:
        raise PropertyRecordValidationError(
            "Metadata listedBuildingStatusCounts does not match record designation states; "
            f"recomputed={expected_listed_building_status_counts}"
        )

    expected_dataset_fingerprint = hashlib.sha256(
        _canonical_json(validated_fingerprints).encode("utf-8")
    ).hexdigest()
    dataset_fingerprint = _digest(meta.get("datasetFingerprint"))
    if not HEX_DIGEST_RE.fullmatch(dataset_fingerprint):
        raise PropertyRecordValidationError("Metadata datasetFingerprint must be a SHA-256 digest")
    if dataset_fingerprint != expected_dataset_fingerprint:
        raise PropertyRecordValidationError("Metadata datasetFingerprint does not match record content")

    return {
        **counts,
        "datasetFingerprint": expected_dataset_fingerprint,
        "coverage": expected_coverage_counts,
    }


def _read_transactions(path: Path) -> list[dict[str, Any]] | None:
    if not path.exists():
        return None
    from insight_data_utils import read_js

    transactions, _summary, _meta = read_js(path)
    return transactions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_RECORDS)
    parser.add_argument("--transactions", type=Path, default=DEFAULT_TRANSACTIONS)
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="Allow pending/partial/error coverage for a development snapshot; never use for publication.",
    )
    args = parser.parse_args()

    try:
        from property_records import read_property_records_js

        records, meta = read_property_records_js(args.path)
        transactions = _read_transactions(args.transactions)
        summary = validate_property_records(
            records,
            meta,
            transactions,
            allow_incomplete=args.allow_incomplete,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1

    print(
        "OK property records "
        f"({summary['propertyCount']:,} properties, {summary['transactionCount']:,} transactions, "
        f"{summary['eventCount']:,} events, {summary['narrativeCount']:,} stories)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
