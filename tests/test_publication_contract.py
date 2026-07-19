import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from enrich_epc_data import enrich_transactions, publishable_epc_fields  # noqa: E402
from insight_data_utils import (  # noqa: E402
    PUBLIC_TRANSACTION_FIELDS,
    RESTRICTED_PUBLIC_TRANSACTION_FIELDS,
    publication_contract_failures,
    read_js,
    write_js,
)


class PublicationContractTests(unittest.TestCase):
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
            "ofsted": {"source": "DfE / Ofsted school data", "nearestSchools": []},
            "historicEngland": {"name": "Test listed building"},
            "planning": {
                "coverageStatus": "unknown",
                "coverageMode": "no-authoritative-negative-coverage",
                "recentApplications": [],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "feed.js"
            write_js(output, [row], {
                "propertyContext": {"openStreetMap": {"records": 1}},
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
                    "historicEngland": {"records": 99},
                    "schools": {"records": 1, "source": "DfE / Ofsted school data"},
                },
            })
            rows, _summary, metadata = read_js(output)

        self.assertFalse(set(rows[0]) & RESTRICTED_PUBLIC_TRANSACTION_FIELDS)
        self.assertNotIn("openStreetMap", rows[0])
        self.assertNotIn("companiesHouse", rows[0])
        self.assertNotIn("epcHistory", rows[0])
        self.assertNotIn("planningHistory", rows[0])
        self.assertNotIn("openStreetMap", metadata["propertyContext"])
        self.assertNotIn("companiesHouse", metadata["dailyIntelligence"])
        self.assertEqual(rows[0]["ofsted"]["source"], "DfE Get Information about Schools (GIAS)")
        self.assertEqual(metadata["weeklyContext"]["schools"]["source"], "DfE Get Information about Schools (GIAS)")
        self.assertEqual(metadata["weeklyContext"]["historicEngland"]["records"], 1)
        self.assertEqual(metadata["dailyIntelligence"]["planning"]["records"], 1)
        self.assertEqual(metadata["dailyIntelligence"]["planning"]["observedRecords"], 0)
        self.assertEqual(metadata["dailyIntelligence"]["planning"]["unknownRecords"], 1)
        self.assertEqual(metadata["dailyIntelligence"]["planning"]["unavailableRecords"], 0)
        self.assertEqual(metadata["dailyIntelligence"]["planning"]["successfulResponses"], 1)

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
        public = publishable_epc_fields({
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
        })

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

    def test_epc_cache_is_private_in_workflows(self):
        self.assertIn("work/epc-cache.json", (ROOT / ".gitignore").read_text(encoding="utf-8"))
        for name in ("monthly-property-refresh.yml", "monthly-land-registry-sweep.yml"):
            workflow = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
            self.assertIn("actions/cache/restore@v4", workflow)
            self.assertIn("actions/cache/save@v4", workflow)
            self.assertIn("work/epc-cache.json.enc", workflow)
            self.assertIn("openssl enc -aes-256-cbc -salt -pbkdf2", workflow)
            self.assertNotIn("git add work/epc-cache.json", workflow)

    def test_every_daily_intelligence_workflow_disables_companies_house(self):
        workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
        invocations = 0
        for path in workflows:
            workflow = path.read_text(encoding="utf-8")
            count = workflow.count("enrich_daily_intelligence.py")
            invocations += count
            self.assertEqual(
                count,
                workflow.count("--disable-companies-house"),
                f"Every daily-intelligence invocation in {path.name} must keep Companies House local-only",
            )
        self.assertGreater(invocations, 0)

    def test_operational_daily_cache_is_off_repository(self):
        self.assertIn("work/daily-intelligence-cache.json", (ROOT / ".gitignore").read_text(encoding="utf-8"))
        for path in (ROOT / ".github" / "workflows").glob("*.yml"):
            workflow = path.read_text(encoding="utf-8")
            self.assertNotIn("git add work/daily-intelligence-cache.json", workflow)
            self.assertNotIn("work/daily-intelligence-cache.json work/os-uprn-cache.json", workflow)


if __name__ == "__main__":
    unittest.main()
