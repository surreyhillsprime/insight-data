#!/usr/bin/env python3
"""Build the browser-safe private-estate registry asset."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Dict, Mapping

from private_estates import GEOMETRY_PATH, REGISTRY_PATH, ROOT, compile_registry, read_geometry, read_registry


DEFAULT_OUTPUT = ROOT / "outputs" / "private-estates.js"


def build_asset_payload(
    registry: Mapping[str, Any] | None = None,
    geometry_manifest: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    source_registry = dict(registry or read_registry())
    geometry = dict(geometry_manifest or read_geometry())
    compiled = compile_registry(source_registry)

    if geometry.get("registryVersion") != compiled.get("registryVersion"):
        raise ValueError("Private-estate registry and geometry manifest versions do not match")
    if registry is None:
        source_hash = hashlib.sha256(REGISTRY_PATH.read_bytes()).hexdigest()
        if geometry.get("sourceRegistrySha256") != source_hash:
            raise ValueError("Private-estate geometry manifest does not match the source registry hash")

    definitions_by_id = {item["estateId"]: item for item in compiled["definitions"]}
    navigation_anchors = []
    approved_pins = []
    for item in geometry.get("entities", []):
        estate_id = item.get("estateId")
        definition = definitions_by_id.get(estate_id)
        if not definition:
            continue

        label_point = item.get("labelPoint")
        label_geometry = label_point.get("geometry") if isinstance(label_point, Mapping) else None
        coordinates = label_geometry.get("coordinates") if isinstance(label_geometry, Mapping) else None
        if (
            isinstance(label_geometry, Mapping)
            and label_geometry.get("type") == "Point"
            and isinstance(coordinates, list)
            and len(coordinates) == 2
            and all(isinstance(value, (int, float)) and math.isfinite(value) for value in coordinates)
        ):
            navigation_anchors.append({
                "estateId": estate_id,
                "name": definition["name"],
                "classification": definition["classification"],
                "estateType": definition["estateType"],
                "geometry": label_geometry,
                "readiness": label_point.get("readiness", "candidate"),
                "anchorBasis": label_point.get("anchorBasis", ""),
                "sourceRefs": label_point.get("sourceRefs", []),
                "navigationOnly": True,
                "legalExtent": False,
            })
        if (
            isinstance(label_point, Mapping)
            and label_point.get("approvedForInstall") is True
            and label_point.get("readiness") == "approved"
            and isinstance(label_point.get("geometry"), Mapping)
        ):
            approved_pins.append({
                "estateId": estate_id,
                "name": definition["name"],
                "classification": definition["classification"],
                "geometry": label_point["geometry"],
                "anchorBasis": label_point.get("anchorBasis", ""),
                "sourceRefs": label_point.get("sourceRefs", []),
            })

    navigation_ids = {item["estateId"] for item in navigation_anchors}
    missing_navigation_ids = sorted(set(definitions_by_id) - navigation_ids)
    if missing_navigation_ids:
        raise ValueError(f"Active estates lack navigation anchors: {', '.join(missing_navigation_ids)}")

    metadata = dict(compiled["metadata"])
    metadata.update({
        "registryVersion": compiled["registryVersion"],
        "geometryVersion": geometry.get("geometryVersion", ""),
        "geometryEntityCount": len(geometry.get("entities", [])),
        "approvedPinCount": len(approved_pins),
        "navigationAnchorCount": len(navigation_anchors),
        "candidatePinsExcluded": sum(
            1
            for item in geometry.get("entities", [])
            if isinstance(item.get("labelPoint"), Mapping)
            and item["labelPoint"].get("approvedForInstall") is not True
        ),
    })
    return {
        "schemaVersion": 1,
        "registryVersion": compiled["registryVersion"],
        "geometryVersion": geometry.get("geometryVersion", ""),
        "asOf": compiled["asOf"],
        "activeInstallStatuses": compiled["activeInstallStatuses"],
        "definitions": compiled["definitions"],
        "rules": compiled["rules"],
        "navigationAnchors": navigation_anchors,
        "approvedPins": approved_pins,
        "metadata": metadata,
    }


def write_asset(path: Path = DEFAULT_OUTPUT) -> Dict[str, Any]:
    payload = build_asset_payload()
    content = "\n".join([
        "window.INSIGHT_PRIVATE_ESTATES = " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";",
        "window.INSIGHT_PRIVATE_ESTATE_REGISTRY = window.INSIGHT_PRIVATE_ESTATES;",
        "",
    ])
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build INSIGHT's installed private-estate browser asset.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = write_asset(Path(args.output))
    print(f"Private-estate definitions: {payload['metadata']['activeDefinitionCount']}")
    print(f"Private-estate rules: {payload['metadata']['activeRuleCount']}")
    print(f"Navigation anchors: {payload['metadata']['navigationAnchorCount']}")
    print(f"Approved pins: {payload['metadata']['approvedPinCount']}")
    print(f"Updated {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
