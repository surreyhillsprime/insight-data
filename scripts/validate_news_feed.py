#!/usr/bin/env python3
"""Validate an INSIGHT link-only news feed."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from insight_data_utils import parse_window_json


def validate(path: Path) -> tuple[list[dict], dict]:
    text = path.read_text(encoding="utf-8")
    if "<<<<<<<" in text or "=======" in text or ">>>>>>>" in text:
        raise ValueError("news feed contains conflict markers")
    items = parse_window_json(text, "INSIGHT_NEWS_ITEMS", None)
    metadata = parse_window_json(text, "INSIGHT_NEWS_META", None)
    if not isinstance(items, list) or not isinstance(metadata, dict):
        raise ValueError("news items must be an array and metadata must be an object")
    if metadata.get("schemaVersion") != 1:
        raise ValueError("news feed schemaVersion must be 1")
    if metadata.get("rightsMode") != "link-only":
        raise ValueError("news feed must remain link-only")
    if len(items) > 100:
        raise ValueError("news feed exceeds 100 articles")
    required = {"id", "title", "url", "source", "sourceId", "publishedAt", "score", "scoringVersion", "location", "topics", "reason", "rightsMode"}
    ids = set()
    urls = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict) or required - item.keys():
            raise ValueError(f"article {index} is missing required fields")
        if not re.fullmatch(r"news-[0-9a-f]{20}", str(item["id"])) or item["id"] in ids:
            raise ValueError(f"article {index} has an invalid or duplicate id")
        parts = urlsplit(str(item["url"]))
        if parts.scheme != "https" or not parts.netloc or item["url"] in urls:
            raise ValueError(f"article {index} has an invalid or duplicate URL")
        if not isinstance(item["score"], int) or not 0 <= item["score"] <= 100:
            raise ValueError(f"article {index} has an invalid score")
        if item["scoringVersion"] != metadata.get("scoringVersion"):
            raise ValueError(f"article {index} uses a stale scoring version")
        if item["rightsMode"] != "link-only" or "summary" in item or "image" in item or "body" in item:
            raise ValueError(f"article {index} contains unlicensed content fields")
        ids.add(item["id"])
        urls.add(item["url"])
    return items, metadata


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "outputs/news-feed.js"
    try:
        items, metadata = validate(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    print(f"OK news feed ({len(items)} link-only articles, generated {metadata.get('generatedAt', 'unknown')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
