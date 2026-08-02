#!/usr/bin/env python3
"""Build the local full Price Paid transaction history for INSIGHT properties.

This is a private, resumable cache. It queries HM Land Registry by postcode
without the app ledger's price or date filters, then matches exact addresses.
It does not represent the legal title register, ownership, deeds, or charges.
"""

import argparse
import json
import os
import re
import tempfile
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from insight_data_utils import DEFAULT_INPUT_JS, clean, load_cache, normalise_postcode, read_js, utc_now, write_cache
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


def address_key(value):
    return re.sub(r"[^A-Z0-9]+", " ", clean(value).upper()).strip()


def property_key(item):
    if clean(item.get("propertyRecordId")):
        return clean(item.get("propertyRecordId"))
    return f"property:{address_key(item.get('address'))}|{normalise_postcode(item.get('postcode'))}"


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
        maximum_age_days=refresh_days,
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


def fetch_batch(postcodes, timeout):
    body = urllib.parse.urlencode({
        "query": sparql_query(postcodes),
        "format": "application/sparql-results+json",
    }).encode("utf-8")
    request = urllib.request.Request(
        SPARQL_ENDPOINT,
        data=body,
        headers={
            "Accept": "application/sparql-results+json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "INSIGHT local full Price Paid history collector",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
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
    args = parser.parse_args()

    transactions, _summary, _meta = read_js(args.input)
    properties = {}
    transaction_ids = defaultdict(list)
    current_sales = defaultdict(list)
    for item in transactions:
        key = property_key(item)
        properties.setdefault(key, item)
        transaction_ids[key].append(str(item.get("id", "")))
        current_sales[key].append(item)

    requested = {normalise_postcode(value) for value in args.postcode if normalise_postcode(value)}
    postcode_labels = {}
    for item in properties.values():
        normalised = normalise_postcode(item.get("postcode"))
        if normalised and (not requested or normalised in requested):
            postcode_labels.setdefault(normalised, clean(item.get("postcode")).upper())
    selected = sorted(postcode_labels)
    if args.limit_postcodes > 0:
        selected = selected[:args.limit_postcodes]

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
    for key, item in properties.items():
        postcode = normalise_postcode(item.get("postcode"))
        if postcode:
            properties_by_postcode[postcode].append(key)
    pending = [
        postcode
        for postcode in selected
        if not cache_is_fresh(store.get(postcode), args.refresh_days)
        and not all(
            property_id in fresh_seed
            for property_id in properties_by_postcode[postcode]
        )
    ]
    batches = list(chunks(pending, max(1, args.batch_size)))
    print(
        f"Price Paid history: {len(properties)} properties, {len(selected)} postcodes, "
        f"{len(fresh_seed)} fresh seed records, {len(pending)} postcodes to fetch.",
        flush=True,
    )

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(fetch_batch, [postcode_labels[key] for key in batch], args.timeout): batch
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
            write_cache(args.cache, cache, CACHE_VERSION)
            print(f"Fetched {completed}/{len(pending)} postcodes.", flush=True)
            if args.pause:
                time.sleep(args.pause)

    history = {}
    matched_transactions = 0
    properties_checked = 0
    properties_unavailable = 0
    properties_not_checked = 0
    collected_at = utc_now()
    complete_check_times = []
    for key, item in properties.items():
        postcode = normalise_postcode(item.get("postcode"))
        target = address_key(item.get("address"))
        cache_record = store.get(postcode, {}) if postcode else {}
        (
            coverage_status,
            coverage_reason,
            rows,
            cache_checked_at,
        ) = cache_coverage(
            postcode,
            selected,
            cache_record,
            refresh_days=(args.refresh_days if args.refresh_days > 0 else None),
        )
        seed_record = fresh_seed.get(key) if postcode in selected else None
        if coverage_status != "complete" and seed_record:
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
            canonical = target
            match_method = "exact-address"
            exact_rows = [row for row in rows if address_key(build_address(row)) == target]
            if not exact_rows:
                known_signatures = {
                    (str(sale.get("date", ""))[:10], int(float(sale.get("price", 0))))
                    for sale in current_sales[key]
                }
                anchors = [
                    row for row in rows
                    if (clean(row.get("date"))[:10], int(float(row.get("price", 0)))) in known_signatures
                ]
                if anchors:
                    anchor = max(
                        anchors,
                        key=lambda row: address_score(item.get("address"), build_address(row)),
                    )
                    canonical = address_key(build_address(anchor))
                    match_method = "known-sale-anchor"
            matched_rows = [
                row for row in rows
                if address_key(build_address(row)) == canonical
            ]
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
            sales = sorted(unique.values(), key=lambda sale: sale["date"], reverse=True)
            record = {
                "propertyRecordId": key,
                "address": item.get("address", ""),
                "postcode": item.get("postcode", ""),
                "coverageStatus": coverage_status,
                "totalTransactions": len(sales),
                "latestTransaction": sales[0] if sales else None,
                "transactions": sales,
                "matchMethod": match_method,
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
