#!/usr/bin/env python3
"""Publish fail-closed Historic England listed-building evidence for INSIGHT.

The statutory source is Historic England's National Heritage List for England
(NHLE). Matching is performed once per canonical full-address Property Record.
The reviewed address ledger is authoritative for current properties. Postcode
centroids are never allowed to create a user-visible listing candidate; official
NHLE geometry refines the map only after the property identity is confirmed.
"""

import argparse
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from insight_data_utils import (
    DEFAULT_INPUT_JS,
    canonical_address,
    clean,
    coordinates_from_item,
    haversine_metres,
    property_record_id,
    read_js,
    request_json,
    utc_now,
    write_js as write_canonical_js,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OVERRIDES = ROOT / "config" / "heritage-listing-overrides.json"
FEATURE_SERVICE = (
    "https://services-eu1.arcgis.com/ZOdPfBS3aqqDYPUQ/ArcGIS/rest/services/"
    "National_Heritage_List_for_England_NHLE_v02_VIEW/FeatureServer"
)
SOURCE_ITEM_ID = "767f279327a24845bf47dfe5eae9862b"
POINT_LAYER_ID = 0
POLYGON_LAYER_ID = 3
SOURCE_NAME = "Historic England NHLE"
CONFIRMED_LOCATION_PRECISION = "confirmed-nhle-designation-location"
CONFIRMED_LOCATION_MODE = "single-reviewed-entry-single-nhle-point"
SOURCE_URL = "https://historicengland.org.uk/listing/the-list/data-downloads"
ATTRIBUTION_TEMPLATE = (
    "© Historic England {year}. Contains Ordnance Survey data "
    "© Crown copyright and database right {year}."
)
OVERRIDE_SCHEMA_VERSION = 1
HERITAGE_SCHEMA_VERSION = 1
COVERAGE_MODE = "property-grain-full-address-reviewed-fail-closed"
HERITAGE_STATUSES = frozenset({
    "confirmed_listed",
    "candidate_review",
    "no_direct_match",
    "unknown",
})
NHLE_GRADES = frozenset({"I", "II*", "II"})
ENTRY_MATCH_METHODS = frozenset({
    "reviewed_override",
    "genuine_polygon_contains",
    "nearby_nhle_point",
})
ENTRY_CONFIDENCE = frozenset({"confirmed", "review_required"})
HERITAGE_METADATA_FIELDS = frozenset({
    "schemaVersion",
    "status",
    "source",
    "sourceUrl",
    "sourceItemId",
    "pointLayerId",
    "polygonLayerId",
    "sourceDataLastEditDate",
    "fetchedAt",
    "sourceSnapshot",
    "sourceRecordsFetched",
    "polygonRecordsFetched",
    "propertiesAccountedFor",
    "confirmedListed",
    "candidateReview",
    "noDirectMatch",
    "unknown",
    "overridesApplied",
    "confirmedEntries",
    "confirmedUniqueListEntries",
    "confirmedEntryGradeCounts",
    "confirmedLocationsApplied",
    "confirmedLocationMode",
    "inputFingerprint",
    "sourceFingerprint",
    "overrideFingerprint",
    "outputFingerprint",
    "coverageMode",
    "attribution",
    "geography",
})
# Covers every current canonical property coordinate with a c. 3-5 km buffer,
# without sweeping central London into the Surrey snapshot.
DEFAULT_BBOX = (-0.88, 51.03, 0.08, 51.48)
DEFAULT_CANDIDATE_RADIUS_METRES = 250
DEFAULT_NAME_RADIUS_METRES = 2000
DEFAULT_MAX_FEED_BYTES = 25 * 1024 * 1024
DEFAULT_MIN_SOURCE_RECORDS = 13000
DEFAULT_MAX_SOURCE_RECORDS = 17000
DEFAULT_MIN_POLYGON_RECORDS = 500
DEFAULT_MAX_POLYGON_RECORDS = 800
MAX_ENVELOPE_ONLY_POINT_FEATURES = 2000
MAX_ENVELOPE_ONLY_POINT_FRACTION = 0.10
MAX_ORPHAN_POLYGON_FEATURES = 100
MAX_ORPHAN_POLYGON_FRACTION = 0.10
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
LIST_ENTRY_RE = re.compile(r"^\d{7}$")
SOURCE_SNAPSHOT_RE = re.compile(r"^nhle-\d{4}-\d{2}-\d{2}-[0-9a-f]{12}$")
ISO_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def stable_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(value):
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def timestamp_from_arcgis(value):
    """Return an ISO timestamp/date from an ArcGIS date field."""

    if isinstance(value, (int, float)) and math.isfinite(value):
        # ArcGIS esriFieldTypeDate values are always Unix epoch milliseconds.
        # The previous magnitude heuristic misread dates in early 1970 because
        # their millisecond values are still below ten billion.
        seconds = value / 1000
        return (
            datetime.fromtimestamp(seconds, tz=timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    text = clean(value)
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def date_from_arcgis(value):
    timestamp = timestamp_from_arcgis(value)
    # Listing/amendment dates are optional.  Exclude obviously corrupt
    # out-of-era source values instead of publishing a fabricated legal date.
    match = re.match(r"^(?:19|20)\d{2}-\d{2}-\d{2}", timestamp)
    return match.group(0) if match else ""


def parse_iso_date(value):
    text = clean(value)
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_iso_timestamp(value):
    text = clean(value)
    if not ISO_TIMESTAMP_RE.fullmatch(text):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc)


def normalise_grade(value):
    grade = clean(value).upper().replace("GRADE", "").replace(" ", "")
    aliases = {"1": "I", "2": "II", "2*": "II*"}
    grade = aliases.get(grade, grade)
    if grade not in NHLE_GRADES:
        raise ValueError(f"Unsupported NHLE listed-building grade: {value!r}")
    return grade


def attribution_for_date(value):
    match = re.match(r"^(\d{4})", clean(value))
    if not match:
        raise ValueError("NHLE source date cannot provide the attribution year")
    return ATTRIBUTION_TEMPLATE.format(year=match.group(1))


def normalised_field_name(value):
    return re.sub(r"[^a-z0-9]", "", clean(value).lower())


def layer_field_map(metadata, *, polygon=False):
    """Resolve reviewed NHLE field names from live ArcGIS layer metadata."""

    fields = metadata.get("fields") if isinstance(metadata, dict) else None
    if not isinstance(fields, list):
        raise RuntimeError("NHLE layer metadata did not provide a field schema")
    available = {
        normalised_field_name(field.get("name")): clean(field.get("name"))
        for field in fields
        if isinstance(field, dict) and clean(field.get("name"))
    }
    aliases = {
        "objectId": (
            clean(metadata.get("objectIdField")),
            "OBJECTID",
        ),
        "listEntryNumber": ("ListEntry", "ListEntryNumber"),
        "name": ("Name",),
        "grade": ("Grade",),
        "listDate": ("ListDate",),
        "amendDate": ("AmendDate",),
        "captureScale": ("CaptureScale",),
        "hyperlink": ("hyperlink", "Hyperlink"),
        "areaHectares": ("area_ha", "AreaHectares"),
    }
    resolved = {}
    for logical, candidates in aliases.items():
        for candidate in candidates:
            actual = available.get(normalised_field_name(candidate))
            if actual:
                resolved[logical] = actual
                break
    required = {"objectId", "listEntryNumber", "name", "grade"}
    if polygon:
        required.add("areaHectares")
    missing = sorted(required - set(resolved))
    if missing:
        raise RuntimeError(
            "NHLE layer schema is missing reviewed fields: " + ", ".join(missing)
        )
    return resolved


def validate_layer_pin(metadata, *, layer_id, name, geometry_type):
    if not isinstance(metadata, dict):
        raise RuntimeError(f"NHLE layer {layer_id} metadata is missing")
    if metadata.get("id") != layer_id:
        raise RuntimeError(
            f"NHLE layer pin expected ID {layer_id}, found {metadata.get('id')!r}"
        )
    if clean(metadata.get("name")) != name:
        raise RuntimeError(
            f"NHLE layer {layer_id} name changed from {name!r} to "
            f"{clean(metadata.get('name'))!r}"
        )
    if metadata.get("geometryType") != geometry_type:
        raise RuntimeError(
            f"NHLE layer {layer_id} geometry changed from {geometry_type} to "
            f"{metadata.get('geometryType')!r}"
        )
    if clean(metadata.get("type")) != "Feature Layer":
        raise RuntimeError(f"NHLE layer {layer_id} is no longer a Feature Layer")
    if clean(metadata.get("serviceItemId")) != SOURCE_ITEM_ID:
        raise RuntimeError(
            f"NHLE layer {layer_id} is not owned by pinned item {SOURCE_ITEM_ID}"
        )


def response_error(payload, context):
    if not isinstance(payload, dict):
        raise RuntimeError(f"{context} did not return a JSON object")
    error = payload.get("error")
    if isinstance(error, dict):
        raise RuntimeError(
            f"{context} failed: {clean(error.get('message')) or stable_json(error)[:240]}"
        )


def fetch_layer_features(
    layer_url,
    metadata,
    field_map,
    bbox,
    args,
    *,
    where="1=1",
):
    """Fetch one ArcGIS layer with deterministic, transfer-limit-aware paging."""

    west, south, east, north = bbox
    page_size = min(2000, max(1, args.page_size))
    output_fields = sorted(set(field_map.values()))
    features = []
    seen_object_ids = set()
    offset = 0
    page = 0
    while True:
        page += 1
        if page > args.max_pages:
            raise RuntimeError(f"NHLE pagination exceeded {args.max_pages} pages")
        payload = request_json(
            layer_url + "/query",
            params={
                "where": where,
                "geometry": stable_json({
                    "xmin": west,
                    "ymin": south,
                    "xmax": east,
                    "ymax": north,
                    "spatialReference": {"wkid": 4326},
                }),
                "geometryType": "esriGeometryEnvelope",
                "inSR": 4326,
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": ",".join(output_fields),
                "returnGeometry": "true",
                "outSR": 4326,
                "orderByFields": f"{field_map['objectId']} ASC",
                "resultOffset": offset,
                "resultRecordCount": page_size,
                "f": "json",
            },
            timeout=args.timeout,
            retries=args.retries,
            user_agent="INSIGHT Historic England NHLE sync",
        )
        response_error(payload, "NHLE feature query")
        page_features = payload.get("features")
        if not isinstance(page_features, list):
            raise RuntimeError("NHLE feature query did not prove a feature result")
        for feature in page_features:
            attributes = feature.get("attributes") if isinstance(feature, dict) else None
            object_id = attributes.get(field_map["objectId"]) if isinstance(attributes, dict) else None
            if object_id in seen_object_ids:
                raise RuntimeError(
                    "NHLE pagination repeated an object ID; refusing an incomplete snapshot"
                )
            seen_object_ids.add(object_id)
            features.append(feature)
        offset += len(page_features)
        exceeded = payload.get("exceededTransferLimit") is True
        if not page_features or (len(page_features) < page_size and not exceeded):
            break
        if len(page_features) == 0:
            break
    return features


def fetch_layer_count(layer_url, bbox, args, *, where="1=1"):
    """Return the authoritative feature count for the exact spatial query."""

    west, south, east, north = bbox
    payload = request_json(
        layer_url + "/query",
        params={
            "where": where,
            "geometry": stable_json({
                "xmin": west,
                "ymin": south,
                "xmax": east,
                "ymax": north,
                "spatialReference": {"wkid": 4326},
            }),
            "geometryType": "esriGeometryEnvelope",
            "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "returnCountOnly": "true",
            "f": "json",
        },
        timeout=args.timeout,
        retries=args.retries,
        user_agent="INSIGHT Historic England NHLE sync",
    )
    response_error(payload, "NHLE feature-count query")
    count = payload.get("count") if isinstance(payload, dict) else None
    if type(count) is not int or count < 0:
        raise RuntimeError("NHLE feature-count query did not return an integer count")
    return count


def source_last_edit_date(*metadata_objects):
    values = []
    for metadata in metadata_objects:
        if not isinstance(metadata, dict):
            continue
        editing = metadata.get("editingInfo")
        value = editing.get("lastEditDate") if isinstance(editing, dict) else None
        if value in (None, ""):
            value = metadata.get("lastEditDate")
        timestamp = timestamp_from_arcgis(value)
        if timestamp:
            values.append(timestamp)
    if not values:
        raise RuntimeError("NHLE metadata did not expose its source last-edit date")
    return max(values)


def listed_building_source_last_edit_date(point_metadata, polygon_metadata):
    """Return currency for the two pinned listed-building layers only."""

    return source_last_edit_date(point_metadata, polygon_metadata)


def attribute_value(attributes, field_map, logical):
    field = field_map.get(logical)
    return attributes.get(field) if field and isinstance(attributes, dict) else None


def normalise_point_feature(feature, field_map):
    attributes = feature.get("attributes") if isinstance(feature, dict) else None
    geometry = feature.get("geometry") if isinstance(feature, dict) else None
    if not isinstance(attributes, dict) or not isinstance(geometry, dict):
        raise ValueError("NHLE point feature is missing attributes or geometry")
    entry_number = clean(attribute_value(attributes, field_map, "listEntryNumber"))
    if not LIST_ENTRY_RE.fullmatch(entry_number):
        raise ValueError(f"Invalid NHLE List Entry Number: {entry_number!r}")
    name = clean(attribute_value(attributes, field_map, "name"))
    if not name:
        raise ValueError(f"NHLE {entry_number} has no designation name")
    raw_locations = geometry.get("points")
    if not isinstance(raw_locations, list):
        raw_locations = [[geometry.get("x"), geometry.get("y")]]
    locations = []
    for location in raw_locations:
        if (
            not isinstance(location, list)
            or len(location) < 2
            or not isinstance(location[0], (int, float))
            or not isinstance(location[1], (int, float))
        ):
            raise ValueError(f"NHLE {entry_number} has invalid WGS84 multipoint geometry")
        locations.append((
            round(float(location[0]), 7),
            round(float(location[1]), 7),
        ))
    if not locations:
        raise ValueError(f"NHLE {entry_number} has no valid WGS84 point")
    longitude, latitude = locations[0]
    result = {
        "listEntryNumber": entry_number,
        "grade": normalise_grade(attribute_value(attributes, field_map, "grade")),
        "name": name,
        "url": (
            "https://historicengland.org.uk/listing/the-list/list-entry/"
            + entry_number
        ),
        "longitude": longitude,
        "latitude": latitude,
        "locations": locations,
    }
    list_date = date_from_arcgis(attribute_value(attributes, field_map, "listDate"))
    amend_date = date_from_arcgis(attribute_value(attributes, field_map, "amendDate"))
    if list_date:
        result["listDate"] = list_date
    if amend_date:
        result["amendDate"] = amend_date
    capture_scale = attribute_value(attributes, field_map, "captureScale")
    if isinstance(capture_scale, (int, float)):
        result["captureScale"] = capture_scale
    return result


def retain_point_locations_in_bbox(point, bbox):
    """Keep only multipoint members that are inside the reviewed query area."""

    west, south, east, north = bbox
    retained = [
        (longitude, latitude)
        for longitude, latitude in point.get("locations") or []
        if (
            west <= longitude <= east
            and south <= latitude <= north
        )
    ]
    if not retained:
        raise ValueError(
            f"NHLE {point.get('listEntryNumber', '<unknown>')} was returned by "
            "the Surrey spatial query but has no point inside its reviewed envelope"
        )
    output = dict(point)
    output["locations"] = retained
    output["longitude"], output["latitude"] = retained[0]
    return output


def filter_point_snapshot_to_bbox(
    points,
    bbox,
    *,
    max_dropped=MAX_ENVELOPE_ONLY_POINT_FEATURES,
    max_dropped_fraction=MAX_ENVELOPE_ONLY_POINT_FRACTION,
):
    """Clip ArcGIS multipoints exactly and gate its envelope-only cohort."""

    retained = []
    dropped = []
    for point in points:
        try:
            retained.append(retain_point_locations_in_bbox(point, bbox))
        except ValueError:
            dropped.append(clean(point.get("listEntryNumber")))
    dropped_fraction = len(dropped) / len(points) if points else 0
    if (
        len(dropped) > max_dropped
        or dropped_fraction > max_dropped_fraction
    ):
        raise ValueError(
            "NHLE point spatial query returned an implausible envelope-only "
            f"cohort: {len(dropped):,} of {len(points):,} features "
            f"({dropped_fraction:.2%}); first List Entry "
            f"{dropped[0] if dropped else '<none>'}"
        )
    return retained, dropped


def normalise_polygon_feature(feature, field_map):
    attributes = feature.get("attributes") if isinstance(feature, dict) else None
    geometry = feature.get("geometry") if isinstance(feature, dict) else None
    if not isinstance(attributes, dict) or not isinstance(geometry, dict):
        raise ValueError("NHLE polygon feature is missing attributes or geometry")
    entry_number = clean(attribute_value(attributes, field_map, "listEntryNumber"))
    if not LIST_ENTRY_RE.fullmatch(entry_number):
        raise ValueError(f"Invalid NHLE polygon List Entry Number: {entry_number!r}")
    grade = normalise_grade(attribute_value(attributes, field_map, "grade"))
    rings = geometry.get("rings")
    if not isinstance(rings, list) or not rings:
        raise ValueError(f"NHLE polygon {entry_number} has no rings")
    clean_rings = []
    coordinates = []
    for ring in rings:
        if not isinstance(ring, list) or len(ring) < 4:
            raise ValueError(f"NHLE polygon {entry_number} has a malformed ring")
        clean_ring = []
        for coordinate in ring:
            if (
                not isinstance(coordinate, list)
                or len(coordinate) < 2
                or not isinstance(coordinate[0], (int, float))
                or not isinstance(coordinate[1], (int, float))
            ):
                raise ValueError(f"NHLE polygon {entry_number} has invalid WGS84 geometry")
            pair = (float(coordinate[0]), float(coordinate[1]))
            clean_ring.append(pair)
            coordinates.append(pair)
        clean_rings.append(clean_ring)
    area = attribute_value(attributes, field_map, "areaHectares")
    if not isinstance(area, (int, float)) or area <= 0:
        raise ValueError(
            f"NHLE polygon {entry_number} is not a genuine positive-area outline"
        )
    return {
        "listEntryNumber": entry_number,
        "grade": grade,
        "rings": clean_rings,
        "bbox": (
            min(point[0] for point in coordinates),
            min(point[1] for point in coordinates),
            max(point[0] for point in coordinates),
            max(point[1] for point in coordinates),
        ),
        "areaHectares": float(area),
    }


def filter_polygon_snapshot_to_retained_points(
    polygons,
    points,
    *,
    max_dropped=MAX_ORPHAN_POLYGON_FEATURES,
    max_dropped_fraction=MAX_ORPHAN_POLYGON_FRACTION,
):
    """Drop boundary polygons that lack publishable in-envelope point evidence."""

    point_numbers = {
        point["listEntryNumber"]
        for point in points
    }
    retained = [
        polygon
        for polygon in polygons
        if polygon["listEntryNumber"] in point_numbers
    ]
    dropped = [
        polygon["listEntryNumber"]
        for polygon in polygons
        if polygon["listEntryNumber"] not in point_numbers
    ]
    dropped_fraction = len(dropped) / len(polygons) if polygons else 0
    if (
        len(dropped) > max_dropped
        or dropped_fraction > max_dropped_fraction
    ):
        raise ValueError(
            "NHLE polygon spatial query returned an implausible orphan cohort: "
            f"{len(dropped):,} of {len(polygons):,} genuine polygons "
            f"({dropped_fraction:.2%}); first List Entry "
            f"{dropped[0] if dropped else '<none>'}"
        )
    return retained, dropped


def validate_source_snapshot(
    points,
    polygons,
    bbox,
    *,
    minimum,
    maximum,
    minimum_polygons=0,
    maximum_polygons=5000,
):
    if len(points) < minimum:
        raise ValueError(
            f"NHLE Surrey snapshot has only {len(points):,} points; expected at least {minimum:,}"
        )
    if len(points) > maximum:
        raise ValueError(
            f"NHLE Surrey snapshot has {len(points):,} points; exceeds geography gate {maximum:,}"
        )
    if len(polygons) < minimum_polygons:
        raise ValueError(
            f"NHLE Surrey snapshot has only {len(polygons):,} genuine polygons; "
            f"expected at least {minimum_polygons:,}"
        )
    if len(polygons) > maximum_polygons:
        raise ValueError(
            f"NHLE Surrey snapshot has {len(polygons):,} genuine polygons; "
            f"exceeds geography gate {maximum_polygons:,}"
        )
    numbers = [point["listEntryNumber"] for point in points]
    if len(numbers) != len(set(numbers)):
        raise ValueError("NHLE point snapshot contains duplicate List Entry Numbers")
    west, south, east, north = bbox
    outside = []
    for point in points:
        for longitude, latitude in point["locations"]:
            if not (
                west <= longitude <= east
                and south <= latitude <= north
            ):
                outside.append(point["listEntryNumber"])
                break
    if outside:
        raise ValueError(
            "NHLE point query escaped the reviewed Surrey envelope "
            f"(first List Entry {outside[0]})"
        )
    point_numbers = set(numbers)
    orphan_polygons = [
        polygon["listEntryNumber"]
        for polygon in polygons
        if polygon["listEntryNumber"] not in point_numbers
    ]
    if orphan_polygons:
        raise ValueError(
            "NHLE polygon snapshot contains an entry absent from the point source "
            f"(first List Entry {orphan_polygons[0]})"
        )


def acquire_source(args):
    """Fetch and validate the official point and genuine-outline layers."""

    service_metadata = request_json(
        FEATURE_SERVICE,
        params={"f": "json"},
        timeout=args.timeout,
        retries=args.retries,
        user_agent="INSIGHT Historic England NHLE sync",
    )
    response_error(service_metadata, "NHLE service metadata")
    service_item = clean(
        service_metadata.get("serviceItemId") or service_metadata.get("itemId")
    )
    if service_item != SOURCE_ITEM_ID:
        raise RuntimeError(
            f"NHLE service item changed from {SOURCE_ITEM_ID} to "
            f"{service_item or '<missing>'}"
        )

    point_url = f"{FEATURE_SERVICE}/{POINT_LAYER_ID}"
    polygon_url = f"{FEATURE_SERVICE}/{POLYGON_LAYER_ID}"
    point_metadata = request_json(
        point_url,
        params={"f": "json"},
        timeout=args.timeout,
        retries=args.retries,
        user_agent="INSIGHT Historic England NHLE sync",
    )
    polygon_metadata = request_json(
        polygon_url,
        params={"f": "json"},
        timeout=args.timeout,
        retries=args.retries,
        user_agent="INSIGHT Historic England NHLE sync",
    )
    response_error(point_metadata, "NHLE point-layer metadata")
    response_error(polygon_metadata, "NHLE polygon-layer metadata")
    validate_layer_pin(
        point_metadata,
        layer_id=POINT_LAYER_ID,
        name="Listed Building points",
        geometry_type="esriGeometryMultipoint",
    )
    validate_layer_pin(
        polygon_metadata,
        layer_id=POLYGON_LAYER_ID,
        name="Listed Building polygons",
        geometry_type="esriGeometryPolygon",
    )
    point_fields = layer_field_map(point_metadata)
    polygon_fields = layer_field_map(polygon_metadata, polygon=True)

    polygon_where = f"{polygon_fields['areaHectares']} IS NOT NULL"
    expected_point_count = fetch_layer_count(
        point_url,
        args.bbox,
        args,
    )
    expected_polygon_count = fetch_layer_count(
        polygon_url,
        args.bbox,
        args,
        where=polygon_where,
    )
    point_features = fetch_layer_features(
        point_url,
        point_metadata,
        point_fields,
        args.bbox,
        args,
    )
    polygon_features = fetch_layer_features(
        polygon_url,
        polygon_metadata,
        polygon_fields,
        args.bbox,
        args,
        where=polygon_where,
    )
    if len(point_features) != expected_point_count:
        raise RuntimeError(
            f"NHLE point pagination returned {len(point_features):,} of "
            f"{expected_point_count:,} declared features"
        )
    if len(polygon_features) != expected_polygon_count:
        raise RuntimeError(
            f"NHLE polygon pagination returned {len(polygon_features):,} of "
            f"{expected_polygon_count:,} declared features"
        )
    raw_points = [
        normalise_point_feature(feature, point_fields)
        for feature in point_features
    ]
    points, dropped_envelope_only_points = filter_point_snapshot_to_bbox(
        raw_points,
        args.bbox,
    )
    if dropped_envelope_only_points:
        print(
            "Discarded "
            f"{len(dropped_envelope_only_points):,} NHLE multipoint "
            "feature(s) whose geometry envelope intersected the reviewed "
            "bbox but had no member point inside it.",
            flush=True,
        )
    raw_polygons = [
        normalise_polygon_feature(feature, polygon_fields)
        for feature in polygon_features
    ]
    points.sort(key=lambda item: item["listEntryNumber"])
    polygons, dropped_orphan_polygons = (
        filter_polygon_snapshot_to_retained_points(
            raw_polygons,
            points,
        )
    )
    if dropped_orphan_polygons:
        print(
            "Discarded "
            f"{len(dropped_orphan_polygons):,} genuine NHLE polygon(s) "
            "whose designation point was outside the reviewed bbox.",
            flush=True,
        )
    polygons.sort(
        key=lambda item: (
            item["listEntryNumber"],
            item["bbox"],
        )
    )
    validate_source_snapshot(
        points,
        polygons,
        args.bbox,
        minimum=args.minimum_source_records,
        maximum=args.maximum_source_records,
        minimum_polygons=args.minimum_polygon_records,
        maximum_polygons=args.maximum_polygon_records,
    )
    # The FeatureServer root covers unrelated NHLE designation layers. Only
    # the two pinned listed-building layers can advance this publication's
    # source currency.
    source_updated_at = listed_building_source_last_edit_date(
        point_metadata,
        polygon_metadata,
    )
    source_fingerprint = fingerprint({
        "points": points,
        "polygons": polygons,
    })
    return {
        "points": points,
        "polygons": polygons,
        "sourceUpdatedAt": source_updated_at,
        "sourceFingerprint": source_fingerprint,
    }


def load_overrides(path, property_ids=None):
    path = Path(path)
    if not path.exists():
        raise ValueError(f"Reviewed heritage override ledger is missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Reviewed heritage override ledger is invalid JSON: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schemaVersion") != OVERRIDE_SCHEMA_VERSION:
        raise ValueError(
            f"Heritage override ledger must use schemaVersion {OVERRIDE_SCHEMA_VERSION}"
        )
    allowed_top_level = {
        "$schema",
        "schemaVersion",
        "updatedAt",
        "productionRequired",
        "mappings",
    }
    unexpected_top_level = sorted(set(payload) - allowed_top_level)
    if unexpected_top_level:
        raise ValueError(
            "Heritage override ledger has unreviewed top-level fields: "
            + ", ".join(unexpected_top_level)
        )
    if parse_iso_date(payload.get("updatedAt")) is None:
        raise ValueError("Heritage override ledger updatedAt must be ISO YYYY-MM-DD")
    if payload.get("productionRequired") is not True:
        raise ValueError(
            "Heritage override ledger must explicitly set productionRequired to true"
        )
    mappings = payload.get("mappings")
    if not isinstance(mappings, list):
        raise ValueError("Heritage override ledger mappings must be an array")
    output = {}
    for index, mapping in enumerate(mappings, start=1):
        if not isinstance(mapping, dict):
            raise ValueError(f"Heritage override {index} must be an object")
        allowed_mapping_fields = {
            "propertyRecordId",
            "address",
            "postcode",
            "status",
            "listEntryNumbers",
            "reviewedBy",
            "reviewedAt",
            "evidenceUrl",
            "note",
        }
        unexpected_mapping = sorted(set(mapping) - allowed_mapping_fields)
        if unexpected_mapping:
            raise ValueError(
                f"Heritage override {index} has unreviewed fields: "
                + ", ".join(unexpected_mapping)
            )
        record_id = clean(mapping.get("propertyRecordId"))
        if not record_id:
            raise ValueError(f"Heritage override {index} has no propertyRecordId")
        if record_id in output:
            raise ValueError(f"Duplicate heritage override for {record_id}")
        if mapping.get("address") not in (None, ""):
            derived = property_record_id({
                "address": mapping.get("address"),
                "postcode": mapping.get("postcode"),
            })
            if derived != record_id:
                raise ValueError(
                    f"Heritage override {record_id} does not match its full address identity"
                )
        status = clean(mapping.get("status"))
        if status not in HERITAGE_STATUSES:
            raise ValueError(
                f"Heritage override {record_id} has unsupported status {status!r}"
            )
        entries = mapping.get("listEntryNumbers")
        if not isinstance(entries, list):
            raise ValueError(
                f"Heritage override {record_id} listEntryNumbers must be an array"
            )
        entries = [clean(value) for value in entries]
        if len(entries) != len(set(entries)) or any(
            not LIST_ENTRY_RE.fullmatch(value) for value in entries
        ):
            raise ValueError(
                f"Heritage override {record_id} has invalid or duplicate List Entry Numbers"
            )
        if status == "candidate_review":
            raise ValueError(
                f"Heritage override {record_id} cannot publish a candidate; "
                "candidate_review is generated only by nearby NHLE evidence"
            )
        if status == "confirmed_listed" and not entries:
            raise ValueError(
                f"Heritage override {record_id} status {status} requires List Entry Numbers"
            )
        if status in {"no_direct_match", "unknown"} and entries:
            raise ValueError(
                f"Heritage override {record_id} status {status} cannot carry List Entry Numbers"
            )
        reviewed_at = clean(mapping.get("reviewedAt"))
        if not clean(mapping.get("reviewedBy")) or not reviewed_at:
            raise ValueError(
                f"Heritage override {record_id} must record reviewedBy and reviewedAt"
            )
        if parse_iso_date(reviewed_at) is None:
            raise ValueError(
                f"Heritage override {record_id} reviewedAt must be ISO YYYY-MM-DD"
            )
        evidence_url = clean(mapping.get("evidenceUrl"))
        if status == "confirmed_listed" and not evidence_url:
            raise ValueError(
                f"Heritage override {record_id} confirmed evidenceUrl is required"
            )
        if evidence_url:
            expected_urls = {
                "https://historicengland.org.uk/listing/the-list/list-entry/" + number
                for number in entries
            }
            if evidence_url not in expected_urls:
                raise ValueError(
                    f"Heritage override {record_id} evidenceUrl is not an official "
                    "URL for its List Entry Numbers"
                )
        if property_ids is not None and record_id not in property_ids:
            raise ValueError(
                f"Heritage override {record_id} is outside the canonical property universe"
            )
        output[record_id] = {
            **mapping,
            "status": status,
            "listEntryNumbers": entries,
        }
    return output, fingerprint(payload)


def heritage_publication_required(path=DEFAULT_OVERRIDES):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            "Heritage production policy cannot be read from the reviewed ledger"
        ) from exc
    return payload.get("productionRequired") is True


def property_coordinate_is_trusted(item):
    """Only property/building-level coordinates may prove polygon containment."""

    if (
        clean(item.get("coordinateSource")) == SOURCE_NAME
        or clean(item.get("coordinatePrecision")) == CONFIRMED_LOCATION_PRECISION
    ):
        # An NHLE designation marker is authoritative designation evidence, not
        # a property footprint capable of self-confirming polygon containment.
        return False
    precision_values = [
        item.get("coordinatePrecision"),
        (item.get("geocode") or {}).get("precision")
        if isinstance(item.get("geocode"), dict)
        else None,
        (item.get("ordnanceSurvey") or {}).get("coordinatePrecision")
        if isinstance(item.get("ordnanceSurvey"), dict)
        else None,
    ]
    precision = " ".join(clean(value).lower() for value in precision_values if clean(value))
    if "postcode" in precision or "centroid" in precision:
        return False
    trusted_terms = ("address-point", "address point", "building", "property", "rooftop")
    if any(term in precision for term in trusted_terms):
        return True
    os_context = item.get("ordnanceSurvey")
    return (
        isinstance(os_context, dict)
        and clean(os_context.get("matchStatus")).lower() == "confirmed"
        and bool(clean(os_context.get("uprn")))
    )


def finite_coordinate_pair(latitude, longitude):
    return (
        type(latitude) in (int, float)
        and math.isfinite(latitude)
        and type(longitude) in (int, float)
        and math.isfinite(longitude)
    )


def valid_wgs84_coordinate_pair(latitude, longitude):
    return (
        finite_coordinate_pair(latitude, longitude)
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    )


def is_nhle_refined_coordinate(item):
    return (
        clean(item.get("coordinateSource")) == SOURCE_NAME
        or clean(item.get("coordinatePrecision")) == CONFIRMED_LOCATION_PRECISION
    )


def base_coordinate_view(item):
    """Return the pre-NHLE coordinate evidence used for all matching.

    A published NHLE point is a useful designation marker, but it must never
    feed back into later candidate or polygon matching. Refined rows therefore
    reconstruct their original postcode-centroid view from the preserved
    geocode fields.
    """

    row = dict(item)
    if not is_nhle_refined_coordinate(row):
        return row
    geocode = row.get("geocode")
    geocode = geocode if isinstance(geocode, dict) else {}
    latitude = geocode.get("postcodeCentroidLatitude")
    longitude = geocode.get("postcodeCentroidLongitude")
    if finite_coordinate_pair(latitude, longitude):
        row["latitude"] = latitude
        row["longitude"] = longitude
    else:
        # Fail closed rather than screen from a prior designation marker.
        row["latitude"] = None
        row["longitude"] = None
    source = clean(geocode.get("source"))
    precision = clean(geocode.get("precision"))
    if source:
        row["coordinateSource"] = source
    else:
        row.pop("coordinateSource", None)
    if precision:
        row["coordinatePrecision"] = precision
    else:
        row.pop("coordinatePrecision", None)
    return row


def property_coordinate_is_explicitly_approximate(property_data):
    """Allow display refinement only for complete, stable approximate inputs."""

    base_rows = property_data.get("baseRows") or []
    original_rows = property_data.get("rows") or []
    if len(base_rows) != len(original_rows):
        return False
    coordinate_evidence = []
    for original_row, row in zip(original_rows, base_rows):
        latitude, longitude = coordinates_from_item(row)
        if not valid_wgs84_coordinate_pair(latitude, longitude):
            return False
        if property_coordinate_is_trusted(row):
            return False
        geocode = row.get("geocode")
        geocode = geocode if isinstance(geocode, dict) else {}
        if (
            clean(geocode.get("source")) != "Postcodes.io"
            or clean(geocode.get("precision")).lower() != "postcode centroid"
        ):
            return False
        original_geocode = original_row.get("geocode")
        original_geocode = (
            original_geocode if isinstance(original_geocode, dict) else {}
        )
        saved_latitude_present = "postcodeCentroidLatitude" in original_geocode
        saved_longitude_present = "postcodeCentroidLongitude" in original_geocode
        if saved_latitude_present != saved_longitude_present:
            return False
        if saved_latitude_present and not valid_wgs84_coordinate_pair(
            original_geocode.get("postcodeCentroidLatitude"),
            original_geocode.get("postcodeCentroidLongitude"),
        ):
            return False
        coordinate_evidence.append((
            latitude,
            longitude,
            clean(geocode.get("source")),
            clean(geocode.get("precision")),
        ))
    return bool(coordinate_evidence) and len(set(coordinate_evidence)) == 1


def build_properties(transactions):
    grouped = defaultdict(list)
    for item in transactions:
        if not isinstance(item, dict):
            raise ValueError("Transaction is not an object")
        record_id = property_record_id(item)
        if not record_id:
            raise ValueError(
                f"Transaction {clean(item.get('id')) or '<unknown>'} has no full-address identity"
            )
        if clean(item.get("propertyRecordId")) not in ("", record_id):
            raise ValueError(
                f"Transaction {clean(item.get('id')) or '<unknown>'} has stale propertyRecordId"
            )
        grouped[record_id].append(item)
    properties = {}
    for record_id, rows in grouped.items():
        ordered = sorted(
            rows,
            key=lambda row: (clean(row.get("date")), clean(row.get("id"))),
            reverse=True,
        )
        base_rows = [base_coordinate_view(row) for row in ordered]
        representative = base_rows[0]
        coordinate_rows = []
        for row in base_rows:
            lat, lon = coordinates_from_item(row)
            if lat is not None and lon is not None:
                coordinate_rows.append((row, lat, lon))
        trusted = [value for value in coordinate_rows if property_coordinate_is_trusted(value[0])]
        selected = trusted[0] if trusted else (coordinate_rows[0] if coordinate_rows else None)
        conflict = False
        if selected and trusted:
            conflict = any(
                haversine_metres(selected[1], selected[2], value[1], value[2]) > 25
                for value in trusted[1:]
            )
        properties[record_id] = {
            "recordId": record_id,
            "rows": ordered,
            "baseRows": base_rows,
            "item": representative,
            "latitude": selected[1] if selected else None,
            "longitude": selected[2] if selected else None,
            "coordinateTrusted": bool(selected and selected in trusted and not conflict),
            "coordinateConflict": conflict,
        }
    return properties


def point_in_ring(longitude, latitude, ring):
    inside = False
    previous = ring[-1]
    for current in ring:
        x1, y1 = previous
        x2, y2 = current
        intersects = (y1 > latitude) != (y2 > latitude)
        if intersects:
            crossing = (x2 - x1) * (latitude - y1) / ((y2 - y1) or 1e-15) + x1
            if longitude < crossing:
                inside = not inside
        previous = current
    return inside


def point_in_polygon(longitude, latitude, polygon):
    west, south, east, north = polygon["bbox"]
    if not (west <= longitude <= east and south <= latitude <= north):
        return False
    # ArcGIS rings contain exteriors and holes; even/odd parity handles both.
    return sum(
        1 for ring in polygon["rings"] if point_in_ring(longitude, latitude, ring)
    ) % 2 == 1


def grid_key(latitude, longitude, size=0.02):
    return (math.floor(latitude / size), math.floor(longitude / size))


def point_index(points, size=0.02):
    index = defaultdict(list)
    for point in points:
        for longitude, latitude in point["locations"]:
            location = {
                **point,
                "longitude": longitude,
                "latitude": latitude,
            }
            index[grid_key(latitude, longitude, size)].append(location)
    return index


def nearby_index_points(index, latitude, longitude, radius_metres, size=0.02):
    lat_span = radius_metres / 111_320
    lon_span = radius_metres / (
        111_320 * max(0.2, math.cos(math.radians(latitude)))
    )
    min_lat, min_lon = grid_key(latitude - lat_span, longitude - lon_span, size)
    max_lat, max_lon = grid_key(latitude + lat_span, longitude + lon_span, size)
    for lat_cell in range(min_lat, max_lat + 1):
        for lon_cell in range(min_lon, max_lon + 1):
            yield from index.get((lat_cell, lon_cell), ())


GENERIC_NAME_TOKENS = frozenset({
    "HOUSE",
    "THE",
    "OLD",
    "NEW",
    "FARM",
    "HALL",
    "LODGE",
    "COTTAGE",
    "COURT",
    "MANOR",
})


def significant_name_tokens(value):
    return {
        token
        for token in canonical_address(value).split()
        if len(token) >= 3 and token not in GENERIC_NAME_TOKENS and not token.isdigit()
    }


def property_name_matches(item, designation_name):
    property_name = clean(item.get("paon"))
    if not property_name or not re.search(r"[A-Za-z]", property_name):
        return False
    left = significant_name_tokens(property_name)
    right = significant_name_tokens(designation_name)
    if not left or not right:
        return False
    overlap = len(left & right)
    return overlap >= 1 and overlap / len(left) >= 0.67


def public_entry(point, *, method, confidence, distance_metres=None):
    entry = {
        key: point[key]
        for key in (
            "listEntryNumber",
            "grade",
            "name",
            "url",
            "listDate",
            "amendDate",
        )
        if point.get(key) not in (None, "")
    }
    entry["matchMethod"] = method
    entry["matchConfidence"] = confidence
    if distance_metres is not None:
        entry["distanceMetres"] = round(distance_metres)
    return entry


def sort_entries(entries):
    grade_order = {"I": 0, "II*": 1, "II": 2}
    return sorted(
        entries,
        key=lambda entry: (
            grade_order.get(entry.get("grade"), 9),
            entry.get("distanceMetres", 10**9),
            entry.get("listEntryNumber", ""),
        ),
    )


def projection_base(checked_at, source_updated_at, source_snapshot):
    return {
        "source": SOURCE_NAME,
        "checkedAt": checked_at,
        "sourceUpdatedAt": source_updated_at,
        "sourceSnapshot": source_snapshot,
    }


def override_projection(override, points_by_number, base, property_data):
    entries = []
    for entry_number in override["listEntryNumbers"]:
        point = points_by_number.get(entry_number)
        if not point:
            raise ValueError(
                f"Reviewed override references NHLE {entry_number}, absent from current source"
            )
        confirmed = override["status"] == "confirmed_listed"
        entries.append(public_entry(
            point,
            method="reviewed_override",
            confidence="confirmed" if confirmed else "review_required",
        ))
    projection = {
        "status": override["status"],
        "entries": sort_entries(entries),
        **base,
    }
    if (
        override["status"] == "confirmed_listed"
        and len(entries) == 1
        and len(points_by_number[entries[0]["listEntryNumber"]]["locations"]) == 1
        and property_coordinate_is_explicitly_approximate(property_data)
    ):
        longitude, latitude = points_by_number[
            entries[0]["listEntryNumber"]
        ]["locations"][0]
        projection["_coordinateUpdate"] = {
            "latitude": latitude,
            "longitude": longitude,
            "coordinateSource": SOURCE_NAME,
            "coordinatePrecision": CONFIRMED_LOCATION_PRECISION,
        }
    return projection


def automatic_projection(
    property_data,
    points_by_number,
    point_grid,
    polygons,
    base,
    *,
    candidate_radius_metres,
    name_radius_metres,
):
    latitude = property_data["latitude"]
    longitude = property_data["longitude"]
    if (
        latitude is None
        or longitude is None
        or property_data.get("coordinateConflict")
    ):
        return {"status": "unknown", "entries": [], **base}

    if property_data["coordinateTrusted"]:
        polygon_numbers = {
            polygon["listEntryNumber"]
            for polygon in polygons
            if point_in_polygon(longitude, latitude, polygon)
        }
        if polygon_numbers:
            entries = [
                public_entry(
                    points_by_number[number],
                    method="genuine_polygon_contains",
                    confidence="confirmed",
                )
                for number in polygon_numbers
            ]
            return {
                "status": "confirmed_listed",
                "entries": sort_entries(entries),
                **base,
            }

    # Distance from a postcode centroid cannot establish property identity.
    # Properties not yet represented in the reviewed address ledger remain
    # explicitly unknown until an address/document pass is published.
    return {"status": "unknown", "entries": [], **base}


def input_fingerprint(properties):
    values = []
    for record_id, data in sorted(properties.items()):
        item = data["item"]
        geocode = item.get("geocode")
        geocode = geocode if isinstance(geocode, dict) else {}
        values.append({
            "propertyRecordId": record_id,
            "address": clean(item.get("address")),
            "paon": clean(item.get("paon")),
            "saon": clean(item.get("saon")),
            "street": clean(item.get("street")),
            "locality": clean(item.get("locality")),
            "town": clean(item.get("town")),
            "postcode": clean(item.get("postcode")),
            "latitude": data.get("latitude"),
            "longitude": data.get("longitude"),
            "coordinateSource": (
                clean(geocode.get("source"))
                or clean(item.get("coordinateSource"))
            ),
            "coordinatePrecision": (
                clean(geocode.get("precision"))
                or clean(item.get("coordinatePrecision"))
            ),
            "coordinateTrusted": data.get("coordinateTrusted"),
            "coordinateConflict": data.get("coordinateConflict"),
        })
    return fingerprint(values)


def build_projections(transactions, source, overrides, args, checked_at):
    properties = build_properties(transactions)
    points = source["points"]
    points_by_number = {
        point["listEntryNumber"]: point
        for point in points
    }
    grid = point_index(points)
    source_snapshot = (
        "nhle-"
        + source["sourceUpdatedAt"][:10]
        + "-"
        + source["sourceFingerprint"][:12]
    )
    base = projection_base(
        checked_at,
        source["sourceUpdatedAt"],
        source_snapshot,
    )
    projections = {}
    for record_id, property_data in properties.items():
        override = overrides.get(record_id)
        if override:
            projection = override_projection(
                override,
                points_by_number,
                base,
                property_data,
            )
        else:
            projection = automatic_projection(
                property_data,
                points_by_number,
                grid,
                source["polygons"],
                base,
                candidate_radius_metres=args.candidate_radius_metres,
                name_radius_metres=args.name_radius_metres,
            )
        projections[record_id] = projection
    return properties, projections, source_snapshot


def public_property_publications(transactions):
    """Return deterministic public heritage and coordinate state per property."""

    grouped = defaultdict(list)
    for item in transactions:
        if not isinstance(item, dict):
            continue
        record_id = property_record_id(item)
        if record_id:
            grouped[record_id].append(item)
    publications = {}
    for record_id, rows in grouped.items():
        representative = sorted(
            rows,
            key=lambda row: (clean(row.get("date")), clean(row.get("id"))),
            reverse=True,
        )[0]
        geocode = representative.get("geocode")
        geocode = geocode if isinstance(geocode, dict) else {}
        context = representative.get("historicEngland")
        context = context if isinstance(context, dict) else {}
        publications[record_id] = {
            # Provenance timestamps and snapshots are reconciled separately.
            # Keeping them out of the semantic output fingerprint prevents
            # timestamp-only source refreshes from rewriting every row.
            "historicEngland": {
                "status": context.get("status"),
                "entries": context.get("entries"),
            },
            "latitude": representative.get("latitude"),
            "longitude": representative.get("longitude"),
            "coordinateSource": clean(representative.get("coordinateSource")),
            "coordinatePrecision": clean(
                representative.get("coordinatePrecision")
            ),
            "postcodeCentroidLatitude": geocode.get(
                "postcodeCentroidLatitude"
            ),
            "postcodeCentroidLongitude": geocode.get(
                "postcodeCentroidLongitude"
            ),
        }
    return publications


def publication_fingerprint(transactions):
    return fingerprint(public_property_publications(transactions))


def apply_projections(transactions, projections):
    output = []
    for item in transactions:
        record_id = property_record_id(item)
        row = dict(item)
        projection = projections[record_id]
        row["historicEngland"] = {
            key: value
            for key, value in projection.items()
            if not key.startswith("_")
        }
        coordinate_update = projection.get("_coordinateUpdate")
        if isinstance(coordinate_update, dict):
            original_latitude, original_longitude = coordinates_from_item(row)
            original_source = clean(row.get("coordinateSource"))
            original_precision = clean(row.get("coordinatePrecision"))
            geocode = row.get("geocode")
            geocode = dict(geocode) if isinstance(geocode, dict) else {}
            if (
                not is_nhle_refined_coordinate(row)
                and "postcodeCentroidLatitude" not in geocode
                and "postcodeCentroidLongitude" not in geocode
                and finite_coordinate_pair(original_latitude, original_longitude)
            ):
                geocode["postcodeCentroidLatitude"] = original_latitude
                geocode["postcodeCentroidLongitude"] = original_longitude
                if "source" not in geocode and original_source:
                    geocode["source"] = original_source
                if "precision" not in geocode and original_precision:
                    geocode["precision"] = original_precision
            row["geocode"] = geocode
            row.update(coordinate_update)
        elif (
            is_nhle_refined_coordinate(row)
            and isinstance(row.get("geocode"), dict)
            and finite_coordinate_pair(
                row["geocode"].get("postcodeCentroidLatitude"),
                row["geocode"].get("postcodeCentroidLongitude"),
            )
        ):
            row["latitude"] = row["geocode"]["postcodeCentroidLatitude"]
            row["longitude"] = row["geocode"]["postcodeCentroidLongitude"]
            geocode_source = clean(row["geocode"].get("source"))
            geocode_precision = clean(row["geocode"].get("precision"))
            if geocode_source:
                row["coordinateSource"] = geocode_source
            else:
                row.pop("coordinateSource", None)
            if geocode_precision:
                row["coordinatePrecision"] = geocode_precision
            else:
                row.pop("coordinatePrecision", None)
        output.append(row)
    return output


def confirmed_entry_counts(projections):
    """Count confirmed property-entry relationships and unique designations."""

    relationships = 0
    relationship_grades = Counter()
    grades_by_entry = {}
    for projection in projections.values():
        if (
            not isinstance(projection, dict)
            or projection.get("status") != "confirmed_listed"
        ):
            continue
        entries = projection.get("entries")
        entries = entries if isinstance(entries, list) else []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            relationships += 1
            number = clean(entry.get("listEntryNumber"))
            grade = clean(entry.get("grade"))
            relationship_grades[grade] += 1
            existing = grades_by_entry.get(number)
            if existing and existing != grade:
                raise ValueError(
                    f"NHLE {number} has conflicting confirmed grades {existing} and {grade}"
                )
            if number:
                grades_by_entry[number] = grade
    return {
        "confirmedEntries": relationships,
        "confirmedUniqueListEntries": len(grades_by_entry),
        "confirmedEntryGradeCounts": {
            grade: relationship_grades[grade]
            for grade in ("I", "II*", "II")
        },
    }


def heritage_contract_failures(
    transactions,
    metadata,
    *,
    require_complete=False,
    now=None,
):
    """Return contract failures without mutating the candidate/public feed."""

    failures = []
    if not isinstance(transactions, list):
        return ["Heritage sync: transaction payload is not an array"]
    if not isinstance(metadata, dict):
        return ["Heritage sync: metadata root is not an object"]
    has_rows = any(
        isinstance(item, dict) and "historicEngland" in item
        for item in transactions
    )
    heritage_meta = metadata.get("heritageSync")
    heritage_meta = heritage_meta if isinstance(heritage_meta, dict) else None
    if not has_rows and not heritage_meta:
        return ["Heritage sync: no property states have been published"] if require_complete else []
    if heritage_meta is None:
        return ["Heritage sync: metadata is missing"]
    metadata_fields = set(heritage_meta)
    if metadata_fields != HERITAGE_METADATA_FIELDS:
        missing = sorted(HERITAGE_METADATA_FIELDS - metadata_fields)
        unexpected = sorted(metadata_fields - HERITAGE_METADATA_FIELDS)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if unexpected:
            detail.append("unreviewed " + ", ".join(unexpected))
        failures.append(
            "Heritage metadata: field set is invalid (" + "; ".join(detail) + ")"
        )

    by_property = {}
    projection_by_property = {}
    coordinate_states_by_property = defaultdict(set)
    refined_location_properties = set()
    state_counts = Counter()
    allowed_outer = {
        "status",
        "entries",
        "source",
        "checkedAt",
        "sourceUpdatedAt",
        "sourceSnapshot",
    }
    allowed_entry = {
        "listEntryNumber",
        "grade",
        "name",
        "url",
        "listDate",
        "amendDate",
        "matchMethod",
        "matchConfidence",
        "distanceMetres",
    }
    metadata_source_date = clean(heritage_meta.get("sourceDataLastEditDate"))
    metadata_snapshot = clean(heritage_meta.get("sourceSnapshot"))
    metadata_fetched_at = clean(heritage_meta.get("fetchedAt"))
    for index, item in enumerate(transactions, start=1):
        if not isinstance(item, dict):
            failures.append(f"Heritage row {index}: transaction is not an object")
            continue
        record_id = property_record_id(item)
        context = item.get("historicEngland")
        if not isinstance(context, dict):
            failures.append(f"Heritage row {index}: historicEngland state is missing")
            continue
        unexpected = sorted(set(context) - allowed_outer)
        if unexpected:
            failures.append(
                f"Heritage row {index}: unreviewed fields {', '.join(unexpected)}"
            )
        status = clean(context.get("status"))
        entries = context.get("entries")
        source_is_nhle = clean(item.get("coordinateSource")) == SOURCE_NAME
        precision_is_nhle = (
            clean(item.get("coordinatePrecision"))
            == CONFIRMED_LOCATION_PRECISION
        )
        is_refined_location = source_is_nhle and precision_is_nhle
        if source_is_nhle != precision_is_nhle:
            failures.append(
                f"Heritage row {index}: NHLE coordinate source and precision "
                "must be published together"
            )
        geocode = item.get("geocode")
        geocode = geocode if isinstance(geocode, dict) else {}
        coordinate_state = {
            "latitude": item.get("latitude"),
            "longitude": item.get("longitude"),
            "coordinateSource": clean(item.get("coordinateSource")),
            "coordinatePrecision": clean(item.get("coordinatePrecision")),
            "postcodeCentroidLatitude": geocode.get(
                "postcodeCentroidLatitude"
            ),
            "postcodeCentroidLongitude": geocode.get(
                "postcodeCentroidLongitude"
            ),
        }
        if record_id:
            coordinate_states_by_property[record_id].add(
                stable_json(coordinate_state)
            )
        if is_refined_location:
            if record_id:
                refined_location_properties.add(record_id)
            if not valid_wgs84_coordinate_pair(
                item.get("latitude"),
                item.get("longitude"),
            ):
                failures.append(
                    f"Heritage row {index}: refined NHLE coordinate is not valid WGS84"
                )
            if not valid_wgs84_coordinate_pair(
                geocode.get("postcodeCentroidLatitude"),
                geocode.get("postcodeCentroidLongitude"),
            ):
                failures.append(
                    f"Heritage row {index}: refined NHLE coordinate has no "
                    "valid WGS84 preserved postcode centroid"
                )
            if (
                clean(geocode.get("source")) != "Postcodes.io"
                or clean(geocode.get("precision")).lower()
                != "postcode centroid"
            ):
                failures.append(
                    f"Heritage row {index}: refined NHLE coordinate must preserve "
                    "Postcodes.io postcode-centroid provenance"
                )
        if status not in HERITAGE_STATUSES:
            failures.append(f"Heritage row {index}: invalid status {status!r}")
            if is_refined_location:
                failures.append(
                    f"Heritage row {index}: invalid state cannot carry an "
                    "NHLE coordinate refinement"
                )
            continue
        if not isinstance(entries, list):
            failures.append(f"Heritage row {index}: entries must be an array")
            continue
        if status in {"confirmed_listed", "candidate_review"} and not entries:
            failures.append(f"Heritage row {index}: {status} requires entries")
        if status in {"no_direct_match", "unknown"} and entries:
            failures.append(f"Heritage row {index}: {status} cannot carry entries")
        if context.get("source") != SOURCE_NAME:
            failures.append(f"Heritage row {index}: source is not {SOURCE_NAME}")
        checked_at = clean(context.get("checkedAt"))
        source_updated_at = clean(context.get("sourceUpdatedAt"))
        source_snapshot = clean(context.get("sourceSnapshot"))
        if parse_iso_timestamp(checked_at) is None:
            failures.append(f"Heritage row {index}: checkedAt is not an ISO timestamp")
        if parse_iso_timestamp(source_updated_at) is None:
            failures.append(
                f"Heritage row {index}: sourceUpdatedAt is not an ISO timestamp"
            )
        if not SOURCE_SNAPSHOT_RE.fullmatch(source_snapshot):
            failures.append(f"Heritage row {index}: sourceSnapshot is invalid")
        if source_updated_at != metadata_source_date:
            failures.append(
                f"Heritage row {index}: sourceUpdatedAt disagrees with metadata"
            )
        if source_snapshot != metadata_snapshot:
            failures.append(
                f"Heritage row {index}: sourceSnapshot disagrees with metadata"
            )
        if checked_at != metadata_fetched_at:
            failures.append(
                f"Heritage row {index}: checkedAt disagrees with metadata"
            )

        entry_numbers = []
        for entry in entries:
            if not isinstance(entry, dict):
                failures.append(f"Heritage row {index}: an entry is not an object")
                continue
            unexpected_entry = sorted(set(entry) - allowed_entry)
            if unexpected_entry:
                failures.append(
                    f"Heritage row {index}: unreviewed entry fields "
                    + ", ".join(unexpected_entry)
                )
            number = clean(entry.get("listEntryNumber"))
            entry_numbers.append(number)
            if not LIST_ENTRY_RE.fullmatch(number):
                failures.append(f"Heritage row {index}: invalid List Entry Number")
            if entry.get("grade") not in NHLE_GRADES:
                failures.append(f"Heritage row {index}: invalid listed-building grade")
            if not clean(entry.get("name")):
                failures.append(f"Heritage row {index}: designation name is missing")
            if entry.get("url") != (
                "https://historicengland.org.uk/listing/the-list/list-entry/" + number
            ):
                failures.append(f"Heritage row {index}: official entry URL is invalid")
            if entry.get("matchMethod") not in ENTRY_MATCH_METHODS:
                failures.append(f"Heritage row {index}: invalid match method")
            if entry.get("matchConfidence") not in ENTRY_CONFIDENCE:
                failures.append(f"Heritage row {index}: invalid match confidence")
            for date_field in ("listDate", "amendDate"):
                if (
                    date_field in entry
                    and parse_iso_date(entry.get(date_field)) is None
                ):
                    failures.append(
                        f"Heritage row {index}: {date_field} is not ISO YYYY-MM-DD"
                    )
            if "distanceMetres" in entry and (
                type(entry.get("distanceMetres")) is not int
                or entry["distanceMetres"] < 0
            ):
                failures.append(
                    f"Heritage row {index}: distanceMetres must be a non-negative integer"
                )
            if status == "confirmed_listed" and entry.get("matchConfidence") != "confirmed":
                failures.append(f"Heritage row {index}: confirmed state has unconfirmed evidence")
            if (
                status == "confirmed_listed"
                and entry.get("matchMethod")
                not in {"reviewed_override", "genuine_polygon_contains"}
            ):
                failures.append(
                    f"Heritage row {index}: confirmed state uses an unproved match method"
                )
            if status == "candidate_review" and entry.get("matchConfidence") != "review_required":
                failures.append(f"Heritage row {index}: candidate state claims confirmation")
            if (
                status == "candidate_review"
                and entry.get("matchMethod") != "nearby_nhle_point"
            ):
                failures.append(
                    f"Heritage row {index}: candidate state is not nearby NHLE evidence"
                )
        if len(entry_numbers) != len(set(entry_numbers)):
            failures.append(f"Heritage row {index}: duplicate List Entry Numbers")
        eligible_refined_entries = [
            entry
            for entry in entries
            if isinstance(entry, dict)
            and entry.get("matchMethod") == "reviewed_override"
            and entry.get("matchConfidence") == "confirmed"
        ]
        if is_refined_location and not (
            status == "confirmed_listed"
            and len(entries) == 1
            and len(eligible_refined_entries) == 1
        ):
            failures.append(
                f"Heritage row {index}: NHLE coordinate refinement requires "
                "one confirmed reviewed entry"
            )
        serialised = stable_json(context)
        if record_id in by_property:
            if by_property[record_id] != serialised:
                failures.append(
                    f"Heritage property {record_id}: transaction rows disagree"
                )
        elif record_id:
            by_property[record_id] = serialised
            projection_by_property[record_id] = context
            state_counts[status] += 1

    for record_id in sorted(refined_location_properties):
        if len(coordinate_states_by_property.get(record_id, ())) != 1:
            failures.append(
                f"Heritage property {record_id}: refined coordinates disagree "
                "between transaction rows"
            )

    source_date = clean(heritage_meta.get("sourceDataLastEditDate"))
    try:
        expected_attribution = attribution_for_date(source_date)
    except ValueError:
        expected_attribution = ""
    try:
        confirmed_counts = confirmed_entry_counts(projection_by_property)
    except ValueError as exc:
        failures.append(str(exc))
        confirmed_counts = {
            "confirmedEntries": 0,
            "confirmedUniqueListEntries": 0,
            "confirmedEntryGradeCounts": {},
        }
    overrides_applied = sum(
        1
        for projection in projection_by_property.values()
        if any(
            isinstance(entry, dict)
            and entry.get("matchMethod") == "reviewed_override"
            for entry in projection.get("entries") or []
        )
    )
    expected_meta = {
        "schemaVersion": HERITAGE_SCHEMA_VERSION,
        "status": "complete",
        "source": SOURCE_NAME,
        "sourceUrl": SOURCE_URL,
        "sourceItemId": SOURCE_ITEM_ID,
        "pointLayerId": POINT_LAYER_ID,
        "polygonLayerId": POLYGON_LAYER_ID,
        "propertiesAccountedFor": len(by_property),
        "confirmedListed": state_counts["confirmed_listed"],
        "candidateReview": state_counts["candidate_review"],
        "noDirectMatch": state_counts["no_direct_match"],
        "unknown": state_counts["unknown"],
        "overridesApplied": overrides_applied,
        "confirmedLocationsApplied": len(refined_location_properties),
        "confirmedLocationMode": CONFIRMED_LOCATION_MODE,
        **confirmed_counts,
        "coverageMode": COVERAGE_MODE,
    }
    if expected_attribution:
        expected_meta["attribution"] = expected_attribution
    elif not clean(heritage_meta.get("attribution")):
        failures.append("Heritage metadata: attribution is missing")
    for field, expected in expected_meta.items():
        if heritage_meta.get(field) != expected:
            failures.append(
                f"Heritage metadata: {field} reports "
                f"{heritage_meta.get(field, 'missing')!r}, expected {expected!r}"
            )

    for field in ("sourceDataLastEditDate", "fetchedAt"):
        if parse_iso_timestamp(heritage_meta.get(field)) is None:
            failures.append(f"Heritage metadata: {field} is not an ISO timestamp")
    if not SOURCE_SNAPSHOT_RE.fullmatch(clean(heritage_meta.get("sourceSnapshot"))):
        failures.append("Heritage metadata: sourceSnapshot is invalid")
    for field in (
        "inputFingerprint",
        "sourceFingerprint",
        "overrideFingerprint",
        "outputFingerprint",
    ):
        if not FINGERPRINT_RE.fullmatch(clean(heritage_meta.get(field))):
            failures.append(f"Heritage metadata: {field} is not a SHA-256 fingerprint")
    source_records_fetched = heritage_meta.get("sourceRecordsFetched")
    if (
        type(source_records_fetched) is not int
        or not (
            DEFAULT_MIN_SOURCE_RECORDS
            <= source_records_fetched
            <= DEFAULT_MAX_SOURCE_RECORDS
        )
    ):
        failures.append(
            "Heritage metadata: sourceRecordsFetched is outside the reviewed "
            f"{DEFAULT_MIN_SOURCE_RECORDS:,}-{DEFAULT_MAX_SOURCE_RECORDS:,} cohort"
        )
    polygon_records_fetched = heritage_meta.get("polygonRecordsFetched")
    if (
        type(polygon_records_fetched) is not int
        or not (
            DEFAULT_MIN_POLYGON_RECORDS
            <= polygon_records_fetched
            <= DEFAULT_MAX_POLYGON_RECORDS
        )
    ):
        failures.append(
            "Heritage metadata: polygonRecordsFetched is outside the reviewed "
            f"{DEFAULT_MIN_POLYGON_RECORDS:,}-{DEFAULT_MAX_POLYGON_RECORDS:,} cohort"
        )
    geography = heritage_meta.get("geography")
    expected_geography = {
        "name": "Surrey search envelope",
        "bbox": list(DEFAULT_BBOX),
        "coordinateSystem": "EPSG:4326",
    }
    if geography != expected_geography:
        failures.append(
            "Heritage metadata: reviewed Surrey geography is not the approved "
            "search envelope"
        )
    valid_transactions = [
        item for item in transactions if isinstance(item, dict)
    ]
    canonical_ids = {
        property_record_id(item)
        for item in valid_transactions
        if property_record_id(item)
    }
    if len(by_property) != len(canonical_ids):
        failures.append("Heritage sync: not every canonical property is accounted for")
    try:
        current_input_fingerprint = input_fingerprint(
            build_properties(valid_transactions)
        )
    except ValueError as exc:
        failures.append(f"Heritage input fingerprint: {exc}")
    else:
        if heritage_meta.get("inputFingerprint") != current_input_fingerprint:
            failures.append(
                "Heritage metadata: inputFingerprint does not match the current property universe"
            )
    if projection_by_property:
        current_output_fingerprint = publication_fingerprint(valid_transactions)
        if heritage_meta.get("outputFingerprint") != current_output_fingerprint:
            failures.append(
                "Heritage metadata: outputFingerprint does not match published property states"
            )
    return failures


def metadata_for_run(
    metadata,
    properties,
    projections,
    source,
    source_snapshot,
    override_fingerprint,
    checked_at,
    bbox,
    published_transactions=None,
):
    states = Counter(projection["status"] for projection in projections.values())
    confirmed_counts = confirmed_entry_counts(projections)
    if published_transactions is None:
        source_transactions = [
            row
            for property_data in properties.values()
            for row in property_data["rows"]
        ]
        published_transactions = apply_projections(
            source_transactions,
            projections,
        )
    public_properties = public_property_publications(published_transactions)
    confirmed_locations_applied = sum(
        1
        for publication in public_properties.values()
        if publication["coordinateSource"] == SOURCE_NAME
        and publication["coordinatePrecision"] == CONFIRMED_LOCATION_PRECISION
    )
    output = dict(metadata)
    weekly = output.get("weeklyContext")
    if isinstance(weekly, dict) and "historicEngland" in weekly:
        weekly = dict(weekly)
        weekly.pop("historicEngland", None)
        output["weeklyContext"] = weekly
    output["heritageSync"] = {
        "schemaVersion": HERITAGE_SCHEMA_VERSION,
        "status": "complete",
        "source": SOURCE_NAME,
        "sourceUrl": SOURCE_URL,
        "sourceItemId": SOURCE_ITEM_ID,
        "pointLayerId": POINT_LAYER_ID,
        "polygonLayerId": POLYGON_LAYER_ID,
        "sourceDataLastEditDate": source["sourceUpdatedAt"],
        "fetchedAt": checked_at,
        "sourceSnapshot": source_snapshot,
        "sourceRecordsFetched": len(source["points"]),
        "polygonRecordsFetched": len(source["polygons"]),
        "propertiesAccountedFor": len(projections),
        "confirmedListed": states["confirmed_listed"],
        "candidateReview": states["candidate_review"],
        "noDirectMatch": states["no_direct_match"],
        "unknown": states["unknown"],
        "overridesApplied": sum(
            1
            for projection in projections.values()
            if any(
                entry.get("matchMethod") == "reviewed_override"
                for entry in projection["entries"]
            )
        ),
        "confirmedLocationsApplied": confirmed_locations_applied,
        "confirmedLocationMode": CONFIRMED_LOCATION_MODE,
        **confirmed_counts,
        "inputFingerprint": input_fingerprint(properties),
        "sourceFingerprint": source["sourceFingerprint"],
        "overrideFingerprint": override_fingerprint,
        "outputFingerprint": publication_fingerprint(published_transactions),
        "coverageMode": COVERAGE_MODE,
        "attribution": attribution_for_date(source["sourceUpdatedAt"]),
        "geography": {
            "name": "Surrey search envelope",
            "bbox": list(bbox),
            "coordinateSystem": "EPSG:4326",
        },
    }
    return output


def existing_last_known_good(transactions, metadata):
    return not heritage_contract_failures(
        transactions,
        metadata,
        require_complete=True,
    )


def semantic_inputs_unchanged(metadata, run_metadata):
    old = metadata.get("heritageSync")
    new = run_metadata.get("heritageSync")
    if not isinstance(old, dict) or not isinstance(new, dict):
        return False
    return all(
        old.get(field) == new.get(field)
        for field in (
            "inputFingerprint",
            "sourceFingerprint",
            "overrideFingerprint",
            "outputFingerprint",
        )
    )


def atomic_write(path, transactions, metadata, max_feed_bytes):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.heritage-{os.getpid()}.tmp")
    try:
        write_canonical_js(temporary, transactions, metadata)
        size = temporary.stat().st_size
        if size > max_feed_bytes:
            raise ValueError(
                f"Heritage projection would grow the feed to {size:,} bytes; "
                f"limit is {max_feed_bytes:,}"
            )
        candidate_rows, _summary, candidate_meta = read_js(temporary)
        failures = heritage_contract_failures(
            candidate_rows,
            candidate_meta,
            require_complete=True,
        )
        if failures:
            raise ValueError(
                "Heritage candidate feed failed validation:\n- "
                + "\n- ".join(failures[:20])
            )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def parse_bbox(value):
    try:
        parts = tuple(float(part.strip()) for part in str(value).split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("bbox must contain four numbers") from exc
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must be west,south,east,north")
    west, south, east, north = parts
    if not (-8 <= west < east <= 2 and 49 <= south < north <= 61):
        raise argparse.ArgumentTypeError("bbox must be a valid WGS84 England envelope")
    return parts


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-js", default=str(DEFAULT_INPUT_JS))
    parser.add_argument("--write-js", default=str(DEFAULT_INPUT_JS))
    parser.add_argument("--overrides", default=str(DEFAULT_OVERRIDES))
    parser.add_argument(
        "--bbox",
        type=parse_bbox,
        default=DEFAULT_BBOX,
        help="Reviewed WGS84 search envelope west,south,east,north.",
    )
    parser.add_argument(
        "--candidate-radius-metres",
        type=int,
        default=DEFAULT_CANDIDATE_RADIUS_METRES,
    )
    parser.add_argument(
        "--name-radius-metres",
        type=int,
        default=DEFAULT_NAME_RADIUS_METRES,
    )
    parser.add_argument("--page-size", type=int, default=2000)
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument(
        "--minimum-source-records",
        type=int,
        default=DEFAULT_MIN_SOURCE_RECORDS,
    )
    parser.add_argument(
        "--maximum-source-records",
        type=int,
        default=DEFAULT_MAX_SOURCE_RECORDS,
    )
    parser.add_argument(
        "--minimum-polygon-records",
        type=int,
        default=DEFAULT_MIN_POLYGON_RECORDS,
    )
    parser.add_argument(
        "--maximum-polygon-records",
        type=int,
        default=DEFAULT_MAX_POLYGON_RECORDS,
    )
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--max-feed-bytes", type=int, default=DEFAULT_MAX_FEED_BYTES)
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate the current feed without contacting Historic England.",
    )
    parser.add_argument(
        "--force-write",
        action="store_true",
        help="Write even when source, property and override fingerprints are unchanged.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    transactions, _summary, metadata = read_js(args.input_js)
    if not transactions:
        raise ValueError("INSIGHT transaction feed is empty")
    if args.validate_only:
        failures = heritage_contract_failures(
            transactions,
            metadata,
            require_complete=True,
        )
        properties = build_properties(transactions)
        _overrides, current_override_fingerprint = load_overrides(
            args.overrides,
            property_ids=set(properties),
        )
        if (
            isinstance(metadata.get("heritageSync"), dict)
            and metadata["heritageSync"].get("overrideFingerprint")
            != current_override_fingerprint
        ):
            failures.append(
                "Heritage metadata: overrideFingerprint does not match the reviewed ledger"
            )
        if failures:
            raise ValueError("Heritage validation failed:\n- " + "\n- ".join(failures))
        print(
            "Heritage validation passed: "
            f"{metadata['heritageSync']['propertiesAccountedFor']:,} properties accounted for."
        )
        return 0

    properties = build_properties(transactions)
    overrides, override_fingerprint = load_overrides(
        args.overrides,
        property_ids=set(properties),
    )
    checked_at = utc_now()
    try:
        source = acquire_source(args)
    except Exception as exc:
        if (
            existing_last_known_good(transactions, metadata)
            and isinstance(metadata.get("heritageSync"), dict)
            and metadata["heritageSync"].get("overrideFingerprint")
            == override_fingerprint
        ):
            raise RuntimeError(
                "Historic England refresh failed; the validated last-known-good "
                "publication was retained unchanged and the workflow is failing "
                "to alert maintainers"
            ) from exc
        raise RuntimeError(
            "Historic England refresh failed and no complete last-known-good "
            "heritage publication exists"
        ) from exc

    properties, projections, source_snapshot = build_projections(
        transactions,
        source,
        overrides,
        args,
        checked_at,
    )
    enriched = apply_projections(transactions, projections)
    run_metadata = metadata_for_run(
        metadata,
        properties,
        projections,
        source,
        source_snapshot,
        override_fingerprint,
        checked_at,
        args.bbox,
        published_transactions=enriched,
    )
    if (
        not args.force_write
        and existing_last_known_good(transactions, metadata)
        and semantic_inputs_unchanged(metadata, run_metadata)
    ):
        print("Historic England source and property mappings are unchanged; no write needed.")
        return 0
    failures = heritage_contract_failures(
        enriched,
        run_metadata,
        require_complete=True,
    )
    if failures:
        raise ValueError(
            "Heritage candidate publication failed before write:\n- "
            + "\n- ".join(failures[:20])
        )
    atomic_write(
        args.write_js,
        enriched,
        run_metadata,
        args.max_feed_bytes,
    )
    counts = Counter(projection["status"] for projection in projections.values())
    print(
        "Historic England sync complete: "
        + ", ".join(
            f"{status}={counts[status]:,}" for status in sorted(HERITAGE_STATUSES)
        )
    )
    print(f"Updated {args.write_js}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
