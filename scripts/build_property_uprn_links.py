#!/usr/bin/env python3
"""Initialise the fail-closed future property-to-UPRN runtime feed."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from runtime_release import finalise_body


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "outputs" / "property-uprn-links.js"
GLOBAL_NAME = "window.INSIGHT_PROPERTY_UPRN_LINKS"


def empty_payload() -> dict:
    return {
        "schemaVersion": 1,
        "canonicalIdentityMode": "full-normalised-address-plus-postcode-fail-closed",
        "identityWarning": "UPRN evidence never creates, merges or replaces canonical INSIGHT property identity",
        "sources": [],
        "linksByProperty": {},
    }


def empty_feed() -> dict:
    payload = empty_payload()
    generated_at = "2026-08-10T00:00:00Z"
    body, _release_id = finalise_body(payload, generated_at, "property-uprn-links", generated_at[:10])
    return json.loads(body)


def atomic_write(path: Path, content: str) -> bool:
    encoded = content.encode("utf-8")
    if path.exists() and path.read_bytes() == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    payload = empty_payload()
    generated_at = "2026-08-10T00:00:00Z"
    body, _release_id = finalise_body(payload, generated_at, "property-uprn-links", generated_at[:10])
    content = GLOBAL_NAME + " = " + body + ";\n"
    changed = atomic_write(args.output, content)
    print(f"{'Wrote' if changed else 'Unchanged'} {args.output} (empty fail-closed UPRN stream)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
