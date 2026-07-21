#!/usr/bin/env python3
"""Enrich INSIGHT sales with optional public property-context feeds.

The enrichment is deliberately source-aware: if a feed is unavailable, slow,
rate-limited, or cannot confidently return data, the script omits that section
from the transaction rather than writing placeholder fields.
"""

import argparse
import json
import math
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
DEFAULT_CACHE = ROOT / "work" / "property-context-cache.json"
CACHE_VERSION = 1
DEFAULT_FLOOD_MAX_AGE_HOURS = 30
POSTCODES_API = "https://api.postcodes.io/postcodes/"
EA_FLOOD_API = "https://environment.data.gov.uk/flood-monitoring/id/floods"
OVERPASS_API = "https://overpass-api.de/api/interpreter"
SQM_TO_SQFT = 10.76391041671


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean(value):
    return " ".join(str(value or "").replace("\xa0", " ").split()).strip()


def normalise_postcode(value):
    return re.sub(r"[^A-Z0-9]", "", clean(value).upper())


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


def numeric(value):
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


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
    """Compatibility wrapper; publication goes through the canonical writer."""

    write_canonical_js(path, transactions, meta)


def load_cache(path):
    path = Path(path)
    if not path.exists():
        return {"version": CACHE_VERSION, "postcodes": {}, "osm": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"version": CACHE_VERSION, "postcodes": {}, "osm": {}}
    if payload.get("version") != CACHE_VERSION:
        return {"version": CACHE_VERSION, "postcodes": {}, "osm": {}}
    payload.setdefault("postcodes", {})
    payload.setdefault("osm", {})
    return payload


def write_cache(path, cache):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cache["version"] = CACHE_VERSION
    cache["updatedAt"] = utc_now()
    path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def cache_fresh(record, refresh_days):
    if not record or record.get("status") != "matched":
        return False
    if refresh_days <= 0:
        return False
    try:
        updated = datetime.fromisoformat(record.get("updatedAt", "").replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - updated).days < refresh_days


def environment_agency_is_fresh(context, max_age_hours, now=None):
    if not isinstance(context, dict) or max_age_hours <= 0:
        return False
    timestamp = context.get("observedAt") or context.get("updatedAt")
    try:
        observed = datetime.fromisoformat(str(timestamp or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    checked_at = now or datetime.now(timezone.utc)
    age_hours = (checked_at - observed).total_seconds() / 3600
    return 0 <= age_hours <= max_age_hours


def flood_freshness_counts(transactions, max_age_hours, now=None):
    counts = Counter()
    for item in transactions:
        context = item.get("environmentAgency")
        if not isinstance(context, dict):
            counts["missing"] += 1
        elif environment_agency_is_fresh(context, max_age_hours, now=now):
            counts["fresh"] += 1
        else:
            counts["stale"] += 1
    return counts


def request_json(url, *, method="GET", data=None, timeout=15, retries=1, headers=None):
    body = None
    if data is not None:
        body = data.encode("utf-8")
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "INSIGHT Surrey property-context enrichment",
    }
    request_headers.update(headers or {})
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {"status": 404, "result": None, "items": []}
            if exc.code == 429 and attempt < retries:
                wait = parse_float(exc.headers.get("Retry-After")) or min(90, 20 * (attempt + 1))
                print(f"Optional feed rate limit reached; waiting {wait:.0f}s before retry {attempt + 1}/{retries}.", flush=True)
                time.sleep(wait)
                continue
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {detail[:240]}") from exc
        except urllib.error.URLError as exc:
            if attempt < retries:
                time.sleep(1.5 + attempt)
                continue
            raise RuntimeError(str(exc)) from exc
    return {}


def parse_float(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group(0)) if match else None


def format_distance(metres):
    if metres < 950:
        return f"{round(metres / 10) * 10:.0f}m"
    return f"{metres / 1000:.1f}km"


def approx_walk_time(metres):
    minutes = max(1, round((metres * 1.25) / 80))
    return f"c. {minutes} min"


def haversine_metres(lat1, lon1, lat2, lon2):
    radius = 6_371_000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def clear_context_fields(item):
    cleaned = dict(item)
    for key in ("latitude", "longitude", "lat", "lon", "geocode", "location", "environmentAgency", "openStreetMap", "osm"):
        cleaned.pop(key, None)
    return cleaned


def postcode_context(postcode, cache, args):
    key = normalise_postcode(postcode)
    if not key:
        return None
    cached = cache.setdefault("postcodes", {}).get(key)
    if cache_fresh(cached, args.geocode_refresh_days):
        return cached.get("data")
    url = POSTCODES_API + urllib.parse.quote(clean(postcode))
    payload = request_json(url, timeout=args.timeout, retries=args.retries)
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        cache["postcodes"][key] = {"status": "no_match", "updatedAt": utc_now()}
        return None
    lon = parse_float(result.get("longitude"))
    lat = parse_float(result.get("latitude"))
    if lon is None or lat is None:
        cache["postcodes"][key] = {"status": "no_match", "updatedAt": utc_now()}
        return None
    data = {
        "longitude": round(lon, 7),
        "latitude": round(lat, 7),
        "geocode": {
            "source": "Postcodes.io",
            "precision": "Postcode centroid",
            "postcodeDistrict": clean(result.get("outcode")),
            "adminDistrict": clean(result.get("admin_district")),
            "region": clean(result.get("region")),
            "country": clean(result.get("country")),
        },
    }
    cache["postcodes"][key] = {"status": "matched", "updatedAt": utc_now(), "data": data}
    return data


def flood_context(lat, lon, args):
    params = urllib.parse.urlencode({"lat": f"{lat:.7f}", "long": f"{lon:.7f}", "dist": args.flood_radius_km})
    payload = request_json(f"{EA_FLOOD_API}?{params}", timeout=args.timeout, retries=args.retries)
    if not isinstance(payload, dict) or payload.get("status") == 404 or "items" not in payload:
        raise RuntimeError("Environment Agency response did not prove the current flood-alert state")
    items = payload.get("items")
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        raise RuntimeError("Environment Agency flood-alert items were not a list")
    active = []
    for item in items if isinstance(items, list) else []:
        severity = int(parse_float(item.get("severityLevel")) or 0)
        if 1 <= severity <= 3:
            active.append(item)
    active.sort(key=lambda item: int(parse_float(item.get("severityLevel")) or 9))
    if active:
        highest = active[0]
        severity_text = clean(highest.get("severity")) or f"Severity {highest.get('severityLevel')}"
        status = f"{len(active)} active alert{'s' if len(active) != 1 else ''} within {args.flood_radius_km:g}km"
        nearest = clean(highest.get("description")) or clean((highest.get("floodArea") or {}).get("label"))
    else:
        severity_text = "None"
        status = f"No current flood alert within {args.flood_radius_km:g}km"
        nearest = ""
    observed_at = utc_now()
    return {
        "environmentAgency": {
            "floodStatus": status,
            "currentFloodAlertCount": len(active),
            "highestCurrentSeverity": severity_text,
            "nearestFloodAlert": nearest,
            "searchRadius": f"{args.flood_radius_km:g}km",
            "source": "Environment Agency Real Time flood-monitoring API",
            "observedAt": observed_at,
            "updatedAt": observed_at,
        }
    }


def _geometry_polygons(payload):
    """Return GeoJSON polygons while preserving every interior hole."""

    polygons = []
    features = payload.get("features") if isinstance(payload, dict) else None
    for feature in features if isinstance(features, list) else []:
        geometry = feature.get("geometry") if isinstance(feature, dict) else None
        if not isinstance(geometry, dict):
            continue
        coordinates = geometry.get("coordinates")
        if geometry.get("type") == "Polygon" and isinstance(coordinates, list):
            candidates = [coordinates]
        elif geometry.get("type") == "MultiPolygon" and isinstance(coordinates, list):
            candidates = coordinates
        else:
            candidates = []
        for candidate in candidates:
            parsed_rings = []
            for candidate_ring in candidate if isinstance(candidate, list) else []:
                ring = []
                for point in candidate_ring if isinstance(candidate_ring, list) else []:
                    if not isinstance(point, list) or len(point) < 2:
                        continue
                    try:
                        ring.append((float(point[0]), float(point[1])))
                    except (TypeError, ValueError):
                        continue
                if len(ring) >= 4:
                    parsed_rings.append(ring)
            if parsed_rings:
                polygons.append({"outer": parsed_rings[0], "holes": parsed_rings[1:]})
    return polygons


def _point_in_ring(lon, lat, ring):
    inside = False
    previous_lon, previous_lat = ring[-1]
    for current_lon, current_lat in ring:
        crosses = (current_lat > lat) != (previous_lat > lat)
        if crosses:
            crossing_lon = (previous_lon - current_lon) * (lat - current_lat) / (previous_lat - current_lat) + current_lon
            if lon < crossing_lon:
                inside = not inside
        previous_lon, previous_lat = current_lon, current_lat
    return inside


def _point_segment_distance_km(lon, lat, start, end):
    cos_lat = max(0.01, math.cos(math.radians(lat)))
    start_x = (start[0] - lon) * 111.320 * cos_lat
    start_y = (start[1] - lat) * 110.574
    end_x = (end[0] - lon) * 111.320 * cos_lat
    end_y = (end[1] - lat) * 110.574
    delta_x = end_x - start_x
    delta_y = end_y - start_y
    length_sq = delta_x * delta_x + delta_y * delta_y
    if length_sq <= 0:
        return math.hypot(start_x, start_y)
    projection = max(0.0, min(1.0, -(start_x * delta_x + start_y * delta_y) / length_sq))
    return math.hypot(start_x + projection * delta_x, start_y + projection * delta_y)


def _distance_to_polygons_km(lon, lat, polygons, maximum_distance_km):
    best = math.inf
    latitude_margin = maximum_distance_km / 110.574
    longitude_margin = maximum_distance_km / (111.320 * max(0.01, math.cos(math.radians(lat))))
    for polygon in polygons:
        outer = polygon.get("outer") if isinstance(polygon, dict) else None
        holes = polygon.get("holes", []) if isinstance(polygon, dict) else []
        if not outer:
            continue
        longitudes = [point[0] for point in outer]
        latitudes = [point[1] for point in outer]
        if lon < min(longitudes) - longitude_margin or lon > max(longitudes) + longitude_margin:
            continue
        if lat < min(latitudes) - latitude_margin or lat > max(latitudes) + latitude_margin:
            continue
        inside_outer = _point_in_ring(lon, lat, outer)
        inside_hole = any(_point_in_ring(lon, lat, hole) for hole in holes)
        if inside_outer and not inside_hole:
            return 0.0
        for ring in [outer, *holes]:
            for index, start in enumerate(ring):
                distance = _point_segment_distance_km(lon, lat, start, ring[(index + 1) % len(ring)])
                best = min(best, distance)
                if best <= 0:
                    return 0.0
    return best


def active_flood_snapshot(args):
    """Fetch all current alerts once and load polygons only for active alerts."""

    payload = request_json(EA_FLOOD_API, timeout=args.timeout, retries=args.retries)
    if not isinstance(payload, dict) or "items" not in payload:
        raise RuntimeError("Environment Agency bulk response did not prove the current flood-alert state")
    items = payload.get("items")
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        raise RuntimeError("Environment Agency bulk flood-alert items were not a list")

    alerts = []
    for item in items:
        if not isinstance(item, dict):
            continue
        severity = int(parse_float(item.get("severityLevel")) or 0)
        if not 1 <= severity <= 3:
            continue
        flood_area = item.get("floodArea") if isinstance(item.get("floodArea"), dict) else {}
        polygon_url = clean(flood_area.get("polygon"))
        if not polygon_url:
            raise RuntimeError(f"Active Environment Agency alert {clean(item.get('floodAreaID')) or 'unknown'} has no polygon")
        polygon_url = re.sub(r"^http://", "https://", polygon_url)
        polygon = request_json(polygon_url, timeout=args.timeout, retries=args.retries)
        polygons = _geometry_polygons(polygon)
        if not polygons:
            raise RuntimeError(f"Active Environment Agency alert {clean(item.get('floodAreaID')) or 'unknown'} has no usable polygon")
        alerts.append({"alert": item, "polygons": polygons})
        if args.pause:
            time.sleep(args.pause)
    return {"observedAt": utc_now(), "alerts": alerts}


def flood_context_from_snapshot(lat, lon, args, snapshot):
    nearby = []
    for entry in snapshot.get("alerts", []):
        polygons = entry.get("polygons")
        if polygons is None:
            polygons = [{"outer": ring, "holes": []} for ring in entry.get("rings", [])]
        distance = _distance_to_polygons_km(lon, lat, polygons, args.flood_radius_km)
        if distance <= args.flood_radius_km:
            nearby.append((int(parse_float(entry["alert"].get("severityLevel")) or 9), distance, entry["alert"]))
    nearby.sort(key=lambda entry: (entry[0], entry[1]))
    if nearby:
        highest = nearby[0][2]
        severity_text = clean(highest.get("severity")) or f"Severity {highest.get('severityLevel')}"
        status = f"{len(nearby)} active alert{'s' if len(nearby) != 1 else ''} within {args.flood_radius_km:g}km"
        nearest = clean(highest.get("description")) or clean((highest.get("floodArea") or {}).get("label"))
    else:
        severity_text = "None"
        status = f"No current flood alert within {args.flood_radius_km:g}km"
        nearest = ""
    observed_at = snapshot["observedAt"]
    return {
        "environmentAgency": {
            "floodStatus": status,
            "currentFloodAlertCount": len(nearby),
            "highestCurrentSeverity": severity_text,
            "nearestFloodAlert": nearest,
            "searchRadius": f"{args.flood_radius_km:g}km",
            "source": "Environment Agency Real Time flood-monitoring API (bulk alert polygons)",
            "observedAt": observed_at,
            "updatedAt": observed_at,
        }
    }


def overpass_query(lat, lon, radius):
    return f"""
[out:json][timeout:25];
(
  node(around:{radius},{lat:.7f},{lon:.7f})["railway"="station"];
  way(around:{radius},{lat:.7f},{lon:.7f})["railway"="station"];
  node(around:{radius},{lat:.7f},{lon:.7f})["public_transport"="station"];
  way(around:{radius},{lat:.7f},{lon:.7f})["public_transport"="station"];
  node(around:{radius},{lat:.7f},{lon:.7f})["amenity"~"school|restaurant|cafe|pub|pharmacy|doctors|hospital|bank|fuel|parking|place_of_worship|theatre|cinema"];
  way(around:{radius},{lat:.7f},{lon:.7f})["amenity"~"school|restaurant|cafe|pub|pharmacy|doctors|hospital|bank|fuel|parking|place_of_worship|theatre|cinema"];
  node(around:{radius},{lat:.7f},{lon:.7f})["shop"];
  way(around:{radius},{lat:.7f},{lon:.7f})["shop"];
);
out center tags 80;
""".strip()


def element_point(element):
    lat = parse_float(element.get("lat"))
    lon = parse_float(element.get("lon"))
    center = element.get("center") if isinstance(element.get("center"), dict) else {}
    if lat is None:
        lat = parse_float(center.get("lat"))
    if lon is None:
        lon = parse_float(center.get("lon"))
    return lat, lon


def element_type(tags):
    if tags.get("railway") == "station" or tags.get("public_transport") == "station":
        return "station"
    if tags.get("amenity"):
        return clean(tags.get("amenity")).replace("_", " ")
    if tags.get("shop"):
        return clean(tags.get("shop")).replace("_", " ")
    return "place"


def osm_context(postcode, lat, lon, cache, args):
    key = normalise_postcode(postcode) or f"{lat:.5f},{lon:.5f}"
    cached = cache.setdefault("osm", {}).get(key)
    if cache_fresh(cached, args.osm_refresh_days):
        return cached.get("data")
    payload = request_json(
        OVERPASS_API,
        method="POST",
        data="data=" + urllib.parse.quote(overpass_query(lat, lon, args.osm_radius_m)),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=args.overpass_timeout,
        retries=args.retries,
    )
    elements = payload.get("elements", []) if isinstance(payload, dict) else []
    places = []
    seen = set()
    for element in elements if isinstance(elements, list) else []:
        tags = element.get("tags") if isinstance(element.get("tags"), dict) else {}
        name = clean(tags.get("name"))
        if not name:
            continue
        point_lat, point_lon = element_point(element)
        if point_lat is None or point_lon is None:
            continue
        place_type = element_type(tags)
        dedupe = (name.lower(), place_type)
        if dedupe in seen:
            continue
        seen.add(dedupe)
        metres = haversine_metres(lat, lon, point_lat, point_lon)
        places.append({
            "name": name,
            "type": place_type.title(),
            "metres": round(metres),
            "distance": format_distance(metres),
            "walkTime": approx_walk_time(metres),
        })
    places.sort(key=lambda item: item["metres"])
    stations = [item for item in places if item["type"].lower() == "station"]
    amenities = [item for item in places if item["type"].lower() != "station"][:6]
    walks = (stations[:1] + amenities[:4])[:5]
    data = {}
    if stations:
        station = stations[0]
        data["nearestStation"] = f"{station['name']} ({station['distance']}, {station['walkTime']})"
    if amenities:
        data["amenities"] = [
            {"name": item["name"], "type": item["type"], "distance": item["distance"], "walkTime": item["walkTime"]}
            for item in amenities
        ]
    if walks:
        data["walkingDistances"] = [
            {"place": item["name"], "distance": item["distance"], "walkTime": item["walkTime"]}
            for item in walks
        ]
    if not data:
        cache["osm"][key] = {"status": "no_match", "updatedAt": utc_now()}
        return None
    data["source"] = "OpenStreetMap via Overpass API"
    output = {"openStreetMap": data}
    cache["osm"][key] = {"status": "matched", "updatedAt": utc_now(), "data": output}
    return output


def enrich_transactions(transactions, cache, args):
    enriched = []
    stats = Counter()
    disabled = set()
    flood_runtime_cache = {}
    flood_query_mode = getattr(args, "flood_query_mode", "point")
    bulk_flood_snapshot = None
    if not args.disable_environment_agency and flood_query_mode == "bulk":
        try:
            bulk_flood_snapshot = active_flood_snapshot(args)
            stats["environmentAgencyRequests"] += 1 + len(bulk_flood_snapshot["alerts"])
            stats["environmentAgencyActiveAreas"] = len(bulk_flood_snapshot["alerts"])
        except Exception as exc:
            stats["environmentAgencyErrors"] += 1
            disabled.add("environmentAgency")
            print(f"Environment Agency bulk snapshot skipped: {exc}", file=sys.stderr)
    limit = args.limit if args.limit and args.limit > 0 else None
    context_fields = ("latitude", "longitude", "geocode", "environmentAgency", "openStreetMap")
    donors = {}
    if args.missing_only:
        for transaction in transactions:
            postcode = normalise_postcode(transaction.get("postcode"))
            if postcode and parse_float(transaction.get("latitude")) is not None and parse_float(transaction.get("longitude")) is not None:
                donors.setdefault(postcode, transaction)

    for index, item in enumerate(transactions, start=1):
        output = dict(item)
        if limit and index > limit:
            enriched.append(output)
            continue

        if args.missing_only:
            donor = donors.get(normalise_postcode(item.get("postcode")), {})
            for field in context_fields:
                if field not in output and field in donor:
                    output[field] = donor[field]

        postcode_data = None
        has_coordinates = parse_float(output.get("latitude")) is not None and parse_float(output.get("longitude")) is not None
        needs_postcode_context = not args.missing_only or not has_coordinates or not output.get("geocode")
        if needs_postcode_context and "postcodes" not in disabled:
            try:
                postcode_data = postcode_context(item.get("postcode"), cache, args)
                if postcode_data:
                    output.update(postcode_data)
                    stats["postcodes"] += 1
                if args.pause:
                    time.sleep(args.pause)
            except Exception as exc:
                stats["postcodeErrors"] += 1
                print(f"Postcodes.io skipped for {item.get('id')}: {exc}", file=sys.stderr)
                if args.max_source_errors and stats["postcodeErrors"] >= args.max_source_errors:
                    disabled.add("postcodes")

        lat = parse_float(output.get("latitude"))
        lon = parse_float(output.get("longitude"))
        if lat is not None and lon is not None:
            if not args.disable_environment_agency:
                if args.missing_only and environment_agency_is_fresh(
                    output.get("environmentAgency"),
                    args.flood_max_age_hours,
                ):
                    stats["environmentAgencyFreshRetained"] += 1
                elif "environmentAgency" in disabled:
                    stats["environmentAgencySkippedAfterErrors"] += 1
                    if output.get("environmentAgency"):
                        stats["environmentAgencyRetainedAfterError"] += 1
                else:
                    flood_key = normalise_postcode(item.get("postcode")) or f"{lat:.5f},{lon:.5f}"
                    first_request = flood_key not in flood_runtime_cache
                    if first_request:
                        try:
                            if bulk_flood_snapshot is not None:
                                flood_runtime_cache[flood_key] = {
                                    "data": flood_context_from_snapshot(lat, lon, args, bulk_flood_snapshot)
                                }
                                stats["environmentAgencyEvaluations"] += 1
                            else:
                                flood_runtime_cache[flood_key] = {"data": flood_context(lat, lon, args)}
                                stats["environmentAgencyRequests"] += 1
                                if args.pause:
                                    time.sleep(args.pause)
                        except Exception as exc:
                            flood_runtime_cache[flood_key] = {"error": exc}
                            stats["environmentAgencyErrors"] += 1
                            print(f"Environment Agency skipped for {item.get('id')}: {exc}", file=sys.stderr)
                            if args.max_source_errors and stats["environmentAgencyErrors"] >= args.max_source_errors:
                                disabled.add("environmentAgency")
                    lookup = flood_runtime_cache[flood_key]
                    if lookup.get("data"):
                        output.update(lookup["data"])
                        stats["environmentAgency"] += 1
                    elif output.get("environmentAgency"):
                        stats["environmentAgencyRetainedAfterError"] += 1

            needs_osm_context = not args.missing_only or not output.get("openStreetMap")
            if needs_osm_context and not args.disable_osm and "openStreetMap" not in disabled:
                try:
                    osm = osm_context(item.get("postcode"), lat, lon, cache, args)
                    if osm:
                        output.update(osm)
                        stats["openStreetMap"] += 1
                    if args.pause:
                        time.sleep(args.pause)
                except Exception as exc:
                    stats["openStreetMapErrors"] += 1
                    print(f"OpenStreetMap skipped for {item.get('id')}: {exc}", file=sys.stderr)
                    if args.max_source_errors and stats["openStreetMapErrors"] >= args.max_source_errors:
                        disabled.add("openStreetMap")

        enriched.append(output)
        if index % args.progress_every == 0:
            print(f"Processed {index}/{len(transactions)} properties; context fields so far: {dict(stats)}")

    return enriched, stats


def parse_args():
    parser = argparse.ArgumentParser(description="Enrich INSIGHT sales with optional public property-context data.")
    parser.add_argument("--input-js", default=str(DEFAULT_INPUT_JS), help="Input INSIGHT JS feed.")
    parser.add_argument("--write-js", default=str(DEFAULT_OUTPUT_JS), help="Output INSIGHT JS feed.")
    parser.add_argument("--cache", default=str(DEFAULT_CACHE), help="Optional property-context cache path.")
    parser.add_argument("--limit", type=int, default=0, help="Only enrich the first N transactions; useful for testing.")
    parser.add_argument("--timeout", type=float, default=15, help="Standard feed request timeout in seconds.")
    parser.add_argument("--overpass-timeout", type=float, default=35, help="OpenStreetMap Overpass timeout in seconds.")
    parser.add_argument("--retries", type=int, default=1, help="Retries for transient optional-feed failures.")
    parser.add_argument("--pause", type=float, default=0.12, help="Pause between properties to keep public feeds comfortable.")
    parser.add_argument("--progress-every", type=int, default=25, help="Print progress every N processed transactions.")
    parser.add_argument("--max-source-errors", type=int, default=20, help="Disable an optional source after this many errors. Use 0 to never disable.")
    parser.add_argument("--geocode-refresh-days", type=int, default=365, help="How long to cache postcode coordinates.")
    parser.add_argument("--osm-refresh-days", type=int, default=120, help="How long to cache OSM amenity context.")
    parser.add_argument("--flood-radius-km", type=float, default=5, help="Environment Agency current-alert radius.")
    parser.add_argument(
        "--flood-max-age-hours",
        type=float,
        default=DEFAULT_FLOOD_MAX_AGE_HOURS,
        help="Maximum age for a flood observation to count as current in the publication gate.",
    )
    parser.add_argument("--osm-radius-m", type=int, default=1800, help="OpenStreetMap nearby amenity radius.")
    parser.add_argument("--disable-environment-agency", action="store_true", help="Skip Environment Agency live flood alerts.")
    parser.add_argument(
        "--flood-query-mode",
        choices=("bulk", "point"),
        default="bulk",
        help="Use one bulk alert snapshot plus official polygons, or the legacy per-point endpoint.",
    )
    parser.add_argument("--disable-osm", action="store_true", help="Skip OpenStreetMap amenities.")
    parser.add_argument("--missing-only", action="store_true", help="Preserve existing context and only populate transactions that still lack it.")
    parser.add_argument("--dry-run", action="store_true", help="Report without writing files.")
    return parser.parse_args()


def main():
    args = parse_args()
    args.progress_every = max(1, args.progress_every)
    transactions, _summary, meta = read_js(args.input_js)
    cache = load_cache(args.cache)
    print(f"Transactions: {len(transactions)}")
    enriched, stats = enrich_transactions(transactions, cache, args)
    print("Property context summary: " + ", ".join(f"{key}={value}" for key, value in sorted(stats.items())))
    if args.dry_run:
        return 0

    flood_freshness = flood_freshness_counts(enriched, args.flood_max_age_hours)
    meta["propertyContext"] = {
        "updatedAt": utc_now(),
        "postcodes": {
            "source": "Postcodes.io",
            "matched": sum(1 for item in enriched if item.get("latitude") is not None and item.get("longitude") is not None),
            "precision": "postcode centroid",
        },
        "environmentAgency": {
            "source": "Environment Agency Real Time flood-monitoring API",
            "queryMode": args.flood_query_mode,
            "records": sum(1 for item in enriched if item.get("environmentAgency")),
            "freshRecords": flood_freshness["fresh"],
            "staleRecords": flood_freshness["stale"],
            "missingRecords": flood_freshness["missing"],
            "maximumAgeHours": args.flood_max_age_hours,
            "freshnessCheckedAt": utc_now(),
            "requestFailures": stats["environmentAgencyErrors"],
            "freshObservationsRetainedWithinTtl": stats["environmentAgencyFreshRetained"],
            "retainedAfterRequestFailure": stats["environmentAgencyRetainedAfterError"],
            "skippedAfterRequestFailures": stats["environmentAgencySkippedAfterErrors"],
            "type": "current flood alerts within configured radius",
        },
        "openStreetMap": {
            "source": "OpenStreetMap via Overpass API",
            "records": sum(1 for item in enriched if item.get("openStreetMap")),
            "type": "nearby amenities and approximate walking-time labels",
        },
    }
    write_cache(args.cache, cache)
    write_canonical_js(args.write_js, enriched, meta)
    print(f"Updated {args.write_js}")
    print(f"Updated {args.cache}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
