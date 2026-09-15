"""Offline contracts for the isolated, non-publishing EPC backfill lane."""

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "monthly-property-refresh.yml"
BACKFILL_IF = "${{ github.event_name == 'workflow_dispatch' && inputs.epc_backfill_only }}"
MONTHLY_IF = "${{ github.event_name != 'workflow_dispatch' || !inputs.epc_backfill_only }}"
PREFLIGHT = (
    "PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts python3 -m unittest "
    "tests.test_epc_backfill_candidate tests.test_epc_backfill_concurrency tests.test_epc_query_client "
    "tests.test_expand_epc_recovery tests.test_epc_backfill_workflow "
    "tests.test_epc_candidate_result tests.test_epc_identity tests.test_publication_contract"
)
PROVIDER_COMMAND = """if [ "$EPC_EXPAND_MISSING" = "true" ]; then
  python3 scripts/expand_epc_recovery.py --output-dir "$RUNNER_TEMP/insight-epc-result"
else
  python3 scripts/backfill_epc_candidate.py --output-dir "$RUNNER_TEMP/insight-epc-result"
fi"""


class EPCBackfillWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")
        jobs = cls.workflow.split("\njobs:\n", 1)[1]
        cls.jobs = dict(re.findall(
            r"^  ([a-z][a-z0-9-]*):\n(.*?)(?=^  [a-z][a-z0-9-]*:|\Z)",
            jobs, re.MULTILINE | re.DOTALL,
        ))
        cls.backfill = cls.jobs["epc-backfill"]
        provider = cls.backfill.split("      - name: Backfill the reviewed EPC candidate and encrypt its private result\n", 1)[1].split("\n      - name:", 1)[0]
        cls.provider_command = textwrap.dedent(provider.split("        run: |\n", 1)[1]).strip()

    def test_exact_typed_optional_dispatch_inputs_and_default_is_monthly(self):
        dispatch = self.workflow.split("  workflow_dispatch:\n", 1)[1].split("  schedule:\n", 1)[0]
        inputs = dict(re.findall(
            r"^      ([a-z][a-z0-9_]*):\n(.*?)(?=^      [a-z][a-z0-9_]*:|\Z)",
            dispatch, re.MULTILINE | re.DOTALL,
        ))
        self.assertEqual(set(inputs), {"epc_backfill_only", "epc_result_public_key",
                                       "epc_expand_missing", "epc_recovery_context_sha256"})
        for name in ("epc_backfill_only", "epc_expand_missing"):
            self.assertIn("        type: boolean\n", inputs[name])
            self.assertIn("        default: false\n", inputs[name])
        for name in ("epc_result_public_key", "epc_recovery_context_sha256"):
            self.assertIn("        type: string\n", inputs[name])
            self.assertIn('        default: ""\n', inputs[name])
        for definition in inputs.values():
            self.assertIn("        required: false\n", definition)
        self.assertIn("  workflow_call:\n  workflow_dispatch:\n", self.workflow)
        self.assertIn('    - cron: "0 6 1 * *"\n', self.workflow)

    def test_backfill_is_explicit_dispatch_only_and_monthly_conditions_are_complements(self):
        self.assertIn(f"    if: {BACKFILL_IF}\n", self.backfill)
        self.assertIn(f"    if: {MONTHLY_IF}\n", self.jobs["monthly-property-refresh"])
        # Exercise the two supported predicates for manual/default/called runs.
        # Exact expressions above keep these cases tied to the workflow syntax.
        for event in ("workflow_dispatch", "schedule", "workflow_call", "push"):
            for enabled in (False, True, None):
                with self.subTest(event=event, enabled=enabled):
                    backfill = event == "workflow_dispatch" and bool(enabled)
                    monthly = event != "workflow_dispatch" or not enabled
                    self.assertNotEqual(backfill, monthly)
                    self.assertEqual(backfill, event == "workflow_dispatch" and enabled is True)

    def test_all_existing_mutating_jobs_remain_downstream_of_the_disabled_root(self):
        pipeline = (
            "monthly-property-refresh", "refresh-epc", "verify-epc-complete",
            "refresh-monthly-context", "align-expanded-context",
            "reconcile-inspire-coverage", "refresh-sales-history", "materialise-today",
        )
        self.assertEqual(set(self.jobs), set(pipeline) | {"epc-backfill"})
        for predecessor, dependent in zip(pipeline, pipeline[1:]):
            with self.subTest(job=dependent):
                self.assertEqual(
                    re.findall(r"^    needs: (.+)$", self.jobs[dependent], re.MULTILINE),
                    [predecessor],
                )
                self.assertNotRegex(self.jobs[dependent], r"(?m)^    if:")
        self.assertNotRegex(self.backfill, r"(?m)^    needs:")

    def test_default_shared_queue_and_backfill_queue_are_separate(self):
        self.assertIn(
            "  group: insight-data-refresh${{ github.event_name == 'workflow_dispatch' "
            "&& inputs.epc_backfill_only && '-epc-backfill' || '' }}\n",
            self.workflow,
        )
        self.assertIn("  queue: max\n  cancel-in-progress: false\n", self.workflow)

    def test_backfill_token_is_read_only_and_checkout_is_exact_without_credentials(self):
        self.assertIn("permissions:\n  contents: write\n", self.workflow)
        self.assertIn("    permissions:\n      contents: read\n", self.backfill)
        permissions = self.backfill.split("    permissions:\n", 1)[1].split("    runs-on:", 1)[0]
        self.assertEqual(permissions, "      contents: read\n")
        self.assertEqual(self.backfill.count("uses: actions/checkout@v4"), 1)
        self.assertIn("          ref: ${{ github.sha }}\n", self.backfill)
        self.assertIn("          persist-credentials: false\n", self.backfill)
        self.assertIn("    timeout-minutes: 90\n", self.backfill)
        self.assertNotRegex(self.backfill, r"\bgit (push|commit|add|checkout|fetch)\b")
        self.assertNotIn("STAGING_BRANCH", self.backfill)
        self.assertNotIn("RELEASE_BRANCH", self.backfill)

    def test_sensitive_and_user_controlled_inputs_are_environment_only(self):
        self.assertIn("          EPC_BEARER_TOKEN: ${{ secrets.EPC_BEARER_TOKEN }}\n", self.backfill)
        self.assertIn("          EPC_RESULT_PUBLIC_KEY: ${{ inputs.epc_result_public_key }}\n", self.backfill)
        self.assertIn("          EPC_EXPAND_MISSING: ${{ inputs.epc_expand_missing }}\n", self.backfill)
        self.assertIn("          EPC_RECOVERY_CONTEXT_SHA256: ${{ inputs.epc_recovery_context_sha256 }}\n", self.backfill)
        self.assertIn("          EPC_RECOVERY_CONTEXT_B64: ${{ secrets.EPC_RECOVERY_CONTEXT_B64 }}\n", self.backfill)
        self.assertEqual(re.findall(r"^        run: (.+)$", self.backfill, re.MULTILINE), [PREFLIGHT, "|"])
        self.assertEqual(self.provider_command, PROVIDER_COMMAND)
        commands = [PREFLIGHT, self.provider_command]
        for command in commands:
            self.assertNotIn("${{", command)
            self.assertNotIn("EPC_BEARER_TOKEN", command)
            self.assertNotIn("EPC_RESULT_PUBLIC_KEY", command)
            self.assertNotIn("EPC_RECOVERY_CONTEXT_SHA256", command)
            self.assertNotIn("EPC_RECOVERY_CONTEXT_B64", command)
        # No other step receives any private or user-controlled input.
        for expression in ("secrets.EPC_BEARER_TOKEN", "inputs.epc_result_public_key",
                           "inputs.epc_expand_missing", "inputs.epc_recovery_context_sha256",
                           "secrets.EPC_RECOVERY_CONTEXT_B64"):
            self.assertEqual(self.backfill.count(expression), 1)
        for name, job in self.jobs.items():
            if name != "epc-backfill":
                self.assertNotIn("epc_expand_missing", job)
                self.assertNotIn("EPC_RECOVERY_CONTEXT", job)

    def test_runner_preflight_passes_before_provider_step_and_receives_no_credentials(self):
        preflight = self.backfill.index("      - name: Verify runner cryptography and EPC contracts before provider access\n")
        provider = self.backfill.index("      - name: Backfill the reviewed EPC candidate and encrypt its private result\n")
        self.assertLess(preflight, provider)
        section = self.backfill[preflight:provider]
        self.assertIn("        run: " + PREFLIGHT + "\n", section)
        self.assertNotIn("env:", section)
        self.assertNotIn("continue-on-error:", section)
        self.assertNotIn("if:", section)
        provider_section = self.backfill[provider:].split("\n      - name:", 1)[0]
        self.assertNotIn("if:", provider_section)
        self.assertNotIn("continue-on-error:", provider_section)

    def test_artifact_allowlist_excludes_plaintext_cache_feed_and_key_material(self):
        self.assertEqual(self.backfill.count("uses: actions/upload-artifact@v4"), 1)
        upload = self.backfill.split("      - name: Retain only the encrypted result and safe receipt\n", 1)[1]
        self.assertIn("        if: always()\n", upload)
        self.assertIn("          name: epc-backfill-${{ github.run_id }}\n", upload)
        paths = upload.split("          path: |\n", 1)[1].split("          retention-days:", 1)[0]
        self.assertEqual([line.strip() for line in paths.splitlines() if line.strip()], [
            "${{ runner.temp }}/insight-epc-result/*.enc",
            "${{ runner.temp }}/insight-epc-result/*.key",
            "${{ runner.temp }}/insight-epc-result/receipt.json",
        ])
        self.assertIn("          retention-days: 3\n", upload)
        self.assertIn("          if-no-files-found: error\n", upload)
        self.assertIn("          overwrite: false\n", upload)
        for forbidden in ("work/", "outputs/", "*.json", "*.pem", "**", "*.zip", "*.tar"):
            self.assertNotIn(forbidden, paths)

    def test_shell_runs_exact_selected_script_without_executing_or_splitting_inputs(self):
        command = self.provider_command
        with tempfile.TemporaryDirectory(prefix="insight-epc-workflow-") as directory:
            temporary = Path(directory)
            executable = temporary / "python3"
            captured = temporary / "captured.json"
            injected = temporary / "unexpected-execution"
            # The stub records arguments/environment; no script or provider runs.
            executable.write_text(
                f"#!{sys.executable}\n"
                "import json, os, pathlib, sys\n"
                "pathlib.Path(os.environ['TEST_CAPTURE']).write_text(json.dumps({"
                "'arguments': sys.argv[1:], 'key': os.environ['EPC_RESULT_PUBLIC_KEY'],"
                "'token': os.environ['EPC_BEARER_TOKEN'],"
                "'context': os.environ['EPC_RECOVERY_CONTEXT_B64'],"
                "'contextPin': os.environ['EPC_RECOVERY_CONTEXT_SHA256']}))\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            public_key = (
                "-----BEGIN PUBLIC KEY-----\n"
                f'$(touch "{injected}")\n"; touch "{injected}"; #\n'
                "-----END PUBLIC KEY-----"
            )
            runner_temp = str(temporary / "runner temporary folder")
            environment = {
                **os.environ,
                "PATH": str(temporary) + os.pathsep + os.defpath,
                "TEST_CAPTURE": str(captured),
                "RUNNER_TEMP": runner_temp,
                "EPC_RESULT_PUBLIC_KEY": public_key,
                "EPC_BEARER_TOKEN": "synthetic-offline-token",
                "EPC_RECOVERY_CONTEXT_B64": f'$(touch "{injected}"); synthetic-context',
                "EPC_RECOVERY_CONTEXT_SHA256": f'"; touch "{injected}"; #',
            }
            for expanded in ("false", "true", "", f'$(touch "{injected}")'):
                with self.subTest(expanded=expanded):
                    run = subprocess.run(["/bin/bash", "-eu", "-c", command],
                                         env={**environment, "EPC_EXPAND_MISSING": expanded}, check=True,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    result = json.loads(captured.read_text(encoding="utf-8"))
                    self.assertEqual(result["arguments"], [
                        "scripts/expand_epc_recovery.py" if expanded == "true" else "scripts/backfill_epc_candidate.py",
                        "--output-dir", runner_temp + "/insight-epc-result",
                    ])
                    self.assertEqual(result["key"], public_key)
                    self.assertEqual(result["token"], "synthetic-offline-token")
                    self.assertEqual(result["context"], environment["EPC_RECOVERY_CONTEXT_B64"])
                    self.assertEqual(result["contextPin"], environment["EPC_RECOVERY_CONTEXT_SHA256"])
                    self.assertEqual(run.stdout + run.stderr, "")
                    self.assertFalse(injected.exists())

    def test_nonzero_selected_script_does_not_fall_back_to_another_producer(self):
        with tempfile.TemporaryDirectory(prefix="insight-epc-workflow-exit-") as directory:
            temporary = Path(directory)
            executable = temporary / "python3"
            captured = temporary / "called.jsonl"
            executable.write_text(
                f"#!{sys.executable}\n"
                "import json, os, pathlib, sys\n"
                "with pathlib.Path(os.environ['TEST_CAPTURE']).open('a') as f:\n"
                " f.write(json.dumps(sys.argv[1:])+'\\n')\n"
                "raise SystemExit(2)\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            for enabled in ("false", "true"):
                captured.unlink(missing_ok=True)
                run = subprocess.run(["/bin/bash", "-eu", "-c", self.provider_command],
                                     env={**os.environ, "PATH": str(temporary) + os.pathsep + os.defpath,
                                          "TEST_CAPTURE": str(captured), "RUNNER_TEMP": str(temporary),
                                          "EPC_EXPAND_MISSING": enabled},
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                self.assertEqual(run.returncode, 2)
                calls = [json.loads(line) for line in captured.read_text().splitlines()]
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][0], "scripts/expand_epc_recovery.py" if enabled == "true"
                                 else "scripts/backfill_epc_candidate.py")


if __name__ == "__main__":
    unittest.main()
