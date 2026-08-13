import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from enrich_epc_data import (  # noqa: E402
    cache_record_is_fresh,
    enrich_transactions,
    fetch_certificate,
    publishable_epc_fields,
    public_epc_record,
    retry_wait_seconds,
    search_candidates,
    stable_transaction_key,
    terminal_cache_accounting,
    terminal_cache_can_reconcile,
)
from insight_data_utils import (  # noqa: E402
    PUBLIC_TRANSACTION_FIELDS,
    RESTRICTED_PUBLIC_TRANSACTION_FIELDS,
    publication_contract_failures,
    read_js,
    write_js,
)
from remove_nearby_planning import (  # noqa: E402
    strip_nearby_planning,
    write_migrated_js,
)


class PublicationContractTests(unittest.TestCase):
    def test_transient_epc_errors_are_retried_on_the_next_checkpoint(self):
        self.assertFalse(cache_record_is_fresh({
            "status": "error",
            "searchedAt": "2099-01-01T00:00:00Z",
        }, 30))

    def test_current_public_feed_contains_only_reviewed_fields(self):
        rows, _summary, _metadata = read_js(ROOT / "outputs" / "surrey-transactions.js")
        self.assertEqual(publication_contract_failures(rows), [])
        self.assertTrue(all(set(row).issubset(PUBLIC_TRANSACTION_FIELDS) for row in rows))

    def test_writer_strips_known_legacy_epc_leakage(self):
        row = {
            "id": "lr-test",
            "address": "1 TEST ROAD, ESHER",
            "postcode": "KT10 0AA",
            "market": "elmbridge-prime",
            "price": 3_000_000,
            "date": "2026-01-01",
            "epcAddress": "1 TEST ROAD, ESHER, KT10 0AA",
            "epcCertificateNumber": "1234-5678-9012-3456-7890",
            "epcMatchScore": 0.99,
            "epcHistory": [{"certificateNumber": "1234-5678-9012-3456-7890"}],
            "openStreetMap": {"source": "OpenStreetMap via Overpass API"},
            "companiesHouse": {"companyNumber": "01234567"},
            "planningHistory": [{"reference": "licensed-private-record"}],
            "environmentAgency": {"floodStatus": "legacy live status"},
            "ofsted": {"source": "DfE / Ofsted school data", "nearestSchools": []},
            "historicEngland": {
                "status": "no_direct_match",
                "entries": [],
                "source": "Historic England NHLE",
                "checkedAt": "2026-07-26T12:00:00Z",
                "sourceUpdatedAt": "2026-07-25T23:00:00Z",
                "sourceSnapshot": "nhle-2026-07-25-0123456789ab",
            },
            "planning": {
                "coverageStatus": "unknown",
                "coverageMode": "no-authoritative-negative-coverage",
                "recentApplications": [],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "feed.js"
            write_js(output, [row], {
                "propertyContext": {
                    "openStreetMap": {"records": 1},
                    "environmentAgency": {"records": 1},
                },
                "dailyIntelligence": {
                    "planning": {
                        "records": 99,
                        "observedRecords": 99,
                        "unknownRecords": 0,
                        "unavailableRecords": 0,
                        "successfulResponses": 99,
                    },
                    "companiesHouse": {"records": 1},
                },
                "weeklyContext": {
                    "historicEngland": {
                        "source": "Planning Data API listed-building dataset",
                        "records": 99,
                    },
                    "schools": {"records": 1, "source": "DfE / Ofsted school data"},
                },
            })
            rows, _summary, metadata = read_js(output)

        self.assertFalse(set(rows[0]) & RESTRICTED_PUBLIC_TRANSACTION_FIELDS)
        self.assertNotIn("openStreetMap", rows[0])
        self.assertNotIn("companiesHouse", rows[0])
        self.assertNotIn("epcHistory", rows[0])
        self.assertNotIn("planningHistory", rows[0])
        self.assertNotIn("environmentAgency", rows[0])
        self.assertNotIn("openStreetMap", metadata["propertyContext"])
        self.assertNotIn("environmentAgency", metadata["propertyContext"])
        self.assertNotIn("companiesHouse", metadata.get("dailyIntelligence", {}))
        self.assertNotIn("planning", metadata.get("dailyIntelligence", {}))
        self.assertEqual(rows[0]["ofsted"]["source"], "DfE Get Information about Schools (GIAS)")
        self.assertEqual(metadata["weeklyContext"]["schools"]["source"], "DfE Get Information about Schools (GIAS)")
        self.assertNotIn("historicEngland", metadata["weeklyContext"])
        self.assertEqual(rows[0]["historicEngland"]["status"], "no_direct_match")

    def test_nearby_planning_migration_is_narrow_and_idempotent(self):
        row = {
            "id": "lr-test",
            "planning": {"source": "Planning Data API"},
            "planningConstraints": {"lookupStatus": "successful", "constraintCount": 1},
            "historicEngland": {"status": "confirmed_listed"},
            "planningHistory": [{"reference": "property-specific-history"}],
        }
        metadata = {
            "dailyIntelligence": {
                "updatedAt": "2026-07-29T10:00:00Z",
                "planning": {"source": "Planning Data API"},
            },
            "weeklyContext": {
                "planningConstraints": {"source": "Planning Data API"},
            },
        }

        cleaned, cleaned_metadata, stats = strip_nearby_planning([row], metadata)
        repeated, repeated_metadata, repeated_stats = strip_nearby_planning(
            cleaned,
            cleaned_metadata,
        )

        self.assertNotIn("planning", cleaned[0])
        self.assertEqual(cleaned[0]["planningConstraints"], row["planningConstraints"])
        self.assertEqual(cleaned[0]["historicEngland"], row["historicEngland"])
        self.assertEqual(cleaned[0]["planningHistory"], row["planningHistory"])
        self.assertNotIn("dailyIntelligence", cleaned_metadata)
        self.assertEqual(cleaned_metadata["weeklyContext"], metadata["weeklyContext"])
        self.assertEqual(
            stats,
            {
                "transactionPlanningFieldsRemoved": 1,
                "planningMetadataRemoved": 1,
            },
        )
        self.assertEqual(repeated, cleaned)
        self.assertEqual(repeated_metadata, cleaned_metadata)
        self.assertEqual(
            repeated_stats,
            {
                "transactionPlanningFieldsRemoved": 0,
                "planningMetadataRemoved": 0,
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "feed.js"
            summary = {"untouched": {"count": 1}}
            write_migrated_js(
                output,
                cleaned,
                summary,
                cleaned_metadata,
            )
            written_rows, written_summary, written_metadata = read_js(output)
        self.assertEqual(written_rows, cleaned)
        self.assertEqual(written_summary, summary)
        self.assertEqual(written_metadata, cleaned_metadata)

    def test_writer_rejects_an_unreviewed_public_field(self):
        row = {
            "id": "lr-test",
            "address": "1 TEST ROAD, ESHER",
            "postcode": "KT10 0AA",
            "market": "elmbridge-prime",
            "price": 3_000_000,
            "date": "2026-01-01",
            "rawSearchResponse": {"unexpected": True},
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "rawSearchResponse"):
                write_js(Path(directory) / "feed.js", [row], {})

    def test_epc_enricher_minimises_legacy_cached_matches(self):
        cached = {
            "epcMatched": True,
            "floorAreaSqm": 200.0,
            "floorAreaSqft": 2153,
            "pricePerSqft": 1393,
            "epcRating": "C",
            "epcRegistrationDate": "2025-01-01",
            "epcSource": "MHCLG EPC Register",
            "epcAddress": "1 TEST ROAD, ESHER, KT10 0AA",
            "epcCertificateNumber": "1234-5678-9012-3456-7890",
            "epcMatchScore": 0.99,
            "diagnostics": {"candidateCount": 3},
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
        cleaned = public_epc_record({
            "id": "lr-test",
            "address": "1 TEST ROAD, ESHER",
            "postcode": "KT10 0AA",
            "market": "elmbridge-prime",
            "price": 3_000_000,
            "date": "2026-01-01",
            **cached,
        })
        self.assertEqual(cleaned["floorAreaSqft"], 2153)
        self.assertNotIn("epcAddress", cleaned)
        self.assertNotIn("epcCertificateNumber", cleaned)
        self.assertNotIn("epcMatchScore", cleaned)

    def test_epc_enricher_preserves_approved_facts_when_no_token_is_available(self):
        row = {
            "id": "lr-test",
            "address": "1 TEST ROAD, ESHER, KT10 0AA",
            "postcode": "KT10 0AA",
            "price": 3_000_000,
            "floorAreaSqft": 3_000,
            "epcRating": "C",
            "epcAddress": "1 TEST ROAD, ESHER, KT10 0AA",
            "epcCertificateNumber": "1234-5678-9012-3456-7890",
            "epcHistory": [{"certificateNumber": "1234-5678-9012-3456-7890"}],
        }
        args = SimpleNamespace(
            limit=0,
            max_run_minutes=0,
            refresh_days=90,
            page_size=10,
            min_score=0.55,
            max_certificate_fetches=1,
            pause=0,
            max_errors=25,
            fail_if_no_matches_after=0,
            progress_every=100,
        )
        rows, _stats, _reasons, _aborted = enrich_transactions([row], {"records": {}}, "", args)
        self.assertEqual(rows[0]["floorAreaSqft"], 3_000)
        self.assertEqual(rows[0]["epcRating"], "C")
        self.assertNotIn("epcAddress", rows[0])
        self.assertNotIn("epcCertificateNumber", rows[0])
        self.assertNotIn("epcHistory", rows[0])

    def test_epc_terminal_accounting_cannot_hide_missing_or_error_rows(self):
        matched_row = {
            "address": "1 TEST ROAD, ESHER",
            "postcode": "KT10 0AA",
            "price": 2_000_000,
            "date": "2026-01-01",
        }
        missing_row = {**matched_row, "address": "2 TEST ROAD, ESHER"}
        error_row = {**matched_row, "address": "3 TEST ROAD, ESHER"}
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
        self.assertFalse(terminal_cache_can_reconcile(accounting, 3))

    def test_complete_epc_cache_can_reconcile_metadata_without_api_access(self):
        accounting = {"requested": 2, "resolved": 2, "pending": 0, "errors": 0}
        self.assertTrue(terminal_cache_can_reconcile(accounting, 2))

    def test_epc_accounting_counts_distinct_rows_that_share_lookup_evidence(self):
        category_a = {
            "id": "a",
            "category": "A",
            "address": "14 RIVER AVENUE, THAMES DITTON",
            "postcode": "KT7 0RS",
            "price": 2_500_000,
            "date": "2023-11-03",
        }
        category_b = {**category_a, "id": "b", "category": "B"}
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
        row = {
            "address": "1 TEST ROAD, ESHER",
            "postcode": "KT10 0AA",
            "price": 2_000_000,
            "date": "2026-01-01",
        }
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

    def test_epc_retry_after_cannot_consume_the_checkpoint_commit_window(self):
        self.assertEqual(retry_wait_seconds("3600", 0), 90)
        self.assertEqual(retry_wait_seconds(None, 1), 50)

if __name__ == "__main__":
    unittest.main()
