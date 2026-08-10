"""Cross-language-stable raw-byte release IDs for minified runtime JSON feeds."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


def finalise_body(core_payload: dict, generated_at: str, release_prefix: str, release_date: str) -> tuple[str, str]:
    if "releaseId" in core_payload or "generatedAt" in core_payload:
        raise ValueError("generatedAt/releaseId must be appended only after hashing the raw core payload")
    raw_payload = json.dumps(core_payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    digest = hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()[:12]
    release_id = f"{release_prefix}-{release_date}-{digest}"
    body = (
        raw_payload[:-1]
        + ",\"generatedAt\":" + json.dumps(generated_at)
        + ",\"releaseId\":" + json.dumps(release_id)
        + "}"
    )
    return body, release_id


def parse_runtime(path: Path, global_name: str) -> tuple[dict, str, str]:
    text = path.read_text(encoding="utf-8").strip()
    prefix = global_name + " = "
    if not text.startswith(prefix) or not text.endswith(";"):
        raise ValueError(f"{path} must assign exactly {global_name}")
    body = text[len(prefix):-1]
    match = re.search(r',"generatedAt":"([^"\\]+)","releaseId":"([^"\\]+)"}$', body)
    if match is None:
        raise ValueError(f"{path} must publish releaseId as the last top-level member")
    raw_payload = body[:match.start()] + "}"
    digest = hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()[:12]
    release_id = match.group(2)
    if not release_id.endswith("-" + digest):
        raise ValueError(f"{path} releaseId digest does not match its exact raw payload bytes")
    return json.loads(body), raw_payload, digest
