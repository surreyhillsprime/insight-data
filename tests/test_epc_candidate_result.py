"""Real-OpenSSL sealed-result tests using synthetic records and throwaway keys."""

import hashlib
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import epc_candidate_result as result


class CandidateResultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory(prefix="epc-result-test-keys-")
        cls.key_root = Path(cls.keys.name)
        cls.private = cls.key_root / "private.pem"
        cls.wrong = cls.key_root / "wrong.pem"
        cls.weak = cls.key_root / "weak.pem"
        for path, bits in ((cls.private, 3072), (cls.wrong, 3072), (cls.weak, 2048)):
            result._write_private(path, b"")
            subprocess.run([result.OPENSSL, "genpkey", "-algorithm", "RSA", "-pkeyopt",
                            f"rsa_keygen_bits:{bits}", "-out", str(path)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.public = result._openssl(["pkey", "-in", str(cls.private), "-pubout"]).decode("ascii")
        cls.weak_public = result._openssl(["pkey", "-in", str(cls.weak), "-pubout"]).decode("ascii")

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="epc-result-test-")
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / "sealed"
        self.payload = {"records": [{"address": "SYNTHETIC PRIVATE CERTIFICATE ADDRESS",
                                    "certificateId": "SYNTHETIC-SECRET-RECORD-93713",
                                    "floorAreaSqm": 208}], "pending": ["synthetic-sale-2"]}
        self.public_receipt = {"requested": 2, "resolved": 1, "pending": 1,
                               "sourceCommit": "c" * 40, "purpose": "Offline synthetic test"}

    def seal(self):
        return result.seal_result(self.payload, self.public_receipt, self.public, self.output)

    def change_manifest(self, update):
        path = self.output / "receipt.json"
        manifest = json.loads(path.read_text())
        update(manifest)
        path.write_text(json.dumps(manifest))

    def test_roundtrip_public_receipt_binding_and_private_permissions(self):
        manifest = self.seal()
        opened = result.open_result(self.output, self.private)
        self.assertEqual(opened, {"payload": self.payload, "public_receipt": self.public_receipt,
                                  "receipt": manifest})
        self.assertEqual(manifest["recipient"]["rsaBits"], 3072)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
        self.assertEqual({p.name for p in self.output.iterdir()}, {"result.enc", "result.key", "receipt.json"})
        for path in self.output.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertNotIn(b"SYNTHETIC PRIVATE CERTIFICATE ADDRESS", path.read_bytes())
            self.assertNotIn(b"SYNTHETIC-SECRET-RECORD-93713", path.read_bytes())
        self.assertEqual((self.output / "result.key").stat().st_size, 384)

    def test_each_seal_has_independent_random_encryption(self):
        first = self.seal()
        other = Path(self.directory.name) / "other"
        second = result.seal_result(self.payload, self.public_receipt, self.public, other)
        self.assertNotEqual(first["ciphertext"], second["ciphertext"])
        self.assertNotEqual(first["wrappedKey"], second["wrappedKey"])
        self.assertNotEqual(first["authenticationTag"], second["authenticationTag"])

    def test_ciphertext_tampering_rejected_before_aes_even_with_updated_public_hash(self):
        self.seal()
        path = self.output / "result.enc"
        value = bytearray(path.read_bytes())
        value[-1] ^= 1
        path.write_bytes(value)
        self.change_manifest(lambda manifest: manifest["ciphertext"].update(sha256=hashlib.sha256(value).hexdigest()))
        with patch.object(result, "_aes", side_effect=AssertionError("Decryption must not start")):
            with self.assertRaisesRegex(result.ResultError, "authentication failed"):
                result.open_result(self.output, self.private)

    def test_receipt_tampering_rejected_before_aes(self):
        self.seal()
        self.change_manifest(lambda manifest: manifest["publicReceipt"].update(resolved=2, pending=0))
        with patch.object(result, "_aes", side_effect=AssertionError("Decryption must not start")):
            with self.assertRaisesRegex(result.ResultError, "authentication failed"):
                result.open_result(self.output, self.private)

    def test_wrapped_key_tampering_rejected(self):
        self.seal()
        path = self.output / "result.key"
        value = bytearray(path.read_bytes())
        value[0] ^= 1
        path.write_bytes(value)
        with self.assertRaises(result.ResultError):
            result.open_result(self.output, self.private)

    def test_wrong_private_key_fails_with_sanitized_error(self):
        self.seal()
        with self.assertRaisesRegex(result.ResultError, "^OpenSSL operation failed$"):
            result.open_result(self.output, self.wrong)

    def test_public_key_validation_rejects_weak_private_malformed_and_appended_keys(self):
        for public in (self.weak_public, self.private.read_text(), "not a key", self.public + self.public,
                       self.public + "trailing text", "-----BEGIN PUBLIC KEY-----\ninvalid\n-----END PUBLIC KEY-----"):
            with self.subTest(kind=public[:26]):
                with self.assertRaises(result.ResultError):
                    result.validate_recipient(public)
        self.assertFalse(self.output.exists())

    def test_invalid_recipient_fails_before_result_directory_is_created(self):
        with self.assertRaises(result.ResultError):
            result.seal_result(self.payload, self.public_receipt, self.weak_public, self.output)
        self.assertFalse(self.output.exists())

    def test_degenerate_public_exponent_rejected_before_wrap_probe(self):
        with patch.object(result, "_openssl", return_value=b"Public-Key: (3072 bit)\nExponent: 1 (0x1)\n") as command:
            with self.assertRaisesRegex(result.ResultError, "exponent"):
                result.validate_recipient(self.public)
            self.assertEqual(command.call_count, 1)

    def test_output_directory_is_exclusive_and_preserves_existing_files(self):
        self.output.mkdir()
        marker = self.output / "user-file"
        marker.write_text("preserve")
        with self.assertRaises(FileExistsError):
            self.seal()
        self.assertEqual(marker.read_text(), "preserve")

    def test_credentials_rejected_without_raw_output(self):
        for payload in ({"EPC_BEARER_TOKEN": "synthetic-credential"},
                        {"nested": [{"Authorization": "Bearer synthetic"}]},
                        {"value": "Bearer synthetic-credential"},
                        {"value": self.private.read_text()}):
            with self.subTest(keys=list(payload)):
                with self.assertRaisesRegex(result.ResultError, "credentials"):
                    result.seal_result(payload, self.public_receipt, self.public, self.output)
                self.assertFalse(self.output.exists())

    def test_plaintext_and_secret_temporary_files_removed_after_success_and_failure(self):
        real_temporary = tempfile.TemporaryDirectory
        observed = []

        def tracked(*args, **kwargs):
            directory = real_temporary(*args, **kwargs)
            observed.append(Path(directory.name))
            return directory

        with patch.object(result.tempfile, "TemporaryDirectory", side_effect=tracked):
            self.seal()
            result.open_result(self.output, self.private)
            with self.assertRaises(result.ResultError):
                result.open_result(self.output, self.wrong)
        self.assertGreaterEqual(len(observed), 5)
        self.assertTrue(all(not path.exists() for path in observed))

    def test_failure_during_seal_removes_only_new_output_directory(self):
        with patch.object(result, "_aes", side_effect=result.ResultError("Synthetic encryption failure")):
            with self.assertRaises(result.ResultError):
                self.seal()
        self.assertFalse(self.output.exists())

    def test_bounded_files_and_symlinks_rejected(self):
        self.seal()
        with patch.object(result, "MAX_RECEIPT_BYTES", 4):
            with self.assertRaises(result.ResultError):
                result.open_result(self.output, self.private)
        ciphertext = self.output / "result.enc"
        ciphertext.unlink()
        ciphertext.symlink_to(self.private)
        with self.assertRaises(result.ResultError):
            result.open_result(self.output, self.private)

    def test_duplicate_receipt_keys_and_algorithm_changes_rejected(self):
        self.seal()
        self.change_manifest(lambda manifest: manifest["algorithms"].update(iterations=1))
        with self.assertRaises(result.ResultError):
            result.open_result(self.output, self.private)
        path = self.output / "receipt.json"
        path.write_text('{"schema":"one","schema":"two"}')
        with self.assertRaises(result.ResultError):
            result.open_result(self.output, self.private)

    def test_subprocess_errors_do_not_expose_stderr_or_secret_values(self):
        failure = subprocess.CompletedProcess([], 1, stdout=b"private output", stderr=b"private key / token")
        with patch.object(result.subprocess, "run", return_value=failure):
            with self.assertRaisesRegex(result.ResultError, "^OpenSSL operation failed$"):
                result._openssl(["pkey"])


if __name__ == "__main__":
    unittest.main()
