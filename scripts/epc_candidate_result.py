"""Seal private EPC candidate results for one local RSA recipient.

Only result.enc, result.key and receipt.json may be published. The recipient's
private key never belongs on a runner. The receipt is public, so callers must
put certificate records in payload, not public_receipt, and never include
provider credentials in either object.

Version 1 uses RSA-OAEP (SHA-256 and MGF1-SHA-256) to wrap 64 random bytes.
The first 32 bytes, base64 encoded, are an AES-256-CBC password; OpenSSL derives
the encryption key and IV using PBKDF2-HMAC-SHA-256, 200,000 iterations and its
random salted file format. The other 32 bytes independently key HMAC-SHA-256.
The MAC binds length-framed canonical receipt metadata, ciphertext and wrapped
key. Authentication is checked before AES decryption. This authenticates the
sealed contents, not the identity of whoever possesses the public key.

Requires system OpenSSL (tested with LibreSSL 3.3.6). No third-party Python
packages, shell invocation, command-line secrets or raw OpenSSL error output.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
from typing import Any


SCHEMA = "insight.epc-candidate-result.v1"
MAX_PAYLOAD_BYTES = 128 * 1024 * 1024
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_KEY_BYTES = 16 * 1024
PBKDF2_ITERATIONS = 200_000
OPENSSL = "/usr/bin/openssl"
_DOMAIN = b"INSIGHT EPC candidate result v1\x00"
_ALGORITHMS = {
    "cipher": "AES-256-CBC",
    "kdf": "PBKDF2-HMAC-SHA256",
    "iterations": PBKDF2_ITERATIONS,
    "saltBytes": 8,
    "keyWrap": "RSA-OAEP-SHA256-MGF1-SHA256",
    "authentication": "HMAC-SHA256",
}
_CREDENTIAL_KEYS = {
    "token", "bearertoken", "epcbearertoken", "accesstoken", "refreshtoken",
    "apikey", "homedataapikey", "authorization", "password", "privatekey",
    "clientsecret", "credentials",
}


class ResultError(ValueError):
    """A candidate result or recipient failed a safe, non-sensitive check."""


def _openssl(arguments: list[str]) -> bytes:
    try:
        result = subprocess.run(
            [OPENSSL, *arguments], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            timeout=60, env={"LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ResultError("OpenSSL operation failed") from None
    if result.returncode:
        # In particular, do not surface certificate data, paths, key material
        # or inherited provider credentials from a subprocess exception.
        raise ResultError("OpenSSL operation failed") from None
    return result.stdout


def _write_private(path: Path, value: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(value)


def _read_bounded(path: Path, limit: int) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ResultError("Result file is not a bounded regular file")
            value = handle.read(limit + 1)
            if len(value) > limit:
                raise ResultError("Result file exceeds its size limit")
            return value
    except OSError:
        raise ResultError("Result file cannot be read safely") from None


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ResultError("Result must contain finite JSON values") from None


def _reject_credentials(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ResultError("Result object keys must be strings")
            if re.sub(r"[^a-z0-9]", "", key.lower()) in _CREDENTIAL_KEYS:
                raise ResultError("Provider credentials must not enter candidate results")
            _reject_credentials(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_credentials(item)
    elif isinstance(value, str) and (
        re.match(r"(?i)^bearer\s+\S", value.strip())
        or "PRIVATE KEY-----" in value
    ):
        raise ResultError("Provider credentials must not enter candidate results")


def validate_recipient(recipient_pem: str) -> dict[str, Any]:
    """Validate before fetching anything; return only public key metadata.

    Accept exactly one PEM SubjectPublicKeyInfo RSA public key, 3072–16384
    bits. Private keys, additional PEM objects and trailing material fail.
    """
    if not isinstance(recipient_pem, str) or len(recipient_pem) > MAX_KEY_BYTES:
        raise ResultError("A bounded RSA public key is required")
    pem = recipient_pem.strip()
    if not re.fullmatch(
        r"-----BEGIN PUBLIC KEY-----\s+[A-Za-z0-9+/=\r\n]+\s+-----END PUBLIC KEY-----",
        pem,
    ):
        raise ResultError("Exactly one RSA public key is required; private keys are forbidden")
    with tempfile.TemporaryDirectory(prefix="epc-recipient-") as scratch:
        public_path = Path(scratch) / "recipient.pem"
        _write_private(public_path, (pem + "\n").encode("ascii"))
        description = _openssl(["rsa", "-pubin", "-in", str(public_path), "-text", "-noout"])
        match = re.search(rb"Public-Key:\s*\((\d+) bit\)", description)
        bits = int(match.group(1)) if match else 0
        if not 3072 <= bits <= 16384:
            raise ResultError("Recipient RSA key must contain at least 3072 bits")
        exponent_match = re.search(rb"Exponent:\s*(\d{1,20})\s", description)
        exponent = int(exponent_match.group(1)) if exponent_match else 0
        if exponent < 3 or exponent % 2 == 0:
            raise ResultError("Recipient RSA public exponent is invalid")
        der = _openssl(["pkey", "-pubin", "-in", str(public_path), "-outform", "DER"])
        # Prove this public key supports the exact wrapping operation before
        # callers make a provider request (e.g. RSA-PSS-only keys cannot wrap).
        probe = Path(scratch) / "probe"
        wrapped_probe = Path(scratch) / "probe.enc"
        _write_private(probe, b"\x00" * 64)
        _write_private(wrapped_probe, b"")
        _openssl(["pkeyutl", "-encrypt", "-pubin", "-inkey", str(public_path),
                  "-in", str(probe), "-out", str(wrapped_probe), *_oaep()])
    return {"rsaBits": bits, "publicKeySha256": hashlib.sha256(der).hexdigest()}


def _authentication(secret: bytes, metadata: dict, ciphertext: bytes, wrapped: bytes) -> str:
    mac = hmac.new(secret, _DOMAIN, hashlib.sha256)
    for part in (_canonical(metadata), ciphertext, wrapped):
        mac.update(len(part).to_bytes(8, "big"))
        mac.update(part)
    return mac.hexdigest()


def _oaep() -> list[str]:
    return ["-pkeyopt", "rsa_padding_mode:oaep", "-pkeyopt", "rsa_oaep_md:sha256",
            "-pkeyopt", "rsa_mgf1_md:sha256"]


def _aes(password: Path, source: Path, destination: Path, decrypt: bool = False) -> None:
    # Pre-create the destination privately: OpenSSL otherwise honours the
    # process umask, which may create world-readable temporary plaintext.
    _write_private(destination, b"")
    _openssl(["enc", "-aes-256-cbc", "-d" if decrypt else "-e", "-salt",
              "-pbkdf2", "-iter", str(PBKDF2_ITERATIONS), "-md", "sha256",
              "-pass", "file:" + str(password), "-in", str(source), "-out", str(destination)])


def seal_result(payload: dict, public_receipt: dict, recipient_pem: str,
                output_dir: Path) -> dict:
    """Write a new private directory containing only three publishable files.

    Returns the public manifest. Existing output paths are never overwritten.
    Ephemeral plaintext and key files live only in a private temporary directory
    which is deleted whether sealing succeeds or raises.
    """
    recipient = validate_recipient(recipient_pem)
    if not isinstance(payload, dict) or not isinstance(public_receipt, dict):
        raise ResultError("Payload and public receipt must be JSON objects")
    try:
        _reject_credentials(payload)
        _reject_credentials(public_receipt)
    except RecursionError:
        raise ResultError("Result structure is too deeply nested") from None
    plaintext = _canonical(payload)
    if len(plaintext) > MAX_PAYLOAD_BYTES or len(_canonical(public_receipt)) > MAX_RECEIPT_BYTES // 2:
        raise ResultError("Candidate result exceeds its size limit")
    output_dir = Path(output_dir)
    created = False
    try:
        output_dir.mkdir(mode=0o700)
        created = True
        output_dir.chmod(0o700)
        with tempfile.TemporaryDirectory(prefix="epc-result-seal-") as scratch:
            root = Path(scratch)
            secret = secrets.token_bytes(64)
            _write_private(root / "secret", secret)
            _write_private(root / "password", base64.b64encode(secret[:32]) + b"\n")
            _write_private(root / "recipient.pem", recipient_pem.encode("ascii"))
            _write_private(root / "payload", plaintext)
            _aes(root / "password", root / "payload", root / "ciphertext")
            _write_private(root / "wrapped", b"")
            _openssl(["pkeyutl", "-encrypt", "-pubin", "-inkey", str(root / "recipient.pem"),
                      "-in", str(root / "secret"), "-out", str(root / "wrapped"), *_oaep()])
            ciphertext = _read_bounded(root / "ciphertext", MAX_PAYLOAD_BYTES + 1024)
            wrapped = _read_bounded(root / "wrapped", MAX_KEY_BYTES)
            if (not ciphertext.startswith(b"Salted__") or len(ciphertext) < 32
                    or (len(ciphertext) - 16) % 16 or len(wrapped) != (recipient["rsaBits"] + 7) // 8):
                raise ResultError("OpenSSL produced an unsupported encrypted result")
            metadata = {
                "schema": SCHEMA, "algorithms": dict(_ALGORITHMS), "recipient": recipient,
                "publicReceipt": public_receipt,
                "ciphertext": {"bytes": len(ciphertext), "sha256": hashlib.sha256(ciphertext).hexdigest()},
                "wrappedKey": {"bytes": len(wrapped), "sha256": hashlib.sha256(wrapped).hexdigest()},
            }
            manifest = {**metadata, "authenticationTag": _authentication(secret[32:], metadata, ciphertext, wrapped)}
            _write_private(output_dir / "result.enc", ciphertext)
            _write_private(output_dir / "result.key", wrapped)
            _write_private(output_dir / "receipt.json", _canonical(manifest) + b"\n")
        return manifest
    except Exception:
        if created:
            shutil.rmtree(output_dir)
        raise


def _json_object(value: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ResultError("Duplicate result object keys are forbidden")
            result[key] = item
        return result

    def finite_only(_value):
        raise ResultError("Non-finite result value")

    try:
        result = json.loads(value, object_pairs_hook=unique,
                            parse_constant=finite_only)
    except (ValueError, UnicodeError, RecursionError):
        raise ResultError("Result contains invalid JSON") from None
    if not isinstance(result, dict):
        raise ResultError("Result must be a JSON object")
    return result


def open_result(output_dir: Path, private_key_path: Path) -> dict:
    """Authenticate and open a sealed result locally, without writing raw output.

    Returns {payload, public_receipt, receipt}. The latter is the authenticated
    manifest and includes the recipient fingerprint. Private keys must already
    be local regular files; encrypted/passphrase-protected keys fail closed.
    """
    root = Path(output_dir)
    if root.is_symlink() or not root.is_dir():
        raise ResultError("Result directory must be a real directory")
    manifest = _json_object(_read_bounded(root / "receipt.json", MAX_RECEIPT_BYTES))
    expected_keys = {"schema", "algorithms", "recipient", "publicReceipt", "ciphertext", "wrappedKey", "authenticationTag"}
    if (set(manifest) != expected_keys or manifest.get("schema") != SCHEMA
            or manifest.get("algorithms") != _ALGORITHMS
            or not isinstance(manifest.get("publicReceipt"), dict)
            or not isinstance(manifest.get("recipient"), dict)
            or not isinstance(manifest.get("authenticationTag"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", manifest["authenticationTag"])):
        raise ResultError("Unsupported or malformed result manifest")
    ciphertext = _read_bounded(root / "result.enc", MAX_PAYLOAD_BYTES + 1024)
    wrapped = _read_bounded(root / "result.key", MAX_KEY_BYTES)
    for field, value in (("ciphertext", ciphertext), ("wrappedKey", wrapped)):
        if manifest.get(field) != {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}:
            raise ResultError("Result authentication failed")
    private_pem = _read_bounded(Path(private_key_path), MAX_KEY_BYTES)
    metadata = {key: value for key, value in manifest.items() if key != "authenticationTag"}
    with tempfile.TemporaryDirectory(prefix="epc-result-open-") as scratch:
        work = Path(scratch)
        _write_private(work / "private.pem", private_pem)
        _write_private(work / "wrapped", wrapped)
        _write_private(work / "secret", b"")
        # An explicit empty passphrase plus disconnected stdin prevents an
        # encrypted key from opening a prompt on a unattended local operation.
        _openssl(["pkeyutl", "-decrypt", "-inkey", str(work / "private.pem"), "-passin", "pass:",
                  "-in", str(work / "wrapped"), "-out", str(work / "secret"), *_oaep()])
        secret = _read_bounded(work / "secret", 64)
        if len(secret) != 64 or not hmac.compare_digest(
            _authentication(secret[32:], metadata, ciphertext, wrapped), manifest["authenticationTag"]
        ):
            raise ResultError("Result authentication failed")
        public_pem = _openssl(["pkey", "-in", str(work / "private.pem"), "-passin", "pass:", "-pubout"])
        if validate_recipient(public_pem.decode("ascii")) != manifest["recipient"]:
            raise ResultError("Result recipient binding failed")
        _write_private(work / "password", base64.b64encode(secret[:32]) + b"\n")
        _write_private(work / "ciphertext", ciphertext)
        _aes(work / "password", work / "ciphertext", work / "payload", decrypt=True)
        payload = _json_object(_read_bounded(work / "payload", MAX_PAYLOAD_BYTES))
    return {"payload": payload, "public_receipt": manifest["publicReceipt"], "receipt": manifest}
