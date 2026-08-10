from __future__ import annotations

import hashlib
import hmac
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import ENDPOINT, TRANSPORT_SECRET

from freio_b2b_discovery.signing import (
    ENDPOINT_PATH,
    build_signed_request,
    load_private_secret,
    normalize_endpoint,
)
from freio_prospecting.common import ValidationError


class SigningTests(unittest.TestCase):
    def test_stable_hmac_contract_and_headers(self) -> None:
        body = b'{"schemaVersion":"1"}'
        receipt_id = "sha256:" + "a" * 64
        signed = build_signed_request(
            endpoint=ENDPOINT,
            body=body,
            secret=TRANSPORT_SECRET,
            receipt_id=receipt_id,
            timestamp=1_786_363_200,
            nonce="0123456789abcdef0123456789abcdef",
        )
        body_hash = hashlib.sha256(body).hexdigest()
        canonical = (
            "freio-b2b-discovery-v1\n1786363200\n"
            "0123456789abcdef0123456789abcdef\nPOST\n"
            f"{ENDPOINT_PATH}\n{body_hash}"
        ).encode()
        signature = hmac.new(TRANSPORT_SECRET, canonical, hashlib.sha256).hexdigest()
        self.assertEqual(signed.canonical_request, canonical)
        self.assertEqual(
            signed.headers["X-Freio-B2B-Discovery-Signature"], f"v1={signature}"
        )
        self.assertEqual(signed.headers["Idempotency-Key"], receipt_id)

    def test_endpoint_is_exact_https_and_queryless(self) -> None:
        self.assertEqual(normalize_endpoint(ENDPOINT), ENDPOINT)
        for invalid in (
            ENDPOINT + "?debug=1",
            "http://outreach.freio.cz" + ENDPOINT_PATH,
            "https://freio.cz" + ENDPOINT_PATH,
            "https://outreach.freio.cz/api/internal/outreach/run",
        ):
            with self.assertRaises(ValidationError):
                normalize_endpoint(invalid)

    def test_credential_is_private_literal_lowercase_hex(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret"
            path.write_bytes(TRANSPORT_SECRET + b"\n")
            os.chmod(path, 0o600)
            self.assertEqual(load_private_secret(path, "test"), TRANSPORT_SECRET)
            os.chmod(path, 0o640)
            with self.assertRaises(ValidationError):
                load_private_secret(path, "test")

    def test_accepts_systemd_0440_only_from_exact_credentials_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            credentials = Path(directory) / "credentials"
            credentials.mkdir(mode=0o700)
            path = credentials / "secret"
            path.write_bytes(TRANSPORT_SECRET + b"\n")
            os.chmod(path, 0o440)

            with self.assertRaises(ValidationError):
                load_private_secret(path, "test")
            with patch.dict(
                os.environ,
                {"CREDENTIALS_DIRECTORY": str(credentials)},
            ):
                self.assertEqual(
                    load_private_secret(path, "test"),
                    TRANSPORT_SECRET,
                )


if __name__ == "__main__":
    unittest.main()
