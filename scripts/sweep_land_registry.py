#!/usr/bin/env python3
"""Refresh the Surrey GBP 3m+ Land Registry ledger for INSIGHT.

The script fetches HM Land Registry Price Paid Data via the official SPARQL
endpoint, falls back to the last local CSV if the endpoint is unavailable, and
regenerates outputs/surrey-transactions.js for the static app.
"""

import argparse
import csv
import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path


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
    "id", "market", "district", "address", "town", "postcode", "price", "priceText",
    "date", "propertyType", "estate", "source", "kind", "category",
}
BASE_METADATA_FIELDS = {
    "rawRows", "residentialRows", "mappedTransactions", "uniquePostcodes", "from", "to",
    "priceFloor", "source", "propertyTypes", "updateCadence", "officialSearch", "estateSummary",
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

ESTATE_RULES = [
    ("Wentworth", ["WENTWORTH", "ABBOTS DRIVE", "NORTH DRIVE", "SOUTH DRIVE", "WEST DRIVE", "PINEWOOD ROAD", "VIRGINIA AVENUE", "GORSE HILL ROAD", "WOODLANDS ROAD WEST"]),
    ("St George's Hill", ["ST GEORGE", "OLD AVENUE", "EAST ROAD", "WEST ROAD", "CAMP END ROAD", "GODOLPHIN ROAD", "BOWATER RIDGE", "SOUTH ROAD"]),
    ("Crown Estate / Oxshott", ["CROWN ESTATE", "CROWN DRIVE", "LEYS ROAD", "PRINCES DRIVE", "QUEENS DRIVE", "STOKESHEATH ROAD", "MOLES HILL", "BIRDS HILL DRIVE", "GOLDRINGS ROAD"]),
    ("Burwood Park", ["BURWOOD PARK", "CRANLEY ROAD", "INCE ROAD", "ONSLOW ROAD", "CHARGATE CLOSE", "FRIARS CLOSE", "ERISWELL CRESCENT"]),
    ("Blackhills", ["BLACKHILLS", "BLACK HILLS"]),
    ("Fairmile", ["FAIRMILE AVENUE", "FAIRMILE LANE", "FAIRMILE PARK"]),
    ("Ashley Park", ["ASHLEY PARK AVENUE", "ASHLEY RISE", "ASHLEY PARK"]),
    ("Pachesham Park", ["PACHESHAM PARK"]),
    ("Eaton Park", ["EATON PARK", "EATON PARK ROAD"]),
]


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


def estate_for(address, existing=""):
    if clean(existing):
        return clean(existing)
    upper_address = clean(address).upper()
    for estate, terms in ESTATE_RULES:
        if any(term in upper_address for term in terms):
            return estate
    return ""


def build_address(row):
    if clean(row.get("address")):
        return clean(row.get("address"))
    parts = [
        clean(row.get("paon")),
        clean(row.get("saon")),
        clean(row.get("street")),
        clean(row.get("locality")),
        clean(row.get("town")),
        clean(row.get("postcode")),
    ]
    return ", ".join(part for part in parts if part)


def sparql_query(start_date=CURRENT_START_DATE):
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
  FILTER(?propertyType IN (
    lrcommon:detached,
    lrcommon:semi-detached,
    lrcommon:terraced,
    lrcommon:flat-maisonette
  ))
}}
ORDER BY DESC(?date)
""".strip()


def fetch_current_rows():
    body = urllib.parse.urlencode({
        "query": sparql_query(),
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


def historical_rows():
    if HISTORICAL_CSV.exists():
        rows = read_csv(HISTORICAL_CSV)
        print(f"Historical cache: {len(rows):,} rows", flush=True)
        return rows
    raw = []
    for year in range(1995, 2010):
        raw.extend(fetch_archive_year(year))
    _raw_count, transactions = normalise_rows(raw)
    write_processed_csv(HISTORICAL_CSV, transactions)
    print(f"Created historical cache: {len(transactions):,} rows", flush=True)
    return transactions


def fetch_rows(use_current_cache=False, existing_transactions=None):
    history = historical_rows()
    if use_current_cache:
        current = [item for item in (existing_transactions or []) if clean(item.get("date")) >= CURRENT_START_DATE]
        if not current:
            current = read_csv(CURRENT_CSV)
        print(f"Current cache: {len(current):,} rows", flush=True)
    else:
        try:
            current = fetch_current_rows()
        except Exception as error:
            current = [item for item in (existing_transactions or []) if clean(item.get("date")) >= CURRENT_START_DATE]
            if not current:
                if not CURRENT_CSV.exists():
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
    return "|".join([
        re.sub(r"[^A-Z0-9]+", " ", clean(item.get("address")).upper()).strip(),
        re.sub(r"[^A-Z0-9]", "", clean(item.get("postcode")).upper()),
    ])


def preserve_existing_enrichments(transactions, metadata, existing_transactions, existing_metadata):
    existing_by_key = {stable_transaction_key(item): item for item in existing_transactions}
    existing_by_property = {}
    for item in existing_transactions:
        if any(key not in BASE_TRANSACTION_FIELDS for key in item):
            existing_by_property.setdefault(stable_property_key(item), item)
    preserved = 0
    property_reused = 0
    enriched = []
    for item in transactions:
        exact = existing_by_key.get(stable_transaction_key(item), {})
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


def normalise_rows(rows):
    transactions = []
    seen = set()
    raw_count = len(rows)
    for row in rows:
        price = parse_price(row.get("price", 0))
        if price < PRICE_FLOOR:
            continue
        date = clean(row.get("date"))
        if date < START_DATE:
            continue
        district = canonical_district(row.get("district"))
        market = clean(row.get("market")) or MARKET_BY_DISTRICT.get(district, "")
        if not market:
            continue
        address = build_address(row)
        town = clean(row.get("town")).upper()
        postcode = clean(row.get("postcode")).upper()
        property_type = property_label(row.get("propertyType"))
        estate = estate_for(address, row.get("estate"))
        category = category_label(row.get("category"))
        key = (address.upper(), postcode, price, date, property_type, category)
        if key in seen:
            continue
        seen.add(key)
        transactions.append({
            "market": market,
            "district": district,
            "address": address.upper(),
            "town": town,
            "postcode": postcode,
            "price": price,
            "priceText": price_text(price),
            "date": date,
            "propertyType": property_type,
            "estate": estate,
            "source": "HM Land Registry",
            "kind": "transaction",
            "category": category,
        })
    transactions.sort(key=lambda item: (item["date"], item["price"], item["address"]), reverse=True)
    for index, item in enumerate(transactions, start=1):
        item["id"] = f"lr-{index}"
    ordered = []
    for item in transactions:
        ordered.append({
            "id": item["id"],
            "market": item["market"],
            "district": item["district"],
            "address": item["address"],
            "town": item["town"],
            "postcode": item["postcode"],
            "price": item["price"],
            "priceText": item["priceText"],
            "date": item["date"],
            "propertyType": item["propertyType"],
            "estate": item["estate"],
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
    return {
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
        "estateSummary": dict(estates),
    }


def write_processed_csv(path, transactions):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["address", "town", "postcode", "district", "propertyType", "estate", "price", "date", "market", "category"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
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
    else:
        try:
            rows = fetch_rows(args.use_current_cache, existing_transactions)
            if args.use_current_cache:
                source = "official HMLR yearly archive + checked-in 2010+ cache"
        except Exception as exc:
            source = f"local CSV fallback after fetch error: {exc}"
            rows = read_csv(args.from_csv)

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
    write_processed_csv(CURRENT_CSV, [item for item in transactions if item.get("date", "") >= CURRENT_START_DATE])
    write_js(Path(args.write_js), transactions, meta)
    print(f"Updated {args.write_csv}")
    print(f"Updated {args.write_js}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
