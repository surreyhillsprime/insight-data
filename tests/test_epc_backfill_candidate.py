"""Candidate retrieval tests use synthetic certificates and no provider access."""

import copy
import io
import json
import sys
import time
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import backfill_epc_candidate as candidate
import enrich_epc_data as epc
from tests.test_epc_identity import sale, record


def certificate(number="synthetic-one", paon="2", area=200, date="2025-01-01", rating="C"):
    return {"certificateNumber": number, "addressLine1": paon + " HIGH STREET",
            "postTown": "BAGSHOT", "postcode": "GU19 5AE", "totalFloorArea": area,
            "registrationDate": date, "currentEnergyEfficiencyBand": rating}


class MemoryClient:
    def __init__(self, certificates):
        self.values = certificates
        self.requests = 0
        self.max_requests = 100
        self.deadline = time.monotonic() + 100
        self.auth_failed = False
        self.searched = []

    def search(self, row):
        self.requests += 1
        self.searched.append(row["id"])
        return self.values

    def certificate(self, number):
        self.requests += 1
        return next(value for value in self.values if value["certificateNumber"] == number)


class Response(io.BytesIO):
    pass


class Opener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return Response(json.dumps(response).encode())


class CandidateMatchingTests(unittest.TestCase):
    def test_epc_patches_preserve_app_context_and_clear_old_unknown_measurements(self):
        original = sale(planningConstraints={"preserved": True}, floorAreaSqm=999,
                        floorAreaSqft=10000, pricePerSqft=300, epcMatched=True)
        cache = {"version": 3, "records": {}}
        patched = candidate.apply_epc_patches([original], [{"id": original["id"], "epcMatched": False}], cache)
        self.assertEqual(patched[0]["planningConstraints"], {"preserved": True})
        self.assertNotIn("floorAreaSqm", patched[0])
        self.assertNotIn("pricePerSqft", patched[0])
        self.assertEqual(candidate.non_epc_digest([original]), candidate.non_epc_digest(patched))
        for patch in [
            {"id": "other", "epcMatched": False},
            {"id": original["id"], "epcMatched": False, "uprn": "different"},
            {"id": original["id"], "epcMatched": True, "floorAreaSqm": 200},
        ]:
            with self.assertRaises(ValueError):
                candidate.apply_epc_patches([original], [patch], cache)
        with self.assertRaises(ValueError):
            candidate.apply_epc_patches([original], [], cache)

    def test_patch_transport_contains_no_full_context_rows_or_source_metadata(self):
        row = sale(planningConstraints={"private_context": True})
        payload = {"rows": [row], "meta": {"epcEnrichment": {"status": "partial"},
                   "propertyContext": "do not copy"}, "cache": {"records": {}}, "report": {}}
        with patch.object(candidate, "COHORT_IDENTITY_SHA256", candidate.identity_digest([row])):
            result = candidate.patch_candidate(payload)
        self.assertNotIn("rows", result)
        self.assertNotIn("meta", result)
        self.assertNotIn("planningConstraints", result["epcPatches"][0])
        self.assertNotIn("propertyContext", json.dumps(result))
        self.assertEqual(result["frozenAppInputSha256"], candidate.INPUT_SHA256)

    def test_full_certificate_required_and_wrong_neighbour_rejected(self):
        client = MemoryClient([certificate(paon="8")])
        result = candidate.exact_register_match(sale(), client)
        self.assertEqual(result["status"], "no_match")
        self.assertEqual(client.requests, 1)
        client = MemoryClient([certificate()])
        client.certificate = lambda number: certificate(paon="8")
        with self.assertRaisesRegex(RuntimeError, "identity_conflict"):
            candidate.exact_register_match(sale(), client)

    def test_latest_exact_certificate_selected_and_conflicting_tie_unresolved(self):
        client = MemoryClient([certificate("old", date="2020-01-01", area=180),
                               certificate("new", date="2025-01-01", area=250)])
        self.assertEqual(candidate.exact_register_match(sale(), client)["epc"]["floorAreaSqm"], 250)
        client.values.append(certificate("tie", date="2025-01-01", area=251))
        with self.assertRaisesRegex(RuntimeError, "conflicting_latest"):
            candidate.exact_register_match(sale(), client)

    def test_summary_floor_area_is_never_substituted_for_missing_full_data(self):
        client = MemoryClient([certificate()])
        client.certificate = lambda number: certificate(area=None)
        self.assertEqual(candidate.exact_register_match(sale(), client)["status"], "no_match")

    def test_documented_full_schema_without_repeated_identifier_remains_request_bound(self):
        full = {"address_line_1": "2 HIGH STREET", "post_town": "BAGSHOT",
                "postcode": "GU19 5AE", "total_floor_area": 240,
                "registration_date": "2025-01-01", "schema_type": "RdSAP-Schema-21.0.1"}
        client = MemoryClient([certificate(rating="B")])
        client.certificate = lambda number: full
        result = candidate.exact_register_match(sale(), client)
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["epc"]["floorAreaSqm"], 240)
        self.assertEqual(result["epc"]["epcRating"], "B")
        self.assertEqual(result["certificateFetch"]["requestedNumber"], "synthetic-one")
        self.assertEqual(result["certificateFetch"]["returnedNumber"], "")
        full["registration_date"] = "2024-01-01"
        with self.assertRaisesRegex(RuntimeError, "registration_conflict"):
            candidate.exact_register_match(sale(), client)
        client.certificate = lambda number: certificate(date="2025-02-31")
        with self.assertRaisesRegex(RuntimeError, "invalid_certificate_date"):
            candidate.exact_register_match(sale(), client)
        client = MemoryClient([certificate("old", date="2020-01-01"), certificate("new", area=None)])
        self.assertEqual(candidate.exact_register_match(sale(), client)["status"], "no_match")

    def test_refetch_retained_measurements_and_search_only_unresolved_reprice_repeats(self):
        old = sale("1", id="verified")
        first = sale(id="missing-one")
        repeat = sale(id="missing-repeat", date="2025-02-01", price=4_000_000)
        cache = {"version": 3, "records": {epc.stable_transaction_key(old):
                 record("1 HIGH STREET BAGSHOT GU19 5AE", number="retained")}}
        original = copy.deepcopy(cache)
        rows = [old, first, repeat]
        client = MemoryClient([certificate(), certificate("retained", paon="1", date="2024-01-01", rating="D")])
        payload, report = candidate.backfill(rows, {}, cache, client)
        self.assertEqual(cache, original)
        self.assertEqual(client.searched, ["missing-one"])
        self.assertEqual(report["verifiedBefore"], 1)
        self.assertEqual(report["verifiedAfter"], 3)
        self.assertEqual(report["addedVerifiedSales"], 2)
        self.assertEqual(report["uniquePropertyLookups"], 1)
        self.assertEqual(report["retainedTransactionRowsRechecked"], 1)
        self.assertEqual(report["verifiedRetainedCertificateIds"], 1)
        self.assertEqual(report["sourceAccounting"]["pending"], 0)
        self.assertEqual(candidate.non_epc_digest(rows), candidate.non_epc_digest(payload["rows"]))
        a, b = payload["rows"][1:]
        self.assertEqual(a["floorAreaSqft"], b["floorAreaSqft"])
        self.assertNotEqual(a["pricePerSqft"], b["pricePerSqft"])
        self.assertTrue(all(set(row) <= epc.PUBLIC_EPC_FIELDS | set(rows[i])
                            for i, row in enumerate(payload["rows"])))

    def test_retained_partial_floor_is_replaced_by_full_sap_area_with_replayable_evidence(self):
        row = sale()
        cached = record("2 HIGH STREET BAGSHOT GU19 5AE", number="retained", area=100)
        cache = {"version": 3, "records": {epc.stable_transaction_key(row): cached}}
        full = certificate("retained", date="2024-01-01", rating="D")
        del full["totalFloorArea"]
        full.update({"schema_type": "SAP-Schema-12.0", "assessor_name": "must be removed",
                     "sap_building_parts": [{"building_part_number": 1, "private_note": "remove",
                         "sap_floor_dimensions": [
                             {"floor": 0, "total_floor_area": 100, "heat_loss_area": 45},
                             {"floor": 1, "total_floor_area": 100},
                         ], "sap_room_in_roof": {"floor_area": 30.5, "other": "remove"}}]})
        client = MemoryClient([full])
        client.certificates = {"retained": full}
        payload, report = candidate.backfill([row], {}, cache, client)
        self.assertEqual(payload["rows"][0]["floorAreaSqm"], 231)
        self.assertEqual(report["correctedRetainedAreaSales"], 1)
        self.assertEqual(report["withdrawnRetainedSales"], 0)
        self.assertEqual(report["newlyVerifiedSales"], 0)
        kept = payload["registerEvidence"]["requestedCertificates"]["retained"]
        evidence = payload["cache"]["records"][epc.stable_transaction_key(row)]["floorAreaEvidence"]
        self.assertEqual(epc.floor_area_evidence(kept), evidence)
        self.assertNotIn("must be removed", json.dumps(kept))
        self.assertNotIn("heat_loss_area", json.dumps(kept))
        self.assertNotIn("private_note", json.dumps(kept))
        self.assertNotIn("other", json.dumps(kept))
        self.assertEqual(cached["epc"]["floorAreaSqm"], 100)

    def test_retained_missing_or_conflicting_measurement_cannot_keep_old_ppsf(self):
        for full in (certificate("retained", area=None, date="2024-01-01"),
                     certificate("retained", paon="8", date="2024-01-01")):
            row = sale()
            cache = {"version": 3, "records": {epc.stable_transaction_key(row):
                     record("2 HIGH STREET BAGSHOT GU19 5AE", number="retained")}}
            payload, report = candidate.backfill([row], {}, cache, MemoryClient([full]))
            self.assertFalse(payload["rows"][0]["epcMatched"])
            self.assertNotIn("pricePerSqft", payload["rows"][0])
            self.assertEqual(report["withdrawnRetainedSales"], 1)
            self.assertEqual(report["addedVerifiedSales"], -1)
            self.assertEqual(report["sourceAccounting"]["pending"], 1)
            self.assertEqual(report["sourceAccounting"]["noMatchCacheRecords"], 0)

    def test_minimization_preserves_all_supported_certificate_aliases(self):
        for keys, extract, value in [
            (("certificateNumber", "certificate_number", "certificate-number", "lmkKey", "lmk-key", "LMK_KEY"),
             epc.extract_certificate_number, "synthetic"),
            (("registrationDate", "registration_date", "lodgementDate", "lodgement_date", "lodgement-datetime"),
             epc.extract_registration_date, "2025-01-01"),
            (("currentEnergyEfficiencyBand", "current_energy_efficiency_band", "current-energy-efficiency", "current-energy-rating"),
             epc.extract_rating, "B"),
        ]:
            for key in keys:
                source = {key: value, "assessor_contact": "removed"}
                kept = candidate.minimized_certificate(source)
                self.assertEqual(extract(kept), value)
                self.assertNotIn("assessor_contact", kept)

    def test_duplicate_sale_keys_keep_baseline_identity_when_refetch_fails(self):
        first, second = sale(id="one"), sale(id="two")
        self.assertEqual(epc.stable_transaction_key(first), epc.stable_transaction_key(second))
        cache = {"version": 3, "records": {epc.stable_transaction_key(first):
                 record("2 HIGH STREET BAGSHOT GU19 5AE", number="retained")}}
        client = MemoryClient([certificate("retained", area=None, date="2024-01-01")])
        payload, report = candidate.backfill([first, second], {}, cache, client)
        self.assertEqual(report["withdrawnRetainedSales"], 2)
        self.assertEqual(report["sourceAccounting"]["pending"], 2)
        self.assertEqual(report["sourceAccounting"]["errors"], 2)
        self.assertEqual(client.requests, 1)
        self.assertTrue(all(not row["epcMatched"] for row in payload["rows"]))
        self.assertEqual([row["id"] for row in payload["rows"]], ["one", "two"])

    def test_prefetched_source_evidence_is_admitted_after_last_request_budget_is_spent(self):
        old = sale("1", id="verified")
        new = sale(id="new")
        retained = certificate("retained", paon="1", date="2024-01-01", rating="D")
        fresh = certificate("fresh")
        cache = {"version": 3, "records": {epc.stable_transaction_key(old):
                 record("1 HIGH STREET BAGSHOT GU19 5AE", number="retained")}}
        opener = Opener([{"data": retained}, {"data": [retained, fresh], "pagination": {
            "totalRecords": 2, "totalPages": 1, "currentPage": 1}}, {"data": fresh}])
        client = candidate.RegisterClient("synthetic", opener=opener, spacing=0, max_requests=3)
        payload, report = candidate.backfill([old, new], {}, cache, client)
        self.assertEqual(report["verifiedAfter"], 2)
        self.assertEqual(report["sourceAccounting"]["pending"], 0)
        self.assertEqual(client.requests, 3)
        self.assertEqual(set(payload["registerEvidence"]["requestedCertificates"]), {"retained", "fresh"})

    def test_last_allowed_lookup_is_reused_for_later_sales_of_same_property(self):
        client = MemoryClient([certificate()])
        client.max_requests = 2
        rows = [sale(id="one"), sale(id="two", date="2025-02-01")]
        payload, report = candidate.backfill(rows, {}, {"version": 3, "records": {}}, client)
        self.assertEqual(report["verifiedAfter"], 2)
        self.assertEqual(client.requests, 2)
        self.assertEqual(report["sourceAccounting"]["pending"], 0)

    def test_error_stays_pending_and_provider_body_not_retained_or_logged(self):
        client = MemoryClient([])
        client.search = lambda row: (_ for _ in ()).throw(RuntimeError("Bearer synthetic-secret"))
        with redirect_stdout(io.StringIO()) as output:
            payload, report = candidate.backfill([sale()], {}, {"version": 3, "records": {}}, client)
        self.assertNotIn("synthetic-secret", json.dumps(payload) + output.getvalue())
        self.assertFalse(payload["rows"][0]["epcMatched"])
        self.assertEqual(report["sourceAccounting"]["pending"], 1)
        self.assertEqual(report["sourceAccounting"]["errors"], 1)
        self.assertEqual(report["status"], "candidate_partial")

    def test_completed_no_exact_match_distinguished_from_budget_not_searched(self):
        client = MemoryClient([])
        payload, report = candidate.backfill([sale()], {}, {"version": 3, "records": {}}, client)
        self.assertEqual(report["sourceAccounting"]["noMatchCacheRecords"], 1)
        self.assertEqual(report["sourceAccounting"]["resolved"], 1)
        self.assertEqual(report["verifiedAfter"], 0)
        client.max_requests = 0
        payload, report = candidate.backfill([sale()], {}, {"version": 3, "records": {}}, client)
        self.assertEqual(report["sourceAccounting"]["noMatchCacheRecords"], 0)
        self.assertEqual(report["sourceAccounting"]["pending"], 1)
        self.assertEqual(report["attemptedTransactionRows"], 0)


class RegisterTransportTests(unittest.TestCase):
    def client(self, responses):
        opener = Opener(responses)
        return candidate.RegisterClient("synthetic-token", opener=opener, sleep=lambda value: None), opener

    def test_complete_pagination_and_postcode_request_cache(self):
        first = certificate("one")
        second = certificate("two")
        client, opener = self.client([
            {"data": [first], "pagination": {"totalRecords": 2, "totalPages": 2, "currentPage": 1}},
            {"data": [second], "pagination": {"totalRecords": 2, "totalPages": 2, "currentPage": 2}},
        ])
        self.assertEqual(client.search(sale()), [first, second])
        self.assertEqual(client.search(sale("3")), [first, second])
        self.assertEqual(len(opener.requests), 2)
        self.assertIn("current_page=2", opener.requests[1].full_url)

    def test_incomplete_or_changing_pages_never_become_no_match(self):
        for pagination in [
            {"totalRecords": 2, "totalPages": 1, "currentPage": 1},
            {"totalRecords": 0, "totalPages": 22, "currentPage": 1},
            {"totalRecords": 0, "totalPages": 1, "currentPage": "1"},
        ]:
            client, _ = self.client([{"data": [], "pagination": pagination}])
            with self.assertRaises(RuntimeError):
                client.search(sale())
            self.assertEqual(client.searches, {})
        client, _ = self.client([{"data": []}])
        with self.assertRaisesRegex(RuntimeError, "incomplete_search_pagination"):
            client.search(sale())

    def test_unscoped_request_and_cross_origin_redirect_are_rejected(self):
        client, opener = self.client([])
        with self.assertRaisesRegex(RuntimeError, "postcode_unresolved"):
            client.search(sale(postcode=""))
        self.assertEqual(opener.requests, [])
        with self.assertRaises(ValueError):
            client.request("https://different.example/", {})
        handler = candidate.NoRedirects()
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://different.example"))

    def test_duplicate_pages_cannot_hide_missing_newer_certificates(self):
        page = {"data": [certificate()], "pagination": {
            "totalRecords": 2, "totalPages": 2, "currentPage": 1}}
        second = copy.deepcopy(page)
        second["pagination"]["currentPage"] = 2
        client, _ = self.client([page, second])
        with self.assertRaisesRegex(RuntimeError, "duplicate_or_missing"):
            client.search(sale())
        self.assertEqual(client.searches, {})

    def test_auth_failure_stops_further_requests(self):
        failure = urllib.error.HTTPError("https://example.invalid", 401, "denied", {}, io.BytesIO(b"private"))
        client, opener = self.client([failure])
        with self.assertRaisesRegex(RuntimeError, "register_http_401"):
            client.search(sale())
        with self.assertRaisesRegex(RuntimeError, "authentication_failed"):
            client.search(sale())
        self.assertEqual(len(opener.requests), 1)

    def test_response_size_budget_and_full_certificate_identifier(self):
        with patch.object(candidate, "MAX_RESPONSE_BYTES", 4):
            with self.assertRaisesRegex(RuntimeError, "response_size_limit"):
                candidate.bounded_body(io.BytesIO(b"12345"))
        client, opener = self.client([{"data": certificate("wrong-id")}])
        with self.assertRaisesRegex(RuntimeError, "certificate_number_conflict"):
            client.certificate("expected-id")
        client.max_requests = client.requests
        with self.assertRaisesRegex(RuntimeError, "budget_reached"):
            client.request("/api/certificate", {"certificate_number": "other"})
        self.assertEqual(len(opener.requests), 1)

    def test_frozen_input_hash_checked_before_reading_any_other_inputs(self):
        with patch.object(Path, "read_bytes", return_value=b"different cohort"):
            with self.assertRaisesRegex(ValueError, "transaction input hash"):
                candidate.load_frozen_inputs("synthetic.js")


if __name__ == "__main__":
    unittest.main()
