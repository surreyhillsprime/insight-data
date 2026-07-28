#!/usr/bin/env python3
"""Refresh only the news-derived lane of an existing INSIGHT Today feed."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from insight_data_utils import parse_window_json, read_js
from today_feed import (
    LANE_ORDER,
    SUMMARY_LABELS,
    canonical_json,
    entity_news_changes,
    item_sort_key,
    opportunities_from,
    write_today_feed,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TODAY = ROOT / "outputs" / "today-feed.js"
DEFAULT_NEWS = ROOT / "outputs" / "news-feed.js"
DEFAULT_TRANSACTIONS = ROOT / "outputs" / "surrey-transactions.js"


def read_today(path: Path) -> tuple[dict, dict]:
    text = path.read_text(encoding="utf-8")
    feed = parse_window_json(text, "INSIGHT_TODAY_FEED", None)
    metadata = parse_window_json(text, "INSIGHT_TODAY_META", None)
    if not isinstance(feed, dict) or not isinstance(metadata, dict):
        raise ValueError("Today feed must contain object assignments")
    return feed, metadata


def read_news(path: Path) -> tuple[list[dict], dict]:
    text = path.read_text(encoding="utf-8")
    items = parse_window_json(text, "INSIGHT_NEWS_ITEMS", None)
    metadata = parse_window_json(text, "INSIGHT_NEWS_META", None)
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ValueError("news feed items must be an array of objects")
    if not isinstance(metadata, dict):
        raise ValueError("news metadata must be an object")
    return items, metadata


def transaction_records(transactions: list[dict]) -> dict[str, dict]:
    """Build the identity/profile subset needed for deterministic news matching."""

    records: dict[str, dict] = {}
    for transaction in transactions:
        property_id = str(transaction.get("propertyRecordId") or "").strip()
        if not property_id:
            continue
        record = records.setdefault(
            property_id,
            {
                "propertyId": property_id,
                "canonicalAddress": str(transaction.get("address") or "").strip(),
                "profile": {},
            },
        )
        profile = record["profile"]
        for field in ("market", "district", "town", "estate", "estateId"):
            if not profile.get(field) and transaction.get(field):
                profile[field] = transaction[field]
    return records


def parse_timestamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def newest_timestamp(*values: object) -> str:
    parsed = [timestamp for timestamp in map(parse_timestamp, values) if timestamp]
    if not parsed:
        raise ValueError("Today and news metadata do not contain a valid generatedAt")
    return max(parsed).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def update_today_news(
    feed: dict,
    metadata: dict,
    news_items: list[dict],
    news_metadata: dict,
    records: dict[str, dict],
    *,
    minimum_score: int | None = None,
) -> tuple[dict, dict]:
    if feed.get("schemaVersion") != 1 or metadata.get("schemaVersion") != 1:
        raise ValueError("Today schemaVersion must remain 1")
    threshold = (
        int(minimum_score)
        if minimum_score is not None
        else int(metadata.get("criteria", {}).get("newsMinimumScore", 55))
    )
    if not 0 <= threshold <= 100:
        raise ValueError("news minimum score must be between 0 and 100")

    signals = [dict(item) for item in feed.get("signals", []) if isinstance(item, dict)]
    non_news_changes = [
        dict(item)
        for item in feed.get("placeChanges", [])
        if isinstance(item, dict) and item.get("kind") != "entity_news"
    ]
    current_news = sorted(
        (dict(item) for item in news_items if isinstance(item, dict)),
        key=canonical_json,
    )
    news_changes = entity_news_changes(records, current_news, threshold)
    place_changes = [*non_news_changes, *news_changes]
    signals.sort(key=item_sort_key)
    place_changes.sort(key=item_sort_key)
    opportunities = opportunities_from(signals, place_changes)
    opportunities.sort(key=item_sort_key)

    updated_feed = {
        "schemaVersion": 1,
        "asOf": feed.get("asOf"),
        "signals": signals,
        "opportunities": opportunities,
        "placeChanges": place_changes,
    }
    counts = {
        "signals": len(signals),
        "opportunities": len(opportunities),
        "placeChanges": len(place_changes),
    }
    updated_metadata = dict(metadata)
    updated_metadata.update(
        {
            "generatedAt": newest_timestamp(
                metadata.get("generatedAt"), news_metadata.get("generatedAt")
            ),
            "counts": counts,
            "summary": [
                {"id": lane, "label": SUMMARY_LABELS[lane], "count": counts[lane]}
                for lane in LANE_ORDER
            ],
        }
    )
    updated_metadata.setdefault("criteria", {})["newsMinimumScore"] = threshold
    updated_metadata.setdefault("sourceFingerprints", {})["news"] = hashlib.sha256(
        canonical_json(current_news).encode("utf-8")
    ).hexdigest()
    updated_metadata.setdefault("sourceGeneratedAt", {})["news"] = str(
        news_metadata.get("generatedAt") or ""
    )
    updated_metadata["datasetFingerprint"] = hashlib.sha256(
        canonical_json(updated_feed).encode("utf-8")
    ).hexdigest()
    return updated_feed, updated_metadata


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--today", type=Path, default=DEFAULT_TODAY)
    parser.add_argument("--news", type=Path, default=DEFAULT_NEWS)
    parser.add_argument("--transactions", type=Path, default=DEFAULT_TRANSACTIONS)
    parser.add_argument("--output", type=Path, default=DEFAULT_TODAY)
    parser.add_argument("--news-minimum-score", type=int)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    feed, metadata = read_today(args.today)
    news_items, news_metadata = read_news(args.news)
    transactions, _summary, _transaction_metadata = read_js(args.transactions)
    updated_feed, updated_metadata = update_today_news(
        feed,
        metadata,
        news_items,
        news_metadata,
        transaction_records(transactions),
        minimum_score=args.news_minimum_score,
    )
    write_today_feed(args.output, updated_feed, updated_metadata)
    print(
        "Today news lane: "
        f"{sum(item.get('kind') == 'entity_news' for item in updated_feed['placeChanges'])} "
        f"news changes, {len(updated_feed['opportunities'])} opportunities"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR {error}", file=sys.stderr)
        raise SystemExit(1)
