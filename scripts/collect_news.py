#!/usr/bin/env python3
"""Collect and rank link-only property news for the INSIGHT homepage."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from insight_data_utils import parse_window_json, read_js


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCES = ROOT / "config/news-sources.json"
DEFAULT_TRANSACTIONS = ROOT / "outputs/surrey-transactions.js"
DEFAULT_OUTPUT = ROOT / "outputs/news-feed.js"
USER_AGENT = "INSIGHT Surrey property intelligence/1.0 (+link-only news metadata)"
SCORING_VERSION = 1

TOPIC_KEYWORDS = {
    "Planning": ("planning", "application", "approved", "refused", "appeal", "development", "redevelopment"),
    "Transaction": ("sold", "sale", "deal", "transaction", "acquisition", "bought", "buyer"),
    "Prime market": ("prime", "super-prime", "super prime", "luxury", "country house", "mansion", "estate"),
    "Market": ("house price", "property price", "housing market", "market activity", "transactions", "demand", "supply"),
    "Policy": ("stamp duty", "tax", "mortgage", "bank rate", "interest rate", "regulation", "leasehold", "housing policy"),
    "Heritage": ("listed building", "conservation area", "heritage", "historic house", "architecture"),
    "Infrastructure": ("rail", "station", "airport", "road", "infrastructure", "school"),
    "Environment": ("flood", "environment", "green belt", "biodiversity", "climate"),
}

PROPERTY_KEYWORDS = (
    "property", "properties", "home", "homes", "house", "houses", "housing", "residential",
    "estate", "mansion", "apartment", "development", "planning", "mortgage", "land", "freehold",
    "leasehold", "developer", "house price", "stamp duty",
)
MATERIAL_KEYWORDS = (
    "approved", "refused", "appeal", "major", "record", "highest", "lowest", "largest", "acquisition",
    "completed", "sold", "tax", "bank rate", "interest rate", "consultation", "local plan", "green belt",
)
PROMOTIONAL_KEYWORDS = (
    "sponsored", "partner content", "advertorial", "competition", "giveaway", "dream home for sale",
    "property of the week", "interiors", "decorating", "shopping",
)
HOME_COUNTIES = ("home counties", "south east", "berkshire", "buckinghamshire", "hampshire", "sussex", "kent")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalise(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", html.unescape(str(value or "")).upper()).strip()


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
    for child in list(element):
        local = child.tag.rsplit("}", 1)[-1].lower()
        if local in names and child.text:
            return child.text.strip()
    return ""


def feed_entries(payload: bytes, source: dict, fetched_at: datetime) -> list[dict]:
    root = ET.fromstring(payload)
    entries = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1].lower() not in {"item", "entry"}:
            continue
        title = clean_text(child_text(element, ("title",)))
        link = child_text(element, ("link",))
        if not link:
            for child in list(element):
                if child.tag.rsplit("}", 1)[-1].lower() == "link" and child.attrib.get("href"):
                    link = child.attrib["href"]
                    if child.attrib.get("rel", "alternate") == "alternate":
                        break
        url = canonical_url(link)
        if not title or not url:
            continue
        description = clean_text(child_text(element, ("description", "summary", "content", "encoded")))
        published = parse_date(child_text(element, ("pubdate", "published", "updated", "date")), fetched_at)
        entries.append({
            "title": title[:300],
            "url": url,
            "publishedAt": iso_z(published),
            "sourceId": source["id"],
            "source": source["name"],
            "sourceCategory": source.get("category", "editorial"),
            "rightsMode": source.get("rightsMode", "link-only"),
            "_description": description[:2000],
        })
    return entries


def fetch_source(source: dict, timeout: int = 20) -> list[dict]:
    request = urllib.request.Request(source["url"], headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, text/xml;q=0.9"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if getattr(response, "status", 200) != 200:
            raise RuntimeError(f"HTTP {response.status}")
        payload = response.read(5 * 1024 * 1024 + 1)
    if len(payload) > 5 * 1024 * 1024:
        raise RuntimeError("feed exceeds 5 MB")
    return feed_entries(payload, source, utc_now())


def location_catalog(transactions: list[dict]) -> dict[str, list[str]]:
    def values(field: str, minimum: int = 3) -> list[str]:
        unique = {clean_text(item.get(field, "")) for item in transactions}
        return sorted((item for item in unique if len(item) >= minimum), key=lambda item: (-len(item), item))

    estates = values("estate", 4)
    towns = values("town", 4)
    districts = values("district", 4)
    addresses = []
    for item in transactions:
        first = clean_text(str(item.get("address", "")).split(",", 1)[0])
        # Named houses are often generic phrases (for example "Thatched Cottage").
        # Exact property matching is therefore restricted to numbered addresses;
        # named homes can still match safely through an estate, road, town or postcode.
        if len(first) >= 8 and re.search(r"\d", first):
            addresses.append(first)
    return {"properties": sorted(set(addresses), key=lambda item: (-len(item), item)), "estates": estates, "towns": towns, "districts": districts}


def matched_terms(normalised_text: str, terms: list[str], limit: int = 3) -> list[str]:
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
    for number, unit in re.findall(r"£\s*([0-9]+(?:\.[0-9]+)?)\s*(BN|BILLION|M|MILLION|K|THOUSAND)?", text.upper()):
        scale = {"BN": 1e9, "BILLION": 1e9, "M": 1e6, "MILLION": 1e6, "K": 1e3, "THOUSAND": 1e3}.get(unit, 1)
        values.append(float(number) * scale)
    return max(values, default=0)


def score_article(article: dict, source: dict, catalog: dict[str, list[str]], now: datetime | None = None) -> dict | None:
    now = now or utc_now()
    combined = f"{article.get('title', '')} {article.get('_description', '')}"
    upper = normalise(combined)
    lower = combined.lower()
    property_matches = matched_terms(upper, catalog["properties"], 1)
    estate_matches = matched_terms(upper, catalog["estates"], 2)
    town_matches = matched_terms(upper, catalog["towns"], 3)
    district_matches = matched_terms(upper, catalog["districts"], 2)
    mentions_surrey = " SURREY " in f" {upper} " or normalise(source.get("defaultGeography", "")) == "SURREY"
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
    prime_hits = sum(1 for term in ("prime", "super-prime", "luxury", "country house", "mansion", "high-end") if term in lower)
    property_relevance = min(20, property_hits * 3 + prime_hits * 3 + int(source.get("primePropertyBias", 0)))

    topics = [name for name, keywords in TOPIC_KEYWORDS.items() if any(term in lower for term in keywords)]
    value = money_value(combined)
    material_hits = sum(1 for term in MATERIAL_KEYWORDS if term in lower)
    materiality = min(15, material_hits * 2 + (7 if value >= 10_000_000 else 5 if value >= 3_000_000 else 3 if value >= 1_000_000 else 0))
    if "Planning" in topics and ("approved" in lower or "refused" in lower or "appeal" in lower):
        materiality = min(15, materiality + 3)

    quality = max(0, min(10, int(source.get("quality", 5))))
    connection = 10 if match_type in {"property", "estate"} else 7 if match_type == "town" else 5 if match_type == "district" else 3 if match_type == "county" else 0
    published = parse_date(article.get("publishedAt", ""), now)
    age = max(timedelta(0), now - published)
    freshness = 5 if age <= timedelta(days=1) else 4 if age <= timedelta(days=3) else 2 if age <= timedelta(days=7) else 0
    penalty = 15 if any(term in lower for term in PROMOTIONAL_KEYWORDS) else 0
    if age > timedelta(days=30):
        penalty += 20
    elif age > timedelta(days=7):
        penalty += 10

    score = max(0, min(100, geography + property_relevance + entity + materiality + quality + connection + freshness - penalty))
    nationally_material = property_relevance >= 15 and materiality >= 8 and quality >= 7
    if not ((geography >= 8 and property_relevance >= 7) or nationally_material):
        return None

    reason_parts = []
    if locations:
        reason_parts.append(f"Matches {', '.join(locations[:2])}")
    if topics:
        reason_parts.append(topics[0])
    if value:
        reason_parts.append("High-value event")
    if not reason_parts:
        reason_parts.append("Prime property market")

    identifier = hashlib.sha256(f"{article['sourceId']}|{article['url']}".encode()).hexdigest()[:20]
    return {
        "id": f"news-{identifier}",
        "title": article["title"],
        "url": article["url"],
        "sourceId": article["sourceId"],
        "source": article["source"],
        "sourceCategory": article.get("sourceCategory", "editorial"),
        "rightsMode": article.get("rightsMode", "link-only"),
        "publishedAt": iso_z(published),
        "score": score,
        "scoringVersion": SCORING_VERSION,
        "location": locations[0] if locations else "UK prime market",
        "matchType": match_type,
        "topics": topics[:3] or ["Property"],
        "reason": " · ".join(reason_parts[:3]),
    }


def title_fingerprint(title: str) -> set[str]:
    ignored = {"THE", "A", "AN", "AND", "TO", "OF", "IN", "FOR", "ON", "WITH", "AS", "AT", "IS"}
    return {token for token in normalise(title).split() if len(token) > 2 and token not in ignored}


def deduplicate(items: list[dict]) -> list[dict]:
    selected = []
    urls = set()
    for item in sorted(items, key=lambda row: (-int(row.get("score", 0)), row.get("publishedAt", "")), reverse=False):
        url = canonical_url(item.get("url", ""))
        if not url or url in urls:
            continue
        fingerprint = title_fingerprint(item.get("title", ""))
        duplicate = False
        for existing in selected:
            other = title_fingerprint(existing.get("title", ""))
            union = fingerprint | other
            similarity = len(fingerprint & other) / len(union) if union else 0
            same_context = item.get("location") == existing.get("location") and set(item.get("topics", [])) & set(existing.get("topics", []))
            if similarity >= 0.72 and same_context:
                duplicate = True
                break
        if duplicate:
            continue
        urls.add(url)
        selected.append(item)
    return sorted(selected, key=lambda row: (int(row.get("score", 0)), row.get("publishedAt", "")), reverse=True)


def read_existing(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        return parse_window_json(path.read_text(encoding="utf-8"), "INSIGHT_NEWS_ITEMS", [])
    except (json.JSONDecodeError, ValueError):
        return []


def write_feed(path: Path, items: list[dict], metadata: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        "window.INSIGHT_NEWS_ITEMS = " + json.dumps(items, ensure_ascii=False, separators=(",", ":")) + ";\n"
        "window.INSIGHT_NEWS_META = " + json.dumps(metadata, ensure_ascii=False, separators=(",", ":")) + ";\n"
    )
    path.write_text(text, encoding="utf-8")


def collect(sources_path: Path, transactions_path: Path, output_path: Path, minimum_score: int, retention_days: int, timeout: int) -> tuple[list[dict], dict]:
    manifest = json.loads(sources_path.read_text(encoding="utf-8"))
    transactions, _summary, _metadata = read_js(transactions_path)
    catalog = location_catalog(transactions)
    now = utc_now()
    cutoff = now - timedelta(days=retention_days)
    existing = [
        item for item in read_existing(output_path)
        if item.get("scoringVersion") == SCORING_VERSION and parse_date(item.get("publishedAt", ""), now) >= cutoff
    ]
    source_by_id = {source["id"]: source for source in manifest.get("sources", [])}
    candidates = list(existing)
    source_errors = []
    fetched_sources = 0
    discovered = 0

    for source in manifest.get("sources", []):
        if not source.get("enabled", True):
            continue
        try:
            entries = fetch_source(source, timeout)
            fetched_sources += 1
            discovered += len(entries)
            for entry in entries:
                scored = score_article(entry, source, catalog, now)
                source_minimum = min(minimum_score, int(source.get("minimumScore", minimum_score)))
                if scored and scored["score"] >= source_minimum and parse_date(scored["publishedAt"], now) >= cutoff:
                    candidates.append(scored)
        except Exception as exc:  # A single publication must not blank the feed.
            source_errors.append({"sourceId": source.get("id", "unknown"), "error": str(exc)[:240]})

    enabled_count = sum(1 for source in manifest.get("sources", []) if source.get("enabled", True))
    if enabled_count and fetched_sources == 0:
        raise RuntimeError("Every enabled news source failed; preserving the last valid feed")

    items = deduplicate(candidates)[:60]
    metadata = {
        "schemaVersion": 1,
        "scoringVersion": SCORING_VERSION,
        "generatedAt": iso_z(now),
        "minimumScore": minimum_score,
        "retentionDays": retention_days,
        "articleCount": len(items),
        "sourcesConfigured": enabled_count,
        "sourcesFetched": fetched_sources,
        "candidatesDiscovered": discovered,
        "sourceErrors": source_errors,
        "rightsMode": "link-only",
        "refreshMinutes": 60,
    }
    write_feed(output_path, items, metadata)
    return items, metadata


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    result.add_argument("--transactions", type=Path, default=DEFAULT_TRANSACTIONS)
    result.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    result.add_argument("--minimum-score", type=int, default=55)
    result.add_argument("--retention-days", type=int, default=14)
    result.add_argument("--timeout", type=int, default=20)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        items, metadata = collect(args.sources, args.transactions, args.output, args.minimum_score, args.retention_days, args.timeout)
    except (OSError, ValueError, RuntimeError, ET.ParseError, json.JSONDecodeError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    print(f"News feed: {len(items)} articles from {metadata['sourcesFetched']}/{metadata['sourcesConfigured']} sources; {len(metadata['sourceErrors'])} source errors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
