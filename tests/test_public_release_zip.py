import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import validate_public_release_zip  # noqa: E402


class PublicReleaseZipTests(unittest.TestCase):
    def test_tracked_public_package_passes_the_profile_and_data_boundary(self):
        result = validate_public_release_zip.validate(
            ROOT / "downloads" / "INSIGHT-macOS.zip"
        )
        self.assertEqual(result["version"], "2.0.0")
        self.assertEqual(result["build"], "37")
        self.assertEqual(result["propertyCount"], 3947)

    def test_populated_or_oversized_planning_asset_is_rejected(self):
        source = ROOT / "downloads" / "INSIGHT-macOS.zip"
        planning_member = (
            "INSIGHT.app/Contents/Resources/web/planning-history.js"
        )
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "INSIGHT.zip"
            with zipfile.ZipFile(source) as original, zipfile.ZipFile(
                candidate,
                "w",
                compression=zipfile.ZIP_DEFLATED,
            ) as modified:
                for item in original.infolist():
                    payload = original.read(item.filename)
                    if item.filename == planning_member:
                        payload = (
                            b"window.SURREY_PLANNING_HISTORY = {"
                            + b'"property:private":{"applications":[{}]}};\n'
                            + b"window.SURREY_PLANNING_HISTORY_META = {};\n"
                        )
                    modified.writestr(item, payload)
            with self.assertRaises(ValueError):
                validate_public_release_zip.validate(candidate)


if __name__ == "__main__":
    unittest.main()
