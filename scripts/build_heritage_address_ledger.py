#!/usr/bin/env python3
"""Build the complete reviewed heritage ledger for the current property universe.

The compact audit records the address/document decisions and their evidence.
This script expands those decisions to one deterministic production mapping per
canonical INSIGHT property. Properties without a supported direct NHLE identity
are published as ``no_direct_match``; this is not a legal assertion that the
property is unlisted or outside the curtilage of a listed building.
"""

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path

from enrich_listed_buildings import (
    DEFAULT_INPUT_JS,
    DEFAULT_OVERRIDES,
    build_properties,
    load_overrides,
)
from insight_data_utils import clean, property_record_id, read_js


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUDIT = ROOT / "config" / "heritage-listing-address-audit.json"


def sha256_lines(values):
    return hashlib.sha256("\n".join(sorted(values)).encode("utf-8")).hexdigest()


def load_audit(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("auditVersion") != 1:
        raise ValueError("Heritage address auditVersion must be 1")
    if not clean(payload.get("reviewedBy")) or not clean(payload.get("reviewedAt")):
        raise ValueError("Heritage address audit must record reviewedBy and reviewedAt")
    for field in ("confirmedMappings", "noDirectMappings", "unknownMappings"):
        if not isinstance(payload.get(field), list):
            raise ValueError(f"Heritage address audit {field} must be an array")
    return payload


def indexed_decisions(audit):
    decisions = {}
    for status, field in (
        ("confirmed_listed", "confirmedMappings"),
        ("no_direct_match", "noDirectMappings"),
        ("unknown", "unknownMappings"),
    ):
        for item in audit[field]:
            if not isinstance(item, dict):
                raise ValueError(f"Heritage address audit {field} entries must be objects")
            record_id = clean(item.get("propertyRecordId"))
            if not record_id:
                raise ValueError(f"Heritage address audit {field} entry has no propertyRecordId")
            if record_id in decisions:
                raise ValueError(f"Duplicate heritage address decision for {record_id}")
            entries = [clean(value) for value in item.get("listEntryNumbers", [])]
            if status == "confirmed_listed" and not entries:
                raise ValueError(f"Confirmed heritage decision {record_id} has no NHLE entry")
            if status != "confirmed_listed" and entries:
                raise ValueError(
                    f"Non-confirmed heritage decision {record_id} carries NHLE entries"
                )
            decisions[record_id] = {
                **item,
                "status": status,
                "listEntryNumbers": entries,
            }
    return decisions


def representative_address(property_data):
    item = property_data["item"]
    address = clean(item.get("address"))
    postcode = clean(item.get("postcode"))
    if property_record_id({"address": address, "postcode": postcode}) != property_data["recordId"]:
        raise ValueError(
            f"Representative address does not reproduce {property_data['recordId']}"
        )
    return address, postcode


def build_payload(transactions, audit):
    properties = build_properties(transactions)
    decisions = indexed_decisions(audit)
    unknown_ids = sorted(set(decisions) - set(properties))
    if unknown_ids:
        raise ValueError(
            "Heritage address audit contains properties outside the canonical universe: "
            + ", ".join(unknown_ids[:5])
        )

    property_digest = sha256_lines(properties)
    expected_property_digest = clean(audit.get("canonicalPropertyDigest"))
    if expected_property_digest and property_digest != expected_property_digest:
        raise ValueError(
            "Canonical property universe changed since the address audit; "
            "new properties must remain fail-closed until screened"
        )

    confirmed_pairs = [
        f"{record_id}|{number}"
        for record_id, item in decisions.items()
        if item["status"] == "confirmed_listed"
        for number in item["listEntryNumbers"]
    ]
    pair_digest = sha256_lines(confirmed_pairs)
    if pair_digest != clean(audit.get("confirmedPairDigest")):
        raise ValueError("Heritage address audit confirmedPairDigest does not match")

    generic_no_direct_note = (
        "Exhaustive current Surrey NHLE address-corpus screen found no supported "
        "direct property identity. This is not legal proof that the property is "
        "unlisted or outside listed-building curtilage."
    )
    mappings = []
    for record_id, property_data in sorted(properties.items()):
        address, postcode = representative_address(property_data)
        decision = decisions.get(record_id, {
            "status": "no_direct_match",
            "listEntryNumbers": [],
            "note": generic_no_direct_note,
        })
        note = clean(decision.get("note")) or generic_no_direct_note
        if (
            decision["status"] == "no_direct_match"
            and "not legal proof" not in note.lower()
        ):
            note += (
                " This is not legal proof that the property is unlisted or "
                "outside listed-building curtilage."
            )
        mapping = {
            "propertyRecordId": record_id,
            "address": address,
            "postcode": postcode,
            "status": decision["status"],
            "listEntryNumbers": decision["listEntryNumbers"],
            "reviewedBy": clean(decision.get("reviewedBy")) or audit["reviewedBy"],
            "reviewedAt": clean(decision.get("reviewedAt")) or audit["reviewedAt"],
            "note": note,
        }
        if decision["status"] == "confirmed_listed":
            mapping["evidenceUrl"] = clean(decision.get("evidenceUrl"))
        mappings.append(mapping)

    expected_count = audit.get("canonicalPropertyCount")
    if expected_count != len(mappings):
        raise ValueError(
            f"Heritage address audit expected {expected_count} properties; "
            f"found {len(mappings)}"
        )
    return {
        "$schema": "./heritage-listing-overrides.schema.json",
        "schemaVersion": 1,
        "updatedAt": clean(
            (audit.get("universeReconciliation") or {}).get("reconciledAt")
        ) or audit["reviewedAt"],
        "productionRequired": True,
        "mappings": mappings,
    }


def stable_text(payload):
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-js", default=str(DEFAULT_INPUT_JS))
    parser.add_argument("--audit", default=str(DEFAULT_AUDIT))
    parser.add_argument("--write", default=str(DEFAULT_OVERRIDES))
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if the committed ledger differs from the deterministic expansion.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    transactions, _summary, _metadata = read_js(args.input_js)
    audit = load_audit(args.audit)
    payload = build_payload(transactions, audit)
    expected = stable_text(payload)
    destination = Path(args.write)
    if args.check:
        actual = destination.read_text(encoding="utf-8")
        if actual != expected:
            raise ValueError(
                "Committed heritage ledger differs from the address-audit expansion"
            )
        load_overrides(destination, property_ids=set(build_properties(transactions)))
        print(f"Heritage address ledger is current: {len(payload['mappings']):,} properties.")
        return 0

    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=destination.name + ".",
        suffix=".tmp",
        dir=destination.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(expected)
            handle.flush()
            os.fsync(handle.fileno())
        load_overrides(
            temporary_name,
            property_ids=set(build_properties(transactions)),
        )
        os.replace(temporary_name, destination)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    print(f"Wrote complete heritage address ledger: {len(payload['mappings']):,} properties.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
