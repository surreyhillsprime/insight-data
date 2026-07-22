import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from enrich_epc_data import (  # noqa: E402
    cache_record_is_fresh,
    fetch_certificate,
    publishable_epc_fields,
    public_epc_record,
    search_candidates,
    stable_transaction_key,
    terminal_cache_accounting,
)
from insight_data_utils import (  # noqa: E402
    RESTRICTED_PUBLIC_TRANSACTION_FIELDS,
    publication_contract_failures,
    read_js,
    write_js,
)


def transaction(**overrides):
    row = {
        "id": "lr-test",
        "address": "1 TEST ROAD, ESHER",
        "postcode": "KT10 0AA",
        "market": "elmbridge-prime",
        "district": "Elmbridge",
        "price": 2_000_000,
        "date": "2026-01-01",
    }
    row.update(overrides)
    return row


class PublicationContractTests(unittest.TestCase):
    def test_transient_epc_errors_are_retried_on_the_next_checkpoint(self):
        self.assertFalse(cache_record_is_fresh({
            "status": "error",
            "searchedAt": "2099-01-01T00:00:00Z",
        }, 30))

    def test_epc_terminal_accounting_cannot_hide_missing_or_error_rows(self):
        matched_row = transaction(id="matched")
        missing_row = transaction(id="missing", address="2 TEST ROAD, ESHER")
        error_row = transaction(id="error", address="3 TEST ROAD, ESHER")
        cache = {"records": {
            stable_transaction_key(matched_row): {
                "status": "matched",
                "epc": {"floorAreaSqft": 2000},
            },
            stable_transaction_key(error_row): {
                "status": "error",
                "searchedAt": "2099-01-01T00:00:00Z",
            },
        }}
        accounting = terminal_cache_accounting(
            [matched_row, missing_row, error_row], cache, 30
        )
        self.assertEqual(accounting["requested"], 3)
        self.assertEqual(accounting["resolved"], 1)
        self.assertEqual(accounting["pending"], 2)
        self.assertEqual(accounting["errors"], 1)

    def test_epc_accounting_counts_distinct_rows_that_share_lookup_evidence(self):
        category_a = transaction(id="a", category="A")
        category_b = transaction(id="b", category="B")
        cache = {"records": {
            stable_transaction_key(category_a): {
                "status": "matched",
                "epc": {"floorAreaSqft": 2000},
            }
        }}
        accounting = terminal_cache_accounting([category_a, category_b], cache, 30)
        self.assertEqual(accounting["requested"], 2)
        self.assertEqual(accounting["resolved"], 2)
        self.assertEqual(accounting["pending"], 0)

    def test_epc_run_deduplicates_identical_postcode_and_certificate_requests(self):
        candidate_cache = {}
        certificate_cache = {}
        row = transaction()
        with patch("enrich_epc_data.request_json") as request:
            request.side_effect = [
                {"data": [{"certificateNumber": "certificate-1"}]},
                {"data": {"certificateNumber": "certificate-1"}},
            ]
            first_candidates = search_candidates(row, "token", 5000, candidate_cache)
            second_candidates = search_candidates(row, "token", 5000, candidate_cache)
            first_certificate = fetch_certificate(
                "certificate-1", "token", certificate_cache
            )
            second_certificate = fetch_certificate(
                "certificate-1", "token", certificate_cache
            )
        self.assertIs(first_candidates, second_candidates)
        self.assertIs(first_certificate, second_certificate)
        self.assertEqual(request.call_count, 2)

    def test_writer_strips_known_private_or_legacy_fields(self):
        row = transaction(
            epcMatched=True,
            floorAreaSqft=3000,
            epcAddress="1 TEST ROAD, ESHER, KT10 0AA",
            epcCertificateNumber="1234-5678-9012-3456-7890",
            epcMatchScore=0.99,
            epcHistory=[{"certificateNumber": "private"}],
            openStreetMap={"source": "OpenStreetMap"},
            companiesHouse={"companyNumber": "01234567"},
            planningHistory=[{"reference": "licensed-private-record"}],
            ofsted={"source": "DfE / Ofsted school data", "nearestSchools": []},
            planning={
                "coverageStatus": "unknown",
                "coverageMode": "no-authoritative-negative-coverage",
                "recentApplications": [],
            },
        )
        metadata = {
            "propertyContext": {"openStreetMap": {"records": 1}},
            "dailyIntelligence": {
                "planning": {"records": 99, "successfulResponses": 99},
                "companiesHouse": {"records": 1},
            },
            "weeklyContext": {
                "schools": {"records": 1, "source": "DfE / Ofsted school data"},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "feed.js"
            write_js(output, [row], metadata)
            rows, _summary, written_metadata = read_js(output)

        self.assertFalse(set(rows[0]) & RESTRICTED_PUBLIC_TRANSACTION_FIELDS)
        self.assertEqual(publication_contract_failures(rows), [])
        self.assertNotIn("openStreetMap", written_metadata["propertyContext"])
        self.assertNotIn("companiesHouse", written_metadata["dailyIntelligence"])
        self.assertEqual(
            rows[0]["ofsted"]["source"],
            "DfE Get Information about Schools (GIAS)",
        )
        self.assertEqual(written_metadata["dailyIntelligence"]["planning"]["records"], 1)
        self.assertEqual(
            written_metadata["dailyIntelligence"]["planning"]["successfulResponses"],
            1,
        )

    def test_writer_rejects_an_unreviewed_public_field(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "rawSearchResponse"):
                write_js(
                    Path(directory) / "feed.js",
                    [transaction(rawSearchResponse={"unexpected": True})],
                    {},
                )

    def test_epc_minimisation_preserves_only_approved_derived_facts(self):
        cached = {
            "epcMatched": True,
            "floorAreaSqm": 200.0,
            "floorAreaSqft": 2153,
            "pricePerSqft": 929,
            "epcRating": "C",
            "epcRegistrationDate": "2025-01-01",
            "epcSource": "MHCLG EPC Register",
            "epcAddress": "1 TEST ROAD, ESHER, KT10 0AA",
            "epcCertificateNumber": "1234-5678-9012-3456-7890",
            "epcMatchScore": 0.99,
        }
        public = publishable_epc_fields(cached)
        self.assertEqual(
            set(public),
            {
                "epcMatched",
                "floorAreaSqm",
                "floorAreaSqft",
                "pricePerSqft",
                "epcRating",
                "epcRegistrationDate",
                "epcSource",
            },
        )
        cleaned = public_epc_record({**transaction(), **cached})
        self.assertEqual(cleaned["floorAreaSqft"], 2153)
        self.assertNotIn("epcAddress", cleaned)
        self.assertNotIn("epcCertificateNumber", cleaned)
        self.assertNotIn("epcMatchScore", cleaned)


if __name__ == "__main__":
    unittest.main()
