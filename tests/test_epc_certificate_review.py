"""Offline approval, once-only transport and encrypted review-lane contracts."""

import base64
import copy
import gzip
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fetch_epc_certificate_review as review
import epc_candidate_result as crypto

REFS = [f"{index:04d}-1111-2222-3333-4444" for index in range(1, 4)]
PROVENANCE = {"producerCommit": "c" * 40, "githubRunId": "123456", "githubRunAttempt": "1"}


def manifest(refs=None, recipient="a" * 64):
    value = {"schema": review.REQUEST_SCHEMA, "producerCommit": PROVENANCE["producerCommit"],
             "certificateReferences": REFS[:2] if refs is None else refs,
             **{field: "b" * 64 for field in review.PIN_FIELDS}}
    value["recipientPublicKeySha256"] = recipient
    return value


def encoded(value):
    raw = value if isinstance(value, bytes) else review.canonical(value)
    return base64.b64encode(gzip.compress(raw)).decode("ascii")


def certificate(reference):
    return {"certificate_number": reference, "address_line_1": "SYNTHETIC WILLOW HOUSE",
            "post_town": "GUILDFORD", "postcode": "GU1 1AA", "lodgement_date": "2026-09-01",
            "current_energy_rating": "C", "total_floor_area": {"quantity": "sq m", "value": 208},
            "assessor_email": "private@example.invalid", "contact_details": {"token": "must-remove"}}


class FakeOpener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if hasattr(outcome, "read"):
            return outcome
        return io.BytesIO(json.dumps({"data": outcome}).encode())


class BrokenRead(io.BytesIO):
    def read(self, size=-1):
        raise OSError("private response read details")


class CertificateReviewTests(unittest.TestCase):
    def load(self, value, **overrides):
        args = {"encoded": encoded(value), "expected_sha256": review.digest(value),
                "producer_commit": PROVENANCE["producerCommit"], "recipient_sha256": "a" * 64}
        args.update(overrides)
        return review.load_manifest(**args)

    def fetch(self, outcomes, refs=None, **kwargs):
        opener = FakeOpener(outcomes)
        payload, receipt = review.fetch_review(manifest(refs), "synthetic-private-credential",
                                               PROVENANCE, opener=opener, spacing=0, **kwargs)
        return payload, receipt, opener

    def test_manifest_exact_approval_source_recipient_and_external_hash_bindings(self):
        value = manifest()
        self.assertEqual(self.load(value), value)
        for field in ("expected_sha256", "producer_commit", "recipient_sha256"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.load(value, **{field: "0" * (40 if field == "producer_commit" else 64)})
        for field in ("schema", *review.PIN_FIELDS):
            changed = copy.deepcopy(value)
            changed[field] = "invalid"
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.load(changed)
        for change in ({**value, "privateExtra": "not accepted"},
                       {key: item for key, item in value.items() if key != "approvalDecisionsSha256"}):
            with self.assertRaises(ValueError):
                self.load(change)

    def test_manifest_rejects_duplicate_keys_malformed_and_compressed_oversize(self):
        value = manifest()
        for raw in (b'{"schema":1,"schema":2}', b'{"schema":NaN}', b"[", b"x" * 65537):
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                self.load(value, encoded=encoded(raw))
        for transport in ("invalid!", "x" * 48001, base64.b64encode(b"not gzip").decode()):
            with self.assertRaises(ValueError):
                self.load(value, encoded=transport)

    def test_reference_membership_count_format_order_and_ascii_are_strict(self):
        cases = [[], REFS[:1] * 2, REFS[::-1], ["not-a-reference"], [123],
                 [REFS[0].replace("1", "١")], [REFS[0] + "&postcode=GU1"],
                 [f"{index:04d}-1111-2222-3333-4444" for index in range(101)]]
        for refs in cases:
            with self.subTest(refs=refs[:2]), self.assertRaises(ValueError):
                self.load(manifest(refs))

    def test_success_uses_only_approved_full_endpoint_and_minimized_measurement(self):
        with patch.object(review.base.RegisterClient, "search", side_effect=AssertionError("No search")), \
                patch.object(review.base.RegisterClient, "search_query", side_effect=AssertionError("No search")):
            payload, receipt, opener = self.fetch([certificate(ref) for ref in REFS[:2]])
        self.assertEqual((receipt["status"], receipt["httpRequests"], receipt["fetchedCount"]),
                         ("complete", 2, 2))
        for index, (request, timeout) in enumerate(opener.requests):
            self.assertEqual(request.full_url, review.base.API_BASE + "/api/certificate?certificate_number=" + REFS[index])
            self.assertEqual(timeout, 15)
            evidence = payload["records"][index]["evidence"]
            self.assertEqual(evidence["total_floor_area"], {"quantity": "sq m", "value": 208})
            self.assertEqual(review.base.epc.floor_area_from_certificate(evidence), 208)
            self.assertNotIn("assessor_email", evidence)
            self.assertNotIn("contact_details", evidence)
            self.assertEqual(payload["records"][index]["evidenceSha256"], review.digest(evidence))
        self.assertFalse(payload["candidateUsable"])

    def test_omitted_full_identifier_is_not_invented_and_conflicting_identifier_fails(self):
        missing = certificate(REFS[0])
        del missing["certificate_number"]
        payload, receipt, _ = self.fetch([missing, certificate(REFS[2])])
        self.assertNotIn("certificate_number", payload["records"][0]["evidence"])
        self.assertEqual(payload["records"][1]["code"], "certificate_number_conflict")
        self.assertEqual((receipt["fetchedCount"], receipt["failedCount"]), (1, 1))

    def test_transient_and_read_failures_are_once_only_and_symbolic(self):
        failures = [urllib.error.HTTPError("private-url", code, "private body", {}, None)
                    for code in (429, 503, 404)]
        failures += [urllib.error.URLError("private host"), BrokenRead(), ValueError("private unexpected error")]
        expected = ["register_http_429", "register_http_503", "register_http_404",
                    "register_connection_failed", "register_connection_failed", "invalid_register_json"]
        for failure, code in zip(failures, expected):
            with self.subTest(code=code):
                payload, receipt, opener = self.fetch([failure], refs=REFS[:1])
                self.assertEqual(len(opener.requests), 1)
                self.assertEqual(receipt["httpRequests"], 1)
                self.assertEqual(payload["records"][0]["status"], "failed")
                self.assertEqual(payload["records"][0]["code"], code)
                self.assertIsNone(payload["records"][0]["retrievedAt"])
                self.assertNotIn("private", review.canonical(payload).decode())

    def test_authentication_failure_stops_remaining_requests_without_check_dates(self):
        for code in (401, 403):
            failure = urllib.error.HTTPError("private-url", code, "private body", {}, None)
            payload, receipt, opener = self.fetch([failure], refs=REFS)
            self.assertEqual((len(opener.requests), receipt["failedCount"], receipt["notAttemptedCount"]), (1, 1, 2))
            for record in payload["records"][1:]:
                self.assertEqual(record["status"], "not_attempted")
                self.assertEqual(record["code"], "register_authentication_failed")
                self.assertIsNone(record["attemptedAt"])
                self.assertIsNone(record["retrievedAt"])

    def test_expired_budget_makes_no_request_or_completed_check(self):
        payload, receipt, opener = self.fetch([], max_seconds=-1)
        self.assertEqual(opener.requests, [])
        self.assertEqual((receipt["httpRequests"], receipt["notAttemptedCount"]), (0, 2))
        self.assertTrue(all(record["code"] == "register_budget_reached" and record["attemptedAt"] is None
                            for record in payload["records"]))

    def test_transport_rejects_foreign_endpoint_reference_and_duplicates(self):
        opener = FakeOpener([certificate(REFS[0])])
        transport = review.SingleAttemptTransport(REFS[:1], opener)
        valid = review.base.API_BASE + "/api/certificate?certificate_number=" + REFS[0]
        urls = [valid.replace(review.base.API_BASE, "https://example.invalid"),
                valid.replace("/api/certificate", "/api/domestic/search"),
                valid.replace(REFS[0], REFS[1]), valid + "&postcode=GU1", valid + "&certificate_number=" + REFS[0]]
        for url in urls:
            with self.assertRaises(RuntimeError):
                with transport.open(urllib.request.Request(url), 15):
                    self.fail("Invalid endpoint must never open")
        self.assertEqual(opener.requests, [])
        with transport.open(urllib.request.Request(valid), 15) as response:
            response.read()
        with self.assertRaisesRegex(RuntimeError, "review_reference_forbidden"):
            with transport.open(urllib.request.Request(valid), 15):
                self.fail("Duplicate must not open")
        self.assertEqual(len(opener.requests), 1)

    def test_cli_checks_first_attempt_main_source_manifest_and_destination_before_fetch(self):
        value = manifest()
        env = {"GITHUB_SHA": PROVENANCE["producerCommit"], "GITHUB_RUN_ID": "123456", "GITHUB_RUN_ATTEMPT": "1",
               "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
               "EPC_RESULT_PUBLIC_KEY": "synthetic", "EPC_BEARER_TOKEN": "synthetic-private-credential",
               "EPC_CERTIFICATE_REVIEW_MANIFEST_B64": encoded(value),
               "EPC_CERTIFICATE_REVIEW_MANIFEST_SHA256": review.digest(value)}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(review, "fetch_review") as fetch, \
                patch.object(review, "validate_recipient", return_value={"publicKeySha256": "a" * 64}):
            output = Path(directory) / "fresh"
            for key, wrong in (("GITHUB_RUN_ATTEMPT", "2"), ("GITHUB_REF", "refs/heads/other"),
                               ("GITHUB_EVENT_NAME", "push"), ("GITHUB_SHA", "d" * 40),
                               ("EPC_CERTIFICATE_REVIEW_MANIFEST_SHA256", "0" * 64)):
                with patch.dict(os.environ, {**env, key: wrong}, clear=True), \
                        patch.object(sys, "argv", ["review", "--output-dir", str(output)]), self.assertRaises(ValueError):
                    review.main()
            output.mkdir()
            marker = output / "preserved"
            marker.write_text("unchanged")
            with patch.dict(os.environ, env, clear=True), \
                    patch.object(sys, "argv", ["review", "--output-dir", str(output)]), self.assertRaises(ValueError):
                review.main()
            self.assertEqual(marker.read_text(), "unchanged")
            fetch.assert_not_called()

    def test_workflow_is_separate_read_only_dispatch_and_uploads_only_safe_files(self):
        source = (ROOT / ".github/workflows/epc-certificate-review.yml").read_text()
        triggers = source.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertEqual(re.findall(r"^  ([a-z_]+):", triggers, re.M), ["workflow_dispatch"])
        self.assertEqual(re.findall(r"^      ([a-z0-9_]+):", triggers, re.M), ["manifest_sha256", "epc_result_public_key"])
        self.assertIn("permissions:\n  contents: read\n", source)
        self.assertIn("github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'", source)
        self.assertIn("ref: ${{ github.sha }}\n          persist-credentials: false", source)
        runs = re.findall(r"^        run: (.+)$", source, re.M)
        self.assertEqual(runs, ["PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts python3 -m unittest tests.test_epc_certificate_review tests.test_epc_candidate_result",
                               'python3 scripts/fetch_epc_certificate_review.py --output-dir "$RUNNER_TEMP/insight-epc-certificate-review"'])
        self.assertFalse(any("${{" in command for command in runs))
        for name, value in {"EPC_BEARER_TOKEN": "secrets.EPC_BEARER_TOKEN",
                            "EPC_CERTIFICATE_REVIEW_MANIFEST_B64": "secrets.EPC_CERTIFICATE_REVIEW_MANIFEST_B64",
                            "EPC_CERTIFICATE_REVIEW_MANIFEST_SHA256": "inputs.manifest_sha256",
                            "EPC_RESULT_PUBLIC_KEY": "inputs.epc_result_public_key"}.items():
            self.assertIn(name + ": ${{ " + value + " }}", source)
        paths = re.findall(r"^            (.+)$", source, re.M)
        self.assertEqual(paths, ["${{ runner.temp }}/insight-epc-certificate-review/" + name
                                 for name in ("result.enc", "result.key", "receipt.json")])
        for required in ("if: always()", "uses: actions/upload-artifact@v4", "retention-days: 3", "overwrite: false"):
            self.assertIn(required, source)
        self.assertNotRegex(source, r"\bgit (push|commit|add)|contents: write|actions/cache|outputs/|work/epc|backfill_epc_candidate.py")


class CertificateReviewEncryptionTests(unittest.TestCase):
    def test_partial_review_roundtrip_redacts_credentials_and_rejects_patch_admission(self):
        with tempfile.TemporaryDirectory(prefix="epc-review-test-") as directory:
            root = Path(directory)
            private = root / "private.pem"
            crypto._write_private(private, b"")
            subprocess.run([crypto.OPENSSL, "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:3072",
                            "-out", str(private)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            public = crypto._openssl(["pkey", "-in", str(private), "-pubout"]).decode("ascii")
            value = manifest(recipient=crypto.validate_recipient(public)["publicKeySha256"])
            opener = FakeOpener([certificate(REFS[0]), RuntimeError("Bearer synthetic-private-credential")])
            payload, receipt = review.fetch_review(value, "synthetic-private-credential", PROVENANCE,
                                                   opener=opener, spacing=0)
            output = root / "sealed"
            crypto.seal_result(payload, receipt, public, output)
            opened = crypto.open_result(output, private)
            self.assertEqual(opened["payload"], payload)
            self.assertEqual(opened["public_receipt"], receipt)
            self.assertEqual(receipt["status"], "partial")
            self.assertFalse(receipt["candidateUsable"])
            self.assertEqual(payload["records"][1]["code"], "review_fetch_failed")
            self.assertEqual({path.name for path in output.iterdir()}, {"result.enc", "result.key", "receipt.json"})
            for path in output.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                for private_text in (*REFS, "SYNTHETIC WILLOW HOUSE", "synthetic-private-credential", "private@example.invalid"):
                    self.assertNotIn(private_text.encode(), path.read_bytes())
            with self.assertRaisesRegex(ValueError, "not bound"):
                review.base.apply_candidate_to_frozen_app(payload, root / "never-read.js")


if __name__ == "__main__":
    unittest.main()
