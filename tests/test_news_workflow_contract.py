import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NewsWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (
            ROOT / ".github" / "workflows" / "news-feed.yml"
        ).read_text(encoding="utf-8")

    def test_cadence_and_concurrency_are_news_specific(self):
        self.assertIn("cron: '17,47 * * * *'", self.workflow)
        self.assertIn("repository_dispatch:", self.workflow)
        self.assertIn("types: [news-refresh]", self.workflow)
        self.assertIn("group: insight-news-refresh-${{ github.ref }}", self.workflow)
        self.assertNotIn("group: insight-data-refresh", self.workflow)
        self.assertIn("timeout-minutes: 15", self.workflow)

    def test_news_and_today_are_validated_and_committed_together(self):
        self.assertIn("scripts/validate_news_sources.py", self.workflow)
        self.assertIn("scripts/check_news_freshness.py", self.workflow)
        self.assertIn("scripts/update_today_news.py", self.workflow)
        self.assertIn("scripts/validate_today_news.py", self.workflow)
        self.assertRegex(
            self.workflow,
            re.compile(
                r"git add outputs/news-feed\.js outputs/today-feed\.js"
            ),
        )


if __name__ == "__main__":
    unittest.main()
