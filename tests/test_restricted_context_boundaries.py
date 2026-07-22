import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from enrich_property_context import remove_disabled_restricted_cache_entries  # noqa: E402
from enrich_daily_intelligence import (  # noqa: E402
    remove_disabled_restricted_cache_entries as remove_disabled_daily_cache_entries,
)


class RestrictedContextBoundaryTests(unittest.TestCase):
    def test_disabling_osm_removes_restricted_cached_payloads(self):
        cache = {
            "version": 4,
            "postcodes": {"KT100AA": {"status": "matched"}},
            "osm": {"KT100AA": {"status": "matched", "data": {"places": ["private"]}}},
        }

        cleaned = remove_disabled_restricted_cache_entries(
            cache,
            SimpleNamespace(disable_osm=True),
        )

        self.assertEqual(cleaned["osm"], {})
        self.assertTrue(cleaned["postcodes"])

    def test_disabling_companies_house_removes_restricted_cached_payloads(self):
        cache = {
            "planningApplications": {"query": {"status": "matched"}},
            "companiesHouse": {"01234567": {"status": "matched", "data": {"private": True}}},
        }

        cleaned = remove_disabled_daily_cache_entries(
            cache,
            SimpleNamespace(disable_companies_house=True),
        )

        self.assertEqual(cleaned["companiesHouse"], {})
        self.assertTrue(cleaned["planningApplications"])

    def test_public_workflows_keep_restricted_context_sources_dormant(self):
        workflows = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / ".github" / "workflows").glob("*.yml")
        )
        self.assertIn("--disable-osm", workflows)
        self.assertIn("--disable-companies-house", workflows)
        self.assertNotIn("COMPANIES_HOUSE_API_KEY:", workflows)


if __name__ == "__main__":
    unittest.main()
