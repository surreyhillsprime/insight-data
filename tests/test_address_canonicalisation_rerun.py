import csv
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from insight_data_utils import read_js  # noqa: E402
from sweep_land_registry import main as sweep_main  # noqa: E402


class AddressCanonicalisationRerunTests(unittest.TestCase):
    def test_no_fetch_local_csv_rerun_preserves_published_source_ledger(self):
        source_feed = ROOT / "outputs" / "surrey-transactions.js"
        source_csv = ROOT / "work" / "land-reg-surrey-2m-1995.csv"
        _rows, _summary, source_meta = read_js(source_feed)
        source_stats = source_meta["addressCanonicalisation"]
        self.assertGreater(source_stats["sourceAddressVariantCount"], 0)
        self.assertGreater(source_stats["identityAliasesCollapsed"], 0)

        with tempfile.TemporaryDirectory() as directory:
            rerun_feed = Path(directory) / "surrey-transactions.js"
            rerun_csv = Path(directory) / "land-reg-surrey-2m-1995.csv"
            shutil.copyfile(source_feed, rerun_feed)
            shutil.copyfile(source_csv, rerun_csv)
            argv = [
                "sweep_land_registry.py",
                "--no-fetch",
                "--from-csv",
                str(rerun_csv),
                "--write-csv",
                str(rerun_csv),
                "--write-js",
                str(rerun_feed),
            ]
            with patch.object(sys, "argv", argv), redirect_stdout(StringIO()):
                self.assertEqual(sweep_main(), 0)
            _rows, _summary, rerun_meta = read_js(rerun_feed)

            with rerun_csv.open(newline="", encoding="utf-8") as handle:
                fieldnames = csv.DictReader(handle).fieldnames
            new_row = {field: "" for field in fieldnames}
            new_row.update({
                "id": "lr-ffffffffffffffffffff",
                "propertyRecordId": "property:999 NEW TEST ROAD ESHER KT10 0ZZ|KT100ZZ",
                "address": "999, NEW TEST ROAD, ESHER, KT10 0ZZ",
                "paon": "999",
                "street": "NEW TEST ROAD",
                "town": "ESHER",
                "postcode": "KT10 0ZZ",
                "district": "Elmbridge",
                "propertyType": "Detached",
                "price": "2100000",
                "date": "2026-07-01",
                "market": "elmbridge-prime",
                "category": "A",
            })
            with rerun_csv.open("a", newline="", encoding="utf-8") as handle:
                csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n").writerow(new_row)
            with patch.object(sys, "argv", argv), redirect_stdout(StringIO()):
                self.assertEqual(sweep_main(), 0)
            _rows, _summary, grown_meta = read_js(rerun_feed)

        self.assertEqual(
            rerun_meta["addressCanonicalisation"],
            source_stats,
        )
        grown_stats = grown_meta["addressCanonicalisation"]
        self.assertEqual(
            grown_stats["sourceAddressVariants"],
            source_stats["sourceAddressVariants"],
        )
        self.assertEqual(
            grown_stats["identityAliasesCollapsed"],
            source_stats["identityAliasesCollapsed"],
        )
        self.assertEqual(grown_stats["rows"], source_stats["rows"] + 1)
        self.assertEqual(
            grown_stats["canonicalProperties"],
            source_stats["canonicalProperties"] + 1,
        )
        self.assertEqual(
            grown_stats["sourceAddressIdentities"],
            source_stats["sourceAddressIdentities"] + 1,
        )


if __name__ == "__main__":
    unittest.main()
