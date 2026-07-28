import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from collect_news import SCORING_VERSION, collect, write_feed


NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


def existing_item(source_id, source, publisher_group, lane, url):
    return {
        "id": "news-" + ("a" if source_id == "guildford-dragon" else "b") * 20,
        "title": "Guildford housing development approved",
        "url": url,
        "sourceId": source_id,
        "source": source,
        "sourceCategory": "surrey-local",
        "publisherGroup": publisher_group,
        "lane": lane,
        "rightsMode": "link-only",
        "publishedAt": "2026-07-27T10:00:00Z",
        "score": 75,
        "scoreBase": 70,
        "scoringVersion": SCORING_VERSION,
        "location": "Guildford",
        "matchType": "town",
        "topics": ["Planning"],
        "reason": "Matches Guildford · Planning",
    }


class NewsCollectionTests(unittest.TestCase):
    def setUp(self):
        self.registry = json.loads(
            (ROOT / "config" / "news-sources.json").read_text(encoding="utf-8")
        )

    def files(self, directory):
        root = Path(directory)
        registry = root / "sources.json"
        transactions = root / "transactions.js"
        output = root / "news.js"
        registry.write_text(json.dumps(self.registry), encoding="utf-8")
        transactions.write_text(
            'window.SURREY_LAND_REG_TRANSACTIONS = '
            + json.dumps(
                [
                    {
                        "address": "1 Test Road, Guildford",
                        "town": "Guildford",
                        "district": "Guildford",
                        "estate": "",
                    }
                ]
            )
            + ";\n"
            "window.SURREY_LAND_REG_SUMMARY = {};\n"
            "window.SURREY_LAND_REG_META = {};\n",
            encoding="utf-8",
        )
        return registry, transactions, output

    def test_partial_failure_carries_only_that_sources_last_known_good(self):
        with tempfile.TemporaryDirectory() as directory:
            registry, transactions, output = self.files(directory)
            carried = existing_item(
                "guildford-dragon",
                "Guildford Dragon",
                "guildford-dragon",
                "surrey-local",
                "https://guildford-dragon.com/carried-story",
            )
            obsolete_success = existing_item(
                "hmlr-official",
                "HM Land Registry",
                "uk-government",
                "official-market",
                "https://www.gov.uk/government/statistics/old-hpi",
            )
            write_feed(
                output,
                [carried, obsolete_success],
                {
                    "sourceDiagnostics": [
                        {
                            "sourceId": "guildford-dragon",
                            "lastSuccessAt": "2026-07-27T11:00:00Z",
                            "consecutiveFailures": 0,
                        }
                    ]
                },
            )
            called = []

            def fetcher(source, _timeout):
                called.append(source["id"])
                if source["id"] == "guildford-dragon":
                    raise RuntimeError("fixture outage")
                if source["id"] == "epsom-ewell-times-planning":
                    return (
                        [
                            {
                                "title": "Major Surrey housing development approved",
                                "url": "https://epsomandewelltimes.com/new-homes-approved",
                                "publishedAt": "2026-07-28T11:00:00Z",
                                "sourceId": source["id"],
                                "source": source["name"],
                                "sourceCategory": source["category"],
                                "rightsMode": "link-only",
                                "_description": "A planning decision for new homes.",
                            }
                        ],
                        {"discovered": 1, "parsed": 1},
                    )
                return [], {"discovered": 0, "parsed": 0}

            items, metadata = collect(
                registry,
                transactions,
                output,
                45,
                30,
                5,
                fetcher=fetcher,
                now=NOW,
            )
        urls = {item["url"] for item in items}
        self.assertIn("https://guildford-dragon.com/carried-story", urls)
        self.assertIn("https://epsomandewelltimes.com/new-homes-approved", urls)
        self.assertNotIn("https://www.gov.uk/government/statistics/old-hpi", urls)
        self.assertNotIn("prime-resi", called)
        diagnostic = {
            row["sourceId"]: row for row in metadata["sourceDiagnostics"]
        }["guildford-dragon"]
        self.assertEqual(diagnostic["status"], "failed")
        self.assertEqual(diagnostic["retained"], 1)

    def test_shadow_source_is_scored_but_never_published(self):
        self.registry["sources"][0]["publicationMode"] = "shadow"
        with tempfile.TemporaryDirectory() as directory:
            registry, transactions, output = self.files(directory)

            def fetcher(source, _timeout):
                if source["id"] == "epsom-ewell-times-planning":
                    return (
                        [
                            {
                                "title": "Major Surrey housing development approved",
                                "url": "https://epsomandewelltimes.com/shadow-story",
                                "publishedAt": "2026-07-28T11:00:00Z",
                                "sourceId": source["id"],
                                "source": source["name"],
                                "sourceCategory": source["category"],
                                "rightsMode": "link-only",
                                "_description": "A planning decision for new homes.",
                            }
                        ],
                        {},
                    )
                return [], {}

            items, metadata = collect(
                registry,
                transactions,
                output,
                45,
                30,
                5,
                fetcher=fetcher,
                now=NOW,
            )
        self.assertEqual(items, [])
        diagnostic = {
            row["sourceId"]: row for row in metadata["sourceDiagnostics"]
        }["epsom-ewell-times-planning"]
        self.assertEqual(diagnostic["qualified"], 1)
        self.assertEqual(diagnostic["retained"], 0)

    def test_total_live_failure_preserves_output(self):
        with tempfile.TemporaryDirectory() as directory:
            registry, transactions, output = self.files(directory)
            output.write_text("sentinel", encoding="utf-8")

            def fetcher(_source, _timeout):
                raise RuntimeError("fixture outage")

            with self.assertRaisesRegex(RuntimeError, "Every live"):
                collect(
                    registry,
                    transactions,
                    output,
                    45,
                    30,
                    5,
                    fetcher=fetcher,
                    now=NOW,
                )
            self.assertEqual(output.read_text(encoding="utf-8"), "sentinel")


if __name__ == "__main__":
    unittest.main()
