#!/usr/bin/env python3
"""Build the reviewed INSIGHT property-to-INSPIRE association registry.

This is an explicit bootstrap/migration tool.  It turns the measured August
2026 feasibility-study outputs into a compact, versioned registry.  It never
uses a UPRN as canonical property identity and deliberately omits candidate
UPRNs from the published registry.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import tempfile
from collections import defaultdict
from pathlib import Path

from insight_data_utils import read_js


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRANSACTIONS = ROOT / "outputs" / "surrey-transactions.js"
DEFAULT_OUTPUT = ROOT / "config" / "inspire-parcel-associations.json"
IDENTITY_MODE = "full-normalised-address-plus-postcode-fail-closed"
ASSOCIATION_SEMANTICS = (
    "indicative parcel association; not title, exact UPRN, ownership or "
    "legal-boundary confirmation"
)
NUMBERED_PAON_RE = re.compile(
    r"^\d+[A-Z]?(?:\s*(?:-|TO)\s*\d+[A-Z]?)?$",
    re.IGNORECASE,
)


def clean(value: object) -> str:
    return " ".join(str(value or "").replace("\u2013", "-").replace("\u2014", "-").split()).strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def numbered_paon(value: object) -> bool:
    text = clean(value).upper().replace(",", " ")
    text = " ".join(text.split())
    return bool(NUMBERED_PAON_RE.fullmatch(text))


def numbered_property_ids(transactions: list[dict]) -> set[str]:
    variants: dict[str, list[str]] = defaultdict(list)
    for row in transactions:
        property_id = clean(row.get("propertyRecordId"))
        if property_id:
            variants[property_id].append(clean(row.get("paon")))
    return {
        property_id
        for property_id, values in variants.items()
        if values and all(numbered_paon(value) for value in values)
    }


def split_ids(value: object) -> list[str]:
    return [part for part in clean(value).split("|") if part]


def truthy(value: object) -> bool:
    return clean(value).casefold() == "true"


def automatic_associations(
    audit_rows: list[dict[str, str]],
    numbered_properties: set[str],
) -> list[dict]:
    selected = []
    for row in audit_rows:
        origin = clean(row.get("origin"))
        calibrated_epc = (
            clean(row.get("propertyId")) in numbered_properties
            and clean(row.get("epcUprnSources")) == "ENERGY ASSESSOR"
            and clean(row.get("classification")) == "unique_interior_clear"
        )
        if origin == "ubdc_ppd_transaction" or calibrated_epc:
            selected.append(row)

    uprn_owners: dict[str, set[str]] = defaultdict(set)
    parcel_owners: dict[str, set[str]] = defaultdict(set)
    for row in selected:
        property_id = clean(row.get("propertyId"))
        uprn = clean(row.get("uprn"))
        if uprn:
            uprn_owners[uprn].add(property_id)
        for inspire_id in split_ids(row.get("inspireIds")):
            parcel_owners[inspire_id].add(property_id)
    shared_uprns = {key for key, owners in uprn_owners.items() if len(owners) > 1}
    shared_parcels = {key for key, owners in parcel_owners.items() if len(owners) > 1}

    records = []
    for row in selected:
        property_id = clean(row.get("propertyId"))
        uprn = clean(row.get("uprn"))
        inspire_ids = split_ids(row.get("inspireIds"))
        if clean(row.get("classification")) != "unique_interior_clear":
            continue
        if uprn in shared_uprns or any(item in shared_parcels for item in inspire_ids):
            continue
        if len(inspire_ids) != 1:
            raise ValueError(f"Automatic association must select one parcel: {property_id}")
        origin = clean(row.get("origin"))
        records.append({
            "propertyId": property_id,
            "inspireId": inspire_ids[0],
            "associationStatus": "automatic_indicative",
            "matchMethod": (
                "ppd-linked-uprn-unique-clear-containment"
                if origin == "ubdc_ppd_transaction"
                else "strict-epc-uprn-unique-clear-containment"
            ),
            "evidenceTier": (
                "transaction_linked_indicative"
                if origin == "ubdc_ppd_transaction"
                else "calibrated_epc_indicative"
            ),
            "spatialClassification": "unique_interior_clear",
            "boundaryDistanceMetres": round(float(row["boundaryDistanceM"]), 4),
            "reviewDecision": None,
            "titleConfirmed": False,
            "exactUprnIdentityConfirmed": False,
            "legalBoundaryConfirmed": False,
        })
    return records


def reviewed_associations(ledger: dict) -> list[dict]:
    if ledger.get("approvedCount") != len(ledger.get("records") or []):
        raise ValueError("Reviewed decision ledger count is inconsistent")
    batch_times = {
        clean(item.get("name")): clean(item.get("recordedAt"))
        for item in ledger.get("decisionBatches") or []
        if isinstance(item, dict)
    }
    records = []
    for source in ledger.get("records") or []:
        if source.get("decision") != "approve_indicative_parcel":
            raise ValueError(f"Unsupported review decision for {source.get('propertyId')}")
        if any(source.get(field) is not False for field in (
            "titleConfirmed",
            "exactUprnIdentityConfirmed",
            "legalBoundaryConfirmed",
        )):
            raise ValueError("Reviewed association overstates title, UPRN or legal-boundary certainty")
        classification = clean(source.get("spatialClassification")) or "unique_interior_clear"
        if classification not in {"unique_interior_clear", "unique_interior_edge"}:
            raise ValueError(f"Unsupported reviewed spatial classification: {classification}")
        decision_batch = clean(source.get("decisionBatch")) or "target_80_shortlist"
        reviewed_at = batch_times.get(decision_batch)
        if not reviewed_at:
            reviewed_at = clean(ledger.get("recordedAt"))
        records.append({
            "propertyId": clean(source.get("propertyId")),
            "inspireId": clean(source.get("chosenInspireId")),
            "associationStatus": "reviewed_indicative",
            "matchMethod": "reviewed-uprn-point-to-inspire-polygon",
            "evidenceTier": "reviewed_indicative",
            "spatialClassification": classification,
            "boundaryDistanceMetres": (
                round(float(source["boundaryDistanceM"]), 4)
                if source.get("boundaryDistanceM") not in (None, "")
                else None
            ),
            "reviewDecision": {
                "decision": "approve_indicative_parcel",
                "decisionBatch": decision_batch,
                "reviewedAt": reviewed_at,
                "semantics": "reviewed indicative parcel association only",
            },
            "titleConfirmed": False,
            "exactUprnIdentityConfirmed": False,
            "legalBoundaryConfirmed": False,
        })
    return records


def build_registry(
    transactions_path: Path,
    automatic_audit_path: Path,
    review_ledger_path: Path,
    *,
    expected_automatic: int = 2871,
    expected_reviewed: int = 357,
    approval_canonical_properties: int = 3766,
) -> dict:
    transactions, _summary, _metadata = read_js(transactions_path)
    canonical_properties = {clean(row.get("propertyRecordId")) for row in transactions}
    canonical_properties.discard("")
    automatic = automatic_associations(
        read_csv(automatic_audit_path),
        numbered_property_ids(transactions),
    )
    ledger = json.loads(review_ledger_path.read_text(encoding="utf-8"))
    reviewed = reviewed_associations(ledger)

    if len(automatic) != expected_automatic:
        raise ValueError(f"Expected {expected_automatic:,} automatic links, got {len(automatic):,}")
    if len(reviewed) != expected_reviewed:
        raise ValueError(f"Expected {expected_reviewed:,} reviewed links, got {len(reviewed):,}")
    records = sorted(automatic + reviewed, key=lambda row: row["propertyId"])
    property_ids = [row["propertyId"] for row in records]
    inspire_ids = [row["inspireId"] for row in records]
    if len(property_ids) != len(set(property_ids)):
        raise ValueError("Association registry contains duplicate property IDs")
    if len(inspire_ids) != len(set(inspire_ids)):
        raise ValueError("Association registry contains a shared INSPIRE ID")
    missing = sorted(set(property_ids) - canonical_properties)
    if missing:
        raise ValueError(f"Association registry contains unknown canonical property: {missing[0]}")
    if len(canonical_properties) < approval_canonical_properties:
        raise ValueError(
            f"Canonical property universe regressed below approval baseline: "
            f"{len(canonical_properties):,} < {approval_canonical_properties:,}"
        )
    if not all(re.fullmatch(r"\d+", value) for value in inspire_ids):
        raise ValueError("Every INSPIRE ID must be numeric")

    source_snapshots = dict(ledger.get("sourceSnapshots") or {})
    return {
        "schemaVersion": 1,
        "registryVersion": "inspire-parcel-associations-2026-08-10-v1",
        "canonicalIdentityMode": IDENTITY_MODE,
        "associationSemantics": ASSOCIATION_SEMANTICS,
        "sourceStudy": {
            "implementationAuthorisedAt": "2026-08-10",
            "automaticRuleVersion": "calibrated-conservative-uprn-containment-v1",
            "ubdcTransactionLinkageNote": (
                "Transaction-linked UPRN point with unique clear containment; "
                "not part of the EPC expansion calibration sample"
            ),
            "epcExpansionCalibration": {
                "observedCorrect": 815,
                "observedTotal": 816,
                "parcelPrecisionPercent": 99.8775,
            },
            "automaticAuditSha256": sha256_file(automatic_audit_path),
            "reviewDecisionLedgerSha256": sha256_file(review_ledger_path),
            "ubdcPricePaidToUprnLookup": {
                "publisher": "Urban Big Data Centre, University of Glasgow",
                "year": 2023,
                "title": "Price paid data to UPRN lookup",
                "doi": "https://doi.org/10.20394/agu7hprj",
                "availability": "UBDC Open Dataset",
                "licence": "Open Government Licence v3.0",
                "licenceUrl": "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/",
                "coverage": "January 1995 to January 2022",
            },
            "hmlrInspireSnapshot": clean(source_snapshots.get("hmlrInspire")) or "2026-08-02",
            "osOpenUprnSnapshot": clean(source_snapshots.get("osOpenUprn")) or "2026-08",
            "reviewDecisionSemantics": clean(ledger.get("decisionSemantics")),
        },
        "approvalBaseline": {
            "canonicalProperties": approval_canonical_properties,
            "automaticIndicative": expected_automatic,
            "reviewedIndicative": expected_reviewed,
            "associatedProperties": expected_automatic + expected_reviewed,
            "coveragePercent": round((expected_automatic + expected_reviewed) / approval_canonical_properties * 100, 4),
            "semantics": "minimum approved association provenance; live coverage denominator is rebuilt from the current canonical transaction feed",
        },
        "records": records,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transactions", type=Path, default=DEFAULT_TRANSACTIONS)
    parser.add_argument("--automatic-audit", type=Path, required=True)
    parser.add_argument("--review-ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--expected-automatic", type=int, default=2871)
    parser.add_argument("--expected-reviewed", type=int, default=357)
    parser.add_argument("--approval-canonical-properties", type=int, default=3766)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    registry = build_registry(
        args.transactions,
        args.automatic_audit,
        args.review_ledger,
        expected_automatic=args.expected_automatic,
        expected_reviewed=args.expected_reviewed,
        approval_canonical_properties=args.approval_canonical_properties,
    )
    atomic_json(args.output, registry)
    counts = registry["approvalBaseline"]
    coverage = counts["coveragePercent"]
    print(
        f"Wrote {args.output}: {counts['associatedProperties']:,} associations "
        f"({coverage:.4f}% canonical coverage; "
        f"{counts['automaticIndicative']:,} automatic + "
        f"{counts['reviewedIndicative']:,} reviewed)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
