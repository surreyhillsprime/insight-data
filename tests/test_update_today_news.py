import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from update_today_news import transaction_records, update_today_news


class UpdateTodayNewsTests(unittest.TestCase):
    def test_replaces_stale_news_projection_and_rebuilds_opportunities(self):
        property_id = "property:1 TEST ROAD GUILDFORD GU1 1AA|GU11AA"
        signal = {
            "id": "signal-1",
            "lane": "signals",
            "kind": "epc_observation",
            "rank": 70,
            "fact": "EPC evidence",
            "effectiveDate": "2026-07-20",
            "datePrecision": "day",
            "sourceFamily": "epc",
            "sourceIds": ["epc-1"],
            "evidenceIds": ["epc-1"],
            "evidence": [
                {
                    "evidenceId": "epc-1",
                    "sourceId": "epc-1",
                    "source": "EPC",
                    "effectiveDate": "2026-07-20",
                    "datePrecision": "day",
                }
            ],
            "coverage": {"sourceKey": "epc", "limitations": ["fixture"]},
            "limitations": ["fixture"],
            "property": {
                "propertyId": property_id,
                "address": "1 Test Road, Guildford",
            },
            "propertyIds": [property_id],
        }
        stale = {
            "id": "old-news",
            "kind": "entity_news",
            "lane": "placeChanges",
            "attributes": {"newsId": "old"},
        }
        feed = {
            "schemaVersion": 1,
            "asOf": "2026-07-28",
            "signals": [signal],
            "opportunities": [],
            "placeChanges": [stale],
        }
        metadata = {
            "schemaVersion": 1,
            "generatedAt": "2026-07-28T10:00:00Z",
            "criteria": {"newsMinimumScore": 55},
            "sourceFingerprints": {"propertyRecords": "fixture"},
            "sourceGeneratedAt": {"propertyRecords": "2026-07-28T09:00:00Z"},
        }
        news = [
            {
                "id": "news-0123456789abcdef0123",
                "title": "Guildford housing development approved",
                "url": "https://example.com/guildford",
                "source": "Example",
                "publishedAt": "2026-07-28T11:00:00Z",
                "score": 75,
                "location": "Guildford",
                "matchType": "town",
                "topics": ["Planning"],
                "reason": "Matches Guildford",
                "rightsMode": "link-only",
            }
        ]
        records = transaction_records(
            [
                {
                    "propertyRecordId": property_id,
                    "address": "1 Test Road, Guildford",
                    "town": "Guildford",
                    "district": "Guildford",
                }
            ]
        )
        updated, updated_meta = update_today_news(
            feed,
            metadata,
            news,
            {"generatedAt": "2026-07-28T11:30:00Z"},
            records,
        )
        news_changes = [
            item for item in updated["placeChanges"] if item.get("kind") == "entity_news"
        ]
        self.assertEqual(len(news_changes), 1)
        self.assertEqual(news_changes[0]["attributes"]["newsId"], news[0]["id"])
        self.assertEqual(len(updated["opportunities"]), 1)
        self.assertEqual(updated_meta["sourceGeneratedAt"]["news"], "2026-07-28T11:30:00Z")

    def test_county_news_stays_out_of_today(self):
        feed = {
            "schemaVersion": 1,
            "asOf": "2026-07-28",
            "signals": [],
            "opportunities": [],
            "placeChanges": [],
        }
        metadata = {
            "schemaVersion": 1,
            "generatedAt": "2026-07-28T10:00:00Z",
            "criteria": {"newsMinimumScore": 55},
        }
        news = [
            {
                "id": "news-0123456789abcdef0123",
                "title": "Surrey housing policy",
                "url": "https://example.com/surrey",
                "source": "Example",
                "publishedAt": "2026-07-28T11:00:00Z",
                "score": 90,
                "location": "Surrey",
                "matchType": "county",
                "topics": ["Policy"],
                "reason": "Matches Surrey",
                "rightsMode": "link-only",
            }
        ]
        updated, _metadata = update_today_news(
            feed,
            metadata,
            news,
            {"generatedAt": "2026-07-28T11:30:00Z"},
            {},
        )
        self.assertEqual(updated["placeChanges"], [])


if __name__ == "__main__":
    unittest.main()
