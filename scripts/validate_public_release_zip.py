#!/usr/bin/env python3
"""Fail closed when the tracked macOS package crosses public data boundaries."""

from __future__ import annotations

import argparse
import json
import plistlib
import re
import sys
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ZIP = ROOT / "downloads" / "INSIGHT-macOS.zip"
MAX_PUBLIC_ZIP_BYTES = 15 * 1024 * 1024
APP_ROOT = "INSIGHT.app/Contents"


def assignment(text: str, name: str):
    matches = re.findall(
        rf"^window\.{re.escape(name)}\s*=\s*(.*);$",
        text,
        flags=re.M,
    )
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one window.{name} assignment")
    return json.loads(matches[0])


def validate(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise ValueError(f"Public macOS package is missing: {path}")
    if path.stat().st_size > MAX_PUBLIC_ZIP_BYTES:
        raise ValueError("Public macOS package exceeds the reviewed 15 MiB limit")

    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        required = {
            f"{APP_ROOT}/Info.plist",
            f"{APP_ROOT}/Resources/web/planning-history.js",
            f"{APP_ROOT}/Resources/web/property-records.js",
            "OPEN-INSIGHT-FIRST-PUBLIC.txt",
        }
        missing = sorted(required - names)
        if missing:
            raise ValueError(
                "Public macOS package is missing required members: "
                + ", ".join(missing)
            )
        if any(
            name.startswith("/")
            or ".." in Path(name).parts
            or name.startswith("__MACOSX/")
            for name in names
        ):
            raise ValueError("Public macOS package contains an unsafe archive path")

        info = plistlib.loads(archive.read(f"{APP_ROOT}/Info.plist"))
        expected_info = {
            "CFBundleShortVersionString": "2.0.0",
            "CFBundleVersion": "37",
            "INSIGHTReleaseProfile": "public",
            "INSIGHTPlanningFeedMode": "remote",
            "INSIGHTSalesHistoryFeedMode": "remote",
        }
        mismatched = {
            key: (info.get(key), expected)
            for key, expected in expected_info.items()
            if str(info.get(key)) != expected
        }
        if mismatched:
            raise ValueError(f"Public macOS package profile mismatch: {mismatched}")

        planning = archive.read(
            f"{APP_ROOT}/Resources/web/planning-history.js"
        )
        if len(planning) > 2_048:
            raise ValueError("Public planning placeholder exceeds 2 KiB")
        planning_text = planning.decode("utf-8")
        with tempfile.TemporaryDirectory(prefix="insight-public-planning-") as directory:
            planning_path = Path(directory) / "planning-history.js"
            planning_path.write_text(planning_text, encoding="utf-8")
            sys.path.insert(0, str(ROOT / "scripts"))
            from validate_planning_feed import validate as validate_planning

            result = validate_planning(planning_path, allow_blocked=True)
        if result.get("publicationStatus") != "blocked-missing-licensed-source":
            raise ValueError("Public macOS package contains a populated planning feed")

        property_records_text = archive.read(
            f"{APP_ROOT}/Resources/web/property-records.js"
        ).decode("utf-8")
        property_metadata = assignment(
            property_records_text,
            "SURREY_PROPERTY_RECORDS_META",
        )
        runtime = property_metadata.get("runtimeProjection")
        if (
            not isinstance(runtime, dict)
            or runtime.get("schemaVersion") != 1
            or runtime.get("mode") != "app-required-fields"
            or runtime.get("propertyCount") != property_metadata.get("propertyCount")
        ):
            raise ValueError(
                "Public macOS package lacks the reviewed Property Records projection"
            )

    return {
        "version": info["CFBundleShortVersionString"],
        "build": info["CFBundleVersion"],
        "propertyCount": property_metadata["propertyCount"],
        "zipBytes": path.stat().st_size,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_ZIP)
    args = parser.parse_args()
    result = validate(args.path)
    print(
        "Valid public INSIGHT package "
        f"(v{result['version']} build {result['build']}, "
        f"{result['propertyCount']:,} properties, "
        f"{result['zipBytes']:,} bytes)"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as error:
        print(f"Public INSIGHT package validation failed: {error}", file=sys.stderr)
        raise SystemExit(1)
