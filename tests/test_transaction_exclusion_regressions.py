"""Reviewed input errors stay quarantined across refresh and history rebuilds."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from collect_title_history import matched_history_rows, migrate_existing_history, transaction_from_row
from sweep_land_registry import normalise_rows
from transaction_exclusions import find_transaction_exclusion, load_transaction_exclusion_ledger
from validate_sales_history_feed import validate

SOURCE_ID = "http://landregistry.data.gov.uk/data/ppi/transaction/5834E4E9-453F-29C7-E063-4804A8C015BC/current"
PROPERTY_ID = "property:9 ST JOHNS AVENUE LEATHERHEAD KT22 7HT|KT227HT"
BAD_RAW = dict(tx=SOURCE_ID, price=112_500_000, date="2026-06-19", postcode="KT22 7HT", propertyType="D", paon="9", street="ST JOHNS AVENUE", town="LEATHERHEAD", district="MOLE VALLEY", category="A")


class TransactionExclusionRegressionTests(unittest.TestCase):
    def test_st_johns_input_error_is_removed_from_source_and_processed_refresh_rows(self):
        processed = {**BAD_RAW, "tx": "", "id": "lr-b4ed8ccb8d031a5ef07f"}
        reformatted = {**BAD_RAW, "paon": "9A", "price": 11_250_000}
        neighbouring = {**BAD_RAW, "tx": "different-source", "paon": "11", "price": 2_250_000}
        later_sale = {**BAD_RAW, "tx": "later-source", "date": "2026-07-01", "price": 2_500_000}
        _, rows = normalise_rows([BAD_RAW, processed, reformatted, neighbouring, later_sale])
        self.assertEqual(sorted(row["price"] for row in rows), [2_250_000, 2_500_000])
        self.assertEqual(len(load_transaction_exclusion_ledger()["exclusions"]), 2)

    def test_source_identity_stays_quarantined_when_history_address_or_price_changes(self):
        sale = transaction_from_row(BAD_RAW)
        self.assertIsNotNone(find_transaction_exclusion({**sale, "address": "REFORMATTED ADDRESS", "price": 1_125_000}))
        self.assertIsNone(find_transaction_exclusion({**sale, "id": SOURCE_ID.replace("5834E4E9", "00000000")}))

    def test_history_refresh_retains_legitimate_earlier_sale(self):
        good = {**BAD_RAW, "tx": "earlier-source", "date": "2018-06-07", "price": 681_000}
        item = {**transaction_from_row(BAD_RAW), "paon": "9", "street": "ST JOHNS AVENUE", "town": "LEATHERHEAD"}
        rows, _ = matched_history_rows(item, [BAD_RAW, good], [item])
        self.assertEqual([row["price"] for row in rows], [681_000])

    def test_unmatched_malformed_price_is_not_materialised_by_exclusion_filter(self):
        good = {**BAD_RAW, "tx": "earlier-source", "date": "2018-06-07", "price": 681_000}
        item = {**transaction_from_row(good), "paon": "9", "street": "ST JOHNS AVENUE", "town": "LEATHERHEAD"}
        for price in ("not-a-price", "NaN", "Infinity"):
            with self.subTest(price=price):
                unrelated = {**good, "tx": "unrelated-source", "paon": "11", "price": price}
                rows, _ = matched_history_rows(item, [unrelated, good], [])
                self.assertEqual(rows, [good])

    def test_history_migration_removes_only_reviewed_sale_and_retains_source_dates(self):
        bad = transaction_from_row(BAD_RAW)
        good = {**bad, "id": "earlier-source", "date": "2018-06-07", "price": 681_000}
        base = {**bad, "id": "lr-later-legitimate", "propertyRecordId": PROPERTY_ID, "price": 2_500_000, "date": "2026-07-01"}
        prior = {"propertyRecordId": PROPERTY_ID, "coverageStatus": "complete", "transactions": [bad, good], "updatedAt": "2026-09-01T08:11:42Z"}
        histories, _ = migrate_existing_history([base], {PROPERTY_ID: prior, base["id"]: prior}, {}, "local")
        self.assertEqual(histories[PROPERTY_ID]["transactions"], [good])
        self.assertEqual(histories[PROPERTY_ID]["latestTransaction"], good)
        self.assertEqual(histories[PROPERTY_ID]["updatedAt"], prior["updatedAt"])

    def test_publication_gate_rejects_reviewed_transaction_in_history(self):
        sale = transaction_from_row(BAD_RAW)
        record = {"propertyRecordId": PROPERTY_ID, "coverageStatus": "complete", "totalTransactions": 1, "latestTransaction": sale, "transactions": [sale]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sales-history.js"
            path.write_text("window.SURREY_SALES_HISTORY = " + json.dumps({PROPERTY_ID: record}) + ";\nwindow.SURREY_SALES_HISTORY_META = " + json.dumps({"schemaVersion": 1, "deploymentMode": "local"}) + ";\n")
            with self.assertRaisesRegex(ValueError, "Reviewed transaction exclusion.*sales history"):
                validate(path, allow_local=True)


if __name__ == "__main__":
    unittest.main()
