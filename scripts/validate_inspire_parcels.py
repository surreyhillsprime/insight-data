#!/usr/bin/env python3
"""Validate INSIGHT's indicative HMLR INSPIRE parcel registry and feed."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

from insight_data_utils import read_js
from runtime_release import parse_runtime


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FEED = ROOT / "outputs" / "inspire-parcels.js"
DEFAULT_REGISTRY = ROOT / "config" / "inspire-parcel-associations.json"
DEFAULT_AUTHORITIES = ROOT / "config" / "inspire-authorities.json"
DEFAULT_TRANSACTIONS = ROOT / "outputs" / "surrey-transactions.js"
DEFAULT_ASSOCIATION_TRANSITIONS = ROOT / "config" / "inspire-association-transitions.json"
GLOBAL_PREFIX = "window.INSIGHT_INSPIRE_PARCELS = "
IDENTITY_MODE = "full-normalised-address-plus-postcode-fail-closed"
ASSOCIATION_SEMANTICS = (
    "indicative parcel association; not title, exact UPRN, ownership or "
    "legal-boundary confirmation"
)
ALLOWED_TIERS = {
    "transaction_linked_indicative",
    "calibrated_epc_indicative",
    "reviewed_indicative",
    "authoritative_uprn_indicative",
}
MAX_FEED_BYTES = 16 * 1024 * 1024
OSTN15_GRID_SHA256 = "5d6ed64d2119952c4c559fa1fccbc594b6520fc3ec3ef2fc10be13202c4384fa"


def parse_feed(path: Path) -> dict:
    feed, _raw_core, _digest = parse_runtime(path, "window.INSIGHT_INSPIRE_PARCELS")
    return feed


def object_contains_key(value: object, forbidden: str) -> bool:
    if isinstance(value, dict):
        return any(key.casefold() == forbidden.casefold() or object_contains_key(child, forbidden) for key, child in value.items())
    if isinstance(value, list):
        return any(object_contains_key(child, forbidden) for child in value)
    return False


def finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def ring_signed_area(ring: list[list[float]]) -> float:
    x0, y0 = ring[0]
    return math.fsum(
        (first[0] - x0) * (second[1] - y0) - (second[0] - x0) * (first[1] - y0)
        for first, second in zip(ring, ring[1:])
    ) / 2


def registry_failures(registry: dict, canonical_ids: set[str]) -> list[str]:
    failures = []
    if registry.get("schemaVersion") != 1:
        failures.append("registry schemaVersion must be 1")
    if registry.get("canonicalIdentityMode") != IDENTITY_MODE:
        failures.append("registry canonical identity mode has drifted")
    if registry.get("associationSemantics") != ASSOCIATION_SEMANTICS:
        failures.append("registry association semantics have drifted")
    source_study = registry.get("sourceStudy") if isinstance(registry.get("sourceStudy"), dict) else {}
    calibration = source_study.get("epcExpansionCalibration") if isinstance(source_study.get("epcExpansionCalibration"), dict) else {}
    if calibration != {"observedCorrect": 815, "observedTotal": 816, "parcelPrecisionPercent": 99.8775}:
        failures.append("registry EPC calibration must remain the measured 815/816 sample")
    if "automaticRuleMeasuredPrecisionPercent" in source_study:
        failures.append("registry must not mislabel EPC calibration as precision for the whole automatic tier")
    records = registry.get("records")
    if not isinstance(records, list):
        return failures + ["registry records must be an array"]
    properties, parcels = [], []
    status_counts = {"automatic_indicative": 0, "reviewed_indicative": 0}
    for record in records:
        property_id, inspire_id = record.get("propertyId"), record.get("inspireId")
        properties.append(property_id)
        parcels.append(inspire_id)
        if property_id not in canonical_ids:
            failures.append(f"registry has unknown canonical property {property_id}")
        if not isinstance(inspire_id, str) or not inspire_id.isdigit():
            failures.append(f"{property_id}: INSPIRE ID must be a numeric string")
        status = record.get("associationStatus")
        if status not in status_counts:
            failures.append(f"{property_id}: invalid associationStatus")
        else:
            status_counts[status] += 1
        if record.get("evidenceTier") not in ALLOWED_TIERS:
            failures.append(f"{property_id}: invalid evidenceTier")
        if status == "reviewed_indicative":
            decision = record.get("reviewDecision")
            if not isinstance(decision, dict) or decision.get("decision") != "approve_indicative_parcel":
                failures.append(f"{property_id}: reviewed association lacks indicative approval")
            if record.get("evidenceTier") != "reviewed_indicative":
                failures.append(f"{property_id}: reviewed association has wrong evidenceTier")
        elif record.get("reviewDecision") is not None:
            failures.append(f"{property_id}: automatic association must not carry a review decision")
        for flag in ("titleConfirmed", "exactUprnIdentityConfirmed", "legalBoundaryConfirmed"):
            if record.get(flag) is not False:
                failures.append(f"{property_id}: {flag} must be false")
    if len(properties) != len(set(properties)):
        failures.append("registry contains duplicate property IDs")
    if len(parcels) != len(set(parcels)):
        failures.append("registry contains a parcel shared across properties")
    baseline = registry.get("approvalBaseline") if isinstance(registry.get("approvalBaseline"), dict) else {}
    expected_baseline = {
        "canonicalProperties": 3766,
        "automaticIndicative": 2871,
        "reviewedIndicative": 357,
        "associatedProperties": 3228,
        "coveragePercent": 85.7143,
        "semantics": "minimum approved association provenance; live coverage denominator is rebuilt from the current canonical transaction feed",
    }
    if baseline != expected_baseline:
        failures.append(f"registry approval baseline has drifted: {baseline} != {expected_baseline}")
    if len(canonical_ids) < baseline.get("canonicalProperties", 0):
        failures.append("current canonical universe regressed below the approval baseline")
    reviewed_surplus = max(
        0,
        status_counts["reviewed_indicative"] - baseline.get("reviewedIndicative", 0),
    )
    if status_counts["automatic_indicative"] + reviewed_surplus < baseline.get("automaticIndicative", 0):
        failures.append("automatic registry associations regressed below approval baseline")
    if status_counts["reviewed_indicative"] < baseline.get("reviewedIndicative", 0):
        failures.append("reviewed registry associations regressed below approval baseline")
    if len(records) < baseline.get("associatedProperties", 0):
        failures.append("registry associations regressed below approval baseline")
    if object_contains_key(registry, "uprn"):
        failures.append("registry must not publish candidate UPRNs")
    return failures


def feed_failures(feed: dict, registry: dict, authorities: dict, canonical_ids: set[str], feed_bytes: int, registry_sha256: str | None = None, configured_transitions: list[dict] | None = None) -> list[str]:
    failures = []
    if feed_bytes > MAX_FEED_BYTES:
        failures.append(f"parcel feed is {feed_bytes:,} bytes; maximum is {MAX_FEED_BYTES:,}")
    if feed.get("schemaVersion") != 1:
        failures.append("feed schemaVersion must be 1")
    try:
        generated_at = datetime.fromisoformat(str(feed.get("generatedAt") or "").replace("Z", "+00:00"))
    except ValueError:
        generated_at = None
    if generated_at is None or generated_at.tzinfo is None:
        failures.append("feed generatedAt must be an ISO UTC publication timestamp")
    elif generated_at > datetime.now(timezone.utc):
        failures.append("feed generatedAt must not be in the future")
    if not re.fullmatch(r"inspire-parcels-\d{4}-\d{2}-\d{2}-[0-9a-f]{12}", str(feed.get("releaseId") or "")):
        failures.append("feed releaseId is malformed")
    if feed.get("canonicalIdentityMode") != IDENTITY_MODE:
        failures.append("feed canonical identity mode has drifted")
    if feed.get("associationSemantics") != ASSOCIATION_SEMANTICS:
        failures.append("feed association semantics have drifted")
    if object_contains_key(feed, "uprn"):
        failures.append("parcel feed must not contain UPRN keys")
    associations = feed.get("associationsByProperty")
    parcels = feed.get("parcelsById")
    if not isinstance(associations, dict) or not isinstance(parcels, dict):
        return failures + ["feed association and parcel indexes must be objects"]
    registry_by_property = {row["propertyId"]: row for row in registry["records"]}
    transitions = feed.get("associationTransitions")
    if not isinstance(transitions, list):
        failures.append("associationTransitions must be an array")
        transitions = []
    if configured_transitions is not None and transitions != configured_transitions:
        failures.append("published association transitions differ from reviewed configuration")
    required_transition_fields = {
        "transitionId", "propertyId", "previousParcelId", "priorAssociationReleaseId",
        "priorSourceSnapshot", "action", "replacementParcelId", "reviewedAt",
        "reviewedBy", "reason",
    }
    seen_transition_ids = set()
    transitions_by_property: dict[str, list[dict]] = {}
    for transition in transitions:
        if not isinstance(transition, dict):
            failures.append("association transition must be an object")
            continue
        property_id = transition.get("propertyId")
        transition_id = transition.get("transitionId")
        if set(transition) != required_transition_fields:
            failures.append(f"{property_id}: association transition fields do not match the public contract")
        if property_id not in canonical_ids:
            failures.append(f"{property_id}: association transition has unknown canonical identity")
        if not re.fullmatch(r"inspire-transition-[a-z0-9_-]+", str(transition_id or "")):
            failures.append(f"{property_id}: association transition ID is invalid")
        elif transition_id in seen_transition_ids:
            failures.append(f"{property_id}: duplicate association transition ID {transition_id}")
        seen_transition_ids.add(transition_id)
        transitions_by_property.setdefault(property_id, []).append(transition)
        if not str(transition.get("previousParcelId") or "").isdigit():
            failures.append(f"{property_id}: association transition previous parcel is invalid")
        prior_release = str(transition.get("priorAssociationReleaseId") or "")
        prior_snapshot = str(transition.get("priorSourceSnapshot") or "")
        if not re.fullmatch(r"inspire-parcels-\d{4}-\d{2}-\d{2}-[0-9a-f]{12}", prior_release):
            failures.append(f"{property_id}: association transition prior release is invalid")
        if not re.fullmatch(r"hmlr-inspire-\d{4}-\d{2}-\d{2}", prior_snapshot):
            failures.append(f"{property_id}: association transition prior source snapshot is invalid")
        elif re.fullmatch(r"inspire-parcels-\d{4}-\d{2}-\d{2}-[0-9a-f]{12}", prior_release) and prior_release[16:26] != prior_snapshot[13:23]:
            failures.append(f"{property_id}: association transition prior release/snapshot dates differ")
        action = transition.get("action")
        replacement = transition.get("replacementParcelId")
        if action == "remove":
            if replacement is not None:
                failures.append(f"{property_id}: remove transition must have a null replacement")
        elif action == "replace":
            if not str(replacement or "").isdigit() or replacement == transition.get("previousParcelId"):
                failures.append(f"{property_id}: replace transition replacement is invalid or unchanged")
        else:
            failures.append(f"{property_id}: association transition action is invalid")
        try:
            reviewed_at = datetime.fromisoformat(str(transition.get("reviewedAt") or "").replace("Z", "+00:00"))
        except ValueError:
            reviewed_at = None
        if (
            reviewed_at is None
            or reviewed_at.tzinfo is None
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", str(transition.get("reviewedAt") or ""))
            or reviewed_at > datetime.now(timezone.utc)
        ):
            failures.append(f"{property_id}: association transition review time is invalid")
        if not str(transition.get("reviewedBy") or "").strip() or not str(transition.get("reason") or "").strip():
            failures.append(f"{property_id}: association transition review evidence is incomplete")
    for property_id, history in transitions_by_property.items():
        for previous, following in zip(history, history[1:]):
            if previous.get("action") == "remove":
                failures.append(f"{property_id}: terminal remove transition cannot be followed by another transition")
            elif following.get("previousParcelId") != previous.get("replacementParcelId"):
                failures.append(f"{property_id}: association transition parcel chain is discontinuous")
        latest = history[-1]
        current = associations.get(property_id)
        if latest.get("action") == "remove":
            if current is not None:
                failures.append(f"{property_id}: terminal remove transition does not match resulting feed state")
        elif not isinstance(current, dict) or current.get("primaryParcelId") != latest.get("replacementParcelId"):
            failures.append(f"{property_id}: latest replace transition does not match resulting feed state")
    if not set(registry_by_property).issubset(associations):
        failures.append("feed is missing one or more approved registry associations")
    if not {row["inspireId"] for row in registry["records"]}.issubset(parcels):
        failures.append("feed is missing one or more approved registry parcels")
    required_association_fields = {
        "associationStatus", "primaryParcelId", "parcelIds", "matchMethod", "evidenceTier",
        "spatialClassification", "boundaryDistanceMetres", "reviewDecision", "sourceSnapshot",
        "titleConfirmed", "exactUprnIdentityConfirmed", "legalBoundaryConfirmed",
    }
    parcel_owners = {}
    for property_id, association in associations.items():
        if property_id not in canonical_ids:
            failures.append(f"feed contains unknown canonical property {property_id}")
            continue
        if set(association) != required_association_fields:
            failures.append(f"{property_id}: association fields do not match the public contract")
        parcel_id = association.get("primaryParcelId")
        if association.get("parcelIds") != [parcel_id] or parcel_id not in parcels:
            failures.append(f"{property_id}: primary/parcelIds reference is invalid")
        if parcel_id in parcel_owners:
            failures.append(f"parcel {parcel_id} is shared by {parcel_owners[parcel_id]} and {property_id}")
        else:
            parcel_owners[parcel_id] = property_id
        if association.get("associationStatus") not in {"automatic_indicative", "reviewed_indicative"}:
            failures.append(f"{property_id}: invalid associationStatus")
        if association.get("evidenceTier") not in ALLOWED_TIERS:
            failures.append(f"{property_id}: invalid evidenceTier")
        if association.get("spatialClassification") not in {"unique_interior_clear", "unique_interior_edge"}:
            failures.append(f"{property_id}: invalid spatialClassification")
        distance = association.get("boundaryDistanceMetres")
        if distance is not None and (not finite_number(distance) or distance < 0):
            failures.append(f"{property_id}: invalid boundaryDistanceMetres")
        for flag in ("titleConfirmed", "exactUprnIdentityConfirmed", "legalBoundaryConfirmed"):
            if association.get(flag) is not False:
                failures.append(f"{property_id}: {flag} must be false")
        registry_record = registry_by_property.get(property_id)
        if registry_record is None:
            if association.get("matchMethod") != "accepted-authoritative-uprn-unique-clear-containment":
                failures.append(f"{property_id}: non-registry association has an unsupported onboarding method")
            if association.get("associationStatus") != "automatic_indicative" or association.get("evidenceTier") != "authoritative_uprn_indicative":
                failures.append(f"{property_id}: UPRN-onboarded association has invalid status/evidence tier")
            if association.get("spatialClassification") != "unique_interior_clear" or not finite_number(association.get("boundaryDistanceMetres")) or association["boundaryDistanceMetres"] <= 2:
                failures.append(f"{property_id}: UPRN-onboarded association is not uniquely clear by more than 2m")
            if association.get("reviewDecision") is not None:
                failures.append(f"{property_id}: automatic UPRN association must not carry reviewDecision")
            continue
        if parcel_id != registry_record["inspireId"] or association.get("parcelIds") != [parcel_id]:
            failures.append(f"{property_id}: parcel index differs from registry")
        for field in ("associationStatus", "matchMethod", "evidenceTier", "spatialClassification", "boundaryDistanceMetres", "reviewDecision"):
            if association.get(field) != registry_record.get(field):
                failures.append(f"{property_id}: {field} differs from registry")
        if not re.fullmatch(r"hmlr-inspire-\d{4}-\d{2}-\d{2}", str(association.get("sourceSnapshot") or "")):
            failures.append(f"{property_id}: invalid sourceSnapshot")
    if set(parcel_owners) != set(parcels):
        failures.append("association-to-parcel relationship is not a complete bijection")
    for parcel_id, parcel in parcels.items():
        if parcel.get("inspireId") != parcel_id:
            failures.append(f"parcel {parcel_id}: embedded ID differs from object key")
        area = parcel.get("areaSquareMetres")
        if not finite_number(area) or area <= 0:
            failures.append(f"parcel {parcel_id}: areaSquareMetres must be positive and finite")
        else:
            if parcel.get("areaSquareFeet") != round(area * 10.763910416709722):
                failures.append(f"parcel {parcel_id}: square-foot conversion does not reconcile")
            if abs(parcel.get("areaAcres", -1) - round(area * 0.0002471053814671653, 4)) > 0.0001:
                failures.append(f"parcel {parcel_id}: acre conversion does not reconcile")
        if parcel.get("isExactLegalExtent") is not False:
            failures.append(f"parcel {parcel_id}: legal extent flag must be false")
        geometry = parcel.get("geometry")
        rings = geometry.get("coordinates") if isinstance(geometry, dict) and geometry.get("type") == "Polygon" else None
        if not isinstance(rings, list) or not rings:
            failures.append(f"parcel {parcel_id}: Polygon coordinates are missing")
            continue
        points = []
        for index, ring in enumerate(rings):
            if not isinstance(ring, list) or len(ring) < 4 or ring[0] != ring[-1] or len({tuple(point) for point in ring[:-1] if isinstance(point, list) and len(point) == 2}) < 3:
                failures.append(f"parcel {parcel_id}: ring {index} is malformed")
                continue
            if any(first == second for first, second in zip(ring, ring[1:])):
                failures.append(f"parcel {parcel_id}: ring {index} has consecutive duplicate points")
            winding = ring_signed_area(ring)
            if winding == 0 or (index == 0 and winding < 0) or (index > 0 and winding > 0):
                failures.append(f"parcel {parcel_id}: ring {index} has non-canonical winding")
            for point in ring:
                if not isinstance(point, list) or len(point) != 2 or not all(finite_number(value) for value in point):
                    failures.append(f"parcel {parcel_id}: ring {index} has invalid coordinates")
                    continue
                if not (-1.0 <= point[0] <= 0.2 and 50.7 <= point[1] <= 51.6):
                    failures.append(f"parcel {parcel_id}: coordinate is outside the Surrey guardrail")
                points.append(point)
        if points:
            expected_bbox = [min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)]
            if parcel.get("bbox") != expected_bbox:
                failures.append(f"parcel {parcel_id}: bbox does not reconcile")
        centroid = parcel.get("centroid")
        bbox = parcel.get("bbox")
        if not isinstance(centroid, list) or len(centroid) != 2 or not all(finite_number(value) for value in centroid):
            failures.append(f"parcel {parcel_id}: centroid is invalid")
        elif isinstance(bbox, list) and len(bbox) == 4 and not (bbox[0] <= centroid[0] <= bbox[2] and bbox[1] <= centroid[1] <= bbox[3]):
            failures.append(f"parcel {parcel_id}: centroid is outside bbox")
    coverage = feed.get("coverage") if isinstance(feed.get("coverage"), dict) else {}
    expected_coverage = {
        "canonicalProperties": len(canonical_ids),
        "associatedProperties": len(associations),
        "automaticIndicative": sum(item.get("associationStatus") == "automatic_indicative" for item in associations.values()),
        "reviewedIndicative": sum(item.get("associationStatus") == "reviewed_indicative" for item in associations.values()),
        "coveragePercent": round(len(associations) / len(canonical_ids) * 100, 4),
        "unassociatedProperties": len(canonical_ids) - len(associations),
    }
    if coverage != expected_coverage:
        failures.append(f"feed coverage metadata does not reconcile: {coverage} != {expected_coverage}")
    source = feed.get("source") if isinstance(feed.get("source"), dict) else {}
    if registry_sha256 is not None and source.get("associationRegistrySha256") != registry_sha256:
        failures.append("feed association registry SHA does not match the current registry")
    if source.get("publicationCadence") != authorities.get("publicationCadence"):
        failures.append("source publication cadence has drifted")
    if source.get("sourceCrs") != "EPSG:27700" or source.get("displayCrs") != "EPSG:4326":
        failures.append("source/display CRS declaration is invalid")
    if source.get("displayCoordinateDecimalPlaces") != 8:
        failures.append("display coordinate precision has drifted")
    transform = source.get("displayTransform") if isinstance(source.get("displayTransform"), dict) else {}
    if transform.get("gridSha256") != OSTN15_GRID_SHA256 or transform.get("gridFilename") != "uk_os_OSTN15_NTv2_OSGBtoETRS.tif":
        failures.append("display transform must use the pinned official OSTN15 grid")
    if transform.get("benchmarkWgs84") != [-0.3907157736, 51.3377744847]:
        failures.append("display transform benchmark metadata is invalid")
    if source.get("areaCaveat") != "Indicative HMLR INSPIRE index-polygon area only; not a measured site survey, title plan or exact legal boundary.":
        failures.append("source area caveat is missing or overstates the polygon")
    if source.get("sourceDistinctInspireIds", 0) < authorities.get("minimumDistinctInspireIds", 0):
        failures.append("source distinct INSPIRE count regressed")
    stats = source.get("authorityStats") if isinstance(source.get("authorityStats"), list) else []
    authority_by_slug = {item["slug"]: item for item in authorities.get("authorities") or []}
    if {item.get("authoritySlug") for item in stats} != set(authority_by_slug):
        failures.append("source authority list does not match configured Surrey authorities")
    for stat in stats:
        config = authority_by_slug.get(stat.get("authoritySlug"))
        if config and stat.get("featureOccurrences", 0) < config["minimumFeatures"]:
            failures.append(f"{stat.get('authoritySlug')}: source count regressed")
        if stat.get("declaredFeatures") != stat.get("featureOccurrences") or stat.get("malformedFeatures") != 0:
            failures.append(f"{stat.get('authoritySlug')}: source structure metadata is inconsistent")
    if source.get("sourceFeatureOccurrences") != sum(item.get("featureOccurrences", 0) for item in stats):
        failures.append("source occurrence total does not reconcile")
    source_times = []
    for stat in stats:
        try:
            source_times.append(datetime.fromisoformat(str(stat.get("generatedAt") or "").replace("Z", "+00:00")))
        except ValueError:
            failures.append(f"{stat.get('authoritySlug')}: invalid HMLR source timestamp")
    if generated_at is not None and source_times and generated_at < max(source_times):
        failures.append("publication generatedAt predates the HMLR source snapshot")
    if source.get("sourceDuplicateOccurrences") != source.get("sourceFeatureOccurrences", 0) - source.get("sourceDistinctInspireIds", 0):
        failures.append("source duplicate count does not reconcile")
    baseline = registry.get("approvalBaseline") or {}
    if source.get("associationApprovalBaseline") != baseline:
        failures.append("feed does not retain the approved association baseline provenance")
    provenance = source.get("automaticCohortProvenance") if isinstance(source.get("automaticCohortProvenance"), dict) else {}
    ubdc = provenance.get("ubdcPricePaidToUprnLookup") if isinstance(provenance.get("ubdcPricePaidToUprnLookup"), dict) else {}
    if ubdc.get("doi") != "https://doi.org/10.20394/agu7hprj" or ubdc.get("publisher") != "Urban Big Data Centre, University of Glasgow":
        failures.append("UBDC automatic-cohort provenance is missing")
    if source.get("datasetScope") != "HMLR INSPIRE freehold index polygons; leasehold extents are not included":
        failures.append("freehold-only INSPIRE dataset scope is missing")
    flat_context = source.get("flatMaisonetteAssociationContext") if isinstance(source.get("flatMaisonetteAssociationContext"), dict) else {}
    if flat_context.get("associatedProperties", 0) < 17 or flat_context.get("expectedApprovedBaseline") != 17:
        failures.append("flat/maisonette superior-freehold association context regressed below the 17-property baseline")
    expected_quarantines = authorities.get("knownSourceQuarantines") or []
    if source.get("knownSourceQuarantines") != expected_quarantines:
        failures.append("source quarantine ledger differs from configuration")
    if any(item["inspireId"] in parcels for item in expected_quarantines):
        failures.append("published feed contains a quarantined parcel")
    snapshot = source.get("sourceSnapshot")
    if isinstance(snapshot, str) and feed.get("releaseId", "")[16:26] != snapshot.removeprefix("hmlr-inspire-"):
        failures.append("feed releaseId date differs from HMLR source snapshot")
    snapshot_year = str(snapshot or "")[13:17]
    expected_hmlr_attribution = f"This information is subject to Crown copyright and database rights {snapshot_year} and is reproduced with the permission of HM Land Registry."
    expected_os_attribution = f"The polygons (including the associated geometry, namely x, y co-ordinates) are subject to Crown copyright and database rights {snapshot_year} Ordnance Survey AC0000851063."
    if source.get("hmlrAttribution") != expected_hmlr_attribution:
        failures.append("HM Land Registry attribution is not the required exact statement")
    if source.get("osAttribution") != expected_os_attribution:
        failures.append("Ordnance Survey attribution is not the required exact statement")
    if source.get("conditionsUrl") != "https://use-land-property-data.service.gov.uk/datasets/inspire/#conditions":
        failures.append("INSPIRE conditions link is missing or incorrect")
    if any(item.get("sourceSnapshot") != snapshot for item in associations.values()):
        failures.append("association source snapshots are not aligned")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("feed", nargs="?", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--authorities", type=Path, default=DEFAULT_AUTHORITIES)
    parser.add_argument("--transactions", type=Path, default=DEFAULT_TRANSACTIONS)
    parser.add_argument("--association-transitions", type=Path, default=DEFAULT_ASSOCIATION_TRANSITIONS)
    args = parser.parse_args()
    transactions, _summary, _metadata = read_js(args.transactions)
    canonical_ids = {row.get("propertyRecordId") for row in transactions if row.get("propertyRecordId")}
    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    authorities = json.loads(args.authorities.read_text(encoding="utf-8"))
    configured_transitions = json.loads(args.association_transitions.read_text(encoding="utf-8")).get("records") or []
    feed = parse_feed(args.feed)
    failures = registry_failures(registry, canonical_ids)
    registry_sha256 = hashlib.sha256(args.registry.read_bytes()).hexdigest()
    failures.extend(feed_failures(feed, registry, authorities, canonical_ids, args.feed.stat().st_size, registry_sha256, configured_transitions))
    if failures:
        for failure in failures:
            print(f"ERROR: {failure}")
        return 1
    print(
        f"Validated {args.feed}: {len(feed['associationsByProperty']):,} associations, "
        f"{len(feed['parcelsById']):,} parcels, {feed['coverage']['coveragePercent']:.4f}% coverage"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
