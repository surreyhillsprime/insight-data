import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures" / "news"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from news_adapters import NewsAdapterError, fetch_source, parse_feed  # noqa: E402
from news_sources import NewsSourceValidationError  # noqa: E402


def source(adapter="rss", publication_mode="live", collection="approved", publication="approved"):
    return {
        "id": f"fixture-{adapter}",
        "name": "Fixture News",
        "url": "https://feeds.example.test/property.xml",
        "adapter": adapter,
        "articleHosts": ["news.example.test"],
        "category": "fixture",
        "publicationMode": publication_mode,
        "rights": {
            "collectionStatus": collection,
            "publicationStatus": publication,
            "mode": "link-only",
        },
    }


class FakeResponse:
    status = 200

    def __init__(self, payload, url="https://feeds.example.test/property.xml"):
        self.payload = payload
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.payload

    def geturl(self):
        return self.url


class NewsAdapterTests(unittest.TestCase):
    def fixture(self, name):
        return (FIXTURES / name).read_bytes()

    def test_rss_is_normalised_to_the_link_only_article_contract(self):
        articles, diagnostics = parse_feed(self.fixture("rss-valid.xml"), source())
        self.assertEqual(diagnostics["entriesSeen"], 1)
        self.assertEqual(diagnostics["articlesAccepted"], 1)
        article = articles[0]
        self.assertEqual(
            set(article),
            {
                "title",
                "url",
                "publishedAt",
                "sourceId",
                "source",
                "sourceCategory",
                "rightsMode",
                "_description",
            },
        )
        self.assertEqual(
            article["url"],
            "https://news.example.test/property/surrey-market?edition=morning",
        )
        self.assertEqual(article["publishedAt"], "2026-07-28T08:30:00Z")
        self.assertEqual(article["_description"], "A factual property description.")

    def test_atom_uses_alternate_link_and_updated_date(self):
        articles, diagnostics = parse_feed(
            self.fixture("atom-valid.xml"),
            source(adapter="atom"),
        )
        self.assertEqual(diagnostics["articlesAccepted"], 1)
        self.assertEqual(
            articles[0]["url"],
            "https://news.example.test/policy/housing-update",
        )
        self.assertEqual(articles[0]["publishedAt"], "2026-07-28T10:00:00Z")

    def test_missing_dates_and_unregistered_domains_are_rejected(self):
        articles, diagnostics = parse_feed(
            self.fixture("rss-rejections.xml"),
            source(),
        )
        self.assertEqual(len(articles), 1)
        self.assertEqual(diagnostics["entriesSeen"], 5)
        self.assertEqual(diagnostics["articlesRejected"], 4)
        self.assertEqual(
            diagnostics["rejectionReasons"],
            {
                "insecure-link": 1,
                "missing-or-invalid-date": 1,
                "missing-title": 1,
                "wrong-domain-link": 1,
            },
        )

    def test_news_sitemap_date_only_is_normalised_to_utc(self):
        articles, diagnostics = parse_feed(
            self.fixture("news-sitemap-valid.xml"),
            source(adapter="news-sitemap"),
        )
        self.assertEqual(diagnostics["articlesAccepted"], 1)
        self.assertEqual(articles[0]["publishedAt"], "2026-07-28T00:00:00Z")

    def test_adapter_root_mismatch_and_entity_declarations_fail(self):
        with self.assertRaisesRegex(NewsAdapterError, "Atom adapter"):
            parse_feed(self.fixture("rss-valid.xml"), source(adapter="atom"))
        with self.assertRaisesRegex(NewsAdapterError, "entity declarations"):
            parse_feed(
                b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x "x">]><rss/>',
                source(),
            )

    def test_disabled_and_unapproved_sources_cannot_be_parsed_or_fetched(self):
        with self.assertRaisesRegex(NewsSourceValidationError, "disabled source"):
            parse_feed(
                self.fixture("rss-valid.xml"),
                source(publication_mode="disabled"),
            )
        with mock.patch("news_adapters.urllib.request.urlopen") as urlopen:
            with self.assertRaisesRegex(NewsSourceValidationError, "collection rights"):
                fetch_source(source(collection="permission-required"))
            urlopen.assert_not_called()

    def test_fetch_retries_transport_failures_and_returns_diagnostics(self):
        payload = self.fixture("rss-valid.xml")
        response = FakeResponse(payload)
        with (
            mock.patch(
                "news_adapters.urllib.request.urlopen",
                side_effect=[urllib.error.URLError("temporary"), response],
            ) as urlopen,
            mock.patch("news_adapters.time.sleep") as sleep,
        ):
            articles, diagnostics = fetch_source(source(), attempts=3)
        self.assertEqual(len(articles), 1)
        self.assertEqual(diagnostics["fetchAttempts"], 2)
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()

    def test_fetch_rejects_redirects_outside_registered_hosts(self):
        response = FakeResponse(
            self.fixture("rss-valid.xml"),
            url="https://redirect.attacker.invalid/feed.xml",
        )
        with mock.patch("news_adapters.urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(NewsAdapterError, "redirected outside"):
                fetch_source(source(), attempts=1)


if __name__ == "__main__":
    unittest.main()
