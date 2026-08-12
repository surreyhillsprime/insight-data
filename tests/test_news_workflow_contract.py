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
        cls.daily_workflow = (
            ROOT / ".github" / "workflows" / "daily-intelligence.yml"
        ).read_text(encoding="utf-8")

    def test_cadence_and_concurrency_are_news_specific(self):
        self.assertIn("cron: '0 6,18 * * *'", self.workflow)
        self.assertIn('timezone: "Europe/London"', self.workflow)
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

    def test_independent_publishers_retry_after_concurrent_main_updates(self):
        github_ref_writers = (
            "news-feed.yml",
            "daily-intelligence.yml",
            "planning-history-feed.yml",
            "sales-history-feed.yml",
            "heritage-listed-buildings.yml",
            "weekly-context.yml",
            "six-week-os-refresh.yml",
        )
        for name in github_ref_writers:
            workflow = (ROOT / ".github" / "workflows" / name).read_text(
                encoding="utf-8"
            )
            self.assertIn("for attempt in 1 2 3; do", workflow)
            self.assertIn('git fetch origin "$GITHUB_REF_NAME"', workflow)
            self.assertIn('git rebase --autostash "origin/$GITHUB_REF_NAME"', workflow)
            self.assertIn('git push origin "HEAD:$GITHUB_REF_NAME"', workflow)
            self.assertIn('if [ "$attempt" -eq 3 ]; then', workflow)

        daily_view = (
            ROOT / ".github" / "workflows" / "daily-insight-view.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("for attempt in 1 2 3; do", daily_view)
        self.assertIn('git fetch origin "$TARGET_BRANCH"', daily_view)
        self.assertIn('git rebase "origin/$TARGET_BRANCH"', daily_view)
        self.assertIn('git push origin "HEAD:$TARGET_BRANCH"', daily_view)

        monthly = (
            ROOT / ".github" / "workflows" / "monthly-property-refresh.yml"
        ).read_text(encoding="utf-8")
        release_retry = monthly[monthly.rindex("for attempt in 1 2 3; do") :]
        self.assertIn('refs/remotes/origin/$RELEASE_BRANCH', release_retry)
        self.assertIn('validate_release', release_retry)
        self.assertIn('git push origin "HEAD:$RELEASE_BRANCH"', release_retry)

    def test_remaining_shared_publishers_revalidate_after_each_rebase(self):
        validators = {
            "planning-history-feed.yml": (
                "python3 scripts/validate_planning_feed.py",
                "python3 scripts/build_property_records.py",
                "python3 scripts/validate_property_records.py",
                "python3 scripts/build_today_feed.py",
                "python3 scripts/validate_today_feed.py",
            ),
            "sales-history-feed.yml": (
                "python3 scripts/validate_sales_history_feed.py",
                "python3 scripts/build_property_records.py",
                "python3 scripts/validate_property_records.py",
                "python3 scripts/build_today_feed.py",
                "python3 scripts/validate_today_feed.py",
            ),
            "heritage-listed-buildings.yml": (
                "tests.test_heritage_audit_reconciliation",
                "python3 scripts/build_heritage_address_ledger.py --check",
                "python3 scripts/enrich_listed_buildings.py --validate-only",
            ),
            "weekly-context.yml": (
                "python3 scripts/check_data_completeness.py",
                "FEED_SCHEMA_VERSION",
            ),
            "six-week-os-refresh.yml": (
                "python3 scripts/check_data_completeness.py",
                "FEED_SCHEMA_VERSION",
            ),
        }
        for name, expected_validators in validators.items():
            workflow = (ROOT / ".github" / "workflows" / name).read_text(
                encoding="utf-8"
            )
            retry = workflow[workflow.rindex("for attempt in 1 2 3; do") :]
            rebase_index = retry.index(
                'git rebase --autostash "origin/$GITHUB_REF_NAME"'
            )
            push_index = retry.index('git push origin "HEAD:$GITHUB_REF_NAME"')
            for validator in expected_validators:
                with self.subTest(workflow=name, validator=validator):
                    validation_index = retry.index(validator)
                    self.assertLess(rebase_index, validation_index)
                    self.assertLess(validation_index, push_index)

    def test_property_feed_publishers_rebuild_and_commit_today_after_rebase(self):
        for name in (
            "planning-history-feed.yml",
            "sales-history-feed.yml",
        ):
            workflow = (ROOT / ".github" / "workflows" / name).read_text(
                encoding="utf-8"
            )
            retry = workflow[workflow.rindex("for attempt in 1 2 3; do") :]
            operations = (
                'git rebase --autostash "origin/$GITHUB_REF_NAME"',
                "python3 scripts/build_property_records.py",
                "python3 scripts/validate_property_records.py",
                "python3 scripts/build_today_feed.py",
                "python3 scripts/validate_today_feed.py",
                "git add outputs/today-feed.js",
                "git commit --amend --no-edit",
                'git push origin "HEAD:$GITHUB_REF_NAME"',
            )
            positions = []
            for operation in operations:
                with self.subTest(workflow=name, operation=operation):
                    positions.append(retry.index(operation))
            self.assertEqual(positions, sorted(positions), name)
            self.assertIn(
                "if ! git diff --cached --quiet; then\n"
                "                git commit --amend --no-edit\n"
                "              fi",
                retry,
                name,
            )

    def test_daily_flood_retry_revalidates_the_rebased_base_feed(self):
        commit_index = self.daily_workflow.index(
            'git commit -m "Update INSIGHT daily flood context"'
        )
        retry_index = self.daily_workflow.index(
            "for attempt in 1 2 3; do", commit_index
        )
        next_step_index = self.daily_workflow.index(
            "- name: Check whether all Today dependencies are release-ready",
            retry_index,
        )
        retry = self.daily_workflow[retry_index:next_step_index]
        rebase_index = retry.index(
            'git rebase --autostash "origin/$GITHUB_REF_NAME"'
        )
        validation_index = retry.index(
            "python3 scripts/check_data_completeness.py --base-only"
        )
        push_index = retry.index('git push origin "HEAD:$GITHUB_REF_NAME"')

        self.assertLess(rebase_index, validation_index)
        self.assertLess(validation_index, push_index)

    def test_long_data_producers_keep_the_full_shared_queue(self):
        shared = (
            "daily-intelligence.yml",
            "data-completeness.yml",
            "heritage-listed-buildings.yml",
            "monthly-property-refresh.yml",
            "planning-history-feed.yml",
            "sales-history-feed.yml",
            "six-week-os-refresh.yml",
            "weekly-context.yml",
        )
        for name in shared:
            workflow = (ROOT / ".github" / "workflows" / name).read_text(
                encoding="utf-8"
            )
            self.assertIn("group: insight-data-refresh", workflow, name)
            self.assertIn("queue: max", workflow, name)
            self.assertIn("cancel-in-progress: false", workflow, name)

        manual = (
            ROOT / ".github" / "workflows" / "monthly-land-registry-sweep.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "uses: ./.github/workflows/monthly-property-refresh.yml",
            manual,
        )
        self.assertNotIn("scripts/sweep_land_registry.py", manual)


if __name__ == "__main__":
    unittest.main()
