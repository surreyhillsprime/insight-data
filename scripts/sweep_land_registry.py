#!/usr/bin/env python3
"""Refresh the Surrey GBP 3m+ Land Registry ledger for INSIGHT.

The script fetches HM Land Registry Price Paid Data via the official SPARQL
endpoint, falls back to the last local CSV if the endpoint is unavailable, and
regenerates outputs/surrey-transactions.js for the static app.
"""

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

from private_estates import classify_estate, load_compiled_registry
from insight_data_utils import FEED_SCHEMA_VERSION, PROPERTY_RECORD_SCHEMA_VERSION, property_record_id


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV = ROOT / "work" / "land-reg-surrey-3m-1995.csv"
HISTORICAL_CSV = ROOT / "work" / "land-reg-surrey-3m-1995-2009.csv"
CURRENT_CSV = ROOT / "work" / "land-reg-surrey-3m-2010.csv"
DEFAULT_JS = ROOT / "outputs" / "surrey-transactions.js"
START_DATE = "1995-01-01"
CURRENT_START_DATE = "2010-01-01"
PRICE_FLOOR = 3_000_000
SPARQL_ENDPOINT = "https://landregistry.data.gov.uk/landregistry/query"
FETCH_RETRIES = 2
ARCHIVE_URL = "https://price-paid-data.publicdata.landregistry.gov.uk/pp-{year}.csv"
BASE_TRANSACTION_FIELDS = {
    "id", "propertyRecordId", "market", "district", "address", "paon", "saon", "street", "locality", "town",
    "postcode", "price", "priceText", "date", "propertyType", "estateId", "estate",
    "estateClassification", "estateType", "estateRuleId", "estateRegistryVersion",
    "estateEvidenceStatus", "estateReviewStatus", "source", "kind", "category",
}
BASE_METADATA_FIELDS = {
    "schemaVersion", "rawRows", "residentialRows", "mappedTransactions", "uniquePostcodes", "from", "to",
    "priceFloor", "source", "propertyTypes", "updateCadence", "officialSearch", "estateSummary", "estateIdSummary",
    "estateTypeSummary", "estateRegistryVersion", "estateClassifierMode",
    "estateClassificationMode", "estateStructuredFieldCoverage",
    "estateActiveDefinitionCount", "estateActiveRuleCount",
    "propertyRecordSchemaVersion", "canonicalPropertyRecords", "propertyIdentityMode",
}

CANONICAL_DISTRICTS = {
    "elmbridge": "Elmbridge",
    "epsom and ewell": "Epsom and Ewell",
    "guildford": "Guildford",
    "mole valley": "Mole Valley",
    "reigate and banstead": "Reigate and Banstead",
    "runnymede": "Runnymede",
    "spelthorne": "Spelthorne",
    "surrey heath": "Surrey Heath",
    "tandridge": "Tandridge",
    "waverley": "Waverley",
    "woking": "Woking",
}

MARKET_BY_DISTRICT = {
    "Elmbridge": "elmbridge-prime",
    "Epsom and Ewell": "epsom-ewell",
    "Guildford": "guildford-district",
    "Mole Valley": "mole-valley",
    "Reigate and Banstead": "reigate-banstead",
    "Runnymede": "runnymede-wentworth",
    "Spelthorne": "spelthorne",
    "Surrey Heath": "surrey-heath",
    "Tandridge": "tandridge-oxted",
    "Waverley": "waverley-south-surrey",
    "Woking": "woking-district",
}

PROPERTY_TYPES = {
    "detached": "Detached",
    "semi-detached": "Semi Detached",
    "terraced": "Terraced",
    "flat-maisonette": "Flat Maisonette",
}
PROPERTY_TYPE_CODES = {"d": "Detached", "s": "Semi Detached", "t": "Terraced", "f": "Flat Maisonette"}

def clean(value):
    return " ".join(str(value or "").replace("\xa0", " ").split()).strip()


def uri_tail(value):
    return clean(value).rstrip("/").split("/")[-1]


def canonical_district(value):
    return CANONICAL_DISTRICTS.get(clean(value).lower(), clean(value).title())


def property_label(value):
    tail = uri_tail(value).lower()
    return PROPERTY_TYPE_CODES.get(tail, PROPERTY_TYPES.get(tail, clean(value)))


def category_label(value):
    tail = uri_tail(value).upper()
    if tail.endswith("CATEGORY-A"):
        return "A"
    if tail.endswith("CATEGORY-B"):
        return "B"
    return tail.replace("CATEGORY-", "") or "A"


def price_text(price):
    return "GBP " + f"{price:,.0f}"


def estate_for(record, existing=""):
    """Return a derived estate name; legacy values and address strings are ignored."""

    if not isinstance(record, dict):
        return ""
    return classify_estate(record).get("estate", "")


def build_address(row):
    # HMLR's secondary addressable object (for example a flat number) precedes
    # the primary object in a display address. Prefer the structured source
    # fields whenever they exist so a legacy flattened address cannot override
    # the authoritative components retained for estate classification.
    parts = [
        clean(row.get("saon")),
        clean(row.get("paon")),
        clean(row.get("street")),
        clean(row.get("locality")),
        clean(row.get("town")),
        clean(row.get("postcode")),
    ]
    if any(parts[:4]):
        return ", ".join(part for part in parts if part)
    return clean(row.get("address"))


def sparql_query(start_date=CURRENT_START_DATE, end_date=""):
    end_filter = f'FILTER(?date < "{end_date}"^^xsd:date)' if end_date else ""
    return f"""
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
PREFIX lrppi: <http://landregistry.data.gov.uk/def/ppi/>
PREFIX lrcommon: <http://landregistry.data.gov.uk/def/common/>

SELECT ?paon ?saon ?street ?locality ?town ?district ?county ?postcode ?propertyType ?price ?date ?category
WHERE {{
  ?tx lrppi:propertyAddress ?addr ;
      lrppi:pricePaid ?price ;
      lrppi:transactionDate ?date ;
      lrppi:propertyType ?propertyType ;
      lrppi:transactionCategory ?category .
  ?addr lrcommon:county "SURREY" .
  OPTIONAL {{ ?addr lrcommon:paon ?paon . }}
  OPTIONAL {{ ?addr lrcommon:saon ?saon . }}
  OPTIONAL {{ ?addr lrcommon:street ?street . }}
  OPTIONAL {{ ?addr lrcommon:locality ?locality . }}
  OPTIONAL {{ ?addr lrcommon:town ?town . }}
  OPTIONAL {{ ?addr lrcommon:district ?district . }}
  OPTIONAL {{ ?addr lrcommon:postcode ?postcode . }}
  FILTER(?price >= {PRICE_FLOOR})
  FILTER(?date >= "{start_date}"^^xsd:date)
  {end_filter}
  FILTER(?propertyType IN (
    lrcommon:detached,
    lrcommon:semi-detached,
    lrcommon:terraced,
    lrcommon:flat-maisonette
  ))
}}
ORDER BY DESC(?date)
""".strip()


def fetch_sparql_rows(start_date=CURRENT_START_DATE, end_date=""):
    body = urllib.parse.urlencode({
        "query": sparql_query(start_date, end_date),
        "format": "application/sparql-results+json",
    }).encode("utf-8")
    request = urllib.request.Request(
        SPARQL_ENDPOINT,
        data=body,
        headers={
            "Accept": "application/sparql-results+json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "INSIGHT Surrey Land Registry monthly sweep",
        },
    )
    last_error = None
    for attempt in range(FETCH_RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except Exception as error:
            last_error = error
            if attempt >= FETCH_RETRIES:
                raise
            wait = 4 * (attempt + 1)
            print(f"Retrying current HMLR query in {wait}s after {type(error).__name__}: {error}", flush=True)
            time.sleep(wait)
    if last_error and 'payload' not in locals():
        raise last_error
    rows = []
    for binding in payload.get("results", {}).get("bindings", []):
        rows.append({key: value.get("value", "") for key, value in binding.items()})
    return rows


def fetch_current_rows():
    return fetch_sparql_rows(CURRENT_START_DATE)


def archive_row(values):
    if len(values) < 15:
        return None
    try:
        price = int(values[1])
    except (TypeError, ValueError):
        return None
    property_type = clean(values[4]).upper()
    if price < PRICE_FLOOR or clean(values[13]).upper() != "SURREY" or property_type not in {"D", "S", "T", "F"}:
        return None
    if len(values) > 15 and clean(values[15]).upper() == "D":
        return None
    return {
        "tx": clean(values[0]),
        "price": price,
        "date": clean(values[2])[:10],
        "postcode": clean(values[3]),
        "propertyType": property_type,
        "paon": clean(values[7]),
        "saon": clean(values[8]),
        "street": clean(values[9]),
        "locality": clean(values[10]),
        "town": clean(values[11]),
        "district": clean(values[12]),
        "county": clean(values[13]),
        "category": clean(values[14]),
    }


def fetch_archive_year(year):
    url = ARCHIVE_URL.format(year=year)
    last_error = None
    for attempt in range(FETCH_RETRIES + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "INSIGHT historical Surrey Price Paid sweep"})
            matches = []
            scanned = 0
            with urllib.request.urlopen(request, timeout=120) as response:
                reader = csv.reader(io.TextIOWrapper(response, encoding="utf-8-sig", newline=""))
                for values in reader:
                    scanned += 1
                    row = archive_row(values)
                    if row:
                        matches.append(row)
            print(f"Archive {year}: {len(matches):,} Surrey GBP 3m+ rows from {scanned:,}", flush=True)
            return matches
        except Exception as error:
            last_error = error
            if attempt >= FETCH_RETRIES:
                raise
            wait = 5 * (attempt + 1)
            print(f"Retrying archive {year} in {wait}s after {type(error).__name__}: {error}", flush=True)
            time.sleep(wait)
    raise last_error


def csv_has_structured_address_schema(path):
    path = Path(path)
    if not path.exists():
        return False
    with path.open(newline="", encoding="utf-8") as handle:
        fields = set(csv.DictReader(handle).fieldnames or [])
    return {"paon", "saon", "street", "locality", "town", "postcode", "district"}.issubset(fields)


def rows_have_structured_address_schema(rows):
    required = {"paon", "saon", "street", "locality", "town", "postcode", "district"}
    return bool(rows) and all(required.issubset(row) for row in rows)


def historical_rows(refresh=False):
    if HISTORICAL_CSV.exists() and not refresh and csv_has_structured_address_schema(HISTORICAL_CSV):
        rows = read_csv(HISTORICAL_CSV)
        print(f"Historical cache: {len(rows):,} rows", flush=True)
        return rows
    if HISTORICAL_CSV.exists() and not csv_has_structured_address_schema(HISTORICAL_CSV):
        print("Historical cache rejected: structured HMLR address fields are missing", flush=True)
    raw = []
    for year in range(1995, 2010):
        raw.extend(fetch_archive_year(year))
    _raw_count, transactions = normalise_rows(raw)
    write_processed_csv(HISTORICAL_CSV, transactions)
    print(f"Created historical cache: {len(transactions):,} rows", flush=True)
    return transactions


def fetch_rows(use_current_cache=False, existing_transactions=None, refresh_history=False):
    history = historical_rows(refresh=refresh_history)
    if use_current_cache:
        current = [item for item in (existing_transactions or []) if clean(item.get("date")) >= CURRENT_START_DATE]
        if not rows_have_structured_address_schema(current):
            current = []
        if not current and csv_has_structured_address_schema(CURRENT_CSV):
            current = read_csv(CURRENT_CSV)
        if not current:
            raise RuntimeError("The 2010+ cache lacks structured HMLR address fields; a live refresh is required")
        print(f"Current cache: {len(current):,} rows", flush=True)
    else:
        try:
            current = fetch_current_rows()
            _raw_count, current_transactions = normalise_rows(current)
            write_processed_csv(CURRENT_CSV, current_transactions)
            print(f"Refreshed current cache: {len(current_transactions):,} rows", flush=True)
        except Exception as error:
            if not csv_has_structured_address_schema(CURRENT_CSV):
                raise
            current = read_csv(CURRENT_CSV)
            print(f"WARNING current query failed; using {len(current):,} cached rows: {error}", flush=True)
    return history + current


def read_csv(path):
    with Path(path).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def parse_window_json(text, name, default):
    match = re.search(rf"window\.{re.escape(name)}\s*=\s*(.*?);\s*(?=window\.|$)", text, re.S)
    return json.loads(match.group(1)) if match else default


def read_existing_js(path):
    path = Path(path)
    if not path.exists():
        return [], {}
    text = path.read_text(encoding="utf-8")
    return (
        parse_window_json(text, "SURREY_LAND_REG_TRANSACTIONS", []),
        parse_window_json(text, "SURREY_LAND_REG_META", {}),
    )


def stable_transaction_key(item):
    return "|".join([
        re.sub(r"[^A-Z0-9]+", " ", clean(item.get("address")).upper()).strip(),
        re.sub(r"[^A-Z0-9]", "", clean(item.get("postcode")).upper()),
        str(item.get("price", "")),
        clean(item.get("date"))[:10],
    ])


def stable_property_key(item):
    return property_record_id(item)


def source_transaction_key(item):
    """Address-independent HMLR tuple used only when it is unique on both sides."""

    return "|".join([
        re.sub(r"[^A-Z0-9]", "", clean(item.get("postcode")).upper()),
        str(item.get("price", "")),
        clean(item.get("date"))[:10],
        clean(item.get("propertyType")).upper(),
        clean(item.get("category")).upper(),
    ])


def order_insensitive_address_key(item):
    """Match the same HMLR address when PAON and SAON display order changed."""

    tokens = sorted(re.findall(r"[A-Z0-9]+", clean(item.get("address")).upper()))
    return "|".join([" ".join(tokens), source_transaction_key(item)])


def preserve_existing_enrichments(transactions, metadata, existing_transactions, existing_metadata):
    existing_by_key = {stable_transaction_key(item): item for item in existing_transactions}
    existing_by_source_key = defaultdict(list)
    existing_by_address_tokens = defaultdict(list)
    transaction_source_counts = Counter(source_transaction_key(item) for item in transactions)
    transaction_address_token_counts = Counter(order_insensitive_address_key(item) for item in transactions)
    for item in existing_transactions:
        existing_by_source_key[source_transaction_key(item)].append(item)
        existing_by_address_tokens[order_insensitive_address_key(item)].append(item)
    existing_by_property = {}
    for item in existing_transactions:
        if any(key not in BASE_TRANSACTION_FIELDS for key in item):
            existing_by_property.setdefault(stable_property_key(item), item)
    preserved = 0
    property_reused = 0
    enriched = []
    for item in transactions:
        exact = existing_by_key.get(stable_transaction_key(item), {})
        if not exact:
            source_key = source_transaction_key(item)
            candidates = existing_by_source_key.get(source_key, [])
            if len(candidates) == 1 and transaction_source_counts[source_key] == 1:
                exact = candidates[0]
        if not exact:
            address_token_key = order_insensitive_address_key(item)
            candidates = existing_by_address_tokens.get(address_token_key, [])
            if len(candidates) == 1 and transaction_address_token_counts[address_token_key] == 1:
                exact = candidates[0]
        property_donor = existing_by_property.get(stable_property_key(item), {})
        previous = {**property_donor, **exact}
        donor_extra_keys = {key for key in property_donor if key not in BASE_TRANSACTION_FIELDS}
        exact_extra_keys = {key for key in exact if key not in BASE_TRANSACTION_FIELDS}
        if donor_extra_keys - exact_extra_keys:
            property_reused += 1
        extras = {key: value for key, value in previous.items() if key not in BASE_TRANSACTION_FIELDS}
        if extras:
            preserved += 1
        enriched.append({**extras, **item})
    inherited_meta = {key: value for key, value in existing_metadata.items() if key not in BASE_METADATA_FIELDS}
    metadata = {**inherited_meta, **metadata}
    metadata["historicalExpansion"] = {
        "coverageFrom": START_DATE,
        "pre2010Transactions": sum(1 for item in transactions if item.get("date", "") < CURRENT_START_DATE),
        "existingEnrichmentsPreserved": preserved,
        "samePropertyEnrichmentsReused": property_reused,
        "newTransactionsPendingEnrichment": len(transactions) - preserved,
    }
    return enriched, metadata


def parse_price(value):
    return int(float(str(value).replace(",", "").replace("GBP", "").strip()))


def stable_transaction_id(address, postcode, price, date, property_type, category):
    identity = "|".join([
        clean(address).upper(),
        clean(postcode).upper().replace(" ", ""),
        str(price),
        clean(date),
        clean(property_type).upper(),
        clean(category).upper(),
    ])
    return "lr-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def normalise_rows(rows):
    transactions = []
    seen = set()
    raw_count = len(rows)
    estate_registry_version = load_compiled_registry()["registryVersion"]
    for row in rows:
        price = parse_price(row.get("price") or row.get("price_paid") or 0)
        if price < PRICE_FLOOR:
            continue
        date = clean(row.get("date") or row.get("deed_date"))[:10]
        if date < START_DATE:
            continue
        district = canonical_district(row.get("district"))
        market = clean(row.get("market")) or MARKET_BY_DISTRICT.get(district, "")
        if not market:
            continue
        paon = clean(row.get("paon")).upper()
        saon = clean(row.get("saon")).upper()
        street = clean(row.get("street")).upper()
        locality = clean(row.get("locality")).upper()
        town = clean(row.get("town")).upper()
        postcode = clean(row.get("postcode")).upper()
        # HMLR omits the postcode on this one transaction. Elmbridge's planning
        # record repeatedly identifies the exact site as KT11 2JJ, so apply the
        # sourced correction without weakening postcode-gated estate rules.
        if (
            not postcode
            and paon == "MANSFIELD HOUSE, 11"
            and street == "EATON PARK ROAD"
            and town == "COBHAM"
            and district == "Elmbridge"
            and date == "2012-06-13"
            and price == 3_300_000
        ):
            postcode = "KT11 2JJ"
        address_row = {**row, "paon": paon, "saon": saon, "street": street, "locality": locality, "town": town, "postcode": postcode}
        address = build_address(address_row)
        property_type = property_label(row.get("propertyType") or row.get("property_type"))
        estate_metadata = classify_estate({
            "paon": paon,
            "saon": saon,
            "street": street,
            "district": district,
            "locality": locality,
            "town": town,
            "postcode": postcode,
        })
        category = category_label(row.get("category") or row.get("transaction_category"))
        key = (address.upper(), postcode, price, date, property_type, category)
        if key in seen:
            continue
        seen.add(key)
        transactions.append({
            "market": market,
            "district": district,
            "address": address.upper(),
            "paon": paon,
            "saon": saon,
            "street": street,
            "locality": locality,
            "town": town,
            "postcode": postcode,
            "price": price,
            "priceText": price_text(price),
            "date": date,
            "propertyType": property_type,
            "estateId": estate_metadata.get("estateId", ""),
            "estate": estate_metadata.get("estate", ""),
            "estateClassification": estate_metadata.get("estateClassification", ""),
            "estateType": estate_metadata.get("estateType", ""),
            "estateRuleId": estate_metadata.get("estateRuleId", ""),
            "estateRegistryVersion": estate_metadata.get("estateRegistryVersion", estate_registry_version),
            "estateEvidenceStatus": estate_metadata.get("estateEvidenceStatus", ""),
            "estateReviewStatus": estate_metadata.get("estateReviewStatus", ""),
            "source": "HM Land Registry",
            "kind": "transaction",
            "category": category,
        })
    transactions.sort(key=lambda item: (item["date"], item["price"], item["address"]), reverse=True)
    for item in transactions:
        item["id"] = stable_transaction_id(
            item["address"], item["postcode"], item["price"], item["date"], item["propertyType"], item["category"]
        )
        item["propertyRecordId"] = property_record_id(item)
    ordered = []
    for item in transactions:
        ordered.append({
            "id": item["id"],
            "propertyRecordId": item["propertyRecordId"],
            "market": item["market"],
            "district": item["district"],
            "address": item["address"],
            "paon": item["paon"],
            "saon": item["saon"],
            "street": item["street"],
            "locality": item["locality"],
            "town": item["town"],
            "postcode": item["postcode"],
            "price": item["price"],
            "priceText": item["priceText"],
            "date": item["date"],
            "propertyType": item["propertyType"],
            "estateId": item["estateId"],
            "estate": item["estate"],
            "estateClassification": item["estateClassification"],
            "estateType": item["estateType"],
            "estateRuleId": item["estateRuleId"],
            "estateRegistryVersion": item["estateRegistryVersion"],
            "estateEvidenceStatus": item["estateEvidenceStatus"],
            "estateReviewStatus": item["estateReviewStatus"],
            "source": item["source"],
            "kind": item["kind"],
            "category": item["category"],
        })
    return raw_count, ordered


def summary_by_market(transactions):
    grouped = defaultdict(list)
    for item in transactions:
        grouped[item["market"]].append(item)
    return {
        market: {
            "count": len(items),
            "avg": round(sum(item["price"] for item in items) / len(items)),
            "latest": max(item["date"] for item in items),
            "max": max(item["price"] for item in items),
        }
        for market, items in grouped.items()
    }


def metadata(raw_count, transactions):
    estates = Counter(item["estate"] for item in transactions if item["estate"])
    estate_ids = Counter(item["estateId"] for item in transactions if item["estateId"])
    estate_types = Counter(item["estateType"] for item in transactions if item["estateType"])
    estate_registry = load_compiled_registry()
    return {
        "schemaVersion": FEED_SCHEMA_VERSION,
        "rawRows": raw_count,
        "residentialRows": len(transactions),
        "mappedTransactions": len(transactions),
        "uniquePostcodes": len({item["postcode"] for item in transactions if item["postcode"]}),
        "from": START_DATE,
        "to": max((item["date"] for item in transactions), default=START_DATE),
        "priceFloor": PRICE_FLOOR,
        "source": "HM Land Registry Price Paid Data",
        "propertyTypes": list(PROPERTY_TYPES.keys()),
        "updateCadence": "monthly",
        "officialSearch": "county=Surrey; price >= GBP 3,000,000; date >= 1995-01-01; residential property types",
        "propertyRecordSchemaVersion": PROPERTY_RECORD_SCHEMA_VERSION,
        "canonicalPropertyRecords": len({item["propertyRecordId"] for item in transactions}),
        "propertyIdentityMode": "full-normalised-address-plus-postcode-fail-closed",
        "estateSummary": dict(estates),
        "estateIdSummary": dict(estate_ids),
        "estateTypeSummary": dict(estate_types),
        "estateRegistryVersion": estate_registry["registryVersion"],
        "estateClassifierMode": "structured-exact-fail-closed",
        "estateClassificationMode": "audited-road-matrix",
        "estateStructuredFieldCoverage": {
            "rows": len(transactions),
            "rowsWithStreet": sum(1 for item in transactions if item.get("street")),
            "rowsWithPaon": sum(1 for item in transactions if item.get("paon")),
            "rowsEvaluatedAgainstRegistry": len(transactions),
        },
        "estateActiveDefinitionCount": estate_registry["metadata"]["activeDefinitionCount"],
        "estateActiveRuleCount": estate_registry["metadata"]["activeRuleCount"],
    }


def write_processed_csv(path, transactions):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "propertyRecordId", "address", "paon", "saon", "street", "locality", "town", "postcode", "district",
        "propertyType", "estateId", "estate", "estateClassification", "estateType", "estateRuleId",
        "estateRegistryVersion", "estateEvidenceStatus", "estateReviewStatus",
        "price", "date", "market", "category",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for item in transactions:
            writer.writerow({field: item[field] for field in fields})


def write_js(path, transactions, meta):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join([
        "window.SURREY_LAND_REG_TRANSACTIONS = " + json.dumps(transactions, separators=(",", ":")) + ";",
        "window.SURREY_LAND_REG_SUMMARY = " + json.dumps(summary_by_market(transactions), separators=(",", ":")) + ";",
        "window.SURREY_LAND_REG_META = " + json.dumps(meta, separators=(",", ":")) + ";",
        "",
    ])
    path.write_text(content, encoding="utf-8")


def parse_args():
    parser = argparse.ArgumentParser(description="Refresh INSIGHT Surrey Land Registry data.")
    parser.add_argument("--from-csv", default=str(DEFAULT_CSV), help="Fallback or explicit source CSV.")
    parser.add_argument("--write-csv", default=str(DEFAULT_CSV), help="Processed CSV output path.")
    parser.add_argument("--write-js", default=str(DEFAULT_JS), help="Generated JS output path.")
    parser.add_argument("--preserve-from-js", default="", help="Optional prior enriched feed used to preserve matching context fields.")
    parser.add_argument("--no-fetch", action="store_true", help="Skip the official SPARQL fetch and rebuild from CSV.")
    parser.add_argument("--use-current-cache", action="store_true", help="Download/build 1995-2009 history but reuse the checked-in 2010+ cache.")
    parser.add_argument("--refresh-history", action="store_true", help="Rebuild 1995-2009 from official structured HMLR sources.")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print a summary without writing files.")
    return parser.parse_args()


def main():
    args = parse_args()
    existing_path = Path(args.preserve_from_js) if args.preserve_from_js else Path(args.write_js)
    existing_transactions, existing_metadata = read_existing_js(existing_path)
    source = "official HMLR yearly archive + current SPARQL"
    if args.no_fetch:
        source = "local CSV"
        rows = read_csv(args.from_csv)
        if not rows_have_structured_address_schema(rows):
            raise RuntimeError("Local CSV lacks the structured HMLR address fields required for estate classification")
    else:
        try:
            rows = fetch_rows(args.use_current_cache, existing_transactions, args.refresh_history)
            if args.use_current_cache:
                source = "official HMLR yearly archive + checked-in 2010+ cache"
        except Exception as exc:
            source = f"local CSV fallback after fetch error: {exc}"
            rows = read_csv(args.from_csv)
            if not rows_have_structured_address_schema(rows):
                raise RuntimeError(
                    "Official refresh failed and the fallback CSV lacks structured HMLR address fields"
                ) from exc

    raw_count, transactions = normalise_rows(rows)
    meta = metadata(raw_count, transactions)
    transactions, meta = preserve_existing_enrichments(transactions, meta, existing_transactions, existing_metadata)
    print(f"Source: {source}")
    print(f"Transactions: {len(transactions)}")
    print(f"Latest sale date: {meta['to']}")
    print(f"Unique postcodes: {meta['uniquePostcodes']}")

    if args.dry_run:
        return 0

    write_processed_csv(Path(args.write_csv), transactions)
    write_js(Path(args.write_js), transactions, meta)
    print(f"Updated {args.write_csv}")
    print(f"Updated {args.write_js}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
