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


class FeedSchemaContractTests(unittest.TestCase):
    def test_published_feed_uses_exact_fail_closed_property_identity(self):
        rows, _summary, metadata = read_js(ROOT / "outputs" / "surrey-transactions.js")
        expected_ids = [property_record_id(row) for row in rows]

        self.assertEqual(FEED_SCHEMA_VERSION, 3)
        self.assertEqual(metadata["schemaVersion"], FEED_SCHEMA_VERSION)
        self.assertEqual(metadata["propertyRecordSchemaVersion"], PROPERTY_RECORD_SCHEMA_VERSION)
        self.assertEqual(metadata["canonicalPropertyRecords"], len(set(expected_ids)))
        self.assertEqual(metadata["propertyIdentityMode"], "full-normalised-address-plus-postcode-fail-closed")
        self.assertTrue(all(row["propertyRecordId"] == expected for row, expected in zip(rows, expected_ids)))

        no_postcode = [row for row in rows if not row.get("postcode")]
        self.assertEqual(len(no_postcode), 1)
        self.assertEqual(
            no_postcode[0]["propertyRecordId"],
            "property:THE OLD GROVE HIGH PITFOLD HINDHEAD|NOPOSTCODE",
        )

    def test_shared_writer_cannot_downgrade_a_legacy_input(self):
        row = {
            "id": "lr-test",
            "address": "1 Test Road, Esher, KT10 0AA",
            "postcode": "KT10 0AA",
            "market": "elmbridge-prime",
            "price": 3_000_000,
            "date": "2026-01-01",
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "feed.js"
            write_js(output, [row], {"schemaVersion": 2})
            rows, _summary, metadata = read_js(output)

        self.assertEqual(metadata["schemaVersion"], 3)
        self.assertEqual(metadata["propertyRecordSchemaVersion"], 1)
        self.assertEqual(rows[0]["propertyRecordId"], property_record_id(row))

    def test_independent_enrichers_publish_through_the_canonical_writer(self):
        for filename in ("enrich_epc_data.py", "enrich_property_context.py"):
            source = (ROOT / "scripts" / filename).read_text(encoding="utf-8")
            self.assertIn("from insight_data_utils import write_js as write_canonical_js", source)
            self.assertIn("write_canonical_js(args.write_js, enriched, meta)", source)


if __name__ == "__main__":
    unittest.main()
