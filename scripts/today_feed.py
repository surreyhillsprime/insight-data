#!/usr/bin/env python3
"""Build INSIGHT's deterministic, read-only Today evidence feed.

The feed deliberately separates property-level signals, place-level changes,
and corroborated opportunities.  An opportunity is only created when a direct
property signal is joined to an independently sourced place change.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit


SCHEMA_VERSION = 1
GENERATOR_VERSION = "today-feed-1"
TODAY_FEED_NAME = "INSIGHT_TODAY_FEED"
TODAY_META_NAME = "INSIGHT_TODAY_META"
LANE_ORDER = ("signals", "opportunities", "placeChanges")
SUMMARY_LABELS = {
    "signals": "New signals",
    "opportunities": "Opportunities",
    "placeChanges": "Place changes",
}
SIGNAL_KINDS = {"epc_observation", "property_planning", "sale_age_milestone"}
PLACE_CHANGE_KINDS = {"nearby_planning", "entity_news"}
OPPORTUNITY_KINDS = {"corroborated_property_opportunity"}
MINIMUM_MARKET_HOLDING_GAPS = 20
DATE_RE = re.compile(r"^(?:19|20)\d{2}(?:-\d{2}(?:-\d{2})?)?$")
MATERIAL_PLANNING_RE = re.compile(
    r"\b(?:"
    r"major|demolit(?:ion|ish)|redevelop|develop(?:ment)?|new\s+(?:build|dwelling|house|home)|"
    r"\d+\s+(?:new\s+)?(?:dwelling|house|home)s?|residential|change\s+of\s+use|conversion|"
    r"subdivi(?:de|sion)|basement|additional\s+storey|two[- ]storey|block\s+of\s+flats|"
    r"swimming\s+pool|tennis\s+court"
    r")\b",
    re.I,
)
NON_MATERIAL_PLANNING_RE = re.compile(
    r"\b(?:"
    r"non[- ]material\s+amendment|discharge\s+of\s+condition|tree\s+(?:work|works|prun|fell)|"
    r"certificate\s+of\s+lawful|advert(?:isement|ising)|telecom|prior\s+approval"
    r")\b",
    re.I,
)
MATERIAL_NEWS_TOPICS = {
    "Planning",
    "Transaction",
    "Infrastructure",
    "Policy",
    "Heritage",
    "Environment",
}
ENTITY_NEWS_MATCH_TYPES = {"property", "estate", "town", "district"}
SOURCE_FAMILY_BY_SIGNAL_KIND = {
    "epc_observation": "epc",
    "property_planning": "planning",
    "sale_age_milestone": "land_registry",
}
SOURCE_FAMILY_BY_PLACE_KIND = {
    "nearby_planning": "planning",
    "entity_news": "news",
}
COMMON_LIMITATION = (
    "INSIGHT does not infer seller intent, marketing activity, whether a property is marketed, "
    "or a future sale from this evidence."
)


def clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split()).strip()


def normalise(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", clean(value).upper()).strip()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def stable_id(prefix: str, *parts: Any) -> str:
    payload = "|".join(canonical_json(part) if isinstance(part, (dict, list)) else clean(part) for part in parts)
    return f"{prefix}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def canonical_url(value: Any) -> str:
    try:
        parts = urlsplit(clean(value))
    except ValueError:
        return ""
    if parts.scheme != "https" or not parts.netloc:
        return ""
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/") or "/"
    return urlunsplit(("https", parts.netloc.lower(), path, parts.query, ""))


def iso_datetime(value: Any) -> datetime | None:
    text = clean(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_date(value: Any) -> date | None:
    text = clean(value)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def date_precision(value: Any, declared: Any = "") -> str:
    precision = clean(declared).lower()
    if precision in {"day", "month", "year", "unknown"}:
        return precision
    text = clean(value)
    if re.fullmatch(r"(?:19|20)\d{2}-\d{2}-\d{2}", text):
        return "day"
    if re.fullmatch(r"(?:19|20)\d{2}-\d{2}", text):
        return "month"
    if re.fullmatch(r"(?:19|20)\d{2}", text):
        return "year"
    return "unknown"


def date_sort_key(value: Any, precision: Any = "") -> date:
    text = clean(value)
    declared = date_precision(text, precision)
    if declared == "day":
        return iso_date(text) or date.min
    if declared == "month":
        try:
            year, month = (int(part) for part in text.split("-"))
            next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
            return next_month - timedelta(days=1)
        except (TypeError, ValueError):
            return date.min
    if declared == "year":
        try:
            return date(int(text), 12, 31)
        except ValueError:
            return date.min
    return date.min


def signal_date_is_current(value: Any, precision: Any, as_of: date, lookback_days: int) -> bool:
    declared = date_precision(value, precision)
    if declared == "day":
        observed = iso_date(value)
        return bool(observed and as_of - timedelta(days=lookback_days - 1) <= observed <= as_of)
    if declared == "month":
        end = date_sort_key(value, declared)
        return end != date.min and as_of - timedelta(days=lookback_days - 1) <= end <= as_of
    if declared == "year":
        return clean(value) == str(as_of.year)
    return False


def unique_strings(values: Iterable[Any]) -> list[str]:
    return sorted({clean(value) for value in values if clean(value)})


def unique_limitations(*collections: Iterable[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for collection in collections:
        for value in collection or []:
            text = clean(value)
            if text and text not in seen:
                seen.add(text)
                output.append(text)
    return output


def property_ref(record: Mapping[str, Any]) -> dict[str, Any]:
    profile = record.get("profile") if isinstance(record.get("profile"), Mapping) else {}
    context = record.get("context") if isinstance(record.get("context"), Mapping) else {}
    output: dict[str, Any] = {
        "propertyId": clean(record.get("propertyId")),
        "address": clean(record.get("canonicalAddress")),
        "postcode": clean(record.get("postcode")),
        "market": clean(profile.get("market")),
        "district": clean(profile.get("district")),
        "town": clean(profile.get("town")),
        "estate": clean(profile.get("estate")),
        "estateId": clean(profile.get("estateId")),
    }
    latitude = context.get("latitude")
    longitude = context.get("longitude")
    if isinstance(latitude, (int, float)) and math.isfinite(latitude):
        output["latitude"] = round(float(latitude), 7)
    if isinstance(longitude, (int, float)) and math.isfinite(longitude):
        output["longitude"] = round(float(longitude), 7)
    precision = clean(
        context.get("coordinatePrecision")
        or (context.get("geocode") or {}).get("precision")
    )
    if precision:
        output["coordinatePrecision"] = precision
    return output


def evidence_map(record: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        clean(item.get("evidenceId")): item
        for item in record.get("evidence", [])
        if isinstance(item, Mapping) and clean(item.get("evidenceId"))
    }


def evidence_refs(record: Mapping[str, Any], event: Mapping[str, Any]) -> list[dict[str, Any]]:
    by_id = evidence_map(record)
    output: list[dict[str, Any]] = []
    for evidence_id in event.get("evidenceIds", []):
        item = by_id.get(clean(evidence_id), {})
        data = item.get("data") if isinstance(item.get("data"), Mapping) else {}
        effective_date = clean(item.get("effectiveDate") or data.get("date") or event.get("date"))
        precision = date_precision(effective_date, data.get("datePrecision") or event.get("datePrecision"))
        reference = {
            "evidenceId": clean(evidence_id),
            "source": clean(item.get("source") or event.get("source")),
            "sourceId": clean(item.get("sourceId") or data.get("reference") or event.get("eventId")),
            "effectiveDate": effective_date,
            "datePrecision": precision,
        }
        url = canonical_url(data.get("portalUrl") or data.get("url"))
        if url:
            reference["url"] = url
        output.append(reference)
    return sorted(output, key=lambda item: (item["evidenceId"], item["sourceId"]))


def coverage_ref(record: Mapping[str, Any], source_key: str) -> dict[str, Any]:
    coverage = record.get("coverage") if isinstance(record.get("coverage"), Mapping) else {}
    item = coverage.get(source_key) if isinstance(coverage.get(source_key), Mapping) else {}
    return {
        "sourceKey": source_key,
        "status": clean(item.get("status")) or "unavailable",
        "complete": item.get("complete") is True,
        "coverageMode": clean(item.get("coverageMode")) or "not-established",
        "basis": clean(item.get("basis")) or "No completed source coverage statement is available.",
        "checkedAt": clean(item.get("checkedAt")),
        "limitations": unique_limitations(item.get("limitations", [])),
    }


def evidence_data_for_event(record: Mapping[str, Any], event: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    by_id = evidence_map(record)
    output = []
    for evidence_id in event.get("evidenceIds", []):
        evidence = by_id.get(clean(evidence_id))
        data = evidence.get("data") if isinstance(evidence, Mapping) else None
        if isinstance(data, Mapping):
            output.append(data)
    return output


def item_sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        -int(item.get("rank") or 0),
        -date_sort_key(item.get("effectiveDate"), item.get("datePrecision")).toordinal(),
        clean(item.get("id")),
    )


def latest_event(record: Mapping[str, Any], event_type: str) -> Mapping[str, Any] | None:
    events = [
        item
        for item in record.get("events", [])
        if isinstance(item, Mapping) and item.get("type") == event_type
    ]
    return max(
        events,
        key=lambda item: (
            date_sort_key(item.get("date"), item.get("datePrecision")),
            clean(item.get("eventId")),
        ),
        default=None,
    )


def base_property_item(
    *,
    item_id: str,
    lane: str,
    kind: str,
    rank: int,
    record: Mapping[str, Any],
    title: str,
    fact: str,
    why: str,
    context: str,
    effective_date: str,
    precision: str,
    confidence: str,
    source: str,
    evidence: list[dict[str, Any]],
    coverage: dict[str, Any],
    limitations: Iterable[Any],
    attributes: Mapping[str, Any],
) -> dict[str, Any]:
    prop = property_ref(record)
    source_ids = unique_strings(item.get("sourceId") for item in evidence)
    evidence_ids = unique_strings(item.get("evidenceId") for item in evidence)
    return {
        "id": item_id,
        "lane": lane,
        "kind": kind,
        "rank": max(0, min(100, int(rank))),
        "title": title,
        "summary": fact,
        "fact": fact,
        "why": why,
        "context": context,
        "effectiveDate": effective_date,
        "datePrecision": precision,
        "confidence": confidence,
        "source": source,
        "sourceIds": source_ids,
        "evidenceIds": evidence_ids,
        "sourceFamily": SOURCE_FAMILY_BY_SIGNAL_KIND.get(kind, "insight"),
        "evidence": evidence,
        "coverage": coverage,
        "limitations": unique_limitations(limitations, [COMMON_LIMITATION]),
        "property": prop,
        "propertyIds": [prop["propertyId"]],
        "place": {
            "type": "property",
            "id": prop["propertyId"],
            "name": prop["address"],
        },
        "attributes": dict(attributes),
    }


def source_observation_properties(
    records: Mapping[str, Mapping[str, Any]],
    event_type: str,
) -> dict[tuple[str, str], set[str]]:
    properties_by_observation: dict[tuple[str, str], set[str]] = defaultdict(set)
    for property_id in sorted(records):
        record = records[property_id]
        for event in record.get("events", []):
            if not isinstance(event, Mapping) or event.get("type") != event_type:
                continue
            for reference in evidence_refs(record, event):
                source_id = clean(reference.get("sourceId"))
                source = normalise(reference.get("source"))
                if source_id:
                    properties_by_observation[(source, source_id)].add(property_id)
    return properties_by_observation


def epc_signals(
    records: Mapping[str, Mapping[str, Any]],
    as_of: date,
    lookback_days: int,
) -> list[dict[str, Any]]:
    properties_by_observation = source_observation_properties(records, "epc_certificate")
    output = []
    for property_id in sorted(records):
        record = records[property_id]
        event = latest_event(record, "epc_certificate")
        if not event or not signal_date_is_current(
            event.get("date"), event.get("datePrecision"), as_of, lookback_days
        ):
            continue
        observed = iso_date(event.get("date"))
        age_days = (as_of - observed).days if observed else lookback_days
        coverage = coverage_ref(record, "epc")
        references = [
            reference
            for reference in evidence_refs(record, event)
            if len(properties_by_observation.get(
                (
                    normalise(reference.get("source")),
                    clean(reference.get("sourceId")),
                ),
                set(),
            )) == 1
        ]
        if not references:
            continue
        data = evidence_data_for_event(record, event)
        attributes: dict[str, Any] = {"ageDays": age_days, "lookbackDays": lookback_days}
        if data:
            for source, target in (
                ("rating", "rating"),
                ("floorAreaSqft", "floorAreaSqft"),
                ("floorAreaSqm", "floorAreaSqm"),
            ):
                value = data[0].get(source)
                if value not in (None, ""):
                    if target == "floorAreaSqm" and isinstance(value, float) and value.is_integer():
                        # NSJSONSerialization emits whole-valued JSON numbers as
                        # integers. Publish the same representation so the native
                        # cache retains the canonical dataset fingerprint.
                        value = int(value)
                    attributes[target] = value
        output.append(
            base_property_item(
                item_id=stable_id("today-signal", "epc", event.get("eventId")),
                lane="signals",
                kind="epc_observation",
                rank=max(55, 92 - age_days // 5),
                record=record,
                title=f"EPC observation · {property_ref(record)['address']}",
                fact=clean(event.get("summary")) or f"EPC certificate dated {event.get('date')}",
                why=f"The property-level EPC observation is dated within the last {lookback_days} days.",
                context=(
                    "An EPC is a dated administrative observation on this property file. "
                    "It does not establish why the certificate was produced."
                ),
                effective_date=clean(event.get("date")),
                precision=date_precision(event.get("date"), event.get("datePrecision")),
                confidence="medium",
                source=clean(event.get("source")) or "MHCLG EPC Register",
                evidence=references,
                coverage=coverage,
                limitations=coverage["limitations"],
                attributes=attributes,
            )
        )
    return output


def exact_property_planning_evidence(
    record: Mapping[str, Any],
    event: Mapping[str, Any],
) -> tuple[Mapping[str, Any], dict[str, Any]] | None:
    by_id = evidence_map(record)
    for evidence_id in event.get("evidenceIds", []):
        evidence = by_id.get(clean(evidence_id))
        data = evidence.get("data") if isinstance(evidence, Mapping) else None
        if not isinstance(data, Mapping):
            continue
        try:
            confidence = float(data.get("matchConfidence") or 0)
        except (TypeError, ValueError):
            confidence = 0
        if confidence < 0.9 or not clean(data.get("reference")) or not clean(data.get("siteAddress")):
            continue
        return evidence, {
            "reference": clean(data.get("reference")),
            "status": clean(data.get("status")),
            "proposal": clean(data.get("proposal") or event.get("summary")),
            "siteAddress": clean(data.get("siteAddress")),
            "matchConfidence": round(confidence, 3),
        }
    return None


def planning_signals(
    records: Mapping[str, Mapping[str, Any]],
    as_of: date,
    lookback_days: int,
) -> list[dict[str, Any]]:
    output = []
    for property_id in sorted(records):
        record = records[property_id]
        for event in record.get("events", []):
            if not isinstance(event, Mapping) or event.get("type") != "planning_application":
                continue
            precision = date_precision(event.get("date"), event.get("datePrecision"))
            if precision == "year":
                continue
            if not signal_date_is_current(event.get("date"), precision, as_of, lookback_days):
                continue
            exact = exact_property_planning_evidence(record, event)
            if not exact:
                continue
            _evidence, attributes = exact
            references = evidence_refs(record, event)
            if not references:
                continue
            coverage = coverage_ref(record, "planning")
            why = (
                f"The source links this planning record to the property within the "
                f"{lookback_days}-day signal window."
            )
            output.append(
                base_property_item(
                    item_id=stable_id("today-signal", "planning", event.get("eventId")),
                    lane="signals",
                    kind="property_planning",
                    rank=rank,
                    record=record,
                    title=f"Property planning record · {property_ref(record)['address']}",
                    fact=attributes["proposal"] or clean(event.get("summary")),
                    why=why,
                    context=(
                        "This is an exact-property source match. An application or decision does not "
                        "prove that proposed or permitted work was started or completed."
                    ),
                    effective_date=clean(event.get("date")),
                    precision=precision,
                    confidence="high",
                    source=clean(event.get("source")),
                    evidence=references,
                    coverage=coverage,
                    limitations=coverage["limitations"],
                    attributes={**attributes, "lookbackDays": lookback_days},
                )
            )
    return output


def recorded_sale_dates(
    record: Mapping[str, Any],
    as_of: date,
) -> list[date]:
    return sorted({
        observed
        for event in record.get("events", [])
        if isinstance(event, Mapping) and event.get("type") == "sale"
        for observed in [iso_date(event.get("date"))]
        if observed and observed <= as_of
    })


def sale_age_signals(
    records: Mapping[str, Mapping[str, Any]],
    as_of: date,
    crossing_window_days: int,
) -> list[dict[str, Any]]:
    overall_gaps: list[int] = []
    gaps_by_market: dict[str, list[int]] = defaultdict(list)
    entries: list[tuple[Mapping[str, Any], Mapping[str, Any], date, str]] = []
    for property_id in sorted(records):
        record = records[property_id]
        dates = recorded_sale_dates(record, as_of)
        market = clean((record.get("profile") or {}).get("market"))
        gaps = [
            (later - earlier).days
            for earlier, later in zip(dates, dates[1:])
            if later > earlier
        ]
        overall_gaps.extend(gaps)
        gaps_by_market[market].extend(gaps)
        event = latest_event(record, "sale")
        sold = iso_date(event.get("date")) if event else None
        if not event or not sold or sold > as_of:
            continue
        entries.append((record, event, sold, market))
    if not overall_gaps:
        return []

    overall_average_days = round(sum(overall_gaps) / len(overall_gaps))
    output = []
    for record, event, sold, market in entries:
        market_gaps = gaps_by_market.get(market, [])
        if market and len(market_gaps) >= MINIMUM_MARKET_HOLDING_GAPS:
            cohort_gaps = market_gaps
            cohort_basis = "market"
            cohort_name = market
        else:
            cohort_gaps = overall_gaps
            cohort_basis = "overall"
            cohort_name = "all tracked markets"
        average_days = round(sum(cohort_gaps) / len(cohort_gaps))
        crossing_date = sold + timedelta(days=average_days)
        days_since_crossing = (as_of - crossing_date).days
        if not 0 <= days_since_crossing < crossing_window_days:
            continue
        references = evidence_refs(record, event)
        if not references:
            continue
        coverage = coverage_ref(record, "sales")
        average_years = average_days / 365.2425
        cohort_label = (
            f"the {cohort_name} market cohort"
            if cohort_basis == "market"
            else "all tracked markets"
        )
        output.append(
            base_property_item(
                item_id=stable_id(
                    "today-signal",
                    "sale-age",
                    record.get("propertyId"),
                    sold.isoformat(),
                    crossing_date.isoformat(),
                    cohort_basis,
                    cohort_name,
                ),
                lane="signals",
                kind="sale_age_milestone",
                rank=max(55, 78 - days_since_crossing),
                record=record,
                title=f"Recorded holding-interval crossing · {property_ref(record)['address']}",
                fact=(
                    f"The latest matched Price Paid sale is dated {sold.isoformat()}. "
                    f"Its elapsed interval crossed the tracked {average_years:.1f}-year "
                    f"average for {cohort_label} on {crossing_date.isoformat()}."
                ),
                why=(
                    f"The crossing occurred {days_since_crossing} day"
                    f"{'s' if days_since_crossing != 1 else ''} ago, within the "
                    f"{crossing_window_days}-day alert window. The benchmark uses "
                    f"{len(cohort_gaps):,} consecutive recorded Price Paid sale gaps."
                ),
                context=(
                    "This is a benchmark against recorded Price Paid transaction gaps, not proof "
                    "of continuous legal ownership. Crossing the average does not predict a future transaction."
                ),
                effective_date=crossing_date.isoformat(),
                precision="day",
                confidence="high",
                source=(
                    f"{clean(event.get('source')) or 'HM Land Registry Price Paid Data'} · "
                    "INSIGHT recorded-gap calculation"
                ),
                evidence=references,
                coverage=coverage,
                limitations=coverage["limitations"],
                attributes={
                    "latestSaleDate": sold.isoformat(),
                    "crossingDate": crossing_date.isoformat(),
                    "daysSinceCrossing": days_since_crossing,
                    "crossingWindowDays": crossing_window_days,
                    "averageRecordedHoldingDays": average_days,
                    "averageRecordedHoldingYears": round(average_years, 2),
                    "holdingIntervalCohortBasis": cohort_basis,
                    "holdingIntervalCohort": cohort_name,
                    "holdingIntervalSampleSize": len(cohort_gaps),
                    "minimumMarketSampleSize": MINIMUM_MARKET_HOLDING_GAPS,
                    "overallAverageRecordedHoldingDays": overall_average_days,
                },
            )
        )
    return output


def material_planning_application(application: Mapping[str, Any]) -> bool:
    text = " ".join(
        clean(application.get(key))
        for key in ("name", "description", "proposal", "address", "status", "decision")
    )
    if not text or NON_MATERIAL_PLANNING_RE.search(text):
        return False
    return bool(MATERIAL_PLANNING_RE.search(text))


def nearby_planning_changes(
    records: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for property_id in sorted(records):
        record = records[property_id]
        context = record.get("context") if isinstance(record.get("context"), Mapping) else {}
        nearby = context.get("nearbyPlanning") if isinstance(context.get("nearbyPlanning"), Mapping) else {}
        applications = nearby.get("recentApplications")
        if not isinstance(applications, list):
            continue
        for application in applications:
            if not isinstance(application, Mapping) or not material_planning_application(application):
                continue
            reference = normalise(application.get("reference"))
            fallback = "|".join(
                normalise(application.get(key))
                for key in ("name", "address", "date")
            )
            key = reference or fallback
            if not key:
                continue
            group = grouped.setdefault(
                key,
                {
                    "application": dict(application),
                    "source": clean(nearby.get("source")) or "Planning Data API",
                    "updatedAt": clean(nearby.get("updatedAt")),
                    "searchRadii": set(),
                    "propertyIds": set(),
                    "coverageEvidenceIds": set(),
                    "minimumMetres": None,
                    "coverageModes": set(),
                },
            )
            group["propertyIds"].add(property_id)
            group["searchRadii"].add(clean(nearby.get("searchRadius")))
            group["coverageModes"].add(clean(nearby.get("coverageMode")))
            coverage = record.get("coverage") if isinstance(record.get("coverage"), Mapping) else {}
            current = coverage.get("currentPlanning") if isinstance(coverage.get("currentPlanning"), Mapping) else {}
            group["coverageEvidenceIds"].update(current.get("evidenceIds", []))
            metres = application.get("metres")
            if isinstance(metres, (int, float)) and math.isfinite(metres):
                group["minimumMetres"] = (
                    float(metres)
                    if group["minimumMetres"] is None
                    else min(group["minimumMetres"], float(metres))
                )

    output = []
    for key in sorted(grouped):
        group = grouped[key]
        application = group["application"]
        effective_date = clean(application.get("date"))
        precision = date_precision(effective_date)
        reference = clean(application.get("reference"))
        source_id = reference or stable_id("planning-source", key)
        url = canonical_url(application.get("url"))
        evidence = [{
            "evidenceId": stable_id("source-observation", "nearby-planning", key),
            "source": group["source"],
            "sourceId": source_id,
            "effectiveDate": effective_date,
            "datePrecision": precision,
            **({"url": url} if url else {}),
        }]
        property_ids = sorted(group["propertyIds"])
        name = clean(application.get("name")) or clean(application.get("address")) or "Material nearby planning"
        location = clean(application.get("address")) or name
        coverage_mode = (
            sorted(value for value in group["coverageModes"] if value)[0]
            if any(group["coverageModes"])
            else "positive-results-only"
        )
        minimum_metres = group["minimumMetres"]
        output.append({
            "id": stable_id("today-place", "nearby-planning", key),
            "lane": "placeChanges",
            "kind": "nearby_planning",
            "rank": 78 if minimum_metres is not None and minimum_metres <= 500 else 68,
            "title": f"Material nearby planning · {location}",
            "summary": name,
            "fact": name,
            "why": (
                f"The application appears within the configured nearby-planning radius of "
                f"{len(property_ids):,} tracked property file{'s' if len(property_ids) != 1 else ''}."
            ),
            "context": (
                "This is a nearby spatial result, not an exact-property planning match. "
                "An application or decision does not prove that work was started or completed."
            ),
            "effectiveDate": effective_date,
            "datePrecision": precision,
            "confidence": "medium",
            "source": group["source"],
            "sourceIds": unique_strings(item.get("sourceId") for item in evidence),
            "evidenceIds": unique_strings(item.get("evidenceId") for item in evidence),
            "sourceFamily": SOURCE_FAMILY_BY_PLACE_KIND["nearby_planning"],
            "evidence": evidence,
            "coverage": {
                "sourceKey": "currentPlanning",
                "status": "complete",
                "complete": True,
                "coverageMode": coverage_mode,
                "basis": "One or more positive nearby spatial results were retained.",
                "checkedAt": group["updatedAt"],
                "limitations": [
                    "Planning Data coverage varies by authority; this feed cannot establish complete negative coverage.",
                    "Distance is measured from the available property mapping point and may use a postcode centroid.",
                ],
            },
            "limitations": [
                "This is nearby place evidence and is not attributed to the tracked property itself.",
                COMMON_LIMITATION,
            ],
            "propertyIds": property_ids,
            "place": {
                "type": "planning_application",
                "id": source_id,
                "name": location,
            },
            "attributes": {
                "reference": reference,
                "status": clean(application.get("status")),
                "decision": clean(application.get("decision")),
                "address": clean(application.get("address")),
                "minimumDistanceMetres": round(minimum_metres) if minimum_metres is not None else None,
                "affectedPropertyCount": len(property_ids),
                "searchRadii": unique_strings(group["searchRadii"]),
            },
        })
    return output


def news_property_ids(
    news_item: Mapping[str, Any],
    records: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    match_type = clean(news_item.get("matchType")).lower()
    location = normalise(news_item.get("location"))
    if match_type not in ENTITY_NEWS_MATCH_TYPES or not location:
        return []
    output = []
    for property_id in sorted(records):
        record = records[property_id]
        profile = record.get("profile") if isinstance(record.get("profile"), Mapping) else {}
        if match_type == "property":
            matched = normalise(record.get("canonicalAddress")).startswith(location)
        elif match_type == "estate":
            estate = normalise(profile.get("estate"))
            matched = bool(estate and (estate == location or location in estate or estate in location))
        elif match_type == "town":
            matched = normalise(profile.get("town")) == location
        else:
            matched = normalise(profile.get("district")) == location
        if matched:
            output.append(property_id)
    return output


def entity_news_changes(
    records: Mapping[str, Mapping[str, Any]],
    news_items: Iterable[Mapping[str, Any]],
    minimum_score: int,
) -> list[dict[str, Any]]:
    selected: dict[str, Mapping[str, Any]] = {}
    for item in news_items:
        if not isinstance(item, Mapping):
            continue
        try:
            score = int(item.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        topics = {clean(topic) for topic in item.get("topics", [])}
        if (
            score < minimum_score
            or clean(item.get("matchType")).lower() not in ENTITY_NEWS_MATCH_TYPES
            or not topics.intersection(MATERIAL_NEWS_TOPICS)
            or clean(item.get("rightsMode")) != "link-only"
        ):
            continue
        url = canonical_url(item.get("url"))
        key = url or normalise(item.get("title"))
        if not key:
            continue
        prior = selected.get(key)
        if not prior or int(prior.get("score") or 0) < score:
            selected[key] = item

    output = []
    for key, item in sorted(selected.items()):
        property_ids = news_property_ids(item, records)
        if not property_ids:
            continue
        match_type = clean(item.get("matchType")).lower()
        location = clean(item.get("location"))
        published_at = clean(item.get("publishedAt"))
        effective_date = published_at[:10] if iso_datetime(published_at) else ""
        url = canonical_url(item.get("url"))
        source_id = clean(item.get("id")) or stable_id("news-source", key)
        evidence = [{
            "evidenceId": source_id,
            "source": clean(item.get("source")),
            "sourceId": source_id,
            "effectiveDate": effective_date,
            "datePrecision": "day" if effective_date else "unknown",
            **({"url": url} if url else {}),
        }]
        output.append({
            "id": stable_id("today-place", "entity-news", source_id),
            "lane": "placeChanges",
            "kind": "entity_news",
            "rank": max(0, min(100, int(item.get("score") or 0))),
            "title": f"Entity-linked news · {location}",
            "summary": clean(item.get("title")),
            "fact": clean(item.get("title")),
            "why": (
                f"The link-only feed matched this article to {match_type} entity {location}, "
                f"which connects to {len(property_ids):,} tracked property file"
                f"{'s' if len(property_ids) != 1 else ''}."
            ),
            "context": (
                "This is link-only editorial metadata. INSIGHT has not treated the article title "
                "as independently verified property fact."
            ),
            "effectiveDate": effective_date,
            "datePrecision": "day" if effective_date else "unknown",
            "confidence": "medium",
            "source": clean(item.get("source")),
            "sourceIds": unique_strings(reference.get("sourceId") for reference in evidence),
            "evidenceIds": unique_strings(reference.get("evidenceId") for reference in evidence),
            "sourceFamily": SOURCE_FAMILY_BY_PLACE_KIND["entity_news"],
            "evidence": evidence,
            "coverage": {
                "sourceKey": "news",
                "status": "complete",
                "complete": True,
                "coverageMode": "link-only-scored-feed",
                "basis": clean(item.get("reason")) or "The article passed the configured entity and materiality score.",
                "checkedAt": published_at,
                "limitations": [
                    "Only licensed link metadata is retained; article body text is not present in INSIGHT.",
                    "Entity matching is deterministic but can still be broader than an exact property match.",
                ],
            },
            "limitations": [
                "The article title is contextual evidence, not proof of a change at every linked property.",
                COMMON_LIMITATION,
            ],
            "propertyIds": property_ids,
            "place": {
                "type": match_type,
                "id": normalise(location).lower().replace(" ", "-"),
                "name": location,
            },
            "url": url,
            "rightsMode": "link-only",
            "attributes": {
                "newsId": source_id,
                "url": url,
                "rightsMode": "link-only",
                "score": int(item.get("score") or 0),
                "topics": sorted({clean(topic) for topic in item.get("topics", []) if clean(topic)}),
                "matchType": match_type,
                "affectedPropertyCount": len(property_ids),
            },
        })
    return output


def evidence_union(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        for evidence in item.get("evidence", []):
            if not isinstance(evidence, Mapping):
                continue
            key = (clean(evidence.get("evidenceId")), clean(evidence.get("sourceId")))
            indexed[key] = dict(evidence)
    return [indexed[key] for key in sorted(indexed)]


def opportunities_from(
    signals: Iterable[Mapping[str, Any]],
    place_changes: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_property: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for change in place_changes:
        for property_id in change.get("propertyIds", []):
            by_property[clean(property_id)].append(change)
    output = []
    for signal in signals:
        prop = signal.get("property") if isinstance(signal.get("property"), Mapping) else {}
        property_id = clean(prop.get("propertyId"))
        direct_family = clean(signal.get("sourceFamily"))
        corroboration = [
            change
            for change in by_property.get(property_id, [])
            if clean(change.get("sourceFamily")) and clean(change.get("sourceFamily")) != direct_family
        ]
        if not corroboration:
            continue
        corroboration.sort(key=item_sort_key)
        corroboration = corroboration[:3]
        families = sorted({clean(item.get("sourceFamily")) for item in corroboration})
        supporting_families = sorted({direct_family, *families})
        strongest = corroboration[0]
        rank = round(
            int(signal.get("rank") or 0) * 0.6
            + int(strongest.get("rank") or 0) * 0.4
            + (5 if len(families) > 1 else 0)
        )
        evidence = evidence_union([signal, *corroboration])
        why = (
            f"A direct {clean(signal.get('kind')).replace('_', ' ')} is independently "
            f"corroborated by {len(corroboration)} place change"
            f"{'s' if len(corroboration) != 1 else ''} led by “{clean(strongest.get('fact'))}”."
        )
        output.append({
            "id": stable_id(
                "today-opportunity",
                signal.get("id"),
                [item.get("id") for item in corroboration],
            ),
            "lane": "opportunities",
            "kind": "corroborated_property_opportunity",
            "rank": max(0, min(100, rank)),
            "title": f"Corroborated research opportunity · {clean(prop.get('address'))}",
            "summary": why,
            "fact": clean(signal.get("fact")),
            "why": why,
            "context": (
                "This is an evidence-led prompt for further research, not a prediction of a sale, "
                "seller intent, marketing activity, whether a property is marketed, or future value."
            ),
            "effectiveDate": clean(signal.get("effectiveDate")),
            "datePrecision": clean(signal.get("datePrecision")),
            "confidence": "medium",
            "source": "INSIGHT deterministic Today synthesis",
            "sourceIds": unique_strings(item.get("sourceId") for item in evidence),
            "evidenceIds": unique_strings(item.get("evidenceId") for item in evidence),
            "sourceFamily": "insight",
            "evidence": evidence,
            "coverage": dict(signal.get("coverage") or {}),
            "corroborationCoverage": [
                dict(item.get("coverage") or {}) for item in corroboration
            ],
            "limitations": unique_limitations(
                signal.get("limitations", []),
                *(item.get("limitations", []) for item in corroboration),
                [COMMON_LIMITATION],
            ),
            "property": dict(prop),
            "propertyIds": [property_id],
            "place": {
                "type": "property",
                "id": property_id,
                "name": clean(prop.get("address")),
            },
            "directSignalId": clean(signal.get("id")),
            "corroborationIds": [clean(item.get("id")) for item in corroboration],
            "independentSourceCount": len(supporting_families),
            "attributes": {
                "directSignalKind": clean(signal.get("kind")),
                "directSourceFamily": direct_family,
                "corroborationKinds": sorted({clean(item.get("kind")) for item in corroboration}),
                "corroborationSourceFamilies": families,
            },
        })
    return output


def default_clock(
    property_metadata: Mapping[str, Any],
    news_metadata: Mapping[str, Any],
) -> tuple[date, str]:
    timestamps = [
        parsed
        for parsed in (
            iso_datetime(property_metadata.get("generatedAt")),
            iso_datetime(news_metadata.get("generatedAt")),
        )
        if parsed
    ]
    generated = max(timestamps) if timestamps else datetime(1970, 1, 1, tzinfo=timezone.utc)
    generated_at = generated.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return generated.date(), generated_at


def build_today_feed(
    records: Mapping[str, Mapping[str, Any]],
    property_metadata: Mapping[str, Any],
    news_items: Iterable[Mapping[str, Any]],
    news_metadata: Mapping[str, Any],
    *,
    as_of: str | date | None = None,
    generated_at: str | None = None,
    epc_lookback_days: int = 30,
    planning_lookback_days: int = 365,
    sale_age_crossing_window_days: int = 30,
    news_minimum_score: int = 55,
) -> tuple[dict[str, Any], dict[str, Any]]:
    default_as_of, default_generated_at = default_clock(property_metadata, news_metadata)
    if isinstance(as_of, date):
        as_of_date = as_of
    elif as_of:
        as_of_date = iso_date(as_of)
        if not as_of_date:
            raise ValueError("Today as-of date must use YYYY-MM-DD")
    else:
        as_of_date = default_as_of
    generated_value = generated_at or default_generated_at
    if not iso_datetime(generated_value):
        raise ValueError("Today generated-at value must be an ISO-8601 timestamp")
    if (
        epc_lookback_days <= 0
        or planning_lookback_days <= 0
        or sale_age_crossing_window_days <= 0
    ):
        raise ValueError("Today lookback windows must be positive")
    if not 0 <= news_minimum_score <= 100:
        raise ValueError("News minimum score must be between 0 and 100")
    news_records = sorted(
        (dict(item) for item in news_items if isinstance(item, Mapping)),
        key=canonical_json,
    )

    signals = [
        *epc_signals(records, as_of_date, epc_lookback_days),
        *planning_signals(records, as_of_date, planning_lookback_days),
        *sale_age_signals(records, as_of_date, sale_age_crossing_window_days),
    ]
    signals.sort(key=item_sort_key)
    place_changes = [
        *nearby_planning_changes(records),
        *entity_news_changes(records, news_records, news_minimum_score),
    ]
    place_changes.sort(key=item_sort_key)
    opportunities = opportunities_from(signals, place_changes)
    opportunities.sort(key=item_sort_key)

    feed = {
        "schemaVersion": SCHEMA_VERSION,
        "asOf": as_of_date.isoformat(),
        "signals": signals,
        "opportunities": opportunities,
        "placeChanges": place_changes,
    }
    counts = {
        "signals": len(signals),
        "opportunities": len(opportunities),
        "placeChanges": len(place_changes),
    }
    news_fingerprint = hashlib.sha256(
        canonical_json(news_records).encode("utf-8")
    ).hexdigest()
    metadata = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": iso_datetime(generated_value).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "asOf": as_of_date.isoformat(),
        "generatorVersion": GENERATOR_VERSION,
        "sourceFingerprints": {
            "propertyRecords": clean(property_metadata.get("datasetFingerprint")),
            "news": news_fingerprint,
        },
        "sourceGeneratedAt": {
            "propertyRecords": clean(property_metadata.get("generatedAt")),
            "news": clean(news_metadata.get("generatedAt")),
        },
        "criteria": {
            "epcLookbackDays": epc_lookback_days,
            "planningLookbackDays": planning_lookback_days,
            "saleAgeCrossingWindowDays": sale_age_crossing_window_days,
            "newsMinimumScore": news_minimum_score,
            "opportunityRequiresIndependentSource": True,
        },
        "summary": [
            {"id": lane, "label": SUMMARY_LABELS[lane], "count": counts[lane]}
            for lane in LANE_ORDER
        ],
        "counts": counts,
        "datasetFingerprint": hashlib.sha256(canonical_json(feed).encode("utf-8")).hexdigest(),
        "limitations": [
            "Today is a read-only evidence snapshot; it contains no user-managed fields.",
            "An opportunity requires a direct property signal and independently sourced corroboration, but remains a research prompt rather than a prediction.",
            "Year-only source dates remain year-only and are never presented as newly submitted on a specific day.",
            COMMON_LIMITATION,
        ],
    }
    return feed, metadata


def write_today_feed(
    path: str | Path,
    feed: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join([
        f"window.{TODAY_FEED_NAME} = {canonical_json(dict(feed))};",
        f"window.{TODAY_META_NAME} = {canonical_json(dict(metadata))};",
        "",
    ])
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(destination)


__all__ = [
    "GENERATOR_VERSION",
    "LANE_ORDER",
    "OPPORTUNITY_KINDS",
    "PLACE_CHANGE_KINDS",
    "SCHEMA_VERSION",
    "SIGNAL_KINDS",
    "SUMMARY_LABELS",
    "TODAY_FEED_NAME",
    "TODAY_META_NAME",
    "build_today_feed",
    "canonical_json",
    "write_today_feed",
]
