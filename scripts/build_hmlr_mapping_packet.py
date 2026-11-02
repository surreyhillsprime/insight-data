#!/usr/bin/env python3
"""Build private database-import candidates from official monthly HMLR files.

This performs no database writes or identity admission. Official transaction
UUID relations and legacy current-property parcel associations stay separate.
Every existing published transaction receives an explicit mapping disposition.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
from collections import Counter
from datetime import date
from email.utils import parsedate_to_datetime
from pathlib import Path

from collect_hmlr_identifiers import UUID, download_current
from collect_inspire_parcels import GLOBAL_NAME as PARCEL_GLOBAL
from insight_data_utils import parse_window_json, property_record_id


ROOT = Path(__file__).resolve().parents[1]
VERSION = "hmlr-mapping-import-candidate-v1"


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def verified_inputs(directory: Path, manifest: dict) -> dict[str, bytes]:
    if not re.fullmatch(r"\d{4}-\d{2}", manifest.get("release", "")):
        raise ValueError("Invalid HMLR release month")
    date.fromisoformat(manifest["release"] + "-01")
    if set(manifest.get("files", {})) != {"ppd", "uprn", "inspire"}:
        raise ValueError("A complete PPD/UPRN/INSPIRE release is required")
    paths = {}
    publication_dates = set()
    for kind, metadata in manifest["files"].items():
        name = metadata["file"]
        if Path(name).name != name:
            raise ValueError("Source filename must not contain a path")
        path = directory / name
        raw = path.read_bytes()
        if path.is_symlink() or sha256(raw) != metadata["sha256"]:
            raise ValueError("Source SHA-256 verification failed")
        expected = ("pp-monthly-update-new-version.csv" if kind == "ppd"
                    else ("pp-uprn-lookup-" if kind == "uprn" else "pp-inspire-id-lookup-")
                    + date.fromisoformat(manifest["release"] + "-01").strftime("%b-%Y").lower() + ".csv")
        if name != expected or metadata["url"] != "https://price-paid-data.publicdata.landregistry.gov.uk/" + expected:
            raise ValueError("Source URL or filename contradicts the official release")
        publication_dates.add(parsedate_to_datetime(metadata["lastModified"]).date().isoformat())
        paths[kind] = raw
    if publication_dates != {manifest["publishedDate"]}:
        raise ValueError("PPD and identifier files do not share the declared publication date")
    return paths


def lookup_bytes(raw: bytes, kind: str) -> dict[str, list[str]]:
    result = {}
    width = 9 if kind == "inspire" else 12
    for row in csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline="")):
        if len(row) != 2 or not UUID.fullmatch(row[0].upper()) or not re.fullmatch(r"\d{1," + str(width) + "}", row[1]):
            raise ValueError("Malformed " + kind + " identifier row")
        values = result.setdefault(row[0].upper(), set())
        if row[1] in values:
            raise ValueError("Duplicate " + kind + " identifier pair")
        values.add(row[1])
    if not result:
        raise ValueError("Empty " + kind + " identifier lookup")
    return {tx: sorted(values, key=int) for tx, values in result.items()}


def parcel_bytes(raw: bytes) -> dict:
    text = raw.decode("utf-8").strip()
    prefix = PARCEL_GLOBAL + " = "
    if not text.startswith(prefix) or not text.endswith(";"):
        raise ValueError("Invalid published parcel assignment")
    body = text[len(prefix):-1]
    ending = re.search(r',"generatedAt":"([^"\\]+)","releaseId":"([^"\\]+)"}$', body)
    if ending is None or not ending.group(2).endswith("-" + sha256((body[:ending.start()] + "}").encode())[:12]):
        raise ValueError("Parcel release digest does not match its bytes")
    return json.loads(body)


def source_property_key(fields: list[str]) -> str:
    # This is an address-reference candidate, never an accepted identity join.
    return property_record_id(dict(zip(
        ("paon", "saon", "street", "locality", "town", "postcode"),
        [fields[index] for index in (7, 8, 9, 10, 11, 3)],
    )))


def legacy_mapping_candidates(transactions: list[dict], parcels: dict, geometry_holds: dict | None = None) -> list[dict]:
    result = []
    seen = set()
    associations = parcels.get("associationsByProperty", {})
    geometry = parcels.get("parcelsById", {})
    for row in transactions:
        if row["id"] in seen:
            raise ValueError("Duplicate published legacy transaction reference")
        seen.add(row["id"])
        association = associations.get(row["propertyRecordId"])
        ids = association["parcelIds"] if association else []
        if any(parcel not in geometry for parcel in ids):
            raise ValueError("Published parcel association lacks its geometry")
        hold = next((item for item in (geometry_holds or {}).get("records", [])
                     if item["propertyReference"] == row["propertyRecordId"]
                     and item["inspireId"] in ids
                     and parcels["source"]["sourceSnapshot"] >= item["fromSourceSnapshot"]), None)
        result.append({
            "transactionReference": row["id"],
            "transactionNamespace": row.get("transactionReferenceNamespace", "insight_derived_transaction_reference"),
            "propertyReference": row["propertyRecordId"],
            "canonicalPropertyPublicReference": row.get("canonicalPropertyPublicReference"),
            "inspireIds": [] if hold else ids,
            "heldPriorInspireIds": ids if hold else [],
            "disposition": "review_required_geometry_change" if hold else "current_property_association" if ids else "unresolved",
            "geometryHold": hold,
            "method": association["matchMethod"] if association else None,
            "sourceSnapshot": parcels["source"]["sourceSnapshot"],
            "parcelReleaseId": parcels["releaseId"],
            "officialTransactionUuidAssigned": False,
            "historicalSaleExtentConfirmed": False,
        })
    return result


def build_packet(manifest: dict, rows: list[list[str]], uprns: dict, inspire: dict,
                 transactions: list[dict], prior_state: dict, parcels: dict, geometry_holds: dict | None = None) -> dict:
    known_properties = {row["propertyRecordId"] for row in transactions}
    known_uuids = set(prior_state.get("transactions", {}))
    seen = set()
    occurrences = Counter()
    candidates = []
    action_counts = Counter()
    for ordinal, fields in enumerate(rows, 1):
        if len(fields) != 16 or not UUID.fullmatch(fields[0].upper()) or fields[15] not in {"A", "C", "D"}:
            raise ValueError(f"Malformed PPD record at row {ordinal}")
        tx = fields[0].upper()
        seen.add(tx)
        occurrences[tx] += 1
        action_counts[fields[15]] += 1
        if not fields[1].isdigit():
            raise ValueError(f"Malformed PPD price at row {ordinal}")
        date.fromisoformat(fields[2][:10])
        key = source_property_key(fields)
        reasons = []
        if fields[13].upper() == "SURREY" and fields[4] in {"D", "S", "T", "F"} and int(fields[1]) >= 2_000_000:
            reasons.append("surrey_residential_two_million_cohort")
        if key in known_properties:
            reasons.append("existing_exact_address_reference_candidate")
        if tx in known_uuids:
            reasons.append("previously_observed_official_transaction_uuid")
        if not reasons:
            continue
        ids = inspire.get(tx, [])
        if any(not re.fullmatch(r"\d{1,9}", value) for value in ids):
            raise ValueError("INSPIRE identifier exceeds the official field width")
        disposition = ("withdrawn_source_transaction" if fields[15] == "D" else
                       "official_link_supplied" if ids else
                       "outside_freehold_lookup_scope" if fields[6] == "L" else
                       "not_supplied_by_hmlr")
        candidates.append({
            "sourceTransactionId": tx, "sourceNamespace": "hmlr_ppd_transaction_uuid",
            "ppdRowOrdinal": ordinal, "operation": fields[15], "rawFields": fields,
            "propertyReferenceCandidate": key, "selectionReasons": reasons,
            "uprns": uprns.get(tx, []), "inspireIds": ids,
            "disposition": disposition, "geometryResolution": "not_checked",
            "existingCanonicalIdentityAdmission": "required",
        })
    if (set(uprns) | set(inspire)) - seen:
        raise ValueError("Identifier lookup UUID is absent from this monthly PPD file")
    for candidate in candidates:
        candidate["sameUuidSourceOccurrences"] = occurrences[candidate["sourceTransactionId"]]
        candidate["sourceOperationOrdering"] = ("review_required" if candidate["sameUuidSourceOccurrences"] > 1 else "single_occurrence")
    legacy = legacy_mapping_candidates(transactions, parcels, geometry_holds)
    return {
        "schemaVersion": 1, "packetVersion": VERSION, "status": "candidate_not_imported",
        "release": manifest["release"], "publishedDate": manifest["publishedDate"],
        "sourceManifest": manifest,
        "limitations": [
            "Official monthly identifier lookups are not backdated and do not guarantee a link for every sale.",
            "INSPIRE links cover freehold polygons; an absent link is not evidence of an absent property.",
            "Legacy current-property parcel links do not prove the extent at an earlier sale date.",
            "No official UUID is assigned to an existing derived transaction reference by this packet.",
            "A/C/D source events and all multi-polygon relationships are retained for database reconciliation.",
        ],
        "officialTransactionCandidates": candidates,
        "legacyMappingCandidates": legacy,
        "coverage": {
            "monthlyPpdRows": len(rows), "monthlyPpdActions": dict(sorted(action_counts.items())),
            "selectedSourceOccurrences": len(candidates),
            "selectedOfficialTransactions": len({item["sourceTransactionId"] for item in candidates}),
            "selectedDispositions": dict(sorted(Counter(item["disposition"] for item in candidates).items())),
            "existingTransactionsAssessed": len(legacy),
            "existingTransactionsWithCurrentPropertyParcel": sum(bool(item["inspireIds"]) for item in legacy),
            "existingTransactionsUnresolved": sum(item["disposition"] == "unresolved" for item in legacy),
            "existingTransactionsHeldForGeometryReview": sum(item["disposition"] == "review_required_geometry_change" for item in legacy),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=ROOT / "work/hmlr-identifier-downloads")
    parser.add_argument("--download-current", action="store_true")
    parser.add_argument("--transactions", type=Path, default=ROOT / "outputs/surrey-transactions.js")
    parser.add_argument("--transaction-index-json", type=Path,
                        help="Orchestrator export of current internal sale UUIDs, public property references and published property keys")
    parser.add_argument("--transaction-index-sha256", help="Required SHA-256 pin for the orchestrator export")
    parser.add_argument("--parcels", type=Path, default=ROOT / "outputs/inspire-parcels.js")
    parser.add_argument("--previous-state", type=Path, default=ROOT / "work/hmlr-identifier-state.json")
    parser.add_argument("--geometry-holds-json", type=Path,
                        help="Explicit per-property geometry holds; held IDs are not emitted as active mappings")
    parser.add_argument("--output", type=Path, default=ROOT / "work/hmlr-mapping-candidate.json")
    args = parser.parse_args()
    manifest = (download_current(args.source_dir) if args.download_current
                else json.loads((args.source_dir / "manifest.json").read_text()))
    source_bytes = verified_inputs(args.source_dir, manifest)
    rows = list(csv.reader(io.StringIO(source_bytes["ppd"].decode("utf-8-sig"), newline="")))
    transaction_path = args.transaction_index_json or args.transactions
    input_bytes = {key: path.read_bytes() for key, path in
                   (("transactions", transaction_path), ("parcels", args.parcels), ("previousState", args.previous_state))}
    holds = None
    if args.geometry_holds_json:
        input_bytes["geometryHolds"] = args.geometry_holds_json.read_bytes()
        holds = json.loads(input_bytes["geometryHolds"])
        if holds.get("schemaVersion") != 1 or not isinstance(holds.get("records"), list):
            raise ValueError("Invalid geometry hold ledger")
        for hold in holds["records"]:
            if not re.fullmatch(r"hmlr-inspire-\d{4}-\d{2}-\d{2}", hold.get("fromSourceSnapshot", "")) or not hold.get("reason"):
                raise ValueError("Geometry hold lacks a source snapshot or reason")
            date.fromisoformat(hold["fromSourceSnapshot"].removeprefix("hmlr-inspire-"))
            if not re.fullmatch(r"\d{1,9}", hold.get("inspireId", "")) or not hold.get("propertyReference"):
                raise ValueError("Geometry hold lacks a property or INSPIRE identifier")
    transaction_input = {"namespace": "insight_derived_transaction_reference"}
    if args.transaction_index_json:
        if not args.transaction_index_sha256 or sha256(input_bytes["transactions"]) != args.transaction_index_sha256:
            raise ValueError("Orchestrator transaction index SHA-256 does not match")
        exported = json.loads(input_bytes["transactions"])
        transactions = [{"id": row["id"], "propertyRecordId": row["publishedPropertyKey"],
                         "canonicalPropertyPublicReference": row["propertyId"],
                         "transactionReferenceNamespace": "insight_internal_transaction_uuid"}
                        for row in exported["rows"]]
        transaction_input = {"namespace": "insight_internal_transaction_uuid",
                             **{key: exported[key] for key in ("sourceRevision", "sourceCheckedAt", "identityNote")}}
    else:
        transaction_text = input_bytes["transactions"].decode("utf-8")
        if transaction_text.count("window.SURREY_LAND_REG_TRANSACTIONS = ") != 1:
            raise ValueError("Expected exactly one published transaction assignment")
        transactions = parse_window_json(transaction_text, "SURREY_LAND_REG_TRANSACTIONS", None)
    if not isinstance(transactions, list) or not transactions:
        raise ValueError("Published transaction universe is empty or malformed")
    parcels = parcel_bytes(input_bytes["parcels"])
    prior = json.loads(input_bytes["previousState"])
    packet = build_packet(manifest, rows, lookup_bytes(source_bytes["uprn"], "uprn"), lookup_bytes(source_bytes["inspire"], "inspire"),
                          transactions, prior, parcels, holds)
    packet["inputHashes"] = {key: sha256(raw) for key, raw in input_bytes.items()}
    packet["transactionInput"] = transaction_input
    identity = {"release": manifest["release"], "sources": {key: value["sha256"] for key, value in manifest["files"].items()},
                "inputHashes": packet["inputHashes"], "packetVersion": VERSION}
    packet["idempotencyKey"] = sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode())
    content = (json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    from collect_inspire_parcels import atomic_write_bytes
    atomic_write_bytes(args.output, content)
    receipt = {"packetVersion": VERSION, "release": manifest["release"], "publishedDate": manifest["publishedDate"],
               "sha256": sha256(content), "bytes": len(content), "coverage": packet["coverage"],
               "status": packet["status"], "idempotencyKey": packet["idempotencyKey"]}
    atomic_write_bytes(args.output.with_suffix(".receipt.json"), (json.dumps(receipt, indent=2) + "\n").encode())
    print(json.dumps(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
