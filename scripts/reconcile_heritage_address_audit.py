#!/usr/bin/env python3
"""Reconcile the reviewed heritage audit with a changed property universe.

The previous complete ledger is the evidence boundary. Reviewed decisions for
properties that leave the canonical universe are retained as retired records.
New canonical identities are added explicitly as ``unknown`` until a separate
address/document review publishes a supported decision.
"""

import argparse
import json
import os
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from build_heritage_address_ledger import (
    DEFAULT_AUDIT,
    indexed_decisions,
    load_audit,
    sha256_lines,
    stable_text,
)
from enrich_listed_buildings import DEFAULT_INPUT_JS, DEFAULT_OVERRIDES, build_properties
from insight_data_utils import clean, read_js


MAX_UNIVERSE_CHANGE = 500
MAX_UNIVERSE_CHANGE_FRACTION = 0.10


def load_ledger(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    mappings = payload.get("mappings") if isinstance(payload, dict) else None
    if payload.get("schemaVersion") != 1 or not isinstance(mappings, list):
        raise ValueError("Reviewed heritage ledger is missing or malformed")
    indexed = {}
    for item in mappings:
        record_id = clean(item.get("propertyRecordId")) if isinstance(item, dict) else ""
        if not record_id or record_id in indexed:
            raise ValueError("Reviewed heritage ledger has a missing or duplicate property ID")
        indexed[record_id] = item
    return indexed


def validate_baseline(audit, ledger):
    ledger_ids = set(ledger)
    if audit.get("canonicalPropertyCount") != len(ledger_ids):
        raise ValueError("Heritage audit count does not match its reviewed ledger")
    if clean(audit.get("canonicalPropertyDigest")) != sha256_lines(ledger_ids):
        raise ValueError("Heritage audit digest does not match its reviewed ledger")
    for record_id, decision in indexed_decisions(audit).items():
        published = ledger.get(record_id)
        if not published:
            raise ValueError(f"Active heritage decision {record_id} is absent from its ledger")
        if (
            published.get("status") != decision.get("status")
            or published.get("listEntryNumbers") != decision.get("listEntryNumbers")
        ):
            raise ValueError(f"Active heritage decision {record_id} disagrees with its ledger")


def active_field_for_status(status):
    return {
        "confirmed_listed": "confirmedMappings",
        "no_direct_match": "noDirectMappings",
        "unknown": "unknownMappings",
    }[status]


def reconcile_payload(
    audit,
    ledger,
    properties,
    *,
    reconciled_at,
    max_change=MAX_UNIVERSE_CHANGE,
    max_change_fraction=MAX_UNIVERSE_CHANGE_FRACTION,
):
    validate_baseline(audit, ledger)
    current_ids = set(properties)
    previous_ids = set(ledger)
    added = sorted(current_ids - previous_ids)
    removed = sorted(previous_ids - current_ids)
    changed = len(added) + len(removed)
    allowed_fraction = max(1, int(len(previous_ids) * max_change_fraction))
    if changed > max_change or changed > allowed_fraction:
        raise ValueError(
            "Heritage canonical-universe change exceeds the reconciliation safety gate: "
            f"{changed:,} identities changed"
        )

    output = json.loads(json.dumps(audit))
    if not changed:
        return output

    active = indexed_decisions(output)
    retired = output.setdefault("retiredMappings", [])
    if not isinstance(retired, list):
        raise ValueError("Heritage address audit retiredMappings must be an array")
    retired_ids = {
        clean(item.get("propertyRecordId"))
        for item in retired
        if isinstance(item, dict)
    }

    for record_id in removed:
        published = ledger[record_id]
        decision = active.get(record_id)
        if decision:
            field = active_field_for_status(decision["status"])
            output[field] = [
                item for item in output[field]
                if clean(item.get("propertyRecordId")) != record_id
            ]
            archived = {
                **decision,
                "status": decision["status"],
            }
        else:
            archived = {
                key: value
                for key, value in published.items()
                if key in {
                    "propertyRecordId",
                    "address",
                    "postcode",
                    "status",
                    "listEntryNumbers",
                    "reviewedBy",
                    "reviewedAt",
                    "evidenceUrl",
                    "note",
                }
            }
        archived["retiredAt"] = reconciled_at
        archived["retirementReason"] = (
            "Canonical property identity absent from the current £2m+ base feed; "
            "evidence retained for audit history."
        )
        if record_id not in retired_ids:
            retired.append(archived)
            retired_ids.add(record_id)

    for record_id in added:
        item = properties[record_id]["item"]
        output["unknownMappings"].append({
            "propertyRecordId": record_id,
            "address": clean(item.get("address")),
            "postcode": clean(item.get("postcode")),
            "listEntryNumbers": [],
            "reviewedBy": "Automated canonical-universe reconciliation (fail-closed)",
            "reviewedAt": reconciled_at,
            "note": (
                "New canonical property identity. No current address/document screening "
                "decision has been published; heritage status remains unknown."
            ),
        })

    output["confirmedMappings"] = sorted(
        output["confirmedMappings"], key=lambda item: item["propertyRecordId"]
    )
    output["noDirectMappings"] = sorted(
        output["noDirectMappings"], key=lambda item: item["propertyRecordId"]
    )
    output["unknownMappings"] = sorted(
        output["unknownMappings"], key=lambda item: item["propertyRecordId"]
    )
    output["retiredMappings"] = sorted(
        retired, key=lambda item: (item["propertyRecordId"], item.get("retiredAt", ""))
    )

    confirmed = output["confirmedMappings"]
    confirmed_pairs = [
        f"{item['propertyRecordId']}|{number}"
        for item in confirmed
        for number in item["listEntryNumbers"]
    ]
    grade_counts = Counter(clean(item.get("grade")) for item in confirmed)
    output["canonicalPropertyCount"] = len(current_ids)
    output["canonicalPropertyDigest"] = sha256_lines(current_ids)
    output["confirmedPropertyCount"] = len(confirmed)
    output["confirmedUniqueListEntryCount"] = len({
        number for item in confirmed for number in item["listEntryNumbers"]
    })
    output["confirmedGradeCounts"] = {
        grade: grade_counts.get(grade, 0) for grade in ("I", "II", "II*")
    }
    output["confirmedPairDigest"] = sha256_lines(confirmed_pairs)
    output["documentedNoDirectPropertyCount"] = len(output["noDirectMappings"])
    output["unknownPropertyCount"] = len(output["unknownMappings"])
    output["genericNoDirectPropertyCount"] = (
        len(current_ids)
        - len(confirmed)
        - len(output["noDirectMappings"])
        - len(output["unknownMappings"])
    )
    if output["genericNoDirectPropertyCount"] < 0:
        raise ValueError("Heritage audit active decision counts exceed the canonical universe")
    output["universeReconciliation"] = {
        "schemaVersion": 1,
        "reconciledAt": reconciled_at,
        "previousPropertyCount": len(previous_ids),
        "currentPropertyCount": len(current_ids),
        "addedPropertyCount": len(added),
        "removedPropertyCount": len(removed),
        "newIdentityPolicy": "explicit_unknown_pending_review",
        "removedIdentityPolicy": "retained_in_retired_mappings",
    }
    return output


def atomic_write(path, payload):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=destination.name + ".",
        suffix=".tmp",
        dir=destination.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(stable_text(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-js", default=str(DEFAULT_INPUT_JS))
    parser.add_argument("--audit", default=str(DEFAULT_AUDIT))
    parser.add_argument("--ledger", default=str(DEFAULT_OVERRIDES))
    parser.add_argument("--write", action="store_true")
    parser.add_argument(
        "--reconciled-at",
        default=datetime.now(ZoneInfo("Europe/London")).date().isoformat(),
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    transactions, _summary, _metadata = read_js(args.input_js)
    properties = build_properties(transactions)
    audit = load_audit(args.audit)
    ledger = load_ledger(args.ledger)
    reconciled = reconcile_payload(
        audit,
        ledger,
        properties,
        reconciled_at=args.reconciled_at,
    )
    changed = stable_text(reconciled) != stable_text(audit)
    if changed and not args.write:
        raise ValueError(
            "Heritage canonical universe requires reconciliation; rerun with --write"
        )
    if changed:
        atomic_write(args.audit, reconciled)
    print(
        "Heritage audit universe "
        + ("reconciled" if changed else "already current")
        + f": {len(properties):,} properties."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
