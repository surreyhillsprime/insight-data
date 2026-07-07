#!/usr/bin/env python3
"""Six-week INSIGHT enrichment for OS Open UPRN geometry/linkage."""

import argparse
import csv
import os
import sys
import urllib.request
from collections import Counter, defaultdict
from io import StringIO

from insight_data_utils import (
    DEFAULT_INPUT_JS,
    clean,
    ensure_coordinates,
    format_distance,
    haversine_metres,
    load_cache,
    normalise_postcode,
    parse_float,
    read_js,
    utc_now,
    write_cache,
    write_js,
)


CACHE_VERSION = 1
DEFAULT_CACHE = DEFAULT_INPUT_JS.parents[1] / "work" / "os-uprn-cache.json"
DEFAULT_OS_CSV = DEFAULT_INPUT_JS.parents[1] / "work" / "os-open-uprn-surrey.csv"

SURREY_BOUNDS = {
    "lat_min": 51.03,
    "lat_max": 51.55,
    "lon_min": -0.90,
    "lon_max": 0.12,
    "e_min": 475_000,
    "e_max": 545_000,
    "n_min": 125_000,
    "n_max": 180_000,
}


def try_transformer():
    try:
        from pyproj import Transformer
    except Exception:
        return None
    return Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True)


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


def read_text(path_or_url, args):
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


def in_surrey_bounds(lat, lon):
    return (
        SURREY_BOUNDS["lat_min"] <= lat <= SURREY_BOUNDS["lat_max"]
        and SURREY_BOUNDS["lon_min"] <= lon <= SURREY_BOUNDS["lon_max"]
    )


def uprn_rows(args):
    source = os.environ.get("OS_OPEN_UPRN_CSV_URL", "").strip()
    text = ""
    if source:
        text = read_text(source, args)
        if text:
            DEFAULT_OS_CSV.parent.mkdir(parents=True, exist_ok=True)
            DEFAULT_OS_CSV.write_text(text, encoding="utf-8")
    if not text:
        text = read_text(str(DEFAULT_OS_CSV), args)
    if not text:
        return [], False

    transformer = try_transformer()
    rows = []
    for raw in csv.DictReader(StringIO(text)):
        uprn = column(raw, ["UPRN", "uprn"])
        if not uprn:
            continue
        lat = parse_float(column(raw, ["LATITUDE", "Latitude", "lat"]))
        lon = parse_float(column(raw, ["LONGITUDE", "Longitude", "LONG", "lon"]))
        easting = parse_float(column(raw, ["X_COORDINATE", "X", "EASTING", "Easting"]))
        northing = parse_float(column(raw, ["Y_COORDINATE", "Y", "NORTHING", "Northing"]))
        if (lat is None or lon is None) and easting is not None and northing is not None:
            if not (SURREY_BOUNDS["e_min"] <= easting <= SURREY_BOUNDS["e_max"] and SURREY_BOUNDS["n_min"] <= northing <= SURREY_BOUNDS["n_max"]):
                continue
            if transformer:
                lon, lat = transformer.transform(easting, northing)
        if lat is None or lon is None or not in_surrey_bounds(lat, lon):
            continue
        postcode = normalise_postcode(column(raw, ["POSTCODE", "Postcode", "POSTCODE_LOCATOR"]))
        rows.append({
            "uprn": uprn,
            "lat": lat,
            "lon": lon,
            "postcode": postcode,
            "source": "OS Open UPRN",
        })
    return rows, bool(transformer)


def grid_key(lat, lon):
    return (round(lat, 2), round(lon, 2))


def build_index(rows):
    by_postcode = defaultdict(list)
    by_grid = defaultdict(list)
    for row in rows:
        if row.get("postcode"):
            by_postcode[row["postcode"]].append(row)
        by_grid[grid_key(row["lat"], row["lon"])].append(row)
    return by_postcode, by_grid


def nearby_grid_rows(by_grid, lat, lon):
    base_lat, base_lon = grid_key(lat, lon)
    rows = []
    for lat_offset in (-0.02, -0.01, 0, 0.01, 0.02):
        for lon_offset in (-0.02, -0.01, 0, 0.01, 0.02):
            rows.extend(by_grid.get((round(base_lat + lat_offset, 2), round(base_lon + lon_offset, 2)), []))
    return rows


def match_uprn(item, lat, lon, by_postcode, by_grid, args):
    postcode = normalise_postcode(item.get("postcode"))
    candidates = by_postcode.get(postcode, []) if postcode else []
    if not candidates:
        candidates = nearby_grid_rows(by_grid, lat, lon)
    best = None
    best_distance = None
    for candidate in candidates:
        metres = haversine_metres(lat, lon, candidate["lat"], candidate["lon"])
        if best_distance is None or metres < best_distance:
            best = candidate
            best_distance = metres
    if not best or best_distance is None or best_distance > args.max_match_distance_m:
        return None
    return {
        "ordnanceSurvey": {
            "source": "OS Open UPRN",
            "updatedAt": utc_now(),
            "uprn": best["uprn"],
            "uprnMatchDistance": format_distance(best_distance),
            "uprnPrecision": "Nearest OS Open UPRN to available coordinate",
        }
    }


def enrich_transactions(transactions, cache, args):
    rows, transformer_available = uprn_rows(args)
    if not rows:
        print("No OS Open UPRN CSV found; OS enrichment skipped.")
        return transactions, Counter(), False, transformer_available
    print(f"Loaded {len(rows)} Surrey UPRN rows.")
    by_postcode, by_grid = build_index(rows)
    enriched = []
    stats = Counter()
    limit = args.limit if args.limit and args.limit > 0 else None

    for index, item in enumerate(transactions, start=1):
        output = dict(item)
        if limit and index > limit:
            enriched.append(output)
            continue
        try:
            lat, lon, coord_data = ensure_coordinates(item, cache, args)
            if coord_data:
                output.update(coord_data)
                stats["postcodes"] += 1
        except Exception as exc:
            stats["geocodeErrors"] += 1
            print(f"Postcode geocode skipped for {item.get('id')}: {exc}", file=sys.stderr)
            lat, lon = None, None

        if lat is not None and lon is not None:
            match = match_uprn(item, lat, lon, by_postcode, by_grid, args)
            if match:
                existing = output.get("ordnanceSurvey") if isinstance(output.get("ordnanceSurvey"), dict) else {}
                merged = dict(existing)
                merged.update(match["ordnanceSurvey"])
                output["ordnanceSurvey"] = merged
                stats["uprnMatches"] += 1
        enriched.append(output)
        if index % args.progress_every == 0:
            print(f"Processed {index}/{len(transactions)} properties; OS matches so far: {dict(stats)}", flush=True)
    return enriched, stats, True, transformer_available


def parse_args():
    parser = argparse.ArgumentParser(description="Refresh INSIGHT OS Open UPRN matching.")
    parser.add_argument("--input-js", default=str(DEFAULT_INPUT_JS), help="Input INSIGHT JS feed.")
    parser.add_argument("--write-js", default=str(DEFAULT_INPUT_JS), help="Output INSIGHT JS feed.")
    parser.add_argument("--cache", default=str(DEFAULT_CACHE), help="OS cache path.")
    parser.add_argument("--limit", type=int, default=0, help="Only enrich the first N records.")
    parser.add_argument("--timeout", type=float, default=60, help="CSV/API timeout in seconds.")
    parser.add_argument("--retries", type=int, default=1, help="Retries for postcode geocoding.")
    parser.add_argument("--geocode-refresh-days", type=int, default=365, help="Postcode coordinate cache lifetime.")
    parser.add_argument("--max-match-distance-m", type=int, default=150, help="Maximum nearest-UPRN match distance.")
    parser.add_argument("--progress-every", type=int, default=25, help="Print progress every N records.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write outputs.")
    return parser.parse_args()


def main():
    args = parse_args()
    args.progress_every = max(1, args.progress_every)
    transactions, _summary, meta = read_js(args.input_js)
    cache = load_cache(args.cache, CACHE_VERSION)
    print(f"Transactions: {len(transactions)}")
    enriched, stats, source_loaded, transformer_available = enrich_transactions(transactions, cache, args)
    print("OS UPRN summary: " + ", ".join(f"{key}={value}" for key, value in sorted(stats.items())))
    if args.dry_run:
        return 0

    meta["osRefresh"] = {
        "updatedAt": utc_now(),
        "source": "OS Open UPRN",
        "sourceLoaded": source_loaded,
        "bngTransformerAvailable": transformer_available,
        "uprnMatches": stats.get("uprnMatches", 0),
        "maxMatchDistanceMetres": args.max_match_distance_m,
    }
    write_cache(args.cache, cache, CACHE_VERSION)
    write_js(args.write_js, enriched, meta)
    print(f"Updated {args.write_js}")
    print(f"Updated {args.cache}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
