"""Bounded concurrent retrieval uses synthetic responses, never the register."""

import io
import json
import sys
import threading
import unittest
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import backfill_epc_candidate as candidate


def response(payload):
    return io.BytesIO(json.dumps(payload).encode())


def empty_search():
    return {"data": [], "pagination": {
        "totalRecords": 0, "currentPage": 1, "totalPages": 0}}


class SyntheticOpener:
    def __init__(self, handler=None):
        self.handler = handler
        self.calls = []
        self.lock = threading.Lock()

    def open(self, request, timeout):
        with self.lock:
            self.calls.append(request.full_url)
        if self.handler:
            return self.handler(request)
        parts = urllib.parse.urlsplit(request.full_url)
        if parts.path == "/api/domestic/search":
            return response(empty_search())
        number = urllib.parse.parse_qs(parts.query)["certificate_number"][0]
        return response({"data": {"certificateNumber": number}})


class RegisterConcurrencyTests(unittest.TestCase):
    def test_simultaneous_duplicate_searches_and_certificates_are_single_flight(self):
        for kind in ("search", "certificate"):
            with self.subTest(kind=kind):
                entered = threading.Event()
                release = threading.Event()

                def handler(request):
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError("Synthetic request was not released")
                    return response(empty_search() if kind == "search" else {
                        "data": {"certificateNumber": "same"}})

                opener = SyntheticOpener(handler)
                client = candidate.RegisterClient("synthetic", opener=opener, spacing=0)
                retrieve = (lambda _: client.search({"postcode": "gu19 5ae"})) if kind == "search" else (
                    lambda _: client.certificate("same"))
                with ThreadPoolExecutor(max_workers=12) as pool:
                    futures = [pool.submit(retrieve, i) for i in range(12)]
                    try:
                        self.assertTrue(entered.wait(5))
                        self.assertEqual(len(opener.calls), 1)
                    finally:
                        release.set()
                    results = [future.result(timeout=5) for future in futures]
                self.assertEqual(len(opener.calls), 1)
                self.assertEqual(client.requests, 1)
                self.assertTrue(all(value is results[0] for value in results))
                self.assertEqual(client._inflight, {})

    def test_prefetch_deduplicates_postcodes_and_identifiers_and_waits_for_completion(self):
        opener = SyntheticOpener()
        client = candidate.RegisterClient("synthetic", opener=opener, spacing=0)
        client.prefetch_searches([
            {"postcode": "gu19 5ae"}, {"postcode": "GU195AE"},
            {"postcode": "KT11 2PB"}, {"postcode": ""}])
        client.prefetch_certificates(["one", "two", "one"])
        self.assertEqual(client.requests, 4)
        self.assertEqual(len(client.searches), 2)
        self.assertEqual(set(client.certificates), {"one", "two"})
        self.assertEqual(client.search({"postcode": "GU19 5AE"}), [])
        self.assertEqual(client.certificate("one")["certificateNumber"], "one")
        self.assertEqual(client.requests, 4)
        with self.assertRaisesRegex(RuntimeError, "property_postcode_unresolved"):
            client.search({"postcode": ""})

    def test_parallel_workers_share_a_hard_request_budget(self):
        opener = SyntheticOpener()
        client = candidate.RegisterClient("synthetic", opener=opener, spacing=0, max_requests=7)
        numbers = ["certificate-" + str(i) for i in range(80)]
        client.prefetch_certificates(numbers)
        self.assertEqual(client.requests, 7)
        self.assertEqual(len(opener.calls), 7)
        self.assertEqual(len(client.certificates), 7)
        self.assertEqual(len(client._failures), 73)
        client.prefetch_certificates(numbers)
        self.assertEqual(len(opener.calls), 7)
        self.assertEqual(client._inflight, {})

    def test_prefetch_has_at_most_four_concurrent_requests(self):
        barrier = threading.Barrier(4)
        lock = threading.Lock()
        active = 0
        peak = 0

        def handler(request):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait(timeout=5)
                number = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)["certificate_number"][0]
                return response({"data": {"certificateNumber": number}})
            finally:
                with lock:
                    active -= 1

        client = candidate.RegisterClient("synthetic", opener=SyntheticOpener(handler), spacing=0)
        client.prefetch_certificates([str(i) for i in range(12)])
        self.assertEqual(peak, 4)
        self.assertEqual(len(client.certificates), 12)

    def test_default_openers_belong_to_individual_worker_threads(self):
        barrier = threading.Barrier(4)
        built = []
        lock = threading.Lock()

        def factory(*handlers):
            self.assertTrue(any(isinstance(handler, candidate.NoRedirects) for handler in handlers))
            owner = threading.get_ident()

            def handle(request):
                self.assertEqual(threading.get_ident(), owner)
                barrier.wait(timeout=5)
                number = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)["certificate_number"][0]
                return response({"data": {"certificateNumber": number}})

            with lock:
                built.append(owner)
            return SyntheticOpener(handle)

        with patch.object(candidate.urllib.request, "build_opener", side_effect=factory):
            client = candidate.RegisterClient("synthetic", spacing=0)
            client.prefetch_certificates([str(i) for i in range(8)])
        self.assertEqual(len(built), 4)
        self.assertEqual(len(set(built)), 4)
        self.assertEqual(len(client.certificates), 8)

    def test_pacing_and_deadline_apply_to_all_workers(self):
        now = [10.0]
        admitted = []

        def sleep(seconds):
            now[0] += seconds

        def handler(request):
            # Admission occurs before the network call; record its reservation
            # independently because a thread can be descheduled before open().
            return response(empty_search())

        opener = SyntheticOpener(handler)
        with patch.object(candidate.time, "monotonic", side_effect=lambda: now[0]):
            client = candidate.RegisterClient("synthetic", opener=opener, sleep=sleep,
                                              max_seconds=1, spacing=0.3)
            admit = client._admit_request

            def record_admission():
                admit()
                with client._lock:
                    admitted.append(client._next_request_at - client.spacing)

            # Direct sequential admissions make timestamps exact under this
            # deterministic clock; the parallel budget test covers contention.
            client._admit_request = record_admission
            for index in range(4):
                client.search({"postcode": "KT11 " + str(index) + "AA"})
            with self.assertRaisesRegex(RuntimeError, "register_budget_reached"):
                client.search({"postcode": "KT11 9AA"})
        self.assertEqual(client.requests, 4)
        self.assertEqual(len(opener.calls), 4)
        for actual, expected in zip(admitted, [10, 10.3, 10.6, 10.9]):
            self.assertAlmostEqual(actual, expected)

    def test_prefetch_failures_are_symbolic_cached_and_do_not_repeat_requests(self):
        def handler(request):
            raise RuntimeError("Bearer synthetic-private-body")

        opener = SyntheticOpener(handler)
        client = candidate.RegisterClient("synthetic", opener=opener, spacing=0)
        client.prefetch_certificates(["failed"] * 12)
        for _ in range(3):
            with self.assertRaisesRegex(RuntimeError, "^register_request_failed$"):
                client.certificate("failed")
        self.assertEqual(client.requests, 1)
        self.assertEqual(len(opener.calls), 1)
        self.assertNotIn("synthetic-private-body", str(client._failures))
        self.assertEqual(client._inflight, {})

    def test_invalid_search_failure_is_cached_after_prefetch(self):
        opener = SyntheticOpener(lambda request: response({"data": []}))
        client = candidate.RegisterClient("synthetic", opener=opener, spacing=0)
        client.prefetch_searches([{"postcode": "GU19 5AE"}])
        with self.assertRaisesRegex(RuntimeError, "incomplete_search_pagination"):
            client.search({"postcode": "GU195AE"})
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(client.searches, {})

    def test_retries_share_the_budget_and_authentication_stops_new_admissions(self):
        for code, expected, max_requests in ((503, 2, 2), (401, 1, 50)):
            with self.subTest(code=code):
                def handler(request):
                    raise urllib.error.HTTPError(request.full_url, code, "private", {}, io.BytesIO(b"private"))

                opener = SyntheticOpener(handler)
                client = candidate.RegisterClient("synthetic", opener=opener, sleep=lambda _: None,
                                                  spacing=0, max_requests=max_requests)
                client.prefetch_certificates(["one"])
                with self.assertRaisesRegex(RuntimeError, "budget_reached|authentication_failed"):
                    client.certificate("two")
                self.assertEqual(client.requests, expected)
                self.assertEqual(len(opener.calls), expected)

    def test_unexpected_failure_releases_all_duplicate_waiters(self):
        entered = threading.Event()
        release = threading.Event()

        def handler(request):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Synthetic request was not released")
            raise LookupError("private unexpected failure")

        opener = SyntheticOpener(handler)
        client = candidate.RegisterClient("synthetic", opener=opener, spacing=0)
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(client.certificate, "same") for _ in range(8)]
            try:
                self.assertTrue(entered.wait(5))
            finally:
                release.set()
            failures = [future.exception(timeout=5) for future in futures]
        self.assertEqual(sum(isinstance(error, LookupError) for error in failures), 1)
        self.assertEqual(sum(isinstance(error, RuntimeError) for error in failures), 7)
        self.assertEqual(client.requests, 1)
        self.assertEqual(client._inflight, {})
        self.assertEqual(client._failures, {("certificate", "same"): "register_unexpected_failure"})

    def test_authentication_failure_cancels_another_workers_paced_admission(self):
        first_opened = threading.Event()
        pacing_started = threading.Event()
        release_pacing = threading.Event()

        def sleep(seconds):
            pacing_started.set()
            if not release_pacing.wait(5):
                raise AssertionError("Synthetic pacing was not released")

        def handler(request):
            first_opened.set()
            if not pacing_started.wait(5):
                raise AssertionError("Second request did not reach pacing")
            raise urllib.error.HTTPError(request.full_url, 401, "private", {}, io.BytesIO(b"private"))

        opener = SyntheticOpener(handler)
        client = candidate.RegisterClient("synthetic", opener=opener, sleep=sleep, spacing=2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(client.certificate, "first")
            try:
                self.assertTrue(first_opened.wait(5))
                second = pool.submit(client.certificate, "second")
                self.assertTrue(pacing_started.wait(5))
                with self.assertRaisesRegex(RuntimeError, "register_http_401"):
                    first.result(timeout=5)
                self.assertTrue(client.auth_failed)
            finally:
                release_pacing.set()
            with self.assertRaisesRegex(RuntimeError, "authentication_failed"):
                second.result(timeout=5)
        self.assertEqual(client.requests, 1)
        self.assertEqual(len(opener.calls), 1)


if __name__ == "__main__":
    unittest.main()
