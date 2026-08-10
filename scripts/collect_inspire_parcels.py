#!/usr/bin/env python3
"""Collect the public INSIGHT HMLR INSPIRE parcel feed.

The collector audits all configured Surrey source files, but publishes only
the explicit property associations in the reviewed registry. Source polygons
remain indicative index polygons: they are neither title plans nor legal
boundary or exact UPRN evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import math
import os
import re
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path

from insight_data_utils import read_js
from validate_property_uprn_links import parse_feed as parse_uprn_feed, validation_failures as uprn_validation_failures
from runtime_release import finalise_body, parse_runtime


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUTHORITIES = ROOT / "config" / "inspire-authorities.json"
DEFAULT_ASSOCIATIONS = ROOT / "config" / "inspire-parcel-associations.json"
DEFAULT_OUTPUT = ROOT / "outputs" / "inspire-parcels.js"
DEFAULT_TRANSACTIONS = ROOT / "outputs" / "surrey-transactions.js"
DEFAULT_PROPERTY_UPRN_LINKS = ROOT / "outputs" / "property-uprn-links.js"
DEFAULT_REVIEW_OUTPUT = ROOT / "outputs" / "inspire-parcel-review-queue.js"
DEFAULT_ASSOCIATION_TRANSITIONS = ROOT / "config" / "inspire-association-transitions.json"
DEFAULT_TRANSITION_DIAGNOSTIC = ROOT / "work" / "inspire-association-transition-required.json"
GLOBAL_NAME = "window.INSIGHT_INSPIRE_PARCELS"
REVIEW_GLOBAL_NAME = "window.INSIGHT_INSPIRE_PARCEL_REVIEW_QUEUE"
LR_NS = "www.landregistry.gov.uk"
GML_NS = "http://www.opengis.net/gml/3.2"
WFS_NS = "http://www.opengis.net/wfs/2.0"
SOURCE_CRS = "urn:ogc:def:crs:EPSG::27700"
METRES_TO_SQUARE_FEET = 10.763910416709722
METRES_TO_ACRES = 0.0002471053814671653
IDENTITY_MODE = "full-normalised-address-plus-postcode-fail-closed"
ASSOCIATION_SEMANTICS = (
    "indicative parcel association; not title, exact UPRN, ownership or "
    "legal-boundary confirmation"
)
OSTN15_GRID_SHA256 = "5d6ed64d2119952c4c559fa1fccbc594b6520fc3ec3ef2fc10be13202c4384fa"
OSTN15_GRID_FILENAME = "uk_os_OSTN15_NTv2_OSGBtoETRS.tif"


def clean(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path: Path, content: str) -> bool:
    return atomic_write_bytes(path, content.encode("utf-8"))


def atomic_write_bytes(path: Path, encoded: bytes) -> bool:
    if path.exists() and path.read_bytes() == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def pair_coordinates(text: str) -> list[tuple[float, float]]:
    values = text.split()
    if len(values) < 8 or len(values) % 2:
        raise ValueError("GML ring must contain at least four coordinate pairs")
    coordinates = []
    for index in range(0, len(values), 2):
        x, y = float(values[index]), float(values[index + 1])
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError("GML ring contains a non-finite coordinate")
        coordinates.append((x, y))
    if coordinates[0] != coordinates[-1]:
        raise ValueError("GML LinearRing is not closed")
    if len(set(coordinates[:-1])) < 3:
        raise ValueError("GML LinearRing has fewer than three distinct vertices")
    return coordinates


def translated_signed_area(ring: list[tuple[float, float]]) -> float:
    """Shoelace area translated to the first point to avoid cancellation."""

    x0, y0 = ring[0]
    terms = []
    for first, second in zip(ring, ring[1:]):
        ax, ay = first[0] - x0, first[1] - y0
        bx, by = second[0] - x0, second[1] - y0
        terms.append(ax * by - bx * ay)
    area = math.fsum(terms) / 2.0
    if abs(area) > 1e-7:
        return area
    # Preserve a true zero/collinearity decision for tiny source rings.
    dx0, dy0 = Decimal(str(x0)), Decimal(str(y0))
    precise = Decimal(0)
    for first, second in zip(ring, ring[1:]):
        ax = Decimal(str(first[0])) - dx0
        ay = Decimal(str(first[1])) - dy0
        bx = Decimal(str(second[0])) - dx0
        by = Decimal(str(second[1])) - dy0
        precise += ax * by - bx * ay
    return float(precise / Decimal(2))


def ring_centroid(ring: list[tuple[float, float]]) -> tuple[float, float]:
    x0, y0 = ring[0]
    twice_area = 0.0
    x_sum = 0.0
    y_sum = 0.0
    for first, second in zip(ring, ring[1:]):
        ax, ay = first[0] - x0, first[1] - y0
        bx, by = second[0] - x0, second[1] - y0
        cross = ax * by - bx * ay
        twice_area += cross
        x_sum += (ax + bx) * cross
        y_sum += (ay + by) * cross
    if twice_area == 0:
        raise ValueError("Cannot calculate centroid of a zero-area ring")
    return x0 + x_sum / (3 * twice_area), y0 + y_sum / (3 * twice_area)


def polygon_measurements(
    rings: list[list[tuple[float, float]]],
) -> tuple[float, tuple[float, float]]:
    weighted_x = weighted_y = weight_total = 0.0
    net_area = 0.0
    for index, ring in enumerate(rings):
        area = abs(translated_signed_area(ring))
        if area == 0:
            raise ValueError(f"zero-area ring {index}")
        weight = area if index == 0 else -area
        centroid_x, centroid_y = ring_centroid(ring)
        net_area += weight
        weighted_x += centroid_x * weight
        weighted_y += centroid_y * weight
        weight_total += weight
    if net_area <= 0 or weight_total <= 0:
        raise ValueError("Polygon holes consume its exterior area")
    return net_area, (weighted_x / weight_total, weighted_y / weight_total)


def point_to_segment_distance(point, first, second) -> float:
    px, py = point
    ax, ay = first
    bx, by = second
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    proportion = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + proportion * dx), py - (ay + proportion * dy))


def point_in_ring(point, ring) -> bool:
    x, y = point
    inside = False
    for first, second in zip(ring, ring[1:]):
        x1, y1 = first
        x2, y2 = second
        if point_to_segment_distance(point, first, second) <= 1e-9:
            return True
        if (y1 > y) != (y2 > y):
            intersection_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < intersection_x:
                inside = not inside
    return inside


def point_in_polygon(point, rings) -> bool:
    return point_in_ring(point, rings[0]) and not any(point_in_ring(point, ring) for ring in rings[1:])


def boundary_distance(point, rings) -> float:
    return min(
        point_to_segment_distance(point, first, second)
        for ring in rings
        for first, second in zip(ring, ring[1:])
    )


def configure_ostn15_transform(grid_path: Path):
    """Return the pinned official OSTN15 transform, or fail closed."""

    if not grid_path.exists():
        raise FileNotFoundError(f"Pinned OSTN15 grid is unavailable: {grid_path}")
    observed_hash = sha256_file(grid_path)
    if observed_hash != OSTN15_GRID_SHA256:
        raise ValueError(f"OSTN15 grid hash mismatch: {observed_hash}")
    try:
        from pyproj import Transformer, datadir
    except ImportError as error:
        raise RuntimeError("pyproj is required for the official OSTN15 display transform") from error
    datadir.append_data_dir(str(grid_path.parent))
    pipeline = (
        "+proj=pipeline "
        "+step +inv +proj=tmerc +lat_0=49 +lon_0=-2 +k=0.9996012717 "
        "+x_0=400000 +y_0=-100000 +ellps=airy "
        f"+step +proj=hgridshift +grids={grid_path.name} "
        "+step +proj=unitconvert +xy_in=rad +xy_out=deg"
    )
    transformer = Transformer.from_pipeline(pipeline)
    observed = transformer.transform(512196, 161083)
    expected = (-0.39071577362838394, 51.337774484695124)
    if max(abs(observed[index] - expected[index]) for index in (0, 1)) > 1e-10:
        raise RuntimeError(f"OSTN15 transform benchmark failed: {observed}")
    return transformer


class DownloadLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._anchor_text: list[str] = []
        self._in_row = False
        self._row_text: list[str] = []
        self._row_links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.casefold()
        if lowered == "tr":
            self._in_row = True
            self._row_text = []
            self._row_links = []
        elif lowered == "a":
            self._href = dict(attrs).get("href")
            self._anchor_text = []

    def handle_data(self, data: str) -> None:
        if self._in_row:
            self._row_text.append(data)
        if self._href is not None:
            self._anchor_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered == "a" and self._href is not None:
            link = (clean(" ".join(self._anchor_text)), self._href)
            if self._in_row:
                self._row_links.append(link)
            else:
                self.links.append(link)
            self._href = None
        elif lowered == "tr" and self._in_row:
            context = clean(" ".join(self._row_text))
            self.links.extend((context or anchor_text, href) for anchor_text, href in self._row_links)
            self._in_row = False


def download_current_sources(config: dict, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    request = urllib.request.Request(config["sourcePage"], headers={"User-Agent": "INSIGHT public data collector/1.0"})
    with opener.open(request, timeout=90) as response:
        page = response.read().decode("utf-8", "replace")
    parser = DownloadLinkParser()
    parser.feed(page)
    candidate_links = [(text, href) for text, href in parser.links if href and not href.startswith("#")]
    for authority in config["authorities"]:
        matches = authority_download_matches(candidate_links, authority)
        if len(matches) != 1:
            raise ValueError(f"Expected one current ZIP link for {authority['name']}, found {len(matches)}")
        url = urllib.parse.urljoin(config["sourcePage"], matches[0][1])
        target = destination / f"{authority['slug']}.zip"
        with opener.open(urllib.request.Request(url, headers={"User-Agent": "INSIGHT public data collector/1.0"}), timeout=180) as response:
            body = response.read()
        if not body.startswith(b"PK"):
            raise ValueError(f"Download for {authority['name']} is not a ZIP")
        atomic_write_bytes(target, body)


def authority_download_matches(links: list[tuple[str, str]], authority: dict) -> list[tuple[str, str]]:
    target_name = re.sub(r"[^a-z0-9]+", " ", authority["name"].casefold()).strip()
    target_slug = authority["slug"].casefold()
    matches = []
    for text, href in links:
        normalised_text = re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()
        name_match = bool(re.search(rf"(?:^| )({re.escape(target_name)})(?: |$)", normalised_text))
        href_name = Path(urllib.parse.urlparse(href).path).name.casefold()
        slug_match = href_name in {target_slug, target_slug + ".zip", target_slug + ".gml"}
        if name_match or slug_match:
            matches.append((text, href))
    return matches


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def text_child(element: ET.Element, name: str) -> str:
    child = element.find(f"{{{LR_NS}}}{name}")
    return clean(child.text if child is not None else "")


def parse_feature(element: ET.Element) -> tuple[str, list[list[tuple[float, float]]], str, str]:
    inspire_id = text_child(element, "INSPIREID")
    if not re.fullmatch(r"\d+", inspire_id):
        raise ValueError("Feature has a missing/non-numeric INSPIRE ID")
    polygon = element.find(f".//{{{GML_NS}}}Polygon")
    if polygon is None or polygon.get("srsName") != SOURCE_CRS:
        raise ValueError(f"Feature {inspire_id} has missing/unexpected source CRS")
    rings = []
    exterior = polygon.find(f"{{{GML_NS}}}exterior/{{{GML_NS}}}LinearRing/{{{GML_NS}}}posList")
    if exterior is None or not exterior.text:
        raise ValueError(f"Feature {inspire_id} has no exterior ring")
    rings.append(pair_coordinates(exterior.text))
    for interior in polygon.findall(f"{{{GML_NS}}}interior/{{{GML_NS}}}LinearRing/{{{GML_NS}}}posList"):
        if not interior.text:
            raise ValueError(f"Feature {inspire_id} has an empty interior ring")
        rings.append(pair_coordinates(interior.text))
    return inspire_id, rings, text_child(element, "VALIDFROM"), text_child(element, "BEGINLIFESPANVERSION")


def geometry_digest(rings: list[list[tuple[float, float]]]) -> str:
    canonical = json.dumps(rings, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def audit_source(
    zip_path: Path,
    authority: dict,
    selected_ids: set[str],
    known_quarantine_ids: set[str],
    candidate_points: dict[str, tuple[float, float]] | None = None,
) -> tuple[dict, dict[str, dict], dict[str, str], dict[str, dict[str, float]], dict[str, dict]]:
    source_occurrences = 0
    seen_digests: dict[str, str] = {}
    selected: dict[str, dict] = {}
    candidate_hits: dict[str, dict[str, float]] = {}
    candidate_features: dict[str, dict] = {}
    quarantined: set[str] = set()
    duplicate_occurrences = 0
    malformed = 0
    candidate_buckets: dict[tuple[int, int], list[tuple[str, tuple[float, float]]]] = {}
    for property_id, point in (candidate_points or {}).items():
        candidate_buckets.setdefault((int(point[0] // 10000), int(point[1] // 10000)), []).append((property_id, point))
    with zipfile.ZipFile(zip_path) as archive:
        names = [name for name in archive.namelist() if name.endswith("Land_Registry_Cadastral_Parcels.gml")]
        if len(names) != 1:
            raise ValueError(f"{zip_path.name} must contain exactly one cadastral GML")
        gml_info = archive.getinfo(names[0])
        with archive.open(names[0]) as source:
            iterator = ET.iterparse(source, events=("start", "end"))
            _event, root = next(iterator)
            number_matched = int(root.get("numberMatched", "-1"))
            generated_at = clean(root.get("timeStamp"))
            for event, element in iterator:
                if event != "end" or local_name(element.tag) != "PREDEFINED":
                    continue
                source_occurrences += 1
                try:
                    inspire_id, rings, valid_from, begin_lifespan = parse_feature(element)
                    digest = geometry_digest(rings)
                    existing = seen_digests.get(inspire_id)
                    if existing is not None:
                        duplicate_occurrences += 1
                        if existing != digest:
                            raise ValueError(f"INSPIRE ID {inspire_id} has conflicting duplicate geometry")
                    else:
                        seen_digests[inspire_id] = digest
                    zero_rings = [index for index, ring in enumerate(rings) if translated_signed_area(ring) == 0]
                    if zero_rings:
                        if inspire_id not in known_quarantine_ids:
                            raise ValueError(f"Unexpected zero-area ring(s) {zero_rings} in INSPIRE ID {inspire_id}")
                        quarantined.add(inspire_id)
                    if inspire_id in selected_ids:
                        if zero_rings:
                            raise ValueError(f"Selected INSPIRE ID {inspire_id} is structurally quarantined")
                        if inspire_id in selected:
                            selected[inspire_id]["authorities"].add(authority["slug"])
                        else:
                            selected[inspire_id] = {
                                "inspireId": inspire_id,
                                "rings": rings,
                                "validFrom": valid_from,
                                "beginLifespanVersion": begin_lifespan,
                                "authorities": {authority["slug"]},
                                "digest": digest,
                            }
                    if not zero_rings and candidate_buckets:
                        outer = rings[0]
                        min_x = min(point[0] for point in outer)
                        min_y = min(point[1] for point in outer)
                        max_x = max(point[0] for point in outer)
                        max_y = max(point[1] for point in outer)
                        nearby_candidates = []
                        for tile_x in range(int(min_x // 10000), int(max_x // 10000) + 1):
                            for tile_y in range(int(min_y // 10000), int(max_y // 10000) + 1):
                                nearby_candidates.extend(candidate_buckets.get((tile_x, tile_y), ()))
                        for property_id, point in nearby_candidates:
                            if not (min_x <= point[0] <= max_x and min_y <= point[1] <= max_y):
                                continue
                            if not point_in_polygon(point, rings):
                                continue
                            candidate_hits.setdefault(property_id, {})[inspire_id] = round(boundary_distance(point, rings), 4)
                            existing_candidate = candidate_features.get(inspire_id)
                            if existing_candidate is not None:
                                if existing_candidate["digest"] != digest:
                                    raise ValueError(f"Candidate INSPIRE ID {inspire_id} differs within authority source")
                                existing_candidate["authorities"].add(authority["slug"])
                            else:
                                candidate_features[inspire_id] = {
                                    "inspireId": inspire_id,
                                    "rings": rings,
                                    "validFrom": valid_from,
                                    "beginLifespanVersion": begin_lifespan,
                                    "authorities": {authority["slug"]},
                                    "digest": digest,
                                }
                except Exception:
                    malformed += 1
                    raise
                finally:
                    element.clear()
                    root.clear()
    if number_matched != source_occurrences:
        raise ValueError(f"{authority['name']} declares {number_matched:,} features but contains {source_occurrences:,}")
    if source_occurrences < int(authority["minimumFeatures"]):
        raise ValueError(f"{authority['name']} source count regressed to {source_occurrences:,}")
    return ({
        "authority": authority["name"],
        "authoritySlug": authority["slug"],
        "zipFilename": zip_path.name,
        "zipSha256": sha256_file(zip_path),
        "zipBytes": zip_path.stat().st_size,
        "gmlBytes": gml_info.file_size,
        "generatedAt": generated_at,
        "declaredFeatures": number_matched,
        "featureOccurrences": source_occurrences,
        "distinctInspireIds": len(seen_digests),
        "duplicateOccurrences": duplicate_occurrences,
        "malformedFeatures": malformed,
        "quarantinedInspireIds": sorted(quarantined),
    }, selected, seen_digests, candidate_hits, candidate_features)


def public_parcel(record: dict, transformer) -> dict:
    rings = record["rings"]
    area, centroid_bng = polygon_measurements(rings)
    published_area = round(area, 2)
    geometry = []
    all_wgs = []
    for ring_index, ring in enumerate(rings):
        wgs_ring = []
        for easting, northing in ring:
            longitude, latitude = transformer.transform(easting, northing)
            # Eight places retains the smallest structurally valid selected
            # source ring (a millimetre-scale HMLR interior artefact). Exact
            # duplicate rounded vertices are still collapsed below.
            point = [round(longitude, 8), round(latitude, 8)]
            if not wgs_ring or point != wgs_ring[-1]:
                wgs_ring.append(point)
        if wgs_ring[-1] != wgs_ring[0]:
            wgs_ring.append(wgs_ring[0])
        if len(wgs_ring) < 4 or len({tuple(point) for point in wgs_ring[:-1]}) < 3:
            raise ValueError(f"Rounded parcel {record['inspireId']} ring {ring_index} has fewer than three vertices")
        winding = translated_signed_area([(point[0], point[1]) for point in wgs_ring])
        should_be_positive = ring_index == 0
        if (should_be_positive and winding < 0) or (not should_be_positive and winding > 0):
            wgs_ring.reverse()
        all_wgs.extend(wgs_ring)
        geometry.append(wgs_ring)
    centroid = transformer.transform(*centroid_bng)
    return {
        "inspireId": record["inspireId"],
        "validFrom": record["validFrom"],
        "beginLifespanVersion": record["beginLifespanVersion"],
        "authorities": sorted(record["authorities"]),
        "areaSquareMetres": published_area,
        "areaSquareFeet": round(published_area * METRES_TO_SQUARE_FEET),
        "areaAcres": round(published_area * METRES_TO_ACRES, 4),
        "areaBasis": "planar area of the HMLR INSPIRE index polygon in EPSG:27700",
        "isExactLegalExtent": False,
        "centroid": [round(centroid[0], 7), round(centroid[1], 7)],
        "bbox": [
            min(point[0] for point in all_wgs),
            min(point[1] for point in all_wgs),
            max(point[0] for point in all_wgs),
            max(point[1] for point in all_wgs),
        ],
        "geometry": {"type": "Polygon", "coordinates": geometry},
    }


def coverage_metadata(canonical_ids: set[str], associations: dict[str, dict]) -> dict:
    automatic = sum(item.get("associationStatus") == "automatic_indicative" for item in associations.values())
    reviewed = sum(item.get("associationStatus") == "reviewed_indicative" for item in associations.values())
    associated = len(associations)
    total = len(canonical_ids)
    return {
        "canonicalProperties": total,
        "associatedProperties": associated,
        "automaticIndicative": automatic,
        "reviewedIndicative": reviewed,
        "coveragePercent": round(associated / total * 100, 4) if total else 0,
        "unassociatedProperties": total - associated,
    }


def merge_feature(target: dict[str, dict], inspire_id: str, item: dict, context: str) -> None:
    existing = target.get(inspire_id)
    if existing is not None:
        if existing["digest"] != item["digest"]:
            raise ValueError(f"INSPIRE ID {inspire_id} differs across authority downloads ({context})")
        existing["authorities"].update(item["authorities"])
    else:
        target[inspire_id] = item


def build_feed(
    config: dict,
    registry: dict,
    source_dir: Path,
    transformer,
    canonical_ids: set[str],
    flat_property_ids: set[str],
    property_uprn_feed: dict,
    registry_sha256: str,
    prior_associations: dict[str, dict],
    association_transitions: list[dict],
    prior_release_id: str | None,
    prior_source_snapshot: str | None,
    prior_published_transitions: list[dict],
) -> tuple[dict, dict]:
    if registry.get("canonicalIdentityMode") != IDENTITY_MODE:
        raise ValueError("Association registry canonical identity mode has drifted")
    records = registry.get("records") or []
    registry_property_ids = {row["propertyId"] for row in records}
    unknown_registry_properties = sorted(registry_property_ids - canonical_ids)
    if unknown_registry_properties:
        raise ValueError(f"Association registry contains a property absent from the current canonical feed: {unknown_registry_properties[0]}")
    selected_ids = {row["inspireId"] for row in records}
    if len(selected_ids) != len(records):
        raise ValueError("Association registry must map one distinct parcel per property")
    latest_transition_by_property = {}
    for transition in association_transitions:
        latest_transition_by_property[transition["propertyId"]] = transition
    candidate_links = {
        property_id: link
        for property_id, link in (property_uprn_feed.get("linksByProperty") or {}).items()
        if property_id not in registry_property_ids
        and not (
            property_id in latest_transition_by_property
            and latest_transition_by_property[property_id]["action"] == "remove"
        )
    }
    candidate_points = {
        property_id: transformer.transform(link["longitude"], link["latitude"], direction="INVERSE")
        for property_id, link in candidate_links.items()
    }
    known_quarantines = {row["inspireId"]: row for row in config.get("knownSourceQuarantines") or []}
    if selected_ids.intersection(known_quarantines):
        raise ValueError("Association registry selects a structurally quarantined INSPIRE ID")
    all_selected: dict[str, dict] = {}
    source_stats = []
    source_digests: dict[str, str] = {}
    all_candidate_hits: dict[str, dict[str, float]] = {}
    all_candidate_features: dict[str, dict] = {}
    observed_quarantines: set[str] = set()
    for authority in config["authorities"]:
        zip_path = source_dir / f"{authority['slug']}.zip"
        if not zip_path.exists():
            raise FileNotFoundError(f"Missing HMLR source ZIP: {zip_path}")
        stats, selected, authority_digests, candidate_hits, candidate_features = audit_source(
            zip_path,
            authority,
            selected_ids,
            set(known_quarantines),
            candidate_points,
        )
        source_stats.append(stats)
        for inspire_id, digest in authority_digests.items():
            existing_digest = source_digests.get(inspire_id)
            if existing_digest is not None and existing_digest != digest:
                raise ValueError(f"INSPIRE ID {inspire_id} differs across authority downloads")
            source_digests.setdefault(inspire_id, digest)
        observed_quarantines.update(stats["quarantinedInspireIds"])
        for inspire_id, item in selected.items():
            merge_feature(all_selected, inspire_id, item, "approved selection")
        for property_id, hits in candidate_hits.items():
            all_candidate_hits.setdefault(property_id, {}).update(hits)
        for inspire_id, item in candidate_features.items():
            merge_feature(all_candidate_features, inspire_id, item, "UPRN candidate")
    if len(source_digests) < int(config["minimumDistinctInspireIds"]):
        raise ValueError(f"Distinct source ID count regressed to {len(source_digests):,}")
    missing = sorted(selected_ids - set(all_selected))
    if missing:
        affected = [
            {"propertyId": row["propertyId"], "missingParcelId": row["inspireId"], "requiredAction": "review replace or remove transition"}
            for row in records
            if row["inspireId"] in set(missing)
        ]
        diagnostic = {
            "schemaVersion": 1,
            "status": "blocked_reviewed_transition_required",
            "observedAuthoritySourceTimes": sorted({row["generatedAt"] for row in source_stats}),
            "missingParcelIds": missing,
            "affectedAssociations": affected,
            "note": "The prior publication remains authoritative until config/inspire-association-transitions.json and the association registry are reviewed together. Use replace when a reviewed successor parcel exists; remove is terminal.",
        }
        atomic_write(DEFAULT_TRANSITION_DIAGNOSTIC, json.dumps(diagnostic, indent=2, sort_keys=True) + "\n")
        raise ValueError(f"{len(missing):,} registered parcels are absent; first missing: {missing[0]}")
    expected_quarantines = {key for key, row in known_quarantines.items() if not row.get("selected")}
    if observed_quarantines != expected_quarantines:
        raise ValueError(
            f"Source quarantine set changed: expected {sorted(expected_quarantines)}, "
            f"observed {sorted(observed_quarantines)}"
        )
    timestamps = [row["generatedAt"] for row in source_stats]
    snapshot_dates = {timestamp[:10] for timestamp in timestamps if re.match(r"^\d{4}-\d{2}-\d{2}T", timestamp)}
    if len(snapshot_dates) != 1 or len(timestamps) != len(source_stats):
        raise ValueError(f"Authority source timestamps do not share one release date: {sorted(snapshot_dates)}")
    snapshot_date = next(iter(snapshot_dates))
    source_snapshot = f"hmlr-inspire-{snapshot_date}"
    associations = {}
    for row in records:
        associations[row["propertyId"]] = {
            "associationStatus": row["associationStatus"],
            "primaryParcelId": row["inspireId"],
            "parcelIds": [row["inspireId"]],
            "matchMethod": row["matchMethod"],
            "evidenceTier": row["evidenceTier"],
            "spatialClassification": row["spatialClassification"],
            "boundaryDistanceMetres": row["boundaryDistanceMetres"],
            "reviewDecision": row["reviewDecision"],
            "sourceSnapshot": source_snapshot,
            "titleConfirmed": False,
            "exactUprnIdentityConfirmed": False,
            "legalBoundaryConfirmed": False,
        }
    review_candidates = {}
    provisional: dict[str, tuple[str, float]] = {}
    for property_id, link in sorted(candidate_links.items()):
        hits = all_candidate_hits.get(property_id, {})
        parcel_ids = sorted(hits, key=int)
        if link.get("matchStatus") != "confirmed_address_match" or link.get("evidenceTier") != "authoritative_address_source":
            outcome = "review_required_non_authoritative_link"
        elif len(parcel_ids) == 0:
            outcome = "rejected_no_containing_inspire_parcel"
        elif len(parcel_ids) > 1:
            outcome = "review_required_multiple_containing_parcels"
        elif hits[parcel_ids[0]] <= 2:
            outcome = "review_required_boundary_proximity"
        elif parcel_ids[0] in selected_ids:
            outcome = "review_required_parcel_already_associated"
        else:
            outcome = "provisional_unique_clear"
            provisional[property_id] = (parcel_ids[0], hits[parcel_ids[0]])
        review_candidates[property_id] = {
            "propertyId": property_id,
            "linkMatchStatus": link["matchStatus"],
            "linkEvidenceTier": link["evidenceTier"],
            "coordinateSource": link["coordinateSource"],
            "linkSourceSnapshot": link["sourceSnapshot"],
            "candidateParcelIds": parcel_ids,
            "boundaryDistancesMetres": {parcel_id: hits[parcel_id] for parcel_id in parcel_ids},
            "outcome": outcome,
            "titleConfirmed": False,
            "exactUprnIdentityConfirmed": False,
            "legalBoundaryConfirmed": False,
        }
    provisional_parcel_owners: dict[str, list[str]] = {}
    for property_id, (parcel_id, _distance) in provisional.items():
        provisional_parcel_owners.setdefault(parcel_id, []).append(property_id)
    for property_id, (parcel_id, distance) in provisional.items():
        if len(provisional_parcel_owners[parcel_id]) != 1:
            review_candidates[property_id]["outcome"] = "review_required_parcel_shared_by_new_links"
            continue
        associations[property_id] = {
            "associationStatus": "automatic_indicative",
            "primaryParcelId": parcel_id,
            "parcelIds": [parcel_id],
            "matchMethod": "accepted-authoritative-uprn-unique-clear-containment",
            "evidenceTier": "authoritative_uprn_indicative",
            "spatialClassification": "unique_interior_clear",
            "boundaryDistanceMetres": distance,
            "reviewDecision": None,
            "sourceSnapshot": source_snapshot,
            "titleConfirmed": False,
            "exactUprnIdentityConfirmed": False,
            "legalBoundaryConfirmed": False,
        }
        selected_ids.add(parcel_id)
        merge_feature(all_selected, parcel_id, all_candidate_features[parcel_id], "accepted UPRN association")
        review_candidates[property_id]["outcome"] = "automatically_associated_indicative"
    if association_transitions[:len(prior_published_transitions)] != prior_published_transitions:
        raise ValueError("Published association transition history must remain an exact append-only prefix")
    new_transitions = association_transitions[len(prior_published_transitions):]
    new_transition_by_property = {}
    for transition in new_transitions:
        property_id = transition["propertyId"]
        if property_id in new_transition_by_property:
            raise ValueError(f"Only one new association transition per property may be published in a release: {property_id}")
        new_transition_by_property[property_id] = transition
    for property_id, prior in prior_associations.items():
        current = associations.get(property_id)
        if current is not None and current.get("primaryParcelId") == prior.get("primaryParcelId"):
            continue
        if property_id not in new_transition_by_property:
            raise ValueError(
                f"Published association changed/disappeared for {property_id}; "
                "add an explicit reviewed association transition"
            )
    for property_id, transition in new_transition_by_property.items():
        prior = prior_associations.get(property_id)
        if prior is None:
            raise ValueError(f"Association transition for {property_id} has no prior published association")
        if transition["previousParcelId"] != prior.get("primaryParcelId"):
            raise ValueError(f"Association transition previous parcel differs for {property_id}")
        if transition["priorAssociationReleaseId"] != prior_release_id:
            raise ValueError(f"Association transition prior release differs for {property_id}")
        if transition["priorSourceSnapshot"] != prior_source_snapshot:
            raise ValueError(f"Association transition prior source snapshot differs for {property_id}")
        current = associations.get(property_id)
        if transition["action"] == "remove":
            if current is not None or transition["replacementParcelId"] is not None:
                raise ValueError(f"Remove transition did not remove association for {property_id}")
        else:
            if transition["replacementParcelId"] == transition["previousParcelId"]:
                raise ValueError(f"Replace transition is a no-op for {property_id}")
            if current is None or current.get("primaryParcelId") != transition["replacementParcelId"]:
                raise ValueError(f"Replace transition does not match resulting association for {property_id}")
    parcels = {inspire_id: public_parcel(all_selected[inspire_id], transformer) for inspire_id in sorted(selected_ids, key=int)}
    associations = dict(sorted(associations.items()))
    source_study = registry.get("sourceStudy") or {}
    flat_associations = len(set(associations).intersection(flat_property_ids))
    feed = {
        "schemaVersion": 1,
        "canonicalIdentityMode": IDENTITY_MODE,
        "associationSemantics": ASSOCIATION_SEMANTICS,
        "associationTransitions": association_transitions,
        "source": {
            "name": "HM Land Registry INSPIRE Index Polygons",
            "sourcePage": config["sourcePage"],
            "sourceSnapshot": source_snapshot,
            "publicationCadence": config["publicationCadence"],
            "sourceCrs": "EPSG:27700",
            "displayCrs": "EPSG:4326",
            "displayCoordinateDecimalPlaces": 8,
            "displayTransform": {
                "method": "PROJ hgridshift using the official OSTN15 NTv2 grid",
                "gridFilename": OSTN15_GRID_FILENAME,
                "gridSha256": OSTN15_GRID_SHA256,
                "benchmarkBng": [512196, 161083],
                "benchmarkWgs84": [-0.3907157736, 51.3377744847],
            },
            "licence": "Open Government Licence v3.0",
            "licenceUrl": "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/",
            "conditionsUrl": "https://use-land-property-data.service.gov.uk/datasets/inspire/#conditions",
            "hmlrAttribution": f"This information is subject to Crown copyright and database rights {snapshot_date[:4]} and is reproduced with the permission of HM Land Registry.",
            "osAttribution": f"The polygons (including the associated geometry, namely x, y co-ordinates) are subject to Crown copyright and database rights {snapshot_date[:4]} Ordnance Survey AC0000851063.",
            "areaCaveat": "Indicative HMLR INSPIRE index-polygon area only; not a measured site survey, title plan or exact legal boundary.",
            "authorityStats": source_stats,
            "sourceFeatureOccurrences": sum(row["featureOccurrences"] for row in source_stats),
            "sourceDistinctInspireIds": len(source_digests),
            "sourceDuplicateOccurrences": sum(row["featureOccurrences"] for row in source_stats) - len(source_digests),
            "knownSourceQuarantines": [known_quarantines[key] for key in sorted(observed_quarantines, key=int)],
            "associationRegistrySha256": registry_sha256,
            "associationApprovalBaseline": registry["approvalBaseline"],
            "automaticCohortProvenance": {
                "ubdcPricePaidToUprnLookup": source_study.get("ubdcPricePaidToUprnLookup"),
                "epcExpansionCalibration": source_study.get("epcExpansionCalibration"),
            },
            "datasetScope": "HMLR INSPIRE freehold index polygons; leasehold extents are not included",
            "flatMaisonetteAssociationContext": {
                "associatedProperties": flat_associations,
                "expectedApprovedBaseline": 17,
                "semantics": "indicative superior/freehold parcel context only; not a leasehold extent",
            },
        },
        "coverage": coverage_metadata(canonical_ids, associations),
        "associationsByProperty": associations,
        "parcelsById": parcels,
    }
    review_parcel_ids = {parcel_id for item in review_candidates.values() for parcel_id in item["candidateParcelIds"]}
    review_parcels = {
        inspire_id: public_parcel(all_candidate_features[inspire_id], transformer)
        for inspire_id in sorted(review_parcel_ids, key=int)
        if inspire_id in all_candidate_features
    }
    review_queue = {
        "schemaVersion": 1,
        "canonicalIdentityMode": IDENTITY_MODE,
        "sourceSnapshot": source_snapshot,
        "queueSemantics": "UPRN-linked spatial onboarding outcomes; only automatically_associated_indicative enters the parcel feed and never confirms title, exact UPRN identity or legal boundary",
        "counts": {
            "linksEvaluated": len(review_candidates),
            "automaticallyAssociatedIndicative": sum(item["outcome"] == "automatically_associated_indicative" for item in review_candidates.values()),
            "reviewOrRejected": sum(item["outcome"] != "automatically_associated_indicative" for item in review_candidates.values()),
        },
        "candidatesByProperty": review_candidates,
        "candidateParcelsById": review_parcels,
    }
    return feed, review_queue


def publication_time(existing_path: Path, global_name: str, core_payload: dict, requested: str | None) -> str:
    existing = existing_global(existing_path, global_name)
    raw_core = json.dumps(core_payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    if existing_path.exists():
        _existing_runtime, existing_raw_core, _digest = parse_runtime(existing_path, global_name)
        if existing_raw_core == raw_core and isinstance(existing.get("generatedAt"), str):
            return existing["generatedAt"]
    value = requested or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        raise ValueError("Publication time must be a UTC ISO timestamp without fractional seconds")
    return value


def existing_global(path: Path, global_name: str) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    prefix = global_name + " = "
    if not text.startswith(prefix) or not text.endswith(";"):
        raise ValueError(f"Existing {path} does not assign {global_name}")
    return json.loads(text[len(prefix):-1])


def validated_transitions(payload: dict) -> list[dict]:
    if payload.get("schemaVersion") != 1 or not isinstance(payload.get("records"), list):
        raise ValueError("Association transition ledger is malformed")
    if payload.get("semantics") != "ordered append-only reviewed replacement or terminal removal of any previously published property-to-INSPIRE association":
        raise ValueError("Association transition semantics have drifted")
    result = []
    transition_ids = set()
    latest_by_property = {}
    required_fields = {
        "transitionId", "propertyId", "previousParcelId", "priorAssociationReleaseId",
        "priorSourceSnapshot", "action", "replacementParcelId", "reviewedAt",
        "reviewedBy", "reason",
    }
    for record in payload["records"]:
        if not isinstance(record, dict) or set(record) != required_fields:
            raise ValueError("Association transition fields do not match the reviewed contract")
        property_id = record.get("propertyId")
        if not isinstance(property_id, str) or not property_id.startswith("property:"):
            raise ValueError("Association transition property IDs must be canonical keys")
        if not str(record.get("previousParcelId") or "").isdigit():
            raise ValueError(f"Association transition previousParcelId is invalid for {property_id}")
        if not re.fullmatch(r"inspire-transition-[a-z0-9_-]+", str(record.get("transitionId") or "")):
            raise ValueError(f"Association transition ID is invalid for {property_id}")
        if record["transitionId"] in transition_ids:
            raise ValueError(f"Association transition ID is duplicated: {record['transitionId']}")
        transition_ids.add(record["transitionId"])
        if not re.fullmatch(r"inspire-parcels-\d{4}-\d{2}-\d{2}-[0-9a-f]{12}", str(record.get("priorAssociationReleaseId") or "")):
            raise ValueError(f"Prior association release is invalid for {property_id}")
        if not re.fullmatch(r"hmlr-inspire-\d{4}-\d{2}-\d{2}", str(record.get("priorSourceSnapshot") or "")):
            raise ValueError(f"Prior source snapshot is invalid for {property_id}")
        if record["priorAssociationReleaseId"][16:26] != record["priorSourceSnapshot"][13:23]:
            raise ValueError(f"Prior association release/snapshot dates differ for {property_id}")
        reviewed_at = str(record.get("reviewedAt") or "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", reviewed_at):
            raise ValueError(f"Association transition timestamp is invalid for {property_id}")
        if datetime.fromisoformat(reviewed_at.replace("Z", "+00:00")) > datetime.now(timezone.utc):
            raise ValueError(f"Association transition timestamp is in the future for {property_id}")
        if not clean(record.get("reviewedBy")) or not clean(record.get("reason")):
            raise ValueError(f"Association transition review evidence is incomplete for {property_id}")
        action = record.get("action")
        replacement = record.get("replacementParcelId")
        if action == "replace" and not str(replacement or "").isdigit():
            raise ValueError(f"Replace transition needs replacementParcelId for {property_id}")
        if action == "replace" and replacement == record.get("previousParcelId"):
            raise ValueError(f"Replace transition is a no-op for {property_id}")
        if action == "remove" and replacement is not None:
            raise ValueError(f"Remove transition must set replacementParcelId null for {property_id}")
        if action not in {"replace", "remove"}:
            raise ValueError(f"Association transition action is invalid for {property_id}")
        previous = latest_by_property.get(property_id)
        if previous is not None:
            if previous["action"] == "remove":
                raise ValueError(f"Terminal remove transition cannot be followed for {property_id}")
            if record["previousParcelId"] != previous["replacementParcelId"]:
                raise ValueError(f"Association transition parcel chain is discontinuous for {property_id}")
            if record["reviewedAt"] <= previous["reviewedAt"]:
                raise ValueError(f"Association transition review times are not increasing for {property_id}")
        latest_by_property[property_id] = record
        result.append(record)
    return result


def publish_runtime_feeds(
    feed_core: dict,
    review_core: dict,
    output: Path,
    review_output: Path,
    requested_publication_time: str | None,
) -> tuple[dict, dict, bool, bool]:
    feed_generated_at = publication_time(output, GLOBAL_NAME, feed_core, requested_publication_time)
    review_generated_at = publication_time(
        review_output,
        REVIEW_GLOBAL_NAME,
        review_core,
        requested_publication_time,
    )
    snapshot_date = feed_core["source"]["sourceSnapshot"].removeprefix("hmlr-inspire-")
    feed_body, _feed_release = finalise_body(feed_core, feed_generated_at, "inspire-parcels", snapshot_date)
    review_body, _review_release = finalise_body(review_core, review_generated_at, "inspire-parcel-review", snapshot_date)
    feed = json.loads(feed_body)
    review_queue = json.loads(review_body)
    content = GLOBAL_NAME + " = " + feed_body + ";\n"
    review_content = REVIEW_GLOBAL_NAME + " = " + review_body + ";\n"
    changed = atomic_write(output, content)
    review_changed = atomic_write(review_output, review_content)
    return feed, review_queue, changed, review_changed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorities", type=Path, default=DEFAULT_AUTHORITIES)
    parser.add_argument("--associations", type=Path, default=DEFAULT_ASSOCIATIONS)
    parser.add_argument("--transactions", type=Path, default=DEFAULT_TRANSACTIONS)
    parser.add_argument("--property-uprn-links", type=Path, default=DEFAULT_PROPERTY_UPRN_LINKS)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--download-current", action="store_true")
    parser.add_argument("--download-dir", type=Path, default=Path("/tmp/insight-hmlr-inspire"))
    parser.add_argument("--ostn15-grid", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--review-output", type=Path, default=DEFAULT_REVIEW_OUTPUT)
    parser.add_argument("--association-transitions", type=Path, default=DEFAULT_ASSOCIATION_TRANSITIONS)
    parser.add_argument("--publication-time")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = json.loads(args.authorities.read_text(encoding="utf-8"))
    registry = json.loads(args.associations.read_text(encoding="utf-8"))
    transactions, _summary, _metadata = read_js(args.transactions)
    canonical_ids = {row.get("propertyRecordId") for row in transactions if row.get("propertyRecordId")}
    flat_property_ids = {
        row.get("propertyRecordId")
        for row in transactions
        if row.get("propertyRecordId")
        and (
            "flat" in clean(row.get("propertyType")).casefold()
            or "maisonette" in clean(row.get("propertyType")).casefold()
        )
    }
    property_uprn_feed = parse_uprn_feed(args.property_uprn_links)
    uprn_failures = uprn_validation_failures(property_uprn_feed, canonical_ids)
    if uprn_failures:
        raise ValueError("Invalid property-UPRN feed: " + "; ".join(uprn_failures))
    prior_feed = existing_global(args.output, GLOBAL_NAME)
    prior_associations = dict(prior_feed.get("associationsByProperty") or {})
    prior_published_transitions = list(prior_feed.get("associationTransitions") or [])
    association_transitions = validated_transitions(json.loads(args.association_transitions.read_text(encoding="utf-8")))
    transformer = configure_ostn15_transform(args.ostn15_grid)
    if args.download_current:
        download_current_sources(config, args.download_dir)
    source_dir = args.download_dir if args.download_current else args.source_dir
    if source_dir is None:
        raise SystemExit("Pass --source-dir or --download-current")
    feed, review_queue = build_feed(
        config,
        registry,
        source_dir,
        transformer,
        canonical_ids,
        flat_property_ids,
        property_uprn_feed,
        sha256_file(args.associations),
        prior_associations,
        association_transitions,
        prior_feed.get("releaseId"),
        prior_feed.get("source", {}).get("sourceSnapshot") if isinstance(prior_feed.get("source"), dict) else None,
        prior_published_transitions,
    )
    feed, review_queue, changed, review_changed = publish_runtime_feeds(
        feed,
        review_queue,
        args.output,
        args.review_output,
        args.publication_time,
    )
    content = args.output.read_text(encoding="utf-8")
    print(
        f"{'Wrote' if changed else 'Unchanged'} {args.output}: "
        f"{feed['coverage']['associatedProperties']:,} associations, "
        f"{len(feed['parcelsById']):,} parcels, "
        f"{len(content.encode('utf-8')):,} bytes, "
        f"{feed['source']['sourceSnapshot']}"
    )
    print(
        f"{'Wrote' if review_changed else 'Unchanged'} {args.review_output}: "
        f"{review_queue['counts']['linksEvaluated']:,} UPRN-linked properties evaluated"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
