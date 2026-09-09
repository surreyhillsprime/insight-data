#!/usr/bin/env python3
"""Join HMLR's monthly transaction identifiers to canonical properties and OS points.

The cumulative ledger is acquisition evidence, never a canonical identity key.
Missing monthly rows do not withdraw older links. Ambiguous or withdrawn links
stop publication, retaining the last good generation for explicit investigation.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import io
import json
import re
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from build_property_uprn_links import GLOBAL_NAME, atomic_write, empty_payload
from insight_data_utils import read_js
from runtime_release import finalise_body, parse_runtime
from sweep_land_registry import archive_row, normalise_rows
from validate_property_uprn_links import validation_failures

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "work/hmlr-identifier-state.json"
CACHE = ROOT / "work/hmlr-identifier-downloads"
OUTPUT = ROOT / "outputs/property-uprn-links.js"
UPRN_PAGE = "https://www.gov.uk/government/statistical-data-sets/transaction-unique-identifier-and-uprn-look-up-table-dataset"
INSPIRE_PAGE = "https://www.gov.uk/government/statistical-data-sets/transaction-unique-identifier-and-inspire-id-look-up-table-dataset"
PPD_URL = "https://price-paid-data.publicdata.landregistry.gov.uk/pp-monthly-update-new-version.csv"
OS_API = "https://api.os.uk/downloads/v1/products/OpenUPRN"
UUID = re.compile(r"\{[0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}\}")
MONTHS = {month.lower(): index for index, month in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


def digest(path: Path, algorithm: str = "sha256") -> str:
    result = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def request(url: str):
    return urllib.request.urlopen(urllib.request.Request(url, headers={
        "User-Agent": "INSIGHT HMLR identifier refresh", "Cookie": "cookies_policy={\"essential\":true}"
    }), timeout=120)


def latest_lookup(html: str, prefix: str) -> tuple[str, str]:
    matches = re.findall(
        rf'https://price-paid-data\.publicdata\.landregistry\.gov\.uk/{prefix}-([a-z]{{3}})-(\d{{4}})\.csv', html)
    if not matches:
        raise ValueError(f"No official {prefix} CSV link found")
    month, year = max(set(matches), key=lambda pair: (int(pair[1]), MONTHS[pair[0]]))
    return f"https://price-paid-data.publicdata.landregistry.gov.uk/{prefix}-{month}-{year}.csv", f"{year}-{MONTHS[month]:02d}"


def download(url: str, path: Path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    with request(url) as response, temporary.open("wb") as handle:
        modified = response.headers.get("Last-Modified")
        for block in iter(lambda: response.read(1024 * 1024), b""):
            handle.write(block)
    temporary.replace(path)
    return {"url": url, "file": path.name, "sha256": digest(path), "lastModified": modified}


def download_current(directory: Path) -> dict:
    with request(UPRN_PAGE) as response:
        uprn_url, release = latest_lookup(response.read().decode(), "pp-uprn-lookup")
    with request(INSPIRE_PAGE) as response:
        inspire_url, inspire_release = latest_lookup(response.read().decode(), "pp-inspire-id-lookup")
    if release != inspire_release:
        raise ValueError("HMLR lookup publications are from different months")
    files = {key: download(url, directory / Path(urllib.parse.urlparse(url).path).name)
             for key, url in (("uprn", uprn_url), ("inspire", inspire_url), ("ppd", PPD_URL))}
    publication_dates = {parsedate_to_datetime(item["lastModified"]).date().isoformat() for item in files.values()}
    if len(publication_dates) != 1:
        raise ValueError("Rolling PPD and identifier files do not share one publication date")
    manifest = {"release": release, "publishedDate": next(iter(publication_dates)),
                "checkedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"), "files": files}
    atomic_write(directory / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    return manifest


def download_os(directory: Path) -> tuple[Path, dict]:
    with request(OS_API) as response:
        product = json.load(response)
    with request(product["downloadsUrl"]) as response:
        choices = [row for row in json.load(response) if row["format"] == "CSV" and row["area"] == "GB"]
    if len(choices) != 1:
        raise ValueError("OS must advertise one GB CSV download")
    metadata = choices[0]
    path = directory / metadata["fileName"]
    if not path.exists() or path.stat().st_size != metadata["size"] or digest(path, "md5") != metadata["md5"]:
        download(metadata["url"], path)
    if path.stat().st_size != metadata["size"] or digest(path, "md5") != metadata["md5"]:
        raise ValueError("OS download failed its advertised size/MD5 check")
    return path, {"version": product["version"], "sha256": digest(path), "md5": metadata["md5"]}


def lookup_rows(path: Path) -> dict[str, list[str]]:
    result = defaultdict(set)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.reader(handle):
            if len(row) != 2 or not UUID.fullmatch(row[0].upper()) or not re.fullmatch(r"\d{1,12}", row[1]):
                raise ValueError(f"Malformed identifier row in {path.name}")
            key, value = row[0].upper(), row[1]
            if value in result[key]:
                raise ValueError(f"Duplicate identifier pair in {path.name}")
            result[key].add(value)
    if not result:
        raise ValueError(f"Empty identifier lookup: {path.name}")
    return {key: sorted(values, key=int) for key, values in sorted(result.items())}


def update_state(prior: dict, monthly: list[list[str]], uprns: dict, parcels: dict, manifest: dict) -> dict:
    state = copy.deepcopy(prior)
    if state.get("schemaVersion") != 1:
        raise ValueError("Unsupported HMLR acquisition ledger")
    publications = state.setdefault("publications", {})
    previous_publication = publications.get(manifest["release"])
    if previous_publication and source_fingerprint(previous_publication) == source_fingerprint(manifest):
        manifest = previous_publication  # Preserve the first verified observation of these bytes.
    if previous_publication and previous_publication != manifest:
        # A corrected publication can replace transaction evidence, but never
        # silently rewrite our record of the bytes previously ingested.
        history = state.setdefault("publicationCorrections", [])
        if previous_publication not in history:
            history.append(previous_publication)
    publications[manifest["release"]] = manifest
    records = state.setdefault("transactions", {})
    seen = set()
    for values in monthly:
        if len(values) != 16 or not UUID.fullmatch(values[0].upper()) or values[15] not in {"A", "C", "D"}:
            raise ValueError("Malformed monthly PPD row")
        tx = values[0].upper()
        if tx in seen:
            raise ValueError("Duplicate transaction UUID in monthly PPD")
        seen.add(tx)
        old = records.get(tx)
        if old and old["publication"] > manifest["release"]:
            continue
        parsed = archive_row(values)
        normalised = normalise_rows([parsed])[1] if parsed else []
        if not normalised:
            if old:
                records[tx] = {**old, "withdrawn": True, "publication": manifest["release"]}
            continue
        row = normalised[0]
        records[tx] = {
            "propertyId": row["propertyRecordId"], "transactionId": row["id"],
            "saleDate": row["date"], "publication": manifest["release"],
            "uprns": uprns.get(tx, []), "inspireIds": parcels.get(tx, []), "withdrawn": False,
        }
    if (set(uprns) | set(parcels)) - seen:
        raise ValueError("Identifier UUIDs are absent from the monthly PPD publication")
    state["transactions"] = dict(sorted(records.items()))
    state["publications"] = dict(sorted(publications.items()))
    return state


def source_fingerprint(manifest: dict) -> dict:
    return {key: {field: value.get(field) for field in ("url", "sha256")}
            for key, value in manifest["files"].items()}


def preferred_records(state: dict, transactions: list[dict]) -> dict[str, list[tuple[str, dict]]]:
    by_property = defaultdict(list)
    current = {row["id"]: row["propertyRecordId"] for row in transactions}
    canonical = set(current.values())
    for tx, record in state["transactions"].items():
        if record["propertyId"] not in canonical:
            continue  # Retain pending evidence until the canonical ledger catches up.
        if record["withdrawn"]:
            if record["transactionId"] in current:
                raise ValueError("Withdrawn HMLR transaction remains in the canonical ledger; reconcile it first")
            continue
        # Require the complete sale tuple to join an extant transaction. An
        # address alone cannot promote an unobserved/corrected transaction.
        if current.get(record["transactionId"]) != record["propertyId"]:
            continue
        by_property[record["propertyId"]].append((tx, record))
    result = {}
    for property_id, rows in by_property.items():
        # A later sale with no supplied identifier does not retract a reliable
        # older HMLR relationship for the same canonical property.
        rows = [(tx, row) for tx, row in rows if row["uprns"]]
        if not rows:
            continue
        newest_sale = max(row["saleDate"] for _, row in rows)
        newest = [(tx, row) for tx, row in rows if row["saleDate"] == newest_sale]
        identifiers = {value for _, row in newest for value in row["uprns"]}
        if len(identifiers) > 1:
            raise ValueError(f"HMLR identifies multiple UPRNs for {property_id}; no fallback may override this")
        if identifiers:
            result[property_id] = [(tx, row) for tx, row in newest if row["uprns"]]
    return result


def os_coordinates(path: Path, needed: set[str]) -> dict[str, dict]:
    found = {}
    with zipfile.ZipFile(path) as archive:
        files = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(files) != 1:
            raise ValueError("OS archive must contain exactly one coordinate CSV")
        with archive.open(files[0]) as handle:
            reader = csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8-sig", newline=""))
            required = {"UPRN", "LATITUDE", "LONGITUDE", "X_COORDINATE", "Y_COORDINATE"}
            if not required.issubset(reader.fieldnames or []):
                raise ValueError("OS Open UPRN CSV columns changed")
            for row in reader:
                if row["UPRN"] not in needed:
                    continue
                point = {"longitude": float(row["LONGITUDE"]), "latitude": float(row["LATITUDE"])}
                if not (-1.0 <= point["longitude"] <= 0.15 and 51.0 <= point["latitude"] <= 51.55):
                    raise ValueError("HMLR-linked OS point lies outside the supported Surrey area")
                if row["UPRN"] in found:
                    raise ValueError("Duplicate requested UPRN in OS coordinate source")
                found[row["UPRN"]] = point
    missing = needed - set(found)
    if missing:
        raise ValueError(f"OS source is missing {len(missing)} HMLR UPRNs; retain the prior feed, never substitute nearby UPRNs")
    return found


def build_payload(state: dict, preferred: dict, coordinates: dict, os_source: dict) -> dict:
    payload = empty_payload()
    sources = {}
    owners = {}
    for property_id, records in sorted(preferred.items()):
        tx, record = max(records, key=lambda item: (item[1]["publication"], item[0]))
        uprn = next(iter({value for _, row in records for value in row["uprns"]}))
        if uprn in owners:
            raise ValueError(f"HMLR UPRN is shared by distinct canonical properties: {property_id}; review the hierarchy")
        owners[uprn] = property_id
        publication = state["publications"][record["publication"]]
        checked_at = max(publication["checkedAt"], os_source.get("checkedAt", publication["checkedAt"]))
        publication_digest = hashlib.sha256(json.dumps(source_fingerprint(publication), sort_keys=True).encode()).hexdigest()
        snapshot = "hmlr-" + record["publication"] + ":" + publication_digest + "+os:" + os_source["sha256"]
        source_id = "hmlr_ppd_uprn_" + record["publication"].replace("-", "") + "_os_" + os_source["version"].replace("-", "") + "_" + hashlib.sha256(snapshot.encode()).hexdigest()[:12]
        sources[source_id] = {
            "sourceId": source_id, "name": "HM Land Registry PPD UPRN lookup and OS Open UPRN",
            "sourceSnapshot": snapshot, "checkedAt": checked_at, "coordinateBasis": "authoritative_address_point",
            "licenceOrEntitlement": {"type": "open_licence", "reference":
                UPRN_PAGE + " ; https://www.ordnancesurvey.co.uk/products/os-open-uprn ; Open Government Licence v3.0; Contains HM Land Registry data and Ordnance Survey data © Crown copyright and database right " + checked_at[:4],
                "permitsPublicDerivedPublication": True},
            "redistributionClassification": "public_open_data",
        }
        payload["linksByProperty"][property_id] = {
            "propertyId": property_id, "sourceId": source_id, "uprn": uprn,
            "matchStatus": "confirmed_address_match", "evidenceTier": "authoritative_address_source",
            **coordinates[uprn], "coordinateSource": "HMLR transaction UPRN joined to OS Open UPRN",
            "sourceSnapshot": snapshot, "checkedAt": checked_at,
            "limitations": ["HMLR transaction-linked address point; canonical INSIGHT property identity is unchanged.",
                "OS Open UPRN supplies a representative address point, not a surveyed building footprint or exact legal boundary.",
                "HMLR UPRN evidence takes priority; other UPRN sources are fallback only where HMLR has no link."],
        }
    payload["sources"] = [sources[key] for key in sorted(sources)]
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-current", action="store_true")
    parser.add_argument("--source-dir", type=Path, default=CACHE)
    parser.add_argument("--os-zip", type=Path)
    parser.add_argument("--os-version")
    parser.add_argument("--os-md5")
    parser.add_argument("--state", type=Path, default=STATE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--transactions", type=Path, default=ROOT / "outputs/surrey-transactions.js")
    args = parser.parse_args()
    manifest = download_current(args.source_dir) if args.download_current else json.loads((args.source_dir / "manifest.json").read_text())
    for metadata in manifest["files"].values():
        path = args.source_dir / metadata["file"]
        if path.parent != args.source_dir or digest(path) != metadata["sha256"]:
            raise ValueError("Source filename or SHA-256 verification failed")
    uprns = lookup_rows(args.source_dir / manifest["files"]["uprn"]["file"])
    parcels = lookup_rows(args.source_dir / manifest["files"]["inspire"]["file"])
    with (args.source_dir / manifest["files"]["ppd"]["file"]).open(encoding="utf-8-sig", newline="") as handle:
        monthly = list(csv.reader(handle))
    prior = json.loads(args.state.read_text()) if args.state.exists() else {"schemaVersion": 1, "publications": {}, "transactions": {}}
    state = update_state(prior, monthly, uprns, parcels, manifest)
    transactions = read_js(args.transactions)[0]
    preferred = preferred_records(state, transactions)
    needed = {uprn for records in preferred.values() for _, row in records for uprn in row["uprns"]}
    if args.os_zip:
        if not args.os_version or not args.os_md5 or digest(args.os_zip, "md5") != args.os_md5:
            raise ValueError("Offline OS input requires its official version and matching advertised MD5")
        os_path = args.os_zip
        os_source = {"version": args.os_version, "sha256": digest(os_path), "md5": args.os_md5}
    else:
        os_path, os_source = download_os(args.source_dir)
    os_sources = state.setdefault("osSources", {})
    if os_source["sha256"] not in os_sources:
        os_sources[os_source["sha256"]] = {**os_source, "checkedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")}
    os_source = os_sources[os_source["sha256"]]
    payload = build_payload(state, preferred, os_coordinates(os_path, needed), os_source)
    previous = parse_runtime(args.output, GLOBAL_NAME)[0] if args.output.exists() else None
    old_links = previous.get("linksByProperty", {}) if previous else {}
    if set(old_links) - set(payload["linksByProperty"]):
        raise ValueError("HMLR publication would withdraw accepted links; reconcile the source change before publication")
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    body, _ = finalise_body(payload, now, "property-uprn-links", now[:10])
    failures = validation_failures(json.loads(body), {row["propertyRecordId"] for row in transactions})
    if failures:
        raise ValueError("Invalid HMLR point feed: " + "; ".join(failures))
    previous_core = {key: value for key, value in (previous or {}).items() if key not in {"generatedAt", "releaseId"}}
    # Validate everything before either tracked artifact changes. Repeated
    # source checks preserve generatedAt and the existing content release.
    if payload != previous_core:
        atomic_write(args.output, GLOBAL_NAME + " = " + body + ";\n")
    atomic_write(args.state, json.dumps(state, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"hmlrProperties": len(preferred), "retainedTransactions": len(state["transactions"]),
                      "changed": payload != previous_core, "release": manifest["release"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
