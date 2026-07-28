#!/usr/bin/env python3
"""Check INSIGHT news pipeline freshness without treating a quiet news day as failure."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from news_sources import load_registry
from validate_news_feed import DEFAULT_FEED, DEFAULT_SOURCES, validate


def parse_timestamp(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("news generatedAt is not a valid ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("news generatedAt must include a timezone")
    return parsed.astimezone(timezone.utc)


def check(
    feed_path: Path,
    sources_path: Path,
    *,
    now: datetime | None = None,
    maximum_pipeline_age_minutes: int = 90,
    editorial_warning_days: int = 7,
) -> tuple[list[str], list[str], dict]:
    items, metadata = validate(feed_path, sources_path)
    checked_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    generated_at = parse_timestamp(metadata.get("generatedAt"))
    age_minutes = max(0, int((checked_at - generated_at).total_seconds() // 60))
    failures: list[str] = []
    warnings: list[str] = []
    if age_minutes > maximum_pipeline_age_minutes:
        failures.append(
            f"pipeline is {age_minutes} minutes old "
            f"(maximum {maximum_pipeline_age_minutes})"
        )

    newest = max((parse_timestamp(item.get("publishedAt")) for item in items), default=None)
    editorial_age_days = (
        max(0, int((checked_at - newest).total_seconds() // 86400))
        if newest
        else None
    )
    if editorial_age_days is None:
        warnings.append("no qualifying articles are currently published")
    elif editorial_age_days > editorial_warning_days:
        warnings.append(
            f"newest qualifying article is {editorial_age_days} days old; "
            "the pipeline itself is still healthy"
        )

    unhealthy = [
        diagnostic.get("sourceId")
        for diagnostic in metadata.get("sourceDiagnostics", [])
        if diagnostic.get("publicationMode") == "live"
        and diagnostic.get("status") in {"failed", "empty"}
    ]
    if unhealthy:
        warnings.append("live source attention: " + ", ".join(sorted(map(str, unhealthy))))
    registry = load_registry(sources_path)
    source_by_id = {source["id"]: source for source in registry["sources"]}
    stale_sources = []
    for diagnostic in metadata.get("sourceDiagnostics", []):
        if diagnostic.get("publicationMode") != "live" or diagnostic.get("status") != "ok":
            continue
        published = diagnostic.get("newestPublishedAt")
        source = source_by_id.get(str(diagnostic.get("sourceId"))) or {}
        if not published or not source:
            continue
        source_age_minutes = max(
            0,
            int(
                (checked_at - parse_timestamp(published)).total_seconds()
                // 60
            ),
        )
        warning_after = max(
            2 * 1440,
            int(source.get("expectedCadenceMinutes", 1440)) * 3,
        )
        if source_age_minutes > warning_after:
            stale_sources.append(str(diagnostic.get("sourceId")))
    if stale_sources:
        warnings.append(
            "source editorial cadence is stale: " + ", ".join(sorted(stale_sources))
        )
    return failures, warnings, {
        "pipelineAgeMinutes": age_minutes,
        "editorialAgeDays": editorial_age_days,
        "generatedAt": metadata.get("generatedAt"),
        "newestPublishedAt": metadata.get("newestPublishedAt"),
    }


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--maximum-pipeline-age-minutes", type=int, default=90)
    parser.add_argument("--editorial-warning-days", type=int, default=7)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    try:
        failures, warnings, metrics = check(
            args.feed,
            args.sources,
            maximum_pipeline_age_minutes=args.maximum_pipeline_age_minutes,
            editorial_warning_days=args.editorial_warning_days,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR {error}", file=sys.stderr)
        return 1
    for warning in warnings:
        print(f"WARNING {warning}")
    if failures:
        for failure in failures:
            print(f"ERROR {failure}", file=sys.stderr)
        return 1
    print(
        "OK news pipeline "
        f"({metrics['pipelineAgeMinutes']} minutes old; "
        f"newest article {metrics['newestPublishedAt'] or 'unavailable'})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
