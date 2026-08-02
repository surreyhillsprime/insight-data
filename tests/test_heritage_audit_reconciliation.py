import copy
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_heritage_address_ledger import sha256_lines  # noqa: E402
from reconcile_heritage_address_audit import (  # noqa: E402
    reconcile_payload,
    validate_baseline,
)


class HeritageAuditReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.first = "property:FIRST HOUSE TEST ROAD ESHER KT10 0AA|KT100AA"
        self.second = "property:SECOND HOUSE TEST ROAD ESHER KT10 0AB|KT100AB"
        self.third = "property:THIRD HOUSE TEST ROAD ESHER KT10 0AC|KT100AC"
        self.ledger = {
            self.first: {
                "propertyRecordId": self.first,
                "address": "FIRST HOUSE, TEST ROAD, ESHER, KT10 0AA",
                "postcode": "KT10 0AA",
                "status": "confirmed_listed",
                "listEntryNumbers": ["1234567"],
                "reviewedBy": "Reviewer",
                "reviewedAt": "2026-07-28",
                "evidenceUrl": (
                    "https://historicengland.org.uk/listing/the-list/list-entry/1234567"
                ),
                "note": "Exact official identity.",
            },
            self.second: {
                "propertyRecordId": self.second,
                "address": "SECOND HOUSE, TEST ROAD, ESHER, KT10 0AB",
                "postcode": "KT10 0AB",
                "status": "no_direct_match",
                "listEntryNumbers": [],
                "reviewedBy": "Reviewer",
                "reviewedAt": "2026-07-28",
                "note": "Screened; not legal proof.",
            },
        }
        self.audit = {
            "auditVersion": 1,
            "reviewedAt": "2026-07-28",
            "reviewedBy": "Reviewer",
            "canonicalPropertyCount": 2,
            "canonicalPropertyDigest": sha256_lines(self.ledger),
            "confirmedPropertyCount": 1,
            "confirmedUniqueListEntryCount": 1,
            "confirmedGradeCounts": {"I": 0, "II": 1, "II*": 0},
            "confirmedPairDigest": sha256_lines([f"{self.first}|1234567"]),
            "documentedNoDirectPropertyCount": 0,
            "genericNoDirectPropertyCount": 1,
            "unknownPropertyCount": 0,
            "confirmedMappings": [{
                "propertyRecordId": self.first,
                "address": "FIRST HOUSE, TEST ROAD, ESHER, KT10 0AA",
                "postcode": "KT10 0AA",
                "listEntryNumbers": ["1234567"],
                "grade": "II",
                "evidenceUrl": (
                    "https://historicengland.org.uk/listing/the-list/list-entry/1234567"
                ),
                "note": "Exact official identity.",
            }],
            "noDirectMappings": [],
            "unknownMappings": [],
        }

    def test_new_identities_fail_closed_and_retired_evidence_is_preserved(self):
        properties = {
            self.second: {"item": {
                "address": "SECOND HOUSE, TEST ROAD, ESHER, KT10 0AB",
                "postcode": "KT10 0AB",
            }},
            self.third: {"item": {
                "address": "THIRD HOUSE, TEST ROAD, ESHER, KT10 0AC",
                "postcode": "KT10 0AC",
            }},
        }
        result = reconcile_payload(
            self.audit,
            self.ledger,
            properties,
            reconciled_at="2026-08-02",
            max_change_fraction=1.0,
        )
        self.assertEqual(result["canonicalPropertyCount"], 2)
        self.assertEqual(result["confirmedPropertyCount"], 0)
        self.assertEqual(result["genericNoDirectPropertyCount"], 1)
        self.assertEqual(result["unknownPropertyCount"], 1)
        self.assertEqual(
            result["unknownMappings"][0]["propertyRecordId"], self.third
        )
        self.assertEqual(
            result["unknownMappings"][0]["reviewedBy"],
            "Automated canonical-universe reconciliation (fail-closed)",
        )
        retired = result["retiredMappings"][0]
        self.assertEqual(retired["propertyRecordId"], self.first)
        self.assertEqual(retired["status"], "confirmed_listed")
        self.assertEqual(retired["listEntryNumbers"], ["1234567"])
        self.assertEqual(retired["retiredAt"], "2026-08-02")

    def test_tampered_baseline_is_rejected_before_reconciliation(self):
        audit = copy.deepcopy(self.audit)
        audit["canonicalPropertyDigest"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "digest"):
            validate_baseline(audit, self.ledger)


if __name__ == "__main__":
    unittest.main()
