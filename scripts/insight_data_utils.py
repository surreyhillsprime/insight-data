#!/usr/bin/env python3
"""Shared helpers for INSIGHT GitHub data refresh jobs."""

import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_JS = ROOT / "outputs" / "surrey-transactions.js"
POSTCODES_API = "https://api.postcodes.io/postcodes/"
FEED_SCHEMA_VERSION = 2


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean(value):
    return " ".join(str(value or "").replace("\xa0", " ").split()).strip()


def normalise_postcode(value):
    return re.sub(r"[^A-Z0-9]", "", clean(value).upper())


def parse_float(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        value = float(value)
        return value if math.isfinite(value) else None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group(0)) if match else None


def numeric(value):
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def parse_window_json(text, name, default):
    prefix = f"window.{name} = "
    for line in text.splitlines():
        if line.startswith(prefix) and line.endswith(";"):
            return json.loads(line[len(prefix) : -1])
    return default


def read_js(path=DEFAULT_INPUT_JS):
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
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = dict(meta)
    meta["schemaVersion"] = FEED_SCHEMA_VERSION
    content = "\n".join(
        [
            "window.SURREY_LAND_REG_TRANSACTIONS = " + json.dumps(transactions, separators=(",", ":")) + ";",
            "window.SURREY_LAND_REG_SUMMARY = " + json.dumps(summary_by_market(transactions), separators=(",", ":")) + ";",
            "window.SURREY_LAND_REG_META = " + json.dumps(meta, separators=(",", ":")) + ";",
            "",
        ]
    )
    path.write_text(content, encoding="utf-8")


def load_cache(path, version):
    path = Path(path)
    if not path.exists():
        return {"version": version}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"version": version}
    if payload.get("version") != version:
        return {"version": version}
    return payload


def write_cache(path, cache, version):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cache["version"] = version
    cache["updatedAt"] = utc_now()
    path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def cache_fresh(record, refresh_seconds):
    if not record or refresh_seconds <= 0:
        return False
    try:
        updated = datetime.fromisoformat(record.get("updatedAt", "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return (datetime.now(timezone.utc) - updated).total_seconds() < refresh_seconds


def request_json(url, *, params=None, method="GET", data=None, timeout=20, retries=2, headers=None, user_agent=None):
    if params:
        separator = "&" if "?" in url else "?"
        url = url + separator + urllib.parse.urlencode(params, doseq=True)
    body = data.encode("utf-8") if isinstance(data, str) else data
    request_headers = {
        "Accept": "application/json",
        "User-Agent": user_agent or "INSIGHT Surrey data refresh",
    }
    request_headers.update(headers or {})
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {"status": 404, "items": [], "result": None}
            if exc.code in (408, 429, 500, 502, 503, 504) and attempt < retries:
                wait = parse_float(exc.headers.get("Retry-After")) or min(120, 4 * (attempt + 1) ** 2)
                print(f"API returned HTTP {exc.code}; waiting {wait:.0f}s before retry {attempt + 1}/{retries}.", flush=True)
                time.sleep(wait)
                continue
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {detail[:260]}") from exc
        except urllib.error.URLError as exc:
            if attempt < retries:
                time.sleep(2 + attempt * 2)
                continue
            raise RuntimeError(str(exc)) from exc
    return {}


def coordinates_from_item(item):
    lon = parse_float(item.get("longitude") or item.get("lon"))
    lat = parse_float(item.get("latitude") or item.get("lat"))
    for container_key in ("geocode", "location"):
        container = item.get(container_key)
        if isinstance(container, dict):
            lon = lon if lon is not None else parse_float(container.get("longitude") or container.get("lon"))
            lat = lat if lat is not None else parse_float(container.get("latitude") or container.get("lat"))
    if lat is None or lon is None:
        return None, None
    return lat, lon


def postcode_lookup(postcode, cache, *, refresh_days=365, timeout=15, retries=1):
    key = normalise_postcode(postcode)
    if not key:
        return None
    store = cache.setdefault("postcodes", {})
    cached = store.get(key)
    if cache_fresh(cached, refresh_days * 24 * 60 * 60):
        return cached.get("data")
    payload = request_json(
        POSTCODES_API + urllib.parse.quote(clean(postcode)),
        timeout=timeout,
        retries=retries,
        user_agent="INSIGHT postcode geocoding",
    )
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        store[key] = {"status": "no_match", "updatedAt": utc_now()}
        return None
    lon = parse_float(result.get("longitude"))
    lat = parse_float(result.get("latitude"))
    if lon is None or lat is None:
        store[key] = {"status": "no_match", "updatedAt": utc_now()}
        return None
    data = {
        "longitude": round(lon, 7),
        "latitude": round(lat, 7),
        "coordinateSource": "Postcodes.io",
        "coordinatePrecision": "postcode-centroid",
        "geocode": {
            "source": "Postcodes.io",
            "precision": "Postcode centroid",
            "postcodeDistrict": clean(result.get("outcode")),
            "adminDistrict": clean(result.get("admin_district")),
            "region": clean(result.get("region")),
            "country": clean(result.get("country")),
        },
    }
    store[key] = {"status": "matched", "updatedAt": utc_now(), "data": data}
    return data


def ensure_coordinates(item, cache, args):
    lat, lon = coordinates_from_item(item)
    if lat is not None and lon is not None:
        return lat, lon, {}
    data = postcode_lookup(
        item.get("postcode"),
        cache,
        refresh_days=getattr(args, "geocode_refresh_days", 365),
        timeout=getattr(args, "timeout", 20),
        retries=getattr(args, "retries", 1),
    )
    if not data:
        return None, None, {}
    return data["latitude"], data["longitude"], data


def haversine_metres(lat1, lon1, lat2, lon2):
    radius = 6_371_000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def format_distance(metres):
    if metres is None:
        return ""
    if metres < 950:
        return f"{round(metres / 10) * 10:.0f}m"
    return f"{metres / 1000:.1f}km"


def approx_walk_time(metres):
    if metres is None:
        return ""
    minutes = max(1, round((metres * 1.25) / 80))
    return f"c. {minutes} min"


def wkt_square(lat, lon, radius_m):
    lat_delta = radius_m / 111_320
    lon_delta = radius_m / (111_320 * max(0.2, math.cos(math.radians(lat))))
    west = lon - lon_delta
    east = lon + lon_delta
    south = lat - lat_delta
    north = lat + lat_delta
    return f"POLYGON(({west:.7f} {south:.7f},{east:.7f} {south:.7f},{east:.7f} {north:.7f},{west:.7f} {north:.7f},{west:.7f} {south:.7f}))"
