import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from check_news_freshness import check


class NewsFreshnessTests(unittest.TestCase):
    def test_stale_pipeline_fails_but_stale_editorial_is_only_a_warning(self):
        metadata = {
            "generatedAt": "2026-07-28T10:00:00Z",
            "newestPublishedAt": "2026-07-01T10:00:00Z",
            "sourceDiagnostics": [],
        }
        items = [{"publishedAt": "2026-07-01T10:00:00Z"}]
        with (
            mock.patch("check_news_freshness.validate", return_value=(items, metadata)),
            mock.patch("check_news_freshness.load_registry", return_value={"sources": []}),
        ):
            failures, warnings, metrics = check(
                Path("feed"),
                Path("sources"),
                now=datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc),
            )
        self.assertEqual(metrics["pipelineAgeMinutes"], 120)
        self.assertTrue(any("pipeline" in failure for failure in failures))
        self.assertTrue(any("newest qualifying article" in warning for warning in warnings))

    def test_current_pipeline_with_quiet_source_passes(self):
        metadata = {
            "generatedAt": "2026-07-28T11:45:00Z",
            "newestPublishedAt": "2026-07-28T09:00:00Z",
            "sourceDiagnostics": [
                {
                    "sourceId": "quiet",
                    "publicationMode": "live",
                    "status": "empty",
                }
            ],
        }
        items = [{"publishedAt": "2026-07-28T09:00:00Z"}]
        with (
            mock.patch("check_news_freshness.validate", return_value=(items, metadata)),
            mock.patch("check_news_freshness.load_registry", return_value={"sources": []}),
        ):
            failures, warnings, _metrics = check(
                Path("feed"),
                Path("sources"),
                now=datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc),
            )
        self.assertEqual(failures, [])
        self.assertTrue(any("quiet" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
