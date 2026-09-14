"""Synthetic generalized EPC queries preserve scope, budgets and replay evidence."""

import copy
import io
import json
import sys
import threading
import unittest
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import backfill_epc_candidate as candidate
from tests.test_epc_backfill_concurrency import SyntheticOpener, empty_search, response


def certificate(number="one", **values):
    return {"certificateNumber": number, "addressLine1": "1 TEST ROAD",
            "postTown": "BAGSHOT", "postcode": "GU19 5AE",
            "registrationDate": "2026-07-01", "totalFloorArea": 150,
            "currentEnergyEfficiencyBand": "D", **values}


def page(records, current=1, total=None, pages=1):
    return {"data": records, "pagination": {
        "totalRecords": len(records) if total is None else total,
        "totalPages": pages, "currentPage": current}}


class GeneralizedRegisterQueryTests(unittest.TestCase):
    def test_normalization_and_query_key_bind_every_filter_and_strategy(self):
        first = {"postcode": " gu19 5ae ", "address": "  1   Test Road, Bagshot ",
                 "uprn": 123456, "council[]": [" Surrey   Heath ", "Elmbridge", "Surrey Heath"]}
        expected = {"postcode": "GU195AE", "address": "1 TEST ROAD, BAGSHOT",
                    "uprn": "000000123456", "council[]": ["Elmbridge", "Surrey Heath"]}
        self.assertEqual(candidate.RegisterClient.normalize_query(first), expected)
        key = candidate.RegisterClient.query_key(first)
        self.assertEqual(candidate.RegisterClient.query_key(expected), key)
        for changed in [{"postcode": "KT112PB"}, {"address": "2 TEST ROAD, BAGSHOT"},
                        {"uprn": "000000123457"}, {"council[]": ["Elmbridge"]}]:
            self.assertNotEqual(candidate.RegisterClient.query_key({**expected, **changed}), key)
        with patch.object(candidate.RegisterClient, "SEARCH_STRATEGY_VERSION", "test-next-strategy"):
            self.assertNotEqual(candidate.RegisterClient.query_key(expected), key)
        self.assertEqual(first["council[]"], [" Surrey   Heath ", "Elmbridge", "Surrey Heath"])

    def test_invalid_or_unbounded_filters_cannot_admit_any_network_request(self):
        opener = SyntheticOpener()
        client = candidate.RegisterClient("synthetic", opener=opener, spacing=0)
        invalid = [None, {}, [], {"unknown": "value"}, {"address": "*"},
                   {"address": "1% ROAD"}, {"address": "1_ROAD"}, {"address": "1? ROAD"},
                   {"address": ".."}, {"address": "A"}, {"address": "A" * 241},
                   {"address": {"contact": "private"}}, {"postcode": "GU19"},
                   {"postcode": "GU19*5AE"}, {"postcode": ""}, {"postcode": 12345},
                   {"postcode": "GU19 5AE", "address": ""},
                   {"uprn": True}, {"uprn": 12.0}, {"uprn": "0"},
                   {"uprn": "1234567890123"}, {"uprn": "-1"}, {"uprn": "1 23"},
                   {"council[]": "Elmbridge"}, {"council[]": []},
                   {"council[]": ["E07000214"]}, {"council[]": ["w06000015"]},
                   {"council[]": ["Elmbridge", " E07000207 "]}, {"council[]": ["Elmbridge"] * 21},
                   {"council[]": [""]}, {"council[]": [" "]}, {"council[]": [".."]},
                   {"council[]": ["Elmbridge%"]}, {"council[]": ["Elmbridge*"]},
                   {"council[]": ["Elmbridge_"]}, {"council[]": ["Elmbridge?"]},
                   {"council[]": ["Elmbridge\n"]}, {"council[]": ["A" * 81]},
                   {"council[]": [12345]}, {"page_size": 1, "address": "TEST ROAD"}]
        for params in invalid:
            with self.subTest(params=params), self.assertRaisesRegex(RuntimeError, "invalid_search_"):
                client.search_query(params)
        client.prefetch_queries(invalid)
        self.assertEqual(client.requests, 0)
        self.assertEqual(opener.calls, [])
        self.assertEqual(client.query_searches, {})

    def test_literal_address_only_uprn_only_and_repeated_council_parameters(self):
        opener = SyntheticOpener()
        client = candidate.RegisterClient("synthetic", opener=opener, spacing=0)
        queries = [{"address": "1 Test Road, Bagshot"}, {"uprn": "12345"},
                   {"council[]": ["Guildford", "Elmbridge"]}]
        client.prefetch_queries(queries)
        parsed = [urllib.parse.parse_qs(urllib.parse.urlsplit(url).query) for url in opener.calls]
        self.assertEqual(len(parsed), 3)
        self.assertIn({"address": ["1 TEST ROAD, BAGSHOT"], "page_size": ["5000"], "current_page": ["1"]}, parsed)
        self.assertIn({"uprn": ["000000012345"], "page_size": ["5000"], "current_page": ["1"]}, parsed)
        self.assertIn({"council[]": ["Elmbridge", "Guildford"], "page_size": ["5000"], "current_page": ["1"]}, parsed)

    def test_official_council_names_preserve_casing_and_punctuation_in_request_and_evidence(self):
        names = ["King's Lynn and West Norfolk", "Bournemouth, Christchurch and Poole",
                 "Newcastle-under-Lyme", "St. Helens", "King's Lynn and West Norfolk"]
        expected = sorted(set(names))
        opener = SyntheticOpener()
        client = candidate.RegisterClient("synthetic", opener=opener, spacing=0)
        client.search_query({"council[]": names})
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(opener.calls[0]).query)
        self.assertEqual(params["council[]"], expected)
        key = client.query_key({"council[]": names})
        retained = candidate.retained_register_evidence(client)["querySearches"][key]
        self.assertEqual(retained["params"], {"council[]": expected})
        self.assertTrue(retained["complete"])
        self.assertNotEqual(client.query_key({"council[]": ["Elmbridge"]}),
                            client.query_key({"council[]": ["ELMBRIDGE"]}))

    def test_direct_request_rejects_unscoped_or_unbounded_page_parameters(self):
        client = candidate.RegisterClient("synthetic", opener=SyntheticOpener(), spacing=0)
        for params in [{}, {"page_size": 5000, "current_page": 1},
                       {"address": "TEST ROAD", "page_size": 5001, "current_page": 1},
                       {"address": "TEST ROAD", "page_size": 5000, "current_page": 21},
                       {"address": "TEST ROAD", "page_size": 5000, "current_page": True},
                       {"address": "TEST ROAD", "page_size": 5000, "current_page": 1, "unapproved": "value"}]:
            with self.subTest(params=params), self.assertRaises(RuntimeError):
                client.request("/api/domestic/search", params)
        self.assertEqual(client.requests, 0)

    def test_legacy_negative_cache_does_not_answer_generalized_query(self):
        item = certificate()
        client = candidate.RegisterClient("synthetic", opener=SyntheticOpener(
            lambda _: response(page([item]))), spacing=0)
        client.searches["GU195AE"] = []
        self.assertEqual(client.search({"postcode": "GU19 5AE"}), [])
        self.assertEqual(client.requests, 0)
        self.assertEqual(client.search_query({"postcode": "GU19 5AE"}), [item])
        self.assertEqual(client.requests, 1)

    def test_legacy_only_lookup_preserves_original_evidence_shape(self):
        client = candidate.RegisterClient("synthetic", opener=SyntheticOpener(), spacing=0)
        client.search({"postcode": "GU19 5AE"})
        evidence = candidate.retained_register_evidence(client)
        self.assertEqual(set(evidence), {"postcodeSearches", "requestedCertificates"})
        self.assertEqual(evidence["postcodeSearches"], {"GU195AE": []})
        self.assertEqual(client.query_searches, {})

    def test_different_filters_keep_same_certificate_representations_separate(self):
        def handle(request):
            params = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
            return response(page([certificate(addressLine1="1 TEST ROAD" if "postcode" in params else "2 TEST ROAD")]))

        client = candidate.RegisterClient("synthetic", opener=SyntheticOpener(handle), spacing=0)
        by_postcode = client.search_query({"postcode": "GU19 5AE"})
        by_uprn = client.search_query({"uprn": "12345"})
        self.assertNotEqual(by_postcode, by_uprn)
        self.assertEqual(client.requests, 2)
        self.assertEqual(len(client.query_searches), 2)
        # A cross-query identity conflict belongs to the exact matcher; the
        # client must not overwrite either original source representation.
        evidence = candidate.retained_register_evidence(client)
        self.assertEqual(len(evidence["querySearches"]), 2)

    def test_equivalent_concurrent_queries_are_single_flight(self):
        entered = threading.Event()
        release = threading.Event()

        def handle(request):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Synthetic query not released")
            return response(empty_search())

        opener = SyntheticOpener(handle)
        client = candidate.RegisterClient("synthetic", opener=opener, spacing=0)
        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = [pool.submit(client.search_query, {"address": " Test  Road " if index % 2 else "TEST ROAD"})
                       for index in range(12)]
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(len(opener.calls), 1)
            finally:
                release.set()
            results = [future.result(timeout=5) for future in futures]
        self.assertTrue(all(item is results[0] for item in results))
        self.assertEqual(client.requests, 1)
        self.assertEqual(client._inflight, {})

    def test_prefetch_queries_has_four_workers_and_shared_certificate_budget(self):
        barrier = threading.Barrier(4)
        lock = threading.Lock()
        active = peak = 0

        def handle(request):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait(5)
                return response(empty_search())
            finally:
                with lock:
                    active -= 1

        opener = SyntheticOpener(handle)
        client = candidate.RegisterClient("synthetic", opener=opener, spacing=0, max_requests=4)
        client.prefetch_queries([{"address": str(index) + " TEST ROAD"} for index in range(8)])
        client.prefetch_certificates(["one"])
        self.assertEqual(peak, 4)
        self.assertEqual(client.requests, 4)
        self.assertEqual(len(client.query_searches), 4)
        self.assertEqual(client.certificates, {})
        with self.assertRaisesRegex(RuntimeError, "register_budget_reached"):
            client.certificate("one")

    def test_complete_twenty_pages_retain_normalized_query_and_page_proof(self):
        def handle(request):
            params = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
            current = int(params["current_page"][0])
            return response(page([certificate(str(current))], current, 20, 20))

        client = candidate.RegisterClient("synthetic", opener=SyntheticOpener(handle), spacing=0)
        params = {"address": "  Test Road,  Bagshot "}
        with patch.object(candidate, "utc_now", return_value="2026-09-14T17:00:00Z"):
            records = client.search_query(params)
        evidence = candidate.retained_register_evidence(client)["querySearches"][client.query_key(params)]
        self.assertEqual(evidence["params"], {"address": "TEST ROAD, BAGSHOT"})
        self.assertEqual(evidence["strategyVersion"], client.SEARCH_STRATEGY_VERSION)
        self.assertEqual(evidence["searchedAt"], "2026-09-14T17:00:00Z")
        self.assertTrue(evidence["complete"])
        self.assertEqual(evidence["totalRecords"], 20)
        self.assertEqual(evidence["totalPages"], 20)
        self.assertEqual([item["currentPage"] for item in evidence["pages"]], list(range(1, 21)))
        self.assertEqual(sum(item["recordCount"] for item in evidence["pages"]), 20)
        self.assertEqual(evidence["records"], records)
        self.assertEqual(client.requests, 20)

    def test_changed_total_pages_and_oversized_pages_never_become_complete(self):
        bad_payloads = [page([certificate()], total=1, pages=21),
                        page([certificate()], total=100001, pages=20),
                        page([certificate(str(index)) for index in range(5001)]),
                        {"data": []}]
        for payload in bad_payloads:
            with self.subTest(kind=list(payload)):
                client = candidate.RegisterClient("synthetic", opener=SyntheticOpener(
                    lambda _, payload=payload: response(payload)), spacing=0)
                client.prefetch_queries([{"address": "TEST ROAD"}])
                with self.assertRaises(RuntimeError):
                    client.search_query({"address": "test road"})
                self.assertEqual(client.requests, 1)
                self.assertEqual(client.query_searches, {})
                self.assertEqual(client.query_metadata, {})
                self.assertNotIn("querySearches", candidate.retained_register_evidence(client))
        values = iter([page([certificate("one")], 1, 2, 2), page([certificate("two")], 2, 2, 3)])
        client = candidate.RegisterClient("synthetic", opener=SyntheticOpener(
            lambda _: response(next(values))), spacing=0)
        with self.assertRaisesRegex(RuntimeError, "search_changed_during_pagination"):
            client.search_query({"uprn": "12345"})
        self.assertEqual(client.query_metadata, {})

    def test_budget_interruption_mid_pagination_retains_no_completed_search(self):
        client = candidate.RegisterClient("synthetic", opener=SyntheticOpener(
            lambda _: response(page([certificate()], 1, 2, 2))), spacing=0, max_requests=1)
        client.prefetch_queries([{"uprn": "12345"}])
        with self.assertRaisesRegex(RuntimeError, "register_budget_reached"):
            client.search_query({"uprn": "12345"})
        self.assertEqual(client.requests, 1)
        self.assertEqual(client.query_searches, {})
        self.assertEqual(client.query_metadata, {})
        self.assertNotIn("querySearches", candidate.retained_register_evidence(client))

    def test_failure_is_symbolic_and_does_not_poison_different_filter(self):
        def handle(request):
            if "address=" in request.full_url:
                raise RuntimeError("Bearer synthetic-private-contact")
            return response(empty_search())

        client = candidate.RegisterClient("synthetic", opener=SyntheticOpener(handle), spacing=0)
        client.prefetch_queries([{"address": "TEST ROAD"}] * 10)
        with self.assertRaisesRegex(RuntimeError, "^register_request_failed$"):
            client.search_query({"address": "test road"})
        self.assertEqual(client.requests, 1)
        self.assertEqual(client.search_query({"uprn": "12345"}), [])
        self.assertEqual(client.requests, 2)
        self.assertNotIn("synthetic-private-contact", json.dumps(candidate.retained_register_evidence(client)))
        self.assertEqual(client._inflight, {})

    def test_query_replay_minimizes_private_fields_and_preserves_identity_aliases(self):
        item = certificate(UPRN="000000012345", property_uprn="12345", propertyUprn="12345",
                           local_authority="E07000214", local_authority_code="E07000214",
                           localAuthorityCode="E07000214", locality="TEST LOCALITY",
                           token="synthetic-private-token", assessor={"email": "private@example.test"})
        before = copy.deepcopy(item)
        client = candidate.RegisterClient("synthetic", opener=SyntheticOpener(
            lambda _: response(page([item]))), spacing=0)
        client.search_query({"uprn": "12345"})
        evidence = candidate.retained_register_evidence(client)
        retained = next(iter(evidence["querySearches"].values()))["records"][0]
        for key in ("UPRN", "property_uprn", "propertyUprn", "local_authority", "local_authority_code", "localAuthorityCode", "locality"):
            self.assertEqual(retained[key], item[key])
        self.assertNotIn("synthetic-private-token", json.dumps(evidence))
        self.assertNotIn("private@example.test", json.dumps(evidence))
        self.assertEqual(item, before)
        with self.assertRaisesRegex(ValueError, "Unexpected compound"):
            candidate.minimized_certificate(certificate(uprn={"contact": "private@example.test"}))
        with self.assertRaisesRegex(ValueError, "Unexpected compound"):
            candidate.minimized_certificate(certificate(totalFloorArea={"value": 150, "unit": "m2", "contact": "private@example.test"}))
        measured = certificate(totalFloorArea={"value": 150, "unit": "sq m"})
        self.assertEqual(candidate.minimized_certificate(measured)["totalFloorArea"], {"value": 150, "unit": "sq m"})

    def test_incomplete_or_extra_query_metadata_is_not_retained(self):
        client = candidate.RegisterClient("synthetic", opener=SyntheticOpener(), spacing=0)
        params = {"postcode": "GU19 5AE"}
        client.search_query(params)
        key = client.query_key(params)
        baseline = copy.deepcopy(client.query_metadata[key])
        for change in [{"complete": False}, {"totalRecords": 1},
                       {"params": {"postcode": "KT11 2PB"}}, {"token": "private-token"},
                       {"totalPages": True}, {"pages": []}, {"searchedAt": "not-an-original-time"},
                       {"searchedAt": "2026-02-31T00:00:00Z"},
                       {"pages": [{"currentPage": 2, "totalPages": 0, "totalRecords": 0, "recordCount": 0}]},
                       {"pages": [{"currentPage": 1, "totalPages": 0, "totalRecords": 0, "recordCount": 1}]}]:
            client.query_metadata[key] = {**baseline, **change}
            with self.subTest(change=change), self.assertRaises(ValueError):
                candidate.retained_register_evidence(client)

    def test_malformed_provider_evidence_is_unresolved_before_cache_or_result_sealing(self):
        malformed = certificate(uprn={"contact": "synthetic-private-contact"})
        for kind in ("query", "legacy", "certificate"):
            with self.subTest(kind=kind):
                payload = {"data": malformed} if kind == "certificate" else page([malformed])
                opener = SyntheticOpener(lambda _, payload=payload: response(payload))
                client = candidate.RegisterClient("synthetic", opener=opener, spacing=0)
                if kind == "certificate":
                    client.prefetch_certificates(["one"])
                    retrieve = lambda: client.certificate("one")
                    expected = "invalid_full_certificate_evidence"
                elif kind == "legacy":
                    client.prefetch_searches([{"postcode": "GU19 5AE"}])
                    retrieve = lambda: client.search({"postcode": "GU19 5AE"})
                    expected = "invalid_search_certificate_evidence"
                else:
                    client.prefetch_queries([{"address": "TEST ROAD"}])
                    retrieve = lambda: client.search_query({"address": "TEST ROAD"})
                    expected = "invalid_search_certificate_evidence"
                with self.assertRaisesRegex(RuntimeError, "^" + expected + "$"):
                    retrieve()
                self.assertEqual(client.requests, 1)
                self.assertEqual(client.searches, {})
                self.assertEqual(client.certificates, {})
                self.assertEqual(client.query_searches, {})
                self.assertEqual(client.query_metadata, {})
                retained = candidate.retained_register_evidence(client)
                self.assertEqual(retained, {"postcodeSearches": {}, "requestedCertificates": {}})
                self.assertNotIn("synthetic-private-contact", json.dumps(retained))
                self.assertEqual(client._inflight, {})

    def test_nonfinite_provider_numbers_never_poison_cached_or_sealed_evidence(self):
        for number in ("NaN", "Infinity", "-Infinity", "1e400", "-1e400"):
            for kind in ("query", "legacy", "certificate"):
                with self.subTest(number=number, kind=kind):
                    item = certificate(totalFloorArea={"value": "NONFINITE_PLACEHOLDER", "unit": "m2"})
                    payload = {"data": item} if kind == "certificate" else page([item])
                    raw = json.dumps(payload).replace('"NONFINITE_PLACEHOLDER"', number).encode()
                    client = candidate.RegisterClient("synthetic", opener=SyntheticOpener(
                        lambda _, raw=raw: io.BytesIO(raw)), spacing=0)
                    if kind == "certificate":
                        client.prefetch_certificates(["one"])
                        retrieve = lambda: client.certificate("one")
                    elif kind == "legacy":
                        client.prefetch_searches([{"postcode": "GU19 5AE"}])
                        retrieve = lambda: client.search({"postcode": "GU19 5AE"})
                    else:
                        client.prefetch_queries([{"address": "TEST ROAD"}])
                        retrieve = lambda: client.search_query({"address": "TEST ROAD"})
                    with self.assertRaisesRegex(RuntimeError, "^invalid_register_json$"):
                        retrieve()
                    self.assertEqual(client.requests, 1)
                    self.assertEqual(client.searches, {})
                    self.assertEqual(client.certificates, {})
                    self.assertEqual(client.query_searches, {})
                    self.assertEqual(client.query_metadata, {})
                    retained = candidate.retained_register_evidence(client)
                    json.dumps(retained, allow_nan=False)
                    self.assertEqual(retained, {"postcodeSearches": {}, "requestedCertificates": {}})
                    self.assertEqual(client._inflight, {})

    def test_minimized_retained_scalars_and_quantities_must_be_finite(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            for item in (
                certificate(uprn=value),
                certificate(totalFloorArea=value),
                certificate(totalFloorArea={"value": value, "unit": "m2"}),
                certificate(sap_building_parts=[{"building_part_number": 1,
                    "sap_floor_dimensions": [{"floor": 0, "total_floor_area": value}]}]),
                certificate(sap_building_parts=[{"building_part_number": 1,
                    "sap_room_in_roof": {"floor_area": {"value": value, "unit": "m2"}}}]),
            ):
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Non-finite"):
                    candidate.minimized_certificate(item)


if __name__ == "__main__":
    unittest.main()
