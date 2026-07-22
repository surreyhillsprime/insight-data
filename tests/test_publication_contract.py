import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from enrich_epc_data import cache_record_is_fresh, publishable_epc_fields, public_epc_record  # noqa: E402
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
