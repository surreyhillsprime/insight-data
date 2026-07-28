#!/usr/bin/env python3
"""Validate INSIGHT's rights-gated, link-only news publication."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from insight_data_utils import parse_window_json
from news_sources import article_url_is_allowed, load_registry


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FEED = ROOT / "outputs" / "news-feed.js"
DEFAULT_SOURCES = ROOT / "config" / "news-sources.json"
PROHIBITED_ARTICLE_FIELDS = {
    "summary",
    "description",
    "body",
    "content",
    "image",
    "imageUrl",
    "_description",
}
DIAGNOSTIC_STATUSES = {"ok", "empty", "failed", "blocked", "not-modified"}


def parse_timestamp(value: object, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label} is not a valid ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed


def live_source(source: dict) -> bool:
    rights = source.get("rights", {})
    return (
        source.get("publicationMode") == "live"
        and rights.get("collectionStatus") == "approved"
        and rights.get("publicationStatus") == "approved"
        and rights.get("mode") == "link-only"
    )


def validate(
    path: Path,
    sources_path: Path | None = DEFAULT_SOURCES,
) -> tuple[list[dict], dict]:
    text = path.read_text(encoding="utf-8")
    if any(marker in text for marker in ("<<<<<<<", "=======", ">>>>>>>")):
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
    if metadata.get("articleCount") != len(items):
        raise ValueError("news articleCount does not reconcile")
    parse_timestamp(metadata.get("generatedAt"), "news generatedAt")
    parse_timestamp(
        metadata.get("lastCheckedAt", metadata.get("generatedAt")),
        "news lastCheckedAt",
    )

    registry = load_registry(sources_path) if sources_path is not None else None
    source_by_id = (
        {source["id"]: source for source in registry["sources"]}
        if registry
        else {}
    )
    required = {
        "id",
        "title",
        "url",
        "source",
        "sourceId",
        "publishedAt",
        "score",
        "scoreBase",
        "scoringVersion",
        "location",
        "matchType",
        "topics",
        "reason",
        "rightsMode",
        "publisherGroup",
        "lane",
    }
    ids: set[str] = set()
    urls: set[str] = set()
    source_counts: dict[str, int] = {}
    previous_order: tuple[str, int] | None = None
    for index, item in enumerate(items):
        if not isinstance(item, dict) or required - item.keys():
            raise ValueError(f"article {index} is missing required fields")
        if PROHIBITED_ARTICLE_FIELDS.intersection(item):
            raise ValueError(f"article {index} contains unlicensed content fields")
        if (
            not re.fullmatch(r"news-[0-9a-f]{20}", str(item["id"]))
            or item["id"] in ids
        ):
            raise ValueError(f"article {index} has an invalid or duplicate id")
        parts = urlsplit(str(item["url"]))
        if (
            parts.scheme != "https"
            or not parts.netloc
            or item["url"] in urls
        ):
            raise ValueError(f"article {index} has an invalid or duplicate URL")
        if not str(item.get("title") or "").strip():
            raise ValueError(f"article {index} has an empty title")
        if (
            not isinstance(item["score"], int)
            or not 0 <= item["score"] <= 100
            or not isinstance(item["scoreBase"], int)
            or not 0 <= item["scoreBase"] <= 100
        ):
            raise ValueError(f"article {index} has an invalid score")
        if item["scoringVersion"] != metadata.get("scoringVersion"):
            raise ValueError(f"article {index} uses a stale scoring version")
        if item["rightsMode"] != "link-only":
            raise ValueError(f"article {index} is not link-only")
        parse_timestamp(item["publishedAt"], f"article {index} publishedAt")
        order = (str(item["publishedAt"]), int(item["score"]))
        if previous_order is not None and order > previous_order:
            raise ValueError("news articles must be ordered newest first")
        previous_order = order
        if not isinstance(item.get("topics"), list) or not item["topics"]:
            raise ValueError(f"article {index} has no topics")

        if registry:
            source = source_by_id.get(str(item["sourceId"]))
            if not source or not live_source(source):
                raise ValueError(
                    f"article {index} is not from a rights-approved live source"
                )
            if not article_url_is_allowed(source, str(item["url"])):
                raise ValueError(
                    f"article {index} is outside its source articleHosts"
                )
            if item.get("source") != source.get("name"):
                raise ValueError(f"article {index} source name does not reconcile")
            if item.get("publisherGroup") != source.get("publisherGroup"):
                raise ValueError(
                    f"article {index} publisherGroup does not reconcile"
                )
            if item.get("lane") != source.get("lane"):
                raise ValueError(f"article {index} lane does not reconcile")

        ids.add(str(item["id"]))
        urls.add(str(item["url"]))
        source_id = str(item["sourceId"])
        source_counts[source_id] = source_counts.get(source_id, 0) + 1

    newest = max((str(item["publishedAt"]) for item in items), default=None)
    if metadata.get("newestPublishedAt") != newest:
        raise ValueError("news newestPublishedAt does not reconcile")

    diagnostics = metadata.get("sourceDiagnostics")
    if not isinstance(diagnostics, list):
        raise ValueError("news sourceDiagnostics must be an array")
    diagnostic_by_id: dict[str, dict] = {}
    for index, diagnostic in enumerate(diagnostics):
        if not isinstance(diagnostic, dict):
            raise ValueError(f"source diagnostic {index} must be an object")
        source_id = str(diagnostic.get("sourceId") or "")
        if not source_id or source_id in diagnostic_by_id:
            raise ValueError("source diagnostics contain a missing or duplicate sourceId")
        if diagnostic.get("status") not in DIAGNOSTIC_STATUSES:
            raise ValueError(f"{source_id}: invalid diagnostic status")
        for field in (
            "consecutiveFailures",
            "discovered",
            "parsed",
            "eligible",
            "qualified",
            "retained",
            "durationMs",
        ):
            if not isinstance(diagnostic.get(field), int) or diagnostic[field] < 0:
                raise ValueError(f"{source_id}: invalid diagnostic {field}")
        if diagnostic["retained"] != source_counts.get(source_id, 0):
            raise ValueError(f"{source_id}: retained count does not reconcile")
        if diagnostic.get("status") == "failed" and not diagnostic.get("errorCode"):
            raise ValueError(f"{source_id}: failed diagnostic lacks errorCode")
        if diagnostic.get("status") != "failed" and (
            diagnostic.get("errorCode") or diagnostic.get("errorMessage")
        ):
            raise ValueError(f"{source_id}: healthy diagnostic contains an error")
        diagnostic_by_id[source_id] = diagnostic

    if registry:
        expected_ids = {source["id"] for source in registry["sources"]}
        if set(diagnostic_by_id) != expected_ids:
            raise ValueError("source diagnostics do not cover the complete registry")
        live_sources = [source for source in registry["sources"] if live_source(source)]
        if metadata.get("sourcesConfigured") != len(live_sources):
            raise ValueError("sourcesConfigured does not reconcile to live sources")
        fetched = sum(
            diagnostic_by_id[source["id"]]["status"]
            in {"ok", "empty", "not-modified"}
            for source in live_sources
        )
        if metadata.get("sourcesFetched") != fetched:
            raise ValueError("sourcesFetched does not reconcile to diagnostics")

    errors = metadata.get("sourceErrors")
    if not isinstance(errors, list):
        raise ValueError("sourceErrors must be an array")
    failed_ids = {
        source_id
        for source_id, diagnostic in diagnostic_by_id.items()
        if diagnostic.get("status") == "failed"
    }
    error_ids = {
        str(error.get("sourceId"))
        for error in errors
        if isinstance(error, dict)
    }
    if error_ids != failed_ids:
        raise ValueError("sourceErrors do not reconcile to failed diagnostics")
    return items, metadata


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("feed", nargs="?", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    try:
        items, metadata = validate(args.feed, args.sources)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR {error}", file=sys.stderr)
        return 1
    print(
        "OK news feed "
        f"({len(items)} link-only articles, "
        f"checked {metadata.get('lastCheckedAt', 'unknown')})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
