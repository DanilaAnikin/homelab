from __future__ import annotations

import hashlib
import hmac
import tempfile
import unittest
from pathlib import Path

from helpers import PACKAGE_ROOT, batch_envelope  # noqa: F401

from freio_prospecting.common import ValidationError, canonical_json_bytes
from freio_prospecting.signing import (
    build_signed_request,
    load_secret,
    normalize_submit_endpoint,
)


class SigningTests(unittest.TestCase):
    def test_canonical_request_and_signature_are_exact(self) -> None:
        body = b'{"items":[]}'
        secret = b"s" * 32
        signed = build_signed_request(
            endpoint="https://OUTREACH.freio.cz/api/internal/growth-partners/prospect-intake",
            body=body,
            secret=secret,
            timestamp=1_786_267_200,
            nonce="0123456789abcdef0123456789abcdef",
        )
        expected = (
            "freio-prospecting-v1\n1786267200\n0123456789abcdef0123456789abcdef\n"
            f"POST\n/api/internal/growth-partners/prospect-intake\n{hashlib.sha256(body).hexdigest()}"
        ).encode()
        self.assertEqual(signed.canonical_request, expected)
        signature = hmac.new(secret, expected, hashlib.sha256).hexdigest()
        self.assertEqual(
            signed.headers["X-Freio-Prospecting-Signature"], f"v1={signature}"
        )
        self.assertEqual(signed.headers["Content-Length"], str(len(body)))

    def test_rejects_invalid_endpoint_nonce_body_and_secret(self) -> None:
        for endpoint in (
            "http://outreach.freio.cz/api",
            "https://user@outreach.freio.cz/api",
            "https://outreach.freio.cz:8443/api",
            "https://localhost/api",
            "https://outreach.freio.cz/api#fragment",
            "https://evil.example.cz/api/internal/growth-partners/prospect-intake",
            "https://outreach.freio.cz/api/internal/growth-partners/prospect-intake?mode=v1",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValidationError):
                normalize_submit_endpoint(endpoint)
        with self.assertRaises(ValidationError):
            build_signed_request(
                endpoint="https://outreach.freio.cz/api", body=b"", secret=b"s" * 32
            )
        with self.assertRaises(ValidationError):
            build_signed_request(
                endpoint="https://outreach.freio.cz/api", body=b"{}", secret=b"short"
            )
        with self.assertRaises(ValidationError):
            build_signed_request(
                endpoint="https://outreach.freio.cz/api",
                body=b"{}",
                secret=b"s" * 32,
                nonce="bad",
            )

    def test_secret_file_requires_private_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = root / "valid"
            valid.write_bytes(b"a" * 64)
            valid.chmod(0o600)
            self.assertEqual(load_secret(valid), b"a" * 64)
            valid.chmod(0o640)
            with self.assertRaisesRegex(ValidationError, "group or others"):
                load_secret(valid)
            target = root / "target"
            target.write_bytes(b"y" * 32)
            target.chmod(0o600)
            link = root / "link"
            link.symlink_to(target)
            with self.assertRaisesRegex(ValidationError, "non-symlink"):
                load_secret(link)

    def test_tampering_even_non_recomputable_manifest_receipt_changes_hmac(
        self,
    ) -> None:
        first = batch_envelope()
        tampered = batch_envelope()
        tampered["receipt"]["researchManifestSha256"] = "f" * 64
        common = {
            "endpoint": "https://outreach.freio.cz/api/internal/growth-partners/prospect-intake",
            "secret": b"s" * 32,
            "timestamp": 1_786_267_200,
            "nonce": "0123456789abcdef0123456789abcdef",
        }
        first_signed = build_signed_request(body=canonical_json_bytes(first), **common)
        tampered_signed = build_signed_request(
            body=canonical_json_bytes(tampered), **common
        )
        self.assertNotEqual(
            first_signed.headers["X-Freio-Prospecting-Content-SHA256"],
            tampered_signed.headers["X-Freio-Prospecting-Content-SHA256"],
        )
        self.assertNotEqual(
            first_signed.headers["X-Freio-Prospecting-Signature"],
            tampered_signed.headers["X-Freio-Prospecting-Signature"],
        )


if __name__ == "__main__":
    unittest.main()
