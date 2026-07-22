#!/usr/bin/env python3
"""Enrich INSIGHT Land Registry sales with domestic EPC floor areas.

The script reads outputs/surrey-transactions.js, looks up matching domestic
EPC certificates through the official GOV.UK API, extracts floor area, and
adds price-per-square-foot fields to each matched transaction.
"""

import argparse
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


def address_score(transaction, candidate, certificate=None):
    candidate = candidate or {}
    certificate = certificate or {}
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
                wait = parse_float(exc.headers.get("Retry-After")) or min(90, 25 * (attempt + 1))
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
            if extract_certificate_number(candidate)
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


def cache_record_is_fresh(record, refresh_days):
    if not record:
        return False
    if record.get("status") == "matched":
        return True
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


def terminal_cache_accounting(transactions, cache, refresh_days):
    """Reconcile every current transaction key to fresh terminal evidence."""

    records = cache.get("records", {})
    requested_keys = {stable_transaction_key(item) for item in transactions}
    matched = 0
    no_match = 0
    errors = 0
    for key in requested_keys:
        record = records.get(key)
        if not record:
            continue
        status = record.get("status")
        if status == "error":
            errors += 1
        elif status == "matched" and cache_record_is_fresh(record, refresh_days):
            matched += 1
        elif status == "no_match" and cache_record_is_fresh(record, refresh_days):
            no_match += 1
    resolved = matched + no_match
    return {
        "requested": len(requested_keys),
        "resolved": resolved,
        "pending": len(requested_keys) - resolved,
        "errors": errors,
        "matchedCacheRecords": matched,
        "noMatchCacheRecords": no_match,
    }


def enrich_transactions(transactions, cache, token, args):
    records = cache.setdefault("records", {})
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

    for index, item in enumerate(transactions, start=1):
        if max_seconds and time.monotonic() - started > max_seconds:
            aborted_reason = f"Stopped after {args.max_run_minutes} minutes before processing transaction {index}."
            break

        if limit and index > limit:
            enriched.append(public_epc_record(item))
            continue

        key = stable_transaction_key(item)
        cached = records.get(key)
        result = None
        if cached and cache_record_is_fresh(cached, args.refresh_days):
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

        output = public_epc_record(item)
        if result and result.get("status") == "matched" and result.get("epc"):
            for key in PUBLIC_EPC_FIELDS:
                output.pop(key, None)
            output.update(publishable_epc_fields(result["epc"]))
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
        enriched.extend(public_epc_record(item) for item in transactions[len(enriched):])

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
    return parser.parse_args()


def main():
    args = parse_args()
    global REQUEST_TIMEOUT, REQUEST_RETRIES
    REQUEST_TIMEOUT = max(3, args.request_timeout)
    REQUEST_RETRIES = max(0, args.request_retries)
    args.progress_every = max(1, args.progress_every)
    token = clean(os.getenv(args.token_env))
    transactions, _summary, meta = read_js(args.input_js)
    cache = load_cache(args.cache)

    existing_matches = sum(1 for item in transactions if numeric(item.get("pricePerSqft")))
    print(f"Transactions: {len(transactions)}")
    print(f"Existing EPC price-per-sqft matches: {existing_matches}")
    if not token:
        print(f"No {args.token_env} found; API lookups will be skipped.")
        if not args.dry_run:
            print("Add the GOV.UK EPC bearer token before running a write sweep.", file=sys.stderr)
            return 2

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
