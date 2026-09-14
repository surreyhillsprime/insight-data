"""Offline end-to-end recovery contracts; all register responses are synthetic."""

import base64
import copy
import gzip
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import backfill_epc_candidate as base
import enrich_epc_data as epc
import expand_epc_recovery as recovery
from epc_recovery_identity import cohort_digest, context_digest, load_recovery_context
from insight_data_utils import utc_now
from tests.test_epc_backfill_candidate import certificate
from tests.test_epc_identity import sale


def target(paon="2", **extra):
    return sale(paon, propertyRecordId="property:synthetic-" + paon, **extra)


def trusted(rows, entries=None):
    payload = {"schemaVersion": 1, "cohortIdentitySha256": cohort_digest(rows),
               "frozenAppCommit": "a" * 40, "sourceHashes": {},
               "properties": {row["propertyRecordId"]: {} for row in rows}}
    if entries is not None:
        payload["properties"] = entries
    return payload, load_recovery_context(payload, rows, expected_sha256=context_digest(payload))


def baseline(rows, statuses=None):
    cache = {"version": 3, "records": {}}
    statuses = statuses or ["no_match"] * len(rows)
    for row, status in zip(rows, statuses):
        cache["records"][epc.stable_transaction_key(row)] = {
            "status": status, "reason": "previous_lookup", "searchedAt": utc_now(),
            "address": row["address"], "postcode": row["postcode"],
            "identityGuardVersion": epc.IDENTITY_GUARD_VERSION,
        }
    return {"rows": [epc.without_unverified_epc(row) for row in rows],
            "cache": cache, "meta": {"epcEnrichment": {}}, "report": {},
            "initialIdentityReview": {}}


class FixtureRegister(base.RegisterClient):
    """Exercise the real query/cache/pagination code, with no network transport."""
    def __init__(self, summaries=(), full=None, fail=None):
        super().__init__("synthetic-token", spacing=0)
        self.summaries = list(summaries)
        self.full = full if full is not None else {
            item["certificateNumber"]: copy.deepcopy(item) for item in summaries}
        self.fail = fail
        self.calls = []

    def request(self, path, params):
        with self._lock:
            self.requests += 1
            self.calls.append((path, copy.deepcopy(params)))
        if self.fail:
            problem = self.fail(path, params)
            if problem:
                raise RuntimeError(problem)
        if path == "/api/certificate":
            return {"data": copy.deepcopy(self.full[params["certificate_number"]])}
        return {"data": copy.deepcopy(self.summaries), "pagination": {
            "currentPage": 1, "totalPages": 1 if self.summaries else 0,
            "totalRecords": len(self.summaries)}}


class RecoveryDecodeTests(unittest.TestCase):
    def encode(self, raw):
        return base64.b64encode(gzip.compress(raw)).decode()

    def test_external_hash_and_compression_bounds(self):
        payload, _ = trusted([target()])
        encoded = self.encode(json.dumps(payload).encode())
        self.assertEqual(recovery.decode_context(encoded, context_digest(payload)), payload)
        for value, digest in [(encoded, "f" * 64), (encoded, ""), ("not base64", "f" * 64),
                              ("A" * (recovery.MAX_CONTEXT_ENCODED + 1), "f" * 64),
                              (self.encode(b" " * (recovery.MAX_CONTEXT_BYTES + 1)), "f" * 64)]:
            with self.subTest(size=len(value)), self.assertRaises(ValueError):
                recovery.decode_context(value, digest)

    def test_duplicate_keys_and_nonfinite_values_cannot_hash_as_valid_context(self):
        for raw, replacement in [(b'{"x":1,"x":2}', {"x": 2}), (b'{"x":NaN}', {"x": None})]:
            with self.assertRaises(ValueError):
                recovery.decode_context(self.encode(raw), context_digest(replacement))


class RecoveryExecutionTests(unittest.TestCase):
    def run_recovery(self, rows, client, statuses=None, entries=None):
        payload, context = trusted(rows, entries)
        initial = baseline(rows, statuses)
        original = copy.deepcopy(initial)
        result, report = recovery.expanded_recovery(initial, payload, context, client)
        self.assertEqual(initial, original)
        self.assertEqual(base.non_epc_digest(rows), base.non_epc_digest(result["rows"]))
        self.assertEqual(base.identity_digest(rows), base.identity_digest(result["rows"]))
        self.assertTrue(epc.publication_matches_cache(result["rows"], result["cache"], recovery_context=context))
        return result, report, context

    def test_all_prior_statuses_reopened_and_repeated_sales_repriced(self):
        rows = [target(id="first", planningConstraints={"preserve": True}),
                target(id="repeat", date="2025-02-01", price=4_000_000),
                target("3", id="third")]
        client = FixtureRegister([certificate(), certificate("third-cert", paon="3")])
        result, report, context = self.run_recovery(rows, client, ["no_match", "error", "no_match"])
        self.assertEqual(report["targetProperties"], 2)
        self.assertEqual(report["targetSaleRows"], 3)
        self.assertEqual(report["verifiedAfter"], 3)
        self.assertEqual(report["sourceAccounting"]["pending"], 0)
        self.assertNotEqual(result["rows"][0]["pricePerSqft"], result["rows"][1]["pricePerSqft"])
        self.assertEqual(result["rows"][0]["planningConstraints"], {"preserve": True})
        query_calls = [(p, q) for p, q in client.calls if p.endswith("search")]
        self.assertEqual(len(query_calls), len({client.query_key({k: v for k, v in q.items()
                         if k not in ("current_page", "page_size")}) for _, q in query_calls}))
        with patch.object(base, "COHORT_IDENTITY_SHA256", base.identity_digest(rows)):
            transported = base.patch_candidate(result)
        replay = base.apply_epc_patches(rows, transported["epcPatches"], transported["cache"], recovery_context=context)
        self.assertEqual(replay, result["rows"])

    def test_every_query_must_complete_before_a_match_is_admitted(self):
        client = FixtureRegister([certificate()], fail=lambda path, params:
                                 "register_http_503" if "address" in params else None)
        result, report, _ = self.run_recovery([target()], client)
        self.assertEqual(report["sourceAccounting"]["pending"], 1)
        self.assertEqual(report["sourceAccounting"]["errors"], 1)
        self.assertFalse(result["rows"][0]["epcMatched"])
        self.assertFalse(any(path == "/api/certificate" for path, _ in client.calls))

    def test_latest_unusable_area_never_falls_back_to_older_or_summary_area(self):
        summaries = [certificate("old", date="2020-01-01"), certificate("new", area=250)]
        full = {c["certificateNumber"]: copy.deepcopy(c) for c in summaries}
        full["new"]["totalFloorArea"] = None
        result, report, _ = self.run_recovery([target()], FixtureRegister(summaries, full))
        self.assertEqual(report["sourceAccounting"]["pending"], 0)
        self.assertEqual(report["residualReasonsSales"], {"latest_exact_area_unavailable": 1})
        self.assertNotIn("pricePerSqft", result["rows"][0])

    def test_conflicting_latest_area_is_unresolved(self):
        _, report, _ = self.run_recovery([target()], FixtureRegister([
            certificate("a", area=200), certificate("b", area=201)]))
        self.assertEqual(report["sourceAccounting"]["errors"], 1)
        self.assertEqual(report["residualReasonsSales"], {"conflicting_latest_exact_certificates": 1})

    def test_invalid_rating_is_unresolved_even_with_usable_area(self):
        _, report, _ = self.run_recovery([target()], FixtureRegister([certificate(rating="H")]))
        self.assertEqual(report["sourceAccounting"]["pending"], 1)
        self.assertEqual(report["residualReasonsSales"], {"invalid_certificate_rating": 1})

    def test_replay_failure_for_one_sale_stays_pending_in_property_and_row_counts(self):
        rows = [target(id="first"), target(id="repeat", date="2025-02-01")]
        original = epc.validated_cached_epc

        def fail_repeat(row, *args, **kwargs):
            return None if row["id"] == "repeat" else original(row, *args, **kwargs)

        with patch.object(epc, "validated_cached_epc", side_effect=fail_repeat):
            result, report, _ = self.run_recovery(rows, FixtureRegister([certificate()]))
        self.assertEqual(report["verifiedAfter"], 1)
        self.assertEqual(report["sourceAccounting"]["pending"], 1)
        self.assertEqual(report["recoveryOutcomesProperties"], {"request_or_verification_unresolved": 1})
        failed = result["cache"]["records"][epc.stable_transaction_key(rows[1])]
        self.assertEqual(failed["status"], "error")
        self.assertEqual(failed["reason"], "sale_certificate_replay_unresolved")

    def test_provider_failures_preserve_partial_results_and_hide_error_bodies(self):
        _, report, _ = self.run_recovery([target()], FixtureRegister([certificate()],
            fail=lambda path, params: "unexpected body with private details" if path == "/api/certificate" else None))
        self.assertEqual(report["sourceAccounting"]["errors"], 1)
        self.assertEqual(report["residualReasonsSales"], {"register_request_failed": 1})
        self.assertNotIn("private details", json.dumps(report))

    def test_full_certificate_identity_conflict_is_not_no_match(self):
        _, report, _ = self.run_recovery([target()], FixtureRegister([certificate()],
            {"synthetic-one": certificate(paon="8")}))
        self.assertEqual(report["sourceAccounting"]["pending"], 1)
        self.assertEqual(report["residualReasonsSales"], {"full_certificate_identity_conflict": 1})

    def test_no_exact_result_is_complete_without_discarding_canonical_sale(self):
        result, report, _ = self.run_recovery([target()], FixtureRegister([certificate(paon="8")]))
        self.assertEqual(report["sourceAccounting"]["noMatchCacheRecords"], 1)
        self.assertEqual(report["sourceAccounting"]["pending"], 0)
        self.assertEqual(result["rows"][0]["propertyRecordId"], target()["propertyRecordId"])

    def test_newly_unresolved_property_outside_reviewed_context_stops_before_queries(self):
        rows = [target(id="first"), target("8", id="other")]
        client = FixtureRegister([certificate()])
        with self.assertRaisesRegex(ValueError, "omits newly unresolved"):
            self.run_recovery(rows, client, entries={rows[0]["propertyRecordId"]: {}})
        self.assertEqual(client.calls, [])

    def test_source_shape_error_has_symbolic_helper_boundary(self):
        row = target()
        _, context = trusted([row])
        malformed = certificate()
        malformed["uprn"] = {"contact": "private"}
        client = FixtureRegister()
        client.search_query = lambda params: [malformed]
        with self.assertRaisesRegex(RuntimeError, "invalid_search_evidence_shape"):
            recovery.collect_exact_summaries([row], [{"postcode": "GU195AE"}], context, client)
        client.certificate = lambda number: malformed
        with self.assertRaisesRegex(RuntimeError, "invalid_certificate_evidence_shape"):
            recovery.latest_property_record([row], {"synthetic-one": certificate()}, context, client, 1)


class RecoveryEntrypointTests(unittest.TestCase):
    def test_pinned_context_produces_epc_only_sealed_candidate_with_safe_receipt(self):
        rows = [target()]
        context_payload, _ = trusted(rows)
        context_bytes = base64.b64encode(gzip.compress(json.dumps(context_payload).encode())).decode()
        client = FixtureRegister([certificate()])
        constructor_calls = []

        class EntrypointClient(base.RegisterClient):
            def __new__(cls, *args, **kwargs):
                constructor_calls.append((args, kwargs))
                return client

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "result"
            environment = {"EPC_BEARER_TOKEN": "synthetic-token", "EPC_RESULT_PUBLIC_KEY": "synthetic-public-key",
                           "EPC_RECOVERY_CONTEXT_B64": context_bytes,
                           "EPC_RECOVERY_CONTEXT_SHA256": context_digest(context_payload),
                           "GITHUB_SHA": "b" * 40, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}
            with patch.dict(os.environ, environment, clear=True), \
                    patch.object(sys, "argv", ["expand_epc_recovery", "--output-dir", str(destination)]), \
                    patch.object(recovery, "validate_recipient"), \
                    patch.object(base, "load_frozen_inputs", return_value=(rows, {}, {"records": {}})), \
                    patch.object(base, "COHORT_IDENTITY_SHA256", base.identity_digest(rows)), \
                    patch.object(base, "RegisterClient", EntrypointClient), \
                    patch.object(recovery, "seal_result") as seal, redirect_stdout(io.StringIO()) as output:
                self.assertEqual(recovery.main(), 0)
            self.assertEqual(constructor_calls, [(("synthetic-token",), {"max_requests": 9000, "max_seconds": 4500})])
            candidate, receipt, recipient, path = seal.call_args.args
            self.assertEqual(candidate["recoveryContext"], context_payload)
            self.assertEqual(candidate["report"], receipt)
            self.assertEqual(receipt["producerCommit"], "b" * 40)
            self.assertFalse(receipt["publicationPerformed"])
            self.assertNotIn("rows", candidate)
            self.assertNotIn("meta", candidate)
            self.assertEqual(path, destination)
            self.assertEqual(recipient, "synthetic-public-key")
            for private_value in (rows[0]["address"], rows[0]["propertyRecordId"], "synthetic-token", context_bytes):
                self.assertNotIn(private_value, json.dumps(receipt))
                self.assertNotIn(private_value, output.getvalue())

    def test_context_tampering_stops_before_provider_client_or_output(self):
        payload, _ = trusted([target()])
        encoded = base64.b64encode(gzip.compress(json.dumps(payload).encode())).decode()
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {"EPC_BEARER_TOKEN": "synthetic-token", "EPC_RESULT_PUBLIC_KEY": "synthetic-public-key",
                          "EPC_RECOVERY_CONTEXT_B64": encoded, "EPC_RECOVERY_CONTEXT_SHA256": "f" * 64}, clear=True), \
                patch.object(sys, "argv", ["expand_epc_recovery", "--output-dir", temporary + "/result"]), \
                patch.object(recovery, "validate_recipient"), patch.object(base, "RegisterClient") as client, \
                patch.object(recovery, "seal_result") as seal:
            with self.assertRaises(ValueError):
                recovery.main()
            client.assert_not_called()
            seal.assert_not_called()


if __name__ == "__main__":
    unittest.main()
