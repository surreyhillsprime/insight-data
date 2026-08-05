import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_commercial_planning_feed  # noqa: E402
import validate_planning_feed  # noqa: E402
from insight_data_utils import property_record_id  # noqa: E402


def transaction(transaction_id):
    item = {
        "id": transaction_id,
        "market": "waverley-south-surrey",
        "district": "Waverley",
        "address": "FORTUNE HOUSE, KNOWLE LANE, CRANLEIGH, GU6 8JP",
        "postcode": "GU6 8JP",
        "price": 3_750_000,
        "date": "2020-01-02",
        "uprn": "100000000001",
    }
    item["propertyRecordId"] = property_record_id(item)
    return item


def write_base_feed(path, rows, metadata):
    path.write_text("\n".join([
        "window.SURREY_LAND_REG_TRANSACTIONS = "
        + json.dumps(rows, separators=(",", ":"))
        + ";",
        "window.SURREY_LAND_REG_SUMMARY = {};",
        "window.SURREY_LAND_REG_META = "
        + json.dumps(metadata, separators=(",", ":"))
        + ";",
        "",
    ]), encoding="utf-8")


class CommercialPlanningFeedTests(unittest.TestCase):
    def test_declared_legacy_address_unions_once_and_excludes_neighbour(self):
        first = transaction("txn-fortune-old")
        repeat = transaction("txn-fortune-new")
        canonical_id = first["propertyRecordId"]
        legacy_id = (
            "property:FORTUNE HOUSE KNOWLE LANE CRANLEIGH GU6 8UW|GU68UW"
        )
        base_metadata = {
            "schemaVersion": 3,
            "priceFloor": 2_000_000,
            "addressCanonicalisation": {
                "sourceAddressVariants": {
                    canonical_id: [{
                        "propertyRecordId": legacy_id,
                        "address": (
                            "FORTUNE HOUSE, KNOWLE LANE, CRANLEIGH, GU6 8UW"
                        ),
                        "postcode": "GU68UW",
                    }],
                },
            },
        }
        source = [
            {
                "authority": "Waverley",
                "reference": "WA/2020/0001",
                "siteAddress": (
                    "Fortune House, Knowle Lane, Cranleigh, GU6 8UW"
                ),
                "postcode": "GU6 8UW",
                "decisionDate": "2020-04-01",
                "proposal": "Alterations",
                "uprn": "100000000001",
            },
            {
                "authority": "Waverley",
                "reference": "WA/2021/0002",
                "siteAddress": (
                    "Fortune House, Knowle Lane, Cranleigh, GU6 8UW"
                ),
                "postcode": "GU6 8UW",
                "decisionDate": "2021-05-01",
                "proposal": "Extension",
            },
            {
                "authority": "Waverley",
                "reference": "WA/2022/NEIGHBOUR",
                "siteAddress": (
                    "Neighbour House, Knowle Lane, Cranleigh, GU6 8UW"
                ),
                "postcode": "GU6 8UW",
                "decisionDate": "2022-06-01",
                "proposal": "Neighbouring works",
            },
        ]

        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            base_path = directory / "transactions.js"
            source_path = directory / "planning.json"
            output_path = directory / "planning-history.js"
            write_base_feed(
                base_path,
                [first, repeat],
                base_metadata,
            )
            source_path.write_text(json.dumps(source), encoding="utf-8")
            with patch.object(sys, "argv", [
                "build_commercial_planning_feed.py",
                "--source", str(source_path),
                "--source-name", "Test licensed source",
                "--source-licence-url", "https://example.test/licence",
                "--input-js", str(base_path),
                "--write-js", str(output_path),
            ]):
                build_commercial_planning_feed.main()
            result = validate_planning_feed.validate(
                output_path,
                base_feed=base_path,
            )
            text = output_path.read_text(encoding="utf-8")
            histories = validate_planning_feed.assignment(
                text,
                "SURREY_PLANNING_HISTORY",
            )

        canonical = histories[canonical_id]
        self.assertEqual(result["canonicalPropertyRecords"], 1)
        self.assertEqual(result["transactionAliases"], 2)
        self.assertEqual(result["propertiesWithHistory"], 1)
        self.assertEqual(result["applicationsFound"], 2)
        self.assertEqual(canonical["totalApplications"], 2)
        self.assertEqual(
            {application["reference"] for application in canonical["applications"]},
            {"WA/2020/0001", "WA/2021/0002"},
        )
        self.assertEqual(
            canonical["matchMethod"],
            "uprn+declared-source-address-variants",
        )
        self.assertEqual(histories[first["id"]], canonical)
        self.assertEqual(histories[repeat["id"]], canonical)


if __name__ == "__main__":
    unittest.main()
