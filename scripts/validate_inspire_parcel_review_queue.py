#!/usr/bin/env python3
"""Validate UPRN-linked INSPIRE automatic onboarding and review outcomes."""

from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

from insight_data_utils import read_js
from validate_inspire_parcels import parse_feed as parse_parcel_feed
from runtime_release import parse_runtime


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QUEUE = ROOT / "outputs" / "inspire-parcel-review-queue.js"
DEFAULT_PARCELS = ROOT / "outputs" / "inspire-parcels.js"
DEFAULT_TRANSACTIONS = ROOT / "outputs" / "surrey-transactions.js"
GLOBAL_PREFIX = "window.INSIGHT_INSPIRE_PARCEL_REVIEW_QUEUE = "
IDENTITY_MODE = "full-normalised-address-plus-postcode-fail-closed"
AUTO_OUTCOME = "automatically_associated_indicative"
ALLOWED_OUTCOMES = {
    AUTO_OUTCOME,
    "review_required_non_authoritative_link",
    "rejected_no_containing_inspire_parcel",
    "review_required_multiple_containing_parcels",
    "review_required_boundary_proximity",
    "review_required_parcel_already_associated",
    "review_required_parcel_shared_by_new_links",
}
QUEUE_SEMANTICS = (
    "UPRN-linked spatial onboarding outcomes; only automatically_associated_indicative "
    "enters the parcel feed and never confirms title, exact UPRN identity or legal boundary"
)
TOP_LEVEL_FIELDS = {
    "schemaVersion", "releaseId", "generatedAt", "canonicalIdentityMode",
    "sourceSnapshot", "queueSemantics", "counts", "candidatesByProperty",
    "candidateParcelsById",
}
COUNT_FIELDS = {"linksEvaluated", "automaticallyAssociatedIndicative", "reviewOrRejected"}
CANDIDATE_FIELDS = {
    "propertyId", "linkMatchStatus", "linkEvidenceTier", "coordinateSource",
    "linkSourceSnapshot", "candidateParcelIds", "boundaryDistancesMetres",
    "outcome", "titleConfirmed", "exactUprnIdentityConfirmed",
    "legalBoundaryConfirmed",
}
PARCEL_FIELDS = {
    "inspireId", "validFrom", "beginLifespanVersion", "authorities",
    "areaSquareMetres", "areaSquareFeet", "areaAcres", "areaBasis",
    "isExactLegalExtent", "centroid", "bbox", "geometry",
}
FORBIDDEN_PUBLIC_KEYS = {
    "uprn", "sourceid", "longitude", "latitude", "checkedat",
    "shareduprnreview", "limitations", "address", "fulladdress", "rawaddress",
    "addressline1", "addressline2", "lmkkey", "certificatenumber",
    "sourcepayload", "rawrecord", "reviewnotes", "internalevidence",
}


def finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def ring_signed_area(ring: list[list[float]]) -> float:
    x0, y0 = ring[0]
    return math.fsum(
        (first[0] - x0) * (second[1] - y0) - (second[0] - x0) * (first[1] - y0)
        for first, second in zip(ring, ring[1:])
    ) / 2


def forbidden_public_paths(value: object, path: str = "$") -> list[str]:
    failures = []
    if isinstance(value, dict):
        for key, child in value.items():
            normalised = re.sub(r"[^a-z0-9]+", "", str(key).casefold())
            if (
                normalised in FORBIDDEN_PUBLIC_KEYS
                or normalised.startswith("private")
                or normalised.startswith("internal")
                or normalised.startswith("raw")
            ):
                failures.append(f"{path}.{key}")
            failures.extend(forbidden_public_paths(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            failures.extend(forbidden_public_paths(child, f"{path}[{index}]"))
    return failures


def parse_queue(path: Path) -> dict:
    queue, _raw_core, _digest = parse_runtime(path, "window.INSIGHT_INSPIRE_PARCEL_REVIEW_QUEUE")
    return queue


def validation_failures(queue: dict, parcel_feed: dict, canonical_ids: set[str]) -> list[str]:
    failures = []
    if set(queue) != TOP_LEVEL_FIELDS:
        failures.append("review queue top-level fields do not match the public contract")
    forbidden = forbidden_public_paths(queue)
    if forbidden:
        failures.append(f"review queue contains UPRN/private fields: {', '.join(forbidden[:3])}")
    if queue.get("schemaVersion") != 1 or queue.get("canonicalIdentityMode") != IDENTITY_MODE:
        failures.append("review queue schema/identity contract is invalid")
    if queue.get("queueSemantics") != QUEUE_SEMANTICS:
        failures.append("review queue semantics have drifted")
    if not re.fullmatch(r"inspire-parcel-review-\d{4}-\d{2}-\d{2}-[0-9a-f]{12}", str(queue.get("releaseId") or "")):
        failures.append("review queue releaseId shape is invalid")
    elif queue["releaseId"][22:32] != str(queue.get("sourceSnapshot") or "").removeprefix("hmlr-inspire-"):
        failures.append("review queue release date differs from source snapshot")
    generated_text = str(queue.get("generatedAt") or "")
    try:
        generated_at = datetime.fromisoformat(generated_text.replace("Z", "+00:00"))
    except ValueError:
        generated_at = None
    if (
        generated_at is None
        or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", generated_text)
        or generated_at > datetime.now(timezone.utc)
    ):
        failures.append("review queue generatedAt must be a non-future ISO timestamp")
    if queue.get("sourceSnapshot") != parcel_feed.get("source", {}).get("sourceSnapshot"):
        failures.append("review queue source snapshot differs from parcel feed")
    candidates = queue.get("candidatesByProperty")
    parcels = queue.get("candidateParcelsById")
    if not isinstance(candidates, dict) or not isinstance(parcels, dict):
        return failures + ["review queue candidate indexes must be objects"]
    auto_count = 0
    referenced = set()
    for property_id, candidate in candidates.items():
        if not isinstance(candidate, dict):
            failures.append(f"{property_id}: review-queue candidate must be an object")
            continue
        if set(candidate) != CANDIDATE_FIELDS:
            failures.append(f"{property_id}: candidate fields do not match the public contract")
        if candidate.get("propertyId") != property_id or property_id not in canonical_ids:
            failures.append(f"{property_id}: invalid canonical review-queue identity")
        status, tier = candidate.get("linkMatchStatus"), candidate.get("linkEvidenceTier")
        if status not in {"confirmed_address_match", "reviewed_accepted"}:
            failures.append(f"{property_id}: invalid link match status")
        if (status == "confirmed_address_match" and tier != "authoritative_address_source") or (status == "reviewed_accepted" and tier != "reviewed"):
            failures.append(f"{property_id}: link match status/evidence tier do not reconcile")
        for field in ("coordinateSource", "linkSourceSnapshot"):
            if not isinstance(candidate.get(field), str) or not candidate[field].strip():
                failures.append(f"{property_id}: {field} is required")
        outcome = candidate.get("outcome")
        if outcome not in ALLOWED_OUTCOMES:
            failures.append(f"{property_id}: invalid onboarding outcome")
        parcel_ids = candidate.get("candidateParcelIds")
        distances = candidate.get("boundaryDistancesMetres")
        if (
            not isinstance(parcel_ids, list)
            or any(not isinstance(parcel_id, str) or not parcel_id.isdigit() for parcel_id in parcel_ids)
            or len(parcel_ids) != len(set(parcel_ids))
            or not isinstance(distances, dict)
            or set(distances) != set(parcel_ids)
            or any(not finite_number(distance) or distance < 0 for distance in distances.values())
        ):
            failures.append(f"{property_id}: candidate parcel/distance indexes do not reconcile")
            continue
        referenced.update(parcel_ids)
        for flag in ("titleConfirmed", "exactUprnIdentityConfirmed", "legalBoundaryConfirmed"):
            if candidate.get(flag) is not False:
                failures.append(f"{property_id}: {flag} must be false")
        published_association = parcel_feed.get("associationsByProperty", {}).get(property_id)
        if outcome == AUTO_OUTCOME:
            auto_count += 1
            if len(parcel_ids) != 1 or distances[parcel_ids[0]] <= 2:
                failures.append(f"{property_id}: automatic outcome is not unique and more than 2m clear")
            if not isinstance(published_association, dict) or published_association.get("matchMethod") != "accepted-authoritative-uprn-unique-clear-containment":
                failures.append(f"{property_id}: automatic outcome is absent from parcel feed")
        elif isinstance(published_association, dict) and published_association.get("matchMethod") == "accepted-authoritative-uprn-unique-clear-containment":
            failures.append(f"{property_id}: review/rejection outcome was incorrectly auto-published")
    if referenced != set(parcels):
        failures.append("review queue candidate parcel geometry index is not an exact reference set")
    for parcel_id, parcel in parcels.items():
        if not isinstance(parcel, dict):
            failures.append(f"candidate parcel {parcel_id}: record must be an object")
            continue
        if set(parcel) != PARCEL_FIELDS:
            failures.append(f"candidate parcel {parcel_id}: fields do not match the public contract")
        if parcel.get("inspireId") != parcel_id or not str(parcel_id).isdigit():
            failures.append(f"candidate parcel {parcel_id}: embedded ID differs from numeric object key")
        for field in ("validFrom", "beginLifespanVersion"):
            if not isinstance(parcel.get(field), str) or not parcel[field].strip():
                failures.append(f"candidate parcel {parcel_id}: {field} is required")
        authorities = parcel.get("authorities")
        if (
            not isinstance(authorities, list)
            or not authorities
            or len(authorities) != len(set(authorities))
            or any(not isinstance(item, str) or not item.strip() for item in authorities)
        ):
            failures.append(f"candidate parcel {parcel_id}: authorities are invalid")
        area = parcel.get("areaSquareMetres")
        if not finite_number(area) or area <= 0:
            failures.append(f"candidate parcel {parcel_id}: areaSquareMetres must be positive and finite")
        else:
            if parcel.get("areaSquareFeet") != round(area * 10.763910416709722):
                failures.append(f"candidate parcel {parcel_id}: square-foot conversion does not reconcile")
            acres = parcel.get("areaAcres")
            if not finite_number(acres) or abs(acres - round(area * 0.0002471053814671653, 4)) > 0.0001:
                failures.append(f"candidate parcel {parcel_id}: acre conversion does not reconcile")
        if parcel.get("areaBasis") != "planar area of the HMLR INSPIRE index polygon in EPSG:27700":
            failures.append(f"candidate parcel {parcel_id}: area basis is invalid")
        if parcel.get("isExactLegalExtent") is not False:
            failures.append(f"candidate parcel {parcel_id}: legal extent flag must be false")
        geometry = parcel.get("geometry")
        if not isinstance(geometry, dict) or set(geometry) != {"type", "coordinates"} or geometry.get("type") != "Polygon":
            failures.append(f"candidate parcel {parcel_id}: geometry fields/type are invalid")
            continue
        rings = geometry.get("coordinates")
        if not isinstance(rings, list) or not rings:
            failures.append(f"candidate parcel {parcel_id}: Polygon coordinates are missing")
            continue
        points = []
        for index, ring in enumerate(rings):
            if (
                not isinstance(ring, list)
                or len(ring) < 4
                or ring[0] != ring[-1]
                or any(not isinstance(point, list) or len(point) != 2 or not all(finite_number(value) for value in point) for point in ring)
                or len({tuple(point) for point in ring[:-1] if isinstance(point, list) and len(point) == 2}) < 3
            ):
                failures.append(f"candidate parcel {parcel_id}: ring {index} is malformed")
                continue
            if any(first == second for first, second in zip(ring, ring[1:])):
                failures.append(f"candidate parcel {parcel_id}: ring {index} has consecutive duplicate points")
            winding = ring_signed_area(ring)
            if winding == 0 or (index == 0 and winding < 0) or (index > 0 and winding > 0):
                failures.append(f"candidate parcel {parcel_id}: ring {index} has non-canonical winding")
            for point in ring:
                if not (-1.0 <= point[0] <= 0.2 and 50.7 <= point[1] <= 51.6):
                    failures.append(f"candidate parcel {parcel_id}: coordinate is outside the Surrey guardrail")
                points.append(point)
        if points:
            expected_bbox = [
                min(point[0] for point in points), min(point[1] for point in points),
                max(point[0] for point in points), max(point[1] for point in points),
            ]
            if parcel.get("bbox") != expected_bbox:
                failures.append(f"candidate parcel {parcel_id}: bbox does not reconcile")
        bbox, centroid = parcel.get("bbox"), parcel.get("centroid")
        if not isinstance(bbox, list) or len(bbox) != 4 or not all(finite_number(value) for value in bbox):
            failures.append(f"candidate parcel {parcel_id}: bbox is invalid")
        if not isinstance(centroid, list) or len(centroid) != 2 or not all(finite_number(value) for value in centroid):
            failures.append(f"candidate parcel {parcel_id}: centroid is invalid")
        elif isinstance(bbox, list) and len(bbox) == 4 and all(finite_number(value) for value in bbox) and not (bbox[0] <= centroid[0] <= bbox[2] and bbox[1] <= centroid[1] <= bbox[3]):
            failures.append(f"candidate parcel {parcel_id}: centroid is outside bbox")
        published = parcel_feed.get("parcelsById", {}).get(parcel_id)
        if published is not None and published != parcel:
            failures.append(f"candidate parcel {parcel_id}: geometry differs from the published parcel feed")
    counts = queue.get("counts")
    if not isinstance(counts, dict) or set(counts) != COUNT_FIELDS:
        failures.append("review queue count fields do not match the public contract")
    expected_counts = {
        "linksEvaluated": len(candidates),
        "automaticallyAssociatedIndicative": auto_count,
        "reviewOrRejected": len(candidates) - auto_count,
    }
    if counts != expected_counts:
        failures.append("review queue count metadata does not reconcile")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("queue", nargs="?", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--parcels", type=Path, default=DEFAULT_PARCELS)
    parser.add_argument("--transactions", type=Path, default=DEFAULT_TRANSACTIONS)
    args = parser.parse_args()
    transactions, _summary, _metadata = read_js(args.transactions)
    canonical_ids = {row.get("propertyRecordId") for row in transactions if row.get("propertyRecordId")}
    queue = parse_queue(args.queue)
    failures = validation_failures(queue, parse_parcel_feed(args.parcels), canonical_ids)
    if failures:
        for failure in failures:
            print(f"ERROR: {failure}")
        return 1
    print(f"Validated {args.queue}: {queue['counts']['linksEvaluated']:,} UPRN-linked onboarding outcomes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
