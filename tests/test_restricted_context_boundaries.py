import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from enrich_property_context import remove_disabled_restricted_cache_entries  # noqa: E402


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

    def test_public_workflows_keep_restricted_context_sources_dormant(self):
        workflows = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / ".github" / "workflows").glob("*.yml")
        )
        self.assertIn("--disable-osm", workflows)
        self.assertNotIn("COMPANIES_HOUSE_API_KEY:", workflows)
        self.assertNotIn("enrich_daily_intelligence.py", workflows)

    def test_news_refresh_cannot_mutate_today_opportunities(self):
        workflow = (ROOT / ".github" / "workflows" / "news-feed.yml").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("outputs/today-feed.js", workflow)
        self.assertNotIn("update_today_news.py", workflow)


if __name__ == "__main__":
    unittest.main()
