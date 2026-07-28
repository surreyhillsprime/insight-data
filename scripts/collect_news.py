#!/usr/bin/env python3
"""Collect, rank and safely retain link-only property news for INSIGHT."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from insight_data_utils import parse_window_json, read_js
from news_adapters import fetch_source as adapter_fetch_source
from news_sources import load_registry


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCES = ROOT / "config/news-sources.json"
DEFAULT_TRANSACTIONS = ROOT / "outputs/surrey-transactions.js"
DEFAULT_OUTPUT = ROOT / "outputs/news-feed.js"
SCORING_VERSION = 2

TOPIC_KEYWORDS = {
    "Planning": (
        "planning",
        "application",
        "approved",
        "refused",
        "appeal",
        "development",
        "redevelopment",
        "local plan",
    ),
    "Transaction": (
        "sold",
        "sale",
        "deal",
        "transaction",
        "acquisition",
        "bought",
        "buyer",
        "price paid",
    ),
    "Prime market": (
        "prime",
        "super-prime",
        "super prime",
        "luxury",
        "country house",
        "mansion",
        "estate",
    ),
    "Market": (
        "house price",
        "property price",
        "housing market",
        "market activity",
        "transactions",
        "demand",
        "supply",
        "private rent",
        "affordability",
    ),
    "Policy": (
        "stamp duty",
        "tax",
        "mortgage",
        "bank rate",
        "interest rate",
        "regulation",
        "leasehold",
        "freehold",
        "housing policy",
    ),
    "Heritage": (
        "listed building",
        "conservation area",
        "heritage",
        "historic house",
        "architecture",
    ),
    "Infrastructure": (
        "rail",
        "station",
        "airport",
        "road",
        "infrastructure",
        "school",
    ),
    "Environment": (
        "flood",
        "environment",
        "green belt",
        "biodiversity",
        "climate",
    ),
}
PROPERTY_KEYWORDS = (
    "property",
    "properties",
    "home",
    "homes",
    "house",
    "houses",
    "housing",
    "residential",
    "estate",
    "mansion",
    "apartment",
    "development",
    "planning",
    "mortgage",
    "land",
    "freehold",
    "leasehold",
    "developer",
    "house price",
    "private rent",
    "housing affordability",
    "stamp duty",
)
MATERIAL_KEYWORDS = (
    "approved",
    "refused",
    "appeal",
    "major",
    "record",
    "highest",
    "lowest",
    "largest",
    "acquisition",
    "completed",
    "sold",
    "tax",
    "bank rate",
    "interest rate",
    "consultation",
    "local plan",
    "green belt",
    "index",
    "statistics",
)
PROMOTIONAL_KEYWORDS = (
    "sponsored",
    "partner content",
    "advertorial",
    "competition",
    "giveaway",
    "dream home for sale",
    "property of the week",
    "interiors",
    "decorating",
    "shopping",
)
HOME_COUNTIES = (
    "home counties",
    "south east",
    "berkshire",
    "buckinghamshire",
    "hampshire",
    "sussex",
    "kent",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def normalise(value: str) -> str:
    return re.sub(
        r"[^A-Z0-9]+", " ", html.unescape(str(value or "")).upper()
    ).strip()


def clean_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html.unescape(str(value or "")))
    return re.sub(r"\s+", " ", text).strip()


def canonical_url(value: str) -> str:
    try:
        parts = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    if parts.scheme != "https" or not parts.netloc:
        return ""
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/") or "/"
    return urlunsplit(("https", parts.netloc.lower(), path, "", ""))


def parse_date(value: str, fallback: datetime | None = None) -> datetime:
    raw = str(value or "").strip()
    if raw:
        try:
            result = parsedate_to_datetime(raw)
            if result.tzinfo is None:
                result = result.replace(tzinfo=timezone.utc)
            return result.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            pass
        try:
            result = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if result.tzinfo is None:
                result = result.replace(tzinfo=timezone.utc)
            return result.astimezone(timezone.utc)
        except ValueError:
            pass
    return fallback or utc_now()


def child_text(element: ET.Element, names: tuple[str, ...]) -> str:
    """Compatibility helper retained for the existing networkless tests."""

    for child in list(element):
        local = child.tag.rsplit("}", 1)[-1].lower()
        if local in names and child.text:
            return child.text.strip()
    return ""


def feed_entries(
    payload: bytes, source: dict, fetched_at: datetime | None = None
) -> list[dict]:
    """Parse RSS/Atom fixtures without performing network transport.

    Live collection uses ``news_adapters``. This wrapper keeps the established
    scoring API available to downstream tests and local tooling.
    """

    root = ET.fromstring(payload)
    entries = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1].lower() not in {"item", "entry"}:
            continue
        title = clean_text(child_text(element, ("title",)))
        link = child_text(element, ("link",))
        if not link:
            for child in list(element):
                if (
                    child.tag.rsplit("}", 1)[-1].lower() == "link"
                    and child.attrib.get("href")
                ):
                    link = child.attrib["href"]
                    if child.attrib.get("rel", "alternate") == "alternate":
                        break
        url = canonical_url(link)
        date_text = child_text(
            element, ("pubdate", "published", "updated", "date")
        )
        if not title or not url or not date_text:
            continue
        published = parse_date(date_text, None)
        entries.append(
            {
                "title": title[:300],
                "url": url,
                "publishedAt": iso_z(published),
                "sourceId": source["id"],
                "source": source["name"],
                "sourceCategory": source.get("category", "editorial"),
                "rightsMode": "link-only",
                "_description": clean_text(
                    child_text(
                        element,
                        ("description", "summary", "content", "encoded"),
                    )
                )[:2000],
            }
        )
    return entries


def fetch_source(source: dict, timeout: int = 20):
    """Compatibility proxy to the registry-selected adapter implementation."""

    return adapter_fetch_source(source, timeout=timeout)


def location_catalog(transactions: list[dict]) -> dict[str, list[str]]:
    def values(field: str, minimum: int = 3) -> list[str]:
        unique = {clean_text(item.get(field, "")) for item in transactions}
        return sorted(
            (item for item in unique if len(item) >= minimum),
            key=lambda item: (-len(item), item),
        )

    estates = values("estate", 4)
    estate_parts = []
    for estate in estates:
        estate_parts.extend(
            part.strip()
            for part in re.split(r"[/|]", estate)
            if len(part.strip()) >= 4
        )
    estates = sorted(
        set(estates + estate_parts), key=lambda item: (-len(item), item)
    )
    towns = values("town", 4)
    districts = values("district", 4)
    addresses = []
    for item in transactions:
        first = clean_text(str(item.get("address", "")).split(",", 1)[0])
        if len(first) >= 8 and re.search(r"\d", first):
            addresses.append(first)
    return {
        "properties": sorted(
            set(addresses), key=lambda item: (-len(item), item)
        ),
        "estates": estates,
        "towns": towns,
        "districts": districts,
    }


def matched_terms(
    normalised_text: str, terms: list[str], limit: int = 3
) -> list[str]:
    matches = []
    padded = f" {normalised_text} "
    for term in terms:
        token = normalise(term)
        if token and f" {token} " in padded:
            matches.append(term)
            if len(matches) >= limit:
                break
    return matches


def money_value(text: str) -> float:
    values = []
    for number, unit in re.findall(
        r"£\s*([0-9]+(?:\.[0-9]+)?)\s*(BN|BILLION|M|MILLION|K|THOUSAND)?",
        text.upper(),
    ):
        scale = {
            "BN": 1e9,
            "BILLION": 1e9,
            "M": 1e6,
            "MILLION": 1e6,
            "K": 1e3,
            "THOUSAND": 1e3,
        }.get(unit, 1)
        values.append(float(number) * scale)
    return max(values, default=0)


def title_is_allowed(title: str, source: dict) -> bool:
    for pattern in source.get("blockedTitlePatterns", []):
        if re.search(pattern, title, re.I):
            return False
    required = source.get("requiredTitlePatterns", [])
    return not required or any(re.search(pattern, title, re.I) for pattern in required)


def age_adjustment(published: datetime, now: datetime) -> int:
    age = max(timedelta(0), now - published)
    freshness = (
        5
        if age <= timedelta(days=1)
        else 4
        if age <= timedelta(days=3)
        else 2
        if age <= timedelta(days=7)
        else 0
    )
    stale_penalty = (
        20
        if age > timedelta(days=30)
        else 10
        if age > timedelta(days=7)
        else 0
    )
    return freshness - stale_penalty


def score_article(
    article: dict,
    source: dict,
    catalog: dict[str, list[str]],
    now: datetime | None = None,
) -> dict | None:
    now = now or utc_now()
    if not title_is_allowed(str(article.get("title") or ""), source):
        return None
    combined = f"{article.get('title', '')} {article.get('_description', '')}"
    upper = normalise(combined)
    lower = combined.lower()
    property_matches = matched_terms(upper, catalog["properties"], 1)
    estate_matches = matched_terms(upper, catalog["estates"], 2)
    town_matches = matched_terms(upper, catalog["towns"], 3)
    district_matches = matched_terms(upper, catalog["districts"], 2)
    mentions_surrey = (
        " SURREY " in f" {upper} "
        or normalise(source.get("defaultGeography", "")) == "SURREY"
    )
    mentions_home_counties = any(term in lower for term in HOME_COUNTIES)

    if property_matches:
        geography, entity = 25, 15
        locations = property_matches
        match_type = "property"
    elif estate_matches:
        geography, entity = 24, 15
        locations = estate_matches
        match_type = "estate"
    elif town_matches:
        geography, entity = 22, 9
        locations = town_matches
        match_type = "town"
    elif district_matches:
        geography, entity = 18, 5
        locations = district_matches
        match_type = "district"
    elif mentions_surrey:
        geography, entity = 16, 3
        locations = ["Surrey"]
        match_type = "county"
    elif mentions_home_counties:
        geography, entity = 8, 0
        locations = ["Home Counties"]
        match_type = "region"
    else:
        geography, entity = 4, 0
        locations = []
        match_type = "national"

    property_hits = sum(1 for term in PROPERTY_KEYWORDS if term in lower)
    prime_hits = sum(
        1
        for term in (
            "prime",
            "super-prime",
            "luxury",
            "country house",
            "mansion",
            "high-end",
        )
        if term in lower
    )
    property_relevance = min(
        20,
        property_hits * 3
        + prime_hits * 3
        + int(source.get("primePropertyBias", 0)),
    )
    topics = [
        name
        for name, keywords in TOPIC_KEYWORDS.items()
        if any(term in lower for term in keywords)
    ]
    value = money_value(combined)
    material_hits = sum(1 for term in MATERIAL_KEYWORDS if term in lower)
    materiality = min(
        15,
        material_hits * 2
        + (
            7
            if value >= 10_000_000
            else 5
            if value >= 2_000_000
            else 3
            if value >= 1_000_000
            else 0
        ),
    )
    if "Planning" in topics and any(
        term in lower for term in ("approved", "refused", "appeal")
    ):
        materiality = min(15, materiality + 3)

    authoritative = bool(source.get("authoritativeNational"))
    if authoritative:
        geography = max(geography, 8)
        property_relevance = max(property_relevance, 20)
        materiality = max(materiality, 15)
        topics = topics or ["Market"]

    quality = max(0, min(10, int(source.get("quality", 5))))
    connection = (
        10
        if match_type in {"property", "estate"}
        else 7
        if match_type == "town"
        else 5
        if match_type == "district"
        else 3
        if match_type == "county"
        else 0
    )
    if authoritative:
        connection = max(connection, 3)
    promotion_penalty = (
        15 if any(term in lower for term in PROMOTIONAL_KEYWORDS) else 0
    )
    score_base = max(
        0,
        min(
            100,
            geography
            + property_relevance
            + entity
            + materiality
            + quality
            + connection
            - promotion_penalty,
        ),
    )
    published = parse_date(article.get("publishedAt", ""), now)
    score = max(0, min(100, score_base + age_adjustment(published, now)))
    nationally_material = (
        property_relevance >= 15 and materiality >= 8 and quality >= 7
    )
    if not authoritative and not (
        (geography >= 8 and property_relevance >= 7) or nationally_material
    ):
        return None

    if authoritative:
        locations = [source.get("defaultLocation", "UK housing market")]
        match_type = "national"
    reason_parts = []
    if authoritative:
        reason_parts.append("Official UK housing release")
    elif locations:
        reason_parts.append(f"Matches {', '.join(locations[:2])}")
    if topics:
        reason_parts.append(topics[0])
    if value:
        reason_parts.append("High-value event")
    if not reason_parts:
        reason_parts.append("Prime property market")

    identifier = hashlib.sha256(
        f"{article['sourceId']}|{article['url']}".encode()
    ).hexdigest()[:20]
    return {
        "id": f"news-{identifier}",
        "title": article["title"],
        "url": article["url"],
        "sourceId": article["sourceId"],
        "source": article["source"],
        "sourceCategory": article.get("sourceCategory", "editorial"),
        "publisherGroup": source.get("publisherGroup", article["sourceId"]),
        "lane": source.get("lane", "uk-market"),
        "rightsMode": "link-only",
        "publishedAt": iso_z(published),
        "score": score,
        "scoreBase": score_base,
        "scoringVersion": SCORING_VERSION,
        "location": locations[0] if locations else "UK prime market",
        "matchType": match_type,
        "topics": topics[:3] or ["Property"],
        "reason": " · ".join(reason_parts[:3]),
    }


def refresh_retained_score(item: dict, now: datetime) -> dict:
    refreshed = dict(item)
    base = refreshed.get("scoreBase")
    if not isinstance(base, int):
        return refreshed
    published = parse_date(refreshed.get("publishedAt", ""), now)
    refreshed["score"] = max(
        0, min(100, base + age_adjustment(published, now))
    )
    return refreshed


def title_fingerprint(title: str) -> set[str]:
    ignored = {
        "THE",
        "A",
        "AN",
        "AND",
        "TO",
        "OF",
        "IN",
        "FOR",
        "ON",
        "WITH",
        "AS",
        "AT",
        "IS",
    }
    return {
        token
        for token in normalise(title).split()
        if len(token) > 2 and token not in ignored
    }


def deduplicate(items: list[dict]) -> list[dict]:
    selected = []
    urls = set()
    for item in sorted(
        items,
        key=lambda row: (
            int(row.get("score", 0)),
            row.get("publishedAt", ""),
        ),
        reverse=True,
    ):
        url = canonical_url(item.get("url", ""))
        if not url or url in urls:
            continue
        fingerprint = title_fingerprint(item.get("title", ""))
        duplicate = False
        for existing in selected:
            other = title_fingerprint(existing.get("title", ""))
            union = fingerprint | other
            similarity = len(fingerprint & other) / len(union) if union else 0
            same_context = (
                item.get("location") == existing.get("location")
                and set(item.get("topics", []))
                & set(existing.get("topics", []))
            )
            if similarity >= 0.72 and same_context:
                duplicate = True
                break
        if duplicate:
            continue
        urls.add(url)
        selected.append(item)
    return sorted(
        selected,
        key=lambda row: (
            row.get("publishedAt", ""),
            int(row.get("score", 0)),
        ),
        reverse=True,
    )


def read_existing(path: Path) -> tuple[list[dict], dict]:
    if not path.is_file():
        return [], {}
    try:
        text = path.read_text(encoding="utf-8")
        items = parse_window_json(text, "INSIGHT_NEWS_ITEMS", [])
        metadata = parse_window_json(text, "INSIGHT_NEWS_META", {})
        return (
            items if isinstance(items, list) else [],
            metadata if isinstance(metadata, dict) else {},
        )
    except (json.JSONDecodeError, ValueError):
        return [], {}


def write_feed(path: Path, items: list[dict], metadata: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        "window.INSIGHT_NEWS_ITEMS = "
        + json.dumps(items, ensure_ascii=False, separators=(",", ":"))
        + ";\n"
        "window.INSIGHT_NEWS_META = "
        + json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        + ";\n"
    )
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def rights_status(source: dict) -> str:
    rights = source.get("rights", {})
    if (
        rights.get("collectionStatus") == "approved"
        and rights.get("publicationStatus") == "approved"
        and rights.get("mode") == "link-only"
    ):
        return "approved"
    return "blocked"


def error_code(error: Exception) -> str:
    message = str(error).lower()
    if "timeout" in message or "timed out" in message:
        return "timeout"
    if "http" in message:
        return "http-error"
    if "xml" in message or "parse" in message:
        return "parse-error"
    if "domain" in message or "host" in message:
        return "article-host-rejected"
    return "fetch-error"


def _fetch_one(
    source: dict,
    timeout: int,
    fetcher: Callable,
) -> tuple[list[dict], dict, int]:
    started = time.monotonic()
    result = fetcher(source, timeout)
    if isinstance(result, tuple):
        entries, adapter_diagnostics = result
    else:
        entries, adapter_diagnostics = result, {}
    if not isinstance(entries, list):
        raise ValueError("adapter did not return an article list")
    duration_ms = round((time.monotonic() - started) * 1000)
    return entries, dict(adapter_diagnostics or {}), duration_ms


def collect(
    sources_path: Path,
    transactions_path: Path,
    output_path: Path,
    minimum_score: int,
    retention_days: int,
    timeout: int,
    *,
    fetcher: Callable | None = None,
    now: datetime | None = None,
) -> tuple[list[dict], dict]:
    manifest = load_registry(sources_path)
    transactions, _summary, _metadata = read_js(transactions_path)
    catalog = location_catalog(transactions)
    now = (now or utc_now()).astimezone(timezone.utc)
    cutoff = now - timedelta(days=retention_days)
    existing, existing_metadata = read_existing(output_path)
    existing_by_source: dict[str, list[dict]] = {}
    for item in existing:
        existing_by_source.setdefault(str(item.get("sourceId") or ""), []).append(
            item
        )
    previous_diagnostics = {
        str(row.get("sourceId")): row
        for row in existing_metadata.get("sourceDiagnostics", [])
        if isinstance(row, dict)
    }

    sources = manifest["sources"]
    live_or_shadow = [
        source
        for source in sources
        if source.get("publicationMode") in {"live", "shadow"}
        and rights_status(source) == "approved"
    ]
    active_live = [
        source
        for source in live_or_shadow
        if source.get("publicationMode") == "live"
    ]
    fetcher = fetcher or fetch_source
    results: dict[str, tuple[list[dict], dict, int]] = {}
    failures: dict[str, Exception] = {}
    workers = max(1, min(6, len(live_or_shadow)))
    if live_or_shadow:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_sources = {
                executor.submit(_fetch_one, source, timeout, fetcher): source
                for source in live_or_shadow
            }
            for future in as_completed(future_sources):
                source = future_sources[future]
                try:
                    results[source["id"]] = future.result()
                except Exception as error:  # One publisher cannot blank peers.
                    failures[source["id"]] = error

    candidates: list[dict] = []
    diagnostics: list[dict] = []
    source_errors: list[dict] = []
    successful_live = 0
    discovered_total = 0
    qualified_total = 0
    for source in sources:
        source_id = source["id"]
        mode = source.get("publicationMode")
        previous = previous_diagnostics.get(source_id, {})
        common = {
            "sourceId": source_id,
            "adapter": source.get("adapter"),
            "publicationMode": mode,
            "rightsStatus": rights_status(source),
            "checkedAt": iso_z(now),
            "lastSuccessAt": previous.get("lastSuccessAt"),
            "consecutiveFailures": 0,
            "discovered": 0,
            "parsed": 0,
            "eligible": 0,
            "qualified": 0,
            "retained": 0,
            "newestPublishedAt": None,
            "durationMs": 0,
            "errorCode": None,
            "errorMessage": None,
        }
        if mode == "disabled" or rights_status(source) != "approved":
            diagnostics.append({**common, "status": "blocked"})
            continue
        if source_id in failures:
            error = failures[source_id]
            code = error_code(error)
            carried = []
            if mode == "live":
                source_minimum = int(
                    source.get("minimumScore", minimum_score)
                )
                for item in existing_by_source.get(source_id, []):
                    if (
                        item.get("scoringVersion") == SCORING_VERSION
                        and parse_date(item.get("publishedAt", ""), now) >= cutoff
                    ):
                        refreshed = refresh_retained_score(item, now)
                        if int(refreshed.get("score", 0)) >= source_minimum:
                            carried.append(refreshed)
                candidates.extend(carried)
            diagnostic = {
                **common,
                "status": "failed",
                "consecutiveFailures": int(
                    previous.get("consecutiveFailures") or 0
                )
                + 1,
                "retained": len(carried),
                "newestPublishedAt": max(
                    (item.get("publishedAt") for item in carried),
                    default=None,
                ),
                "errorCode": code,
                "errorMessage": clean_text(str(error))[:160],
            }
            diagnostics.append(diagnostic)
            source_errors.append(
                {
                    "sourceId": source_id,
                    "errorCode": code,
                    "error": clean_text(str(error))[:160],
                }
            )
            continue

        entries, adapter_diagnostic, duration_ms = results.get(
            source_id, ([], {}, 0)
        )
        discovered = int(
            adapter_diagnostic.get("discovered", len(entries))
        )
        parsed = int(adapter_diagnostic.get("parsed", len(entries)))
        discovered_total += discovered
        eligible = 0
        qualified: list[dict] = []
        source_minimum = int(source.get("minimumScore", minimum_score))
        for entry in entries:
            scored = score_article(entry, source, catalog, now)
            if scored is None:
                continue
            eligible += 1
            if (
                scored["score"] >= source_minimum
                and parse_date(scored["publishedAt"], now) >= cutoff
            ):
                qualified.append(scored)
        qualified_total += len(qualified)
        if mode == "live":
            candidates.extend(qualified)
            successful_live += 1
        newest = max(
            (entry.get("publishedAt") for entry in entries), default=None
        )
        diagnostics.append(
            {
                **common,
                "status": "ok" if entries else "empty",
                "lastSuccessAt": iso_z(now),
                "discovered": discovered,
                "parsed": parsed,
                "eligible": eligible,
                "qualified": len(qualified),
                "newestPublishedAt": newest,
                "durationMs": duration_ms,
            }
        )

    if active_live and successful_live == 0:
        raise RuntimeError(
            "Every live news source failed; preserving the last valid feed"
        )

    items = deduplicate(candidates)[:60]
    retained_counts: dict[str, int] = {}
    for item in items:
        source_id = str(item.get("sourceId") or "")
        retained_counts[source_id] = retained_counts.get(source_id, 0) + 1
    for diagnostic in diagnostics:
        diagnostic["retained"] = retained_counts.get(
            diagnostic["sourceId"], 0
        )
    newest_published_at = max(
        (item.get("publishedAt") for item in items), default=None
    )
    metadata = {
        "schemaVersion": 1,
        "scoringVersion": SCORING_VERSION,
        "generatedAt": iso_z(now),
        "lastCheckedAt": iso_z(now),
        "newestPublishedAt": newest_published_at,
        "minimumScore": minimum_score,
        "retentionDays": retention_days,
        "articleCount": len(items),
        "sourcesConfigured": len(active_live),
        "sourcesFetched": successful_live,
        "candidatesDiscovered": discovered_total,
        "qualifiedBeforeDedupe": qualified_total,
        "sourceDiagnostics": diagnostics,
        "sourceErrors": source_errors,
        "rightsMode": "link-only",
        "refreshMinutes": 30,
    }
    write_feed(output_path, items, metadata)
    return items, metadata


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    result.add_argument(
        "--transactions", type=Path, default=DEFAULT_TRANSACTIONS
    )
    result.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    result.add_argument("--minimum-score", type=int, default=45)
    result.add_argument("--retention-days", type=int, default=30)
    result.add_argument("--timeout", type=int, default=20)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        items, metadata = collect(
            args.sources,
            args.transactions,
            args.output,
            args.minimum_score,
            args.retention_days,
            args.timeout,
        )
    except (
        OSError,
        ValueError,
        RuntimeError,
        ET.ParseError,
        json.JSONDecodeError,
    ) as error:
        print(f"ERROR {error}", file=sys.stderr)
        return 1
    print(
        "News feed: "
        f"{len(items)} articles from "
        f"{metadata['sourcesFetched']}/{metadata['sourcesConfigured']} live sources; "
        f"{len(metadata['sourceErrors'])} source errors"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
