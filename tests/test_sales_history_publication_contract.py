import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from collect_title_history import cache_coverage  # noqa: E402
from validate_sales_history_feed import (  # noqa: E402
    ADDRESS_DATA_USE,
    ATTRIBUTION,
    REDISTRIBUTION_RIGHTS,
    SOURCE_LICENCE_URL,
    SOURCE_NAME,
    base_feed_identity,
    sha256_json,
    validate,
)


def timestamp(days_ago=0):
    value = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_assignments(path, assignments):
    path.write_text(
        "\n".join(
            f"window.{name} = {json.dumps(value, separators=(',', ':'))};"
            for name, value in assignments.items()
        )
        + "\n",
        encoding="utf-8",
    )


class SalesHistoryPublicationContractTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.base_path = self.root / "base.js"
        self.sales_path = self.root / "sales.js"
        self.property_one = "property:1 TEST ROAD|KT100AA"
        self.property_two = "property:2 TEST ROAD|KT100AA"
        self.base_rows = [
            {"id": "txn-one", "propertyRecordId": self.property_one},
            {"id": "txn-two", "propertyRecordId": self.property_two},
        ]
        self.checked_at = timestamp()
        write_assignments(
            self.base_path,
            {"SURREY_LAND_REG_TRANSACTIONS": self.base_rows},
        )

    def tearDown(self):
        self.directory.cleanup()

    def assignments(self):
        sale = {
            "id": "hmlr-sale-one",
            "address": "1 TEST ROAD",
            "postcode": "KT10 0AA",
            "price": 2_000_000,
            "date": "2025-01-02",
            "source": SOURCE_NAME,
        }
        complete = {
            "propertyRecordId": self.property_one,
            "coverageStatus": "complete",
            "totalTransactions": 1,
            "latestTransaction": sale,
            "transactions": [sale],
            "source": SOURCE_NAME,
            "updatedAt": self.checked_at,
        }
        unavailable = {
            "propertyRecordId": self.property_two,
            "coverageStatus": "unavailable",
            "coverageReason": "No postcode in the source Price Paid record",
            "totalTransactions": 0,
            "latestTransaction": None,
            "transactions": [],
            "source": SOURCE_NAME,
            "updatedAt": self.checked_at,
        }
        histories = {
            self.property_one: complete,
            self.property_two: unavailable,
            "txn-one": complete,
            "txn-two": unavailable,
        }
        _, _, _, base_fingerprint = base_feed_identity(self.base_rows)
        metadata = {
            "schemaVersion": 1,
            "deploymentMode": "commercial",
            "publicationStatus": "complete",
            "coverageMode": "full-available-price-paid-history",
            "coverageStatus": "complete-accounted",
            "source": SOURCE_NAME,
            "sourceLicenceUrl": SOURCE_LICENCE_URL,
            "redistributionRights": REDISTRIBUTION_RIGHTS,
            "addressDataUse": ADDRESS_DATA_USE,
            "attribution": ATTRIBUTION,
            "coverageFrom": "1995",
            "updatedAt": self.checked_at,
            "sourceCheckedAt": self.checked_at,
            "freshnessWindowDays": 45,
            "propertiesRequested": 2,
            "propertiesChecked": 1,
            "propertiesUnavailable": 1,
            "propertiesNotChecked": 0,
            "propertiesWithHistory": 1,
            "propertiesCheckedNoHistory": 0,
            "transactionsFound": 1,
            "lookupKeys": 4,
            "canonicalPropertyRecords": 2,
            "transactionAliases": 2,
            "baseFeedFingerprint": base_fingerprint,
            "historyFingerprint": sha256_json(histories),
        }
        return {
            "SURREY_SALES_HISTORY": histories,
            "SURREY_SALES_HISTORY_META": metadata,
        }

    def write_sales(self, assignments):
        write_assignments(self.sales_path, assignments)

    def test_accepts_exact_canonical_and_transaction_identity_universe(self):
        self.write_sales(self.assignments())
        result = validate(
            self.sales_path,
            base_feed=self.base_path,
            minimum_properties_with_history=1,
            minimum_transactions=1,
            maximum_properties_unavailable=1,
        )
        self.assertEqual(result["canonicalPropertyRecords"], 2)
        self.assertEqual(result["transactionAliases"], 2)

    def test_rejects_same_count_base_identity_pair_mutation(self):
        assignments = self.assignments()
        histories = assignments["SURREY_SALES_HISTORY"]
        histories["txn-one"] = histories[self.property_two]
        histories["txn-two"] = histories[self.property_one]
        assignments["SURREY_SALES_HISTORY_META"]["historyFingerprint"] = (
            sha256_json(histories)
        )
        self.write_sales(assignments)
        write_assignments(
            self.base_path,
            {
                "SURREY_LAND_REG_TRANSACTIONS": [
                    {"id": "txn-one", "propertyRecordId": self.property_two},
                    {"id": "txn-two", "propertyRecordId": self.property_one},
                ]
            },
        )
        with self.assertRaisesRegex(ValueError, "baseFeedFingerprint"):
            validate(self.sales_path, base_feed=self.base_path)

    def test_rejects_content_mutation_with_unchanged_history_fingerprint(self):
        assignments = self.assignments()
        changed = copy.deepcopy(
            assignments["SURREY_SALES_HISTORY"][self.property_one]
        )
        changed["transactions"][0]["price"] = 2_100_000
        changed["latestTransaction"] = changed["transactions"][0]
        assignments["SURREY_SALES_HISTORY"][self.property_one] = changed
        assignments["SURREY_SALES_HISTORY"]["txn-one"] = changed
        self.write_sales(assignments)
        with self.assertRaisesRegex(ValueError, "historyFingerprint"):
            validate(self.sales_path, base_feed=self.base_path)

    def test_failed_refresh_never_presents_retained_rows_as_complete(self):
        status, reason, rows, checked_at = cache_coverage(
            "KT100AA",
            {"KT100AA"},
            {
                "updatedAt": timestamp(days_ago=60),
                "rows": [{"tx": "stale-retained-row"}],
                "lastError": "TimeoutError: source unavailable",
            },
        )
        self.assertEqual(status, "unavailable")
        self.assertIn("source unavailable", reason)
        self.assertEqual(rows, [])
        self.assertEqual(checked_at, "")

    def test_unavailable_property_regression_is_fail_closed(self):
        self.write_sales(self.assignments())
        with self.assertRaisesRegex(ValueError, "unavailable-property ceiling"):
            validate(
                self.sales_path,
                base_feed=self.base_path,
                maximum_properties_unavailable=0,
            )

    def test_source_freshness_is_oldest_complete_lookup_not_rewrite_time(self):
        assignments = self.assignments()
        oldest_check = timestamp(days_ago=5)
        complete = copy.deepcopy(
            assignments["SURREY_SALES_HISTORY"][self.property_one]
        )
        complete["updatedAt"] = oldest_check
        assignments["SURREY_SALES_HISTORY"][self.property_one] = complete
        assignments["SURREY_SALES_HISTORY"]["txn-one"] = complete
        assignments["SURREY_SALES_HISTORY_META"]["historyFingerprint"] = (
            sha256_json(assignments["SURREY_SALES_HISTORY"])
        )
        self.write_sales(assignments)
        with self.assertRaisesRegex(ValueError, "oldest complete lookup"):
            validate(self.sales_path, base_feed=self.base_path)

        assignments["SURREY_SALES_HISTORY_META"]["sourceCheckedAt"] = oldest_check
        self.write_sales(assignments)
        validate(self.sales_path, base_feed=self.base_path, maximum_age_days=10)
        with self.assertRaisesRegex(ValueError, "stale"):
            validate(self.sales_path, base_feed=self.base_path, maximum_age_days=1)

    def test_checked_in_sales_feed_preserves_reviewed_counts(self):
        result = validate(
            ROOT / "outputs" / "sales-history.js",
            base_feed=ROOT / "outputs" / "surrey-transactions.js",
            minimum_properties_with_history=3942,
            minimum_transactions=6735,
            maximum_properties_unavailable=4,
            maximum_age_days=45,
        )
        self.assertEqual(
            result,
            {
                "lookupKeys": 8626,
                "canonicalPropertyRecords": 3947,
                "transactionAliases": 4679,
                "propertiesWithHistory": 3942,
                "transactionsFound": 6735,
                "updatedAt": "2026-07-21T22:07:55Z",
            },
        )

    def test_workflows_apply_remote_contract_after_rebase_and_daily(self):
        sales_workflow = (
            ROOT / ".github" / "workflows" / "sales-history-feed.yml"
        ).read_text(encoding="utf-8")
        pull_index = sales_workflow.rindex("git pull --rebase --autostash")
        revalidation_index = sales_workflow.rindex(
            "python3 scripts/validate_sales_history_feed.py"
        )
        add_index = sales_workflow.rindex("git add outputs/sales-history.js")
        self.assertLess(pull_index, revalidation_index)
        self.assertLess(revalidation_index, add_index)
        self.assertEqual(
            sales_workflow.count("--minimum-properties-with-history 3942"),
            2,
        )
        self.assertEqual(sales_workflow.count("--minimum-transactions 6735"), 2)
        self.assertEqual(
            sales_workflow.count("--maximum-properties-unavailable 4"),
            2,
        )
        self.assertIn("--refresh-days 28", sales_workflow)

        daily = (
            ROOT / ".github" / "workflows" / "data-completeness.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("scripts/validate_sales_history_feed.py", daily)
        self.assertIn("--minimum-properties-with-history 3942", daily)
        self.assertIn("--minimum-transactions 6735", daily)
        self.assertIn("--maximum-properties-unavailable 4", daily)
        self.assertIn("scripts/validate_planning_feed.py", daily)
        self.assertIn("--minimum-properties-with-history 3204", daily)
        self.assertIn("--minimum-applications 21180", daily)


if __name__ == "__main__":
    unittest.main()
