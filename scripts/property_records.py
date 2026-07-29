#!/usr/bin/env python3
"""Build durable, evidence-linked INSIGHT property records.

This module is intentionally offline and standard-library only.  It turns the
transaction-led INSIGHT feeds into a property-grain snapshot without treating
an unconfirmed UPRN or a postcode centroid as proof about a physical property.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
GENERATOR_VERSION = "property-records-4"
PROPERTY_RECORDS_NAME = "SURREY_PROPERTY_RECORDS"
PROPERTY_RECORDS_META_NAME = "SURREY_PROPERTY_RECORDS_META"
DEFAULT_PRICE_FLOOR = 2_000_000
NEAR_FLOOR_PRICE_MARGIN = 500_000

COVERAGE_STATES = {
    "complete",
    "partial",
    "checked_none",
    "not_checked",
    "unavailable",
    "failed",
}

BACKGROUND_COVERAGE_SOURCES = (
    "coordinates",
    "currentFlood",
    "planningConstraints",
    "listedBuilding",
    "schools",
    "osUprn",
)

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
LISTED_BUILDING_MATCH_CONFIDENCE = {"confirmed", "review_required"}
HISTORIC_ENGLAND_SOURCE = "Historic England NHLE"

_CONSTRAINT_RESULT_FIELDS = (
    "conservationArea",
    "greenBelt",
    "article4",
    "treePreservationZone",
    "floodRiskZone",
    "ancientWoodland",
    "aonb",
    "sssi",
    "scheduledMonument",
    "heritageAtRisk",
)

_VOLATILE_FINGERPRINT_KEYS = {
    "fingerprint",
    "recordVersion",
    "createdAt",
    "updatedAt",
    "generatedAt",
    "checkedAt",
    "searchedAt",
    "fetchedAt",
    "runAt",
}
_POSTCODE_RE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}$", re.I)
_POSTCODE_AT_END_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\s*$", re.I)
_DATE_RE = re.compile(r"^(?:19|20)\d{2}-\d{2}-\d{2}$")
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")


def clean(value: Any) -> str:
    """Return whitespace-normalised text without changing its case."""

    return " ".join(str(value or "").replace("\xa0", " ").split()).strip()


def normalise_address(value: Any) -> str:
    """Return the v1 canonical full-address token string."""

    return re.sub(r"[^A-Z0-9]+", " ", clean(value).upper()).strip()


def normalise_postcode(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", clean(value).upper())


def _postcode_display(item: Mapping[str, Any]) -> str:
    postcode = clean(item.get("postcode")).upper()
    if postcode:
        return postcode
    match = _POSTCODE_AT_END_RE.search(clean(item.get("address")).upper())
    if match:
        return f"{match.group(1)} {match.group(2)}"
    # Explicit, fail-closed sentinel.  It prevents a missing-postcode row from
    # merging with a postcode-known record while keeping every source
    # transaction represented and its data-quality condition visible.
    return "NOPOSTCODE"


def property_record_id(item: Mapping[str, Any]) -> str:
    """Return the canonical v1 property id: property:<ADDRESS>|<POSTCODE>.

    The complete normalised address is retained.  This deliberately differs
    from the browser's legacy first-two-address-parts grouping and never uses a
    nearest/unconfirmed UPRN as identity.
    """

    address = normalise_address(item.get("address"))
    postcode = normalise_postcode(_postcode_display(item))
    if not address:
        raise ValueError("A property record requires a non-empty address")
    return f"property:{address}|{postcode}"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _stable_id(prefix: str, *parts: Any) -> str:
    digest = sha256_json([clean(part) if not isinstance(part, (dict, list, tuple)) else part for part in parts])[:24]
    return f"{prefix}:{digest}"


def _strip_volatile(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _strip_volatile(child)
            for key, child in value.items()
            if key not in _VOLATILE_FINGERPRINT_KEYS
        }
    if isinstance(value, list):
        return [_strip_volatile(child) for child in value]
    return value


def record_fingerprint(record: Mapping[str, Any]) -> str:
    """Hash the logical record while excluding lifecycle timestamps/version."""

    return sha256_json(_strip_volatile(record))


def dataset_fingerprint(records: Mapping[str, Mapping[str, Any]]) -> str:
    pairs = [[property_id, records[property_id]["fingerprint"]] for property_id in sorted(records)]
    return sha256_json(pairs)


def parse_window_assignment(text: str, name: str, default: Any = None) -> Any:
    pattern = rf"window\.{re.escape(name)}\s*=\s*(.*?);\s*(?=window\.|$)"
    matches = re.findall(pattern, text, flags=re.S)
    if not matches:
        if default is not None:
            return copy.deepcopy(default)
        raise ValueError(f"Missing window.{name} assignment")
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one window.{name} assignment")
    return json.loads(matches[0])


def read_transactions_js(path: str | Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    text = Path(path).read_text(encoding="utf-8")
    transactions = parse_window_assignment(text, "SURREY_LAND_REG_TRANSACTIONS", [])
    metadata = parse_window_assignment(text, "SURREY_LAND_REG_META", {})
    if not isinstance(transactions, list) or not all(isinstance(item, dict) for item in transactions):
        raise ValueError("SURREY_LAND_REG_TRANSACTIONS must be an array of objects")
    if not isinstance(metadata, dict):
        raise ValueError("SURREY_LAND_REG_META must be an object")
    return transactions, metadata


def read_history_js(
    path: str | Path,
    assignment_name: str,
    metadata_name: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    text = Path(path).read_text(encoding="utf-8")
    history = parse_window_assignment(text, assignment_name, {})
    metadata = parse_window_assignment(text, metadata_name, {})
    if not isinstance(history, dict) or not isinstance(metadata, dict):
        raise ValueError(f"window.{assignment_name} and window.{metadata_name} must be objects")
    return history, metadata


def read_property_records_js(path: str | Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    text = Path(path).read_text(encoding="utf-8")
    records = parse_window_assignment(text, PROPERTY_RECORDS_NAME, {})
    metadata = parse_window_assignment(text, PROPERTY_RECORDS_META_NAME, {})
    if isinstance(records, list):
        records = {
            str(record.get("propertyId")): record
            for record in records
            if isinstance(record, dict) and record.get("propertyId")
        }
    if not isinstance(records, dict) or not all(isinstance(item, dict) for item in records.values()):
        raise ValueError(f"window.{PROPERTY_RECORDS_NAME} must be an object of property records")
    if not isinstance(metadata, dict):
        raise ValueError(f"window.{PROPERTY_RECORDS_META_NAME} must be an object")
    return records, metadata


def write_property_records_js(
    path: str | Path,
    records: Mapping[str, Mapping[str, Any]],
    metadata: Mapping[str, Any],
) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    ordered_records = {key: records[key] for key in sorted(records)}
    content = "\n".join(
        [
            f"window.{PROPERTY_RECORDS_NAME} = " + canonical_json(ordered_records) + ";",
            f"window.{PROPERTY_RECORDS_META_NAME} = " + canonical_json(dict(metadata)) + ";",
            "",
        ]
    )
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(destination)


def _parse_date(value: Any) -> date | None:
    text = clean(value)[:10]
    if not _DATE_RE.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _normalise_as_of(value: Any, transactions: Sequence[Mapping[str, Any]], metadata: Mapping[str, Any]) -> str:
    candidate = clean(value)[:10]
    if not candidate:
        candidate = clean(metadata.get("to"))[:10]
    if not candidate:
        candidate = max((clean(item.get("date"))[:10] for item in transactions), default="")
    if not candidate:
        candidate = datetime.now(timezone.utc).date().isoformat()
    if _parse_date(candidate) is None:
        raise ValueError(f"Invalid as-of date: {candidate!r}; expected YYYY-MM-DD")
    return candidate


def _normalise_generated_at(value: Any) -> str:
    text = clean(value)
    if not text:
        return utc_now()
    if _DATE_RE.fullmatch(text):
        return text + "T00:00:00Z"
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"Invalid generated-at timestamp: {text!r}") from error
    return text


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        match = re.search(r"-?\d+(?:\.\d+)?", str(value or "").replace(",", ""))
        if not match:
            return None
        number = float(match.group(0))
    return number if math.isfinite(number) else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(round(number)) if number is not None else None


def _format_money(value: Any) -> str:
    amount = _number(value)
    if amount is None:
        return "an undisclosed amount"
    if abs(amount) >= 1_000_000:
        digits = f"{amount / 1_000_000:.3f}".rstrip("0").rstrip(".")
        return f"£{digits}m"
    return f"£{amount:,.0f}"


def _format_date(value: Any) -> str:
    parsed = _parse_date(value)
    if parsed is None:
        return clean(value) or "an unrecorded date"
    return f"{parsed.day} {parsed.strftime('%B %Y')}"


def _years_between(left: Any, right: Any) -> float | None:
    start = _parse_date(left)
    end = _parse_date(right)
    if not start or not end or end < start:
        return None
    return round((end - start).days / 365.2425, 1)


def _source_record(
    history: Mapping[str, Any],
    property_id: str,
    rows: Sequence[Mapping[str, Any]],
    inline_key: str,
) -> dict[str, Any] | None:
    candidates = [property_id]
    candidates.extend(str(item.get("id")) for item in rows if item.get("id") not in (None, ""))
    for key in candidates:
        record = history.get(key)
        if isinstance(record, dict):
            return record
    for item in rows:
        record = item.get(inline_key)
        if isinstance(record, dict):
            return record
    return None


def _sale_signature(item: Mapping[str, Any]) -> str:
    return "|".join(
        [
            clean(item.get("date"))[:10],
            str(_integer(item.get("price")) or ""),
            normalise_address(item.get("propertyType")),
            normalise_address(item.get("category")),
        ]
    )


def _sale_priority(item: Mapping[str, Any]) -> tuple[int, int]:
    source_id = clean(item.get("sourceTransactionId") or item.get("id"))
    source = clean(item.get("source"))
    return (
        2 if source_id.startswith(("http://landregistry", "https://landregistry")) else 1,
        1 if "PRICE PAID DATA" in source.upper() else 0,
    )


def _sales_for_property(
    property_id: str,
    rows: Sequence[Mapping[str, Any]],
    sales_history: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    history_record = _source_record(sales_history, property_id, rows, "salesHistory")
    candidates: list[Mapping[str, Any]] = []
    if history_record and isinstance(history_record.get("transactions"), list):
        candidates.extend(item for item in history_record["transactions"] if isinstance(item, dict))
    candidates.extend(rows)
    unique: dict[str, Mapping[str, Any]] = {}
    for candidate in candidates:
        signature = _sale_signature(candidate)
        if not signature.strip("|"):
            continue
        if signature not in unique or _sale_priority(candidate) > _sale_priority(unique[signature]):
            unique[signature] = candidate
    sales = []
    for signature, item in unique.items():
        price = _integer(item.get("price"))
        sale = {
            "signature": signature,
            "date": clean(item.get("date"))[:10],
            "price": price,
            "propertyType": clean(item.get("propertyType")),
            "category": clean(item.get("category")),
            "source": clean(item.get("source")) or "HM Land Registry Price Paid Data",
            "sourceTransactionId": clean(item.get("sourceTransactionId") or item.get("id")),
            "address": clean(item.get("address")),
            "postcode": clean(item.get("postcode")),
        }
        sales.append({key: value for key, value in sale.items() if value not in (None, "")})
    sales.sort(key=lambda item: (item.get("date", ""), item.get("price", 0), item.get("signature", "")))
    return sales, history_record


def _normalise_planning_date_text(value: Any) -> str:
    """Return the first explicit portal day as ISO without guessing from a year."""

    text = clean(value)
    if not text:
        return ""
    candidates: list[tuple[str, str]] = []
    iso_match = re.search(r"(?<!\d)((?:19|20)\d{2})-(\d{2})-(\d{2})(?!\d)", text)
    if iso_match:
        candidates.append(("-".join(iso_match.groups()), "%Y-%m-%d"))
    text_match = re.search(
        r"(?<!\d)(\d{1,2})\s+([A-Z]{3,9})\s+((?:19|20)\d{2})(?!\d)",
        text,
        flags=re.I,
    )
    if text_match:
        day, month, year = text_match.groups()
        candidates.extend([
            (f"{day} {month} {year}", "%d %b %Y"),
            (f"{day} {month} {year}", "%d %B %Y"),
        ])
    numeric_match = re.search(
        r"(?<!\d)(\d{1,2})[/-](\d{1,2})[/-]((?:19|20)\d{2})(?!\d)",
        text,
    )
    if numeric_match:
        day, month, year = numeric_match.groups()
        candidates.append((f"{day}/{month}/{year}", "%d/%m/%Y"))
    for candidate, date_format in candidates:
        try:
            parsed = datetime.strptime(candidate, date_format).date()
        except ValueError:
            continue
        if parsed.year == 1900:
            continue
        return parsed.isoformat()
    return ""


def _planning_application_date(application: Mapping[str, Any]) -> tuple[str, str]:
    for key in (
        "decisionDate",
        "validatedDate",
        "receivedDate",
        "submittedDate",
        "registeredDate",
        "date",
        "startDate",
    ):
        value = clean(application.get(key))
        if _DATE_RE.fullmatch(value[:10]):
            return value[:10], "day"
        if _YEAR_RE.fullmatch(value):
            return value, "year"
    portal_date = _normalise_planning_date_text(application.get("dateText"))
    if portal_date:
        return portal_date, "day"
    year = clean(application.get("year"))
    if _YEAR_RE.fullmatch(year):
        return year, "year"
    reference = clean(application.get("reference"))
    reference_year = re.search(
        r"(?:^|[/_])((?:19|20)\d{2})(?=[/_]\d)",
        reference,
        flags=re.I,
    )
    if reference_year:
        return reference_year.group(1), "year"
    short_reference_year = re.match(r"^(\d{2})(?:[A-Z/])", reference, flags=re.I)
    if not short_reference_year:
        short_reference_year = re.search(r"(?:^|[/.-])(\d{2})(?=[/.-])", reference)
    if short_reference_year:
        short_year = int(short_reference_year.group(1))
        return str(2000 + short_year if short_year < 50 else 1900 + short_year), "year"
    return "", "unknown"


def _planning_for_property(
    property_id: str,
    rows: Sequence[Mapping[str, Any]],
    planning_history: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    history_record = _source_record(planning_history, property_id, rows, "planningHistory")
    applications = history_record.get("applications", []) if history_record else []
    if not isinstance(applications, list):
        applications = []
    unique: dict[str, dict[str, Any]] = {}
    for raw in applications:
        if not isinstance(raw, dict):
            continue
        authority = clean(raw.get("authority") or (history_record or {}).get("authority"))
        reference = clean(raw.get("reference") or raw.get("applicationReference"))
        event_date, precision = _planning_application_date(raw)
        signature = (
            f"{normalise_address(authority)}|{normalise_address(reference)}"
            if reference
            else sha256_json(
                [
                    normalise_address(raw.get("siteAddress") or raw.get("address")),
                    normalise_address(raw.get("proposal") or raw.get("description")),
                    event_date,
                ]
            )
        )
        application = {
            "signature": signature,
            "reference": reference,
            "authority": authority,
            "date": event_date,
            "datePrecision": precision,
            "receivedDate": clean(raw.get("receivedDate")),
            "validatedDate": clean(raw.get("validatedDate")),
            "decisionDate": clean(raw.get("decisionDate")),
            "dateText": clean(raw.get("dateText")),
            "proposal": clean(raw.get("proposal") or raw.get("description") or raw.get("title")),
            "applicationType": clean(raw.get("applicationType") or raw.get("type")),
            "status": clean(raw.get("status")),
            "decision": clean(raw.get("decision")),
            "siteAddress": clean(raw.get("siteAddress") or raw.get("address")),
            "portalUrl": clean(raw.get("portalUrl") or raw.get("url")),
            "matchConfidence": _number(raw.get("matchConfidence") or (history_record or {}).get("matchConfidence")),
            "source": clean((history_record or {}).get("source")) or "Property-level planning history",
        }
        # Keep structured area deltas intact for the valuation model.  These
        # fields are materially stronger evidence than trying to infer an
        # incremental extension area from free-text proposal copy.
        for area_key in (
            "additionalFloorAreaSqft",
            "addedAreaSqft",
            "floorAreaIncreaseSqft",
            "additionalFloorAreaSqm",
            "addedAreaSqm",
            "floorAreaIncreaseSqm",
            "existingFloorAreaSqft",
            "proposedFloorAreaSqft",
            "existingFloorAreaSqm",
            "proposedFloorAreaSqm",
        ):
            area_value = _number(raw.get(area_key))
            if area_value is not None:
                application[area_key] = area_value
        cleaned = {key: value for key, value in application.items() if value not in (None, "")}
        existing = unique.get(signature)
        if not existing or len(canonical_json(cleaned)) > len(canonical_json(existing)):
            unique[signature] = cleaned
    result = sorted(unique.values(), key=lambda item: (item.get("date", ""), item.get("reference", "")))
    return result, history_record


def _epc_for_property(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for item in rows:
        registration_date = clean(item.get("epcRegistrationDate"))[:10]
        area_sqft = _integer(item.get("floorAreaSqft"))
        area_sqm = _number(item.get("floorAreaSqm"))
        rating = clean(item.get("epcRating"))
        if not any((registration_date, area_sqft, area_sqm, rating)):
            continue
        signature = sha256_json([registration_date, area_sqft, area_sqm, rating])
        candidate = {
            "signature": signature,
            "date": registration_date,
            "datePrecision": "day" if _DATE_RE.fullmatch(registration_date) else "unknown",
            "floorAreaSqft": area_sqft,
            "floorAreaSqm": round(area_sqm, 1) if area_sqm is not None else None,
            "rating": rating,
            "source": clean(item.get("epcSource")) or "MHCLG EPC Register",
        }
        cleaned = {key: value for key, value in candidate.items() if value not in (None, "")}
        existing = unique.get(signature)
        if not existing or len(canonical_json(cleaned)) > len(canonical_json(existing)):
            unique[signature] = cleaned
    return sorted(unique.values(), key=lambda item: (item.get("date", ""), item.get("signature", "")))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _valid_coordinates(item: Mapping[str, Any]) -> tuple[float, float] | None:
    latitude = _number(item.get("latitude"))
    longitude = _number(item.get("longitude"))
    if latitude is None or longitude is None:
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return latitude, longitude


def _latest_context_mapping(
    rows: Sequence[Mapping[str, Any]],
    key: str,
) -> Mapping[str, Any] | None:
    candidates: list[tuple[str, str, int, Mapping[str, Any]]] = []
    for index, row in enumerate(rows):
        value = row.get(key)
        if not isinstance(value, Mapping):
            continue
        candidates.append(
            (
                clean(
                    value.get("observedAt")
                    or value.get("checkedAt")
                    or value.get("sourceUpdatedAt")
                    or value.get("updatedAt")
                ),
                clean(row.get("date"))[:10],
                index,
                value,
            )
        )
    return max(candidates, default=None, key=lambda item: item[:3])[3] if candidates else None


def _property_context_target(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Merge the newest usable public context onto the latest sale row.

    Context enrichment is transaction-grain, while this module is
    property-grain.  A later sale row must not erase a valid source response
    carried by another transaction for the same canonical property.  The
    restricted/unactivated OpenStreetMap and Companies House fields are
    deliberately not copied.
    """

    target = dict(rows[-1])
    coordinate_rows = [row for row in rows if _valid_coordinates(row)]
    if coordinate_rows:
        coordinate_row = max(
            coordinate_rows,
            key=lambda row: (
                clean(_mapping(row.get("geocode")).get("updatedAt")),
                clean(row.get("date"))[:10],
                clean(row.get("id")),
            ),
        )
        for key in ("latitude", "longitude", "coordinateSource", "coordinatePrecision", "geocode"):
            if coordinate_row.get(key) not in (None, "", [], {}):
                target[key] = copy.deepcopy(coordinate_row[key])

    for key in (
        "environmentAgency",
        "planningConstraints",
        "historicEngland",
        "ofsted",
        "ordnanceSurvey",
    ):
        context = _latest_context_mapping(rows, key)
        if context is not None:
            target[key] = copy.deepcopy(context)
    if not clean(target.get("uprn")):
        for row in reversed(rows):
            if clean(row.get("uprn")):
                target["uprn"] = clean(row.get("uprn"))
                break
    target.pop("openStreetMap", None)
    target.pop("companiesHouse", None)
    target.pop("planning", None)
    return target


def _parse_timestamp(value: Any) -> datetime | None:
    text = clean(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _flood_observation_is_fresh(
    context: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> bool:
    observed = _parse_timestamp(context.get("observedAt") or context.get("updatedAt"))
    checked = _parse_timestamp(metadata.get("freshnessCheckedAt") or metadata.get("updatedAt"))
    maximum_age_hours = _number(metadata.get("maximumAgeHours"))
    if observed is None or checked is None or maximum_age_hours is None or maximum_age_hours <= 0:
        return False
    age_hours = (checked - observed).total_seconds() / 3600
    return 0 <= age_hours <= maximum_age_hours


def _positive_constraint_fields(context: Mapping[str, Any]) -> list[str]:
    return [field for field in _CONSTRAINT_RESULT_FIELDS if context.get(field) not in (None, "", [], {})]


def _background_coverage_records(
    target: Mapping[str, Any],
    transaction_meta: Mapping[str, Any],
    as_of: str,
) -> dict[str, dict[str, Any]]:
    property_context_meta = _mapping(transaction_meta.get("propertyContext"))
    postcode_meta = _mapping(property_context_meta.get("postcodes"))
    flood_meta = _mapping(property_context_meta.get("environmentAgency"))
    weekly_meta = _mapping(transaction_meta.get("weeklyContext"))
    constraints_meta = _mapping(weekly_meta.get("planningConstraints"))
    schools_meta = _mapping(weekly_meta.get("schools"))
    os_meta = _mapping(transaction_meta.get("osRefresh"))
    heritage_meta = _mapping(transaction_meta.get("heritageSync"))

    coordinate_pair = _valid_coordinates(target)
    geocode = _mapping(target.get("geocode"))
    historic_england = _mapping(target.get("historicEngland"))
    coordinate_precision = clean(target.get("coordinatePrecision") or geocode.get("precision"))
    coordinate_source = clean(target.get("coordinateSource") or geocode.get("source") or postcode_meta.get("source")) or "Postcodes.io"
    coordinate_is_nhle_designation = bool(
        coordinate_pair
        and coordinate_source == HISTORIC_ENGLAND_SOURCE
        and coordinate_precision == "confirmed-nhle-designation-location"
        and historic_england.get("status") == "confirmed_listed"
        and len(historic_england.get("entries") or []) == 1
        and historic_england["entries"][0].get("matchMethod") == "reviewed_override"
        and historic_england["entries"][0].get("matchConfidence") == "confirmed"
    )
    coordinate_is_exact = bool(
        coordinate_pair
        and not coordinate_is_nhle_designation
        and ("exact" in coordinate_precision.casefold() or "address" in coordinate_precision.casefold())
        and "postcode" not in coordinate_precision.casefold()
    )
    if coordinate_is_nhle_designation:
        coordinate_coverage_mode = "confirmed-nhle-designation-location"
        coordinate_checked_at = clean(
            historic_england.get("checkedAt") or heritage_meta.get("fetchedAt")
        )
        coordinate_basis = (
            "A human-reviewed one-to-one NHLE designation point is used to refine "
            "the property's map marker."
        )
        coordinate_limitations = [
            "The NHLE designation point improves map placement but is not exact-property, "
            "title-boundary, building-footprint, parcel or listed-curtilage geometry."
        ]
    else:
        coordinate_coverage_mode = (
            "mapped-point-observation" if coordinate_pair else "not-available"
        )
        coordinate_checked_at = clean(
            geocode.get("updatedAt") or property_context_meta.get("updatedAt")
        )
        coordinate_basis = (
            f"A usable mapping coordinate is present at {coordinate_precision or 'declared source precision'}"
            if coordinate_pair
            else "No usable mapping coordinate is available for this canonical property"
        )
        coordinate_limitations = (
            []
            if coordinate_is_exact
            else [
                "The mapping point is a postcode centroid or otherwise approximate "
                "and is not exact-property or parcel geometry."
            ]
        )
    coordinates = {
        "status": "complete" if coordinate_pair else "unavailable",
        "complete": bool(coordinate_pair),
        "coverageMode": coordinate_coverage_mode,
        "source": coordinate_source,
        "checkedAt": coordinate_checked_at,
        "coverageFrom": "",
        "coverageTo": as_of,
        "recordCount": 1 if coordinate_pair else 0,
        "basis": coordinate_basis,
        "limitations": coordinate_limitations,
        "precision": coordinate_precision,
        "coordinateIsExactProperty": coordinate_is_exact,
    }
    if coordinate_pair:
        coordinates.update({"latitude": coordinate_pair[0], "longitude": coordinate_pair[1]})

    flood = _mapping(target.get("environmentAgency"))
    flood_fresh = bool(flood and _flood_observation_is_fresh(flood, flood_meta))
    if flood:
        flood_status = "complete" if flood_fresh else "partial"
    elif not coordinate_pair:
        flood_status = "unavailable"
    elif flood_meta and (_integer(flood_meta.get("requestFailures")) or 0) > 0:
        flood_status = "failed"
    elif flood_meta:
        flood_status = "not_checked"
    else:
        flood_status = "unavailable"
    alert_count = max(0, _integer(flood.get("currentFloodAlertCount")) or 0)
    flood_observed_at = clean(flood.get("observedAt") or flood.get("updatedAt"))
    current_flood = {
        "status": flood_status,
        "complete": flood_status == "complete",
        "coverageMode": "current-alert-radius-observation",
        "source": clean(flood.get("source") or flood_meta.get("source")) or "Environment Agency Real Time flood-monitoring API",
        "checkedAt": flood_observed_at or clean(flood_meta.get("freshnessCheckedAt") or property_context_meta.get("updatedAt")),
        "coverageFrom": flood_observed_at[:10],
        "coverageTo": flood_observed_at[:10] or as_of,
        "recordCount": 1 if flood else 0,
        "basis": (
            f"Dated current-area observation recorded {alert_count} active flood alert{'s' if alert_count != 1 else ''} within the configured radius"
            if flood
            else "No dated current-area flood-alert observation is available"
        ),
        "limitations": [
            "This is a time-specific current-alert observation, not a long-term property flood-risk assessment.",
            "The radius is evaluated from the available mapping point; a postcode-centroid result is wider-area evidence, not exact-property evidence.",
        ],
        "resultStatus": (
            "unknown"
            if not flood
            else (
                "stale_observation"
                if not flood_fresh
                else ("current_alerts_observed" if alert_count else "no_current_alerts_observed")
            )
        ),
        "alertCount": alert_count,
        "floodStatus": clean(flood.get("floodStatus")),
        "highestCurrentSeverity": clean(flood.get("highestCurrentSeverity")),
        "searchRadius": clean(flood.get("searchRadius")),
        "observedAt": flood_observed_at,
    }

    constraints = _mapping(target.get("planningConstraints"))
    constraint_lookup_succeeded = clean(constraints.get("lookupStatus")).lower() in {"success", "successful"}
    positive_fields = _positive_constraint_fields(constraints)
    parsed_constraint_count = _integer(constraints.get("constraintCount"))
    constraint_count = max(0, parsed_constraint_count if parsed_constraint_count is not None else len(positive_fields))
    if constraint_lookup_succeeded:
        constraint_status = "complete" if constraint_count else "checked_none"
    elif constraints:
        constraint_status = "partial"
    elif not coordinate_pair:
        constraint_status = "unavailable"
    elif constraints_meta:
        constraint_status = "not_checked"
    else:
        constraint_status = "unavailable"
    planning_constraints = {
        "status": constraint_status,
        "complete": constraint_status in {"complete", "checked_none"},
        "coverageMode": clean(constraints_meta.get("coverageMode")) or "explicit-per-row-success",
        "source": clean(constraints.get("source") or constraints_meta.get("source")) or "Planning Data API",
        "checkedAt": clean(constraints.get("updatedAt") or weekly_meta.get("updatedAt")),
        "coverageFrom": "",
        "coverageTo": clean(constraints.get("updatedAt") or weekly_meta.get("updatedAt"))[:10] or as_of,
        "recordCount": constraint_count,
        "basis": (
            "Completed mapped-point lookup returned constraints in the configured Planning Data datasets"
            if constraint_count
            else (
                "Completed mapped-point lookup returned zero entities in the configured Planning Data datasets"
                if constraint_lookup_succeeded
                else "No explicit successful static-constraint lookup is recorded for this property"
            )
        ),
        "limitations": [
            "A zero result means only that the configured datasets returned no entity at the lookup point; it is not proof that no constraint or risk exists.",
            "When the lookup point is a postcode centroid, results describe the wider postcode setting rather than exact property or title geometry.",
        ],
        "lookupStatus": "successful" if constraint_lookup_succeeded else "unproven",
        "resultStatus": "mapped_constraints" if constraint_count else ("no_mapped_constraints" if constraint_lookup_succeeded else "unknown"),
        "positiveFields": positive_fields,
        "floodRiskZone": clean(constraints.get("floodRiskZone")),
    }

    historic_england = _mapping(target.get("historicEngland"))
    designation_status = clean(historic_england.get("status")).lower()
    if designation_status not in LISTED_BUILDING_STATUSES:
        designation_status = "unknown"
    listed_entries = historic_england.get("entries")
    listed_entries = (
        [item for item in listed_entries if isinstance(item, Mapping)]
        if isinstance(listed_entries, list)
        else []
    )
    if designation_status in {"confirmed_listed", "candidate_review"}:
        listed_coverage_status = "complete"
    elif designation_status == "no_direct_match":
        listed_coverage_status = "checked_none"
    else:
        listed_coverage_status = "unavailable"
    listed_checked_at = clean(historic_england.get("checkedAt") or heritage_meta.get("fetchedAt"))
    listed_source_updated_at = clean(
        historic_england.get("sourceUpdatedAt")
        or heritage_meta.get("sourceDataLastEditDate")
    )
    listed_building = {
        "status": listed_coverage_status,
        "complete": listed_coverage_status in {"complete", "checked_none"},
        "coverageMode": clean(heritage_meta.get("coverageMode"))
        or "property-grain-full-address-reviewed-fail-closed",
        "source": clean(historic_england.get("source") or heritage_meta.get("source"))
        or HISTORIC_ENGLAND_SOURCE,
        "checkedAt": listed_checked_at,
        "coverageFrom": "",
        "coverageTo": listed_source_updated_at[:10] or listed_checked_at[:10] or as_of,
        "recordCount": len(listed_entries),
        "basis": {
            "confirmed_listed": "One or more NHLE List Entry Numbers are confirmed against this canonical property identity",
            "candidate_review": "The completed NHLE candidate screen returned one or more nearby entries that require property-identity review before listing is asserted",
            "no_direct_match": "The completed NHLE candidate screen around the available mapped coordinate found no direct point or name match",
            "unknown": "The NHLE candidate screen could not run conclusively because usable property location evidence is unavailable or conflicting",
        }[designation_status],
        "limitations": [
            "A zero direct match is not proof that the property is not listed or outside listed-building curtilage.",
            "Where the available coordinate is a postcode centroid, the screen describes the wider postcode setting rather than exact title or building geometry.",
            "Legacy NHLE points, shared entries and curtilage may require verification with Historic England or the local planning authority.",
        ],
        "designationStatus": designation_status,
        "resultStatus": designation_status,
        "sourceUpdatedAt": listed_source_updated_at,
        "sourceSnapshot": clean(
            historic_england.get("sourceSnapshot")
            or heritage_meta.get("sourceSnapshot")
        ),
    }

    school_context = _mapping(target.get("ofsted"))
    nearest_schools = school_context.get("nearestSchools")
    nearest_schools = [item for item in nearest_schools if isinstance(item, Mapping)] if isinstance(nearest_schools, list) else []
    if school_context:
        school_status = "complete" if nearest_schools else "checked_none"
    elif not coordinate_pair or schools_meta.get("loaded") is False:
        school_status = "unavailable"
    elif schools_meta:
        school_status = "not_checked"
    else:
        school_status = "unavailable"
    nearest_school = nearest_schools[0] if nearest_schools else {}
    schools = {
        "status": school_status,
        "complete": school_status in {"complete", "checked_none"},
        "coverageMode": "nearby-schools-radius-lookup",
        "source": clean(school_context.get("source") or schools_meta.get("source")) or "DfE Get Information about Schools (GIAS)",
        "checkedAt": clean(school_context.get("updatedAt") or weekly_meta.get("updatedAt")),
        "coverageFrom": "",
        "coverageTo": clean(school_context.get("updatedAt") or weekly_meta.get("updatedAt"))[:10] or as_of,
        "recordCount": len(nearest_schools),
        "basis": (
            f"Nearby-school lookup returned {len(nearest_schools)} retained school record{'s' if len(nearest_schools) != 1 else ''}"
            if school_context
            else "No completed nearby-school lookup is recorded for this property"
        ),
        "limitations": [
            "The list is radius-limited and capped; it is not a complete school-admissions or catchment assessment.",
            "Distances from a postcode-centroid mapping point are approximate and are not exact-property measurements.",
        ],
        "resultStatus": "nearby_schools" if nearest_schools else ("no_retained_schools" if school_context else "unknown"),
        "searchRadius": clean(school_context.get("searchRadius")),
        "nearestSchool": {
            key: nearest_school[key]
            for key in ("name", "urn", "metres", "postcode", "phase")
            if nearest_school.get(key) not in (None, "")
        },
    }

    os_context = _mapping(target.get("ordnanceSurvey"))
    uprn = clean(target.get("uprn") or os_context.get("uprn"))
    os_source_loaded = os_meta.get("sourceLoaded") is True
    if uprn:
        os_status = "complete"
    elif os_source_loaded and coordinate_pair:
        os_status = "complete"
    elif not coordinate_pair or os_meta.get("sourceLoaded") is False:
        os_status = "unavailable"
    elif os_meta:
        os_status = "not_checked"
    else:
        os_status = "unavailable"
    confirmed_address_match = os_context.get("confirmedAddressMatch") is True
    os_uprn = {
        "status": os_status,
        "complete": os_status == "complete",
        "coverageMode": "nearest-candidate-within-radius",
        "source": clean(os_context.get("source") or os_meta.get("source")) or "OS Open UPRN",
        "checkedAt": clean(os_context.get("updatedAt") or os_meta.get("updatedAt")),
        "coverageFrom": "",
        "coverageTo": clean(os_context.get("updatedAt") or os_meta.get("updatedAt"))[:10] or as_of,
        "recordCount": 1 if uprn else 0,
        "basis": (
            "The OS lookup returned a nearest UPRN candidate to the available coordinate"
            if uprn
            else (
                "The completed nearest-candidate search returned no UPRN within the configured radius"
                if os_status == "complete"
                else "No completed OS Open UPRN candidate search is recorded for this property"
            )
        ),
        "limitations": [
            "A nearest-coordinate UPRN is an unconfirmed candidate and never controls canonical property identity unless an address match is independently confirmed.",
            "No candidate within the search radius is not evidence that the property has no UPRN.",
        ],
        "resultStatus": (
            "confirmed_address_match"
            if confirmed_address_match
            else (
                "nearest_candidate_unconfirmed"
                if uprn
                else ("no_candidate_within_radius" if os_status == "complete" else "unknown")
            )
        ),
        "uprn": uprn,
        "matchDistance": clean(os_context.get("uprnMatchDistance")),
        "precision": clean(os_context.get("uprnPrecision")),
        "matchMethod": clean(os_context.get("matchMethod")) or ("nearest-to-available-coordinate" if uprn else ""),
        "confirmedAddressMatch": confirmed_address_match,
    }

    return {
        "coordinates": coordinates,
        "currentFlood": current_flood,
        "planningConstraints": planning_constraints,
        "listedBuilding": listed_building,
        "schools": schools,
        "osUprn": os_uprn,
    }


def _coverage_evidence(
    property_id: str,
    source_key: str,
    coverage: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    stable = {
        key: value
        for key, value in coverage.items()
        if key not in {"evidenceIds", "checkedAt"}
    }
    evidence_id = _stable_id("evidence:coverage", property_id, source_key, stable)
    evidence = {
        "evidenceId": evidence_id,
        "propertyId": property_id,
        "type": "source_coverage",
        "source": clean(coverage.get("source")) or source_key,
        "sourceId": source_key,
        "effectiveDate": clean(coverage.get("coverageTo") or coverage.get("checkedAt"))[:10],
        "data": stable,
    }
    return evidence_id, {key: value for key, value in evidence.items() if value not in (None, "")}


def _authority_coverage_complete(metadata: Mapping[str, Any], district: str) -> bool:
    rows = metadata.get("authorityCoverage")
    if not isinstance(rows, list) or not rows:
        return True
    relevant = [
        row for row in rows
        if isinstance(row, dict) and clean(row.get("district")).casefold() == clean(district).casefold()
    ]
    if not relevant:
        relevant = [row for row in rows if isinstance(row, dict)]
    incomplete = {"partial", "failed", "error", "unavailable", "not_checked"}
    return bool(relevant) and all(clean(row.get("status")).lower() not in incomplete for row in relevant)


def _planning_full_history_proof(
    property_id: str,
    district: str,
    metadata: Mapping[str, Any],
    property_count: int,
    history_record: Mapping[str, Any] | None,
) -> bool:
    if clean(metadata.get("coverageMode")).lower() != "full-available-history":
        return False
    if isinstance(history_record, Mapping):
        record_status = clean(history_record.get("coverageStatus")).lower()
        record_mode = clean(history_record.get("coverageMode")).lower()
        if record_status in {"complete", "checked_none"} and record_mode == "full-available-history":
            return True
        if record_status in {"unavailable", "failed", "error", "not_checked"}:
            return False
    property_coverage = metadata.get("propertyCoverage")
    if isinstance(property_coverage, dict):
        entry = property_coverage.get(property_id)
        if isinstance(entry, dict) and (
            entry.get("complete") is True
            or clean(entry.get("status")).lower() in {"complete", "checked_none"}
        ):
            return True
    checked_ids = metadata.get("checkedPropertyIds")
    if isinstance(checked_ids, list) and property_id in {str(value) for value in checked_ids}:
        return True
    if history_record is not None:
        return _authority_coverage_complete(metadata, district)
    checked = _integer(metadata.get("propertiesChecked")) or 0
    return checked >= property_count and _authority_coverage_complete(metadata, district)


def _coverage_records(
    property_id: str,
    rows: Sequence[Mapping[str, Any]],
    target: Mapping[str, Any],
    sales: Sequence[Mapping[str, Any]],
    sales_record: Mapping[str, Any] | None,
    epcs: Sequence[Mapping[str, Any]],
    planning: Sequence[Mapping[str, Any]],
    planning_record: Mapping[str, Any] | None,
    transaction_meta: Mapping[str, Any],
    sales_meta: Mapping[str, Any],
    planning_meta: Mapping[str, Any],
    property_count: int,
    as_of: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    district = clean(rows[-1].get("district")) if rows else ""
    full_sales_complete = bool(sales_record and sales_meta.get("coverageFrom"))
    ledger_complete = bool(sales and transaction_meta.get("from"))
    sales_complete = full_sales_complete or ledger_complete
    sales_mode = "price-paid-from-date" if full_sales_complete else "qualifying-ledger-from-date"
    sales_limitations = [
        "Price Paid Data begins in 1995 and is not a legal title or ownership history."
    ]
    if not full_sales_complete:
        threshold = _integer(transaction_meta.get("priceFloor"))
        sales_limitations.append(
            f"This record is complete only for the loaded qualifying ledger{' at or above ' + _format_money(threshold) if threshold else ''}; lower-value transactions may be absent."
        )
    sales_coverage = {
        "status": "complete" if sales_complete else "unavailable",
        "complete": sales_complete,
        "coverageMode": sales_mode if sales_complete else "not-established",
        "source": clean(sales_meta.get("source")) or "HM Land Registry Price Paid Data",
        "checkedAt": clean((sales_record or {}).get("updatedAt") or sales_meta.get("updatedAt")),
        "coverageFrom": clean(sales_meta.get("coverageFrom") or transaction_meta.get("from"))[:10],
        "coverageTo": as_of,
        "recordCount": len(sales),
        "basis": "Property-keyed full Price Paid lookup" if full_sales_complete else "Complete membership of the loaded qualifying transaction ledger",
        "limitations": sales_limitations,
    }

    full_planning = _planning_full_history_proof(
        property_id,
        district,
        planning_meta,
        property_count,
        planning_record,
    )
    planning_source_status = clean(
        (planning_record or {}).get("coverageStatus")
        or planning_meta.get("coverageStatus")
        or planning_meta.get("status")
        or planning_meta.get("deploymentMode")
    ).lower()
    if planning:
        planning_status = "complete" if full_planning else "partial"
    elif full_planning:
        planning_status = "checked_none"
    elif planning_source_status in {"failed", "error"}:
        planning_status = "failed"
    elif planning_source_status in {"unavailable", "not-available"}:
        planning_status = "unavailable"
    elif planning_meta:
        planning_status = "not_checked"
    else:
        planning_status = "unavailable"
    planning_coverage = {
        "status": planning_status,
        "complete": full_planning,
        "coverageMode": clean((planning_record or {}).get("coverageMode") or planning_meta.get("coverageMode")) or "not-established",
        "source": clean((planning_record or {}).get("source") or planning_meta.get("source")) or "Property-level planning history",
        "checkedAt": clean((planning_record or {}).get("updatedAt") or planning_meta.get("updatedAt")),
        "coverageFrom": str(planning_meta.get("earliestApplicationYear") or ""),
        "coverageTo": str(planning_meta.get("latestApplicationYear") or as_of[:4]),
        "recordCount": len(planning),
        "basis": (
            "Completed property-level full-history lookup"
            if full_planning
            else "No completed property-level full-history proof for this property"
        ),
        "limitations": [
            "An application or permission is not proof that work was started or built.",
            "Available archive depth varies by planning authority.",
        ],
    }

    epc_meta = transaction_meta.get("epcEnrichment") if isinstance(transaction_meta.get("epcEnrichment"), dict) else {}
    epc_run_complete = clean(epc_meta.get("status")).lower() == "complete"
    epc_status = "complete" if epcs else ("checked_none" if epc_run_complete else ("not_checked" if epc_meta else "unavailable"))
    epc_coverage = {
        # "complete" means the matched EPC enrichment represented by this
        # record completed successfully; it does not claim a complete historic
        # certificate archive (see coverageMode and limitations).
        "status": epc_status,
        "complete": bool(epcs) or epc_run_complete,
        "coverageMode": "matched-certificates-on-qualifying-transactions",
        "source": clean(epc_meta.get("source")) or "MHCLG EPC Register",
        "checkedAt": clean(epc_meta.get("updatedAt")),
        "coverageFrom": "",
        "coverageTo": as_of,
        "recordCount": len(epcs),
        "basis": "EPC certificates attached to the loaded transaction records" if epcs else "Completed EPC enrichment returned no matched certificate snapshot",
        "limitations": [
            "The feed is not a guaranteed complete EPC certificate history.",
            "An EPC floor-area observation does not prove when or why a physical change occurred.",
        ],
    }

    coverage = {
        "sales": sales_coverage,
        "planning": planning_coverage,
        "epc": epc_coverage,
        **_background_coverage_records(target, transaction_meta, as_of),
    }
    evidence: dict[str, dict[str, Any]] = {}
    for source_key, entry in coverage.items():
        evidence_id, evidence_record = _coverage_evidence(property_id, source_key, entry)
        entry["evidenceIds"] = [evidence_id]
        evidence[evidence_id] = evidence_record
    return coverage, evidence


def _event_and_evidence(
    property_id: str,
    event_type: str,
    source_row: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    signature = clean(source_row.get("signature")) or sha256_json(source_row)
    evidence_id = _stable_id(f"evidence:{event_type}", property_id, signature)
    event_id = _stable_id(f"event:{event_type}", property_id, signature)
    source = clean(source_row.get("source")) or humanise_source(event_type)
    event = {
        "eventId": event_id,
        "propertyId": property_id,
        "type": event_type,
        "date": clean(source_row.get("date")),
        "datePrecision": clean(source_row.get("datePrecision")) or ("day" if _DATE_RE.fullmatch(clean(source_row.get("date"))) else "unknown"),
        "source": source,
        "evidenceIds": [evidence_id],
        "summary": _event_summary(event_type, source_row),
    }
    evidence = {
        "evidenceId": evidence_id,
        "propertyId": property_id,
        "type": event_type,
        "source": source,
        "sourceId": clean(
            source_row.get("sourceTransactionId")
            or source_row.get("certificateNumber")
            or source_row.get("reference")
            or signature
        ),
        "effectiveDate": clean(source_row.get("date")),
        "data": {key: value for key, value in source_row.items() if key != "signature"},
    }
    cleaned_event = {key: value for key, value in event.items() if value not in (None, "")}
    # An honest unknown planning date is still part of the permanent timeline.
    # Keep the required date key empty and pair it with datePrecision=unknown;
    # the timeline sorter places these records after dated evidence.
    cleaned_event["date"] = clean(event.get("date"))
    return (
        cleaned_event,
        {key: value for key, value in evidence.items() if value not in (None, "")},
    )


def humanise_source(event_type: str) -> str:
    return {
        "sale": "HM Land Registry Price Paid Data",
        "planning_application": "Property-level planning history",
        "epc_certificate": "MHCLG EPC Register",
    }.get(event_type, "INSIGHT evidence")


def _event_summary(event_type: str, item: Mapping[str, Any]) -> str:
    if event_type == "sale":
        return f"Registered sale for {_format_money(item.get('price'))}"
    if event_type == "planning_application":
        proposal = clean(item.get("proposal"))
        reference = clean(item.get("reference"))
        return proposal or (f"Planning application {reference}" if reference else "Planning application")
    if event_type == "epc_certificate":
        area = _integer(item.get("floorAreaSqft"))
        rating = clean(item.get("rating"))
        details = [f"{area:,} sq ft" if area else "", f"rating {rating}" if rating else ""]
        return "EPC certificate" + (": " + ", ".join(value for value in details if value) if any(details) else "")
    return event_type.replace("_", " ").title()


def _fact(
    property_id: str,
    fact_type: str,
    text: str,
    evidence_ids: Iterable[str],
    value: Any = None,
    confidence: str = "high",
) -> dict[str, Any]:
    evidence_list = sorted(set(str(item) for item in evidence_ids if item))
    fact_id = _stable_id("fact", property_id, fact_type, text, evidence_list)
    result = {
        "factId": fact_id,
        "type": fact_type,
        "text": text,
        "value": value,
        "evidenceIds": evidence_list,
        "confidence": confidence,
    }
    return {key: child for key, child in result.items() if child not in (None, "")}


_PLANNING_NEGATIVE_RE = re.compile(r"\b(?:refus|withdraw|dismiss|declin|invalid|lapse|reject)", re.I)
_PLANNING_IMPLEMENTATION_RE = re.compile(
    r"\b(?:confirmation of compliance|discharge (?:of )?(?:planning )?conditions?|conditions? (?:approved|discharged)|"
    r"details? (?:pursuant to|required by|reserved by) conditions?|"
    r"(?:submission|approval) of (?:details?|materials?)(?: (?:pursuant to|required by|reserved by) conditions?)?|"
    r"submission of .{0,120}? pursuant to conditions?|"
    r"application (?:for|to) (?:approval of details|(?:the )?(?:proposed )?discharge (?:of )?(?:planning )?conditions?|"
    r"remove (?:a |the )?(?:planning )?condition)|compliance with conditions?|relaxation of conditions?|"
    r"(?:permission|details?) required by conditions?|non[- ]material (?:amendments?|changes?)|"
    r"(?:minor material|material minor) amendment|(?:further )?amendments? to (?:planning )?(?:permission|application)|"
    r"variation of (?:planning )?condition|vary(?:ing)? (?:a |the )?(?:planning )?condition|"
    r"removal of (?:a |the )?(?:planning )?condition|section 73|s\.?73|"
    r"approval of reserved matters|reserved matters (?:pursuant to|to|following)|renewal of .{0,80}? permission|"
    r"certificate of (?:proposed |existing )?lawful development|certificate of lawfulness|"
    r"retrospective|existing lawfulness|grant certificate|prior approval not required|lawful development certi\w*)",
    re.I,
)


def _planning_outcome(application: Mapping[str, Any]) -> str:
    decision = clean(application.get("decision")).casefold()
    status = clean(application.get("status")).casefold()
    outcome = f"{decision} {status}"
    if "allowed on appeal" in outcome:
        return "positive"
    if _PLANNING_NEGATIVE_RE.search(outcome):
        return "negative"
    proposal = clean(application.get("proposal"))
    if _PLANNING_IMPLEMENTATION_RE.search(outcome) or _PLANNING_IMPLEMENTATION_RE.search(proposal):
        return "implementation"
    if (
        decision in {"approve", "approved", "granted", "approved with conditions", "approved with condition"}
        or decision.startswith("grant consent")
        or status in {"decided (approved)", "decided (permitted development)"}
        or status.startswith("permitted subject to")
    ):
        return "positive"
    return "unknown"


def _planning_category(application: Mapping[str, Any]) -> str:
    proposal = clean(application.get("proposal")).lower()
    reference = clean(application.get("reference"))
    if re.search(r"(?:^|[/.-])(?:tpo|trees?)(?:$|[/.-])", reference, re.I) or re.search(
        r"^\s*(?:see condition for .*?\.\s*)?(?:fell|prune|crown|tree|tpo)\b",
        proposal,
        re.I,
    ):
        return "maintenance"
    if _PLANNING_IMPLEMENTATION_RE.search(proposal):
        return "implementation"
    if re.search(r"\b(?:demolition|replacement (?:house|dwelling)|new (?:house|dwelling)|erection of (?:a |one |1 ?no )?(?:detached )?(?:house|dwelling))\b", proposal):
        return "replacement"
    if re.search(r"\b(?:extension|basement|loft conversion|roofspace|roof space|additional storey|two[- ]storey|first[- ]floor)\b", proposal):
        return "enlargement"
    if re.search(r"\b(?:annexe|garage|pool|outbuilding|summerhouse|gym|tennis|orangery|conservatory)\b", proposal):
        return "amenity"
    if re.search(r"\b(?:driveway|vehicular access|crossover|entrance gate|boundary wall|landscap)\b", proposal):
        return "grounds"
    if re.search(r"\b(?:tree|tpo|crown|fell|prun|arbor)\b", proposal):
        return "maintenance"
    return "other"


def _planning_story_date(application: Mapping[str, Any]) -> date | None:
    parsed = _parse_date(application.get("date"))
    if parsed:
        return parsed
    year_match = re.search(r"(?:19|20)\d{2}", clean(application.get("date")))
    if year_match:
        return date(int(year_match.group(0)), 7, 1)
    reference = clean(application.get("reference"))
    year_match = re.search(r"(?:^|[./-])(\d{2})[./-]\d", reference)
    if not year_match:
        year_match = re.match(r"^(\d{2})(?:[/.-])", reference)
    if year_match:
        short_year = int(year_match.group(1))
        return date(1900 + short_year if short_year >= 90 else 2000 + short_year, 7, 1)
    return None


def _planning_story_score(application: Mapping[str, Any]) -> int:
    category_score = {
        "replacement": 8,
        "enlargement": 6,
        "amenity": 4,
        "grounds": 2,
        "implementation": 1,
        "other": 1,
        "maintenance": 0,
    }[_planning_category(application)]
    outcome = _planning_outcome(application)
    return category_score + (3 if outcome == "positive" else 2 if outcome == "implementation" else 0)


def _planning_description(application: Mapping[str, Any], maximum: int = 65) -> str:
    text = clean(application.get("proposal")).rstrip(" .;:")
    if not text:
        text = "planning work"
    if len(text) > maximum:
        shortened = text[: maximum - 1]
        boundary = shortened.rfind(" ")
        text = shortened[: boundary if boundary > maximum * 0.65 else len(shortened)].rstrip() + "…"
    if text and not text[:2].isupper():
        text = text[0].lower() + text[1:]
    planning_date = _planning_story_date(application)
    return text + (f" in {planning_date.year}" if planning_date else "")


def _planning_highlights(applications: Sequence[Mapping[str, Any]], maximum: int = 1) -> list[str]:
    ordered = sorted(
        applications,
        key=lambda item: (_planning_story_score(item), clean(item.get("date"))),
        reverse=True,
    )
    highlights: list[str] = []
    for application in ordered:
        if _planning_category(application) in {"maintenance", "implementation"} and any(
            _planning_category(candidate) not in {"maintenance", "implementation"} for candidate in ordered
        ):
            continue
        description = _planning_description(application)
        if description not in highlights:
            highlights.append(description)
        if len(highlights) >= maximum:
            break
    return highlights


def _join_story_items(items: Sequence[str]) -> str:
    values = [clean(item) for item in items if clean(item)]
    if len(values) < 2:
        return values[0] if values else ""
    return ", ".join(values[:-1]) + " and " + values[-1]


def _material_epc_growth(epcs: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    observations = [
        {
            "date": _parse_date(item.get("date")),
            "floorAreaSqft": _integer(item.get("floorAreaSqft")),
        }
        for item in epcs
        if _parse_date(item.get("date")) and _integer(item.get("floorAreaSqft"))
    ]
    best: dict[str, Any] | None = None
    for index, earlier in enumerate(observations):
        for later in observations[index + 1 :]:
            if later["date"] <= earlier["date"] or (later["date"] - earlier["date"]).days < 180:
                continue
            first_area = int(earlier["floorAreaSqft"])
            last_area = int(later["floorAreaSqft"])
            change = last_area - first_area
            ratio = change / first_area
            if change < 250 or ratio < 0.08 or last_area / first_area > 3:
                continue
            candidate = {
                "from": earlier,
                "to": later,
                "changeSqft": change,
                "changePercent": ratio * 100,
            }
            if not best or candidate["changeSqft"] > best["changeSqft"]:
                best = candidate
    return best


def _best_planning_episode(
    sales: Sequence[Mapping[str, Any]],
    planning: Sequence[Mapping[str, Any]],
    epc_growth: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if len(sales) < 2:
        return None
    best: dict[str, Any] | None = None
    for earlier, later in zip(sales, sales[1:]):
        start = _parse_date(earlier.get("date"))
        end = _parse_date(later.get("date"))
        if not start or not end:
            continue
        applications = [
            item for item in planning
            if (application_date := _planning_story_date(item)) and start <= application_date <= end
        ]
        if not applications:
            continue
        structural = [item for item in applications if _planning_category(item) in {"replacement", "enlargement", "amenity"}]
        positive_structural = [item for item in structural if _planning_outcome(item) == "positive"]
        implementation = [item for item in applications if _planning_outcome(item) == "implementation" or _planning_category(item) == "implementation"]
        area_aligned = bool(
            epc_growth
            and epc_growth["from"]["date"] <= end
            and epc_growth["to"]["date"] <= end
            and epc_growth["to"]["date"] >= start
        )
        price_change = None
        if _number(earlier.get("price")) and _number(later.get("price")):
            price_change = (_number(later["price"]) / _number(earlier["price"]) - 1) * 100
        score = (
            max((_planning_story_score(item) for item in applications), default=0)
            + min(3, len(positive_structural) * 2)
            + min(2, len(implementation))
            + (4 if area_aligned else 0)
            + (1 if price_change is not None and price_change > 0 else 0)
        )
        candidate = {
            "earlierSale": earlier,
            "laterSale": later,
            "applications": applications,
            "structural": structural,
            "positiveStructural": positive_structural,
            "implementation": implementation,
            "areaAligned": area_aligned,
            "priceChangePercent": price_change,
            "score": score,
        }
        if not best or (candidate["score"], end) > (best["score"], _parse_date(best["laterSale"].get("date")) or date.min):
            best = candidate
    return best


VALUATION_MODEL_VERSION = "property-valuation-1"
_CPIH_ANNUAL_INDEX = {
    1995: 66.6, 1996: 68.5, 1997: 70.0, 1998: 71.3, 1999: 72.6,
    2000: 73.4, 2001: 74.6, 2002: 75.7, 2003: 76.7, 2004: 77.8,
    2005: 79.4, 2006: 81.4, 2007: 83.3, 2008: 86.2, 2009: 87.9,
    2010: 90.1, 2011: 93.6, 2012: 96.0, 2013: 98.2, 2014: 99.6,
    2015: 100.0, 2016: 101.0, 2017: 103.6, 2018: 106.0, 2019: 107.8,
    2020: 108.9, 2021: 111.6, 2022: 120.5, 2023: 128.6, 2024: 132.9,
    2025: 138.0, 2026: 142.1,
}


def _cpih_adjusted(value: Any, value_date: Any, as_of: str) -> float | None:
    amount = _number(value)
    year_match = re.search(r"(?:19|20)\d{2}", clean(value_date))
    as_of_year_match = re.search(r"(?:19|20)\d{2}", clean(as_of))
    if amount is None or not year_match or not as_of_year_match:
        return None
    source_index = _CPIH_ANNUAL_INDEX.get(int(year_match.group(0)))
    target_year = int(as_of_year_match.group(0))
    target_index = _CPIH_ANNUAL_INDEX.get(target_year)
    if target_index is None:
        raise ValueError(f"CPIH valuation index requires an explicit annual value for {target_year}")
    return amount * target_index / source_index if source_index else None


def _valuation_point(item: Mapping[str, Any]) -> tuple[float, float] | None:
    longitude = _number(item.get("longitude") or item.get("lon"))
    latitude = _number(item.get("latitude") or item.get("lat"))
    if longitude is None or latitude is None:
        return None
    return longitude, latitude


def _distance_km(left: Mapping[str, Any], right: Mapping[str, Any]) -> float | None:
    left_point = _valuation_point(left)
    right_point = _valuation_point(right)
    if not left_point or not right_point:
        return None
    lon1, lat1 = map(math.radians, left_point)
    lon2, lat2 = map(math.radians, right_point)
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    haversine = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371 * 2 * math.atan2(math.sqrt(haversine), math.sqrt(max(0, 1 - haversine)))


def _postcode_district(value: Any) -> str:
    return normalise_postcode(value)[:-3]


def _valuation_comparable_score(
    target: Mapping[str, Any],
    candidate: Mapping[str, Any],
    as_of: str,
) -> tuple[float, float | None]:
    raw_target_area = _number(target.get("floorAreaSqft")) or 0
    raw_candidate_area = _number(candidate.get("floorAreaSqft")) or 0
    target_area = raw_target_area if raw_target_area > 0 else 0
    candidate_area = raw_candidate_area if raw_candidate_area > 0 else 0
    area_similarity = math.exp(-abs(math.log(candidate_area / target_area)) * 1.8) if target_area and candidate_area else 0.35
    type_match = 1.0 if clean(target.get("propertyType")).casefold() == clean(candidate.get("propertyType")).casefold() else 0.2
    distance = _distance_km(target, candidate)
    distance_score = math.exp(-min(30, distance) / 7) if distance is not None else 0.25
    candidate_date = _parse_date(candidate.get("date"))
    as_of_date = _parse_date(as_of)
    age_years = max(0, (as_of_date - candidate_date).days / 365.2425) if candidate_date and as_of_date else 12
    recency_score = math.exp(-age_years / 6)
    market_match = 1.0 if clean(target.get("market")) == clean(candidate.get("market")) else 0.0
    target_estate = clean(target.get("estateId"))
    candidate_estate = clean(candidate.get("estateId"))
    estate_match = 1.0 if target_estate and target_estate == candidate_estate else 0.5 if not target_estate and not candidate_estate else 0.0
    street_match = 1.0 if clean(target.get("street")) and normalise_address(target.get("street")) == normalise_address(candidate.get("street")) else 0.0
    postcode_match = 1.0 if _postcode_district(target.get("postcode")) == _postcode_district(candidate.get("postcode")) else 0.0
    score = 100 * (
        0.28 * area_similarity
        + 0.15 * type_match
        + 0.14 * distance_score
        + 0.13 * recency_score
        + 0.08 * market_match
        + 0.12 * estate_match
        + 0.07 * street_match
        + 0.03 * postcode_match
    )
    return score, distance


def _weighted_median(values: Sequence[tuple[float, float]]) -> float | None:
    usable = sorted((value, max(0, weight)) for value, weight in values if value > 0 and weight > 0)
    total_weight = sum(weight for _value, weight in usable)
    if not usable or total_weight <= 0:
        return None
    midpoint = total_weight / 2
    running = 0.0
    for value, weight in usable:
        running += weight
        if running >= midpoint:
            return value
    return usable[-1][0]


def _round_estimated_value(value: float) -> int:
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


def _base_property_valuation(
    property_id: str,
    target: Mapping[str, Any],
    sales: Sequence[Mapping[str, Any]],
    universe: Mapping[str, Mapping[str, Any]],
    as_of: str,
    planning: Sequence[Mapping[str, Any]],
    epcs: Sequence[Mapping[str, Any]],
    candidate_area_stale_universe: Mapping[str, bool],
    price_floor: int,
    universe_latest_sale_date: str,
    universe_price_total: int,
) -> dict[str, Any]:
    target_area_value = _integer(target.get("floorAreaSqft")) or 0
    target_area = target_area_value if target_area_value > 0 else 0
    latest_epc_date = max((_parse_date(item.get("date")) for item in epcs if _parse_date(item.get("date"))), default=None)
    area_stale = bool(latest_epc_date and any(
        _planning_outcome(item) == "positive"
        and _planning_category(item) in {"replacement", "enlargement", "amenity"}
        and _planning_story_date(item)
        and _planning_story_date(item) > latest_epc_date
        for item in planning
    ))
    candidates: list[dict[str, Any]] = []
    as_of_date = _parse_date(as_of)
    target_type = clean(target.get("propertyType")).casefold()
    for candidate_id, candidate in universe.items():
        if candidate_id == property_id or not _number(candidate.get("price")):
            continue
        if clean(candidate.get("category")).upper() != "A":
            continue
        if not target_type or clean(candidate.get("propertyType")).casefold() != target_type:
            continue
        candidate_date = _parse_date(candidate.get("date"))
        if not candidate_date or (as_of_date and (candidate_date > as_of_date or (as_of_date - candidate_date).days > 365.2425 * 12)):
            continue
        score, distance = _valuation_comparable_score(target, candidate, as_of)
        adjusted_price = _cpih_adjusted(candidate.get("price"), candidate.get("date"), as_of)
        candidate_area_value = _number(candidate.get("floorAreaSqft")) or 0
        candidate_area = candidate_area_value if candidate_area_value > 0 else 0
        adjusted_ppsf = adjusted_price / candidate_area if adjusted_price and candidate_area else None
        candidate_epc_date = _parse_date(candidate.get("epcRegistrationDate"))
        epc_sale_gap_years = (candidate_date - candidate_epc_date).days / 365.2425 if candidate_epc_date else None
        candidate_area_stale_at_sale = bool(candidate_area_stale_universe.get(candidate_id))
        trusted_ppsf = bool(
            target_area
            and candidate_area
            and 0.5 <= candidate_area / target_area <= 2
            and epc_sale_gap_years is not None
            and -1 <= epc_sale_gap_years <= 5
            and not candidate_area_stale_at_sale
        )
        candidates.append(
            {
                "propertyId": candidate_id,
                "transactionId": clean(candidate.get("id")),
                "category": clean(candidate.get("category")).upper(),
                "date": clean(candidate.get("date"))[:10],
                "price": _integer(candidate.get("price")),
                "floorAreaSqft": _integer(candidate_area),
                "score": round(score, 1),
                "distanceKm": round(distance, 2) if distance is not None else None,
                "adjustedPrice": round(adjusted_price) if adjusted_price else None,
                "adjustedPricePerSqft": round(adjusted_ppsf) if adjusted_ppsf else None,
                "trustedPricePerSqft": trusted_ppsf,
                "areaStaleAtSale": candidate_area_stale_at_sale,
                "ageYears": round((as_of_date - candidate_date).days / 365.2425, 2) if as_of_date else None,
                "estateId": clean(candidate.get("estateId")),
                "town": clean(candidate.get("town")),
                "market": clean(candidate.get("market")),
            }
        )
    candidates.sort(key=lambda item: (item["score"], item["date"]), reverse=True)
    scope_definitions: list[tuple[str, Any]] = []
    if clean(target.get("estateId")):
        scope_definitions.append(("estate", lambda item: item["estateId"] == clean(target.get("estateId"))))
    if clean(target.get("town")):
        scope_definitions.append(("town", lambda item: item["town"].casefold() == clean(target.get("town")).casefold()))
    if clean(target.get("market")):
        scope_definitions.append(("market", lambda item: item["market"] == clean(target.get("market"))))
    scope_definitions.append(("surrey-prime", lambda _item: True))
    selected: list[dict[str, Any]] = []
    selected_scope = "surrey-prime"
    selected_window = 12
    fallback: tuple[list[dict[str, Any]], str, int] = ([], "surrey-prime", 12)
    for scope_name, scope_filter in scope_definitions:
        scoped = [item for item in candidates if scope_filter(item)]
        for window in (5, 7, 10, 12):
            windowed = [item for item in scoped if _number(item.get("ageYears")) is not None and _number(item.get("ageYears")) <= window]
            if len(windowed) > len(fallback[0]):
                fallback = (windowed, scope_name, window)
            if len(windowed) >= 5:
                selected = windowed[:12]
                selected_scope = scope_name
                selected_window = window
                break
        if selected:
            break
    if not selected:
        selected, selected_scope, selected_window = fallback
        selected = selected[:12]

    ppsf_estimates = [
        (float(item["adjustedPricePerSqft"]) * target_area, float(item["score"]) ** 2)
        for item in selected
        if target_area and item.get("adjustedPricePerSqft") and item.get("trustedPricePerSqft") and not area_stale
    ]
    comparable_price_estimates = [
        (float(item["adjustedPrice"]), float(item["score"]) ** 2)
        for item in selected
        if item.get("adjustedPrice")
    ]
    comparable_channel = "price-per-sq-ft" if len(ppsf_estimates) >= 3 else "absolute-price"
    chosen_estimates = ppsf_estimates if comparable_channel == "price-per-sq-ft" else comparable_price_estimates
    comparable_centre = _weighted_median(chosen_estimates)
    comparable_ppsf = _weighted_median([
        (float(item["adjustedPricePerSqft"]), float(item["score"]) ** 2)
        for item in selected if item.get("adjustedPricePerSqft") and item.get("trustedPricePerSqft")
    ])

    latest_sale = sales[-1] if sales else target
    own_anchor = _cpih_adjusted(latest_sale.get("price"), latest_sale.get("date"), as_of)
    latest_sale_date = _parse_date(latest_sale.get("date"))
    sale_age_years = max(0, (as_of_date - latest_sale_date).days / 365.2425) if as_of_date and latest_sale_date else 12
    latest_sale_category = clean(latest_sale.get("category")).upper()
    own_category_factor = 1.0 if latest_sale_category == "A" else 0.35 if latest_sale_category == "B" else 0.2
    own_weight = own_category_factor * math.exp(-sale_age_years / 10)
    selected_weights = [weight for _value, weight in chosen_estimates]
    effective_sample_size = (sum(selected_weights) ** 2 / sum(weight ** 2 for weight in selected_weights)) if selected_weights and sum(weight ** 2 for weight in selected_weights) else 0
    scope_factor = {"estate": 1.0, "town": 0.9, "market": 0.75, "surrey-prime": 0.6}.get(selected_scope, 0.6)
    comparable_weight = (1.0 if comparable_channel == "price-per-sq-ft" else 0.6) * min(1, effective_sample_size / 8) * scope_factor
    near_floor_price_ceiling = price_floor + NEAR_FLOOR_PRICE_MARGIN
    near_floor_price_fallback = comparable_channel == "absolute-price" and (_number(latest_sale.get("price")) or 0) <= near_floor_price_ceiling
    if near_floor_price_fallback:
        comparable_weight *= 0.35
        own_weight = max(own_weight, 0.7)
    split_signal = bool(own_anchor and comparable_centre and max(own_anchor, comparable_centre) / min(own_anchor, comparable_centre) > 1.35)
    if split_signal:
        comparable_weight *= 0.3
    if own_anchor and comparable_centre:
        total_weight = own_weight + comparable_weight
        raw_base = math.exp((own_weight * math.log(own_anchor) + comparable_weight * math.log(comparable_centre)) / total_weight) if total_weight > 0 else own_anchor
    else:
        raw_base = own_anchor or comparable_centre or _number(target.get("price")) or 0
    if own_anchor:
        if sale_age_years <= 3:
            lower, upper = 0.8, 1.3
        elif sale_age_years <= 7:
            lower, upper = 0.7, 1.5
        else:
            lower, upper = 0.6, 1.8
        raw_base = max(own_anchor * lower, min(own_anchor * upper, raw_base))
    base_estimate = _round_estimated_value(raw_base)
    confidence = "high" if comparable_channel == "price-per-sq-ft" and effective_sample_size >= 8 and own_category_factor == 1 else "medium" if effective_sample_size >= 3 else "low"
    if comparable_channel == "absolute-price" and confidence == "high":
        confidence = "medium"
    if near_floor_price_fallback:
        confidence = "low"
    if split_signal:
        confidence = "low"
    return {
        "modelVersion": VALUATION_MODEL_VERSION,
        "cpihVersion": "annual-1995-2026-v1",
        "asOf": as_of,
        "baseEstimate": base_estimate,
        "estimatedCurrentValue": base_estimate,
        "planningAdjustment": 0,
        "confidence": confidence,
        "targetFloorAreaSqft": target_area or None,
        "targetPropertyType": clean(target.get("propertyType")) or None,
        "ownSaleAnchor": round(own_anchor) if own_anchor else None,
        "ownSaleDate": clean(latest_sale.get("date"))[:10],
        "ownSalePrice": _integer(latest_sale.get("price")),
        "comparableCentre": round(comparable_centre) if comparable_centre else None,
        "comparableMedianPricePerSqft": round(comparable_ppsf) if comparable_ppsf else None,
        "comparableCount": len(chosen_estimates),
        "selectedComparableCount": len(selected),
        "comparableChannel": comparable_channel,
        "comparableScope": selected_scope,
        "comparableWindowYears": selected_window,
        "effectiveSampleSize": round(effective_sample_size, 2),
        "targetAreaStaleAfterPlanning": area_stale,
        "nearFloorPriceFallback": near_floor_price_fallback,
        "splitSignal": split_signal,
        "transactionUniverseCount": len(universe),
        "transactionUniverseLatestSaleDate": universe_latest_sale_date,
        "transactionUniversePriceTotal": universe_price_total,
        "comparables": [
            {
                **item,
                "usedInChannel": bool(
                    item.get("adjustedPrice")
                    if comparable_channel == "absolute-price"
                    else item.get("trustedPricePerSqft") and not area_stale
                ),
            }
            for item in selected
        ],
    }


def _planning_added_area_sqft(application: Mapping[str, Any]) -> int | None:
    if _planning_category(application) != "enlargement":
        return None
    explicit_sqft = _number(
        application.get("additionalFloorAreaSqft")
        or application.get("addedAreaSqft")
        or application.get("floorAreaIncreaseSqft")
    )
    explicit_sqm = _number(
        application.get("additionalFloorAreaSqm")
        or application.get("addedAreaSqm")
        or application.get("floorAreaIncreaseSqm")
    )
    values: list[float] = []
    if explicit_sqft:
        values.append(explicit_sqft)
    if explicit_sqm:
        values.append(explicit_sqm * 10.7639)

    existing_sqft = _number(application.get("existingFloorAreaSqft"))
    proposed_sqft = _number(application.get("proposedFloorAreaSqft"))
    existing_sqm = _number(application.get("existingFloorAreaSqm"))
    proposed_sqm = _number(application.get("proposedFloorAreaSqm"))
    if existing_sqft and proposed_sqft and proposed_sqft > existing_sqft:
        values.append(proposed_sqft - existing_sqft)
    if existing_sqm and proposed_sqm and proposed_sqm > existing_sqm:
        values.append((proposed_sqm - existing_sqm) * 10.7639)

    proposal = clean(application.get("proposal")).lower().replace(",", "")
    unit_pattern = r"(sq\.?\s*ft|sqft|square feet|square foot|sq\.?\s*m|sqm|m2|m²|square metres?|square meters?)"

    def to_sqft(value: str, unit: str) -> float:
        amount = float(value)
        return amount * 10.7639 if re.search(r"(?:sq\.?\s*m|sqm|m2|m²|metre|meter)", unit, re.I) else amount

    for match in re.finditer(
        rf"\b(?:from|existing(?: floor area)?(?: of)?)\s*(\d+(?:\.\d+)?)\s*{unit_pattern}?"
        rf".{{0,80}}?\b(?:to|proposed(?: floor area)?(?: of)?)\s*(\d+(?:\.\d+)?)\s*{unit_pattern}\b",
        proposal,
        re.I,
    ):
        before_unit = match.group(2) or match.group(4)
        before = to_sqft(match.group(1), before_unit)
        after = to_sqft(match.group(3), match.group(4))
        if after > before:
            values.append(after - before)

    incremental_prefix = (
        r"(?:additional|adding|adds?|added|another|"
        r"increase(?:d|s|ing)?(?:\s+(?:in|of)\s+(?:the\s+)?(?:gross\s+|total\s+)?floor area)?\s+(?:by|of)|"
        r"extend(?:ed|ing)?\s+by|extension\s+of)"
    )
    for match in re.finditer(rf"\b{incremental_prefix}\s*(\d+(?:\.\d+)?)\s*{unit_pattern}\b", proposal, re.I):
        values.append(to_sqft(match.group(1), match.group(2)))
    for match in re.finditer(rf"\b(\d+(?:\.\d+)?)\s*{unit_pattern}\s+(?:extension|addition)\b", proposal, re.I):
        values.append(to_sqft(match.group(1), match.group(2)))

    plausible = [round(value) for value in values if 100 <= value <= 10_000]
    return max(plausible) if plausible else None


def _planning_value_adjustment(
    valuation: Mapping[str, Any],
    planning: Sequence[Mapping[str, Any]],
    epcs: Sequence[Mapping[str, Any]],
    sales: Sequence[Mapping[str, Any]],
) -> tuple[int, dict[str, Any]]:
    base = _number(valuation.get("baseEstimate")) or 0
    if not base or not planning:
        return 0, {}
    latest_epc_date = max((_parse_date(item.get("date")) for item in epcs if _parse_date(item.get("date"))), default=None)
    latest_sale_date = _parse_date(sales[-1].get("date")) if sales else None
    evidence_date = max((item for item in (latest_epc_date, latest_sale_date) if item), default=None)
    structural_positive = [
        item for item in planning
        if _planning_category(item) in {"replacement", "enlargement", "amenity"}
        and _planning_outcome(item) == "positive"
        and (not evidence_date or (_planning_story_date(item) and _planning_story_date(item) > evidence_date))
    ]
    if not structural_positive:
        return 0, {}
    earliest_positive = min((_planning_story_date(item) for item in structural_positive if _planning_story_date(item)), default=None)
    implementation = [
        item for item in planning
        if (_planning_outcome(item) == "implementation" or _planning_category(item) == "implementation")
        and (not earliest_positive or (_planning_story_date(item) and _planning_story_date(item) >= earliest_positive))
    ]
    added_area = max((_planning_added_area_sqft(item) or 0 for item in structural_positive), default=0)
    comparable_ppsf = _number(valuation.get("comparableMedianPricePerSqft")) or 0
    if added_area and comparable_ppsf:
        completion_weight = 0.6 if implementation else 0.25
        raw_adjustment = added_area * comparable_ppsf * completion_weight
    else:
        strongest_category = max(
            (_planning_category(item) for item in structural_positive),
            key=lambda category: {"replacement": 3, "enlargement": 2, "amenity": 1}.get(category, 0),
        )
        rate = {"replacement": 0.04, "enlargement": 0.03, "amenity": 0.015}.get(strongest_category, 0.015)
        if implementation:
            rate += 0.025
        raw_adjustment = base * min(0.08, rate)
    adjustment = _round_estimated_value(min(base * 0.25, raw_adjustment))
    signals = {
        "postEvidenceApprovedSchemeCount": len(structural_positive),
        "implementationSignalCount": len(implementation),
        "references": [
            clean(item.get("reference") or item.get("signature"))
            for item in structural_positive
            if clean(item.get("reference") or item.get("signature"))
        ],
    }
    if added_area:
        signals["approvedAdditionalAreaSqft"] = added_area
    return adjustment, signals


_STORY_STATIONS = (
    ("Woking", -0.5569, 51.3185, "London Waterloo", 24),
    ("Guildford", -0.5805, 51.2369, "London Waterloo", 32),
    ("Weybridge", -0.4577, 51.3618, "London Waterloo", 29),
    ("Esher", -0.3532, 51.3790, "London Waterloo", 23),
    ("Cobham & Stoke d'Abernon", -0.3899, 51.3180, "London Waterloo", 38),
    ("Oxshott", -0.3622, 51.3364, "London Waterloo", 35),
    ("Walton-on-Thames", -0.4146, 51.3729, "London Waterloo", 25),
    ("Hersham", -0.3899, 51.3769, "London Waterloo", 30),
    ("Virginia Water", -0.5623, 51.4018, "London Waterloo", 43),
    ("Egham", -0.5465, 51.4296, "London Waterloo", 39),
    ("Staines", -0.5039, 51.4322, "London Waterloo", 35),
    ("Farnborough Main", -0.7557, 51.2967, "London Waterloo", 34),
    ("Farnham", -0.7928, 51.2119, "London Waterloo", 53),
    ("Godalming", -0.6188, 51.1866, "London Waterloo", 41),
    ("Haslemere", -0.7190, 51.0888, "London Waterloo", 49),
    ("Dorking", -0.3241, 51.2409, "London Victoria / Waterloo", 49),
    ("Leatherhead", -0.3332, 51.2988, "London Waterloo / Victoria", 43),
    ("Ashtead", -0.3088, 51.3175, "London Waterloo / Victoria", 38),
    ("Epsom", -0.2693, 51.3343, "London Waterloo / Victoria", 34),
    ("Redhill", -0.1658, 51.2402, "London Bridge / Victoria", 29),
    ("Reigate", -0.2038, 51.2419, "London Victoria / London Bridge", 42),
    ("Gatwick Airport", -0.1610, 51.1566, "London Victoria / London Bridge", 29),
    ("Horley", -0.1610, 51.1688, "London Victoria / London Bridge", 34),
    ("Oxted", -0.0048, 51.2579, "London Victoria / London Bridge", 33),
    ("West Byfleet", -0.5055, 51.3392, "London Waterloo", 31),
)
_STORY_AIRPORTS = (
    ("Heathrow Airport", -0.4543, 51.4700, 22),
    ("Gatwick Airport", -0.1821, 51.1537, 30),
    ("Farnborough Airport", -0.7763, 51.2758, 18),
)


def _story_distance(value: float) -> str:
    return f"{round(value * 1000):,}m" if value < 1 else f"{value:.1f}km"


def _property_story_context_signal(
    target: Mapping[str, Any],
    coverage: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    geocode = target.get("geocode") if isinstance(target.get("geocode"), Mapping) else {}
    precision = clean(target.get("coordinatePrecision") or geocode.get("precision")).casefold()
    nhle_designation_point = (
        clean(target.get("coordinateSource")) == HISTORIC_ENGLAND_SOURCE
        and precision == "confirmed-nhle-designation-location"
    )
    approximate_point_label = (
        "NHLE designation map point"
        if nhle_designation_point
        else "postcode-centroid map point"
    )
    approximate_point_article = "an" if nhle_designation_point else "a"
    exact_point = ("exact" in precision or "address" in precision) and "postcode" not in precision

    estate_name = clean(target.get("estate"))
    if estate_name and clean(target.get("estateEvidenceStatus")).casefold() == "verified":
        candidates.append(
            {
                "score": 9,
                "kind": "private_estate",
                "text": f"Its place within {estate_name} is likely to underpin scarcity and buyer appeal.",
            }
        )

    constraints = target.get("planningConstraints") if isinstance(target.get("planningConstraints"), Mapping) else {}
    flood_zone_text = clean(constraints.get("floodRiskZone"))
    flood_zones = [int(value) for value in re.findall(r"(?:zone|/)\s*(2|3)\b", flood_zone_text, re.I)]
    if flood_zones:
        zone = max(flood_zones)
        subject = "The property" if exact_point else "The wider postcode setting"
        candidates.append(
            {
                "score": 9 if exact_point else 7,
                "kind": "flood_risk",
                "coverageSource": "planningConstraints",
                "text": f"{subject} carries a mapped Flood Zone {zone} flag, which may narrow buyer appetite and weigh on resale.",
            }
        )
    if constraints.get("aonb") and constraints.get("greenBelt"):
        candidates.append(
            {
                "score": 8 if exact_point else 5,
                "kind": "landscape",
                "coverageSource": "planningConstraints",
                "text": (
                    "Set within the Surrey Hills National Landscape and Green Belt, reinforcing rural scarcity and lifestyle appeal."
                    if exact_point
                    else "The wider postcode setting sits within the Surrey Hills National Landscape and Green Belt, reinforcing rural scarcity and lifestyle appeal."
                ),
            }
        )
    elif constraints.get("conservationArea"):
        candidates.append(
            {
                "score": 7 if exact_point else 5,
                "kind": "conservation_area",
                "coverageSource": "planningConstraints",
                "text": (
                    "Its conservation-area setting reinforces character and makes the planning history especially relevant."
                    if exact_point
                    else "The wider conservation-area setting reinforces character and makes the planning history especially relevant."
                ),
            }
        )

    ofsted = target.get("ofsted") if isinstance(target.get("ofsted"), Mapping) else {}
    schools = ofsted.get("nearestSchools") if isinstance(ofsted.get("nearestSchools"), list) else []
    school = schools[0] if schools and isinstance(schools[0], Mapping) else {}
    school_metres = _number(school.get("metres")) or 0
    if clean(school.get("name")) and 0 < school_metres <= (1000 if exact_point else 500):
        candidates.append(
            {
                "score": 6,
                "kind": "school_proximity",
                "coverageSource": "schools",
                "text": (
                    f"{clean(school.get('name'))} is close by at around {school_metres:,.0f}m, a location advantage likely to support family-buyer demand."
                    if exact_point
                    else f"The postcode-centroid map point places {clean(school.get('name'))} around {school_metres:,.0f}m away, a wider-area location signal for family buyers."
                ),
            }
        )

    current_flood = target.get("environmentAgency") if isinstance(target.get("environmentAgency"), Mapping) else {}
    current_alert_count = max(0, _integer(current_flood.get("currentFloodAlertCount")) or 0)
    if current_alert_count and coverage.get("currentFlood", {}).get("status") == "complete":
        alert_noun = "alert" if current_alert_count == 1 else "alerts"
        observed_date = clean(current_flood.get("observedAt") or current_flood.get("updatedAt"))[:10]
        date_text = f" on {_format_date(observed_date)}" if observed_date else ""
        candidates.append(
            {
                "score": 10,
                "kind": "current_flood_alert",
                "coverageSource": "currentFlood",
                "text": (
                    f"The Environment Agency's wider-area check recorded {current_alert_count} current flood {alert_noun} "
                    f"within {clean(current_flood.get('searchRadius')) or 'the configured radius'}{date_text}."
                ),
            }
        )

    if _valuation_point(target):
        stations = []
        for name, longitude, latitude, london, fastest_minutes in _STORY_STATIONS:
            distance = _distance_km(target, {"longitude": longitude, "latitude": latitude})
            if distance is not None:
                stations.append((distance, name, london, fastest_minutes))
        if stations:
            distance, name, london, fastest_minutes = min(stations)
            if distance <= (2 if exact_point else 1):
                candidates.append(
                    {
                        "score": 7,
                        "kind": "station_proximity",
                        "coverageSource": "coordinates",
                        "text": (
                            f"{name} station lies around {_story_distance(distance)} away, with {london} services listed at roughly {fastest_minutes} min."
                            if exact_point
                            else f"The {approximate_point_label} lies around {_story_distance(distance)} from {name} station, with {london} services listed at roughly {fastest_minutes} min."
                        ),
                        "compactText": (
                            f"around {_story_distance(distance)} from {name} station ({london} from {fastest_minutes} min)"
                            if exact_point
                            else f"served by {approximate_point_article} {approximate_point_label} around {_story_distance(distance)} from {name} station ({london} from {fastest_minutes} min)"
                        ),
                    }
                )

        airports = []
        for name, longitude, latitude, drive_base_minutes in _STORY_AIRPORTS:
            distance = _distance_km(target, {"longitude": longitude, "latitude": latitude})
            if distance is not None:
                airports.append((round(drive_base_minutes + distance * 1.35), name))
        if airports:
            drive_minutes, name = min(airports)
            if drive_minutes <= 30:
                candidates.append(
                    {
                        "score": 5,
                        "kind": "airport_proximity",
                        "coverageSource": "coordinates",
                        "text": (
                            f"{name} is the closest tracked air link, at an indicative drive of about {drive_minutes} min."
                            if exact_point
                            else f"From the {approximate_point_label}, {name} is the closest tracked air link, at an indicative drive of about {drive_minutes} min."
                        ),
                    }
                )

    candidates.sort(key=lambda candidate: candidate["score"], reverse=True)
    if not candidates:
        return None
    first = dict(candidates[0])
    if first.get("kind") == "private_estate":
        station = next(
            (candidate for candidate in candidates[1:] if candidate.get("kind") == "station_proximity" and candidate["score"] >= 6),
            None,
        )
        if station:
            first["text"] = f"Within {estate_name}, it is {station['compactText']}, reinforcing scarcity and practical access."
            first["secondaryKind"] = station["kind"]
    return first


def _build_fact_packet(
    property_id: str,
    as_of: str,
    target: Mapping[str, Any],
    sales: Sequence[Mapping[str, Any]],
    planning: Sequence[Mapping[str, Any]],
    epcs: Sequence[Mapping[str, Any]],
    coverage: Mapping[str, Mapping[str, Any]],
    event_evidence_ids: Mapping[str, list[str]],
    valuation: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {
        "salesCount": len(sales),
        "planningApplicationCount": len(planning) if planning or coverage["planning"]["complete"] else None,
        "epcObservationCount": len(epcs),
        "backgroundSourceCoverage": {
            source_key: clean(coverage[source_key].get("status"))
            for source_key in BACKGROUND_COVERAGE_SOURCES
        },
    }
    limitations = [
        "Registered sales are Price Paid records, not ownership or legal-title history.",
        "No causal attribution of a value change is made.",
        "The estimated current value is an automated evidence-led indication, not a formal RICS valuation.",
    ]

    background_coverage_evidence = sorted({
        evidence_id
        for source_key in BACKGROUND_COVERAGE_SOURCES
        for evidence_id in coverage[source_key].get("evidenceIds", [])
        if evidence_id
    })
    facts.append(
        _fact(
            property_id,
            "background_source_coverage",
            "Public background-source coverage is recorded separately for mapping, current flood alerts, static constraints, statutory listed-building evidence, schools, current nearby planning and OS UPRN candidates.",
            background_coverage_evidence,
            {
                source_key: {
                    key: coverage[source_key].get(key)
                    for key in (
                        "status",
                        "complete",
                        "coverageMode",
                        "recordCount",
                        "resultStatus",
                        "designationStatus",
                    )
                    if key in coverage[source_key]
                }
                for source_key in BACKGROUND_COVERAGE_SOURCES
            },
        )
    )

    context_signal = _property_story_context_signal(target, coverage)
    if context_signal:
        context_evidence_ids = list(event_evidence_ids.get("property_context", []))
        coverage_source = clean(context_signal.get("coverageSource"))
        if coverage_source in coverage:
            context_evidence_ids.extend(coverage[coverage_source].get("evidenceIds", []))
        facts.append(
            _fact(
                property_id,
                "context_highlight",
                context_signal["text"],
                sorted(set(context_evidence_ids)),
                context_signal,
                "high" if context_signal["score"] >= 8 else "medium",
            )
        )

    sales_evidence = event_evidence_ids.get("sale", [])
    if sales:
        earliest, latest = sales[0], sales[-1]
        metrics.update(
            {
                "firstSaleDate": earliest.get("date"),
                "firstSalePrice": earliest.get("price"),
                "latestSaleDate": latest.get("date"),
                "latestSalePrice": latest.get("price"),
            }
        )
        count_text = "one matched sale" if len(sales) == 1 else f"{len(sales)} matched sales"
        coverage_from = clean(coverage["sales"].get("coverageFrom")) or "the loaded coverage period"
        facts.append(
            _fact(
                property_id,
                "sales_history",
                f"HM Land Registry Price Paid Data records {count_text} from {coverage_from} onward.",
                sales_evidence + coverage["sales"].get("evidenceIds", []),
                {"count": len(sales), "coverageFrom": coverage_from},
            )
        )
        if len(sales) == 1:
            facts.append(
                _fact(
                    property_id,
                    "latest_sale",
                    f"The matched record is {_format_money(latest.get('price'))} on {_format_date(latest.get('date'))}.",
                    sales_evidence,
                    {"date": latest.get("date"), "price": latest.get("price")},
                )
            )
        if len(sales) > 1 and _number(earliest.get("price")) and _number(latest.get("price")):
            change = (_number(latest["price"]) / _number(earliest["price"]) - 1) * 100
            holding = _years_between(earliest.get("date"), latest.get("date"))
            metrics["nominalPriceChangePercent"] = round(change, 1)
            metrics["saleSpanYears"] = holding
            facts.append(
                _fact(
                    property_id,
                    "nominal_price_change",
                    (
                        f"The matched sale price changed from {_format_money(earliest.get('price'))} on {_format_date(earliest.get('date'))} "
                        f"to {_format_money(latest.get('price'))} on {_format_date(latest.get('date'))}, "
                        f"a nominal change of {change:+.1f}%"
                        + (f" over {holding:.1f} years." if holding is not None else ".")
                    ),
                    sales_evidence,
                    {"percent": round(change, 1), "years": holding, "basis": "nominal"},
                )
            )

    planning_evidence = event_evidence_ids.get("planning_application", [])
    if planning:
        positive_count = sum(_planning_outcome(item) == "positive" for item in planning)
        implementation_count = sum(_planning_outcome(item) == "implementation" for item in planning)
        facts.append(
            _fact(
                property_id,
                "planning_history",
                f"The property-level source contains {len(planning)} matched planning application{'s' if len(planning) != 1 else ''}.",
                planning_evidence + coverage["planning"].get("evidenceIds", []),
                {
                    "count": len(planning),
                    "explicitPositiveCount": positive_count,
                    "implementationSignalCount": implementation_count,
                },
                "high" if coverage["planning"]["complete"] else "medium",
            )
        )
        limitations.append("Planning applications and decisions do not prove that proposed or approved work was built.")
    elif coverage["planning"]["status"] == "checked_none" and coverage["planning"]["complete"]:
        facts.append(
            _fact(
                property_id,
                "planning_checked_none",
                "A completed property-level full-history check returned no matched planning applications in the source's available archive.",
                coverage["planning"].get("evidenceIds", []),
                {"count": 0, "coverageMode": "full-available-history"},
            )
        )
    else:
        limitations.append("Property-level planning coverage is not complete, so the absence of a matched application cannot be treated as evidence of no planning history.")

    epc_evidence = event_evidence_ids.get("epc_certificate", [])
    dated_epcs = [item for item in epcs if _parse_date(item.get("date"))]
    area_epcs = [item for item in dated_epcs if _integer(item.get("floorAreaSqft"))]
    latest_epc = dated_epcs[-1] if dated_epcs else (epcs[-1] if epcs else None)
    if latest_epc:
        metrics["latestEpcDate"] = latest_epc.get("date")
        metrics["latestFloorAreaSqft"] = _integer(latest_epc.get("floorAreaSqft"))
        metrics["latestEpcRating"] = latest_epc.get("rating")
        parts = []
        if _integer(latest_epc.get("floorAreaSqft")):
            parts.append(f"{_integer(latest_epc.get('floorAreaSqft')):,} sq ft")
        if latest_epc.get("rating"):
            parts.append(f"rating {latest_epc['rating']}")
        observed_on = f" on {_format_date(latest_epc.get('date'))}" if latest_epc.get("date") else ""
        facts.append(
            _fact(
                property_id,
                "latest_epc",
                f"The latest matched EPC observation{observed_on} records "
                + (" and ".join(parts) if parts else "a certificate without a usable floor-area observation")
                + ".",
                epc_evidence,
                {
                    "date": latest_epc.get("date"),
                    "floorAreaSqft": _integer(latest_epc.get("floorAreaSqft")),
                    "rating": latest_epc.get("rating"),
                },
                "medium",
            )
        )
    if len(area_epcs) > 1:
        earliest_area, latest_area = area_epcs[0], area_epcs[-1]
        first_area = _integer(earliest_area.get("floorAreaSqft"))
        last_area = _integer(latest_area.get("floorAreaSqft"))
        if first_area and last_area and first_area != last_area:
            metrics["observedEpcFloorAreaChangeSqft"] = last_area - first_area
            facts.append(
                _fact(
                    property_id,
                    "epc_floor_area_observations",
                    (
                        f"Matched EPC observations record {first_area:,} sq ft on {_format_date(earliest_area.get('date'))} "
                        f"and {last_area:,} sq ft on {_format_date(latest_area.get('date'))}. "
                        "The EPC evidence alone does not establish when, why, or whether building work caused the difference."
                    ),
                    epc_evidence,
                    {"firstSqft": first_area, "latestSqft": last_area, "changeSqft": last_area - first_area},
                    "medium",
                )
            )

    valuation_evidence = event_evidence_ids.get("valuation_model", [])
    estimated_value = _integer(valuation.get("estimatedCurrentValue"))
    if estimated_value:
        metrics.update(
            {
                "estimatedCurrentValue": estimated_value,
                "valuationBaseEstimate": _integer(valuation.get("baseEstimate")),
                "valuationPlanningAdjustment": _integer(valuation.get("planningAdjustment")),
                "valuationComparableCount": _integer(valuation.get("comparableCount")),
                "valuationModelVersion": clean(valuation.get("modelVersion")),
            }
        )
        facts.append(
            _fact(
                property_id,
                "estimated_current_value",
                f"The evidence-led current value estimate is {_format_money(estimated_value)} as at {_format_date(valuation.get('asOf'))}.",
                valuation_evidence + event_evidence_ids.get("sale", []) + event_evidence_ids.get("epc_certificate", []),
                dict(valuation),
                clean(valuation.get("confidence")) or "medium",
            )
        )

    packet = {
        "version": 1,
        "asOf": as_of,
        "facts": facts,
        "limitations": sorted(set(limitations)),
    }
    story = _story_from_facts(property_id, facts, packet["limitations"], sales, planning, epcs, coverage, valuation)
    return packet, metrics, story


def _story_from_facts(
    property_id: str,
    facts: Sequence[Mapping[str, Any]],
    limitations: Sequence[str],
    sales: Sequence[Mapping[str, Any]],
    planning: Sequence[Mapping[str, Any]],
    epcs: Sequence[Mapping[str, Any]],
    coverage: Mapping[str, Mapping[str, Any]],
    valuation: Mapping[str, Any],
) -> dict[str, Any]:
    fact_by_type = {str(fact.get("type")): fact for fact in facts}
    claims: list[dict[str, Any]] = []

    def add_claim(text: str, fact_types: Sequence[str], confidence: str = "high") -> None:
        supporting = [fact_by_type[fact_type] for fact_type in fact_types if fact_type in fact_by_type]
        evidence_ids = sorted({
            evidence_id
            for fact in supporting
            for evidence_id in fact.get("evidenceIds", [])
            if evidence_id
        })
        if not evidence_ids:
            return
        claims.append(
            {
                "claimId": _stable_id("claim", property_id, text, evidence_ids),
                "text": text,
                "evidenceIds": evidence_ids,
                "confidence": confidence,
            }
        )

    history_sentences: list[str] = []
    history_fact_types = ["sales_history"]
    if sales:
        earliest, latest = sales[0], sales[-1]
        if len(sales) == 1:
            coverage_from = clean((fact_by_type.get("sales_history") or {}).get("value", {}).get("coverageFrom")) or "1995"
            valuation_date = _parse_date(valuation.get("asOf"))
            sale_date = _parse_date(latest.get("date"))
            age = (valuation_date - sale_date).days / 365.2425 if valuation_date and sale_date else 0
            if age >= 15:
                history_sentences.append(
                    f"A long-held house: last sold for {_format_money(latest.get('price'))} in {str(latest.get('date'))[:4]}, with no later Price Paid sale."
                )
            else:
                history_sentences.append(
                    f"The Price Paid record since {coverage_from[:4]} shows one sale: {_format_money(latest.get('price'))} in {str(latest.get('date'))[:4]}."
                )
            history_fact_types.append("latest_sale")
        else:
            count_phrase = "twice" if len(sales) == 2 else f"{len(sales)} times"
            price_change = (_number(latest.get("price")) / _number(earliest.get("price")) - 1) * 100 if _number(latest.get("price")) and _number(earliest.get("price")) else None
            span = _years_between(earliest.get("date"), latest.get("date"))
            movement = ""
            if price_change is not None:
                direction = "rise" if price_change >= 0 else "fall"
                if span is not None:
                    rounded_span = max(1, round(span))
                    span_text = f" over {rounded_span} year{'s' if rounded_span != 1 else ''}"
                else:
                    span_text = ""
                movement = f"—a {abs(price_change):.0f}% nominal {direction}{span_text}"
            history_sentences.append(
                f"Sold {count_phrase}: {_format_money(earliest.get('price'))} in {str(earliest.get('date'))[:4]} to {_format_money(latest.get('price'))} in {str(latest.get('date'))[:4]}{movement}."
            )
            history_fact_types.append("nominal_price_change")

    epc_growth = _material_epc_growth(epcs)
    episode = _best_planning_episode(sales, planning, epc_growth)
    if episode:
        applications = episode["applications"]
        positives = [item for item in applications if _planning_outcome(item) == "positive"]
        follow_ons = [
            item for item in applications
            if _planning_outcome(item) == "implementation" or _planning_category(item) == "implementation"
        ]
        highlights = _planning_highlights(applications)
        start_year = str(episode["earlierSale"].get("date"))[:4]
        end_year = str(episode["laterSale"].get("date"))[:4]
        if follow_ons and len(follow_ons) == len(applications):
            lead = "One planning follow-on record" if len(applications) == 1 else f"A {len(applications)}-record planning follow-on sequence"
        elif positives and len(positives) == len(applications):
            lead = "One approved planning application" if len(applications) == 1 else f"{len(applications)} approved planning applications"
        elif positives:
            lead = f"{len(applications)} planning applications, including {len(positives)} explicit approval{'s' if len(positives) != 1 else ''},"
        else:
            lead = "One planning record" if len(applications) == 1 else f"A {len(applications)}-record planning sequence"
        project = f", led by {_join_story_items(highlights)}" if highlights else ""
        planning_sentence = f"{lead} between {start_year} and {end_year}{project}"
        if epc_growth and episode["areaAligned"]:
            history_sentences.append(planning_sentence + ".")
            history_sentences.append(
                f"EPC floor area then rose from {epc_growth['from']['floorAreaSqft']:,} sq ft in {epc_growth['from']['date'].year} "
                f"to {epc_growth['to']['floorAreaSqft']:,} sq ft in {epc_growth['to']['date'].year}; together, the sequence suggests the house was materially enlarged before resale."
            )
            history_fact_types.append("epc_floor_area_observations")
        elif episode.get("structural"):
            price_change = episode.get("priceChangePercent")
            contribution = f" behind the later {abs(price_change):.0f}% {'uplift' if price_change >= 0 else 'price movement'}" if price_change is not None else " before resale"
            history_sentences.append(f"{planning_sentence}; the sequence suggests a development-led chapter{contribution}, alongside market movement.")
        elif follow_ons:
            history_sentences.append(planning_sentence + ", showing an earlier permission moving through delivery details.")
        else:
            history_sentences.append(planning_sentence + ".")
        history_fact_types.append("planning_history")
    elif planning:
        highlights = _planning_highlights(planning)
        positive_count = sum(_planning_outcome(item) == "positive" for item in planning)
        follow_on_count = sum(
            _planning_outcome(item) == "implementation" or _planning_category(item) == "implementation"
            for item in planning
        )
        maintenance_count = sum(_planning_category(item) == "maintenance" for item in planning)
        application_dates = [date_value for item in planning if (date_value := _planning_story_date(item))]
        latest_sale_date = _parse_date(sales[-1].get("date")) if sales else None
        timing = "around its recorded history"
        if application_dates and latest_sale_date and min(application_dates) > latest_sale_date:
            timing = "since its latest sale"
        elif application_dates and latest_sale_date and max(application_dates) < latest_sale_date:
            timing = "before its latest sale"
        focus = f", focused on {_join_story_items(highlights)}" if highlights else ""
        if follow_on_count == len(planning):
            history_sentences.append(
                f"Its {len(planning)} follow-on planning record{'s' if len(planning) != 1 else ''} {timing}{focus}, showing an earlier permission moving through delivery details."
            )
        elif maintenance_count == len(planning):
            history_sentences.append(
                f"Its {len(planning)} planning record{'s' if len(planning) != 1 else ''} {timing}{focus}, reflecting ongoing management of the house and grounds."
            )
        elif follow_on_count + maintenance_count == len(planning):
            history_sentences.append(
                f"Its {len(planning)} planning records {timing}, covering delivery details and ongoing management of the grounds."
            )
        else:
            noun = "planning application" if len(planning) == 1 else "planning applications"
            if positive_count:
                approval_noun = "explicit approval" if positive_count == 1 else "explicit approvals"
                focus_text = f" and focus on {_join_story_items(highlights)}" if highlights else ""
                verb = "includes" if len(planning) == 1 else "include"
                history_sentences.append(
                    f"Its {len(planning)} {noun} {timing} {verb} {positive_count} {approval_noun}{focus_text}, pointing to an active improvement chapter."
                )
            else:
                history_sentences.append(
                    f"Its {len(planning)} {noun} {timing}{focus}, pointing to an active improvement chapter."
                )
        history_fact_types.append("planning_history")
    elif coverage["planning"]["status"] == "checked_none" and coverage["planning"]["complete"]:
        if len(sales) == 1:
            history_sentences.append("With no property-linked applications in the completed planning search, its recent story is one of long ownership rather than documented redevelopment.")
        else:
            history_sentences.append("No property-linked applications appear in the completed planning search, so the record reads as market resale rather than a documented redevelopment cycle.")
        history_fact_types.append("planning_checked_none")

    latest_epc = max((item for item in epcs if _parse_date(item.get("date"))), key=lambda item: clean(item.get("date")), default=None)
    if latest_epc and not (epc_growth and episode and episode.get("areaAligned")):
        area = _integer(latest_epc.get("floorAreaSqft"))
        if area:
            history_sentences.append(f"The latest EPC records {area:,} sq ft.")
            history_fact_types.append("latest_epc")

    context_fact = fact_by_type.get("context_highlight") or {}
    context_score = _number((context_fact.get("value") or {}).get("score")) if isinstance(context_fact.get("value"), Mapping) else None
    if clean(context_fact.get("text")) and (context_score or 0) >= 5:
        history_sentences.append(clean(context_fact.get("text")))
        history_fact_types.append("context_highlight")

    history_paragraph = " ".join(sentence for sentence in history_sentences if sentence).strip()
    if not history_paragraph:
        history_paragraph = "The house's recorded history begins with its latest Price Paid transaction."
    add_claim(history_paragraph, history_fact_types, "medium" if planning or epc_growth else "high")

    estimated_value = _integer(valuation.get("estimatedCurrentValue")) or _integer(valuation.get("baseEstimate"))
    comparable_count = _integer(valuation.get("comparableCount")) or 0
    target_area = _integer(valuation.get("targetFloorAreaSqft")) or 0
    planning_adjustment = _integer(valuation.get("planningAdjustment")) or 0
    valuation_basis = []
    comparable_channel = clean(valuation.get("comparableChannel"))
    area_stale = bool(valuation.get("targetAreaStaleAfterPlanning"))
    if comparable_count:
        valuation_basis.append(
            f"{comparable_count} floor-area comparable sales"
            if comparable_channel == "price-per-sq-ft"
            else f"{comparable_count} recent comparable sales"
        )
    if target_area:
        valuation_basis.append(
            f"the {target_area:,} sq ft EPC as the pre-scheme baseline"
            if area_stale
            else f"the {target_area:,} sq ft EPC"
        )
    if valuation.get("ownSaleAnchor"):
        valuation_basis.append("inflation-adjusted sale history")
    valuation_sentence = "The estimate uses " + _join_story_items(valuation_basis) + "." if valuation_basis else "The estimate is anchored to the available sale evidence."
    if planning_adjustment:
        planning_signals = valuation.get("planningSignals") if isinstance(valuation.get("planningSignals"), Mapping) else {}
        added_area = _integer(planning_signals.get("approvedAdditionalAreaSqft"))
        scheme = f"the approved {added_area:,} sq ft post-EPC scheme" if added_area else "approved post-EPC development"
        valuation_sentence += f" It adds {_format_money(planning_adjustment)} for {scheme}."
    valuation_paragraph = f"{valuation_sentence} Estimated current value — {_format_money(estimated_value)}."
    add_claim(
        valuation_paragraph,
        ["estimated_current_value", "planning_history"] if planning_adjustment else ["estimated_current_value"],
        clean(valuation.get("confidence")) or "medium",
    )

    paragraphs = [history_paragraph, valuation_paragraph]
    return {
        "text": "\n\n".join(paragraphs),
        "paragraphs": paragraphs,
        "claims": claims,
        "generator": {
            "type": "deterministic",
            "version": GENERATOR_VERSION,
            "model": None,
        },
        "limitations": list(limitations),
        "valuation": dict(valuation),
        "status": "ready" if claims else "fallback",
    }


def _property_profile(
    rows: Sequence[Mapping[str, Any]],
    context_target: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    latest = max(rows, key=lambda item: (clean(item.get("date")), _integer(item.get("price")) or 0))
    context_source = context_target or latest
    profile = {
        "paon": clean(latest.get("paon")),
        "saon": clean(latest.get("saon")),
        "street": clean(latest.get("street")),
        "locality": clean(latest.get("locality")),
        "town": clean(latest.get("town")),
        "district": clean(latest.get("district")),
        "market": clean(latest.get("market")),
        "propertyType": clean(latest.get("propertyType")),
        "estateId": clean(latest.get("estateId")),
        "estate": clean(latest.get("estate")),
    }
    profile = {key: value for key, value in profile.items() if value not in (None, "")}
    context_keys = (
        "latitude",
        "longitude",
        "coordinateSource",
        "coordinatePrecision",
        "geocode",
        "ordnanceSurvey",
        "ofsted",
        "planningConstraints",
        "historicEngland",
        "environmentAgency",
    )
    context = {
        key: copy.deepcopy(context_source[key])
        for key in context_keys
        if context_source.get(key) not in (None, "", [], {})
    }
    proximity: dict[str, Any] = {}
    ofsted = context_source.get("ofsted")
    if isinstance(ofsted, Mapping) and isinstance(ofsted.get("nearestSchools"), list):
        proximity["schools"] = copy.deepcopy(ofsted["nearestSchools"])
    for source_key in ("nearestAirports", "airports", "nearestAirport"):
        if context_source.get(source_key) not in (None, "", [], {}):
            proximity[source_key] = copy.deepcopy(context_source[source_key])
    if proximity:
        context["proximity"] = proximity
    return profile, context


def _normalise_prior_records(prior_records: Any) -> dict[str, Mapping[str, Any]]:
    if isinstance(prior_records, Mapping):
        return {str(key): value for key, value in prior_records.items() if isinstance(value, Mapping)}
    if isinstance(prior_records, list):
        return {
            str(item.get("propertyId")): item
            for item in prior_records
            if isinstance(item, Mapping) and item.get("propertyId")
        }
    return {}


def build_property_records(
    transactions: Sequence[Mapping[str, Any]],
    transaction_meta: Mapping[str, Any] | None = None,
    sales_history: Mapping[str, Any] | None = None,
    sales_meta: Mapping[str, Any] | None = None,
    planning_history: Mapping[str, Any] | None = None,
    planning_meta: Mapping[str, Any] | None = None,
    prior_records: Mapping[str, Mapping[str, Any]] | Sequence[Mapping[str, Any]] | None = None,
    as_of: str | None = None,
    generated_at: str | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Build property-grain records and feed metadata from local JS data.

    All arguments are already-parsed JSON-compatible objects.  The function is
    deterministic when ``as_of`` and ``generated_at`` are supplied.
    """

    transaction_meta = dict(transaction_meta or {})
    sales_history = dict(sales_history or {})
    sales_meta = dict(sales_meta or {})
    planning_history = dict(planning_history or {})
    planning_meta = dict(planning_meta or {})
    prior_by_id = _normalise_prior_records(prior_records)
    as_of_value = _normalise_as_of(as_of, transactions, transaction_meta)
    generated_at_value = _normalise_generated_at(generated_at)
    configured_price_floor = _integer(transaction_meta.get("priceFloor"))
    price_floor = configured_price_floor if configured_price_floor and configured_price_floor > 0 else DEFAULT_PRICE_FLOOR

    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in transactions:
        if not isinstance(item, Mapping):
            raise ValueError("Every transaction must be an object")
        groups[property_record_id(item)].append(item)
    for rows in groups.values():
        rows.sort(key=lambda item: (clean(item.get("date")), str(item.get("id", ""))))
    valuation_universe = {
        property_id: _property_context_target(rows)
        for property_id, rows in groups.items()
    }
    valuation_universe_latest_sale_date = max(
        (clean(item.get("date"))[:10] for item in valuation_universe.values()),
        default="",
    )
    valuation_universe_price_total = sum(
        _integer(item.get("price")) or 0
        for item in valuation_universe.values()
    )
    property_source_data: dict[str, tuple[Any, ...]] = {}
    planning_universe: dict[str, list[dict[str, Any]]] = {}
    for property_id, rows in groups.items():
        property_sales, property_sales_record = _sales_for_property(property_id, rows, sales_history)
        property_planning, property_planning_record = _planning_for_property(property_id, rows, planning_history)
        property_epcs = _epc_for_property(rows)
        property_source_data[property_id] = (
            property_sales,
            property_sales_record,
            property_planning,
            property_planning_record,
            property_epcs,
        )
        planning_universe[property_id] = property_planning
    candidate_area_stale_universe: dict[str, bool] = {}
    for property_id, candidate in valuation_universe.items():
        candidate_epc_date = _parse_date(candidate.get("epcRegistrationDate"))
        candidate_sale_date = _parse_date(candidate.get("date"))
        candidate_area_stale_universe[property_id] = bool(candidate_epc_date and candidate_sale_date and any(
            _planning_outcome(application) == "positive"
            and _planning_category(application) in {"replacement", "enlargement", "amenity"}
            and (application_date := _planning_story_date(application))
            and candidate_epc_date < application_date <= candidate_sale_date
            for application in planning_universe.get(property_id, [])
        ))

    records: dict[str, dict[str, Any]] = {}
    coverage_counts: dict[str, Counter[str]] = {
        "sales": Counter(),
        "planning": Counter(),
        "epc": Counter(),
        **{source_key: Counter() for source_key in BACKGROUND_COVERAGE_SOURCES},
    }

    for property_id in sorted(groups):
        rows = groups[property_id]
        latest = valuation_universe[property_id]
        sales, sales_record, planning, planning_record, epcs = property_source_data[property_id]
        coverage, evidence = _coverage_records(
            property_id,
            rows,
            latest,
            sales,
            sales_record,
            epcs,
            planning,
            planning_record,
            transaction_meta,
            sales_meta,
            planning_meta,
            len(groups),
            as_of_value,
        )

        events = []
        evidence_by_type: dict[str, list[str]] = defaultdict(list)
        for source_key, entry in coverage.items():
            evidence_by_type[f"coverage:{source_key}"].extend(entry.get("evidenceIds", []))
        for event_type, source_rows in (
            ("sale", sales),
            ("planning_application", planning),
            ("epc_certificate", epcs),
        ):
            for source_row in source_rows:
                event, evidence_record = _event_and_evidence(property_id, event_type, source_row)
                events.append(event)
                evidence[evidence_record["evidenceId"]] = evidence_record
                evidence_by_type[event_type].append(evidence_record["evidenceId"])

        context_data = {
            key: copy.deepcopy(latest[key])
            for key in (
                "latitude",
                "longitude",
                "coordinateSource",
                "coordinatePrecision",
                "geocode",
                "estateId",
                "estate",
                "estateEvidenceStatus",
                "planningConstraints",
                "historicEngland",
                "ofsted",
                "environmentAgency",
                "ordnanceSurvey",
            )
            if latest.get(key) not in (None, "", [], {})
        }
        if context_data:
            context_evidence_identity = copy.deepcopy(context_data)
            if isinstance(context_evidence_identity.get("historicEngland"), dict):
                context_evidence_identity["historicEngland"].pop("checkedAt", None)
            context_evidence_id = _stable_id(
                "evidence:property_context",
                property_id,
                context_evidence_identity,
            )
            evidence[context_evidence_id] = {
                "evidenceId": context_evidence_id,
                "propertyId": property_id,
                "type": "property_context",
                "source": "INSIGHT property-context enrichment",
                "sourceId": clean(latest.get("id")) or property_id,
                "effectiveDate": as_of_value,
                "data": context_data,
            }
            evidence_by_type["property_context"].append(context_evidence_id)
        historic_england = _mapping(latest.get("historicEngland"))
        if historic_england:
            designation_evidence_data = copy.deepcopy(dict(historic_england))
            designation_evidence_id = _stable_id(
                "evidence:listed_building",
                property_id,
                _strip_volatile(designation_evidence_data),
            )
            evidence[designation_evidence_id] = {
                "evidenceId": designation_evidence_id,
                "propertyId": property_id,
                "type": "listed_building",
                "source": clean(historic_england.get("source")) or HISTORIC_ENGLAND_SOURCE,
                "sourceId": clean(historic_england.get("sourceSnapshot")) or property_id,
                "effectiveDate": clean(
                    historic_england.get("sourceUpdatedAt")
                    or historic_england.get("checkedAt")
                )[:10],
                "data": designation_evidence_data,
            }
            coverage["listedBuilding"]["evidenceIds"].append(designation_evidence_id)
            evidence_by_type["listed_building"].append(designation_evidence_id)
        events.sort(
            key=lambda event: (
                event.get("date") or "9999",
                event.get("type", ""),
                event.get("eventId", ""),
            )
        )

        valuation = _base_property_valuation(
            property_id,
            latest,
            sales,
            valuation_universe,
            as_of_value,
            planning,
            epcs,
            candidate_area_stale_universe,
            price_floor,
            valuation_universe_latest_sale_date,
            valuation_universe_price_total,
        )
        planning_adjustment, planning_signals = _planning_value_adjustment(valuation, planning, epcs, sales)
        valuation["planningAdjustment"] = planning_adjustment
        valuation["estimatedCurrentValue"] = _round_estimated_value(
            (_number(valuation.get("baseEstimate")) or 0) + planning_adjustment
        )
        if planning_signals:
            valuation["planningSignals"] = planning_signals
        valuation_data = {key: value for key, value in valuation.items() if value not in (None, "", [], {})}
        valuation_evidence_id = _stable_id("evidence:valuation", property_id, valuation_data)
        evidence[valuation_evidence_id] = {
            "evidenceId": valuation_evidence_id,
            "propertyId": property_id,
            "type": "derived_valuation",
            "source": "INSIGHT evidence-led property valuation",
            "sourceId": f"{VALUATION_MODEL_VERSION}:{property_id}",
            "effectiveDate": as_of_value,
            "data": valuation_data,
        }
        evidence_by_type["valuation_model"].append(valuation_evidence_id)

        fact_packet, metrics, story = _build_fact_packet(
            property_id,
            as_of_value,
            latest,
            sales,
            planning,
            epcs,
            coverage,
            evidence_by_type,
            valuation,
        )
        profile, context = _property_profile(rows, latest)
        transaction_ids = sorted({str(item.get("id")) for item in rows if item.get("id") not in (None, "")})
        record: dict[str, Any] = {
            "schemaVersion": SCHEMA_VERSION,
            "propertyId": property_id,
            "recordVersion": 1,
            "createdAt": generated_at_value,
            "updatedAt": generated_at_value,
            "canonicalAddress": clean(latest.get("address")),
            "postcode": _postcode_display(latest),
            "profile": profile,
            "context": context,
            "transactionIds": transaction_ids,
            "events": events,
            "evidence": [evidence[key] for key in sorted(evidence)],
            "coverage": coverage,
            "metrics": {key: value for key, value in metrics.items() if value is not None},
            "factPacket": fact_packet,
            "story": story,
        }
        fingerprint = record_fingerprint(record)
        previous = prior_by_id.get(property_id)
        if previous:
            record["createdAt"] = clean(previous.get("createdAt")) or generated_at_value
            if clean(previous.get("fingerprint")) == fingerprint:
                record["recordVersion"] = max(1, _integer(previous.get("recordVersion")) or 1)
                record["updatedAt"] = clean(previous.get("updatedAt")) or generated_at_value
            else:
                record["recordVersion"] = max(1, _integer(previous.get("recordVersion")) or 1) + 1
        record["fingerprint"] = fingerprint
        records[property_id] = record
        for source_key in coverage_counts:
            status = coverage[source_key]["status"]
            if status not in COVERAGE_STATES:
                raise ValueError(f"Unsupported {source_key} coverage status: {status}")
            coverage_counts[source_key][status] += 1

    metadata = {
        "schemaVersion": SCHEMA_VERSION,
        "generatorVersion": GENERATOR_VERSION,
        "generatedAt": generated_at_value,
        "asOf": as_of_value,
        "propertyCount": len(records),
        "transactionCount": len(transactions),
        "eventCount": sum(len(record["events"]) for record in records.values()),
        "narrativeCount": sum(bool(record.get("story", {}).get("text")) for record in records.values()),
        "readyNarrativeCount": sum(record.get("story", {}).get("status") == "ready" for record in records.values()),
        "coverageCounts": {
            source_key: dict(sorted(counts.items()))
            for source_key, counts in coverage_counts.items()
        },
        "listedBuildingStatusCounts": dict(
            sorted(
                Counter(
                    clean(
                        record.get("coverage", {})
                        .get("listedBuilding", {})
                        .get("designationStatus")
                    )
                    or "unknown"
                    for record in records.values()
                ).items()
            )
        ),
        "datasetFingerprint": dataset_fingerprint(records),
        "sources": {
            "transactions": clean(transaction_meta.get("source")) or "HM Land Registry Price Paid Data",
            "sales": clean(sales_meta.get("source")) or "HM Land Registry Price Paid Data",
            "planning": clean(planning_meta.get("source")) or "Property-level planning history",
            "epc": clean((transaction_meta.get("epcEnrichment") or {}).get("source"))
            if isinstance(transaction_meta.get("epcEnrichment"), dict)
            else "MHCLG EPC Register",
            "coordinates": clean(_mapping(_mapping(transaction_meta.get("propertyContext")).get("postcodes")).get("source")) or "Postcodes.io",
            "currentFlood": clean(_mapping(_mapping(transaction_meta.get("propertyContext")).get("environmentAgency")).get("source")) or "Environment Agency Real Time flood-monitoring API",
            "planningConstraints": clean(_mapping(_mapping(transaction_meta.get("weeklyContext")).get("planningConstraints")).get("source")) or "Planning Data API",
            "listedBuilding": clean(_mapping(transaction_meta.get("heritageSync")).get("source"))
            or HISTORIC_ENGLAND_SOURCE,
            "schools": clean(_mapping(_mapping(transaction_meta.get("weeklyContext")).get("schools")).get("source")) or "DfE Get Information about Schools (GIAS)",
            "osUprn": clean(_mapping(transaction_meta.get("osRefresh")).get("source")) or "OS Open UPRN",
        },
    }
    return records, metadata


__all__ = [
    "COVERAGE_STATES",
    "GENERATOR_VERSION",
    "PROPERTY_RECORDS_META_NAME",
    "PROPERTY_RECORDS_NAME",
    "SCHEMA_VERSION",
    "build_property_records",
    "canonical_json",
    "dataset_fingerprint",
    "normalise_address",
    "normalise_postcode",
    "parse_window_assignment",
    "property_record_id",
    "read_history_js",
    "read_property_records_js",
    "read_transactions_js",
    "record_fingerprint",
    "write_property_records_js",
]
