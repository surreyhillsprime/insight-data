#!/usr/bin/env python3
"""Reconcile derived INSPIRE coverage to the canonical property universe.

This intentionally does not rebuild or reinterpret the audited HMLR source,
parcel geometry, reviewed associations, or transition history. It can publish
only a coverage-only change, and validates both the existing and candidate
feeds fail-closed before replacing the runtime file atomically.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path

from collect_inspire_parcels import (
    GLOBAL_NAME,
    atomic_write,
    coverage_metadata,
    publication_time as resolve_publication_time,
)
from insight_data_utils import read_js
from runtime_release import finalise_body
from validate_inspire_parcels import (
    DEFAULT_ASSOCIATION_TRANSITIONS,
    DEFAULT_AUTHORITIES,
    DEFAULT_FEED,
    DEFAULT_REGISTRY,
    DEFAULT_TRANSACTIONS,
    feed_failures,
    parse_feed,
    registry_failures,
    retired_property_ids_from_metadata,
)
from validate_inspire_json_schemas import validator as schema_validator


COVERAGE_FAILURE_PREFIX = "feed coverage metadata does not reconcile:"
DEFAULT_FEED_SCHEMA = (
    Path(__file__).resolve().parents[1] / "config" / "inspire-parcels.schema.json"
)


def require_schema_valid(feed: dict, schema: dict, context: str) -> None:
    try:
        schema_validator()(feed, schema)
    except Exception as error:
        raise ValueError(
            f"{context} INSPIRE feed violates its published JSON Schema: {error}"
        ) from error


def reconcile_feed(
    feed_path: Path,
    *,
    canonical_ids: set[str],
    registry: dict,
    authorities: dict,
    feed_schema: dict,
    registry_sha256: str,
    configured_transitions: list[dict],
    retired_property_ids: set[str],
    publication_time: str | None = None,
) -> tuple[bool, dict]:
    """Atomically publish a coverage-only reconciliation when one is needed."""

    if not canonical_ids:
        raise ValueError("cannot reconcile INSPIRE coverage against an empty canonical property universe")

    feed = parse_feed(feed_path)
    require_schema_valid(feed, feed_schema, "existing")
    existing_failures = registry_failures(registry, canonical_ids)
    existing_failures.extend(
        feed_failures(
            feed,
            registry,
            authorities,
            canonical_ids,
            feed_path.stat().st_size,
            registry_sha256,
            configured_transitions,
            retired_property_ids,
        )
    )
    blocking_failures = [
        failure
        for failure in existing_failures
        if not failure.startswith(COVERAGE_FAILURE_PREFIX)
    ]
    if blocking_failures:
        raise ValueError(
            "cannot reconcile INSPIRE coverage while another contract failure exists: "
            + "; ".join(blocking_failures)
        )

    expected_coverage = coverage_metadata(
        canonical_ids,
        feed["associationsByProperty"],
    )
    if feed.get("coverage") == expected_coverage:
        return False, feed
    if not any(
        failure.startswith(COVERAGE_FAILURE_PREFIX)
        for failure in existing_failures
    ):
        raise ValueError(
            "refusing INSPIRE coverage rewrite because the mismatch was not isolated by validation"
        )

    core = deepcopy(feed)
    core.pop("generatedAt", None)
    core.pop("releaseId", None)
    core["coverage"] = expected_coverage

    source = core.get("source") if isinstance(core.get("source"), dict) else {}
    source_snapshot = str(source.get("sourceSnapshot") or "")
    if not re.fullmatch(r"hmlr-inspire-\d{4}-\d{2}-\d{2}", source_snapshot):
        raise ValueError("cannot derive release date from the audited HMLR source snapshot")

    body, _release_id = finalise_body(
        core,
        resolve_publication_time(feed_path, GLOBAL_NAME, core, publication_time),
        "inspire-parcels",
        source_snapshot.removeprefix("hmlr-inspire-"),
    )
    candidate = json.loads(body)
    content = GLOBAL_NAME + " = " + body + ";\n"
    require_schema_valid(candidate, feed_schema, "candidate")
    candidate_failures = registry_failures(registry, canonical_ids)
    candidate_failures.extend(
        feed_failures(
            candidate,
            registry,
            authorities,
            canonical_ids,
            len(content.encode("utf-8")),
            registry_sha256,
            configured_transitions,
            retired_property_ids,
        )
    )
    if candidate_failures:
        raise ValueError(
            "refusing invalid reconciled INSPIRE feed: "
            + "; ".join(candidate_failures)
        )

    return atomic_write(feed_path, content), candidate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("feed", nargs="?", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--authorities", type=Path, default=DEFAULT_AUTHORITIES)
    parser.add_argument("--transactions", type=Path, default=DEFAULT_TRANSACTIONS)
    parser.add_argument("--schema", type=Path, default=DEFAULT_FEED_SCHEMA)
    parser.add_argument(
        "--association-transitions",
        type=Path,
        default=DEFAULT_ASSOCIATION_TRANSITIONS,
    )
    args = parser.parse_args()

    try:
        transactions, _summary, metadata = read_js(args.transactions)
        canonical_ids = {
            row.get("propertyRecordId")
            for row in transactions
            if row.get("propertyRecordId")
        }
        registry = json.loads(args.registry.read_text(encoding="utf-8"))
        authorities = json.loads(args.authorities.read_text(encoding="utf-8"))
        feed_schema = json.loads(args.schema.read_text(encoding="utf-8"))
        transitions = json.loads(
            args.association_transitions.read_text(encoding="utf-8")
        ).get("records") or []
        changed, feed = reconcile_feed(
            args.feed,
            canonical_ids=canonical_ids,
            registry=registry,
            authorities=authorities,
            feed_schema=feed_schema,
            registry_sha256=hashlib.sha256(args.registry.read_bytes()).hexdigest(),
            configured_transitions=transitions,
            retired_property_ids=retired_property_ids_from_metadata(metadata),
        )
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}")
        return 1

    action = "Reconciled" if changed else "Already reconciled"
    coverage = feed["coverage"]
    print(
        f"{action} {args.feed}: {coverage['associatedProperties']:,}/"
        f"{coverage['canonicalProperties']:,} properties, "
        f"{coverage['coveragePercent']:.4f}% coverage"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
