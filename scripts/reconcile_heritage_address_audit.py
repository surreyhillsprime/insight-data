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
from insight_data_utils import clean, normalise_postcode, read_js


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


HERITAGE_STATUS_PRIORITY = {
    "unknown": 0,
    "no_direct_match": 1,
    "confirmed_listed": 2,
}


def source_identity_mapping(
    address_canonicalisation,
    properties,
    ledger,
    retired_mappings=(),
):
    """Validate and return the producer-owned legacy-to-canonical identity map."""

    if not isinstance(address_canonicalisation, dict):
        return {}
    variants_by_canonical = address_canonicalisation.get("sourceAddressVariants")
    if not isinstance(variants_by_canonical, dict):
        return {}
    declared_count = address_canonicalisation.get("sourceAddressVariantCount")
    if type(declared_count) is not int or declared_count < 0:
        raise ValueError("Address canonicalisation sourceAddressVariantCount is invalid")
    if address_canonicalisation.get("sourceAddressVariantProperties") != len(
        variants_by_canonical
    ):
        raise ValueError(
            "Address canonicalisation sourceAddressVariantProperties does not reconcile"
        )

    retired = {
        clean(item.get("propertyRecordId")): item
        for item in retired_mappings
        if isinstance(item, dict) and clean(item.get("propertyRecordId"))
    }
    mapping = {}
    target_by_legacy = {}
    seen_legacy_ids = set()
    for canonical_id, variants in variants_by_canonical.items():
        if canonical_id not in properties:
            raise ValueError(
                "Address canonicalisation targets a property outside the current universe: "
                + canonical_id
            )
        if not isinstance(variants, list) or not variants:
            raise ValueError(
                f"Address canonicalisation variants for {canonical_id} must be non-empty"
            )
        for variant in variants:
            if (
                not isinstance(variant, dict)
                or set(variant) != {"propertyRecordId", "address", "postcode"}
            ):
                raise ValueError("Address canonicalisation source variant must be an object")
            legacy_id = clean(variant.get("propertyRecordId"))
            legacy_body, separator, legacy_postcode = (
                legacy_id.removeprefix("property:").rpartition("|")
                if legacy_id.startswith("property:")
                else ("", "", "")
            )
            postcode = variant.get("postcode")
            address = variant.get("address")
            if (
                not legacy_body
                or not separator
                or legacy_id == canonical_id
                or legacy_id in properties
                or not isinstance(address, str)
                or not address
                or address != address.upper()
                or not isinstance(postcode, str)
                or postcode != normalise_postcode(postcode)
                or legacy_postcode != (postcode or "NOPOSTCODE")
            ):
                raise ValueError(
                    "Address canonicalisation source variant is not canonical"
                )
            if legacy_id in seen_legacy_ids:
                raise ValueError(
                    f"Address canonicalisation repeats legacy property ID {legacy_id}"
                )
            seen_legacy_ids.add(legacy_id)
            target_by_legacy[legacy_id] = canonical_id
    if len(seen_legacy_ids) != declared_count:
        raise ValueError(
            "Address canonicalisation sourceAddressVariantCount does not match its ledger"
        )

    for legacy_id, canonical_id in target_by_legacy.items():
        if legacy_id in ledger:
            mapping[legacy_id] = canonical_id
            continue
        retired_target = clean(
            retired.get(legacy_id, {}).get("canonicalPropertyRecordId")
        )
        seen_targets = set()
        while retired_target in target_by_legacy and retired_target not in seen_targets:
            seen_targets.add(retired_target)
            retired_target = target_by_legacy[retired_target]
        if retired_target != canonical_id:
            raise ValueError(
                "Address canonicalisation legacy property ID is neither active "
                f"nor already retired to its canonical target: {legacy_id}"
            )
    return mapping, len(seen_legacy_ids)


def _active_audit_items(audit):
    output = {}
    for status, field in (
        ("confirmed_listed", "confirmedMappings"),
        ("no_direct_match", "noDirectMappings"),
        ("unknown", "unknownMappings"),
    ):
        for item in audit[field]:
            record_id = clean(item.get("propertyRecordId"))
            output[record_id] = (status, dict(item))
    return output


def _decision_for_target(target_id, source_ids, audit, ledger, properties):
    """Carry reviewed evidence through one exact identity migration."""

    published = [ledger[source_id] for source_id in source_ids]
    status = max(
        (clean(item.get("status")) for item in published),
        key=lambda value: HERITAGE_STATUS_PRIORITY.get(value, -1),
    )
    active = _active_audit_items(audit)
    active_for_status = [
        item
        for source_id in source_ids
        for item_status, item in [active.get(source_id, ("", {}))]
        if item_status == status
    ]
    source_statuses = {clean(item.get("status")) for item in published}

    decision = None
    if status in {"confirmed_listed", "unknown"}:
        if len(active_for_status) != 1:
            raise ValueError(
                f"Reviewed heritage {status} decision cannot be migrated uniquely to {target_id}"
            )
        decision = dict(active_for_status[0])
    elif status == "no_direct_match":
        if active_for_status:
            decision = dict(max(
                active_for_status,
                key=lambda item: (
                    clean(item.get("reviewedAt")),
                    clean(item.get("propertyRecordId")),
                ),
            ))
        elif "unknown" in source_statuses:
            # A prior exhaustive no-direct screen outranks a later automated
            # fail-closed unknown identity. Tuscan House is the reviewed
            # production example that exercises this path.
            no_direct_sources = [
                item for item in published
                if item.get("status") == "no_direct_match"
            ]
            decision = {
                key: value
                for key, value in max(
                    no_direct_sources,
                    key=lambda item: (
                        clean(item.get("reviewedAt")),
                        clean(item.get("propertyRecordId")),
                    ),
                ).items()
                if key not in {"status"}
            }

    if decision is None:
        return status, None
    representative = properties[target_id]["item"]
    decision["propertyRecordId"] = target_id
    decision["address"] = clean(representative.get("address"))
    decision["postcode"] = clean(representative.get("postcode"))
    decision.pop("status", None)
    return status, decision


def reconcile_alias_payload(
    audit,
    ledger,
    properties,
    address_canonicalisation,
    *,
    reconciled_at,
    max_change=MAX_UNIVERSE_CHANGE,
    max_change_fraction=MAX_UNIVERSE_CHANGE_FRACTION,
):
    """Migrate reviewed heritage evidence only through producer-owned aliases."""

    validate_baseline(audit, ledger)
    current_ids = set(properties)
    previous_ids = set(ledger)
    legacy_to_current, source_variant_count = source_identity_mapping(
        address_canonicalisation,
        properties,
        ledger,
        audit.get("retiredMappings", []),
    )
    changed = len(legacy_to_current)
    allowed_fraction = max(1, int(len(previous_ids) * max_change_fraction))
    if changed > max_change or changed > allowed_fraction:
        raise ValueError(
            "Heritage address-identity migration exceeds the reconciliation safety gate: "
            f"{changed:,} identities changed"
        )

    sources_by_target = {}
    for previous_id in previous_ids:
        target_id = legacy_to_current.get(previous_id, previous_id)
        sources_by_target.setdefault(target_id, []).append(previous_id)
    migrated_ids = set(sources_by_target)
    added = sorted(current_ids - migrated_ids)
    removed = sorted(migrated_ids - current_ids)
    total_changed = changed + len(added) + len(removed)
    if total_changed > max_change or total_changed > allowed_fraction:
        raise ValueError(
            "Heritage canonical-universe and address-identity change exceeds "
            f"the reconciliation safety gate: {total_changed:,} identities changed"
        )
    if not legacy_to_current and not added and not removed:
        return json.loads(json.dumps(audit))

    output = json.loads(json.dumps(audit))
    retired = output.setdefault("retiredMappings", [])
    if not isinstance(retired, list):
        raise ValueError("Heritage address audit retiredMappings must be an array")
    retired_ids = {
        clean(item.get("propertyRecordId"))
        for item in retired
        if isinstance(item, dict)
    }
    active = _active_audit_items(audit)
    for legacy_id, target_id in sorted(legacy_to_current.items()):
        if legacy_id in retired_ids:
            continue
        status, active_item = active.get(legacy_id, ("", {}))
        published = ledger[legacy_id]
        archived = dict(active_item) if active_item else {
            key: value
            for key, value in published.items()
            if key in {
                "propertyRecordId",
                "address",
                "postcode",
                "listEntryNumbers",
                "reviewedBy",
                "reviewedAt",
                "evidenceUrl",
                "note",
            }
        }
        archived["status"] = status or published["status"]
        archived["retiredAt"] = reconciled_at
        archived["retirementReason"] = (
            "Reviewed address identity consolidated into canonical property "
            f"{target_id}; evidence retained for audit history."
        )
        archived["canonicalPropertyRecordId"] = target_id
        retired.append(archived)
        retired_ids.add(legacy_id)

    # Genuine new/removed properties remain fail-closed. Identity changes are
    # never inferred here; only the exact producer ledger above can migrate one.
    for target_id in removed:
        for previous_id in sources_by_target[target_id]:
            if previous_id in retired_ids:
                continue
            published = ledger[previous_id]
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
            retired.append(archived)
            retired_ids.add(previous_id)

    decisions = {
        "confirmed_listed": [],
        "no_direct_match": [],
        "unknown": [],
    }
    statuses = Counter()
    for target_id in sorted(current_ids):
        if target_id in sources_by_target:
            status, decision = _decision_for_target(
                target_id,
                sources_by_target[target_id],
                audit,
                ledger,
                properties,
            )
        else:
            status = "unknown"
            item = properties[target_id]["item"]
            decision = {
                "propertyRecordId": target_id,
                "address": clean(item.get("address")),
                "postcode": clean(item.get("postcode")),
                "listEntryNumbers": [],
                "reviewedBy": "Automated canonical-universe reconciliation (fail-closed)",
                "reviewedAt": reconciled_at,
                "note": (
                    "New canonical property identity. No current address/document "
                    "screening decision has been published; heritage status remains unknown."
                ),
            }
        statuses[status] += 1
        if decision is not None:
            decisions[status].append(decision)

    output["confirmedMappings"] = sorted(
        decisions["confirmed_listed"], key=lambda item: item["propertyRecordId"]
    )
    output["noDirectMappings"] = sorted(
        decisions["no_direct_match"], key=lambda item: item["propertyRecordId"]
    )
    output["unknownMappings"] = sorted(
        decisions["unknown"], key=lambda item: item["propertyRecordId"]
    )
    output["retiredMappings"] = sorted(
        retired,
        key=lambda item: (item["propertyRecordId"], item.get("retiredAt", "")),
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
    expected_statuses = +Counter({
        "confirmed_listed": len(confirmed),
        "no_direct_match": (
            len(output["noDirectMappings"])
            + output["genericNoDirectPropertyCount"]
        ),
        "unknown": len(output["unknownMappings"]),
    })
    if statuses != expected_statuses:
        raise ValueError("Heritage status migration does not reconcile")
    output["universeReconciliation"] = {
        "schemaVersion": 1,
        "reconciledAt": reconciled_at,
        "previousPropertyCount": len(previous_ids),
        "currentPropertyCount": len(current_ids),
        "addedPropertyCount": len(added),
        "removedPropertyCount": len(removed),
        "sourceAddressVariantCount": source_variant_count,
        "identityAliasesCollapsed": address_canonicalisation.get(
            "identityAliasesCollapsed",
            len(previous_ids) - len(current_ids) + len(added) - len(removed),
        ),
        "newIdentityPolicy": "explicit_unknown_pending_review",
        "removedIdentityPolicy": "retained_in_retired_mappings",
        "addressIdentityPolicy": "producer-source-address-variant-ledger-only",
        "addressIdentityRetirementPolicy": (
            "reviewed-alias-consolidation-retained-in-retired-mappings"
        ),
    }
    return output


def reconcile_payload(
    audit,
    ledger,
    properties,
    *,
    reconciled_at,
    max_change=MAX_UNIVERSE_CHANGE,
    max_change_fraction=MAX_UNIVERSE_CHANGE_FRACTION,
    address_canonicalisation=None,
):
    if (
        isinstance(address_canonicalisation, dict)
        and isinstance(address_canonicalisation.get("sourceAddressVariants"), dict)
    ):
        return reconcile_alias_payload(
            audit,
            ledger,
            properties,
            address_canonicalisation,
            reconciled_at=reconciled_at,
            max_change=max_change,
            max_change_fraction=max_change_fraction,
        )
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
    transactions, _summary, metadata = read_js(args.input_js)
    properties = build_properties(transactions)
    audit = load_audit(args.audit)
    ledger = load_ledger(args.ledger)
    reconciled = reconcile_payload(
        audit,
        ledger,
        properties,
        reconciled_at=args.reconciled_at,
        address_canonicalisation=metadata.get("addressCanonicalisation"),
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
