#!/usr/bin/env python3
"""Build the local full Price Paid transaction history for INSIGHT properties.

This is a private, resumable cache. It queries HM Land Registry by postcode
without the app ledger's price or date filters, then matches exact addresses.
It does not represent the legal title register, ownership, deeds, or charges.
"""

import argparse
import json
import math
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from insight_data_utils import DEFAULT_INPUT_JS, clean, load_cache, normalise_postcode, read_js, utc_now, write_cache
from insight_data_utils import (
    canonical_address,
    parse_window_json,
    property_record_id,
    reviewed_alias_postcodes_by_canonical_property,
    structured_delivery_point_key,
)
from enrich_planning_history import address_score
from sweep_land_registry import SPARQL_ENDPOINT, build_address, category_label, price_text, property_label
from validate_sales_history_feed import (
    ADDRESS_DATA_USE,
    ATTRIBUTION,
    MAX_FEED_BYTES,
    MAX_FRESHNESS_WINDOW_DAYS,
    REDISTRIBUTION_RIGHTS,
    SOURCE_LICENCE_URL,
    SOURCE_NAME,
    assignment,
    base_feed_identity,
    sha256_json,
    validate as validate_sales_publication,
)


CACHE_VERSION = 1
DEFAULT_SUPPORT_ROOT = Path.home() / "Library" / "Application Support" / "INSIGHT"
DEFAULT_LOCAL_ROOT = Path(os.environ.get("INSIGHT_LOCAL_DATA_ROOT", DEFAULT_SUPPORT_ROOT / "LocalData"))
DEFAULT_OUTPUT = DEFAULT_LOCAL_ROOT / "sales-history.js"
DEFAULT_CACHE = DEFAULT_LOCAL_ROOT / "cache" / "title-history-cache.json"
LOCAL_MARKER = ".insight-local-only"
RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
DEFAULT_FETCH_RETRIES = 2
MAX_RETRY_WAIT_SECONDS = 30


class RequestPacer:
    """Space request starts across every worker sharing this pacer."""

    def __init__(self, seconds_between_starts):
        self.seconds_between_starts = max(0.0, float(seconds_between_starts))
        self._lock = threading.Lock()
        self._next_start = 0.0

    def wait(self):
        if not self.seconds_between_starts:
            return
        with self._lock:
            current = time.monotonic()
            wait = max(0.0, self._next_start - current)
            if wait:
                time.sleep(wait)
                current = time.monotonic()
            self._next_start = max(self._next_start, current) + self.seconds_between_starts

    def defer(self, seconds):
        """Apply one server-requested cooldown to every sharing worker."""

        seconds = max(0.0, float(seconds))
        if not seconds:
            return
        with self._lock:
            self._next_start = max(
                self._next_start,
                time.monotonic() + seconds,
            )


def retry_wait_seconds(retry_after, attempt, *, now=None):
    """Return one bounded wait for a transient HMLR response."""

    requested = None
    try:
        requested = float(retry_after)
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(str(retry_after))
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            requested = (
                retry_at.astimezone(timezone.utc) - (now or datetime.now(timezone.utc))
            ).total_seconds()
        except (TypeError, ValueError, OverflowError):
            requested = None
    fallback = 4 * (attempt + 1) ** 2
    if requested is None or not math.isfinite(requested) or requested <= 0:
        requested = fallback
    return min(MAX_RETRY_WAIT_SECONDS, max(1.0, requested))


def address_key(value):
    return canonical_address(value)


def postcode_display(value):
    """Format one normalised UK postcode for the HMLR query."""

    normalised = normalise_postcode(value)
    match = re.fullmatch(r"([A-Z]{1,2}\d[A-Z\d]?)(\d[A-Z]{2})", normalised)
    return f"{match.group(1)} {match.group(2)}" if match else normalised


def canonical_history_display_address(value):
    """Remove adjacent duplicate address components without erasing old names."""

    parts = [clean(part) for part in str(value or "").split(",") if clean(part)]
    output = []
    for part in parts:
        if output and canonical_address(output[-1]) == canonical_address(part):
            continue
        output.append(part)
    return ", ".join(output).upper()


def property_key(item):
    return property_record_id(item)


def cache_is_fresh(record, refresh_days):
    if not record or record.get("lastError") or refresh_days <= 0:
        return False
    try:
        updated = datetime.fromisoformat(record.get("updatedAt", "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return (datetime.now(timezone.utc) - updated).total_seconds() < refresh_days * 86400


def seed_record_is_fresh(
    record,
    property_id,
    refresh_days,
    *,
    required_sales=(),
    now=None,
):
    """Return whether a published complete record is safe to reuse as a seed."""

    if (
        not isinstance(record, dict)
        or record.get("propertyRecordId") != property_id
        or record.get("coverageStatus") != "complete"
        or not isinstance(record.get("transactions"), list)
        or refresh_days <= 0
    ):
        return False
    try:
        updated = datetime.fromisoformat(
            clean(record.get("updatedAt")).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return False
    if updated.tzinfo is None:
        return False
    published_signatures = {
        (
            clean(sale.get("date"))[:10],
            int(float(sale.get("price", 0))),
        )
        for sale in record["transactions"]
        if isinstance(sale, dict)
    }
    required_signatures = {
        (
            clean(sale.get("date"))[:10],
            int(float(sale.get("price", 0))),
        )
        for sale in required_sales
        if isinstance(sale, dict)
    }
    if not required_signatures <= published_signatures:
        return False
    current = now or datetime.now(timezone.utc)
    return (current - updated.astimezone(timezone.utc)).total_seconds() < refresh_days * 86400


def load_seed_history(path, refresh_days, *, allow_local=False):
    if not path:
        return {}
    source = Path(path)
    if not source.exists():
        return {}
    if refresh_days <= 0:
        return {}
    validate_sales_publication(
        source,
        allow_local=allow_local,
        allow_unbound_commercial=True,
        allow_stale=True,
    )
    return assignment(source.read_text(encoding="utf-8"), "SURREY_SALES_HISTORY")


def cache_coverage(postcode, selected, cache_record, *, refresh_days=None):
    """Return one truthful property lookup state from a postcode cache record."""

    rows = cache_record.get("rows", []) if isinstance(cache_record, dict) else []
    if not postcode:
        return (
            "unavailable",
            "No postcode in the source Price Paid record",
            [],
            "",
        )
    if postcode not in selected:
        return (
            "not_checked",
            "Excluded by the requested postcode or limit filter",
            [],
            "",
        )
    if refresh_days is not None and not cache_is_fresh(cache_record, refresh_days):
        return (
            "unavailable",
            "Price Paid postcode cache is stale and could not be refreshed",
            [],
            "",
        )
    last_error = clean(cache_record.get("lastError"))
    if last_error:
        # Retain the old rows in the resumable cache, but never present them as
        # a successful current lookup after the refresh attempt failed.
        return "unavailable", last_error, [], ""
    checked_at = clean(cache_record.get("updatedAt"))
    if not checked_at or not isinstance(rows, list):
        return (
            "unavailable",
            "Price Paid postcode lookup unavailable",
            [],
            "",
        )
    return "complete", "", rows, checked_at


def sparql_query(postcodes):
    values = " ".join(json.dumps(clean(postcode).upper()) for postcode in postcodes)
    return f"""
PREFIX lrppi: <http://landregistry.data.gov.uk/def/ppi/>
PREFIX lrcommon: <http://landregistry.data.gov.uk/def/common/>

SELECT ?tx ?paon ?saon ?street ?locality ?town ?district ?county ?postcode ?propertyType ?price ?date ?category
WHERE {{
  ?tx lrppi:propertyAddress ?addr ;
      lrppi:pricePaid ?price ;
      lrppi:transactionDate ?date ;
      lrppi:propertyType ?propertyType ;
      lrppi:transactionCategory ?category .
  ?addr lrcommon:postcode ?postcode .
  VALUES ?postcode {{ {values} }}
  OPTIONAL {{ ?addr lrcommon:paon ?paon . }}
  OPTIONAL {{ ?addr lrcommon:saon ?saon . }}
  OPTIONAL {{ ?addr lrcommon:street ?street . }}
  OPTIONAL {{ ?addr lrcommon:locality ?locality . }}
  OPTIONAL {{ ?addr lrcommon:town ?town . }}
  OPTIONAL {{ ?addr lrcommon:district ?district . }}
  OPTIONAL {{ ?addr lrcommon:county ?county . }}
  FILTER(?propertyType IN (lrcommon:detached, lrcommon:semi-detached, lrcommon:terraced, lrcommon:flat-maisonette))
}}
ORDER BY DESC(?date)
""".strip()


def fetch_batch(
    postcodes,
    timeout,
    *,
    retries=DEFAULT_FETCH_RETRIES,
    request_pacer=None,
):
    body = urllib.parse.urlencode({
        "query": sparql_query(postcodes),
        "format": "application/sparql-results+json",
    }).encode("utf-8")
    retries = max(0, int(retries))
    for attempt in range(retries + 1):
        if request_pacer is not None:
            request_pacer.wait()
        request = urllib.request.Request(
            SPARQL_ENDPOINT,
            data=body,
            headers={
                "Accept": "application/sparql-results+json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "INSIGHT local full Price Paid history collector",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as error:
            if error.code not in RETRYABLE_HTTP_STATUS_CODES or attempt >= retries:
                raise
            retry_after = error.headers.get("Retry-After") if error.headers else None
            wait = retry_wait_seconds(retry_after, attempt)
            print(
                f"HMLR returned HTTP {error.code}; waiting {wait:.0f}s before "
                f"retry {attempt + 1}/{retries} for {len(postcodes)} postcode(s).",
                flush=True,
            )
            if request_pacer is None:
                time.sleep(wait)
            else:
                request_pacer.defer(wait)
        except (urllib.error.URLError, TimeoutError):
            if attempt >= retries:
                raise
            wait = retry_wait_seconds(None, attempt)
            print(
                f"HMLR connection failed; waiting {wait:.0f}s before "
                f"retry {attempt + 1}/{retries} for {len(postcodes)} postcode(s).",
                flush=True,
            )
            if request_pacer is None:
                time.sleep(wait)
            else:
                request_pacer.defer(wait)
    grouped = {normalise_postcode(postcode): [] for postcode in postcodes}
    for binding in payload.get("results", {}).get("bindings", []):
        row = {key: value.get("value", "") for key, value in binding.items()}
        grouped.setdefault(normalise_postcode(row.get("postcode")), []).append(row)
    return grouped


def transaction_from_row(row):
    address = build_address(row).upper()
    price = int(float(row.get("price", 0)))
    category = category_label(row.get("category"))
    if category == "STANDARDPRICEPAIDTRANSACTION":
        category = "A"
    elif category == "ADDITIONALPRICEPAIDTRANSACTION":
        category = "B"
    return {
        "id": clean(row.get("tx")) or f"{address}|{row.get('date')}|{price}",
        "address": address,
        "postcode": clean(row.get("postcode")).upper(),
        "price": price,
        "priceText": price_text(price),
        "date": clean(row.get("date"))[:10],
        "propertyType": property_label(row.get("propertyType")),
        "category": category,
        "source": SOURCE_NAME,
    }


def transaction_from_base(row):
    """Publish the minimum official sale already proven by the base ledger."""

    price = int(float(row.get("price", 0)))
    return {
        "id": clean(row.get("id")) or (
            f"{clean(row.get('address'))}|{clean(row.get('date'))[:10]}|{price}"
        ),
        "address": clean(row.get("address")).upper(),
        "postcode": clean(row.get("postcode")).upper(),
        "price": price,
        "priceText": clean(row.get("priceText")) or price_text(price),
        "date": clean(row.get("date"))[:10],
        "propertyType": clean(row.get("propertyType")),
        "category": clean(row.get("category")),
        "source": SOURCE_NAME,
    }


def matched_history_rows(item, rows, known_sales, source_address_variants=()):
    """Match reviewed aliases without treating a shared sale fact as identity."""

    target = address_key(item.get("address"))
    delivery_point = structured_delivery_point_key(item)
    delivery_rows = {
        clean(row.get("tx")) or id(row): row
        for row in rows
        if delivery_point and structured_delivery_point_key(row) == delivery_point
    }
    source_variant_addresses = {
        address_key(variant.get("address"))
        for variant in source_address_variants
        if isinstance(variant, dict) and address_key(variant.get("address"))
    }
    reviewed_addresses = source_variant_addresses | {target}
    anchor_addresses = set()
    for sale in known_sales:
        sale_id = clean(sale.get("id"))
        signature = (
            clean(sale.get("date"))[:10],
            int(float(sale.get("price", 0))),
        )
        candidates = [
            row
            for row in rows
            if sale_id and clean(row.get("tx")) == sale_id
        ]
        if not candidates:
            candidates = [
                row
                for row in rows
                if (
                    clean(row.get("date"))[:10],
                    int(float(row.get("price", 0))),
                ) == signature
            ]
        reviewed_candidates = [
            row
            for row in candidates
            if address_key(build_address(row)) in reviewed_addresses
        ]
        candidates = reviewed_candidates or candidates
        if not candidates:
            continue
        expected_address = clean(sale.get("address")) or clean(item.get("address"))
        anchor = max(
            candidates,
            key=lambda row: (
                address_score(expected_address, build_address(row)),
                address_key(build_address(row)) == target,
                address_key(build_address(row)),
                clean(row.get("tx")),
            ),
        )
        anchor_address = address_key(build_address(anchor))
        if anchor_address:
            anchor_addresses.add(anchor_address)
    alias_rows = {
        clean(row.get("tx")) or id(row): row
        for row in rows
        if address_key(build_address(row))
        in anchor_addresses | source_variant_addresses | {target}
    }
    matched = {**delivery_rows, **alias_rows}
    if delivery_rows and len(matched) > len(delivery_rows):
        method = "structured-delivery-point-plus-known-sale-aliases"
    elif delivery_rows:
        method = "structured-delivery-point"
    elif anchor_addresses:
        method = "known-sale-address-aliases"
    else:
        method = "exact-address"
    return list(matched.values()), method


def write_output(path, history, meta):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join([
        "window.SURREY_SALES_HISTORY = " + json.dumps(history, separators=(",", ":")) + ";",
        "window.SURREY_SALES_HISTORY_META = " + json.dumps(meta, separators=(",", ":")) + ";",
        "",
    ])
    if len(content.encode("utf-8")) > MAX_FEED_BYTES:
        raise ValueError("Sales history feed exceeds the native 50 MiB safety limit")
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
    if meta.get("deploymentMode") == "local":
        (path.parent / LOCAL_MARKER).touch(exist_ok=True)


def read_history_output(path):
    text = Path(path).read_text(encoding="utf-8")
    return (
        parse_window_json(text, "SURREY_SALES_HISTORY", {}),
        parse_window_json(text, "SURREY_SALES_HISTORY_META", {}),
    )


def migrate_existing_history(transactions, prior_history, prior_meta, deployment_mode):
    """Re-key a complete history without reinterpreting its source evidence."""

    properties = {}
    transaction_ids = defaultdict(list)
    prior_records_by_property = defaultdict(dict)
    for item in transactions:
        key = property_key(item)
        properties.setdefault(key, item)
        transaction_id = clean(item.get("id"))
        if not transaction_id:
            continue
        transaction_ids[key].append(transaction_id)
        prior_record = prior_history.get(transaction_id)
        if not isinstance(prior_record, dict):
            raise ValueError(f"Prior sales history has no transaction alias for {transaction_id}")
        prior_key = clean(prior_record.get("propertyRecordId"))
        if not prior_key.startswith("property:"):
            raise ValueError(f"Prior sales history alias {transaction_id} has no canonical record")
        canonical_record = prior_history.get(prior_key)
        if canonical_record != prior_record:
            raise ValueError(f"Prior sales history alias {transaction_id} does not equal its canonical record")
        prior_records_by_property[key][prior_key] = prior_record

    history = {}
    for key, item in properties.items():
        source_records = list(prior_records_by_property.get(key, {}).values())
        if not source_records:
            raise ValueError(f"No prior sales-history evidence maps to {key}")
        complete_records = [
            record for record in source_records
            if record.get("coverageStatus") in {"complete", "partial"}
        ]
        selected_records = complete_records or source_records
        transactions_by_id = {}
        for record in selected_records:
            for sale in record.get("transactions") or []:
                source_id = clean(sale.get("id"))
                if not source_id:
                    raise ValueError(f"Prior sales history for {key} contains a transaction without a source id")
                migrated_sale = dict(sale)
                migrated_sale["address"] = canonical_history_display_address(sale.get("address"))
                transactions_by_id[source_id] = migrated_sale
        sales = sorted(
            transactions_by_id.values(),
            key=lambda sale: (clean(sale.get("date")), clean(sale.get("id"))),
            reverse=True,
        )
        coverage_status = "complete" if complete_records else "unavailable"
        updated_at = min(
            (clean(record.get("updatedAt")) for record in selected_records if clean(record.get("updatedAt"))),
            default=clean(prior_meta.get("sourceCheckedAt")) or utc_now(),
        )
        record = {
            "propertyRecordId": key,
            "address": item.get("address", ""),
            "postcode": item.get("postcode", ""),
            "totalTransactions": len(sales),
            "latestTransaction": sales[0] if sales else None,
            "transactions": sales,
            "matchMethod": (
                "canonical-property-address-alias-union"
                if len(source_records) > 1
                else clean(source_records[0].get("matchMethod")) or "exact-address"
            ),
            "coverageStatus": coverage_status,
            "coverageFrom": "1995",
            "source": SOURCE_NAME,
            "updatedAt": updated_at,
        }
        if coverage_status == "unavailable":
            reasons = sorted({
                clean(source.get("coverageReason"))
                for source in source_records
                if clean(source.get("coverageReason"))
            })
            record["coverageReason"] = "; ".join(reasons) or "Prior Price Paid lookup unavailable"
        history[key] = record
        for transaction_id in transaction_ids[key]:
            history[transaction_id] = record

    canonical_records = {key: history[key] for key in properties}
    properties_checked = sum(record["coverageStatus"] == "complete" for record in canonical_records.values())
    properties_unavailable = sum(record["coverageStatus"] == "unavailable" for record in canonical_records.values())
    properties_with_history = sum(bool(record["transactions"]) for record in canonical_records.values())
    transactions_found = sum(len(record["transactions"]) for record in canonical_records.values())
    published_source_ids = [
        clean(sale.get("id"))
        for record in canonical_records.values()
        for sale in record["transactions"]
    ]
    if len(published_source_ids) != len(set(published_source_ids)):
        raise ValueError("Migrated sales history maps one source transaction to multiple properties")

    _property_ids, _transaction_ids, _transaction_properties, base_fingerprint = base_feed_identity(transactions)
    generated_at = utc_now()
    meta = {
        "schemaVersion": 1,
        "source": SOURCE_NAME,
        "coverageFrom": "1995",
        "deploymentMode": deployment_mode,
        "updatedAt": generated_at,
        "propertiesRequested": len(properties),
        "propertiesChecked": properties_checked,
        "propertiesUnavailable": properties_unavailable,
        "propertiesNotChecked": 0,
        "propertiesWithHistory": properties_with_history,
        "transactionsFound": transactions_found,
        "note": "Price Paid transaction history only; not the legal title register, ownership, deeds or charges.",
    }
    if deployment_mode == "commercial":
        source_checked_at = clean(prior_meta.get("sourceCheckedAt")) or min(
            (
                record["updatedAt"]
                for record in canonical_records.values()
                if record["coverageStatus"] == "complete"
            ),
            default=generated_at,
        )
        meta.update({
            "publicationStatus": "complete",
            "coverageMode": "full-available-price-paid-history",
            "coverageStatus": "complete-accounted",
            "sourceCheckedAt": source_checked_at,
            "freshnessWindowDays": MAX_FRESHNESS_WINDOW_DAYS,
            "sourceLicenceUrl": SOURCE_LICENCE_URL,
            "redistributionRights": REDISTRIBUTION_RIGHTS,
            "addressDataUse": ADDRESS_DATA_USE,
            "attribution": ATTRIBUTION,
            "propertiesCheckedNoHistory": sum(
                record["coverageStatus"] == "complete" and not record["transactions"]
                for record in canonical_records.values()
            ),
            "canonicalPropertyRecords": len(canonical_records),
            "transactionAliases": len(transaction_ids_from_history := {
                transaction_id
                for values in transaction_ids.values()
                for transaction_id in values
            }),
            "lookupKeys": len(history),
            "baseFeedFingerprint": base_fingerprint,
            "historyFingerprint": sha256_json(history),
        })
        if len(transaction_ids_from_history) != len(transactions):
            raise ValueError("Migrated sales history transaction aliases do not cover the base feed")
    return history, meta


def chunks(values, size):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(DEFAULT_INPUT_JS))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--cache", default=str(DEFAULT_CACHE))
    parser.add_argument(
        "--seed-feed",
        default="",
        help=(
            "Reuse still-fresh complete canonical records from an existing publication; "
            "only new or stale postcodes are fetched."
        ),
    )
    parser.add_argument("--postcode", action="append", default=[])
    parser.add_argument("--limit-postcodes", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--refresh-days", type=int, default=28)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--pause", type=float, default=0.2)
    parser.add_argument("--deployment-mode", choices=("local", "commercial"), default="local")
    parser.add_argument(
        "--migrate-from-history",
        default="",
        help="Re-key an existing complete sales-history feed without querying HMLR.",
    )
    parser.add_argument(
        "--cache-only",
        action="store_true",
        help="Rebuild only from the exact existing cache; never fetch or mutate it.",
    )
    args = parser.parse_args()

    transactions, _summary, base_meta = read_js(args.input)
    if args.migrate_from_history:
        prior_history, prior_meta = read_history_output(args.migrate_from_history)
        history, meta = migrate_existing_history(
            transactions,
            prior_history,
            prior_meta,
            args.deployment_mode,
        )
        write_output(args.output, history, meta)
        print(json.dumps(meta, indent=2), flush=True)
        return
    properties = {}
    property_postcodes = defaultdict(set)
    transaction_ids = defaultdict(list)
    current_sales = defaultdict(list)
    for item in transactions:
        key = property_key(item)
        properties.setdefault(key, item)
        if normalise_postcode(item.get("postcode")):
            property_postcodes[key].add(normalise_postcode(item.get("postcode")))
        transaction_ids[key].append(str(item.get("id", "")))
        current_sales[key].append(item)

    address_canonicalisation = base_meta.get("addressCanonicalisation") or {}
    source_variants_by_property = address_canonicalisation.get("sourceAddressVariants") or {}
    if not isinstance(source_variants_by_property, dict):
        source_variants_by_property = {}

    for key, postcodes in reviewed_alias_postcodes_by_canonical_property().items():
        if key in properties:
            property_postcodes[key].update(postcodes)

    requested = {normalise_postcode(value) for value in args.postcode if normalise_postcode(value)}
    postcode_labels = {}
    for postcodes in property_postcodes.values():
        for normalised in postcodes:
            if normalised and (not requested or normalised in requested):
                postcode_labels.setdefault(normalised, postcode_display(normalised))
    selected = sorted(postcode_labels)
    if args.limit_postcodes > 0:
        selected = selected[:args.limit_postcodes]
    selected_set = set(selected)

    cache = load_cache(args.cache, CACHE_VERSION)
    store = cache.setdefault("postcodes", {})
    seed_history = load_seed_history(
        args.seed_feed,
        args.refresh_days,
        allow_local=args.deployment_mode == "local",
    )
    fresh_seed = {
        key: seed_history.get(key)
        for key in properties
        if seed_record_is_fresh(
            seed_history.get(key),
            key,
            args.refresh_days,
            required_sales=current_sales[key],
        )
    }
    properties_by_postcode = defaultdict(list)
    for key, postcodes in property_postcodes.items():
        for postcode in postcodes:
            properties_by_postcode[postcode].append(key)
    pending = [] if args.cache_only else [
        postcode
        for postcode in selected
        if not cache_is_fresh(store.get(postcode), args.refresh_days)
        and not all(
            property_id in fresh_seed
            for property_id in properties_by_postcode[postcode]
        )
    ]
    batches = list(chunks(pending, max(1, args.batch_size)))
    request_pacer = RequestPacer(args.pause)
    print(
        f"Price Paid history: {len(properties)} properties, {len(selected)} postcodes, "
        f"{len(fresh_seed)} fresh seed records, {len(pending)} postcodes to fetch.",
        flush=True,
    )

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(
                fetch_batch,
                [postcode_labels[key] for key in batch],
                args.timeout,
                request_pacer=request_pacer,
            ): batch
            for batch in batches
        }
        completed = 0
        for future in as_completed(futures):
            batch = futures[future]
            try:
                results = future.result()
                for key in batch:
                    store[key] = {"updatedAt": utc_now(), "rows": results.get(key, [])}
            except Exception as error:
                print(f"WARNING {', '.join(batch)}: {type(error).__name__}: {error}", flush=True)
                for key in batch:
                    previous = store.setdefault(key, {})
                    previous["lastError"] = f"{type(error).__name__}: {error}"
            completed += len(batch)
            if not args.cache_only:
                write_cache(args.cache, cache, CACHE_VERSION)
            print(f"Fetched {completed}/{len(pending)} postcodes.", flush=True)

    history = {}
    matched_transactions = 0
    properties_checked = 0
    properties_unavailable = 0
    properties_not_checked = 0
    collected_at = utc_now()
    complete_check_times = []
    for key, item in properties.items():
        postcodes = sorted(property_postcodes.get(key, ()))
        seed_record = (
            fresh_seed.get(key)
            if postcodes and all(postcode in selected_set for postcode in postcodes)
            else None
        )
        if seed_record:
            record = dict(seed_record)
            record.update({
                "propertyRecordId": key,
                "address": item.get("address", ""),
                "postcode": item.get("postcode", ""),
            })
            coverage_status = "complete"
            coverage_reason = ""
            cache_checked_at = clean(record.get("updatedAt"))
            sales = record["transactions"]
        else:
            cache_records = [store.get(postcode, {}) for postcode in postcodes]
            coverage_views = [
                cache_coverage(
                    postcode,
                    selected,
                    record,
                    refresh_days=(args.refresh_days if args.refresh_days > 0 else None),
                )
                for postcode, record in zip(postcodes, cache_records)
            ]
            if not postcodes:
                coverage_status = "unavailable"
                coverage_reason = "No postcode in the source Price Paid record"
                rows = []
                cache_checked_at = ""
            elif any(view[0] == "not_checked" for view in coverage_views):
                coverage_status = "not_checked"
                coverage_reason = (
                    "One or more current or reviewed historic postcodes were "
                    "excluded by the requested filter"
                )
                rows = []
                cache_checked_at = ""
            elif any(view[0] != "complete" for view in coverage_views):
                coverage_status = "unavailable"
                coverage_reason = "; ".join(
                    sorted({view[1] for view in coverage_views if view[1]})
                ) or "Price Paid postcode lookup unavailable"
                rows = []
                cache_checked_at = ""
            else:
                coverage_status = "complete"
                coverage_reason = ""
                rows = [
                    row
                    for view in coverage_views
                    for row in view[2]
                ]
                cache_checked_at = min(
                    (view[3] for view in coverage_views if view[3]),
                    default=collected_at,
                )
            matched_rows, match_method = matched_history_rows(
                item,
                rows,
                current_sales[key],
                source_variants_by_property.get(key, ()),
            )
            sales = [transaction_from_row(row) for row in matched_rows]
            if coverage_status == "complete":
                published_signatures = {
                    (sale["date"], sale["price"]) for sale in sales
                }
                base_fallbacks = [
                    transaction_from_base(sale)
                    for sale in current_sales[key]
                    if (
                        clean(sale.get("date"))[:10],
                        int(float(sale.get("price", 0))),
                    ) not in published_signatures
                ]
                if base_fallbacks:
                    sales.extend(base_fallbacks)
                    match_method += "+canonical-base"
            unique = {sale["id"]: sale for sale in sales}
            sales = sorted(
                unique.values(),
                key=lambda sale: (sale["date"], sale["id"]),
                reverse=True,
            )
            record = {
                "propertyRecordId": key,
                "address": item.get("address", ""),
                "postcode": item.get("postcode", ""),
                "coverageStatus": coverage_status,
                "totalTransactions": len(sales),
                "latestTransaction": sales[0] if sales else None,
                "transactions": sales,
                "matchMethod": match_method,
                "coverageFrom": "1995",
                "source": SOURCE_NAME,
                "updatedAt": (
                    cache_checked_at
                    if coverage_status == "complete"
                    else collected_at
                ),
            }
            if coverage_reason:
                record["coverageReason"] = coverage_reason
        if coverage_status == "complete":
            properties_checked += 1
            complete_check_times.append(cache_checked_at)
        elif coverage_status == "not_checked":
            properties_not_checked += 1
        else:
            properties_unavailable += 1
        history[key] = record
        for transaction_id in transaction_ids[key]:
            if transaction_id:
                history[transaction_id] = record
        matched_transactions += len(sales)

    properties_with_history = sum(1 for key in properties if history.get(key, {}).get("transactions"))
    publication_complete = properties_not_checked == 0
    _base_properties, base_transactions, _base_map, base_fingerprint = (
        base_feed_identity(transactions)
    )
    meta = {
        "schemaVersion": 1,
        "deploymentMode": args.deployment_mode,
        "publicationStatus": "complete" if publication_complete else "partial",
        "coverageMode": "full-available-price-paid-history",
        "coverageStatus": "complete-accounted" if publication_complete else "partial",
        "source": SOURCE_NAME,
        "sourceLicenceUrl": SOURCE_LICENCE_URL,
        "redistributionRights": REDISTRIBUTION_RIGHTS,
        "addressDataUse": ADDRESS_DATA_USE,
        "attribution": ATTRIBUTION,
        "coverageFrom": "1995",
        "updatedAt": collected_at,
        "sourceCheckedAt": min(complete_check_times) if complete_check_times else collected_at,
        "freshnessWindowDays": MAX_FRESHNESS_WINDOW_DAYS,
        "propertiesRequested": len(properties),
        "propertiesChecked": properties_checked,
        "propertiesUnavailable": properties_unavailable,
        "propertiesNotChecked": properties_not_checked,
        "propertiesWithHistory": properties_with_history,
        "propertiesCheckedNoHistory": properties_checked - properties_with_history,
        "transactionsFound": matched_transactions,
        "lookupKeys": len(history),
        "canonicalPropertyRecords": len(properties),
        "transactionAliases": len(base_transactions),
        "baseFeedFingerprint": base_fingerprint,
        "historyFingerprint": sha256_json(history),
        "note": "Price Paid transaction history only; not the legal title register, ownership, deeds or charges.",
    }
    write_output(args.output, history, meta)
    print(json.dumps(meta, indent=2), flush=True)


if __name__ == "__main__":
    main()
