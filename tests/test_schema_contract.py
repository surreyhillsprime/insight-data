import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from insight_data_utils import (  # noqa: E402
    FEED_SCHEMA_VERSION,
    PROPERTY_RECORD_SCHEMA_VERSION,
    property_record_id,
    read_js,
    write_js,
)
from migrate_feed_schema import migrate_transaction  # noqa: E402


def transaction(**overrides):
    row = {
        "id": "legacy-id",
        "address": "1 Test Road, Esher, KT10 0AA",
        "postcode": "KT10 0AA",
        "market": "elmbridge-prime",
        "district": "Elmbridge",
        "price": 2_000_000,
        "date": "2026-01-01",
        "propertyType": "Detached",
        "category": "A",
    }
    row.update(overrides)
    return row


class FeedSchemaContractTests(unittest.TestCase):
    def test_shared_writer_upgrades_legacy_metadata_and_property_identity(self):
        row = transaction(uprn="approximate-postcode-centroid")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "feed.js"
            write_js(output, [row], {"schemaVersion": 2})
            rows, _summary, metadata = read_js(output)

        self.assertEqual(FEED_SCHEMA_VERSION, 3)
        self.assertEqual(metadata["schemaVersion"], FEED_SCHEMA_VERSION)
        self.assertEqual(metadata["propertyRecordSchemaVersion"], PROPERTY_RECORD_SCHEMA_VERSION)
        self.assertEqual(metadata["canonicalPropertyRecords"], 1)
        self.assertEqual(
            metadata["propertyIdentityMode"],
            "full-normalised-address-plus-postcode-fail-closed",
        )
        self.assertEqual(rows[0]["propertyRecordId"], property_record_id(row))
        self.assertNotIn("approximate-postcode-centroid", rows[0]["propertyRecordId"])

    def test_missing_postcode_fails_closed_instead_of_merging(self):
        row = transaction(address="The Old Grove, High Pitfold, Hindhead", postcode="")
        self.assertEqual(
            property_record_id(row),
            "property:THE OLD GROVE HIGH PITFOLD HINDHEAD|NOPOSTCODE",
        )

    def test_migration_recomputes_stable_transaction_and_property_ids(self):
        row = transaction(
            geocode={"source": "Postcodes.io", "precision": "Postcode centroid"},
        )
        migrated = migrate_transaction(row)
        self.assertRegex(migrated["id"], r"^lr-[0-9a-f]{20}$")
        self.assertEqual(migrated["propertyRecordId"], property_record_id(row))
        self.assertEqual(migrated["coordinateSource"], "Postcodes.io")
        self.assertEqual(migrated["coordinatePrecision"], "postcode-centroid")

    def test_independent_enrichers_publish_through_canonical_writer(self):
        for filename in ("enrich_epc_data.py", "enrich_property_context.py"):
            source = (ROOT / "scripts" / filename).read_text(encoding="utf-8")
            self.assertIn("from insight_data_utils import write_js as write_canonical_js", source)
            self.assertTrue(
                "write_canonical_js(args.write_js, enriched, meta)" in source
                or "write_canonical_js_atomic(args.write_js, enriched, meta)" in source
            )


if __name__ == "__main__":
    unittest.main()
