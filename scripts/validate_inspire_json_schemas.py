#!/usr/bin/env python3
"""Execute every INSPIRE/UPRN JSON Schema against its tracked publication."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from validate_inspire_parcels import parse_feed
from validate_inspire_parcel_review_queue import parse_queue
from validate_property_uprn_links import parse_feed as parse_uprn_feed


ROOT = Path(__file__).resolve().parents[1]


def is_utc_second_timestamp(value: object) -> bool:
    """Deterministic date-time format check independent of optional packages."""

    if not isinstance(value, str):
        return True  # JSON Schema's type keyword reports non-string values.
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def validator():
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError:
        from json_schema_subset import validate
        return lambda instance, schema: validate(instance, schema)
    checker = FormatChecker()
    # Plain jsonschema does not install its optional RFC3339 dependency. In
    # that environment an unregistered `date-time` format silently succeeds,
    # so register INSIGHT's stricter UTC-seconds contract unconditionally.
    checker.checks("date-time")(is_utc_second_timestamp)
    return lambda instance, schema: Draft202012Validator(schema, format_checker=checker).validate(instance)


def main() -> int:
    validate = validator()
    contracts = [
        ("config/inspire-parcel-associations.json", "config/inspire-parcel-associations.schema.json", lambda path: json.loads(path.read_text())),
        ("outputs/inspire-parcels.js", "config/inspire-parcels.schema.json", parse_feed),
        ("outputs/property-uprn-links.js", "config/property-uprn-links.schema.json", parse_uprn_feed),
        ("outputs/inspire-parcel-review-queue.js", "config/inspire-parcel-review-queue.schema.json", parse_queue),
        ("config/inspire-association-transitions.json", "config/inspire-association-transitions.schema.json", lambda path: json.loads(path.read_text())),
    ]
    for data_name, schema_name, loader in contracts:
        data_path, schema_path = ROOT / data_name, ROOT / schema_name
        validate(loader(data_path), json.loads(schema_path.read_text(encoding="utf-8")))
        print(f"Validated {data_name} against {schema_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
