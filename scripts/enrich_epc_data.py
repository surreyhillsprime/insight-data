#!/usr/bin/env python3
"""Enrich INSIGHT Land Registry sales with domestic EPC floor areas.

The script reads outputs/surrey-transactions.js, looks up matching domestic
EPC certificates through the official GOV.UK API, extracts floor area, and
adds price-per-square-foot fields to each matched transaction.
"""

import argparse
import copy
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from insight_data_utils import write_js as write_canonical_js


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_JS = ROOT / "outputs" / "surrey-transactions.js"
DEFAULT_OUTPUT_JS = DEFAULT_INPUT_JS
DEFAULT_CACHE = ROOT / "work" / "epc-cache.json"
API_BASE = "https://api.get-energy-performance-data.communities.gov.uk"
SQM_TO_SQFT = 10.76391041671
DEFAULT_MIN_SCORE = 0.55
CACHE_VERSION = 3
IDENTITY_GUARD_VERSION = 1
REQUEST_TIMEOUT = 12
REQUEST_RETRIES = 1

# Only these derived, non-address EPC values may cross into the public ledger.
# Certificate identifiers, certificate addresses and match diagnostics remain
# in the private resumable cache.
PUBLIC_EPC_FIELDS = frozenset({
    "epcMatched",
    "floorAreaSqm",
    "floorAreaSqft",
    "pricePerSqft",
    "epcRating",
    "epcRegistrationDate",
    "epcSource",
})

NOISE_TOKENS = {
    "A",
    "AN",
    "AND",
    "AT",
    "FLAT",
    "THE",
    "UNIT",
    "APARTMENT",
    "HOUSE",
    "PROPERTY",
    "SURREY",
}

AREA_KEYS = (
    "total_floor_area",
    "total-floor-area",
    "totalFloorArea",
    "total_floor_area_m2",
    "total-floor-area-m2",
    "totalFloorAreaM2",
    "floor_area",
    "floor-area",
    "floorArea",
)

ADDRESS_KEYS = (
    "addressLine1",
    "addressLine2",
    "addressLine3",
    "addressLine4",
    "address_line_1",
    "address_line_2",
    "address_line_3",
    "address_line_4",
    "address1",
    "address2",
    "address3",
    "address4",
    "postTown",
    "post_town",
    "postcode",
)


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean(value):
    return " ".join(str(value or "").replace("\xa0", " ").split()).strip()


def normalise_postcode(value):
    return re.sub(r"[^A-Z0-9]", "", clean(value).upper())


def normalise_key(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def normalise_text(value):
    return re.sub(r"[^A-Z0-9]+", " ", clean(value).upper()).strip()


def parse_window_json(text, name, default):
    match = re.search(rf"window\.{re.escape(name)}\s*=\s*(.*?);\s*(?=window\.|$)", text, re.S)
    if not match:
        return default
    return json.loads(match.group(1))


def read_js(path):
    text = Path(path).read_text(encoding="utf-8")
    return (
        parse_window_json(text, "SURREY_LAND_REG_TRANSACTIONS", []),
        parse_window_json(text, "SURREY_LAND_REG_SUMMARY", {}),
        parse_window_json(text, "SURREY_LAND_REG_META", {}),
    )


def summary_by_market(transactions):
    grouped = {}
    for item in transactions:
        grouped.setdefault(item.get("market", ""), []).append(item)
    summary = {}
    for market, items in grouped.items():
        if not market or not items:
            continue
        ppsf_values = [item.get("pricePerSqft") for item in items if numeric(item.get("pricePerSqft"))]
        summary[market] = {
            "count": len(items),
            "avg": round(sum(item["price"] for item in items) / len(items)),
            "latest": max(item["date"] for item in items),
            "max": max(item["price"] for item in items),
        }
        if ppsf_values:
            summary[market]["avgPricePerSqft"] = round(sum(ppsf_values) / len(ppsf_values))
            summary[market]["epcMatched"] = len(ppsf_values)
    return summary


def write_js(path, transactions, meta):
    """Compatibility wrapper; all publication goes through the canonical writer."""

    write_canonical_js(path, transactions, meta)


def load_cache(path):
    path = Path(path)
    if not path.exists():
        return {"version": CACHE_VERSION, "records": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"version": CACHE_VERSION, "records": {}}
    if payload.get("version") != CACHE_VERSION:
        return {"version": CACHE_VERSION, "records": {}}
    if "records" not in payload or not isinstance(payload["records"], dict):
        payload["records"] = {}
    return payload


def write_cache(path, cache):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cache["version"] = CACHE_VERSION
    cache["updatedAt"] = utc_now()
    pending = path.with_name(path.name + ".tmp")
    pending.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(pending, path)


def write_canonical_js_atomic(path, transactions, meta):
    """Write a complete canonical ledger before atomically replacing the feed."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".tmp")
    write_canonical_js(pending, transactions, meta)
    os.replace(pending, path)


def stable_transaction_key(item):
    bits = [
        normalise_text(item.get("address")),
        normalise_postcode(item.get("postcode")),
        str(item.get("price", "")),
        clean(item.get("date")),
    ]
    return "|".join(bits)


def numeric(value):
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def publishable_epc_fields(epc):
    """Minimise a cached EPC match before attaching it to a public row."""

    return {key: value for key, value in (epc or {}).items() if key in PUBLIC_EPC_FIELDS}


def parse_float(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group(0)) if match else None


def valid_floor_area_sqm(value):
    area = parse_float(value)
    if area is None:
        return None
    if 25 <= area <= 4000:
        return area
    return None


def retry_wait_seconds(retry_after, attempt):
    """Bound one API backoff so a resumable checkpoint can still be persisted."""

    requested = parse_float(retry_after)
    fallback = 25 * (attempt + 1)
    return min(90, max(1, requested if requested is not None else fallback))


def candidate_address(record):
    parts = []
    for key in ADDRESS_KEYS:
        value = clean(record.get(key))
        if value and value.upper() not in {part.upper() for part in parts}:
            parts.append(value)
    return ", ".join(parts)


def extract_certificate_number(record):
    for key in ("certificateNumber", "certificate_number", "certificate-number", "lmkKey", "lmk-key", "LMK_KEY"):
        value = clean(record.get(key))
        if value:
            return value
    return ""


def extract_postcode(record):
    return clean(record.get("postcode") or record.get("POSTCODE"))


def extract_registration_date(record):
    for key in ("registrationDate", "registration_date", "lodgementDate", "lodgement_date", "lodgement-datetime"):
        value = clean(record.get(key))
        if value:
            return value[:10]
    return ""


def extract_rating(record):
    for key in ("currentEnergyEfficiencyBand", "current_energy_efficiency_band", "current-energy-efficiency", "current-energy-rating"):
        value = clean(record.get(key)).upper()
        if value:
            return value
    return ""


def floor_area_from_certificate(record):
    for key in AREA_KEYS:
        area = valid_floor_area_sqm(record.get(key))
        if area:
            return area
    for key, value in flatten_dict(record).items():
        normalised = normalise_key(key)
        if "floor" in normalised and "area" in normalised and "room" not in normalised:
            area = valid_floor_area_sqm(value)
            if area:
                return area
    return None


def certificate_debug_keys(record):
    keys = []
    for key, value in flatten_dict(record).items():
        normalised = normalise_key(key)
        if any(term in normalised for term in ("floorarea", "totalfloor", "certificate", "address", "postcode")):
            if value not in (None, "", [], {}):
                keys.append(key)
    return keys[:16]


def flatten_dict(value, prefix=""):
    if isinstance(value, dict):
        items = {}
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            items.update(flatten_dict(child, child_prefix))
        return items
    if isinstance(value, list):
        items = {}
        for index, child in enumerate(value):
            items.update(flatten_dict(child, f"{prefix}.{index}"))
        return items
    return {prefix: value}


def significant_tokens(value, postcode="", town=""):
    text = normalise_text(value)
    postcode_norm = normalise_postcode(postcode)
    if postcode_norm:
        text = text.replace(postcode_norm, " ")
    for part in re.findall(r"[A-Z]{1,2}\d{1,2}[A-Z]?\s*\d[A-Z]{2}", text):
        text = text.replace(part, " ")
    town_tokens = set(normalise_text(town).split())
    tokens = []
    for token in text.split():
        if len(token) < 2:
            continue
        if token in NOISE_TOKENS or token in town_tokens:
            continue
        tokens.append(token)
    return tokens


POSTCODE_PATTERN = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b")
ADDRESS_ABBREVIATIONS = {
    "RD": "ROAD", "ST": "STREET", "AVE": "AVENUE", "AV": "AVENUE",
    "LN": "LANE", "DR": "DRIVE", "CL": "CLOSE", "CT": "COURT",
    "GDNS": "GARDENS", "GDEN": "GARDEN", "CRES": "CRESCENT",
    "PL": "PLACE", "SQ": "SQUARE", "TER": "TERRACE",
}
UNACCOUNTED_EXTENT_TOKENS = frozenset({
    "ANNEX", "ANNEXE", "FLAT", "UNIT", "ROOM", "PLOT", "SUITE",
    "MAISONETTE", "FLOOR", "BASEMENT", "REAR", "PENTHOUSE", "OUTBUILDING",
    "OUTBUILDINGS", "BLOCK", "STABLE", "STABLES", "GARAGE", "GARAGES",
    "WING", "COTTAGE", "LODGE", "BARN", "BUNGALOW", "BUILDING", "BUILDINGS",
})
# A reviewed additional road clause on the retained exact Woodside House
# certificate. This is a postcode/street-scoped context allowance, never a
# property-name alias or permission to omit a primary delivery identifier.
REVIEWED_ADDITIONAL_ROAD_CONTEXT = {
    ("KT65HE", "COCKCROW HILL"): ("ST MARYS ROAD",),
}


def identity_text(value, street=False, preserve_article=False):
    """Formatting normalization only; never discard letters, numbers or names."""

    value = clean(value).upper().replace("’", "'")
    value = re.sub(r"(?<=[A-Z])'(?=[A-Z])", "", value)
    # Distinguish a range from separate numbers and from one primary number.
    value = re.sub(r"(?<=\d)\s*[-–—]\s*(?=\d)", " RANGE ", value)
    tokens = normalise_text(value).split()
    if street:
        # ST at the start can mean Saint; only expand a suffix abbreviation.
        if tokens:
            tokens[-1] = ADDRESS_ABBREVIATIONS.get(tokens[-1], tokens[-1])
    else:
        tokens = ["FLAT" if token == "APARTMENT" else token for token in tokens]
        if not preserve_article and tokens[:1] == ["THE"]:
            tokens = tokens[1:]
    return " ".join(tokens)


def attributed_address_suffix(transaction, suffix):
    """Every suffix word must belong to a declared or reviewed place clause."""

    phrases = {
        tuple(identity_text(transaction.get(key), preserve_article=True).split())
        for key in ("town", "locality", "district", "county")
        if clean(transaction.get(key))
    }
    # The producer's declared transaction universe is Surrey. This permits
    # the county label, not arbitrary additional locality or premises text.
    phrases.add(("SURREY",))
    road_key = (normalise_postcode(transaction.get("postcode")),
                identity_text(transaction.get("street") or transaction.get("locality"), street=True))
    phrases.update(tuple(value.split()) for value in REVIEWED_ADDITIONAL_ROAD_CONTEXT.get(road_key, ()))
    reachable = {0}
    for index in range(len(suffix) + 1):
        if index not in reachable:
            continue
        for phrase in phrases:
            if phrase and tuple(suffix[index:index + len(phrase)]) == phrase:
                reachable.add(index + len(phrase))
    return len(suffix) in reachable


def certificate_postcode(record):
    explicit = normalise_postcode(extract_postcode(record))
    embedded = {normalise_postcode(value) for value in POSTCODE_PATTERN.findall(candidate_address(record).upper())}
    if explicit:
        embedded.add(explicit)
    return next(iter(embedded)) if len(embedded) == 1 else ""


def delivery_conflict_reason(expected, actual):
    if not actual:
        return "delivery_identity_missing"
    if ("ANNEX" in expected.split() or "ANNEXE" in expected.split()) != ("ANNEX" in actual.split() or "ANNEXE" in actual.split()):
        return "annexe_extent_conflict"
    unit = r"\b(?:FLAT|UNIT|PLOT|ROOM|SUITE)\s+([A-Z]|\d+[A-Z]?(?: RANGE \d+[A-Z]?)?)\b"
    if re.findall(unit, expected) != re.findall(unit, actual):
        return "unit_identifier_conflict"
    numbers = r"\b\d+[A-Z]?(?: RANGE \d+[A-Z]?)?\b"
    if re.findall(numbers, expected) != re.findall(numbers, actual):
        return "primary_number_conflict_or_missing"
    return "unresolved_named_identity_or_alias"


def delivery_identity(transaction, epc_record):
    """Require the entire delivery point, before ranking or trusting EPC facts.

    Structured HMLR identity is authoritative. Certificate address suffixes may
    contain additional locality/road context, but may not omit or add anything
    in front of the matching street. Unstructured inputs require complete exact
    address equality after removing their declared town/locality suffixes.
    """

    postcode = normalise_postcode(transaction.get("postcode"))
    if not postcode or certificate_postcode(epc_record) != postcode:
        return False, "postcode_missing_or_conflicting"
    epc_address = POSTCODE_PATTERN.sub(" ", candidate_address(epc_record).upper())
    paon = clean(transaction.get("paon"))
    street = clean(transaction.get("street") or transaction.get("locality"))
    if paon and street:
        expected = identity_text(" ".join(filter(None, [clean(transaction.get("saon")), paon])))
        road = identity_text(street, street=True)
        # Normalize a street abbreviation only in the already known road slot.
        road_tokens = road.split()
        if not expected or not road_tokens:
            return False, "identity_missing"
        alternatives = {road_tokens[-1]}
        alternatives.update(key for key, value in ADDRESS_ABBREVIATIONS.items() if value == road_tokens[-1])
        normalized = identity_text(epc_address).split()
        road_positions = []
        for index in range(len(normalized) - len(road_tokens) + 1):
            chunk = normalized[index:index + len(road_tokens)]
            if chunk[:-1] == road_tokens[:-1] and chunk[-1] in alternatives:
                road_positions.append(index)
        if not road_positions:
            return False, "street_not_exact"
        # A house name can contain its street ("Parklands House, Parklands").
        # Match the complete delivery prefix at the road boundary, rather than
        # treating the first occurrence inside the house name as the street.
        road_start = next((index for index in road_positions
                           if " ".join(normalized[:index]) == expected), road_positions[0])
        if " ".join(normalized[:road_start]) != expected:
            return False, delivery_conflict_reason(expected, " ".join(normalized[:road_start]))
        suffix = normalized[road_start + len(road_tokens):]
        # Certificates can put a flat/annexe after the street. Such a suffix is
        # an unaccounted delivery point, not harmless town/locality context.
        if any(token in UNACCOUNTED_EXTENT_TOKENS for token in suffix):
            return False, "unaccounted_suffix_unit_or_extent"
        if any(re.fullmatch(r"\d+[A-Z]?", token) for token in suffix):
            return False, "unaccounted_suffix_number"
        if not attributed_address_suffix(transaction, suffix):
            return False, "unattributed_address_suffix"
        return True, "exact_delivery_point"

    def unstructured(value):
        value = identity_text(POSTCODE_PATTERN.sub(" ", clean(value).upper()))
        # Remove complete declared trailing locality/town phrases only.
        suffixes = [identity_text(transaction.get(key)) for key in ("town", "locality")]
        for _ in range(3):
            for suffix in suffixes:
                if suffix and value.endswith(" " + suffix):
                    value = value[:-(len(suffix) + 1)].strip()
        return identity_text(value, street=True)

    expected = unstructured(transaction.get("address"))
    actual = unstructured(epc_address)
    return (True, "exact_full_address") if expected and expected == actual else (False, delivery_conflict_reason(expected, actual))


def retained_certificate(epc):
    """Adapt private cache facts, never its claimed target address or score."""

    return {
        "addressLine1": epc.get("epcAddress"),
        "certificateNumber": epc.get("epcCertificateNumber"),
        "registrationDate": epc.get("epcRegistrationDate"),
        "totalFloorArea": epc.get("floorAreaSqm"),
        "currentEnergyEfficiencyBand": epc.get("epcRating"),
    }


def validated_cached_epc(transaction, record, conflicting_certificate_ids=()):
    if not isinstance(record, dict) or record.get("status") != "matched":
        return None
    epc = record.get("epc")
    if not isinstance(epc, dict):
        return None
    if not all(isinstance(epc.get(key), str) for key in ("epcAddress", "epcCertificateNumber", "epcRegistrationDate")):
        return None
    rating = epc.get("epcRating")
    if rating is not None and (not isinstance(rating, str) or rating not in ("", "A", "B", "C", "D", "E", "F", "G")):
        return None
    certificate = retained_certificate(epc)
    if extract_certificate_number(certificate) in conflicting_certificate_ids:
        return None
    if not delivery_identity(transaction, certificate)[0]:
        return None
    area = floor_area_from_certificate(certificate)
    date = extract_registration_date(certificate)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return None
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    if not area or not extract_certificate_number(certificate) or not numeric(transaction.get("price")):
        return None
    sqft = round(area * SQM_TO_SQFT)
    result = dict(epc)
    result.update({"epcMatched": True, "floorAreaSqm": round(area, 1),
                   "floorAreaSqft": sqft, "pricePerSqft": round(transaction["price"] / sqft)})
    return result


def address_score(transaction, candidate, certificate=None):
    candidate = candidate or {}
    certificate = certificate or {}
    if not delivery_identity(transaction, certificate or candidate)[0]:
        return 0.0
    land_postcode = normalise_postcode(transaction.get("postcode"))
    candidate_postcode = normalise_postcode(extract_postcode(certificate) or extract_postcode(candidate))
    if land_postcode and candidate_postcode and land_postcode != candidate_postcode:
        return 0.0

    land_address = transaction.get("address", "")
    epc_address = candidate_address(certificate) or candidate_address(candidate)
    land_tokens = significant_tokens(land_address, transaction.get("postcode"), transaction.get("town"))
    epc_tokens = significant_tokens(epc_address, extract_postcode(certificate) or extract_postcode(candidate), certificate.get("post_town") or candidate.get("postTown"))
    if not land_tokens or not epc_tokens:
        return 0.0

    land_set = set(land_tokens)
    epc_set = set(epc_tokens)
    overlap = len(land_set & epc_set)
    containment = overlap / max(1, min(len(land_set), len(epc_set)))
    jaccard = overlap / max(1, len(land_set | epc_set))
    score = containment * 0.62 + jaccard * 0.28

    land_numbers = set(re.findall(r"\b\d+[A-Z]?\b", normalise_text(land_address)))
    epc_numbers = set(re.findall(r"\b\d+[A-Z]?\b", normalise_text(epc_address)))
    if land_numbers and epc_numbers and land_numbers & epc_numbers:
        score += 0.14

    if land_set & epc_set:
        first_land = next((token for token in land_tokens if not token.isdigit()), "")
        if first_land and first_land in epc_set:
            score += 0.06

    if land_postcode and candidate_postcode == land_postcode:
        score += 0.06

    return min(score, 1.0)


def response_rows(payload):
    data = payload.get("data", [])
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "certificates", "results", "items", "rows"):
            rows = data.get(key)
            if isinstance(rows, list):
                return rows
        return [data]
    return []


def request_json(path, token, params=None, retries=None, timeout=None):
    retries = REQUEST_RETRIES if retries is None else retries
    timeout = REQUEST_TIMEOUT if timeout is None else timeout
    query = urllib.parse.urlencode(params or {}, doseq=True)
    url = API_BASE + path + (("?" + query) if query else "")
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "User-Agent": "INSIGHT Surrey EPC enrichment",
    }
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {"data": []}
            if exc.code == 429 and attempt < retries:
                wait = retry_wait_seconds(exc.headers.get("Retry-After"), attempt)
                print(f"EPC API rate limit reached; waiting {wait:.0f}s before retry {attempt + 1}/{retries}.", flush=True)
                time.sleep(wait)
                continue
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"EPC API error {exc.code}: {detail[:300]}") from exc
        except urllib.error.URLError as exc:
            if attempt < retries:
                time.sleep(1.5 + attempt)
                continue
            raise RuntimeError(f"EPC API connection error: {exc}") from exc
    return {"data": []}


def search_candidates(transaction, token, page_size, lookup_cache=None):
    params = {"current_page": 1, "page_size": page_size}
    postcode = clean(transaction.get("postcode"))
    if postcode:
        params["postcode"] = postcode
        lookup_key = "postcode:" + normalise_postcode(postcode)
    else:
        address = clean(transaction.get("address"))
        params["address"] = address
        lookup_key = "address:" + normalise_text(address)
    if lookup_cache is not None and lookup_key in lookup_cache:
        return lookup_cache[lookup_key]
    payload = request_json("/api/domestic/search", token, params)
    rows = response_rows(payload)
    if lookup_cache is not None:
        lookup_cache[lookup_key] = rows
    return rows


def fetch_certificate(certificate_number, token, lookup_cache=None):
    if lookup_cache is not None and certificate_number in lookup_cache:
        return lookup_cache[certificate_number]
    payload = request_json("/api/certificate", token, {"certificate_number": certificate_number})
    data = payload.get("data", {})
    certificate = data if isinstance(data, dict) else {}
    if lookup_cache is not None:
        lookup_cache[certificate_number] = certificate
    return certificate


def candidate_sort_key(record):
    return extract_registration_date(record) or ""


def best_epc_match(
    transaction,
    token,
    page_size,
    min_score,
    max_certificate_fetches,
    candidate_cache=None,
    certificate_cache=None,
):
    candidates = search_candidates(transaction, token, page_size, candidate_cache)
    if not candidates:
        return {
            "status": "no_match",
            "reason": "No domestic EPC certificates found for postcode",
            "candidateCount": 0,
        }

    scored = sorted(
        (
            (address_score(transaction, candidate), candidate)
            for candidate in candidates
            if extract_certificate_number(candidate) and delivery_identity(transaction, candidate)[0]
        ),
        key=lambda item: (item[0], candidate_sort_key(item[1])),
        reverse=True,
    )

    best = None
    diagnostics = Counter()
    best_seen = {
        "roughScore": round(scored[0][0], 3) if scored else 0,
        "finalScore": 0,
        "certificateNumber": extract_certificate_number(scored[0][1]) if scored else "",
        "address": candidate_address(scored[0][1]) if scored else "",
        "areaFound": False,
        "certificateKeys": [],
    }
    for rough_score, candidate in scored[:max_certificate_fetches]:
        if rough_score < min_score - 0.2:
            diagnostics["rough_score_too_low"] += 1
            continue
        certificate_number = extract_certificate_number(candidate)
        certificate = fetch_certificate(certificate_number, token, certificate_cache)
        if certificate and extract_certificate_number(certificate) not in ("", certificate_number):
            diagnostics["certificate_number_conflict"] += 1
            continue
        if certificate and not delivery_identity(transaction, certificate)[0]:
            diagnostics["certificate_identity_rejected"] += 1
            continue
        final_score = address_score(transaction, candidate, certificate)
        area_sqm = floor_area_from_certificate(certificate) or floor_area_from_certificate(candidate)
        if final_score > best_seen["finalScore"]:
            best_seen.update({
                "finalScore": round(final_score, 3),
                "certificateNumber": certificate_number,
                "address": candidate_address(certificate) or candidate_address(candidate),
                "areaFound": bool(area_sqm),
                "certificateKeys": certificate_debug_keys(certificate),
            })
        if final_score < min_score:
            diagnostics["weak_address_match"] += 1
        if not area_sqm:
            diagnostics["missing_floor_area"] += 1
        if final_score >= min_score and area_sqm:
            sqft = round(area_sqm * SQM_TO_SQFT)
            if sqft <= 0:
                diagnostics["invalid_floor_area"] += 1
                continue
            match = {
                "status": "matched",
                "identityGuardVersion": IDENTITY_GUARD_VERSION,
                "candidateCount": len(candidates),
                "epc": {
                    "epcMatched": True,
                    "floorAreaSqm": round(area_sqm, 1),
                    "floorAreaSqft": sqft,
                    "pricePerSqft": round(transaction["price"] / sqft),
                    "epcRating": extract_rating(certificate) or extract_rating(candidate),
                    "epcCertificateNumber": certificate_number,
                    "epcRegistrationDate": extract_registration_date(certificate) or extract_registration_date(candidate),
                    "epcAddress": candidate_address(certificate) or candidate_address(candidate),
                    "epcMatchScore": round(final_score, 3),
                    "epcSource": "MHCLG EPC Register",
                },
            }
            if not best or match["epc"]["epcMatchScore"] > best["epc"]["epcMatchScore"]:
                best = match

    if best:
        return best

    return {
        "status": "no_match",
        "reason": "No certificate cleared address match and floor-area checks",
        "candidateCount": len(candidates),
        "diagnostics": dict(diagnostics),
        "bestRoughScore": best_seen["roughScore"],
        "bestFinalScore": best_seen["finalScore"],
        "bestCertificateNumber": best_seen["certificateNumber"],
        "bestAddress": best_seen["address"],
        "bestAreaFound": best_seen["areaFound"],
        "bestCertificateKeys": best_seen["certificateKeys"],
    }


def cache_record_is_fresh(record, refresh_days, transaction=None, conflicting_certificate_ids=()):
    if not record:
        return False
    if record.get("status") == "matched":
        # A legacy score or target-address key is never identity evidence.
        return transaction is not None and validated_cached_epc(transaction, record, conflicting_certificate_ids) is not None
    if record.get("status") == "no_match" and record.get("evidenceScope") == "retained-cache-only":
        # Finding no exact certificate in retained evidence is not a new
        # register search, even when its previous lookup timestamp is recent.
        return False
    # A transport/API failure is not evidence about the property. Always
    # retry it on the next checkpointed run instead of suppressing recovery
    # for the normal no-match refresh window.
    if record.get("status") == "error":
        return False
    searched = record.get("searchedAt", "")
    if not searched:
        return False
    try:
        searched_dt = datetime.fromisoformat(searched.replace("Z", "+00:00"))
    except ValueError:
        return False
    age = datetime.now(timezone.utc) - searched_dt
    return age.days < refresh_days


def public_epc_record(item):
    """Remove legacy EPC identifiers while preserving approved derived facts."""

    cleaned = dict(item)
    for key in (
        "epcCertificateNumber",
        "epcAddress",
        "epcMatchScore",
        "epcHistory",
        "epcSearch",
        "epcSearchDiagnostics",
        "epcMatchDiagnostics",
        "epcSourceAddress",
    ):
        cleaned.pop(key, None)
    return cleaned


def without_unverified_epc(item):
    cleaned = public_epc_record(item)
    for key in PUBLIC_EPC_FIELDS:
        cleaned.pop(key, None)
    cleaned["epcMatched"] = False
    return cleaned


def retained_certificate_index(cache):
    """Retain original evidence/check dates, and quarantine contradictory IDs."""

    by_id = {}
    conflicts = set()
    for record in cache.get("records", {}).values():
        if not isinstance(record, dict) or record.get("status") != "matched":
            continue
        epc = record.get("epc")
        if not isinstance(epc, dict):
            continue
        certificate = retained_certificate(epc)
        number = extract_certificate_number(certificate)
        if not number:
            continue
        # Price/PPSF and original target address vary across sales; certificate
        # identity, area, rating and registration date must not vary by target.
        signature = (identity_text(epc.get("epcAddress")), certificate_postcode(certificate),
                     epc.get("floorAreaSqm"), epc.get("epcRating"), epc.get("epcRegistrationDate"))
        if number in by_id and by_id[number][0] != signature:
            conflicts.add(number)
        else:
            by_id.setdefault(number, (signature, record))
    by_postcode = {}
    for number, (_signature, record) in by_id.items():
        if number not in conflicts:
            postcode = certificate_postcode(retained_certificate(record["epc"]))
            if postcode:
                by_postcode.setdefault(postcode, []).append(record)
    return by_postcode, conflicts


def revalidate_retained_cache(transactions, cache):
    """Pure offline review: no network, no writes, no new source-check dates.

    This is a review of retained certificates, not proof of register coverage.
    Return candidate rows/cache for separately authorized private staging only.
    """

    by_postcode, conflicts = retained_certificate_index(cache)
    reviewed_cache = copy.deepcopy(cache)
    reviewed_records = reviewed_cache.setdefault("records", {})
    rows, decisions = [], []
    counts = Counter()
    for transaction in transactions:
        key = stable_transaction_key(transaction)
        prior = cache.get("records", {}).get(key)
        eligible = []
        rejected = Counter()
        candidates = by_postcode.get(normalise_postcode(transaction.get("postcode")), [])
        for record in candidates:
            epc = validated_cached_epc(transaction, record)
            if epc:
                eligible.append((epc["epcRegistrationDate"], epc["epcCertificateNumber"], epc, record))
            else:
                accepted_identity, rejection = delivery_identity(transaction, retained_certificate(record["epc"]))
                rejected["invalid_retained_certificate_facts" if accepted_identity else rejection] += 1
        eligible.sort(key=lambda value: (value[0], value[1]), reverse=True)
        chosen = eligible[0] if eligible else None
        if chosen:
            latest = [entry for entry in eligible if entry[0] == chosen[0]]
            if len({(entry[2]["floorAreaSqm"], entry[2].get("epcRating")) for entry in latest}) > 1:
                chosen = None
                reason = "conflicting_latest_exact_certificates"
            else:
                reason = "latest_exact_retained_certificate"
        else:
            reason = "no_exact_retained_certificate" if candidates else "no_retained_certificate_for_postcode"
        row = without_unverified_epc(transaction)
        if chosen:
            _date, _number, epc, retained = chosen
            row.update(publishable_epc_fields(epc))
            reviewed_records[key] = {
                "status": "matched", "epc": epc, "address": transaction.get("address"),
                "postcode": transaction.get("postcode"), "searchedAt": retained.get("searchedAt"),
                "identityGuardVersion": IDENTITY_GUARD_VERSION,
                "evidenceScope": "retained-cache-only", "identityReviewReason": reason,
            }
            disposition = "retained" if prior and (prior.get("epc") or {}).get("epcCertificateNumber") == epc["epcCertificateNumber"] else "replaced"
        else:
            disposition = "unknown"
            reviewed_records[key] = {
                "status": "no_match", "reason": reason, "address": transaction.get("address"),
                "postcode": transaction.get("postcode"), "searchedAt": (prior or {}).get("searchedAt"),
                "identityGuardVersion": IDENTITY_GUARD_VERSION, "evidenceScope": "retained-cache-only",
            }
        counts[disposition] += 1
        prior_epc = (prior or {}).get("epc")
        prior_reason = delivery_identity(transaction, retained_certificate(prior_epc))[1] if isinstance(prior_epc, dict) else "no_retained_match"
        decisions.append({"transactionId": transaction.get("id"), "disposition": disposition,
                          "reason": reason, "previousMatchIdentity": prior_reason,
                          "candidateRejections": dict(rejected), "eligibleRetainedCertificates": len(eligible)})
        rows.append(row)
    return rows, reviewed_cache, {
        "identityGuardVersion": IDENTITY_GUARD_VERSION, "evidenceScope": "retained-cache-only",
        "sourceChecksPerformed": 0, "transactions": len(rows), "counts": dict(counts),
        "conflictingCertificateIds": len(conflicts), "decisions": decisions,
    }


def terminal_cache_accounting(transactions, cache, refresh_days):
    """Reconcile every current transaction key to fresh terminal evidence."""

    records = cache.get("records", {})
    _index, conflicts = retained_certificate_index(cache)
    matched = 0
    no_match = 0
    errors = 0
    for item in transactions:
        key = stable_transaction_key(item)
        record = records.get(key)
        if not record:
            continue
        status = record.get("status")
        if status == "error":
            errors += 1
        elif status == "matched" and cache_record_is_fresh(record, refresh_days, item, conflicts):
            matched += 1
        elif status == "no_match" and cache_record_is_fresh(record, refresh_days, item):
            no_match += 1
    resolved = matched + no_match
    return {
        "requested": len(transactions),
        "resolved": resolved,
        "pending": len(transactions) - resolved,
        "errors": errors,
        "matchedCacheRecords": matched,
        "noMatchCacheRecords": no_match,
    }


def terminal_cache_can_reconcile(accounting, transaction_count):
    """Allow metadata-only alignment when every current row is resolved in cache."""

    return (
        accounting.get("requested") == transaction_count
        and accounting.get("resolved") == transaction_count
        and accounting.get("pending") == 0
        and accounting.get("errors") == 0
    )


def publication_matches_cache(transactions, cache):
    """Equal counts cannot prove the published area/rating/PPSF is still right."""

    _index, conflicts = retained_certificate_index(cache)
    for transaction in transactions:
        epc = validated_cached_epc(transaction, cache.get("records", {}).get(stable_transaction_key(transaction)), conflicts)
        expected = publishable_epc_fields(epc) if epc else {"epcMatched": False}
        actual = {key: value for key, value in transaction.items() if key in PUBLIC_EPC_FIELDS}
        if actual != expected:
            return False
    return True


def enrich_transactions(transactions, cache, token, args):
    records = cache.setdefault("records", {})
    _index, conflicts = retained_certificate_index(cache)
    candidate_cache = {}
    certificate_cache = {}
    enriched = []
    stats = {
        "matched": 0,
        "cached": 0,
        "searched": 0,
        "noMatch": 0,
        "errors": 0,
        "skipped": 0,
    }
    reasons = Counter()
    limit = args.limit if args.limit and args.limit > 0 else None
    started = time.monotonic()
    max_seconds = args.max_run_minutes * 60 if args.max_run_minutes else 0
    aborted_reason = ""

    def checked_existing(item):
        output = without_unverified_epc(item)
        epc = validated_cached_epc(item, records.get(stable_transaction_key(item)), conflicts)
        if epc:
            output.update(publishable_epc_fields(epc))
        return output

    for index, item in enumerate(transactions, start=1):
        if max_seconds and time.monotonic() - started > max_seconds:
            aborted_reason = f"Stopped after {args.max_run_minutes} minutes before processing transaction {index}."
            break

        if limit and index > limit:
            enriched.append(checked_existing(item))
            continue

        key = stable_transaction_key(item)
        cached = records.get(key)
        result = None
        if cached and cache_record_is_fresh(cached, args.refresh_days, item, conflicts):
            stats["cached"] += 1
            result = cached
        elif token:
            try:
                stats["searched"] += 1
                result = best_epc_match(
                    item,
                    token,
                    args.page_size,
                    args.min_score,
                    args.max_certificate_fetches,
                    candidate_cache,
                    certificate_cache,
                )
                result["searchedAt"] = utc_now()
                result["address"] = item.get("address")
                result["postcode"] = item.get("postcode")
                records[key] = result
                if args.pause:
                    time.sleep(args.pause)
            except Exception as exc:
                stats["errors"] += 1
                result = {
                    "status": "error",
                    "reason": str(exc),
                    "searchedAt": utc_now(),
                    "address": item.get("address"),
                    "postcode": item.get("postcode"),
                }
                records[key] = result
                if args.max_errors and stats["errors"] >= args.max_errors:
                    aborted_reason = f"Stopped after {stats['errors']} EPC API errors."
        else:
            stats["skipped"] += 1
            result = cached

        output = without_unverified_epc(item)
        validated_epc = validated_cached_epc(item, result, conflicts)
        if validated_epc:
            output.update(publishable_epc_fields(validated_epc))
            stats["matched"] += 1
        elif result and result.get("status") == "no_match":
            stats["noMatch"] += 1
            reasons[result.get("reason") or "No match"] += 1
            for reason, count in (result.get("diagnostics") or {}).items():
                reasons[f"diagnostic:{reason}"] += count
        elif result and result.get("status") == "error":
            reasons["error:" + (result.get("reason") or "Unknown error")[:120]] += 1
        enriched.append(output)

        if aborted_reason:
            break

        if args.fail_if_no_matches_after and index >= args.fail_if_no_matches_after and stats["matched"] == 0:
            aborted_reason = f"Stopped after {index} transactions because no EPC matches had been found."
            break

        if index % args.progress_every == 0:
            print(f"Processed {index}/{len(transactions)} transactions; EPC matches so far: {stats['matched']}")

    if aborted_reason and len(enriched) < len(transactions):
        enriched.extend(checked_existing(item) for item in transactions[len(enriched):])

    return enriched, stats, reasons, aborted_reason


def parse_args():
    parser = argparse.ArgumentParser(description="Enrich INSIGHT Land Registry data with EPC floor areas.")
    parser.add_argument("--input-js", default=str(DEFAULT_INPUT_JS), help="Input INSIGHT JS feed.")
    parser.add_argument("--write-js", default=str(DEFAULT_OUTPUT_JS), help="Output INSIGHT JS feed.")
    parser.add_argument("--cache", default=str(DEFAULT_CACHE), help="EPC lookup cache path.")
    parser.add_argument("--token-env", default="EPC_BEARER_TOKEN", help="Environment variable containing the GOV.UK EPC API bearer token.")
    parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE, help="Minimum address match score from 0 to 1.")
    parser.add_argument("--page-size", type=int, default=5000, help="Domestic EPC search page size.")
    parser.add_argument("--max-certificate-fetches", type=int, default=8, help="Maximum full certificates to fetch per transaction after postcode search.")
    parser.add_argument("--refresh-days", type=int, default=90, help="How soon to retry prior no-match lookups.")
    parser.add_argument("--pause", type=float, default=0.02, help="Pause between uncached lookups, in seconds.")
    parser.add_argument("--limit", type=int, default=0, help="Only enrich the first N transactions; useful for testing.")
    parser.add_argument("--fail-under-matches", type=int, default=0, help="Return an error if fewer than this many EPC matches are produced.")
    parser.add_argument("--request-timeout", type=float, default=12, help="Seconds before one EPC API request times out.")
    parser.add_argument("--request-retries", type=int, default=1, help="Retries for transient EPC API connection failures.")
    parser.add_argument("--max-errors", type=int, default=25, help="Stop early after this many EPC API errors. Use 0 to disable.")
    parser.add_argument("--max-run-minutes", type=float, default=45, help="Stop early after this many minutes. Use 0 to disable.")
    parser.add_argument("--fail-if-no-matches-after", type=int, default=0, help="Stop early if this many transactions have been searched with zero EPC matches.")
    parser.add_argument("--allow-partial-success", action="store_true", help="Write partial EPC progress and return success when the run reaches the time limit after finding matches.")
    parser.add_argument("--progress-every", type=int, default=25, help="Print progress every N processed transactions.")
    parser.add_argument("--dry-run", action="store_true", help="Report what would happen without writing files.")
    parser.add_argument("--revalidate-cache-only", action="store_true", help="Read-only full identity review using retained cache certificates only; never contacts the API or writes a feed/cache.")
    parser.add_argument("--review-json", help="With --revalidate-cache-only, exclusively create an identity decision report under this worktree's .tmp/ or work/reviews/; no certificate addresses or identifiers are exported.")
    return parser.parse_args()


def main():
    args = parse_args()
    global REQUEST_TIMEOUT, REQUEST_RETRIES
    REQUEST_TIMEOUT = max(3, args.request_timeout)
    REQUEST_RETRIES = max(0, args.request_retries)
    args.progress_every = max(1, args.progress_every)
    transactions, _summary, meta = read_js(args.input_js)
    cache = load_cache(args.cache)

    if args.revalidate_cache_only:
        if args.limit:
            raise ValueError("Offline identity review must cover every current transaction")
        _rows, _cache, report = revalidate_retained_cache(transactions, cache)
        if args.review_json:
            path = Path(args.review_json).resolve()
            allowed = (ROOT / ".tmp", ROOT / "work" / "reviews")
            if not any(base.resolve() in path.parents for base in allowed):
                raise ValueError("Review reports must stay in this worktree's private .tmp/ or work/reviews/ directory")
            if path in {Path(args.cache).resolve(), Path(args.input_js).resolve(), Path(args.write_js).resolve()}:
                raise ValueError("An identity review must not overwrite an input/cache/feed")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as handle:
                json.dump(report, handle, indent=2, sort_keys=True)
                handle.write("\n")
        print(json.dumps({key: value for key, value in report.items() if key != "decisions"}, sort_keys=True))
        return 0
    if args.review_json:
        raise ValueError("--review-json requires --revalidate-cache-only")
    token = clean(os.getenv(args.token_env))

    existing_matches = sum(1 for item in transactions if numeric(item.get("pricePerSqft")))
    print(f"Transactions: {len(transactions)}")
    print(f"Existing EPC price-per-sqft matches: {existing_matches}")
    initial_accounting = terminal_cache_accounting(transactions, cache, args.refresh_days)
    prior_epc = meta.get("epcEnrichment") if isinstance(meta.get("epcEnrichment"), dict) else {}
    if (
        prior_epc.get("status") == "complete"
        and prior_epc.get("identityGuardVersion") == IDENTITY_GUARD_VERSION
        and initial_accounting["resolved"] == len(transactions)
        and initial_accounting["pending"] == 0
        and initial_accounting["errors"] == 0
        and all(prior_epc.get(key) == value for key, value in initial_accounting.items())
        and publication_matches_cache(transactions, cache)
    ):
        print("EPC enrichment is already fully reconciled; no checkpoint changes required.")
        return 0
    if not token:
        print(f"No {args.token_env} found; API lookups will be skipped.")
        if not args.dry_run and not terminal_cache_can_reconcile(initial_accounting, len(transactions)):
            print("Add the GOV.UK EPC bearer token before running a write sweep.", file=sys.stderr)
            return 2
        if not args.dry_run:
            print("Every current transaction is resolved in the cache; reconciling publication metadata without API access.")

    enriched, stats, reasons, aborted_reason = enrich_transactions(transactions, cache, token, args)
    matched = sum(1 for item in enriched if numeric(item.get("pricePerSqft")))
    coverage = round(matched / len(enriched) * 100, 1) if enriched else 0
    print(f"EPC matches: {matched} ({coverage}%)")
    print(
        "Lookup summary: "
        + ", ".join(f"{key}={value}" for key, value in stats.items())
    )
    if reasons:
        print("Top EPC no-match/error reasons:")
        for reason, count in reasons.most_common(12):
            print(f"- {reason}: {count}")

    if args.dry_run:
        return 0

    if args.fail_under_matches and matched < args.fail_under_matches:
        write_cache(args.cache, cache)
        print(
            f"EPC enrichment produced {matched} matches, below required minimum {args.fail_under_matches}.",
            file=sys.stderr,
        )
        return 3

    accounting = terminal_cache_accounting(transactions, cache, args.refresh_days)
    complete = not aborted_reason and accounting["pending"] == 0 and accounting["errors"] == 0
    meta["epcEnrichment"] = {
        "source": "MHCLG Get energy performance of buildings data API",
        "updatedAt": utc_now(),
        "matched": matched,
        "coveragePercent": coverage,
        "floorAreaUnit": "sq m converted to sq ft",
        "pricePerSqft": True,
        "minimumAddressMatchScore": args.min_score,
        "identityGuardVersion": IDENTITY_GUARD_VERSION,
        "status": "complete" if complete else "partial",
        **accounting,
    }
    if not complete:
        meta["epcEnrichment"]["note"] = aborted_reason or (
            f"{accounting['pending']} transaction lookups remain unresolved, "
            f"including {accounting['errors']} transient API errors."
        )
    write_cache(args.cache, cache)
    write_canonical_js_atomic(args.write_js, enriched, meta)
    print(f"Updated {args.write_js}")
    print(f"Updated {args.cache}")
    if not complete:
        note = meta["epcEnrichment"]["note"]
        print(note, file=sys.stderr)
        if args.allow_partial_success and matched > 0:
            print("Partial EPC progress saved; rerun the workflow to continue from the cache.")
            return 0
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
