#!/usr/bin/env python3
"""Recover the reviewed missing-property cohort without publishing a feed.

The trusted lookup context is supplied privately and independently hash-pinned.
The original sales/identity cohort is immutable. Query completion, certificate
identity, complete floor-area evidence and cache replay all precede admission.
"""

import argparse
import base64
import collections
import copy
import gzip
import io
import json
import os
import re
from pathlib import Path

import backfill_epc_candidate as base
import enrich_epc_data as epc
from epc_candidate_result import seal_result, validate_recipient
from epc_recovery_identity import (
    context_digest,
    discovery_queries,
    load_recovery_context,
    resolve_identity,
)
from insight_data_utils import utc_now


MAX_CONTEXT_ENCODED = 64 * 1024
MAX_CONTEXT_BYTES = 512 * 1024
MAX_QUERIES_PER_PROPERTY = 32
MAX_CERTIFICATES_PER_PROPERTY = 32
STRATEGY_VERSION = 1
DIAGNOSTIC_SCHEMA = "insight.epc-recovery-diagnostic.v1"


class RecoveryContextScopeError(ValueError):
    """The completed baseline contains unresolved properties outside approval."""


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate recovery-context field")
        value[key] = item
    return value


def decode_context(encoded, expected_sha256):
    """Decode only bounded private input; never include its contents in errors."""
    if not isinstance(encoded, str) or not encoded or len(encoded) > MAX_CONTEXT_ENCODED:
        raise ValueError("Private recovery context is missing or oversized")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or ""):
        raise ValueError("An explicit recovery context SHA-256 is required")
    try:
        compressed = base64.b64decode(encoded, validate=True)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            raw = stream.read(MAX_CONTEXT_BYTES + 1)
        if len(raw) > MAX_CONTEXT_BYTES:
            raise ValueError("Oversized context")
        payload = json.loads(raw, object_pairs_hook=_unique_object,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite context")))
        if context_digest(payload) != expected_sha256:
            raise ValueError("Context hash mismatch")
    except (ValueError, OSError, EOFError, UnicodeError, TypeError, RecursionError):
        raise ValueError("Private recovery context failed decoding or hash validation") from None
    return payload


def property_groups(rows):
    groups = collections.defaultdict(list)
    for row in rows:
        key = row.get("propertyRecordId")
        if not isinstance(key, str) or not key.startswith("property:"):
            raise ValueError("Recovery requires canonical property identities")
        groups[key].append(row)
    return dict(groups)


def property_queries(rows, context, client):
    queries = {}
    for row in rows:
        for params in discovery_queries(row, context):
            normalized = client.normalize_query(params)
            queries[client.query_key(normalized)] = normalized
    if not queries or len(queries) > MAX_QUERIES_PER_PROPERTY:
        raise RuntimeError("property_query_plan_unavailable_or_over_limit")
    return [queries[key] for key in sorted(queries)]


def collect_exact_summaries(rows, queries, context, client):
    """Finish every applicable query before ranking any discovered certificate."""
    candidates = {}
    for params in queries:
        for candidate in client.search_query(params):
            number = epc.extract_certificate_number(candidate)
            if not number:
                raise RuntimeError("search_identifier_missing")
            try:
                minimized = base.minimized_certificate(candidate)
            except ValueError:
                raise RuntimeError("invalid_search_evidence_shape") from None
            prior = candidates.get(number)
            if prior is not None and prior != minimized:
                raise RuntimeError("conflicting_cross_query_certificate")
            candidates[number] = minimized
    exact = {}
    for number, candidate in candidates.items():
        if any(resolve_identity(row, candidate, context)[0] for row in rows):
            exact[number] = candidate
    if len(exact) > MAX_CERTIFICATES_PER_PROPERTY:
        raise RuntimeError("exact_certificate_review_limit")
    return exact, len(candidates)


def latest_property_record(rows, exact, context, client, candidate_count):
    admitted = []
    latest_seen = ""
    unusable_dates = set()
    for number in sorted(exact):
        full = client.certificate(number)
        try:
            base.minimized_certificate(full)
        except ValueError:
            raise RuntimeError("invalid_certificate_evidence_shape") from None
        compatible = [row for row in rows if resolve_identity(row, exact[number], context)[0]
                      and resolve_identity(row, full, context, summary=exact[number])[0]]
        if not compatible:
            raise RuntimeError("full_certificate_identity_conflict")
        result = base.checked_certificate_record(compatible[0], number, full, exact[number],
                                                 recovery_context=context)
        registered = epc.extract_registration_date(full)
        latest_seen = max(latest_seen, registered)
        if result is None:
            unusable_dates.add(registered)
        else:
            admitted.append(result)
    if not exact:
        return {"status":"no_match", "reason":"no_verified_exact_certificate",
                "candidateCount":candidate_count, "recoveryOutcome":"exhausted_no_exact_match"}
    latest = [record for record in admitted if record["epc"]["epcRegistrationDate"] == latest_seen]
    if not latest or latest_seen in unusable_dates:
        return {"status":"no_match", "reason":"latest_exact_area_unavailable",
                "candidateCount":candidate_count, "recoveryOutcome":"whole_property_area_unavailable"}
    if len({(r["epc"]["floorAreaSqm"], r["epc"]["epcRating"]) for r in latest}) != 1:
        raise RuntimeError("conflicting_latest_exact_certificates")
    result = latest[0]
    result.update(candidateCount=candidate_count, recoveryOutcome="verified_match")
    return result


def expanded_recovery(baseline_payload, context_payload, context, client):
    """Revisit every reviewed target once, including prior no-match and errors."""
    rows = baseline_payload["rows"]
    groups = property_groups(rows)
    target_ids = set(context_payload["properties"])
    if not target_ids or not target_ids <= set(groups):
        raise ValueError("Recovery targets must be a nonempty subset of the frozen property cohort")
    currently_missing = {row["propertyRecordId"] for row in rows if row.get("epcMatched") is not True}
    if not currently_missing <= target_ids:
        # A newly withdrawn baseline certificate needs an updated, reviewed
        # context too. Never quietly omit it from an all-missing recovery run.
        raise RecoveryContextScopeError("Recovery context omits newly unresolved properties")
    plans, pending, exact_by_property, candidate_counts = {}, {}, {}, {}
    for property_id in sorted(target_ids):
        try:
            plans[property_id] = property_queries(groups[property_id], context, client)
        except RuntimeError as error:
            pending[property_id] = base.lookup_error(error)
    if hasattr(client, "prefetch_queries"):
        client.prefetch_queries(params for plan in plans.values() for params in plan)
    for property_id, queries in plans.items():
        try:
            exact, count = collect_exact_summaries(groups[property_id], queries, context, client)
            exact_by_property[property_id] = exact
            candidate_counts[property_id] = count
        except RuntimeError as error:
            pending[property_id] = base.lookup_error(error)
    numbers = {number for exact in exact_by_property.values() for number in exact}
    if hasattr(client, "prefetch_certificates"):
        client.prefetch_certificates(sorted(numbers))
    cache = copy.deepcopy(baseline_payload["cache"])
    results = {}
    for index, property_id in enumerate(sorted(target_ids)):
        if property_id in pending:
            result = pending[property_id]
        else:
            try:
                result = latest_property_record(groups[property_id], exact_by_property[property_id], context,
                                                client, candidate_counts[property_id])
            except RuntimeError as error:
                result = base.lookup_error(error)
        result.update(searchedAt=utc_now(), evidenceScope="expanded-property-register-search",
                      recoveryStrategyVersion=STRATEGY_VERSION, identityGuardVersion=epc.IDENTITY_GUARD_VERSION,
                      recoveryContextSha256=context_digest(context_payload))
        if result["status"] == "error":
            result["recoveryOutcome"] = "request_or_verification_unresolved"
        sale_replay_failed = False
        for row in groups[property_id]:
            record = copy.deepcopy(result)
            record.update(address=row.get("address"), postcode=row.get("postcode"))
            if record["status"] == "matched":
                facts = epc.validated_cached_epc(row, record, recovery_context=context)
                if facts is None:
                    sale_replay_failed = True
                    record = {"status":"error", "reason":"sale_certificate_replay_unresolved",
                              "recoveryOutcome":"request_or_verification_unresolved", "searchedAt":result["searchedAt"],
                              "evidenceScope":result["evidenceScope"], "recoveryStrategyVersion":STRATEGY_VERSION,
                              "recoveryContextSha256":result["recoveryContextSha256"],
                              "address":row.get("address"), "postcode":row.get("postcode")}
                else:
                    record["epc"] = facts
            cache["records"][epc.stable_transaction_key(row)] = record
        results[property_id] = ({**result, "status":"error",
                                 "recoveryOutcome":"request_or_verification_unresolved"}
                                if sale_replay_failed else result)
        if (index + 1) % 100 == 0:
            print(json.dumps({"stage":"expanded_property_admission", "propertiesProcessed":index + 1,
                              "propertiesRequested":len(target_ids), "requests":client.requests}), flush=True)
    output = []
    for row in rows:
        value = epc.without_unverified_epc(row)
        record = cache["records"].get(epc.stable_transaction_key(row))
        facts = epc.validated_cached_epc(row, record, recovery_context=context)
        if facts:
            value.update(epc.publishable_epc_fields(facts))
        output.append(value)
    if base.non_epc_digest(output) != base.non_epc_digest(rows) or base.identity_digest(output) != base.identity_digest(rows):
        raise ValueError("Expanded recovery changed the frozen sale or property cohort")
    if not epc.publication_matches_cache(output, cache, recovery_context=context):
        raise ValueError("Expanded recovery does not replay through certificate cache admission")
    accounting = epc.terminal_cache_accounting(output, cache, 90, recovery_context=context)
    before = sum(row.get("epcMatched") is True for row in rows)
    after = sum(row.get("epcMatched") is True for row in output)
    before_properties = {row["propertyRecordId"] for row in rows if row.get("epcMatched") is True}
    after_properties = {row["propertyRecordId"] for row in output if row.get("epcMatched") is True}
    residual_sales = collections.Counter()
    residual_properties = collections.defaultdict(set)
    for row in output:
        if not row.get("epcMatched"):
            record = cache["records"].get(epc.stable_transaction_key(row), {})
            reason = record.get("reason", "not_verified")
            residual_sales[reason] += 1
            residual_properties[reason].add(row["propertyRecordId"])
    report = {
        "status":"candidate_complete" if accounting["pending"] == 0 else "candidate_partial",
        "createdAt":utc_now(), "transactions":len(rows), "properties":len(groups),
        "verifiedBefore":before, "verifiedAfter":after, "addedVerifiedSales":after - before,
        "verifiedPropertiesBefore":len(before_properties), "verifiedPropertiesAfter":len(after_properties),
        "newlyVerifiedProperties":len(after_properties - before_properties),
        "newlyVerifiedSales":sum(not old.get("epcMatched") and bool(new.get("epcMatched")) for old,new in zip(rows,output)),
        "withdrawnRetainedSales":sum(bool(old.get("epcMatched")) and not new.get("epcMatched") for old,new in zip(rows,output)),
        "targetProperties":len(target_ids), "targetSaleRows":sum(len(groups[key]) for key in target_ids),
        "recoveryOutcomesProperties":dict(collections.Counter(r["recoveryOutcome"] for r in results.values())),
        "recoveryMethodsProperties":dict(collections.Counter(r.get("recoveryMethod", "exact") for r in results.values() if r["status"] == "matched")),
        "residualReasonsSales":dict(residual_sales),
        "residualReasonsProperties":{k:len(v) for k,v in residual_properties.items()},
        "coveragePercent":round(after * 100 / len(rows), 3), "sourceAccounting":accounting,
        "httpRequests":client.requests, "authenticationFailed":client.auth_failed,
        "lookupErrorRows":accounting["errors"], "nonEpcSha256":base.non_epc_digest(output),
        "recoveryContextSha256":context_digest(context_payload), "recoveryStrategyVersion":STRATEGY_VERSION,
        "publicationPerformed":False,
    }
    metadata = copy.deepcopy(baseline_payload["meta"])
    metadata["epcEnrichment"].update(status="complete" if accounting["pending"] == 0 else "partial",
                                   matched=after, coveragePercent=round(after * 100 / len(rows), 1),
                                   evidenceScope="verified-measurements-and-expanded-property-register-search",
                                   expandedSearchCompletedAt=report["createdAt"], **accounting)
    return {"rows":output, "meta":metadata, "cache":cache,
            "initialIdentityReview":baseline_payload["initialIdentityReview"],
            "strictBaselineReport":baseline_payload["report"], "recoveryContext":context_payload,
            "registerEvidence":base.retained_register_evidence(client), "report":report}, report


def failed_expansion_diagnostic(baseline, context_payload, code):
    """Retain only the already completed baseline, never a failed candidate.

    Its register evidence was minimized before backfill returned. Do not inspect
    the live client, exceptions or environment credentials at this boundary.
    The outer diagnostic schema deliberately fails normal candidate admission.
    """
    if code not in {"recovery_context_missing_newly_unresolved", "expanded_validation_failed"}:
        raise ValueError("Unsupported diagnostic failure code")
    source = {
        "inputCommit": base.INPUT_COMMIT, "inputSha256": base.INPUT_SHA256,
        "retainedCacheSha256": base.CACHE_SHA256, "sourceFeedSha256": base.SOURCE_FEED_SHA256,
        "cohortIdentitySha256": base.COHORT_IDENTITY_SHA256,
        "recoveryContextSha256": context_digest(context_payload),
        "producerCommit": os.environ.get("GITHUB_SHA", "local-uncommitted"),
        "githubRunId": os.environ.get("GITHUB_RUN_ID", "local"),
        "githubRunAttempt": os.environ.get("GITHUB_RUN_ATTEMPT", "local"),
    }
    for key, value in source.items():
        if key in {"githubRunId", "githubRunAttempt"}:
            valid = isinstance(value, str) and (value == "local" or re.fullmatch(r"[1-9][0-9]{0,19}", value))
        elif key == "producerCommit" and value == "local-uncommitted":
            valid = True
        else:
            valid = isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}" if key.endswith("Commit") else r"[0-9a-f]{64}", value)
        if not valid:
            raise ValueError("Diagnostic source binding is invalid")
    # Select fields explicitly: neither unrelated future payload additions nor
    # an exception body can silently enter the retained diagnostic.
    selected = {key: baseline[key] for key in ("rows", "meta", "cache", "registerEvidence", "report")}
    projected = base.patch_candidate(selected)
    rows = baseline["rows"]
    report = baseline["report"]
    verified = sum(row.get("epcMatched") is True for row in rows)
    counts = {"strictBaselineTransactions": len(rows), "strictBaselineVerifiedSales": verified,
              "strictBaselineHttpRequests": report["httpRequests"]}
    if (any(type(value) is not int or value < 0 for value in counts.values())
            or report.get("transactions") != len(rows) or report.get("verifiedAfter") != verified):
        raise ValueError("Diagnostic baseline accounting is invalid")
    receipt = {"status": "failed", "stage": "expanded_recovery", "code": code,
               "candidateUsable": False, "publicationPerformed": False, **counts, **source}
    diagnostic = {
        "schema": DIAGNOSTIC_SCHEMA, "candidateUsable": False, "publicationPerformed": False,
        "failure": {key: receipt[key] for key in ("status", "stage", "code")},
        "source": source,
        "strictBaseline": copy.deepcopy({key: projected[key] for key in
            ("epcPatches", "epcEnrichment", "cache", "registerEvidence", "report")}),
    }
    return diagnostic, receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    recipient = os.environ.get("EPC_RESULT_PUBLIC_KEY", "")
    validate_recipient(recipient)
    token = os.environ.get("EPC_BEARER_TOKEN", "").strip()
    if not token or args.output_dir.exists():
        raise ValueError("Provider access and a fresh output destination are required")
    context_payload = decode_context(os.environ.get("EPC_RECOVERY_CONTEXT_B64", ""),
                                     os.environ.get("EPC_RECOVERY_CONTEXT_SHA256", ""))
    rows, meta, cache = base.load_frozen_inputs()
    context = load_recovery_context(context_payload, rows,
                                    expected_sha256=os.environ["EPC_RECOVERY_CONTEXT_SHA256"])
    print(json.dumps({"stage":"trusted_recovery_context_validated", "properties":len(context_payload["properties"]),
                      "contextSha256":context_digest(context_payload)}), flush=True)
    client = base.RegisterClient(token, max_requests=9000, max_seconds=4500)
    baseline, baseline_report = base.backfill(rows, meta, cache, client)
    print(json.dumps({"stage":"strict_baseline_rechecked", "verifiedSales":baseline_report["verifiedAfter"],
                      "requests":client.requests}), flush=True)
    try:
        payload, report = expanded_recovery(baseline, context_payload, context, client)
    except Exception as error:
        code = ("recovery_context_missing_newly_unresolved" if isinstance(error, RecoveryContextScopeError)
                else "expanded_validation_failed")
        try:
            diagnostic, receipt = failed_expansion_diagnostic(baseline, context_payload, code)
            seal_result(diagnostic, receipt, recipient, args.output_dir)
        except Exception:
            # No plaintext fallback, raw exception or second output destination.
            print("Expanded EPC recovery failed validation; no feed or cache was published.")
            return 1
        print(json.dumps(receipt, sort_keys=True), flush=True)
        return 1
    report.update(inputCommit=base.INPUT_COMMIT, inputSha256=base.INPUT_SHA256,
                  retainedCacheSha256=base.CACHE_SHA256, sourceFeedSha256=base.SOURCE_FEED_SHA256,
                  cohortIdentitySha256=base.COHORT_IDENTITY_SHA256,
                  producerCommit=os.environ.get("GITHUB_SHA", "local-uncommitted"),
                  githubRunId=os.environ.get("GITHUB_RUN_ID", "local"),
                  githubRunAttempt=os.environ.get("GITHUB_RUN_ATTEMPT", "local"))
    seal_result(base.patch_candidate(payload), report, recipient, args.output_dir)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["sourceAccounting"]["pending"] == 0 else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("Expanded EPC recovery failed validation; no feed or cache was published.")
        raise SystemExit(1) from None
