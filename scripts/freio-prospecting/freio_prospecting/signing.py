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

from .common import CONTROL_CHARACTERS, ValidationError, sha256_hex


NONCE = re.compile(r"^[0-9a-f]{32}$")
SUBMIT_SECRET = re.compile(rb"^[0-9a-f]{64}$")
SUBMIT_HOSTNAME = "outreach.freio.cz"
SUBMIT_PATH = "/api/internal/growth-partners/prospect-intake"


@dataclass(frozen=True)
class SignedRequest:
    body: bytes
    headers: dict[str, str]
    canonical_request: bytes


def normalize_submit_endpoint(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 2000
        or CONTROL_CHARACTERS.search(value)
        or "\\" in value
    ):
        raise ValidationError("submit endpoint is malformed")
    try:
        parsed = urlsplit(value.strip())
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("submit endpoint is malformed") from exc
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in (None, 443)
        or not parsed.path.startswith("/")
    ):
        raise ValidationError(
            "submit endpoint must be credential-free HTTPS on port 443"
        )
    try:
        ascii_hostname = hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValidationError("submit endpoint hostname is invalid") from exc
    if ascii_hostname != SUBMIT_HOSTNAME or parsed.path != SUBMIT_PATH or parsed.query:
        raise ValidationError(
            "submit endpoint must match the pinned Freio intake origin and path"
        )
    return urlunsplit(("https", ascii_hostname, parsed.path, parsed.query, ""))


def load_secret(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValidationError(f"cannot inspect submit credential: {exc}") from exc
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise ValidationError("submit credential must be a regular non-symlink file")
    if metadata.st_mode & 0o077:
        raise ValidationError(
            "submit credential must not be accessible by group or others"
        )
    if metadata.st_size not in (64, 65):
        raise ValidationError(
            "submit credential must be 64 lowercase hex characters with optional LF"
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError(f"cannot read submit credential: {exc}") from exc
    secret = raw[:-1] if raw.endswith(b"\n") else raw
    if SUBMIT_SECRET.fullmatch(secret) is None:
        raise ValidationError(
            "submit credential must be the literal lowercase hex output of openssl rand -hex 32"
        )
    return secret


def build_signed_request(
    *,
    endpoint: str,
    body: bytes,
    secret: bytes,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> SignedRequest:
    normalized_endpoint = normalize_submit_endpoint(endpoint)
    parsed = urlsplit(normalized_endpoint)
    request_target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    if not body or len(body) > 96 * 1024:
        raise ValidationError("submit body must contain at most 96 KiB")
    if len(secret) < 32:
        raise ValidationError("submit credential is too short")
    request_timestamp = int(time.time()) if timestamp is None else timestamp
    if request_timestamp < 0:
        raise ValidationError("signature timestamp is invalid")
    request_nonce = os.urandom(16).hex() if nonce is None else nonce
    if NONCE.fullmatch(request_nonce) is None:
        raise ValidationError("signature nonce must be 16 lowercase hex bytes")
    content_hash = sha256_hex(body)
    canonical = (
        "freio-prospecting-v1\n"
        f"{request_timestamp}\n"
        f"{request_nonce}\n"
        "POST\n"
        f"{request_target}\n"
        f"{content_hash}"
    ).encode("utf-8")
    signature = hmac.new(secret, canonical, sha256).hexdigest()
    return SignedRequest(
        body=body,
        canonical_request=canonical,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "X-Freio-Prospecting-Version": "1",
            "X-Freio-Prospecting-Timestamp": str(request_timestamp),
            "X-Freio-Prospecting-Nonce": request_nonce,
            "X-Freio-Prospecting-Content-SHA256": content_hash,
            "X-Freio-Prospecting-Signature": f"v1={signature}",
        },
    )
