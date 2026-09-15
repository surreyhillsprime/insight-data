#!/usr/bin/env python3
"""Retrieve missing EPC evidence for the frozen Build 117 cohort, without publishing.

Only an encrypted candidate leaves the runner. Existing feeds, tracked caches,
branches and installed applications are never written by this command.
"""

import argparse
import copy
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
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


def apply_epc_patches(rows, patches, cache, *, recovery_context=None):
    if len(rows) != len(patches) or [row.get("id") for row in rows] != [row.get("id") for row in patches]:
        raise ValueError("EPC patches must preserve the complete ordered transaction cohort")
    result = []
    for row, patch in zip(rows, patches):
        if not set(patch) <= epc.PUBLIC_EPC_FIELDS | {"id"}:
            raise ValueError("EPC patch contains non-EPC fields")
        result.append({**epc.without_unverified_epc(row), **patch})
    reconciles = (epc.publication_matches_cache(result, cache, recovery_context=recovery_context)
                  if recovery_context is not None else epc.publication_matches_cache(result, cache))
    if non_epc_digest(result) != non_epc_digest(rows) or not reconciles:
        raise ValueError("EPC patches do not reconcile to unchanged sales and exact certificates")
    return result


def apply_candidate_to_frozen_app(candidate, input_path, *, recovery_context=None):
    source = Path(input_path)
    if (candidate.get("schema") != "insight.epc-patch-candidate.v1"
            or candidate.get("frozenAppInputSha256") != INPUT_SHA256
            or candidate.get("cohortIdentitySha256") != COHORT_IDENTITY_SHA256
            or digest(source.read_bytes()) != INPUT_SHA256):
        raise ValueError("Candidate is not bound to the frozen application input")
    rows, _summary, meta = read_js(source)
    if non_epc_digest(rows) != NON_EPC_SHA256 or identity_digest(rows) != COHORT_IDENTITY_SHA256:
        raise ValueError("Application cohort changed")
    result = apply_epc_patches(rows, candidate["epcPatches"], candidate["cache"],
                               recovery_context=recovery_context)
    if sum(bool(row.get("epcMatched")) for row in result) != candidate["report"]["verifiedAfter"]:
        raise ValueError("Candidate coverage does not reconcile to its patches")
    previous_check = (meta.get("epcEnrichment") or {}).get("updatedAt")
    meta["epcEnrichment"] = copy.deepcopy(candidate["epcEnrichment"])
    meta["epcEnrichment"].update({"updatedAt": previous_check, "retainedEvidenceCheckedAt": previous_check})
    return result, meta


class RegisterClient:
    """Fixed-origin retrieval with shared budgets and single-flight evidence caches."""

    SEARCH_STRATEGY_VERSION = "epc-register-filter-search-v1"

    def __init__(self, token, *, opener=None, sleep=time.sleep, max_requests=5000,
                 max_seconds=2700, spacing=0.3):
        self.token = token
        self.opener = opener
        self._thread_local = threading.local()
        self._lock = threading.Lock()
        self._admission_lock = threading.Lock()
        self._inflight = {}
        self._failures = {}
        self._next_request_at = 0
        self.sleep = sleep
        self.max_requests = max_requests
        self.deadline = time.monotonic() + max_seconds
        self.spacing = spacing
        self.requests = 0
        self.searches = {}
        # Legacy postcode replay and generalized query evidence are separate:
        # an earlier postcode-only negative cannot answer a new strategy.
        self.query_searches = {}
        self.query_metadata = {}
        self.certificates = {}
        self.auth_failed = False

    @staticmethod
    def normalize_query(params):
        """Accept only bounded literal filters; return one canonical query."""
        allowed = {"postcode", "address", "uprn", "council[]"}
        if not isinstance(params, dict) or not params or not set(params) <= allowed:
            raise RuntimeError("invalid_search_filters")
        normalized = {}
        if "postcode" in params:
            value = params["postcode"]
            if not isinstance(value, str) or len(value) > 12:
                raise RuntimeError("invalid_search_postcode")
            value = re.sub(r"\s+", "", value.upper())
            if not re.fullmatch(r"(?:GIR0AA|[A-PR-UWYZ][A-HK-Y]?[0-9][A-HJKPSTUW0-9]?[0-9][ABD-HJLNP-UW-Z]{2})", value):
                raise RuntimeError("invalid_search_postcode")
            normalized["postcode"] = value
        if "address" in params:
            value = params["address"]
            if not isinstance(value, str) or len(value) > 480:
                raise RuntimeError("invalid_search_address")
            value = " ".join(value.upper().split())
            if (not 3 <= len(value) <= 240 or sum(char.isalnum() for char in value) < 3
                    or any(not (char.isalnum() or char in " ,.'’&()/-") for char in value)):
                raise RuntimeError("invalid_search_address")
            normalized["address"] = value
        if "uprn" in params:
            value = params["uprn"]
            if type(value) is int:
                value = str(value)
            if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,12}", value.strip()):
                raise RuntimeError("invalid_search_uprn")
            value = value.strip()
            if int(value) == 0:
                raise RuntimeError("invalid_search_uprn")
            normalized["uprn"] = value.zfill(12)
        if "council[]" in params:
            values = params["council[]"]
            if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 20:
                raise RuntimeError("invalid_search_councils")
            councils = []
            for value in values:
                if (not isinstance(value, str) or len(value) > 160
                        or any(ord(char) < 32 or ord(char) == 127 for char in value)):
                    raise RuntimeError("invalid_search_councils")
                value = " ".join(value.split())
                # The register compares official council names, not ONS codes.
                # Keep their supplied casing and punctuation for that equality.
                if (not 3 <= len(value) <= 80 or sum(char.isalpha() for char in value) < 3
                        or any(not (char.isalpha() or char in " ,.'’&()-") for char in value)):
                    raise RuntimeError("invalid_search_councils")
                councils.append(value)
            normalized["council[]"] = sorted(set(councils))
        return normalized

    @classmethod
    def query_key(cls, params):
        value = {"strategyVersion": cls.SEARCH_STRATEGY_VERSION,
                 "params": cls.normalize_query(params)}
        return cls.SEARCH_STRATEGY_VERSION + ":" + digest(json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())

    def _request_opener(self):
        if self.opener is not None:
            return self.opener
        if not hasattr(self._thread_local, "opener"):
            self._thread_local.opener = urllib.request.build_opener(NoRedirects())
        return self._thread_local.opener

    def _admit_request(self):
        # Serialize pacing, but release the state lock while sleeping so an
        # authentication failure from another worker can stop this admission.
        with self._admission_lock:
            with self._lock:
                if self.auth_failed:
                    raise RuntimeError("register_authentication_failed")
                now = time.monotonic()
                if self.requests >= self.max_requests or now >= self.deadline:
                    raise RuntimeError("register_budget_reached")
                delay = max(0, self._next_request_at - now)
                if now + delay >= self.deadline:
                    raise RuntimeError("register_budget_reached")
            if delay:
                self.sleep(delay)
            with self._lock:
                if self.auth_failed:
                    raise RuntimeError("register_authentication_failed")
                now = time.monotonic()
                if self.requests >= self.max_requests or now >= self.deadline:
                    raise RuntimeError("register_budget_reached")
                self.requests += 1
                self._next_request_at = now + self.spacing

    def _cached_lookup(self, kind, key, cache, retrieve):
        identity = (kind, key)
        while True:
            with self._lock:
                if key in cache:
                    return cache[key]
                if self.auth_failed:
                    raise RuntimeError("register_authentication_failed")
                if identity in self._failures:
                    raise RuntimeError(self._failures[identity])
                event = self._inflight.get(identity)
                if event is None:
                    event = self._inflight[identity] = threading.Event()
                    owner = True
                else:
                    owner = False
            if owner:
                break
            event.wait()
        try:
            result = retrieve()
            with self._lock:
                cache[key] = result
            return result
        except BaseException as error:
            # Do not preserve provider bodies, exception objects or credentials
            # in the shared failure cache. Always release duplicate waiters,
            # including when an unexpected exception interrupts retrieval.
            reason = str(error) if isinstance(error, RuntimeError) else "register_unexpected_failure"
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", reason):
                reason = "register_request_failed"
            with self._lock:
                self._failures[identity] = reason
            if isinstance(error, RuntimeError):
                raise RuntimeError(reason) from None
            raise
        finally:
            with self._lock:
                self._inflight.pop(identity, None)
                event.set()

    @staticmethod
    def _prefetch(values, retrieve):
        def attempt(value):
            try:
                retrieve(value)
            except RuntimeError:
                # The normal matching path will receive the cached symbolic
                # failure and account for this item as unresolved.
                pass

        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="epc-register") as pool:
            for _ in pool.map(attempt, values):
                pass

    def prefetch_searches(self, rows):
        unique = {}
        for row in rows:
            postcode = epc.normalise_postcode(row.get("postcode"))
            if postcode:
                unique.setdefault(postcode, row)
        self._prefetch(unique.values(), self.search)

    def prefetch_certificates(self, numbers):
        self._prefetch(dict.fromkeys(numbers), self.certificate)

    def prefetch_queries(self, queries):
        unique = {}
        for params in queries:
            try:
                normalized = self.normalize_query(params)
            except RuntimeError:
                # The ordinary lookup reports the validation failure; an
                # invalid item must never become an unbounded fallback.
                continue
            unique.setdefault(self.query_key(normalized), normalized)
        self._prefetch(unique.values(), self.search_query)

    def request(self, path, params):
        if path not in ("/api/domestic/search", "/api/certificate"):
            raise ValueError("Unsupported EPC endpoint")
        if path == "/api/domestic/search":
            if (not isinstance(params, dict) or type(params.get("page_size")) is not int
                    or params["page_size"] != 5000 or type(params.get("current_page")) is not int
                    or not 1 <= params["current_page"] <= 20):
                raise RuntimeError("invalid_search_page_request")
            filters = self.normalize_query({key: value for key, value in params.items()
                                            if key not in ("page_size", "current_page")})
            params = {**filters, "page_size": 5000, "current_page": params["current_page"]}
        url = API_BASE + path + "?" + urllib.parse.urlencode(params, doseq=True)

        def finite_number(value):
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("Non-finite register JSON number")
            return number

        def invalid_constant(_value):
            raise ValueError("Non-finite register JSON constant")

        for attempt in range(3):
            self._admit_request()
            request = urllib.request.Request(url, headers={
                "Authorization": "Bearer " + self.token, "Accept": "application/json",
                "User-Agent": "INSIGHT verified EPC backfill",
            })
            try:
                with self._request_opener().open(request, timeout=15) as response:
                    payload = json.loads(bounded_body(response), parse_float=finite_number,
                                         parse_constant=invalid_constant)
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
                    with self._lock:
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
        return self._cached_lookup("search", postcode, self.searches,
                                   lambda: self._search(postcode))

    def _search(self, postcode):
        rows, _metadata = self._collect_search(self.normalize_query({"postcode": postcode}))
        return rows

    def search_query(self, params):
        normalized = self.normalize_query(params)
        key = self.query_key(normalized)
        return self._cached_lookup("query", key, self.query_searches,
                                   lambda: self._search_query(key, normalized))

    def _search_query(self, key, params):
        rows, metadata = self._collect_search(params)
        with self._lock:
            self.query_metadata[key] = metadata
        return rows

    def _collect_search(self, params):
        rows = []
        expected_total = None
        expected_pages = None
        checked_pages = []
        for page in range(1, 21):
            payload = self.request("/api/domestic/search", {
                **params, "page_size": 5000, "current_page": page,
            })
            data = payload.get("data")
            if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
                raise RuntimeError("invalid_search_records")
            if len(data) > 5000:
                raise RuntimeError("search_page_size_limit")
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
            if current != page or total < 0 or total > 100000 or pages < 0 or pages > 20:
                raise RuntimeError("invalid_search_pagination")
            if (expected_total is not None and (total != expected_total or pages != expected_pages)):
                raise RuntimeError("search_changed_during_pagination")
            expected_total = total
            expected_pages = pages
            checked_pages.append({"currentPage": current, "totalPages": pages,
                                  "totalRecords": total, "recordCount": len(data)})
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
        try:
            for item in rows:
                minimized_certificate(item)
        except (ValueError, TypeError, RecursionError):
            # Source-shape failure belongs to this query, not final result
            # sealing. Never cache an apparently complete malformed search.
            raise RuntimeError("invalid_search_certificate_evidence") from None
        return rows, {
            "strategyVersion": self.SEARCH_STRATEGY_VERSION,
            "params": copy.deepcopy(params), "complete": True,
            "searchedAt": utc_now(), "totalRecords": expected_total,
            "totalPages": expected_pages, "pages": checked_pages,
        }

    def certificate(self, number):
        return self._cached_lookup("certificate", number, self.certificates,
                                   lambda: self._certificate(number))

    def _certificate(self, number):
        payload = self.request("/api/certificate", {"certificate_number": number})
        data = payload.get("data")
        if not isinstance(data, dict) or not data:
            raise RuntimeError("certificate_unavailable")
        # The register's documented RdSAP full-record schemas can omit
        # the identifier. Bind such responses to this exact request and
        # independently require full address and registration-date identity.
        if epc.extract_certificate_number(data) not in ("", number):
            raise RuntimeError("certificate_number_conflict")
        try:
            minimized_certificate(data)
        except (ValueError, TypeError, RecursionError):
            raise RuntimeError("invalid_full_certificate_evidence") from None
        return data


def exact_summaries(transaction, candidates):
    exact = {}
    for item in candidates:
        if epc.delivery_identity(transaction, item)[0]:
            number = epc.extract_certificate_number(item)
            if not number:
                raise RuntimeError("exact_summary_identifier_missing")
            exact[number] = item
    if len(exact) > 32:
        raise RuntimeError("exact_certificate_review_limit")
    return exact


def checked_certificate_record(transaction, number, certificate, summary, *, recovery_context=None):
    """Rebuild measurements only from the requested full certificate."""
    if epc.extract_certificate_number(certificate) not in ("", number):
        raise RuntimeError("certificate_number_conflict")
    recovery_method = None
    if recovery_context is None:
        identity_ok = epc.delivery_identity(transaction, summary)[0] and epc.delivery_identity(transaction, certificate)[0]
    else:
        from epc_recovery_identity import resolve_identity
        summary_ok, _ = resolve_identity(transaction, summary, recovery_context)
        full_ok, recovery_method = resolve_identity(transaction, certificate, recovery_context, summary=summary)
        identity_ok = summary_ok and full_ok
    if not identity_ok:
        raise RuntimeError("full_certificate_identity_conflict")
    registered = epc.extract_registration_date(certificate)
    try:
        if date.fromisoformat(registered) > datetime.now(timezone.utc).date():
            raise ValueError("Future certificate")
    except (TypeError, ValueError):
        raise RuntimeError("invalid_certificate_date") from None
    summary_date = epc.extract_registration_date(summary)
    if summary_date and summary_date != registered:
        raise RuntimeError("certificate_registration_conflict")
    summary_uprn, full_uprn = summary.get("uprn"), certificate.get("uprn")
    if summary_uprn and full_uprn and str(summary_uprn).lstrip("0") != str(full_uprn).lstrip("0"):
        raise RuntimeError("certificate_uprn_conflict")
    rating = epc.extract_rating(certificate) or epc.extract_rating(summary)
    if rating not in (None, "", "A", "B", "C", "D", "E", "F", "G"):
        raise RuntimeError("invalid_certificate_rating")
    measurement = epc.floor_area_evidence(certificate)
    if not measurement:
        return None
    area = measurement["areaSqm"]
    sqft = round(area * epc.SQM_TO_SQFT)
    result = {"status": "matched", "identityGuardVersion": epc.IDENTITY_GUARD_VERSION,
              "epc": {
                  "epcMatched": True, "floorAreaSqm": round(area, 1),
                  "floorAreaSqft": sqft, "pricePerSqft": round(transaction["price"] / sqft),
                  "epcRating": rating,
                  "epcRegistrationDate": registered, "epcCertificateNumber": number,
                  "epcAddress": epc.candidate_address(certificate), "epcMatchScore": 1.0,
                  "epcSource": "MHCLG EPC Register",
              }, "floorAreaEvidence": measurement,
              "certificateFetch": {"requestedNumber": number,
                  "returnedNumber": epc.extract_certificate_number(certificate), "uprn": full_uprn,
                  "summaryRegistrationDate": summary_date,
                  "summaryRating": epc.extract_rating(summary)}}
    if recovery_context is not None:
        from epc_recovery_identity import certificate_identity_snapshot
        try:
            result.update(certificateIdentity=certificate_identity_snapshot(certificate),
                          summaryIdentity=certificate_identity_snapshot(summary),
                          recoveryMethod=recovery_method)
        except ValueError:
            raise RuntimeError("invalid_certificate_identity_shape") from None
        verified = epc.validated_cached_epc(transaction, result, recovery_context=recovery_context)
    else:
        verified = epc.validated_cached_epc(transaction, result)
    if not verified:
        # None is reserved for a full certificate with no usable whole area.
        # A failed fact/identity replay is unresolved, never a completed no-match.
        raise RuntimeError("certificate_facts_or_replay_unresolved")
    return result


def exact_register_match(transaction, client):
    candidates = client.search(transaction)
    exact = exact_summaries(transaction, candidates)
    admitted = []
    latest_seen = ""
    unusable_dates = set()
    for number in sorted(exact):
        certificate = client.certificate(number)
        record = checked_certificate_record(transaction, number, certificate, exact[number])
        registered = epc.extract_registration_date(certificate)
        latest_seen = max(latest_seen, registered)
        if record:
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


def minimized_certificate(item):
    """Retain source fields needed to replay identity, rating and whole area."""
    fields = set(epc.ADDRESS_KEYS) | set(epc.AREA_KEYS) | {
        "certificateNumber", "certificate_number", "certificate-number", "lmkKey", "lmk-key", "LMK_KEY",
        "registrationDate", "registration_date", "lodgementDate", "lodgement_date", "lodgement-datetime",
        "currentEnergyEfficiencyBand", "current_energy_efficiency_band", "current-energy-efficiency",
        "current-energy-rating", "POSTCODE", "uprn", "uprn_source",
        "schema_type", "schemaType", "energy_rating_current", "status",
        "address", "locality", "dependentLocality", "dependent_locality", "town",
        "UPRN", "property_uprn", "propertyUprn", "uprnSource", "UPRN_SOURCE", "uniquePropertyReferenceNumber",
        "unique_property_reference_number", "council", "councilCode", "council_code",
        "councilName", "council_name", "localAuthority", "local_authority",
        "local-authority", "localAuthorityCode", "local_authority_code", "localAuthorityLabel", "local_authority_label",
        "local-authority-label",
    }

    def scalar(value, *, area=False):
        if type(value) is float and not math.isfinite(value):
            raise ValueError("Non-finite field in minimized certificate")
        if value is None or type(value) in (str, int, float, bool):
            return value
        if (area and isinstance(value, dict) and set(value) in ({"value", "unit"}, {"value", "quantity"})
                and all(part is None or type(part) in (str, int, float, bool)
                        for part in value.values())):
            return {key: scalar(part) for key, part in value.items()}
        # An allowlisted identity/area field is not permission to retain an
        # unexpected nested contact payload. Do not turn invalid area into a
        # valid value by silently stripping unknown members either.
        raise ValueError("Unexpected compound field in minimized certificate")

    result = {name: scalar(value, area=name in epc.AREA_KEYS)
              for name, value in item.items() if name in fields}
    if isinstance(item.get("sap_building_parts"), list):
        parts = []
        for part in item["sap_building_parts"]:
            if not isinstance(part, dict):
                parts.append(None)
                continue
            kept = {key: scalar(part[key]) for key in ("building_part_number",) if key in part}
            if "sap_floor_dimensions" in part:
                dimensions = part["sap_floor_dimensions"]
                kept["sap_floor_dimensions"] = [
                    {key: scalar(value, area=key == "total_floor_area") for key, value in floor.items()
                     if key in ("floor", "storey", "total_floor_area")}
                    if isinstance(floor, dict) else None for floor in dimensions
                ] if isinstance(dimensions, list) else None
            if "sap_room_in_roof" in part:
                roof = part["sap_room_in_roof"]
                kept["sap_room_in_roof"] = {
                    "floor_area": scalar(roof.get("floor_area"), area=True)
                } if isinstance(roof, dict) else None
            parts.append(kept)
        result["sap_building_parts"] = parts
    for extract in (epc.floor_area_evidence, epc.candidate_address, epc.extract_postcode,
                    epc.extract_registration_date, epc.extract_rating, epc.extract_certificate_number):
        if extract(result) != extract(item):
            raise ValueError("Minimized certificate changes replayable evidence")
    return result


def retained_register_evidence(client):
    """Keep replayable identity/measurement evidence, excluding contact data."""
    with getattr(client, "_lock", nullcontext()):
        searches = copy.deepcopy(getattr(client, "searches", {}))
        certificates = copy.deepcopy(getattr(client, "certificates", {}))
        queries = copy.deepcopy(getattr(client, "query_searches", {}))
        query_metadata = copy.deepcopy(getattr(client, "query_metadata", {}))
    result = {
        "postcodeSearches": {key: [minimized_certificate(item) for item in rows]
                             for key, rows in searches.items()},
        "requestedCertificates": {key: minimized_certificate(item)
                                  for key, item in certificates.items()},
    }
    if queries:
        result["querySearches"] = {}
        for key, rows in queries.items():
            metadata = query_metadata.get(key)
            fields = {"strategyVersion", "params", "complete", "searchedAt",
                      "totalRecords", "totalPages", "pages"}
            if (not isinstance(rows, list) or len(rows) > 100000
                    or not isinstance(metadata, dict) or set(metadata) != fields
                    or metadata.get("complete") is not True
                    or metadata.get("strategyVersion") != RegisterClient.SEARCH_STRATEGY_VERSION
                    or metadata.get("totalRecords") != len(rows)
                    or type(metadata.get("totalRecords")) is not int
                    or RegisterClient.normalize_query(metadata.get("params")) != metadata["params"]
                    or RegisterClient.query_key(metadata["params"]) != key):
                raise ValueError("Generalized search evidence is not complete")
            stamp = metadata["searchedAt"]
            if not isinstance(stamp, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stamp):
                raise ValueError("Generalized search evidence lacks its original UTC timestamp")
            try:
                datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                raise ValueError("Generalized search evidence has an invalid timestamp") from None
            pages = metadata["pages"]
            total_pages = metadata["totalPages"]
            if (type(total_pages) is not int or not 0 <= total_pages <= 20
                    or not isinstance(pages, list) or len(pages) != max(1, total_pages)
                    or (total_pages == 0 and rows)):
                raise ValueError("Generalized search evidence has incomplete pagination")
            for index, page in enumerate(pages, 1):
                if (not isinstance(page, dict)
                        or set(page) != {"currentPage", "totalPages", "totalRecords", "recordCount"}
                        or any(type(value) is not int for value in page.values())
                        or page["currentPage"] != index or page["totalPages"] != total_pages
                        or page["totalRecords"] != len(rows) or not 0 <= page["recordCount"] <= 5000):
                    raise ValueError("Generalized search evidence has inconsistent pagination")
            numbers = [epc.extract_certificate_number(item) for item in rows if isinstance(item, dict)]
            if (sum(page["recordCount"] for page in pages) != len(rows)
                    or len(numbers) != len(rows) or any(not number for number in numbers)
                    or len(set(numbers)) != len(numbers)):
                raise ValueError("Generalized search evidence has inconsistent records")
            result["querySearches"][key] = {
                **metadata, "records": [minimized_certificate(item) for item in rows],
            }
    return result


def prefetch_backfill_evidence(baseline, records, client):
    if not hasattr(client, "prefetch_certificates") or not hasattr(client, "prefetch_searches"):
        return False
    retained_numbers = {records[epc.stable_transaction_key(row)]["epc"]["epcCertificateNumber"]
                        for row in baseline if row.get("epcMatched")}
    unresolved = [row for row in baseline if not row.get("epcMatched")]
    print(json.dumps({"stage": "retained_certificate_measurements", "certificates": len(retained_numbers)}), flush=True)
    client.prefetch_certificates(sorted(retained_numbers))
    print(json.dumps({"stage": "unresolved_postcode_searches", "requests": client.requests}), flush=True)
    client.prefetch_searches(unresolved)
    exact_numbers = set()
    for row in unresolved:
        try:
            exact_numbers.update(exact_summaries(row, client.search(row)))
        except RuntimeError:
            # The serial admission pass retains the exact symbolic failure.
            continue
    print(json.dumps({"stage": "new_exact_certificate_measurements", "certificates": len(exact_numbers),
                      "requests": client.requests}), flush=True)
    client.prefetch_certificates(sorted(exact_numbers))
    return True


def lookup_error(error):
    # Provider bodies/credentials must never become retained diagnostic strings.
    reason = str(error) if isinstance(error, RuntimeError) else "unexpected_lookup_error"
    return {"status": "error", "reason": reason if re.fullmatch(r"[a-z_0-9]+", reason)
            else "unexpected_lookup_error"}


def backfill(rows, meta, retained_cache, client):
    baseline, reviewed, initial_review = epc.revalidate_retained_cache(rows, retained_cache)
    keys = {epc.stable_transaction_key(row) for row in rows}
    reviewed["records"] = {key: value for key, value in reviewed["records"].items() if key in keys}
    records = reviewed["records"]
    baseline_records = copy.deepcopy(records)
    prefetched = prefetch_backfill_evidence(baseline, records, client)
    output, looked_up = [], {}
    attempted_rows = error_rows = retained_rechecks = 0
    new_identities, retained_ids = set(), set()
    for index, prior_row in enumerate(baseline):
        key = epc.stable_transaction_key(prior_row)
        previous = baseline_records[key]
        retained = bool(prior_row.get("epcMatched"))
        number = previous["epc"]["epcCertificateNumber"] if retained else None
        identity = (number,) + tuple(epc.normalise_text(prior_row.get(field)) for field in (
            "address", "paon", "saon", "street", "postcode", "locality", "town", "district"))
        row = epc.without_unverified_epc(prior_row)
        network_available = (not client.auth_failed and client.requests < client.max_requests
                             and time.monotonic() < client.deadline)
        if identity in looked_up or network_available or prefetched:
            if identity not in looked_up:
                try:
                    if retained:
                        summary = epc.retained_certificate(previous["epc"])
                        result = checked_certificate_record(row, number, client.certificate(number), summary)
                        if result is None:
                            raise RuntimeError("retained_certificate_area_unusable")
                        retained_ids.add(number)
                    else:
                        result = exact_register_match(row, client)
                        new_identities.add(identity)
                except Exception as error:
                    result = lookup_error(error)
                    if not retained:
                        new_identities.add(identity)
                result.update({"searchedAt": utc_now(),
                               "evidenceScope": "retained-certificate-refetch" if retained else "targeted-register-search",
                               "identityGuardVersion": epc.IDENTITY_GUARD_VERSION})
                looked_up[identity] = result
            result = copy.deepcopy(looked_up[identity])
            result.update({"address": row.get("address"), "postcode": row.get("postcode")})
            records[key] = result
            attempted_rows += not retained
            retained_rechecks += retained
            error_rows += result.get("status") == "error"
            facts = epc.validated_cached_epc(row, result)
            if facts:
                result["epc"] = facts
                row.update(epc.publishable_epc_fields(facts))
        else:
            # A retained measurement is not grandfathered after this correction.
            records[key] = {"status": "error", "reason": "register_budget_reached",
                            "address": row.get("address"), "postcode": row.get("postcode"),
                            "identityGuardVersion": epc.IDENTITY_GUARD_VERSION}
            error_rows += 1
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
    added = sum(not old.get("epcMatched") and bool(new.get("epcMatched")) for old, new in zip(baseline, output))
    withdrawn = sum(bool(old.get("epcMatched")) and not new.get("epcMatched") for old, new in zip(baseline, output))
    corrected_areas = sum(bool(old.get("epcMatched")) and bool(new.get("epcMatched"))
                          and old["floorAreaSqm"] != new["floorAreaSqm"] for old, new in zip(baseline, output))
    report = {"status": "candidate_complete" if accounting["pending"] == 0 else "candidate_partial",
              "createdAt": utc_now(), "transactions": len(output), "verifiedBefore": before,
              "verifiedAfter": after, "addedVerifiedSales": after - before,
              "newlyVerifiedSales": added, "withdrawnRetainedSales": withdrawn,
              "correctedRetainedAreaSales": corrected_areas,
              "retainedTransactionRowsRechecked": retained_rechecks,
              "verifiedRetainedCertificateIds": len(retained_ids),
              "coveragePercent": round(after * 100 / len(output), 3),
              "attemptedTransactionRows": attempted_rows, "uniquePropertyLookups": len(new_identities),
              "httpRequests": client.requests, "lookupErrorRows": error_rows,
              "sourceAccounting": accounting, "nonEpcSha256": non_epc_digest(output),
              "authenticationFailed": client.auth_failed, "publicationPerformed": False}
    candidate_meta = copy.deepcopy(meta)
    candidate_meta["epcEnrichment"] = {
        "source": "MHCLG Get energy performance of buildings data API",
        # Known certificates were refetched; only unresolved identities received
        # a complete postcode search for the latest register evidence.
        "updatedAt": (meta.get("epcEnrichment") or {}).get("updatedAt"),
        "targetedSearchCompletedAt": report["createdAt"],
        "retainedMeasurementsRecheckedAt": report["createdAt"],
        "retainedEvidenceCheckedAt": (meta.get("epcEnrichment") or {}).get("updatedAt"),
        "evidenceScope": "refetched-measurements-and-targeted-register-search", "matched": after,
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
    print("EPC candidate stage: certificate measurement checks and targeted searches", flush=True)
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
