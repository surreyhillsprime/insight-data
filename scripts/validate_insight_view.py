#!/usr/bin/env python3
"""Validate the canonical daily INSIGHT View JavaScript asset."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from insight_view import (
    GENERATOR_VERSION,
    META_NAME,
    SCHEMA_VERSION,
    VIEW_NAME,
    InsightViewValidationError,
    clean,
    iso_date,
    iso_datetime,
    validate_insight_view,
)
from property_records import parse_window_assignment
from validate_property_records import _validate_json_schema


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FEED = ROOT / "outputs" / "insight-view.js"
DEFAULT_SCHEMA = ROOT / "config" / "insight-view.schema.json"
MAXIMUM_BYTES = 128_000


def read_insight_view(path: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    source = Path(path)
    data = source.read_bytes()
    if not data or len(data) > MAXIMUM_BYTES:
        raise InsightViewValidationError("INSIGHT View asset is empty or too large")
    text = data.decode("utf-8")
    names = re.findall(r"(?m)^window\.([A-Z0-9_]+)\s*=", text)
    nonempty = [line for line in text.splitlines() if line.strip()]
    if names != [VIEW_NAME, META_NAME] or len(nonempty) != 2:
        raise InsightViewValidationError(
            f"INSIGHT View must contain exactly window.{VIEW_NAME} and window.{META_NAME}"
        )
    view = parse_window_assignment(text, VIEW_NAME, None)
    metadata = parse_window_assignment(text, META_NAME, None)
    if not isinstance(view, dict) or not isinstance(metadata, dict):
        raise InsightViewValidationError("INSIGHT View assignments must be objects")
    return view, metadata


def validate_metadata(view: dict[str, Any], metadata: dict[str, Any]) -> None:
    expected_keys = {
        "schemaVersion",
        "asOf",
        "generatedAt",
        "generatorVersion",
        "datasetFingerprint",
        "sourceCount",
        "staleSources",
    }
    if set(metadata) != expected_keys:
        raise InsightViewValidationError("INSIGHT View metadata fields are invalid")
    if (
        metadata.get("schemaVersion") != SCHEMA_VERSION
        or metadata.get("generatorVersion") != GENERATOR_VERSION
        or metadata.get("asOf") != view.get("briefingDate")
        or metadata.get("generatedAt") != view.get("generatedAt")
        or metadata.get("datasetFingerprint") != view.get("fingerprint")
        or metadata.get("sourceCount") != len(view.get("sources") or [])
        or metadata.get("staleSources") != view.get("staleSources")
        or not iso_date(metadata.get("asOf"))
        or not iso_datetime(metadata.get("generatedAt"))
        or not re.fullmatch(r"[0-9a-f]{64}", clean(metadata.get("datasetFingerprint")))
    ):
        raise InsightViewValidationError("INSIGHT View metadata does not reconcile")


def validate(path: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    view, metadata = read_insight_view(path)
    schema = json.loads(DEFAULT_SCHEMA.read_text(encoding="utf-8"))
    _validate_json_schema(view, schema, schema, "$")
    validate_insight_view(view)
    validate_metadata(view, metadata)
    return view, metadata


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("feed", nargs="?", type=Path, default=DEFAULT_FEED)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    view, metadata = validate(args.feed)
    print(
        "OK INSIGHT View "
        f"({metadata['asOf']}, {view['policy']['bankRate']:.2f}% Bank Rate, "
        f"{view['mortgage']['rate']:.2f}% two-year 75% LTV)"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError, InsightViewValidationError) as error:
        print(f"INSIGHT View validation failed: {error}", file=sys.stderr)
        raise SystemExit(1)
