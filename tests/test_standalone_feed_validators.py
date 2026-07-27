import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import validate_planning_feed  # noqa: E402
import validate_sales_history_feed  # noqa: E402
import build_commercial_planning_feed  # noqa: E402


def write_assignments(path, assignments):
    path.write_text(
        "\n".join(
            f"window.{name} = {json.dumps(value, separators=(',', ':'))};"
            for name, value in assignments.items()
        )
        + "\n",
        encoding="utf-8",
    )


class StandaloneFeedValidatorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.base = self.root / "base.js"
        self.property_id = "property:1 TEST ROAD ESHER KT10 0AA|KT100AA"
        self.transaction_id = "lr-test"
        write_assignments(
            self.base,
            {
                "SURREY_LAND_REG_TRANSACTIONS": [
                    {
                        "id": self.transaction_id,
                        "propertyRecordId": self.property_id,
                    }
                ]
            },
        )

    def tearDown(self):
        self.directory.cleanup()

    def run_validator(self, module, *args):
        with patch.object(sys, "argv", [module.__file__, *map(str, args)]):
            with redirect_stdout(StringIO()):
                module.main()

    def planning_assignments(self, property_id=None, transaction_id=None):
        property_id = property_id or self.property_id
        transaction_id = transaction_id or self.transaction_id
        record = {
            "propertyRecordId": property_id,
            "source": "Test licensed source",
            "updatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "authority": "Elmbridge Borough Council",
            "totalApplications": 1,
            "latestApplication": {"reference": "2026/0001"},
            "applications": [{"reference": "2026/0001"}],
            "matchMethod": "postcode-and-address",
            "matchConfidence": 1.0,
            "coverageMode": "full-available-history",
            "coverageStatus": "complete",
        }
        histories = {property_id: record, transaction_id: record}
        _, _, base_fingerprint = validate_planning_feed.base_feed_identity([
            {"id": self.transaction_id, "propertyRecordId": self.property_id}
        ])
        metadata = {
            "schemaVersion": 1,
            "deploymentMode": "commercial",
            "publicationStatus": "complete",
            "source": "Test licensed source",
            "sourceLicenceUrl": "https://example.test/licence",
            "redistributionRights": "licensed-for-product-redistribution",
            "updatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "propertiesRequested": 1,
            "propertiesChecked": 1,
            "propertiesWithHistory": 1,
            "propertiesCheckedNone": 0,
            "propertiesUnavailable": 0,
            "applicationsFound": 1,
            "lookupKeys": 2,
            "canonicalPropertyRecords": 1,
            "transactionAliases": 1,
            "coverageMode": "full-available-history",
            "coverageStatus": "complete",
            "authorities": ["Elmbridge Borough Council"],
            "authorityCoverage": [{
                "authority": "Elmbridge Borough Council",
                "propertiesChecked": 1,
                "propertiesWithHistory": 1,
                "propertiesCheckedNone": 0,
                "applicationsFound": 1,
            }],
            "baseFeedFingerprint": base_fingerprint,
            "sourceFingerprint": "a" * 64,
            "historyFingerprint": validate_planning_feed.sha256_json(histories),
        }
        return {
            "SURREY_PLANNING_HISTORY": histories,
            "SURREY_PLANNING_HISTORY_META": metadata,
        }

    def test_planning_rejects_property_key_outside_base_feed(self):
        planning = self.root / "planning.js"
        write_assignments(
            planning,
            self.planning_assignments(property_id="property:ARBITRARY|KT100AA"),
        )
        with self.assertRaisesRegex(ValueError, "outside the base feed"):
            self.run_validator(validate_planning_feed, planning, "--base-feed", self.base)

    def test_planning_accepts_canonical_property_and_transaction_aliases(self):
        planning = self.root / "planning.js"
        write_assignments(planning, self.planning_assignments())
        self.run_validator(validate_planning_feed, planning, "--base-feed", self.base)

    def test_planning_accepts_only_the_explicit_blocked_artifact_when_allowed(self):
        blocked = ROOT / "outputs" / "planning-history.js"
        with self.assertRaisesRegex(ValueError, "not complete"):
            self.run_validator(validate_planning_feed, blocked)
        self.run_validator(validate_planning_feed, blocked, "--allow-blocked")

    def test_planning_blocked_mode_never_accepts_populated_unlicensed_history(self):
        planning = self.root / "planning-blocked-with-data.js"
        assignments = self.planning_assignments()
        assignments["SURREY_PLANNING_HISTORY_META"].update({
            "publicationStatus": "blocked-missing-licensed-source",
            "source": "Commercial planning feed not enabled",
            "sourceLicenceUrl": "",
            "redistributionRights": "not-authorised-for-publication",
            "updatedAt": "",
            "coverageMode": "unavailable",
            "coverageStatus": "unavailable",
        })
        write_assignments(planning, assignments)
        with self.assertRaisesRegex(ValueError, "must not contain history"):
            self.run_validator(
                validate_planning_feed,
                planning,
                "--allow-blocked",
            )

    def test_planning_rejects_application_count_regression(self):
        planning = self.root / "planning-regression.js"
        write_assignments(planning, self.planning_assignments())
        with self.assertRaisesRegex(ValueError, "application floor"):
            self.run_validator(
                validate_planning_feed,
                planning,
                "--base-feed",
                self.base,
                "--minimum-applications",
                "2",
            )

    def test_planning_rejects_alias_that_differs_from_canonical_record(self):
        planning = self.root / "planning-alias.js"
        assignments = self.planning_assignments()
        assignments["SURREY_PLANNING_HISTORY"][self.transaction_id] = {
            **assignments["SURREY_PLANNING_HISTORY"][self.transaction_id],
            "applications": [],
            "totalApplications": 0,
        }
        assignments["SURREY_PLANNING_HISTORY_META"]["historyFingerprint"] = (
            validate_planning_feed.sha256_json(
                assignments["SURREY_PLANNING_HISTORY"]
            )
        )
        write_assignments(planning, assignments)
        with self.assertRaisesRegex(ValueError, "does not equal its canonical"):
            self.run_validator(validate_planning_feed, planning, "--base-feed", self.base)

    def test_planning_builder_emits_every_property_and_transaction_alias(self):
        first = {
            "id": "txn-first",
            "propertyRecordId": "property:1 TEST ROAD|KT100AA",
            "address": "1 TEST ROAD",
            "postcode": "KT10 0AA",
            "district": "Elmbridge",
        }
        second = {
            "id": "txn-second",
            "propertyRecordId": "property:2 TEST ROAD|KT100AA",
            "address": "2 TEST ROAD",
            "postcode": "KT10 0AA",
            "district": "Elmbridge",
        }
        planning = {
            "authority": "Elmbridge Borough Council",
            "totalApplications": 1,
            "latestApplication": {"reference": "2026/0001"},
            "applications": [{"reference": "2026/0001"}],
        }
        histories, canonical, coverage = (
            build_commercial_planning_feed.build_histories(
                [first, second],
                [{**first, "planningHistory": planning}, dict(second)],
                checked_at="2026-07-26T12:00:00Z",
            )
        )
        self.assertEqual(len(canonical), 2)
        self.assertEqual(set(histories), {
            first["propertyRecordId"],
            second["propertyRecordId"],
            first["id"],
            second["id"],
        })
        self.assertEqual(sum(row["applicationsFound"] for row in coverage), 1)

    def test_planning_workflow_is_scheduled_but_rights_gated(self):
        workflow = (
            ROOT / ".github" / "workflows" / "planning-history-feed.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("cron: '45 6 * * 1'", workflow)
        self.assertIn("vars.PLANNING_COMMERCIAL_ENABLED == 'true'", workflow)
        self.assertIn("secrets.PLANNING_DATA_SOURCE", workflow)
        self.assertIn("vars.PLANNING_SOURCE_NAME", workflow)
        self.assertIn("vars.PLANNING_SOURCE_LICENCE_URL", workflow)
        self.assertEqual(workflow.count("--minimum-applications 21180"), 2)
        self.assertEqual(workflow.count("--base-feed outputs/surrey-transactions.js"), 2)
        completeness = (
            ROOT / ".github" / "workflows" / "data-completeness.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("vars.PLANNING_COMMERCIAL_ENABLED == 'true'", completeness)
        self.assertIn("vars.PLANNING_COMMERCIAL_ENABLED != 'true'", completeness)
        self.assertIn("--allow-blocked", completeness)

    def test_sales_rejects_stale_canonical_property_set(self):
        sales = self.root / "sales.js"
        write_assignments(
            sales,
            {
                "SURREY_SALES_HISTORY": {},
                "SURREY_SALES_HISTORY_META": {
                    "schemaVersion": 1,
                    "deploymentMode": "local",
                    "propertiesChecked": 1,
                },
            },
        )
        with self.assertRaisesRegex(ValueError, "coverage is stale"):
            self.run_validator(
                validate_sales_history_feed,
                sales,
                "--allow-local",
                "--base-feed",
                self.base,
            )

    def test_sales_accepts_explicitly_accounted_unavailable_property(self):
        sales = self.root / "sales-unavailable.js"
        write_assignments(
            sales,
            {
                "SURREY_SALES_HISTORY": {
                    self.property_id: {
                        "propertyRecordId": self.property_id,
                        "coverageStatus": "unavailable",
                        "transactions": [],
                    }
                },
                "SURREY_SALES_HISTORY_META": {
                    "schemaVersion": 1,
                    "deploymentMode": "local",
                    "propertiesRequested": 1,
                    "propertiesChecked": 0,
                    "propertiesUnavailable": 1,
                    "propertiesNotChecked": 0,
                },
            },
        )
        self.run_validator(
            validate_sales_history_feed,
            sales,
            "--allow-local",
            "--base-feed",
            self.base,
        )


if __name__ == "__main__":
    unittest.main()
