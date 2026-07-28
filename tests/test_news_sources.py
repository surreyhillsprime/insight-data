import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from news_sources import (  # noqa: E402
    DEFAULT_REGISTRY,
    DEFAULT_SCHEMA,
    NewsSourceValidationError,
    load_registry,
    title_is_allowed,
    validate_registry,
)


class NewsSourceRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(DEFAULT_SCHEMA.read_text(encoding="utf-8"))
        cls.registry = json.loads(DEFAULT_REGISTRY.read_text(encoding="utf-8"))

    def changed_registry(self):
        return copy.deepcopy(self.registry)

    def test_checked_in_registry_passes_schema_and_policy(self):
        loaded = load_registry(DEFAULT_REGISTRY)
        self.assertEqual(loaded["schemaVersion"], 2)
        self.assertTrue(any(source["publicationMode"] == "live" for source in loaded["sources"]))

    def test_duplicate_source_ids_fail(self):
        registry = self.changed_registry()
        registry["sources"][1]["id"] = registry["sources"][0]["id"]
        with self.assertRaisesRegex(ValueError, "duplicate source id"):
            validate_registry(registry, self.schema)

    def test_live_source_requires_collection_and_publication_approval(self):
        registry = self.changed_registry()
        live = next(source for source in registry["sources"] if source["publicationMode"] == "live")
        live["rights"]["publicationStatus"] = "permission-required"
        with self.assertRaisesRegex(ValueError, "lacks approved publication rights"):
            validate_registry(registry, self.schema)

        registry = self.changed_registry()
        live = next(source for source in registry["sources"] if source["publicationMode"] == "live")
        live["rights"]["collectionStatus"] = "permission-required"
        with self.assertRaisesRegex(ValueError, "lacks approved automated collection rights"):
            validate_registry(registry, self.schema)

    def test_shadow_source_still_requires_collection_approval(self):
        registry = self.changed_registry()
        source = registry["sources"][0]
        source["publicationMode"] = "shadow"
        source["rights"]["collectionStatus"] = "permission-required"
        source["rights"]["publicationStatus"] = "permission-required"
        with self.assertRaisesRegex(ValueError, "lacks approved automated collection rights"):
            validate_registry(registry, self.schema)

    def test_non_https_and_invalid_article_hosts_fail(self):
        registry = self.changed_registry()
        registry["sources"][0]["url"] = "http://epsomandewelltimes.com/feed/"
        with self.assertRaisesRegex(ValueError, "required pattern|HTTPS"):
            validate_registry(registry, self.schema)

        registry = self.changed_registry()
        registry["sources"][0]["articleHosts"] = ["localhost"]
        with self.assertRaisesRegex(ValueError, "valid public hostname|local hosts"):
            validate_registry(registry, self.schema)

    def test_invalid_regex_fails_at_load_boundary(self):
        registry = self.changed_registry()
        registry["sources"][0]["requiredTitlePatterns"] = ["("]
        with self.assertRaisesRegex(ValueError, "invalid regular expression"):
            validate_registry(registry, self.schema)

    def test_adapter_allowlist_and_live_fetchability_are_fail_closed(self):
        registry = self.changed_registry()
        registry["sources"][0]["adapter"] = "html-scraper"
        with self.assertRaisesRegex(ValueError, "allowed enum"):
            validate_registry(registry, self.schema)

        registry = self.changed_registry()
        registry["sources"][0]["adapter"] = "publisher-feed-required"
        with self.assertRaisesRegex(ValueError, "requires a fetchable"):
            validate_registry(registry, self.schema)

    def test_authoritative_override_is_restricted_to_licensed_official_sources(self):
        registry = self.changed_registry()
        source = registry["sources"][0]
        source["authoritativeNational"] = True
        with self.assertRaisesRegex(ValueError, "authoritativeNational is restricted"):
            validate_registry(registry, self.schema)

    def test_title_gate_blocks_before_required_patterns(self):
        source = {
            "requiredTitlePatterns": [r"\b(housing|planning)\b"],
            "blockedTitlePatterns": [r"\btelecom kiosk\b"],
        }
        self.assertTrue(title_is_allowed(source, "Housing and planning decision"))
        self.assertFalse(title_is_allowed(source, "Planning decision for a telecom kiosk"))
        self.assertFalse(title_is_allowed(source, "Community festival announced"))

    def test_load_registry_wraps_json_errors_as_value_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sources.json"
            path.write_text("{not-json", encoding="utf-8")
            with self.assertRaises(NewsSourceValidationError):
                load_registry(path)


if __name__ == "__main__":
    unittest.main()
