#!/usr/bin/env python3
"""Retrieve missing EPC evidence for the frozen Build 117 cohort, without publishing.

Only an encrypted candidate leaves the runner. Existing feeds, tracked caches,
branches and installed applications are never written by this command.
"""

import argparse
import copy
import hashlib
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

import enrich_epc_data as epc
from epc_candidate_result import seal_result, validate_recipient
from insight_data_utils import read_js, utc_now


ROOT = Path(__file__).resolve().parents[1]
INPUT_COMMIT = "fc9565fd6abdde6ce3b74b888f3173fac2b66ea4"
INPUT_SHA256 = "30774d097d8578cc8025208d8bcf909c2bc4926134681d825f3211067ca6e082"
SOURCE_FEED_SHA256 = "e8d692733241e411ca9d403125cc0a70efd000ac42cff7e5bb64e92f97c77990"
CACHE_SHA256 = "099b1a55936663987dc1a3d8eca61f8e01774f548804547e3f63e2f33860eb82"
NON_EPC_SHA256 = "bcb7df612a891e3b8bba3c8f5ed526c2619a43ae8351aae32025c8f6d893a97f"
COHORT_IDENTITY_SHA256 = "1dcc423f2b0fef79de4c7db9dc6b3581b5142ca1f973918e3fb9193ae9fdc06d"
EXCLUDED_TRANSACTION_ID = "lr-b4ed8ccb8d031a5ef07f"
IDENTITY_FIELDS = ("id", "address", "saon", "paon", "street", "locality", "town",
                   "district", "postcode", "price", "date", "propertyType", "category",
                   "market", "estateId", "propertyRecordId", "county")
TRANSACTIONS = 4738
BASELINE_MATCHES = 2931
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
API_BASE = "https://api.get-energy-performance-data.communities.gov.uk"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def non_epc_digest(rows):
    values = [{key: value for key, value in row.items() if key not in epc.PUBLIC_EPC_FIELDS}
              for row in rows]
    return digest(json.dumps(values, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False).encode())


def identity_digest(rows):
    return digest(json.dumps([{key: row.get(key) for key in IDENTITY_FIELDS} for row in rows],
                             sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def bounded_body(response):
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise RuntimeError("response_size_limit")
    return body


def load_frozen_inputs(input_path=None):
    # The app repository is private. Reconstruct only its exact sales/identity
    # cohort from this repository's pinned feed; return EPC patches, never its
    # different planning/UPRN/school/estate context as replacement app rows.
    source = Path(input_path) if input_path else ROOT / "outputs" / "surrey-transactions.js"
    raw = source.read_bytes()
    expected = INPUT_SHA256 if input_path else SOURCE_FEED_SHA256
    if digest(raw) != expected:
        raise ValueError("Frozen transaction input hash mismatch")
    cache_raw = (ROOT / "work" / "epc-cache.json").read_bytes()
    if digest(cache_raw) != CACHE_SHA256:
        raise ValueError("Frozen retained cache hash mismatch")
    with tempfile.TemporaryDirectory(prefix="insight-epc-input-") as directory:
        path = Path(directory) / "transactions.js"
        path.write_bytes(raw)
        rows, _summary, meta = read_js(path)
    if not input_path:
        if len(rows) != TRANSACTIONS + 1 or sum(row.get("id") == EXCLUDED_TRANSACTION_ID for row in rows) != 1:
            raise ValueError("Frozen exclusion must identify exactly one transaction")
        rows = [row for row in rows if row.get("id") != EXCLUDED_TRANSACTION_ID]
    if (len(rows) != TRANSACTIONS or identity_digest(rows) != COHORT_IDENTITY_SHA256
            or len({row.get("id") for row in rows}) != TRANSACTIONS):
        raise ValueError("Frozen transaction cohort mismatch")
    return rows, meta, json.loads(cache_raw)


def patch_candidate(payload):
    """Transport only EPC changes for the separately pinned private app feed."""
    rows = payload["rows"]
    if identity_digest(rows) != COHORT_IDENTITY_SHA256:
        raise ValueError("Candidate cohort identity changed")
    result = {key: value for key, value in payload.items() if key not in ("rows", "meta")}
    result.update({"schema": "insight.epc-patch-candidate.v1", "frozenAppInputSha256": INPUT_SHA256,
                   "cohortIdentitySha256": COHORT_IDENTITY_SHA256,
                   "epcEnrichment": payload["meta"]["epcEnrichment"],
                   "epcPatches": [{"id": row["id"], **{key: value for key, value in row.items()
                                                       if key in epc.PUBLIC_EPC_FIELDS}} for row in rows]})
    return result


def apply_epc_patches(rows, patches, cache):
    if len(rows) != len(patches) or [row.get("id") for row in rows] != [row.get("id") for row in patches]:
        raise ValueError("EPC patches must preserve the complete ordered transaction cohort")
    result = []
    for row, patch in zip(rows, patches):
        if not set(patch) <= epc.PUBLIC_EPC_FIELDS | {"id"}:
            raise ValueError("EPC patch contains non-EPC fields")
        result.append({**epc.without_unverified_epc(row), **patch})
    if non_epc_digest(result) != non_epc_digest(rows) or not epc.publication_matches_cache(result, cache):
        raise ValueError("EPC patches do not reconcile to unchanged sales and exact certificates")
    return result


def apply_candidate_to_frozen_app(candidate, input_path):
    source = Path(input_path)
    if (candidate.get("schema") != "insight.epc-patch-candidate.v1"
            or candidate.get("frozenAppInputSha256") != INPUT_SHA256
            or candidate.get("cohortIdentitySha256") != COHORT_IDENTITY_SHA256
            or digest(source.read_bytes()) != INPUT_SHA256):
        raise ValueError("Candidate is not bound to the frozen application input")
    rows, _summary, meta = read_js(source)
    if non_epc_digest(rows) != NON_EPC_SHA256 or identity_digest(rows) != COHORT_IDENTITY_SHA256:
        raise ValueError("Application cohort changed")
    result = apply_epc_patches(rows, candidate["epcPatches"], candidate["cache"])
    if sum(bool(row.get("epcMatched")) for row in result) != candidate["report"]["verifiedAfter"]:
        raise ValueError("Candidate coverage does not reconcile to its patches")
    previous_check = (meta.get("epcEnrichment") or {}).get("updatedAt")
    meta["epcEnrichment"] = copy.deepcopy(candidate["epcEnrichment"])
    meta["epcEnrichment"].update({"updatedAt": previous_check, "retainedEvidenceCheckedAt": previous_check})
    return result, meta


class RegisterClient:
    """A fixed-origin, bounded client; provider responses never enter public logs."""

    def __init__(self, token, *, opener=None, sleep=time.sleep, max_requests=5000,
                 max_seconds=2700, spacing=0.3):
        self.token = token
        self.opener = opener or urllib.request.build_opener(NoRedirects())
        self.sleep = sleep
        self.max_requests = max_requests
        self.deadline = time.monotonic() + max_seconds
        self.spacing = spacing
        self.requests = 0
        self.searches = {}
        self.certificates = {}
        self.auth_failed = False

    def request(self, path, params):
        if path not in ("/api/domestic/search", "/api/certificate"):
            raise ValueError("Unsupported EPC endpoint")
        if self.auth_failed:
            raise RuntimeError("register_authentication_failed")
        url = API_BASE + path + "?" + urllib.parse.urlencode(params)
        for attempt in range(3):
            if self.requests >= self.max_requests or time.monotonic() >= self.deadline:
                raise RuntimeError("register_budget_reached")
            self.sleep(self.spacing)
            self.requests += 1
            request = urllib.request.Request(url, headers={
                "Authorization": "Bearer " + self.token, "Accept": "application/json",
                "User-Agent": "INSIGHT verified EPC backfill",
            })
            try:
                with self.opener.open(request, timeout=15) as response:
                    payload = json.loads(bounded_body(response))
                if not isinstance(payload, dict):
                    raise RuntimeError("invalid_register_response")
                return payload
            except urllib.error.HTTPError as error:
                code = error.code
                error.close()
                if code == 404:
                    return {"data": [], "pagination": {
                        "totalRecords": 0, "currentPage": 1, "totalPages": 0}}
                if code in (401, 403):
                    self.auth_failed = True
                if code in (429, 500, 502, 503, 504) and attempt < 2:
                    self.sleep(15 * (attempt + 1))
                    continue
                raise RuntimeError("register_http_" + str(code)) from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt < 2:
                    self.sleep(2 * (attempt + 1))
                    continue
                raise RuntimeError("register_connection_failed") from None
            except (ValueError, UnicodeError):
                raise RuntimeError("invalid_register_json") from None
        raise RuntimeError("register_retry_limit")

    def search(self, transaction):
        postcode = epc.normalise_postcode(transaction.get("postcode"))
        if not postcode:
            # The matcher requires postcode identity; an unscoped address
            # search cannot establish the missing delivery-point evidence.
            raise RuntimeError("property_postcode_unresolved")
        if postcode in self.searches:
            return self.searches[postcode]
        rows = []
        expected_total = None
        for page in range(1, 21):
            payload = self.request("/api/domestic/search", {
                "postcode": postcode, "page_size": 5000, "current_page": page,
            })
            data = payload.get("data")
            if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
                raise RuntimeError("invalid_search_records")
            rows.extend(data)
            pagination = payload.get("pagination")
            if pagination is None and isinstance(payload.get("meta"), dict):
                pagination = payload["meta"].get("pagination")
            if pagination is None:
                raise RuntimeError("incomplete_search_pagination")
            if not isinstance(pagination, dict):
                raise RuntimeError("invalid_search_pagination")
            total = pagination.get("totalRecords")
            pages = pagination.get("totalPages")
            current = pagination.get("currentPage")
            if any(type(value) is not int for value in (total, pages, current)):
                raise RuntimeError("invalid_search_pagination")
            if current != page or total < 0 or pages < 0 or pages > 20:
                raise RuntimeError("invalid_search_pagination")
            if expected_total is not None and total != expected_total:
                raise RuntimeError("search_changed_during_pagination")
            expected_total = total
            if pages == 0 and total == 0 and not rows:
                break
            if page == pages:
                if len(rows) != total:
                    raise RuntimeError("incomplete_search_records")
                break
            if not data or page > pages:
                raise RuntimeError("incomplete_search_records")
        else:
            raise RuntimeError("search_page_limit")
        numbers = [epc.extract_certificate_number(item) for item in rows]
        if any(not value for value in numbers) or len(set(numbers)) != len(numbers):
            raise RuntimeError("duplicate_or_missing_search_identifier")
        self.searches[postcode] = rows
        return rows

    def certificate(self, number):
        if number not in self.certificates:
            payload = self.request("/api/certificate", {"certificate_number": number})
            data = payload.get("data")
            if not isinstance(data, dict) or not data:
                raise RuntimeError("certificate_unavailable")
            # The register's documented RdSAP full-record schemas can omit
            # the identifier. Bind such responses to this exact request and
            # independently require full address and registration-date identity.
            if epc.extract_certificate_number(data) not in ("", number):
                raise RuntimeError("certificate_number_conflict")
            self.certificates[number] = data
        return self.certificates[number]


def exact_register_match(transaction, client):
    candidates = client.search(transaction)
    exact = {}
    for item in candidates:
        if epc.delivery_identity(transaction, item)[0]:
            number = epc.extract_certificate_number(item)
            if not number:
                raise RuntimeError("exact_summary_identifier_missing")
            exact[number] = item
    if len(exact) > 32:
        raise RuntimeError("exact_certificate_review_limit")
    admitted = []
    latest_seen = ""
    unusable_dates = set()
    for number in sorted(exact):
        certificate = client.certificate(number)
        if epc.extract_certificate_number(certificate) not in ("", number):
            raise RuntimeError("certificate_number_conflict")
        if not epc.delivery_identity(transaction, certificate)[0]:
            raise RuntimeError("full_certificate_identity_conflict")
        registered = epc.extract_registration_date(certificate)
        try:
            if date.fromisoformat(registered) > datetime.now(timezone.utc).date():
                raise ValueError("Future certificate")
        except ValueError:
            raise RuntimeError("invalid_certificate_date") from None
        summary_date = epc.extract_registration_date(exact[number])
        if summary_date and summary_date != registered:
            raise RuntimeError("certificate_registration_conflict")
        summary_uprn = exact[number].get("uprn")
        full_uprn = certificate.get("uprn")
        if summary_uprn and full_uprn and str(summary_uprn).lstrip("0") != str(full_uprn).lstrip("0"):
            raise RuntimeError("certificate_uprn_conflict")
        latest_seen = max(latest_seen, registered)
        area = epc.floor_area_from_certificate(certificate)
        if not area:
            unusable_dates.add(registered)
            continue
        sqft = round(area * epc.SQM_TO_SQFT)
        record = {"status": "matched", "identityGuardVersion": epc.IDENTITY_GUARD_VERSION,
                  "epc": {
                      "epcMatched": True, "floorAreaSqm": round(area, 1),
                      "floorAreaSqft": sqft, "pricePerSqft": round(transaction["price"] / sqft),
                      "epcRating": epc.extract_rating(certificate) or epc.extract_rating(exact[number]),
                      "epcRegistrationDate": registered,
                      "epcCertificateNumber": number, "epcAddress": epc.candidate_address(certificate),
                      "epcMatchScore": 1.0, "epcSource": "MHCLG EPC Register",
                  }, "certificateFetch": {"requestedNumber": number,
                      "returnedNumber": epc.extract_certificate_number(certificate),
                      "uprn": full_uprn, "summaryRegistrationDate": summary_date}}
        validated = epc.validated_cached_epc(transaction, record)
        if validated:
            admitted.append(record)
        else:
            unusable_dates.add(registered)
    if not admitted:
        return {"status": "no_match", "reason": "No exact domestic certificate with usable floor area",
                "candidateCount": len(candidates)}
    newest = [item for item in admitted if item["epc"]["epcRegistrationDate"] == latest_seen]
    if not newest or latest_seen in unusable_dates:
        return {"status": "no_match", "reason": "Latest exact domestic certificate lacks usable floor area",
                "candidateCount": len(candidates)}
    if len({(item["epc"]["floorAreaSqm"], item["epc"]["epcRating"]) for item in newest}) != 1:
        raise RuntimeError("conflicting_latest_exact_certificates")
    return newest[0]


def retained_register_evidence(client):
    """Keep just identity/measurement evidence, excluding assessor/contact data."""
    fields = set(epc.ADDRESS_KEYS) | set(epc.AREA_KEYS) | {
        "certificateNumber", "certificate_number", "registrationDate", "registration_date",
        "currentEnergyEfficiencyBand", "current_energy_efficiency_band", "uprn", "uprn_source",
        "schema_type", "schemaType", "energy_rating_current", "status",
    }
    return {
        "postcodeSearches": {key: [{name: value for name, value in item.items() if name in fields}
                                    for item in rows]
                             for key, rows in getattr(client, "searches", {}).items()},
        "requestedCertificates": {key: {name: value for name, value in item.items() if name in fields}
                                  for key, item in getattr(client, "certificates", {}).items()},
    }


def backfill(rows, meta, retained_cache, client):
    baseline, reviewed, initial_review = epc.revalidate_retained_cache(rows, retained_cache)
    keys = {epc.stable_transaction_key(row) for row in rows}
    reviewed["records"] = {key: value for key, value in reviewed["records"].items() if key in keys}
    records = reviewed["records"]
    output = []
    looked_up = {}
    attempted_rows = 0
    error_rows = 0
    for index, row in enumerate(baseline):
        if not row.get("epcMatched"):
            key = epc.stable_transaction_key(row)
            identity = tuple(epc.normalise_text(row.get(field)) for field in (
                "address", "paon", "saon", "street", "postcode", "locality", "town", "district"))
            network_available = (not client.auth_failed and client.requests < client.max_requests
                                 and time.monotonic() < client.deadline)
            if identity in looked_up or network_available:
                if identity not in looked_up:
                    try:
                        result = exact_register_match(row, client)
                    except Exception as error:
                        # Only our symbolic reasons may enter even the private
                        # result; never retain a token-bearing provider body.
                        reason = str(error) if isinstance(error, RuntimeError) else "unexpected_lookup_error"
                        if not re.fullmatch(r"[a-z_0-9]+", reason):
                            reason = "unexpected_lookup_error"
                        result = {"status": "error", "reason": reason}
                    result.update({"searchedAt": utc_now(), "evidenceScope": "targeted-register-search",
                                   "identityGuardVersion": epc.IDENTITY_GUARD_VERSION})
                    looked_up[identity] = result
                result = copy.deepcopy(looked_up[identity])
                result.update({"address": row.get("address"), "postcode": row.get("postcode")})
                records[key] = result
                attempted_rows += 1
                error_rows += result.get("status") == "error"
                facts = epc.validated_cached_epc(row, result)
                if facts:
                    result["epc"] = facts
                    row = {**row, **epc.publishable_epc_fields(facts)}
        output.append(row)
        if (index + 1) % 250 == 0:
            print(json.dumps({"processed": index + 1, "requests": client.requests,
                              "verified": sum(bool(item.get("epcMatched")) for item in output)}), flush=True)
    if non_epc_digest(output) != non_epc_digest(rows):
        raise ValueError("Backfill changed non-EPC facts")
    if not epc.publication_matches_cache(output, reviewed):
        raise ValueError("Backfill facts do not reconcile to exact certificate cache")
    accounting = epc.terminal_cache_accounting(output, reviewed, 90)
    before = sum(bool(item.get("epcMatched")) for item in baseline)
    after = sum(bool(item.get("epcMatched")) for item in output)
    report = {"status": "candidate_complete" if accounting["pending"] == 0 else "candidate_partial",
              "createdAt": utc_now(), "transactions": len(output), "verifiedBefore": before,
              "verifiedAfter": after, "addedVerifiedSales": after - before,
              "coveragePercent": round(after * 100 / len(output), 3),
              "attemptedTransactionRows": attempted_rows, "uniquePropertyLookups": len(looked_up),
              "httpRequests": client.requests, "lookupErrorRows": error_rows,
              "sourceAccounting": accounting, "nonEpcSha256": non_epc_digest(output),
              "authenticationFailed": client.auth_failed, "publicationPerformed": False}
    candidate_meta = copy.deepcopy(meta)
    candidate_meta["epcEnrichment"] = {
        "source": "MHCLG Get energy performance of buildings data API",
        # A mixed candidate is not a fresh register check of all properties.
        # The encrypted cache retains exact per-property searchedAt values for
        # the coordinated rebuild. Keep the old blanket timestamp conservative.
        "updatedAt": (meta.get("epcEnrichment") or {}).get("updatedAt"),
        "targetedSearchCompletedAt": report["createdAt"],
        "retainedEvidenceCheckedAt": (meta.get("epcEnrichment") or {}).get("updatedAt"),
        "evidenceScope": "retained-and-targeted-register-search", "matched": after,
        "coveragePercent": round(after * 100 / len(output), 1),
        "identityGuardVersion": epc.IDENTITY_GUARD_VERSION,
        "status": "complete" if accounting["pending"] == 0 else "partial", **accounting,
    }
    return {"rows": output, "meta": candidate_meta, "cache": reviewed,
            "initialIdentityReview": initial_review, "registerEvidence": retained_register_evidence(client),
            "report": report}, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--input-js", type=Path, help="Optional local copy of the exact pinned input")
    args = parser.parse_args()
    os.umask(0o077)
    print("EPC candidate stage: recipient validation", flush=True)
    recipient = os.environ.get("EPC_RESULT_PUBLIC_KEY", "")
    validate_recipient(recipient)
    token = os.environ.get("EPC_BEARER_TOKEN", "").strip()
    if not token:
        raise ValueError("EPC_BEARER_TOKEN is not configured")
    if args.output_dir.exists():
        raise ValueError("Output directory must be new")
    print("EPC candidate stage: frozen cohort validation", flush=True)
    rows, meta, cache = load_frozen_inputs(args.input_js)
    baseline, _reviewed, _report = epc.revalidate_retained_cache(rows, cache)
    if sum(bool(item.get("epcMatched")) for item in baseline) != BASELINE_MATCHES:
        raise ValueError("Reviewed identity baseline changed")
    print("EPC candidate stage: targeted register searches", flush=True)
    payload, report = backfill(rows, meta, cache, RegisterClient(token))
    report.update({"inputCommit": INPUT_COMMIT, "inputSha256": INPUT_SHA256,
                   "retainedCacheSha256": CACHE_SHA256,
                   "sourceFeedSha256": SOURCE_FEED_SHA256,
                   "cohortIdentitySha256": COHORT_IDENTITY_SHA256,
                   "producerCommit": os.environ.get("GITHUB_SHA", "local-uncommitted"),
                   "githubRunId": os.environ.get("GITHUB_RUN_ID", "local"),
                   "githubRunAttempt": os.environ.get("GITHUB_RUN_ATTEMPT", "local")})
    print("EPC candidate stage: encrypted patch result", flush=True)
    seal_result(patch_candidate(payload), report, recipient, args.output_dir)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["sourceAccounting"]["pending"] == 0 else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("EPC candidate failed validation; no feed or cache was published.")
        raise SystemExit(1) from None
