import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import validate_planning_feed  # noqa: E402
import validate_sales_history_feed  # noqa: E402


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

    def test_planning_rejects_property_key_outside_base_feed(self):
        planning = self.root / "planning.js"
        write_assignments(
            planning,
            {
                "SURREY_PLANNING_HISTORY": {"property:ARBITRARY|KT100AA": {}},
                "SURREY_PLANNING_HISTORY_META": {
                    "schemaVersion": 1,
                    "deploymentMode": "commercial",
                    "propertiesChecked": 1,
                    "propertiesWithHistory": 1,
                },
            },
        )
        with self.assertRaisesRegex(ValueError, "outside the canonical base feed"):
            self.run_validator(validate_planning_feed, planning, "--base-feed", self.base)

    def test_planning_accepts_canonical_property_and_transaction_aliases(self):
        planning = self.root / "planning.js"
        write_assignments(
            planning,
            {
                "SURREY_PLANNING_HISTORY": {
                    self.property_id: {"applications": []},
                    self.transaction_id: {"applications": []},
                },
                "SURREY_PLANNING_HISTORY_META": {
                    "schemaVersion": 1,
                    "deploymentMode": "commercial",
                    "propertiesChecked": 1,
                    "propertiesWithHistory": 1,
                },
            },
        )
        self.run_validator(validate_planning_feed, planning, "--base-feed", self.base)

    def test_sales_rejects_stale_canonical_property_set(self):
        sales = self.root / "sales.js"
        write_assignments(
            sales,
            {
                "SURREY_SALES_HISTORY": {},
                "SURREY_SALES_HISTORY_META": {
                    "schemaVersion": 1,
                    "deploymentMode": "commercial",
                    "propertiesChecked": 1,
                },
            },
        )
        with self.assertRaisesRegex(ValueError, "coverage is stale"):
            self.run_validator(validate_sales_history_feed, sales, "--base-feed", self.base)


if __name__ == "__main__":
    unittest.main()
