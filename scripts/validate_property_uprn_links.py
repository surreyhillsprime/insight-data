#!/usr/bin/env python3
"""Validate the independent, fail-closed property-to-UPRN evidence stream."""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from insight_data_utils import read_js
from runtime_release import parse_runtime


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FEED = ROOT / "outputs" / "property-uprn-links.js"
DEFAULT_TRANSACTIONS = ROOT / "outputs" / "surrey-transactions.js"
GLOBAL_PREFIX = "window.INSIGHT_PROPERTY_UPRN_LINKS = "
IDENTITY_MODE = "full-normalised-address-plus-postcode-fail-closed"
IDENTITY_WARNING = "UPRN evidence never creates, merges or replaces canonical INSIGHT property identity"
ALLOWED_STATUS = {"confirmed_address_match", "reviewed_accepted"}


def parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_feed(path: Path) -> dict:
    feed, _raw_payload, _digest = parse_runtime(path, "window.INSIGHT_PROPERTY_UPRN_LINKS")
    return feed


def validation_failures(feed: dict, canonical_ids: set[str]) -> list[str]:
    failures = []
    required_top_fields = {
        "schemaVersion", "canonicalIdentityMode", "identityWarning", "sources",
        "linksByProperty", "generatedAt", "releaseId",
    }
    if set(feed) != required_top_fields:
        failures.append("top-level UPRN feed fields do not match the public contract")
    if feed.get("schemaVersion") != 1:
        failures.append("schemaVersion must be 1")
    generated_at = parse_utc(feed.get("generatedAt"))
    if generated_at is None:
        failures.append("generatedAt must be a UTC ISO timestamp")
    elif generated_at > datetime.now(timezone.utc):
        failures.append("generatedAt must not be in the future")
    release_id = str(feed.get("releaseId") or "")
    if not re.fullmatch(r"property-uprn-links-\d{4}-\d{2}-\d{2}-[0-9a-f]{12}", release_id):
        failures.append("releaseId has an invalid content-release shape")
    elif isinstance(feed.get("generatedAt"), str) and release_id[20:30] != feed["generatedAt"][:10]:
        failures.append("releaseId date must equal generatedAt date")
    if feed.get("canonicalIdentityMode") != IDENTITY_MODE:
        failures.append("canonicalIdentityMode must remain property-address based")
    if feed.get("identityWarning") != IDENTITY_WARNING:
        failures.append("identityWarning is missing or altered")
    if not isinstance(feed.get("sources"), list):
        failures.append("sources must be an array")
        sources = []
    else:
        sources = feed["sources"]
    source_by_id = {}
    required_source_fields = {
        "sourceId", "name", "sourceSnapshot", "checkedAt", "coordinateBasis",
        "licenceOrEntitlement", "redistributionClassification",
    }
    required_entitlement_fields = {"type", "reference", "permitsPublicDerivedPublication"}
    for source in sources:
        if not isinstance(source, dict):
            failures.append("every UPRN source must be an object")
            continue
        if set(source) != required_source_fields:
            failures.append("UPRN source fields do not match the public contract")
        source_id = source.get("sourceId")
        if not isinstance(source_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]+", source_id) or source_id in source_by_id:
            failures.append("UPRN sourceId values must be nonempty and unique")
            continue
        source_by_id[source_id] = source
        if not str(source.get("name") or "").strip() or not str(source.get("sourceSnapshot") or "").strip():
            failures.append(f"source {source_id}: name and sourceSnapshot are required")
        checked_at = parse_utc(source.get("checkedAt"))
        if checked_at is None or checked_at > datetime.now(timezone.utc):
            failures.append(f"source {source_id}: checkedAt must be a non-future UTC ISO timestamp")
        if source.get("coordinateBasis") not in {"authoritative_property_point", "authoritative_address_point", "reviewed_property_point"}:
            failures.append(f"source {source_id}: invalid coordinateBasis")
        entitlement = source.get("licenceOrEntitlement")
        if (
            not isinstance(entitlement, dict)
            or set(entitlement) != required_entitlement_fields
            or entitlement.get("permitsPublicDerivedPublication") is not True
            or not str(entitlement.get("reference") or "").strip()
        ):
            failures.append(f"source {source_id}: licence/entitlement evidence is incomplete")
        elif entitlement.get("type") not in {"open_licence", "commercial_entitlement"}:
            failures.append(f"source {source_id}: licence/entitlement type is invalid for a public feed")
        if source.get("redistributionClassification") not in {"public_open_data", "licensed_for_insight_publication"}:
            failures.append(f"source {source_id}: invalid redistributionClassification")
    links = feed.get("linksByProperty")
    if not isinstance(links, dict):
        return failures + ["linksByProperty must be an object"]
    owners: dict[str, list[dict]] = defaultdict(list)
    required_link_fields = {
        "propertyId", "sourceId", "uprn", "matchStatus", "evidenceTier",
        "longitude", "latitude", "coordinateSource", "sourceSnapshot",
        "checkedAt", "limitations",
    }
    optional_link_fields = {"sharedUprnReview"}
    for property_id, link in links.items():
        if not isinstance(link, dict):
            failures.append(f"{property_id}: link must be an object")
            continue
        if not required_link_fields.issubset(link) or not set(link).issubset(required_link_fields | optional_link_fields):
            failures.append(f"{property_id}: link fields do not match the public contract")
        if link.get("propertyId") != property_id:
            failures.append(f"{property_id}: propertyId must equal its canonical object key")
        if property_id not in canonical_ids:
            failures.append(f"{property_id}: unknown canonical property")
        source = source_by_id.get(link.get("sourceId"))
        if source is None:
            failures.append(f"{property_id}: sourceId does not reference declared source metadata")
        else:
            if link.get("sourceSnapshot") != source.get("sourceSnapshot"):
                failures.append(f"{property_id}: sourceSnapshot differs from declared source")
            if source.get("redistributionClassification") == "internal_only_no_publication" or source.get("licenceOrEntitlement", {}).get("permitsPublicDerivedPublication") is not True:
                failures.append(f"{property_id}: source entitlement does not permit public derived publication")
        status = link.get("matchStatus")
        tier = link.get("evidenceTier")
        if status not in ALLOWED_STATUS:
            failures.append(f"{property_id}: matchStatus is not an explicit accepted state")
        if status == "confirmed_address_match" and tier != "authoritative_address_source":
            failures.append(f"{property_id}: confirmed address match requires authoritative evidence tier")
        if status == "reviewed_accepted" and tier != "reviewed":
            failures.append(f"{property_id}: reviewed match requires reviewed evidence tier")
        uprn = str(link.get("uprn") or "")
        if not uprn.isdigit():
            failures.append(f"{property_id}: UPRN must be a numeric string")
        else:
            owners[uprn].append(link)
        longitude, latitude = link.get("longitude"), link.get("latitude")
        if not isinstance(longitude, (int, float)) or isinstance(longitude, bool) or not math.isfinite(longitude) or not -8.75 <= longitude <= 2.1:
            failures.append(f"{property_id}: longitude is not a finite GB coordinate")
        if not isinstance(latitude, (int, float)) or isinstance(latitude, bool) or not math.isfinite(latitude) or not 49.8 <= latitude <= 60.95:
            failures.append(f"{property_id}: latitude is not a finite GB coordinate")
        for field in ("coordinateSource", "sourceSnapshot", "checkedAt"):
            if not isinstance(link.get(field), str) or not link[field].strip():
                failures.append(f"{property_id}: {field} is required")
        checked_at = parse_utc(link.get("checkedAt"))
        if checked_at is None:
            failures.append(f"{property_id}: checkedAt must be a UTC ISO timestamp")
        elif checked_at > datetime.now(timezone.utc):
            failures.append(f"{property_id}: checkedAt must not be in the future")
        if not isinstance(link.get("limitations"), list):
            failures.append(f"{property_id}: limitations must be an array")
        elif any(not isinstance(item, str) for item in link["limitations"]):
            failures.append(f"{property_id}: every limitation must be a string")
        review = link.get("sharedUprnReview")
        if review is not None:
            expected_review_fields = {"status", "reviewId", "reviewedAt", "relatedPropertyIds", "rationale"}
            if not isinstance(review, dict) or set(review) != expected_review_fields:
                failures.append(f"{property_id}: shared UPRN review fields do not match the contract")
            else:
                reviewed_at = parse_utc(review.get("reviewedAt"))
                if reviewed_at is None or reviewed_at > datetime.now(timezone.utc):
                    failures.append(f"{property_id}: shared UPRN reviewedAt must be a non-future UTC ISO timestamp")
                if review.get("status") != "approved_shared_hierarchy" or not str(review.get("reviewId") or "").strip() or not str(review.get("rationale") or "").strip():
                    failures.append(f"{property_id}: shared UPRN review evidence is incomplete")
    for uprn, shared_links in owners.items():
        if len(shared_links) < 2:
            if shared_links[0].get("sharedUprnReview") is not None:
                failures.append(f"UPRN {uprn} carries shared-hierarchy review but has only one property owner")
            continue
        property_ids = {link.get("propertyId") for link in shared_links}
        reviews = [link.get("sharedUprnReview") for link in shared_links]
        explicitly_reviewed = all(
            link.get("matchStatus") == "reviewed_accepted"
            and link.get("evidenceTier") == "reviewed"
            and isinstance(review, dict)
            and review.get("status") == "approved_shared_hierarchy"
            and isinstance(review.get("reviewId"), str) and review["reviewId"].strip()
            and (reviewed_at := parse_utc(review.get("reviewedAt"))) is not None
            and reviewed_at <= datetime.now(timezone.utc)
            and isinstance(review.get("rationale"), str) and review["rationale"].strip()
            and set(review.get("relatedPropertyIds") or []) == property_ids
            and len(review.get("relatedPropertyIds") or []) == len(property_ids)
            for link, review in zip(shared_links, reviews)
        )
        if explicitly_reviewed:
            first = reviews[0]
            explicitly_reviewed = all(
                review.get("reviewId") == first.get("reviewId")
                and review.get("reviewedAt") == first.get("reviewedAt")
                and review.get("rationale") == first.get("rationale")
                and review.get("relatedPropertyIds") == first.get("relatedPropertyIds")
                for review in reviews[1:]
            )
        if not explicitly_reviewed:
            failures.append(f"UPRN {uprn} is owned by multiple properties without approved shared-hierarchy review")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("feed", nargs="?", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--transactions", type=Path, default=DEFAULT_TRANSACTIONS)
    args = parser.parse_args()
    transactions, _summary, _metadata = read_js(args.transactions)
    canonical_ids = {row.get("propertyRecordId") for row in transactions if row.get("propertyRecordId")}
    failures = validation_failures(parse_feed(args.feed), canonical_ids)
    if failures:
        for failure in failures:
            print(f"ERROR: {failure}")
        return 1
    print(f"Validated {args.feed}: {len(parse_feed(args.feed)['linksByProperty']):,} explicit UPRN links")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
