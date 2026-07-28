import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from collect_news import (
    SCORING_VERSION,
    deduplicate,
    feed_entries,
    location_catalog,
    score_article,
    write_feed,
)
from validate_news_feed import validate


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
SOURCE = {
    "id": "test-source",
    "name": "Test Source",
    "category": "prime-specialist",
    "quality": 9,
    "primePropertyBias": 6,
    "publisherGroup": "test-source",
    "lane": "prime-market",
}
TRANSACTIONS = [
    {"address": "1 Old Avenue, Weybridge", "town": "Weybridge", "district": "Elmbridge", "estate": "St George's Hill"},
    {"address": "2 North Drive, Virginia Water", "town": "Virginia Water", "district": "Runnymede", "estate": "Wentworth"},
]


class NewsScoringTests(unittest.TestCase):
    def setUp(self):
        self.catalog = location_catalog(TRANSACTIONS)

    def test_surrey_planning_story_scores_for_main_feed(self):
        article = {
            "title": "Major Wentworth redevelopment approved after planning decision",
            "url": "https://example.com/wentworth-planning",
            "publishedAt": "2026-07-15T10:00:00Z",
            "sourceId": SOURCE["id"],
            "source": SOURCE["name"],
            "sourceCategory": SOURCE["category"],
            "rightsMode": "link-only",
            "_description": "The residential application concerns a country house in Surrey.",
        }
        scored = score_article(article, SOURCE, self.catalog, NOW)
        self.assertIsNotNone(scored)
        self.assertGreaterEqual(scored["score"], 70)
        self.assertEqual(scored["location"], "Wentworth")
        self.assertIn("Planning", scored["topics"])

    def test_generic_interior_story_is_rejected(self):
        article = {
            "title": "Ten decorating ideas for a country dining room",
            "url": "https://example.com/interiors",
            "publishedAt": "2026-07-15T10:00:00Z",
            "sourceId": SOURCE["id"],
            "source": SOURCE["name"],
            "sourceCategory": SOURCE["category"],
            "rightsMode": "link-only",
            "_description": "Interior colours and furniture shopping.",
        }
        self.assertIsNone(score_article(article, SOURCE, self.catalog, NOW))

    def test_generic_named_house_is_not_an_exact_property_match(self):
        catalog = location_catalog([
            {"address": "Thatched Cottage, Test Lane", "town": "Weybridge", "district": "Elmbridge", "estate": ""}
        ])
        self.assertNotIn("Thatched Cottage", catalog["properties"])

    def test_two_million_sale_receives_the_prime_materiality_band(self):
        base = {
            "url": "https://example.com/value-band",
            "publishedAt": "2026-07-15T10:00:00Z",
            "sourceId": SOURCE["id"],
            "source": SOURCE["name"],
            "sourceCategory": SOURCE["category"],
            "rightsMode": "link-only",
            "_description": "A luxury mansion sale on Wentworth Estate in Surrey.",
        }
        two_million = score_article(
            {**base, "title": "Wentworth luxury mansion sells for £2.5m"},
            SOURCE,
            self.catalog,
            NOW,
        )
        below_floor = score_article(
            {
                **base,
                "url": "https://example.com/lower-value-band",
                "title": "Wentworth luxury mansion sells for £1.9m",
            },
            SOURCE,
            self.catalog,
            NOW,
        )
        self.assertIsNotNone(two_million)
        self.assertIsNotNone(below_floor)
        self.assertEqual(two_million["score"], below_floor["score"] + 2)

    def test_duplicate_story_cluster_keeps_higher_score(self):
        base = {
            "location": "Wentworth",
            "topics": ["Planning"],
            "publishedAt": "2026-07-15T10:00:00Z",
        }
        items = [
            dict(base, id="low", title="Wentworth planning scheme wins approval", url="https://example.com/one", score=72),
            dict(base, id="high", title="Wentworth planning scheme wins final approval", url="https://example.com/two", score=88),
        ]
        selected = deduplicate(items)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["id"], "high")

    def test_rss_parser_retains_metadata_not_article_body(self):
        payload = b'''<?xml version="1.0"?><rss><channel><item>
          <title>Surrey house sale</title><link>https://example.com/story?tracking=yes</link>
          <pubDate>Wed, 15 Jul 2026 10:00:00 +0000</pubDate>
          <description><![CDATA[<p>Scoring context only.</p>]]></description>
        </item></channel></rss>'''
        entries = feed_entries(payload, SOURCE, NOW)
        self.assertEqual(entries[0]["url"], "https://example.com/story")
        self.assertNotIn("summary", entries[0])
        self.assertNotIn("image", entries[0])

    def test_authoritative_official_release_uses_strict_title_gate(self):
        source = {
            **SOURCE,
            "id": "official",
            "name": "Official",
            "quality": 10,
            "primePropertyBias": 0,
            "authoritativeNational": True,
            "defaultLocation": "UK housing market",
            "requiredTitlePatterns": [r"\bUK House Price Index\b"],
            "lane": "official-market",
        }
        admitted = score_article(
            {
                "title": "UK House Price Index: June 2026",
                "url": "https://example.com/hpi",
                "publishedAt": "2026-06-30T10:00:00Z",
                "sourceId": source["id"],
                "source": source["name"],
                "_description": "Official house price statistics.",
            },
            source,
            self.catalog,
            NOW,
        )
        rejected = score_article(
            {
                "title": "Land Registry training course",
                "url": "https://example.com/training",
                "publishedAt": "2026-07-15T10:00:00Z",
                "sourceId": source["id"],
                "source": source["name"],
                "_description": "Training.",
            },
            source,
            self.catalog,
            NOW,
        )
        self.assertIsNotNone(admitted)
        self.assertGreaterEqual(admitted["score"], 45)
        self.assertEqual(admitted["location"], "UK housing market")
        self.assertEqual(admitted["matchType"], "national")
        self.assertIsNone(rejected)

    def test_curated_local_planning_feed_admits_material_building_story(self):
        source = {
            **SOURCE,
            "id": "local-planning",
            "name": "Local Planning",
            "quality": 7,
            "primePropertyBias": 0,
            "publisherGroup": "local-planning",
            "lane": "surrey-local",
            "defaultGeography": "Surrey",
            "curatedPropertyFeed": True,
            "defaultTopics": ["Planning"],
        }
        admitted = score_article(
            {
                "title": "School given green light for sports hall",
                "url": "https://example.com/sports-hall",
                "publishedAt": "2026-07-15T10:00:00Z",
                "sourceId": source["id"],
                "source": source["name"],
                "_description": "Permission was granted to build replacement facilities.",
            },
            source,
            self.catalog,
            NOW,
        )
        self.assertIsNotNone(admitted)
        self.assertGreaterEqual(admitted["score"], 42)
        self.assertEqual(admitted["location"], "Surrey")
        self.assertEqual(admitted["topics"][0], "Planning")


class NewsFeedValidationTests(unittest.TestCase):
    def test_link_only_feed_round_trip(self):
        item = {
            "id": "news-0123456789abcdef0123",
            "title": "Planning decision in Surrey",
            "url": "https://example.com/story",
            "sourceId": "example",
            "source": "Example",
            "sourceCategory": "official",
            "rightsMode": "link-only",
            "publishedAt": "2026-07-15T10:00:00Z",
            "score": 82,
            "scoringVersion": SCORING_VERSION,
            "scoreBase": 77,
            "location": "Surrey",
            "matchType": "county",
            "topics": ["Planning"],
            "reason": "Matches Surrey · Planning",
            "publisherGroup": "example",
            "lane": "surrey-local",
        }
        metadata = {
            "schemaVersion": 1,
            "scoringVersion": SCORING_VERSION,
            "generatedAt": "2026-07-15T12:00:00Z",
            "lastCheckedAt": "2026-07-15T12:00:00Z",
            "newestPublishedAt": "2026-07-15T10:00:00Z",
            "articleCount": 1,
            "sourceDiagnostics": [],
            "sourceErrors": [],
            "rightsMode": "link-only",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "news-feed.js"
            write_feed(path, [item], metadata)
            items, parsed_metadata = validate(path, None)
        self.assertEqual(items[0]["score"], 82)
        self.assertEqual(parsed_metadata["rightsMode"], "link-only")

    def test_source_registry_is_explicitly_link_only(self):
        manifest = json.loads((ROOT / "config/news-sources.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(manifest["sources"]), 12)
        self.assertEqual(manifest["schemaVersion"], 2)
        self.assertTrue(
            all(source["rights"]["mode"] == "link-only" for source in manifest["sources"])
        )

    def test_surrey_local_source_uses_a_scoped_threshold_override(self):
        manifest = json.loads((ROOT / "config/news-sources.json").read_text(encoding="utf-8"))
        source = next(
            item
            for item in manifest["sources"]
            if item["id"] == "epsom-ewell-times-planning"
        )
        self.assertEqual(source["defaultGeography"], "Surrey")
        self.assertEqual(source["publicationMode"], "live")
        self.assertEqual(source["rights"]["collectionStatus"], "approved")
        self.assertLess(source["minimumScore"], 45)

    def test_restricted_sources_are_registered_but_not_collectable(self):
        manifest = json.loads((ROOT / "config/news-sources.json").read_text(encoding="utf-8"))
        by_id = {source["id"]: source for source in manifest["sources"]}
        for source_id in (
            "surrey-live",
            "prime-resi",
            "estate-agent-today",
            "property-industry-eye",
            "bbc-business",
        ):
            self.assertEqual(by_id[source_id]["publicationMode"], "disabled")
            self.assertNotEqual(
                by_id[source_id]["rights"]["collectionStatus"],
                "approved",
            )


if __name__ == "__main__":
    unittest.main()
