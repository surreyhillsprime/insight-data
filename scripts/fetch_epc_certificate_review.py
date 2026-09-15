#!/usr/bin/env python3
"""Fetch an approved, hash-pinned reference list once; emit encrypted review evidence.

No property search, cache update, identity admission or feed generation occurs.
GitHub encrypts the private manifest secret; gzip/base64 is only its encoding.
"""

import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import backfill_epc_candidate as base
from epc_candidate_result import seal_result, validate_recipient
from insight_data_utils import utc_now

REQUEST_SCHEMA = "insight.epc-certificate-review-request.v1"
RESULT_SCHEMA = "insight.epc-certificate-review-result.v1"
PIN_FIELDS = ("approvalHandoffSha256", "approvalDecisionsSha256", "sourcePayloadSha256",
              "sourceVerificationSha256", "sourceContextSha256", "recipientPublicKeySha256")
REFERENCE = re.compile(r"\d{4}(?:-\d{4}){4}", re.ASCII)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate manifest key")
        result[key] = value
    return result


def load_manifest(encoded, expected_sha256, producer_commit, recipient_sha256):
    if (not isinstance(encoded, str) or not 1 <= len(encoded) <= 48000
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or "")):
        raise ValueError("A bounded private manifest and external hash are required")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(base64.b64decode(encoded, validate=True))) as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("Manifest is too large")
        manifest = json.loads(raw, object_pairs_hook=unique_object,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if (not isinstance(manifest, dict)
                or set(manifest) != {"schema", "producerCommit", "certificateReferences", *PIN_FIELDS}
                or manifest["schema"] != REQUEST_SCHEMA
                or manifest["producerCommit"] != producer_commit
                or not re.fullmatch(r"[0-9a-f]{40}", producer_commit or "")
                or manifest["recipientPublicKeySha256"] != recipient_sha256
                or any(not isinstance(manifest[key], str)
                       or not re.fullmatch(r"[0-9a-f]{64}", manifest[key]) for key in PIN_FIELDS)):
            raise ValueError("Manifest source binding failed")
        refs = manifest["certificateReferences"]
        if (not isinstance(refs, list) or not 1 <= len(refs) <= 100
                or any(not isinstance(ref, str) or not REFERENCE.fullmatch(ref) for ref in refs)
                or refs != sorted(set(refs)) or digest(manifest) != expected_sha256):
            raise ValueError("Manifest reference or hash validation failed")
        return manifest
    except (ValueError, TypeError, KeyError, OSError, EOFError, RecursionError):
        raise ValueError("Private certificate manifest failed validation") from None


class SingleAttemptTransport:
    """Reuse RegisterClient pacing/parsing while preventing its automatic retries.

    Transport and response-read failures become symbolic RuntimeErrors before
    the shared client's retry handlers. The duplicate guard also fails closed.
    """

    def __init__(self, references, opener=None):
        self.allowed = set(references)
        self.attempted = set()
        self.opener = opener or urllib.request.build_opener(base.NoRedirects())

    @contextmanager
    def open(self, request, timeout):
        parsed = urllib.parse.urlsplit(request.full_url)
        params = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
        if (parsed.scheme + "://" + parsed.netloc != base.API_BASE
                or parsed.path != "/api/certificate" or parsed.fragment
                or set(params) != {"certificate_number"}
                or len(params["certificate_number"]) != 1):
            raise RuntimeError("review_endpoint_forbidden")
        reference = params["certificate_number"][0]
        if reference not in self.allowed or reference in self.attempted:
            raise RuntimeError("review_reference_forbidden")
        self.attempted.add(reference)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                yield response
        except urllib.error.HTTPError as error:
            code = error.code
            try:
                error.close()
            except Exception:
                pass  # A body-close failure must not mask an authentication status.
            raise RuntimeError("register_http_" + str(code) if type(code) is int and 100 <= code <= 599
                               else "register_http_failed") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RuntimeError("register_connection_failed") from None


def safe_failure(error):
    code = str(error) if isinstance(error, RuntimeError) else ""
    allowed = {"certificate_number_conflict", "certificate_unavailable", "invalid_full_certificate_evidence",
               "invalid_register_json", "invalid_register_response", "register_connection_failed",
               "register_authentication_failed", "register_budget_reached", "register_http_failed",
               "review_endpoint_forbidden", "review_reference_forbidden"}
    return code if code in allowed or re.fullmatch(r"register_http_[1-5][0-9]{2}", code) else "review_fetch_failed"


def fetch_review(manifest, token, provenance, *, opener=None, spacing=0.3, max_seconds=600):
    references = manifest["certificateReferences"]
    transport = SingleAttemptTransport(references, opener)
    client = base.RegisterClient(token, opener=transport, max_requests=len(references),
                                 max_seconds=max_seconds, spacing=spacing)
    records = []
    for reference in references:
        started = utc_now()
        try:
            evidence = base.minimized_certificate(client.certificate(reference))
            records.append({"reference": reference, "status": "fetched", "attemptedAt": started,
                            "retrievedAt": utc_now(), "evidence": evidence,
                            "evidenceSha256": digest(evidence)})
        except Exception as error:
            code = safe_failure(error)
            if code in {"register_http_401", "register_http_403"}:
                client.auth_failed = True
            attempted = reference in transport.attempted
            records.append({"reference": reference, "status": "failed" if attempted else "not_attempted",
                            "attemptedAt": started if attempted else None, "retrievedAt": None, "code": code})
    if client.requests != len(transport.attempted) or client.searches or client.query_searches:
        raise ValueError("Focused request accounting failed")
    counts = {status: sum(record["status"] == status for record in records)
              for status in ("fetched", "failed", "not_attempted")}
    receipt = {"schema": "insight.epc-certificate-review-receipt.v1", "candidateUsable": False,
               "status": "complete" if counts["fetched"] == len(references) else "partial",
               "manifestSha256": digest(manifest), "requestedCount": len(references),
               "fetchedCount": counts["fetched"], "failedCount": counts["failed"],
               "notAttemptedCount": counts["not_attempted"], "httpRequests": client.requests,
               **{key: manifest[key] for key in PIN_FIELDS}, **provenance}
    payload = {"schema": RESULT_SCHEMA, "candidateUsable": False, "manifest": manifest,
               "report": receipt, "records": records}
    return payload, receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    provenance = {"producerCommit": os.environ.get("GITHUB_SHA", ""),
                  "githubRunId": os.environ.get("GITHUB_RUN_ID", ""),
                  "githubRunAttempt": os.environ.get("GITHUB_RUN_ATTEMPT", "")}
    if (os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or provenance["githubRunAttempt"] != "1"
            or not re.fullmatch(r"[1-9][0-9]{0,19}", provenance["githubRunId"])
            or not re.fullmatch(r"[0-9a-f]{40}", provenance["producerCommit"])
            or os.path.lexists(args.output_dir)):
        raise ValueError("A first-attempt pinned dispatch and fresh destination are required")
    public_key = os.environ.get("EPC_RESULT_PUBLIC_KEY", "")
    recipient = validate_recipient(public_key)
    manifest = load_manifest(os.environ.get("EPC_CERTIFICATE_REVIEW_MANIFEST_B64", ""),
                             os.environ.get("EPC_CERTIFICATE_REVIEW_MANIFEST_SHA256", ""),
                             provenance["producerCommit"], recipient["publicKeySha256"])
    token = os.environ.get("EPC_BEARER_TOKEN", "").strip()
    if not token or "\n" in token or "\r" in token:
        raise ValueError("Provider credential unavailable")
    payload, receipt = fetch_review(manifest, token, provenance)
    seal_result(payload, receipt, public_key, args.output_dir)
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["status"] == "complete" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("Certificate review fetch failed validation; no candidate was produced.")
        raise SystemExit(1)
