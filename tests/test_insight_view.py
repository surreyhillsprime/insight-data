import copy
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from collect_insight_view import (  # noqa: E402
    collect_snapshot,
    parse_boe_csv,
    parse_hpi_market,
    parse_vote_summary,
)
from insight_view import (  # noqa: E402
    InsightViewValidationError,
    build_insight_view,
    decision_sentence,
    load_snapshot,
    market_direction,
    validate_insight_view,
    write_insight_view,
)
from validate_insight_view import validate as validate_asset  # noqa: E402


class InsightViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot_path = ROOT / "config" / "insight-view-snapshot.json"
        cls.snapshot = load_snapshot(cls.snapshot_path)

    def build(self, briefing_date="2026-07-31", generated_at="2026-07-31T10:10:00Z"):
        return build_insight_view(
            copy.deepcopy(self.snapshot),
            news_items=[
                {
                    "title": "Planning applications submitted to Woking Borough Council",
                    "source": "Woking News & Mail",
                    "sourceId": "woking-news-mail",
                    "publisherGroup": "tindle-surrey",
                    "publishedAt": "2026-07-30T15:04:04Z",
                    "rightsMode": "link-only",
                    "score": 63,
                },
                {
                    "title": "Housing safety case studies published",
                    "source": "Ministry of Housing, Communities and Local Government",
                    "sourceId": "mhclg-official",
                    "publisherGroup": "uk-government",
                    "publishedAt": "2026-07-30T08:37:02Z",
                    "rightsMode": "link-only",
                    "score": 61,
                },
            ],
            briefing_date=briefing_date,
            generated_at=generated_at,
        )

    def test_daily_view_is_deterministic_data_first_and_source_governed(self):
        view = self.build()
        repeated = self.build()

        self.assertEqual(view, repeated)
        self.assertEqual(view["briefingDate"], "2026-07-31")
        self.assertEqual(view["timeZone"], "Europe/London")
        self.assertEqual(view["heading"], "INSIGHT View")
        self.assertEqual(view["category"], "Rates and market")
        self.assertEqual(view["policy"]["bankRate"], 3.75)
        self.assertEqual(view["policy"]["countdownDays"], 48)
        self.assertEqual(view["policy"]["signalValue"], "Hold favoured")
        self.assertEqual(
            view["policy"]["signalDetail"],
            "Latest MPC vote · 6–3 to hold",
        )
        self.assertEqual(view["mortgage"]["rate"], 4.81)
        self.assertEqual(view["mortgage"]["previousRate"], 4.92)
        self.assertEqual(view["mortgage"]["trend"], "down")
        self.assertEqual(view["mortgage"]["seriesId"], "IUMBV34")
        self.assertIn("75% LTV", view["mortgage"]["qualifier"])
        self.assertEqual(view["market"]["ukAveragePrice"], 271295)
        self.assertEqual(view["market"]["ukAnnualChange"], 2.7)
        self.assertEqual(view["market"]["surreyAnnualChange"], -0.1)
        self.assertEqual(len(view["narrative"]), 3)
        self.assertIn("not a forecast", view["narrative"][0])
        self.assertIn("property-level", view["narrative"][1])
        self.assertIn("Relevant Market News updates", view["narrative"][2])
        self.assertIn("Woking News & Mail", view["narrative"][2])
        self.assertIn("link-only context", view["narrative"][2])
        self.assertEqual(
            [source["id"] for source in view["sources"]],
            [
                "bank-of-england-mpc",
                "bank-of-england-iumbv34",
                "hm-land-registry-uk-hpi",
            ],
        )
        self.assertTrue(
            all(
                source["rights"] == "Open Government Licence v3.0"
                for source in view["sources"]
            )
        )
        self.assertRegex(view["fingerprint"], r"^[0-9a-f]{64}$")
        validate_insight_view(view)

    def test_mpc_countdown_copy_covers_today_tomorrow_and_useful_day_count(self):
        decision_day = date(2026, 7, 30)

        self.assertEqual(
            decision_sentence(date(2026, 7, 30), decision_day, "12:00"),
            "The next MPC decision is due today at noon.",
        )
        self.assertEqual(
            decision_sentence(date(2026, 7, 29), decision_day, "12:00"),
            "The next MPC decision is tomorrow at noon.",
        )
        self.assertEqual(
            decision_sentence(date(2026, 7, 26), decision_day, "12:00"),
            "There are 4 days until the next MPC decision at noon.",
        )

    def test_market_direction_copy_distinguishes_slowing_strengthening_and_flat(self):
        self.assertEqual(
            market_direction(2.7, 3.9),
            "annual growth has slowed from 3.9%",
        )
        self.assertEqual(
            market_direction(4.1, 3.9),
            "annual growth has strengthened from 3.9%",
        )
        self.assertEqual(
            market_direction(3.92, 3.9),
            "annual growth is broadly unchanged from 3.9%",
        )

    def test_view_rejects_a_fingerprint_or_countdown_that_does_not_reconcile(self):
        view = self.build()
        bad_fingerprint = copy.deepcopy(view)
        bad_fingerprint["fingerprint"] = "0" * 64
        with self.assertRaisesRegex(
            InsightViewValidationError,
            "fingerprint is invalid",
        ):
            validate_insight_view(bad_fingerprint)

        bad_countdown = copy.deepcopy(view)
        bad_countdown["policy"]["countdownDays"] = 4
        with self.assertRaisesRegex(
            InsightViewValidationError,
            "countdown",
        ):
            validate_insight_view(bad_countdown)

    def test_writer_and_standalone_validator_enforce_the_two_assignment_contract(self):
        view = self.build()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "insight-view.js"
            write_insight_view(path, view)

            parsed, metadata = validate_asset(path)

            self.assertEqual(parsed, view)
            self.assertEqual(metadata["generatorVersion"], "insight-view-1")
            self.assertEqual(metadata["asOf"], view["briefingDate"])
            self.assertEqual(
                metadata["datasetFingerprint"],
                view["fingerprint"],
            )
            self.assertEqual(metadata["sourceCount"], 3)
            self.assertEqual(
                [
                    line.split(" = ", 1)[0]
                    for line in path.read_text(encoding="utf-8").splitlines()
                ],
                ["window.INSIGHT_VIEW", "window.INSIGHT_VIEW_META"],
            )

    def test_official_source_parsers_preserve_metric_qualifiers_and_prior_values(self):
        bank_rate = parse_boe_csv(
            "DATE,IUDBEDR\n27 Jul 2026,3.75\n28 Jul 2026,3.75\n",
            "IUDBEDR",
        )
        mortgage = parse_boe_csv(
            "DATE,IUMBV34\n31 May 2026,4.92\n30 Jun 2026,4.81\n",
            "IUMBV34",
        )
        vote = parse_vote_summary(
            (
                "<p>The Committee voted by a majority of 7–2 to maintain "
                "Bank Rate.</p><p>Two members voted to increase Bank Rate.</p>"
            ),
            date(2026, 6, 18),
        )

        self.assertEqual(bank_rate[-1], (date(2026, 7, 28), 3.75))
        self.assertEqual(
            mortgage[-2:],
            [
                (date(2026, 5, 31), 4.92),
                (date(2026, 6, 30), 4.81),
            ],
        )
        self.assertEqual(
            vote,
            {
                "announcementDate": "2026-06-18",
                "outcome": "hold",
                "for": 7,
                "against": 2,
                "alternative": "raise",
            },
        )

    def test_hpi_parser_requires_aligned_latest_months(self):
        def response(region, latest, prior):
            return {
                "result": {
                    "items": [
                        {"region": region, **latest},
                        {"region": region, **prior},
                    ]
                }
            }

        uk = response(
            "United Kingdom",
            {
                "refMonth": "2026-05",
                "averagePrice": 271295,
                "percentageAnnualChange": 2.7,
                "percentageChange": 0.3,
            },
            {
                "refMonth": "2026-04",
                "averagePrice": 270000,
                "percentageAnnualChange": 3.9,
                "percentageChange": 0.1,
            },
        )
        surrey = response(
            "Surrey",
            {
                "refMonth": "2026-05",
                "averagePrice": 521400,
                "percentageAnnualChange": -0.1,
                "percentageChange": -0.5,
            },
            {
                "refMonth": "2026-04",
                "averagePrice": 522000,
                "percentageAnnualChange": 0.0,
                "percentageChange": 0.0,
            },
        )
        london = response(
            "London",
            {
                "refMonth": "2026-05",
                "averagePrice": 544814,
                "percentageAnnualChange": -3.7,
                "percentageChange": -1.2,
            },
            {
                "refMonth": "2026-04",
                "averagePrice": 550000,
                "percentageAnnualChange": -2.3,
                "percentageChange": -0.4,
            },
        )

        market = parse_hpi_market(
            uk,
            surrey,
            london,
            retrieved_at="2026-07-30T05:00:00Z",
            source_url="https://landregistry.data.gov.uk/example",
        )

        self.assertEqual(market["observationMonth"], "2026-05")
        self.assertEqual(market["ukPreviousAnnualChange"], 3.9)
        self.assertEqual(market["londonPreviousAnnualChange"], -2.3)
        self.assertEqual(market["surreyAnnualChange"], -0.1)
        self.assertIs(market["provisional"], True)

        london["result"]["items"][0]["refMonth"] = "2026-04"
        with self.assertRaisesRegex(ValueError, "do not align"):
            parse_hpi_market(
                uk,
                surrey,
                london,
                retrieved_at="2026-07-30T05:00:00Z",
                source_url="https://landregistry.data.gov.uk/example",
            )

    def test_collection_failure_carries_forward_last_known_good_and_marks_sources(self):
        def failing_fetcher(_url):
            raise OSError("offline fixture")

        refreshed = collect_snapshot(
            copy.deepcopy(self.snapshot),
            now=datetime(2026, 7, 30, 5, 0, tzinfo=timezone.utc),
            fetcher=failing_fetcher,
        )

        self.assertEqual(
            refreshed["policy"]["bankRate"],
            self.snapshot["policy"]["bankRate"],
        )
        self.assertEqual(
            refreshed["mortgage"]["rate"],
            self.snapshot["mortgage"]["rate"],
        )
        self.assertEqual(
            refreshed["market"]["observationMonth"],
            self.snapshot["market"]["observationMonth"],
        )
        self.assertEqual(
            refreshed["collectionStatus"],
            {
                "mode": "last-known-good",
                "staleSources": [
                    "bank-of-england-iumbv34",
                    "bank-of-england-mpc",
                    "hm-land-registry-uk-hpi",
                ],
            },
        )

    def test_post_announcement_collection_closes_the_mpc_event_immediately(self):
        def failing_fetcher(_url):
            raise OSError("offline fixture")

        refreshed = collect_snapshot(
            copy.deepcopy(self.snapshot),
            now=datetime(2026, 7, 30, 11, 5, tzinfo=timezone.utc),
            fetcher=failing_fetcher,
            policy_rate_csv=(
                b"DATE,IUDBEDR\n29 Jul 2026,3.75\n30 Jul 2026,3.75\n"
            ),
            vote_html=(
                b"<p>The Committee voted by a majority of 6\xe2\x80\x933 to maintain "
                b"Bank Rate.</p><p>Three members voted to increase Bank Rate.</p>"
            ),
        )

        self.assertEqual(refreshed["policy"]["observationDate"], "2026-07-30")
        self.assertEqual(
            refreshed["policy"]["latestVote"],
            {
                "announcementDate": "2026-07-30",
                "outcome": "hold",
                "for": 6,
                "against": 3,
                "alternative": "raise",
                "sourceUrl": (
                    "https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/"
                    "2026/july-2026"
                ),
            },
        )
        self.assertEqual(refreshed["policy"]["nextDecisionDate"], "2026-09-17")
        self.assertNotIn(
            "bank-of-england-mpc",
            refreshed["collectionStatus"]["staleSources"],
        )


if __name__ == "__main__":
    unittest.main()
