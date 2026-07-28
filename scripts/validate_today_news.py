#!/usr/bin/env python3
"""Validate the atomic news-to-Today projection used by the news workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from today_feed import LANE_ORDER, SUMMARY_LABELS, canonical_json
from update_today_news import DEFAULT_NEWS, DEFAULT_TODAY, read_news, read_today


def validate_projection(today_path: Path, news_path: Path) -> tuple[dict, dict]:
    feed, metadata = read_today(today_path)
    news_items, news_metadata = read_news(news_path)
    if feed.get("schemaVersion") != 1 or metadata.get("schemaVersion") != 1:
        raise ValueError("Today schemaVersion must remain 1")
    counts = {
        lane: len(feed.get(lane, []))
        for lane in LANE_ORDER
        if isinstance(feed.get(lane), list)
    }
    if len(counts) != len(LANE_ORDER) or metadata.get("counts") != counts:
        raise ValueError("Today lane counts do not reconcile")
    expected_summary = [
        {"id": lane, "label": SUMMARY_LABELS[lane], "count": counts[lane]}
        for lane in LANE_ORDER
    ]
    if metadata.get("summary") != expected_summary:
        raise ValueError("Today summary does not reconcile")
    expected_dataset = hashlib.sha256(canonical_json(feed).encode("utf-8")).hexdigest()
    if metadata.get("datasetFingerprint") != expected_dataset:
        raise ValueError("Today dataset fingerprint does not reconcile")
    current_news = sorted(news_items, key=canonical_json)
    expected_news = hashlib.sha256(
        canonical_json(current_news).encode("utf-8")
    ).hexdigest()
    if metadata.get("sourceFingerprints", {}).get("news") != expected_news:
        raise ValueError("Today news fingerprint is not current")
    if metadata.get("sourceGeneratedAt", {}).get("news") != news_metadata.get("generatedAt"):
        raise ValueError("Today news generatedAt is not current")
    news_ids = {str(item.get("id")) for item in news_items}
    for change in feed.get("placeChanges", []):
        if change.get("kind") != "entity_news":
            continue
        if change.get("rightsMode") != "link-only":
            raise ValueError("Today entity news must remain link-only")
        if str(change.get("attributes", {}).get("newsId")) not in news_ids:
            raise ValueError("Today contains an entity-news item absent from current news")
    return feed, metadata


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--today", type=Path, default=DEFAULT_TODAY)
    parser.add_argument("--news", type=Path, default=DEFAULT_NEWS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    try:
        feed, _metadata = validate_projection(args.today, args.news)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR {error}", file=sys.stderr)
        return 1
    print(
        "OK Today news projection "
        f"({len(feed['placeChanges'])} place changes, "
        f"{len(feed['opportunities'])} opportunities)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
