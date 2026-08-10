from __future__ import annotations

import hmac
import os
import re
import stat
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import sys


LEGACY_ROOT = Path(__file__).resolve().parents[2] / "freio-prospecting"
if str(LEGACY_ROOT) not in sys.path:
    sys.path.insert(0, str(LEGACY_ROOT))

from freio_prospecting.common import (  # noqa: E402
    CONTROL_CHARACTERS,
    ValidationError,
    sha256_hex,
)


ENDPOINT_HOST = "outreach.freio.cz"
ENDPOINT_PATH = "/api/internal/b2b-agent/prospect-intake"
MAX_BODY_BYTES = 64 * 1024
SECRET = re.compile(rb"^[0-9a-f]{64}$")
NONCE = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class SignedRequest:
    body: bytes
    headers: dict[str, str]
    canonical_request: bytes


def normalize_endpoint(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 2000
        or CONTROL_CHARACTERS.search(value)
        or "\\" in value
        or value != value.strip()
    ):
        raise ValidationError("B2B discovery endpoint is malformed")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("B2B discovery endpoint is malformed") from exc
    if (
        parsed.scheme != "https"
        or hostname != ENDPOINT_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or port not in (None, 443)
        or parsed.path != ENDPOINT_PATH
    ):
        raise ValidationError("B2B discovery endpoint must match the pinned URL")
    return urlunsplit(("https", ENDPOINT_HOST, ENDPOINT_PATH, "", ""))


def load_private_secret(path: Path, label: str) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValidationError(f"cannot inspect {label} credential") from exc
    permissions = stat.S_IMODE(metadata.st_mode)
    credential_directory = os.environ.get("CREDENTIALS_DIRECTORY")
    systemd_delivered = (
        permissions == 0o440
        and credential_directory is not None
        and Path(credential_directory).is_absolute()
        and path.parent == Path(credential_directory)
    )
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or (permissions not in (0o400, 0o600) and not systemd_delivered)
    ):
        raise ValidationError(f"{label} credential must be a private regular file")
    if metadata.st_size not in (64, 65):
        raise ValidationError(f"{label} credential must contain 64 lowercase hex bytes")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError(f"cannot read {label} credential") from exc
    secret = raw[:-1] if raw.endswith(b"\n") else raw
    if SECRET.fullmatch(secret) is None:
        raise ValidationError(f"{label} credential must be literal lowercase hex")
    return secret


def build_signed_request(
    *,
    endpoint: str,
    body: bytes,
    secret: bytes,
    receipt_id: str,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> SignedRequest:
    normalize_endpoint(endpoint)
    if not body or len(body) > MAX_BODY_BYTES:
        raise ValidationError("B2B discovery request body must contain at most 64 KiB")
    if SECRET.fullmatch(secret) is None:
        raise ValidationError("B2B discovery HMAC credential is invalid")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", receipt_id):
        raise ValidationError("B2B discovery receipt ID is invalid")
    request_timestamp = int(time.time()) if timestamp is None else timestamp
    if request_timestamp < 0:
        raise ValidationError("B2B discovery signature timestamp is invalid")
    request_nonce = os.urandom(16).hex() if nonce is None else nonce
    if NONCE.fullmatch(request_nonce) is None:
        raise ValidationError("B2B discovery nonce must be 32 lowercase hex characters")
    body_hash = sha256_hex(body)
    canonical = (
        "freio-b2b-discovery-v1\n"
        f"{request_timestamp}\n"
        f"{request_nonce}\n"
        "POST\n"
        f"{ENDPOINT_PATH}\n"
        f"{body_hash}"
    ).encode("utf-8")
    signature = hmac.new(secret, canonical, sha256).hexdigest()
    return SignedRequest(
        body=body,
        canonical_request=canonical,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "X-Freio-B2B-Discovery-Version": "1",
            "X-Freio-B2B-Discovery-Timestamp": str(request_timestamp),
            "X-Freio-B2B-Discovery-Nonce": request_nonce,
            "X-Freio-B2B-Discovery-Content-SHA256": body_hash,
            "X-Freio-B2B-Discovery-Signature": f"v1={signature}",
            "Idempotency-Key": receipt_id,
        },
    )
