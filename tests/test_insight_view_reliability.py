import copy
import json
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from collect_insight_view import collect_snapshot, html_text, _official_url
from insight_view import POLICY_SOURCE_ID, build_insight_view
from insight_view_policy import announcement_rate, parse_official_dates, refresh_schedule
from check_insight_view_freshness import FAST_SCHEDULE, freshness_issues, refresh_mode


CALENDAR = (ROOT / "tests/fixtures/insight-view-mpc-calendar.html").read_bytes()


def announcement(day="17 September 2026", rate="3.50", action="reduce"):
    return (f'<div class="published-date">Published on {day}</div>'
            '<p>At its meeting ending on 16 September 2026, the Committee '
            f'voted by a majority of 6&ndash;3 to {action} Bank Rate '
            f'by 0.25 percentage points, to {rate}%. '
            'Three members voted to increase Bank Rate to 4%.</p>'
            '<time itemprop="datePublished">2025-01-01</time>').encode()


def no_network(_url):
    raise AssertionError("Unexpected network use")


class InsightViewReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = json.loads((ROOT / "tests/fixtures/insight-view-snapshot.json").read_text())
        self.now = datetime(2026, 9, 17, 11, 5, tzinfo=timezone.utc)

    def collect(self, **overrides):
        args = dict(now=self.now, calendar_html=CALENDAR,
                    policy_rate_csv=b"DATE,IUDBEDR\n16 Sep 2026,3.75\n17 Sep 2026,3.50\n",
                    vote_html=announcement(), policy_only=True, fetcher=no_network)
        args.update(overrides)
        return collect_snapshot(copy.deepcopy(self.snapshot), **args)

    def view(self, snapshot, now=None):
        now = now or self.now
        from zoneinfo import ZoneInfo
        return build_insight_view(snapshot, news_items=[],
                                 briefing_date=now.astimezone(ZoneInfo("Europe/London")).date(),
                                 generated_at=now.isoformat())

    def assert_retained_policy(self, refreshed):
        for field in ("bankRate", "observationDate", "latestVote"):
            self.assertEqual(refreshed["policy"][field], self.snapshot["policy"][field])
        self.assertIn(POLICY_SOURCE_ID, refreshed["collectionStatus"]["staleSources"])

    def test_new_cut_admits_only_matching_dated_rate_and_publication(self):
        refreshed = self.collect()
        self.assertEqual(refreshed["policy"]["bankRate"], 3.5)
        self.assertEqual(refreshed["policy"]["observationDate"], "2026-09-17")
        self.assertEqual(refreshed["policy"]["latestVote"]["outcome"], "cut")
        self.assertEqual(refreshed["policy"]["nextDecisionDate"], "2026-11-05")
        self.assertEqual(freshness_issues(refreshed, self.view(refreshed), self.now), [])

    def test_lagging_or_mismatched_successful_rate_is_not_a_current_policy(self):
        for csv in (b"DATE,IUDBEDR\n16 Sep 2026,3.75\n",
                    b"DATE,IUDBEDR\n17 Sep 2026,3.75\n",
                    b"DATE,IUDBEDR\n18 Sep 2026,3.50\n",
                    b"DATE,IUDBEDR\n29 Jul 2026,3.50\n"):
            with self.subTest(csv=csv):
                self.assert_retained_policy(self.collect(policy_rate_csv=csv))

    def test_old_successful_announcement_is_pending_after_noon_and_the_next_day(self):
        old = announcement("30 July 2026", "3.75", "maintain")
        for now in (datetime(2026, 9, 17, 11, 0, tzinfo=timezone.utc),
                    datetime(2026, 9, 18, 8, 17, tzinfo=timezone.utc)):
            with self.subTest(now=now):
                refreshed = self.collect(now=now, vote_html=old,
                                         policy_rate_csv=b"DATE,IUDBEDR\n16 Sep 2026,3.75\n")
                self.assert_retained_policy(refreshed)
                view = self.view(refreshed, now)
                self.assertIn("latest scheduled MPC decision has not been coherently observed",
                              freshness_issues(refreshed, view, now))
                self.assertEqual(refresh_mode(refreshed, view, now, "schedule", "17 * * * *"), "all")
        self.assertEqual(refreshed["policy"]["nextDecisionDate"], "2026-11-05")

    def test_before_noon_prior_result_is_valid_but_noon_reopens_expected_decision(self):
        old = announcement("30 July 2026", "3.75", "maintain")
        now = datetime(2026, 9, 17, 10, 59, tzinfo=timezone.utc)
        refreshed = self.collect(now=now, vote_html=old,
                                 policy_rate_csv=b"DATE,IUDBEDR\n16 Sep 2026,3.75\n")
        self.assertEqual(refreshed["collectionStatus"]["staleSources"], [])
        view = self.view(refreshed, now)
        self.assertEqual(refresh_mode(refreshed, view, now, "schedule", FAST_SCHEDULE), "skip")
        self.assertEqual(refresh_mode(refreshed, view, self.now, "schedule", FAST_SCHEDULE), "mpc")

    def test_calendar_failure_retains_evidenced_future_dates_and_retries(self):
        def offline(_url):
            raise OSError("offline fixture")
        refreshed = self.collect(calendar_html=None, fetcher=offline)
        self.assertEqual(refreshed["policy"]["schedule"], self.snapshot["policy"]["schedule"])
        self.assertEqual(refreshed["policy"]["nextDecisionDate"], "2026-11-05")
        self.assertIn(POLICY_SOURCE_ID, refreshed["collectionStatus"]["staleSources"])
        self.assertEqual(refresh_mode(refreshed, self.view(refreshed), self.now,
                                      "schedule", FAST_SCHEDULE), "mpc")

    def test_policy_only_preserves_other_observations_and_existing_failures(self):
        self.snapshot["collectionStatus"]["staleSources"] = ["hm-land-registry-uk-hpi"]
        refreshed = self.collect()
        for section in ("mortgage", "market"):
            self.assertEqual(refreshed[section], self.snapshot[section])
        self.assertEqual(refreshed["collectionStatus"]["staleSources"], ["hm-land-registry-uk-hpi"])

    def test_calendar_is_year_and_table_scoped_and_replaces_postponed_past_date(self):
        parsed = parse_official_dates(CALENDAR)
        self.assertEqual(len(parsed), 16)
        self.assertNotIn(date(2027, 2, 9), parsed)
        moved = CALENDAR.replace(b"Thursday 17 September", b"Thursday 24 September")
        refreshed = refresh_schedule(self.snapshot["policy"]["schedule"], moved, date(2026, 9, 18))
        self.assertNotIn("2026-09-17", refreshed)
        self.assertIn("2026-09-24", refreshed)

    def test_calendar_rejects_duplicates_wrong_weekdays_and_short_horizon(self):
        for invalid in (CALENDAR.replace(b"Thursday 17 September", b"Friday 17 September"),
                        CALENDAR.replace(b"Thursday 5 November", b"Thursday 17 September"),
                        b"<h2>2026 confirmed dates</h2><p>Thursday 17 September</p>"):
            with self.subTest(invalid=invalid[:50]):
                with self.assertRaises(ValueError):
                    parse_official_dates(invalid)
        with self.assertRaisesRegex(ValueError, "future coverage"):
            refresh_schedule(self.snapshot["policy"]["schedule"], CALENDAR, date(2027, 12, 16))

    def test_calendar_extends_automatically_to_a_new_year_without_changing_cron(self):
        days = [date(2028, month, day) for month, day in
                [(2, 3), (3, 16), (5, 4), (6, 22), (8, 3), (9, 21), (11, 2), (12, 14)]]
        new_year = ('<h2>2028 provisional dates</h2><table>' + ''.join(
            f'<tr><td>{day.strftime("%A %-d %B")}</td><td>MPC Summary</td></tr>' for day in days)
                    + '</table>').encode()
        refreshed = refresh_schedule(self.snapshot["policy"]["schedule"], new_year, date(2027, 12, 17))
        self.assertIn("2028-12-14", refreshed)
        self.assertIn("2027-12-16", refreshed)

    def test_truncated_successful_calendar_cannot_cancel_a_pending_decision(self):
        import re
        for partial in (re.sub(rb'<tr><td>Thursday 5 November.*?</tr>', b'', CALENDAR),
                        re.sub(rb'<tr><td>Thursday 17 September.*?</tr>', b'', CALENDAR),
                        CALENDAR[CALENDAR.index(b'<h2>2027 provisional'):]):
            with self.subTest(partial=partial[:40]):
                refreshed = self.collect(calendar_html=partial,
                                         vote_html=announcement("30 July 2026", "3.75", "maintain"),
                                         policy_rate_csv=b"DATE,IUDBEDR\n17 Sep 2026,3.75\n")
                self.assert_retained_policy(refreshed)
                self.assertEqual(refreshed["policy"]["schedule"], self.snapshot["policy"]["schedule"])

    def test_announcement_uses_publication_date_and_majority_rate_only(self):
        payload = announcement()
        self.assertEqual(announcement_rate(payload, date(2026, 9, 17), html_text(payload.decode())), 3.5)
        with self.assertRaisesRegex(ValueError, "due decision"):
            announcement_rate(payload, date(2026, 9, 16), html_text(payload.decode()))
        for bad in (payload.replace(b'published-date', b'other-date'),
                    payload + b'<div class="published-date">Published on 17 September 2026</div>'):
            with self.assertRaises(ValueError):
                announcement_rate(bad, date(2026, 9, 17), html_text(bad.decode()))

    def test_new_london_day_retries_even_when_daily_workflow_was_missed(self):
        refreshed = self.collect()
        view = self.view(refreshed)
        midnight_bst = datetime(2026, 9, 17, 23, 5, tzinfo=timezone.utc)
        self.assertEqual(refresh_mode(refreshed, view, midnight_bst, "schedule", "17 * * * *"), "all")
        self.assertIn("published briefing is not dated today in Europe/London",
                      freshness_issues(refreshed, view, midnight_bst))

    def test_health_uses_fresh_collection_not_unchanged_view_generation_time(self):
        refreshed = self.collect()
        view = self.view(refreshed)
        view["generatedAt"] = "2026-09-17T00:00:00Z"
        self.assertEqual(freshness_issues(refreshed, view, self.now), [])
        refreshed["collectedAt"] = "2026-09-17T00:00:00Z"
        self.assertTrue(any("eight hours" in value for value in freshness_issues(refreshed, view, self.now)))

    def test_official_transport_rejects_external_destinations(self):
        for url in ("https://example.test/", "http://www.bankofengland.co.uk/",
                    "https://user@www.bankofengland.co.uk/", "https://www.bankofengland.co.uk:444/"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    _official_url(url)

    def test_workflow_has_dynamic_polling_recovery_and_post_publication_health(self):
        workflow = (ROOT / ".github/workflows/daily-insight-view.yml").read_text()
        self.assertIn(f'cron: "{FAST_SCHEDULE}"', workflow)
        self.assertIn('cron: "17 * * * *"', workflow)
        self.assertIn('cron: "7 0 * * *"', workflow)
        self.assertIn("--mpc-only", workflow)
        self.assertGreater(workflow.rindex("python3 scripts/check_insight_view_freshness.py"),
                           workflow.index('git push origin "HEAD:$TARGET_BRANCH"'))
        self.assertNotIn("repository_dispatch", workflow)
        self.assertIn("scripts/check_insight_view_freshness.py",
                      (ROOT / ".github/workflows/data-completeness.yml").read_text())


if __name__ == "__main__":
    unittest.main()
