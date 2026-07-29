#!/usr/bin/env python3
"""Build INSIGHT's deterministic, read-only Today evidence feed.

Every signal-bearing property receives one opportunity. A single source family
is Standard; two or more independent families add the Hot flag. Market News
remains a separate context feed and cannot create or strengthen an opportunity.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit


SCHEMA_VERSION = 2
GENERATOR_VERSION = "today-feed-3"
TODAY_FEED_NAME = "INSIGHT_TODAY_FEED"
TODAY_META_NAME = "INSIGHT_TODAY_META"
LANE_ORDER = ("signals", "opportunities")
SUMMARY_LABELS = {
    "signals": "New signals",
    "opportunities": "Opportunities",
}
SIGNAL_KINDS = {"epc_observation", "property_planning", "sale_age_milestone"}
OPPORTUNITY_KINDS = {"property_opportunity"}
MINIMUM_MARKET_HOLDING_GAPS = 20
DATE_RE = re.compile(r"^(?:19|20)\d{2}(?:-\d{2}(?:-\d{2})?)?$")
SOURCE_FAMILY_BY_SIGNAL_KIND = {
    "epc_observation": "epc",
    "property_planning": "planning",
    "sale_age_milestone": "land_registry",
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
            "decision": clean(data.get("decision")),
            "proposal": clean(data.get("proposal") or event.get("summary")),
            "siteAddress": clean(data.get("siteAddress")),
            "matchConfidence": round(confidence, 3),
        }
    return None


def planning_record_type(status: Any, decision: Any = "") -> str:
    """Return approval only for an explicit positive source decision."""
    value = normalise(f"{clean(status)} {clean(decision)}")
    if re.search(
        r"\b(?:NOT\s+APPROV(?:E|ED)|REFUS(?:E|ED|AL)|REJECT(?:ED|ION)?|"
        r"DISMISS(?:ED|AL)?|WITHDRAWN|DECLIN(?:E|ED)|CANCELLED)\b",
        value,
    ):
        return "application"
    if re.search(r"\b(?:APPROVE|APPROVED|GRANT|GRANTED)\b", value):
        return "approval"
    return "application"


def planning_observation_key(signal: Mapping[str, Any]) -> tuple[Any, ...]:
    attributes = signal.get("attributes") if isinstance(signal.get("attributes"), Mapping) else {}
    reference = normalise(attributes.get("reference"))
    authority = normalise(signal.get("source"))
    if reference:
        return ("authority-reference", authority, reference)
    return ("source-identity", *unique_strings(signal.get("sourceIds", [])))


def planning_candidate_sort_key(signal: Mapping[str, Any]) -> tuple[Any, ...]:
    prop = signal.get("property") if isinstance(signal.get("property"), Mapping) else {}
    attributes = signal.get("attributes") if isinstance(signal.get("attributes"), Mapping) else {}
    canonical = normalise(prop.get("address"))
    site = normalise(attributes.get("siteAddress"))
    similarity = SequenceMatcher(None, canonical, site).ratio() if canonical and site else 0.0
    canonical_tokens = canonical.split()
    repeated_token_count = max(0, len(canonical_tokens) - len(set(canonical_tokens)))
    try:
        confidence = float(attributes.get("matchConfidence") or 0)
    except (TypeError, ValueError):
        confidence = 0
    return (
        -confidence,
        -similarity,
        repeated_token_count,
        abs(len(canonical_tokens) - len(site.split())),
        clean(prop.get("propertyId")),
        clean(signal.get("id")),
    )


def deduplicate_planning_signals(
    signals: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], list[Mapping[str, Any]]] = defaultdict(list)
    for signal in signals:
        grouped[planning_observation_key(signal)].append(signal)
    return [
        dict(sorted(grouped[key], key=planning_candidate_sort_key)[0])
        for key in sorted(grouped)
    ]


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
            observed = date_sort_key(event.get("date"), precision)
            age_days = max(0, (as_of - observed).days)
            rank = max(55, 90 - age_days // 7)
            record_type = planning_record_type(
                attributes.get("status"),
                attributes.get("decision"),
            )
            record_label = f"Planning {record_type}"
            status = clean(attributes.get("status"))
            decision = clean(attributes.get("decision"))
            proposal = attributes["proposal"] or clean(event.get("summary"))
            stages = []
            for value in (status, decision):
                if value and value not in stages:
                    stages.append(value)
            fact = " · ".join(value for value in (record_label, *stages, proposal) if value)
            why = (
                f"The source links this planning {record_type} record to the property within "
                f"the {lookback_days}-day signal window."
            )
            context = (
                "The source explicitly records a positive approval decision. Approval does not "
                "prove that permitted work was started or completed."
                if record_type == "approval"
                else (
                    "This is an exact-property planning application record. It is not labelled "
                    "as an approval without an explicit positive source decision, and does not "
                    "prove that proposed work was started or completed."
                )
            )
            output.append(
                base_property_item(
                    item_id=stable_id("today-signal", "planning", event.get("eventId")),
                    lane="signals",
                    kind="property_planning",
                    rank=rank,
                    record=record,
                    title=f"{record_label} · {property_ref(record)['address']}",
                    fact=fact,
                    why=why,
                    context=context,
                    effective_date=clean(event.get("date")),
                    precision=precision,
                    confidence="high",
                    source=clean(event.get("source")),
                    evidence=references,
                    coverage=coverage,
                    limitations=coverage["limitations"],
                    attributes={
                        **attributes,
                        "planningRecordType": record_type,
                        "lookbackDays": lookback_days,
                    },
                )
            )
    return deduplicate_planning_signals(output)


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
) -> list[dict[str, Any]]:
    by_property: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for signal in signals:
        prop = signal.get("property") if isinstance(signal.get("property"), Mapping) else {}
        property_id = clean(prop.get("propertyId"))
        if property_id:
            by_property[property_id].append(signal)

    output = []
    for property_id in sorted(by_property):
        property_signals = sorted(by_property[property_id], key=item_sort_key)
        direct = property_signals[0]
        corroboration = property_signals[1:]
        prop = direct.get("property") if isinstance(direct.get("property"), Mapping) else {}
        direct_family = clean(direct.get("sourceFamily"))
        signal_kinds = sorted({clean(item.get("kind")) for item in property_signals})
        source_families = sorted({
            clean(item.get("sourceFamily"))
            for item in property_signals
            if clean(item.get("sourceFamily"))
        })
        corroboration_families = sorted({
            clean(item.get("sourceFamily"))
            for item in corroboration
            if clean(item.get("sourceFamily"))
        })
        level = "Hot" if len(source_families) >= 2 else "Standard"
        rank = min(100, int(direct.get("rank") or 0) + (5 if level == "Hot" else 0))
        evidence = evidence_union(property_signals)
        if level == "Hot":
            why = (
                f"This Hot opportunity combines {len(property_signals)} qualifying property signals "
                f"across {len(source_families)} independent indicator families."
            )
        else:
            why = (
                f"This Standard opportunity contains {len(property_signals)} qualifying property signal"
                f"{'s' if len(property_signals) != 1 else ''} from one indicator family."
            )
        output.append({
            "id": stable_id("today-opportunity", property_id),
            "lane": "opportunities",
            "kind": "property_opportunity",
            "rank": max(0, min(100, rank)),
            "title": f"{level} opportunity · {clean(prop.get('address'))}",
            "summary": why,
            "fact": clean(direct.get("fact")),
            "why": why,
            "context": (
                "This is an evidence-led prompt for further research, not a prediction of a sale, "
                "seller intent, marketing activity, whether a property is marketed, or future value."
            ),
            "effectiveDate": clean(direct.get("effectiveDate")),
            "datePrecision": clean(direct.get("datePrecision")),
            "confidence": "medium",
            "source": "INSIGHT deterministic Today synthesis",
            "sourceIds": unique_strings(item.get("sourceId") for item in evidence),
            "evidenceIds": unique_strings(item.get("evidenceId") for item in evidence),
            "sourceFamily": "insight",
            "evidence": evidence,
            "coverage": dict(direct.get("coverage") or {}),
            "corroborationCoverage": [
                dict(item.get("coverage") or {}) for item in corroboration
            ],
            "limitations": unique_limitations(
                direct.get("limitations", []),
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
            "directSignalId": clean(direct.get("id")),
            "corroborationIds": [clean(item.get("id")) for item in corroboration],
            "independentSourceCount": len(source_families),
            "indicatorKindCount": len(signal_kinds),
            "opportunityLevel": level,
            "attributes": {
                "directSignalKind": clean(direct.get("kind")),
                "directSourceFamily": direct_family,
                "corroborationKinds": sorted({clean(item.get("kind")) for item in corroboration}),
                "corroborationSourceFamilies": corroboration_families,
                "signalKinds": signal_kinds,
                "sourceFamilies": source_families,
            },
        })
    return output


def default_clock(property_metadata: Mapping[str, Any]) -> tuple[date, str]:
    generated = (
        iso_datetime(property_metadata.get("generatedAt"))
        or datetime(1970, 1, 1, tzinfo=timezone.utc)
    )
    generated_at = generated.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return generated.date(), generated_at


def build_today_feed(
    records: Mapping[str, Mapping[str, Any]],
    property_metadata: Mapping[str, Any],
    *,
    as_of: str | date | None = None,
    generated_at: str | None = None,
    epc_lookback_days: int = 30,
    planning_lookback_days: int = 45,
    sale_age_crossing_window_days: int = 30,
) -> tuple[dict[str, Any], dict[str, Any]]:
    default_as_of, default_generated_at = default_clock(property_metadata)
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
    signals = [
        *epc_signals(records, as_of_date, epc_lookback_days),
        *planning_signals(records, as_of_date, planning_lookback_days),
        *sale_age_signals(records, as_of_date, sale_age_crossing_window_days),
    ]
    signals.sort(key=item_sort_key)
    opportunities = opportunities_from(signals)
    opportunities.sort(key=item_sort_key)

    feed = {
        "schemaVersion": SCHEMA_VERSION,
        "asOf": as_of_date.isoformat(),
        "signals": signals,
        "opportunities": opportunities,
    }
    counts = {
        "signals": len(signals),
        "opportunities": len(opportunities),
    }
    metadata = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": iso_datetime(generated_value).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "asOf": as_of_date.isoformat(),
        "generatorVersion": GENERATOR_VERSION,
        "sourceFingerprints": {
            "propertyRecords": clean(property_metadata.get("datasetFingerprint")),
        },
        "sourceGeneratedAt": {
            "propertyRecords": clean(property_metadata.get("generatedAt")),
        },
        "criteria": {
            "epcLookbackDays": epc_lookback_days,
            "planningLookbackDays": planning_lookback_days,
            "saleAgeCrossingWindowDays": sale_age_crossing_window_days,
            "newsRowsExcluded": True,
            "everyQualifyingSignalCreatesPropertyOpportunity": True,
            "opportunityGrouping": "one-per-property",
            "hotMinimumIndependentSourceFamilies": 2,
        },
        "summary": [
            {"id": lane, "label": SUMMARY_LABELS[lane], "count": counts[lane]}
            for lane in LANE_ORDER
        ],
        "counts": counts,
        "datasetFingerprint": hashlib.sha256(canonical_json(feed).encode("utf-8")).hexdigest(),
        "limitations": [
            "Today is a read-only evidence snapshot; it contains no user-managed fields.",
            "Every qualifying signal is represented by one property-grouped opportunity; Standard has one source family and Hot has two or more.",
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
    "SCHEMA_VERSION",
    "SIGNAL_KINDS",
    "SUMMARY_LABELS",
    "TODAY_FEED_NAME",
    "TODAY_META_NAME",
    "build_today_feed",
    "canonical_json",
    "item_sort_key",
    "normalise",
    "planning_record_type",
    "write_today_feed",
]
