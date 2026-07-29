#!/usr/bin/env python3
"""Validate the canonical INSIGHT Today feed and its evidence guardrails."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

from property_records import parse_window_assignment
from today_feed import (
    GENERATOR_VERSION,
    LANE_ORDER,
    OPPORTUNITY_KINDS,
    SCHEMA_VERSION,
    SIGNAL_KINDS,
    SUMMARY_LABELS,
    TODAY_FEED_NAME,
    TODAY_META_NAME,
    canonical_json,
    item_sort_key,
    iso_datetime,
    normalise,
    planning_record_type,
)
from validate_property_records import _validate_json_schema


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FEED = ROOT / "outputs" / "today-feed.js"
DEFAULT_SCHEMA = ROOT / "config" / "today-feed.schema.json"
USER_STATE_FIELDS = {
    "watch",
    "watched",
    "watching",
    "dismissed",
    "dismissedAt",
    "read",
    "readAt",
    "reviewed",
    "reviewedAt",
    "saved",
    "userState",
    "workflowState",
    "reviewState",
    "watchState",
}
PROHIBITED_CLAIMS = (
    re.compile(r"\bnot\s+yet\s+listed\b", re.I),
    re.compile(r"\b(?:owner|seller)\s+(?:will|is going to|intends? to)\s+sell\b", re.I),
    re.compile(r"\bwill\s+come\s+to\s+market\b", re.I),
    re.compile(r"\bconfirmed\s+off[- ]market\b", re.I),
)


class TodayFeedValidationError(ValueError):
    """Raised when the Today publication contract is violated."""


def read_today_feed(path: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    text = Path(path).read_text(encoding="utf-8")
    feed = parse_window_assignment(text, TODAY_FEED_NAME, {})
    metadata = parse_window_assignment(text, TODAY_META_NAME, {})
    if not isinstance(feed, dict) or not isinstance(metadata, dict):
        raise TodayFeedValidationError("Today feed and metadata assignments must be objects")
    return feed, metadata


def walk(value: Any, path: str = "$"):
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from walk(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, f"{path}[{index}]")


def validate_today_feed(
    feed: Mapping[str, Any],
    metadata: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> None:
    try:
        _validate_json_schema(dict(feed), dict(schema), dict(schema), "$")
    except ValueError as error:
        raise TodayFeedValidationError(str(error)) from error

    if feed.get("schemaVersion") != SCHEMA_VERSION or metadata.get("schemaVersion") != SCHEMA_VERSION:
        raise TodayFeedValidationError(
            f"Today feed and metadata schemaVersion must both be {SCHEMA_VERSION}"
        )
    if metadata.get("asOf") != feed.get("asOf"):
        raise TodayFeedValidationError("Today feed and metadata asOf values differ")
    if metadata.get("generatorVersion") != GENERATOR_VERSION:
        raise TodayFeedValidationError(
            f"Today generatorVersion must be {GENERATOR_VERSION}"
        )
    criteria = metadata.get("criteria")
    if (
        not isinstance(criteria, dict)
        or criteria.get("newsRowsExcluded") is not True
        or criteria.get("everyQualifyingSignalCreatesPropertyOpportunity") is not True
        or criteria.get("opportunityGrouping") != "one-per-property"
        or criteria.get("hotMinimumIndependentSourceFamilies") != 2
        or "newsMinimumScore" in criteria
        or "opportunityRequiresIndependentPropertySignals" in criteria
        or "opportunityRequiresIndependentSource" in criteria
    ):
        raise TodayFeedValidationError(
            "Today metadata must exclude news and declare the one-opportunity-per-property Standard/Hot contract"
        )
    source_fingerprints = metadata.get("sourceFingerprints")
    source_generated_at = metadata.get("sourceGeneratedAt")
    if (
        not isinstance(source_fingerprints, dict)
        or set(source_fingerprints) != {"propertyRecords"}
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            str(source_fingerprints.get("propertyRecords") or "").lower(),
        )
        or not isinstance(source_generated_at, dict)
        or set(source_generated_at) != {"propertyRecords"}
        or not iso_datetime(source_generated_at.get("propertyRecords"))
    ):
        raise TodayFeedValidationError(
            "Today provenance must contain property-record evidence only"
        )

    ids: set[str] = set()
    items_by_lane: dict[str, list[dict[str, Any]]] = {}
    allowed_kinds = {
        "signals": SIGNAL_KINDS,
        "opportunities": OPPORTUNITY_KINDS,
    }
    for lane in LANE_ORDER:
        items = feed.get(lane)
        if not isinstance(items, list):
            raise TodayFeedValidationError(f"Today lane {lane} must be an array")
        items_by_lane[lane] = items
        previous_key = None
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise TodayFeedValidationError(f"{lane}[{index}] must be an object")
            identifier = str(item.get("id") or "")
            if not identifier or identifier in ids:
                raise TodayFeedValidationError("Today item ids must be present and globally unique")
            ids.add(identifier)
            if item.get("lane") != lane or item.get("kind") not in allowed_kinds[lane]:
                raise TodayFeedValidationError(f"{identifier}: lane or kind is outside the canonical enum")
            if not item.get("evidence") or not item.get("limitations"):
                raise TodayFeedValidationError(f"{identifier}: evidence and limitations are required")
            expected_source_ids = sorted({
                str(reference.get("sourceId") or "")
                for reference in item["evidence"]
                if reference.get("sourceId")
            })
            expected_evidence_ids = sorted({
                str(reference.get("evidenceId") or "")
                for reference in item["evidence"]
                if reference.get("evidenceId")
            })
            if item.get("sourceIds") != expected_source_ids:
                raise TodayFeedValidationError(f"{identifier}: sourceIds do not reconcile to evidence")
            if item.get("evidenceIds") != expected_evidence_ids:
                raise TodayFeedValidationError(f"{identifier}: evidenceIds do not reconcile to evidence")
            property_ids = item.get("propertyIds")
            if not isinstance(property_ids, list) or property_ids != sorted(set(property_ids)):
                raise TodayFeedValidationError(f"{identifier}: propertyIds must be sorted and unique")
            prop = item.get("property")
            if not isinstance(prop, dict) or prop.get("propertyId") not in property_ids:
                raise TodayFeedValidationError(f"{identifier}: primary property navigation id is inconsistent")
            place = item.get("place")
            if (
                not isinstance(place, dict)
                or place.get("type") != "property"
                or place.get("id") != prop.get("propertyId")
                or place.get("name") != prop.get("address")
            ):
                raise TodayFeedValidationError(f"{identifier}: property place navigation is inconsistent")
            current_key = (
                -int(item.get("rank") or 0),
                str(item.get("effectiveDate") or ""),
                identifier,
            )
            if previous_key is not None and current_key[0] < previous_key[0]:
                raise TodayFeedValidationError(f"{lane}: items are not deterministically rank-sorted")
            previous_key = current_key
            text = " ".join(
                str(item.get(key) or "") for key in ("title", "fact", "why", "context")
            )
            if any(pattern.search(text) for pattern in PROHIBITED_CLAIMS):
                raise TodayFeedValidationError(f"{identifier}: contains a prohibited seller/listing claim")
            if (
                item.get("kind") == "property_planning"
                and item.get("datePrecision") == "year"
                and "not represented as newly submitted today" not in str(item.get("why") or "").lower()
            ):
                raise TodayFeedValidationError(
                    f"{identifier}: year-only planning must disclose that it is not a new-today claim"
                )
            attributes = item.get("attributes") or {}
            evidence = item.get("evidence") or []
            if (
                item.get("kind") == "entity_news"
                or item.get("sourceFamily") == "news"
                or (item.get("coverage") or {}).get("sourceKey") == "news"
                or attributes.get("newsId")
                or "news" in (attributes.get("corroborationSourceFamilies") or [])
                or any(
                    str(reference.get(field) or "").startswith("news-")
                    for reference in evidence
                    if isinstance(reference, dict)
                    for field in ("evidenceId", "sourceId")
                )
            ):
                raise TodayFeedValidationError(
                    f"{identifier}: news-derived rows and news corroboration are prohibited"
                )

    for path, value in walk({"feed": feed, "metadata": metadata}):
        if isinstance(value, dict):
            prohibited = USER_STATE_FIELDS.intersection(value)
            if prohibited:
                raise TodayFeedValidationError(
                    f"{path}: user workflow state is prohibited: {sorted(prohibited)}"
                )

    signals = {item["id"]: item for item in items_by_lane["signals"]}
    signals_by_property: dict[str, list[dict[str, Any]]] = {}
    planning_observations: set[tuple[str, str]] = set()
    for signal in items_by_lane["signals"]:
        property_id = str(signal.get("property", {}).get("propertyId") or "")
        signals_by_property.setdefault(property_id, []).append(signal)
        if signal.get("kind") != "property_planning":
            continue
        attributes = signal.get("attributes") or {}
        record_type = planning_record_type(
            attributes.get("status"),
            attributes.get("decision"),
        )
        if (
            attributes.get("planningRecordType") != record_type
            or not str(signal.get("title") or "").startswith(
                f"Planning {record_type}"
            )
            or not str(signal.get("fact") or "").startswith(
                f"Planning {record_type}"
            )
        ):
            raise TodayFeedValidationError(
                f"{signal['id']}: planning application/approval wording is not truthful"
            )
        observation = (
            normalise(signal.get("source")),
            normalise(attributes.get("reference")),
        )
        if not all(observation) or observation in planning_observations:
            raise TodayFeedValidationError(
                f"{signal['id']}: planning authority/reference observations must be globally unique"
            )
        planning_observations.add(observation)

    opportunity_properties: set[str] = set()
    for opportunity in items_by_lane["opportunities"]:
        identifier = opportunity["id"]
        direct = signals.get(opportunity.get("directSignalId"))
        if not direct:
            raise TodayFeedValidationError(f"{identifier}: directSignalId does not resolve")
        corroboration_ids = opportunity.get("corroborationIds") or []
        if opportunity.get("directSignalId") in corroboration_ids:
            raise TodayFeedValidationError(
                f"{identifier}: directSignalId cannot also be a corroborationId"
            )
        corroboration = [signals.get(item_id) for item_id in corroboration_ids]
        if any(item is None for item in corroboration):
            raise TodayFeedValidationError(f"{identifier}: corroborationIds do not all resolve")
        property_id = opportunity.get("property", {}).get("propertyId")
        if property_id in opportunity_properties:
            raise TodayFeedValidationError(
                f"{identifier}: each property may have at most one opportunity"
            )
        opportunity_properties.add(property_id)
        if property_id != direct.get("property", {}).get("propertyId"):
            raise TodayFeedValidationError(f"{identifier}: opportunity and direct signal property differ")
        if any(
            property_id != item.get("property", {}).get("propertyId")
            for item in corroboration
            if item
        ):
            raise TodayFeedValidationError(f"{identifier}: corroboration is not linked to the property")
        property_signals = sorted(signals_by_property.get(property_id, []), key=item_sort_key)
        expected_direct = property_signals[0] if property_signals else None
        expected_corroboration_ids = [
            item["id"] for item in property_signals[1:]
        ]
        if direct is not expected_direct or corroboration_ids != expected_corroboration_ids:
            raise TodayFeedValidationError(
                f"{identifier}: opportunity must contain every property signal in deterministic order"
            )
        source_families = sorted({
            str(item.get("sourceFamily") or "")
            for item in property_signals
            if item.get("sourceFamily")
        })
        signal_kinds = sorted({
            str(item.get("kind") or "")
            for item in property_signals
            if item.get("kind")
        })
        expected_level = "Hot" if len(source_families) >= 2 else "Standard"
        if (
            opportunity.get("independentSourceCount") != len(source_families)
            or opportunity.get("indicatorKindCount") != len(signal_kinds)
            or opportunity.get("opportunityLevel") != expected_level
            or not str(opportunity.get("title") or "").startswith(
                f"{expected_level} opportunity"
            )
        ):
            raise TodayFeedValidationError(
                f"{identifier}: Standard/Hot indicator counts are inconsistent"
            )
        attributes = opportunity.get("attributes") or {}
        if (
            attributes.get("directSignalKind") != direct.get("kind")
            or attributes.get("directSourceFamily") != direct.get("sourceFamily")
            or attributes.get("signalKinds") != signal_kinds
            or attributes.get("sourceFamilies") != source_families
            or attributes.get("corroborationKinds") != sorted({
                item.get("kind") for item in corroboration if item
            })
            or attributes.get("corroborationSourceFamilies") != sorted({
                item.get("sourceFamily") for item in corroboration if item
            })
        ):
            raise TodayFeedValidationError(
                f"{identifier}: opportunity indicator attributes do not reconcile"
            )
        if opportunity.get("corroborationCoverage") != [
            item.get("coverage") or {} for item in corroboration if item
        ]:
            raise TodayFeedValidationError(
                f"{identifier}: corroborationCoverage does not reconcile"
            )
        expected_evidence_ids = sorted({
            evidence_id
            for signal in property_signals
            for evidence_id in signal.get("evidenceIds", [])
        })
        if opportunity.get("evidenceIds") != expected_evidence_ids:
            raise TodayFeedValidationError(
                f"{identifier}: opportunity evidence does not cover every property signal"
            )

    if opportunity_properties != set(signals_by_property):
        raise TodayFeedValidationError(
            "Every signal-bearing property must have exactly one opportunity"
        )

    counts = {lane: len(items_by_lane[lane]) for lane in LANE_ORDER}
    if metadata.get("counts") != counts:
        raise TodayFeedValidationError("Today metadata counts do not reconcile to lane arrays")
    expected_summary = [
        {"id": lane, "label": SUMMARY_LABELS[lane], "count": counts[lane]}
        for lane in LANE_ORDER
    ]
    if metadata.get("summary") != expected_summary:
        raise TodayFeedValidationError("Today metadata summary labels, order, or counts are incorrect")
    expected_fingerprint = hashlib.sha256(canonical_json(dict(feed)).encode("utf-8")).hexdigest()
    if metadata.get("datasetFingerprint") != expected_fingerprint:
        raise TodayFeedValidationError("Today datasetFingerprint does not match the canonical feed")


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("feed", nargs="?", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    try:
        feed, metadata = read_today_feed(args.feed)
        schema = json.loads(args.schema.read_text(encoding="utf-8"))
        validate_today_feed(feed, metadata, schema)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Today feed validation failed: {error}", file=sys.stderr)
        return 1
    print(
        "OK Today feed "
        f"({len(feed['signals']):,} signals, "
        f"{len(feed['opportunities']):,} opportunities)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
