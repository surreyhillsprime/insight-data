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
    if not record or refresh_days <= 0:
        return False
    try:
        updated = datetime.fromisoformat(record.get("updatedAt", "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return (datetime.now(timezone.utc) - updated).total_seconds() < refresh_days * 86400


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
        "source": "HM Land Registry Price Paid Data",
    }


def write_output(path, history, meta):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join([
        "window.SURREY_SALES_HISTORY = " + json.dumps(history, separators=(",", ":")) + ";",
        "window.SURREY_SALES_HISTORY_META = " + json.dumps(meta, separators=(",", ":")) + ";",
        "",
    ])
    path.write_text(content, encoding="utf-8")
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
    parser.add_argument("--postcode", action="append", default=[])
    parser.add_argument("--limit-postcodes", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--refresh-days", type=int, default=35)
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
    pending = [key for key in selected if not cache_is_fresh(store.get(key), args.refresh_days)]
    batches = list(chunks(pending, max(1, args.batch_size)))
    print(f"Price Paid history: {len(properties)} properties, {len(selected)} postcodes, {len(pending)} to fetch.", flush=True)

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
    for key, item in properties.items():
        postcode = normalise_postcode(item.get("postcode"))
        target = address_key(item.get("address"))
        cache_record = store.get(postcode, {}) if postcode else {}
        rows = cache_record.get("rows", [])
        coverage_reason = ""
        if not postcode:
            coverage_status = "unavailable"
            coverage_reason = "No postcode in the source Price Paid record"
            rows = []
            properties_unavailable += 1
        elif postcode not in selected:
            coverage_status = "not_checked"
            coverage_reason = "Excluded by the requested postcode or limit filter"
            rows = []
            properties_not_checked += 1
        elif not cache_record.get("updatedAt") or not isinstance(rows, list):
            coverage_status = "unavailable"
            coverage_reason = clean(cache_record.get("lastError")) or "Price Paid postcode lookup unavailable"
            rows = []
            properties_unavailable += 1
        else:
            coverage_status = "complete"
            properties_checked += 1
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
                anchor = max(anchors, key=lambda row: address_score(item.get("address"), build_address(row)))
                canonical = address_key(build_address(anchor))
                match_method = "known-sale-anchor"
        matched_rows = [row for row in rows if address_key(build_address(row)) == canonical]
        sales = [transaction_from_row(row) for row in matched_rows]
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
            "source": "HM Land Registry Price Paid Data",
            "updatedAt": utc_now(),
        }
        if coverage_reason:
            record["coverageReason"] = coverage_reason
        history[key] = record
        for transaction_id in transaction_ids[key]:
            if transaction_id:
                history[transaction_id] = record
        matched_transactions += len(sales)

    properties_with_history = sum(1 for key in properties if history.get(key, {}).get("transactions"))
    meta = {
        "schemaVersion": 1,
        "source": "HM Land Registry Price Paid Data",
        "coverageFrom": "1995",
        "deploymentMode": args.deployment_mode,
        "updatedAt": utc_now(),
        "propertiesRequested": len(properties),
        "propertiesChecked": properties_checked,
        "propertiesUnavailable": properties_unavailable,
        "propertiesNotChecked": properties_not_checked,
        "propertiesWithHistory": properties_with_history,
        "transactionsFound": matched_transactions,
        "note": "Price Paid transaction history only; not the legal title register, ownership, deeds or charges.",
    }
    write_output(args.output, history, meta)
    print(json.dumps(meta, indent=2), flush=True)


if __name__ == "__main__":
    main()
