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

    def test_news_is_validated_and_published_without_mutating_today(self):
        self.assertIn("scripts/validate_news_sources.py", self.workflow)
        self.assertIn("scripts/validate_news_feed.py", self.workflow)
        self.assertIn("scripts/check_news_freshness.py", self.workflow)
        self.assertNotIn("today-feed", self.workflow.lower())
        self.assertNotIn("update_today_news.py", self.workflow)
        self.assertNotIn("validate_today_news.py", self.workflow)
        self.assertRegex(
            self.workflow,
            re.compile(
                r"^\s*git add outputs/news-feed\.js\s*$",
                re.MULTILINE,
            ),
        )

    def test_remote_changes_are_applied_before_final_validation_and_commit(self):
        pull_index = self.workflow.rindex("git pull --rebase --autostash")
        source_validation_index = self.workflow.rindex(
            "python3 scripts/validate_news_sources.py"
        )
        feed_validation_index = self.workflow.rindex(
            "python3 scripts/validate_news_feed.py"
        )
        add_index = self.workflow.rindex("git add outputs/news-feed.js")

        self.assertLess(pull_index, source_validation_index)
        self.assertLess(source_validation_index, feed_validation_index)
        self.assertLess(feed_validation_index, add_index)


if __name__ == "__main__":
    unittest.main()
