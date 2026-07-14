#!/usr/bin/env python3
"""Weekly INSIGHT enrichment for planning constraints, heritage, and schools."""

import argparse
import csv
import os
import sys
import time
import urllib.request
from collections import Counter
from io import StringIO

from insight_data_utils import (
    DEFAULT_INPUT_JS,
    approx_walk_time,
    cache_fresh,
    clean,
    coordinates_from_item,
    ensure_coordinates,
    format_distance,
    haversine_metres,
    load_cache,
    normalise_postcode,
    parse_float,
    postcode_lookup,
    read_js,
    request_json,
    utc_now,
    write_cache,
    write_js,
)


CACHE_VERSION = 1
DEFAULT_CACHE = DEFAULT_INPUT_JS.parents[1] / "work" / "weekly-context-cache.json"
DEFAULT_SCHOOLS_CACHE = DEFAULT_INPUT_JS.parents[1] / "work" / "schools.csv"
PLANNING_ENTITY_API = "https://www.planning.data.gov.uk/entity.json"

CONSTRAINT_DATASETS = [
    "listed-building",
    "conservation-area",
    "scheduled-monument",
    "heritage-at-risk",
    "article-4-direction-area",
    "tree-preservation-zone",
    "green-belt",
    "flood-risk-zone",
    "ancient-woodland",
    "area-of-outstanding-natural-beauty",
    "site-of-special-scientific-interest",
]

CONSTRAINT_FIELDS = {
    "conservation-area": ("conservationArea", "Conservation area"),
    "green-belt": ("greenBelt", "Green belt"),
    "article-4-direction-area": ("article4", "Article 4"),
    "tree-preservation-zone": ("treePreservationZone", "Tree preservation zone"),
    "flood-risk-zone": ("floodRiskZone", "Flood risk zone"),
    "ancient-woodland": ("ancientWoodland", "Ancient woodland"),
    "area-of-outstanding-natural-beauty": ("aonb", "AONB / National landscape"),
    "site-of-special-scientific-interest": ("sssi", "SSSI"),
    "scheduled-monument": ("scheduledMonument", "Scheduled monument"),
    "heritage-at-risk": ("heritageAtRisk", "Heritage at risk"),
}


def entity_list(payload):
    if not isinstance(payload, dict):
        return []
    for key in ("entities", "items", "results", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def entity_value(entity, names):
    if not isinstance(entity, dict):
        return ""
    for name in names:
        for variant in (name, name.replace("_", "-"), name.replace("-", "_")):
            value = entity.get(variant)
            if value not in (None, ""):
                return value
    return ""


def short_names(items, limit=3):
    values = []
    seen = set()
    for item in items:
        name = clean(entity_value(item, ["name", "reference", "entity", "listed_building_grade"]))
        if not name:
            name = clean(item.get("dataset"))
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        values.append(name)
    if not values:
        return ""
    if len(values) > limit:
        return ", ".join(values[:limit]) + f" +{len(values) - limit} more"
    return ", ".join(values)


def constraints_cache_key(item):
    postcode = normalise_postcode(item.get("postcode"))
    uprn = clean(item.get("uprn") or (item.get("ordnanceSurvey") or {}).get("uprn"))
    return uprn or postcode or clean(item.get("id"))


def constraints_for_item(item, lat, lon, cache, args):
    key = constraints_cache_key(item)
    store = cache.setdefault("planningConstraints", {})
    cached = store.get(key)
    if cache_fresh(cached, args.refresh_days * 24 * 60 * 60):
        return cached.get("data")
    params = {
        "latitude": f"{lat:.7f}",
        "longitude": f"{lon:.7f}",
        "dataset": CONSTRAINT_DATASETS,
        "limit": 100,
    }
    payload = request_json(
        PLANNING_ENTITY_API,
        params=params,
        timeout=args.timeout,
        retries=args.retries,
        user_agent="INSIGHT weekly planning constraints",
    )
    by_dataset = {}
    for entity in entity_list(payload):
        dataset = clean(entity.get("dataset"))
        if dataset:
            by_dataset.setdefault(dataset, []).append(entity)

    constraints = {
        "source": "Planning Data API",
        "updatedAt": utc_now(),
        "constraintCount": sum(len(value) for value in by_dataset.values()),
    }
    for dataset, (field, label) in CONSTRAINT_FIELDS.items():
        items = by_dataset.get(dataset, [])
        if items:
            names = short_names(items)
            constraints[field] = f"{label}: {names}" if names else label

    data = {}
    useful_constraints = {key: value for key, value in constraints.items() if key not in ("source", "updatedAt", "constraintCount") and value}
    if useful_constraints:
        data["planningConstraints"] = constraints

    listed = by_dataset.get("listed-building", [])
    if listed:
        first = listed[0]
        grade = clean(entity_value(first, ["listed_building_grade", "grade"]))
        reference = clean(entity_value(first, ["reference", "list_entry_number", "entry_number"]))
        name = clean(entity_value(first, ["name", "description"]))
        data["historicEngland"] = {
            "source": "Planning Data API listed-building dataset",
            "updatedAt": utc_now(),
            "listedStatus": "Listed building match",
            "grade": grade,
            "listEntryNumber": reference,
            "nearestListedBuilding": name,
        }

    store[key] = {"status": "matched" if data else "no_match", "updatedAt": utc_now(), "data": data}
    return data


def column(row, names):
    lowered = {key.lower().strip(): key for key in row.keys()}
    for name in names:
        key = lowered.get(name.lower())
        if key is not None and row.get(key) not in (None, ""):
            return clean(row.get(key))
    for key in row.keys():
        compact = key.lower().replace(" ", "").replace("_", "").replace("-", "")
        for name in names:
            if compact == name.lower().replace(" ", "").replace("_", "").replace("-", ""):
                return clean(row.get(key))
    return ""


def read_schools_csv(path_or_url, args):
    if not path_or_url:
        return ""
    if path_or_url.startswith(("http://", "https://")):
        with urllib.request.urlopen(path_or_url, timeout=args.timeout) as response:
            return response.read().decode("utf-8-sig", errors="replace")
    path = os.path.expanduser(path_or_url)
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
        return handle.read()


def try_transformer():
    try:
        from pyproj import Transformer
    except Exception:
        return None
    return Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True)


def school_rows(cache, args):
    source = os.environ.get("SCHOOLS_CSV_URL", "").strip()
    local = str(DEFAULT_SCHOOLS_CACHE)
    text = ""
    if source:
        text = read_schools_csv(source, args)
        if text:
            DEFAULT_SCHOOLS_CACHE.parent.mkdir(parents=True, exist_ok=True)
            DEFAULT_SCHOOLS_CACHE.write_text(text, encoding="utf-8")
    if not text:
        text = read_schools_csv(local, args)
    if not text:
        return []

    rows = []
    transformer = try_transformer()
    for raw in csv.DictReader(StringIO(text)):
        name = column(raw, ["EstablishmentName", "SchoolName", "Name"])
        postcode = column(raw, ["Postcode", "SchoolPostcode"])
        if not name:
            continue
        status = column(raw, ["EstablishmentStatus (name)", "EstablishmentStatus", "Status"])
        if status and "open" not in status.lower():
            continue
        lat = parse_float(column(raw, ["Latitude", "Lat"]))
        lon = parse_float(column(raw, ["Longitude", "Lon", "Long"]))
        easting = parse_float(column(raw, ["Easting", "EASTING", "X_COORDINATE", "X"]))
        northing = parse_float(column(raw, ["Northing", "NORTHING", "Y_COORDINATE", "Y"]))
        if (lat is None or lon is None) and transformer and easting is not None and northing is not None:
            lon, lat = transformer.transform(easting, northing)
        if (lat is None or lon is None) and postcode:
            try:
                geocode = postcode_lookup(postcode, cache, refresh_days=args.geocode_refresh_days, timeout=args.timeout, retries=args.retries)
                if geocode:
                    lat = geocode["latitude"]
                    lon = geocode["longitude"]
            except Exception as exc:
                print(f"School postcode skipped for {postcode}: {exc}", file=sys.stderr)
        if lat is None or lon is None:
            continue
        rows.append({
            "name": name,
            "postcode": postcode,
            "latitude": lat,
            "longitude": lon,
            "phase": column(raw, ["PhaseOfEducation (name)", "PhaseOfEducation", "Phase", "EducationPhase"]),
            "rating": column(raw, ["OfstedRating", "OverallEffectiveness", "Overall effectiveness", "LatestOfstedOverallEffectiveness"]),
            "type": column(raw, ["TypeOfEstablishment (name)", "TypeOfEstablishment", "Type"]),
            "urn": column(raw, ["URN", "Urn"]),
        })
    return rows


def schools_for_item(lat, lon, schools, args):
    nearby = []
    for school in schools:
        metres = haversine_metres(lat, lon, school["latitude"], school["longitude"])
        if metres > args.school_radius_m:
            continue
        entry = {
            "name": school["name"],
            "phase": school.get("phase"),
            "rating": school.get("rating"),
            "postcode": school.get("postcode"),
            "urn": school.get("urn"),
            "metres": round(metres),
            "distance": format_distance(metres),
            "walkTime": approx_walk_time(metres),
        }
        nearby.append({key: value for key, value in entry.items() if value not in ("", None)})
    nearby.sort(key=lambda row: row["metres"])
    nearest = nearby[: args.max_schools_per_property]
    if not nearest:
        return {}
    rated = [school for school in nearest if school.get("rating")]
    best = ""
    for rating_name in ("Outstanding", "Good"):
        match = next((school for school in rated if rating_name.lower() in school.get("rating", "").lower()), None)
        if match:
            best = f"{match['rating']}: {match['name']} ({match['distance']})"
            break
    if not best and rated:
        best = f"{rated[0]['rating']}: {rated[0]['name']} ({rated[0]['distance']})"
    return {
        "ofsted": {
            "source": "DfE / Ofsted school data",
            "updatedAt": utc_now(),
            "nearestSchools": nearest,
            "bestNearbyRating": best,
            "searchRadius": format_distance(args.school_radius_m),
        }
    }


def enrich_transactions(transactions, cache, args):
    enriched = []
    stats = Counter()
    disabled = set()
    schools = [] if args.disable_schools else school_rows(cache, args)
    if schools:
        print(f"Loaded {len(schools)} school rows.")
    elif not args.disable_schools:
        print("No school CSV supplied; school enrichment skipped.")
    limit = args.limit if args.limit and args.limit > 0 else None

    for index, item in enumerate(transactions, start=1):
        output = dict(item)
        if limit and index > limit:
            enriched.append(output)
            continue
        if "geocode" in disabled:
            lat, lon = coordinates_from_item(output)
        else:
            try:
                lat, lon, coord_data = ensure_coordinates(item, cache, args)
                if coord_data:
                    output.update(coord_data)
                    stats["postcodes"] += 1
            except Exception as exc:
                stats["geocodeErrors"] += 1
                print(f"Postcode geocode skipped for {item.get('id')}: {exc}", file=sys.stderr)
                if args.max_source_errors and stats["geocodeErrors"] >= args.max_source_errors:
                    disabled.add("geocode")
                lat, lon = None, None

        if lat is not None and lon is not None and "planningConstraints" not in disabled and not args.disable_planning_constraints:
            try:
                constraints = constraints_for_item(item, lat, lon, cache, args)
                if constraints:
                    output.update(constraints)
                    if constraints.get("planningConstraints"):
                        stats["planningConstraints"] += 1
                    if constraints.get("historicEngland"):
                        stats["historicEngland"] += 1
            except Exception as exc:
                stats["planningConstraintErrors"] += 1
                print(f"Planning constraints skipped for {item.get('id')}: {exc}", file=sys.stderr)
                if args.max_source_errors and stats["planningConstraintErrors"] >= args.max_source_errors:
                    disabled.add("planningConstraints")

        if lat is not None and lon is not None and schools and not args.disable_schools:
            school_data = schools_for_item(lat, lon, schools, args)
            if school_data:
                output.update(school_data)
                stats["schools"] += 1

        enriched.append(output)
        if args.pause:
            time.sleep(args.pause)
        if index % args.progress_every == 0:
            print(f"Processed {index}/{len(transactions)} properties; weekly fields so far: {dict(stats)}", flush=True)

    return enriched, stats, bool(schools)


def parse_args():
    parser = argparse.ArgumentParser(description="Refresh INSIGHT weekly contextual data.")
    parser.add_argument("--input-js", default=str(DEFAULT_INPUT_JS), help="Input INSIGHT JS feed.")
    parser.add_argument("--write-js", default=str(DEFAULT_INPUT_JS), help="Output INSIGHT JS feed.")
    parser.add_argument("--cache", default=str(DEFAULT_CACHE), help="Weekly context cache path.")
    parser.add_argument("--limit", type=int, default=0, help="Only enrich the first N records.")
    parser.add_argument("--timeout", type=float, default=25, help="API request timeout in seconds.")
    parser.add_argument("--retries", type=int, default=2, help="Retries for transient API failures.")
    parser.add_argument("--pause", type=float, default=0.12, help="Pause between property lookups.")
    parser.add_argument("--progress-every", type=int, default=25, help="Print progress every N records.")
    parser.add_argument("--max-source-errors", type=int, default=25, help="Disable a source after this many errors.")
    parser.add_argument("--refresh-days", type=int, default=6, help="Planning constraint cache lifetime.")
    parser.add_argument("--geocode-refresh-days", type=int, default=365, help="Postcode coordinate cache lifetime.")
    parser.add_argument("--school-radius-m", type=int, default=4000, help="Nearby school radius.")
    parser.add_argument("--max-schools-per-property", type=int, default=5, help="Store this many nearby schools.")
    parser.add_argument("--disable-planning-constraints", action="store_true", help="Skip Planning Data constraints.")
    parser.add_argument("--disable-schools", action="store_true", help="Skip school enrichment.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write outputs.")
    return parser.parse_args()


def main():
    args = parse_args()
    args.progress_every = max(1, args.progress_every)
    transactions, _summary, meta = read_js(args.input_js)
    cache = load_cache(args.cache, CACHE_VERSION)
    print(f"Transactions: {len(transactions)}")
    enriched, stats, schools_loaded = enrich_transactions(transactions, cache, args)
    print("Weekly context summary: " + ", ".join(f"{key}={value}" for key, value in sorted(stats.items())))
    if args.dry_run:
        return 0

    meta["weeklyContext"] = {
        "updatedAt": utc_now(),
        "planningConstraints": {
            "source": "Planning Data API",
            "records": sum(1 for item in enriched if item.get("planningConstraints")),
        },
        "historicEngland": {
            "source": "Planning Data API listed-building dataset",
            "records": sum(1 for item in enriched if item.get("historicEngland")),
        },
        "schools": {
            "source": "DfE / Ofsted school data",
            "records": sum(1 for item in enriched if item.get("ofsted")),
            "loaded": schools_loaded or any(item.get("ofsted") for item in enriched),
        },
    }
    write_cache(args.cache, cache, CACHE_VERSION)
    write_js(args.write_js, enriched, meta)
    print(f"Updated {args.write_js}")
    print(f"Updated {args.cache}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
