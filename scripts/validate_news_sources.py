#!/usr/bin/env python3
"""Validate the INSIGHT news source registry and its rights gates."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from news_sources import DEFAULT_REGISTRY, DEFAULT_SCHEMA, load_registry


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "registry",
        nargs="?",
        type=Path,
        default=DEFAULT_REGISTRY,
        help="News source registry JSON.",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=DEFAULT_SCHEMA,
        help="News source registry JSON Schema.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    try:
        registry = load_registry(args.registry, args.schema)
    except ValueError as error:
        print(f"News source registry validation failed: {error}", file=sys.stderr)
        return 1

    sources = registry["sources"]
    counts = {
        mode: sum(1 for source in sources if source["publicationMode"] == mode)
        for mode in ("live", "shadow", "disabled")
    }
    print(
        "OK news source registry "
        f"({len(sources)} sources; {counts['live']} live, "
        f"{counts['shadow']} shadow, {counts['disabled']} disabled)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
