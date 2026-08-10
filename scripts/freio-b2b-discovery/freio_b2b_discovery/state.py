from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
from pathlib import Path
from typing import Any, Iterator

from .model import (
    Candidate,
    build_research_document,
    parse_research_document,
    validate_intake_envelope,
)

import sys


LEGACY_ROOT = Path(__file__).resolve().parents[2] / "freio-prospecting"
if str(LEGACY_ROOT) not in sys.path:
    sys.path.insert(0, str(LEGACY_ROOT))

from freio_prospecting.common import (  # noqa: E402
    MAX_JSON_BYTES,
    ValidationError,
    atomic_write_bytes,
    canonical_json_bytes,
    parse_utc_timestamp,
    require_exact_keys,
    require_object,
    sha256_hex,
)


MANIFEST = re.compile(r"^([0-9a-f]{64})\.json$")
SIDECAR = re.compile(r"^([0-9a-f]{64})\.(request|retry|error)\.json$")
IDENTITY_DIGEST = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
RECEIPT_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
RAW_TTL = timedelta(hours=72)
RECEIPT_TTL = timedelta(days=30)
RETRY_TTL = timedelta(hours=24)
RETRY_MAX_ATTEMPTS = 5
RETRY_DELAYS = (300, 900, 3600, 10_800, 21_600)


def _now(value: datetime | None = None) -> datetime:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True)
class Claimed:
    path: Path
    document: dict[str, Any]
    candidate: Candidate
    envelope: dict[str, Any] | None


@dataclass(frozen=True)
class Retry:
    attempts: int
    first_at: datetime
    next_at: datetime
    expires_at: datetime
    reason: str


class Spool:
    DIRECTORY_MODES = {
        "ready": 0o770,
        "processing": 0o700,
        "processed": 0o700,
        "state": 0o700,
        "deferred": 0o750,
        "deferred/discovery": 0o700,
        "quarantine": 0o750,
        "quarantine/discovery": 0o700,
        "quarantine/claimed": 0o700,
    }

    def __init__(self, root: Path) -> None:
        self.root = Path(os.path.abspath(root))
        self.ready = self.root / "ready"
        self.processing = self.root / "processing"
        self.processed = self.root / "processed"
        self.state = self.root / "state"
        self.deferred = self.root / "deferred" / "discovery"
        self.quarantine_discovery = self.root / "quarantine" / "discovery"
        self.claimed_quarantine = self.root / "quarantine" / "claimed"
        self.lock_path = self.state / ".submit.lock"
        self.identity_index_path = self.state / "accepted-identities-v1.json"
        self.circuit_path = self.state / "global-submit-circuit-v1.json"
        self.identity_key_id: str | None = None

    @staticmethod
    def _fsync(*directories: Path) -> None:
        for directory in directories:
            descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    @staticmethod
    def _require_directory(path: Path, *, private: bool) -> None:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ValidationError("cannot inspect a spool directory") from exc
        if path.is_symlink() or not path.is_dir() or metadata.st_mode & 0o002:
            raise ValidationError(
                "spool path must be a real non-world-writable directory"
            )
        if private and metadata.st_mode & 0o077:
            raise ValidationError("private spool directory exposes group/other access")

    def ensure(self) -> None:
        self.root.mkdir(mode=0o750, parents=True, exist_ok=True)
        self._require_directory(self.root, private=False)
        if self.root.lstat().st_mode & 0o022:
            raise ValidationError("spool root must not be group/other writable")
        private_names = {
            "processing",
            "processed",
            "state",
            "deferred/discovery",
            "quarantine/discovery",
            "quarantine/claimed",
        }
        for name, mode in self.DIRECTORY_MODES.items():
            path = self.root / name
            path.mkdir(mode=mode, parents=True, exist_ok=True)
            self._require_directory(path, private=name in private_names)

    @contextmanager
    def submit_lock(self) -> Iterator[None]:
        self.ensure()
        descriptor = os.open(
            self.lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def bind_identity_key(self, secret: bytes) -> str:
        if len(secret) != 64:
            raise ValidationError("identity HMAC credential must contain 64 bytes")
        key_id = (
            "v1:"
            + sha256_hex(b"freio-b2b-discovery-identity-key-id-v1\0" + secret)[:16]
        )
        if self.identity_key_id is not None and self.identity_key_id != key_id:
            raise ValidationError("spool is already bound to another identity key")
        self.identity_key_id = key_id
        return key_id

    def identity_digests(self, candidate: Candidate, secret: bytes) -> tuple[str, ...]:
        self.bind_identity_key(secret)
        derived = hmac.new(
            secret, b"freio-b2b-discovery-identity-index-key-v1", sha256
        ).digest()
        values = (("website", candidate.website), ("email", candidate.email))
        return tuple(
            sorted(
                "hmac-sha256:"
                + hmac.new(
                    derived, f"{kind}\0{value}".encode("utf-8"), sha256
                ).hexdigest()
                for kind, value in values
            )
        )

    @staticmethod
    def _manifest_document(candidate: Candidate) -> dict[str, Any]:
        return build_research_document([candidate])

    def enqueue(self, candidate: Candidate) -> Path:
        self.ensure()
        document = self._manifest_document(candidate)
        canonical = canonical_json_bytes(document)
        digest = sha256_hex(canonical)
        destination = self.ready / f"{digest}.json"
        serialized = canonical + b"\n"
        if destination.exists():
            if destination.is_symlink() or destination.read_bytes() != serialized:
                raise ValidationError("ready manifest content-address collision")
            return destination
        # The discovery and intake workers intentionally run as separate users.
        # Keep the global umask strict, but expose the completed immutable handoff
        # to their shared group before the atomic rename into the ready queue.
        atomic_write_bytes(
            destination,
            serialized,
            mode=0o640,
            exact_mode=True,
        )
        return destination

    @staticmethod
    def _load_manifest(path: Path) -> tuple[dict[str, Any], Candidate]:
        if (
            path.is_symlink()
            or not path.is_file()
            or MANIFEST.fullmatch(path.name) is None
            or path.stat().st_size > MAX_JSON_BYTES + 1
        ):
            raise ValidationError("manifest file is unsafe")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ValidationError("manifest cannot be read") from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("manifest is not UTF-8 JSON") from exc
        candidates = parse_research_document(value)
        if len(candidates) != 1:
            raise ValidationError("manifest must contain exactly one candidate")
        normalized = build_research_document(candidates)
        canonical = canonical_json_bytes(normalized)
        if raw != canonical + b"\n" or sha256_hex(canonical) != path.stem:
            raise ValidationError("manifest canonical hash binding is invalid")
        return normalized, candidates[0]

    @staticmethod
    def request_path(path: Path) -> Path:
        return path.with_name(f"{path.stem}.request.json")

    @staticmethod
    def retry_path(path: Path) -> Path:
        return path.with_name(f"{path.stem}.retry.json")

    def persist_request(self, claimed: Claimed, envelope: dict[str, Any]) -> bytes:
        validate_intake_envelope(envelope)
        body = canonical_json_bytes(envelope)
        destination = self.request_path(claimed.path)
        if destination.exists():
            existing = destination.read_bytes()
            if destination.is_symlink() or existing != body:
                raise ValidationError("persisted request content changed")
            return existing
        atomic_write_bytes(destination, body, mode=0o600)
        return body

    @staticmethod
    def _load_request(path: Path) -> dict[str, Any]:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
            raise ValidationError("persisted request file is unsafe")
        raw = path.read_bytes()
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("persisted request is not UTF-8 JSON") from exc
        envelope = validate_intake_envelope(value)
        if raw != canonical_json_bytes(envelope):
            raise ValidationError("persisted request is not canonical")
        return envelope

    def claim_next(self, *, now: datetime | None = None) -> Claimed | None:
        self.ensure()
        reference = _now(now)
        for path in sorted(self.processing.glob("[0-9a-f]*.json")):
            if MANIFEST.fullmatch(path.name) is None:
                continue
            retry_path = self.retry_path(path)
            if retry_path.exists() and self._load_retry(path).next_at > reference:
                continue
            document, candidate = self._load_manifest(path)
            request = self.request_path(path)
            return Claimed(
                path,
                document,
                candidate,
                self._load_request(request) if request.exists() else None,
            )
        for source in sorted(self.ready.glob("[0-9a-f]*.json")):
            if MANIFEST.fullmatch(source.name) is None:
                continue
            document, candidate = self._load_manifest(source)
            destination = self.processing / source.name
            try:
                os.rename(source, destination)
            except FileNotFoundError:
                continue
            self._fsync(self.ready, self.processing)
            return Claimed(destination, document, candidate, None)
        return None

    def defer_discovery(
        self, candidate: Candidate, reason: str, *, now: datetime | None = None
    ) -> bool:
        self.ensure()
        document = self._manifest_document(candidate)
        canonical = canonical_json_bytes(document)
        path = self.deferred / f"{sha256_hex(canonical)}.json"
        if not path.exists():
            atomic_write_bytes(path, canonical + b"\n", mode=0o600)
        if self.record_retry(path, reason, now=now):
            return True
        self._quarantine_file(path, self.quarantine_discovery, "retry_exhausted")
        return False

    def due_discovery(
        self, *, now: datetime | None = None
    ) -> Iterator[tuple[Path, Candidate]]:
        self.ensure()
        reference = _now(now)
        for path in sorted(self.deferred.glob("[0-9a-f]*.json")):
            if MANIFEST.fullmatch(path.name) is None:
                continue
            retry = self._load_retry(path)
            if retry.expires_at <= reference or retry.attempts >= RETRY_MAX_ATTEMPTS:
                self._quarantine_file(
                    path, self.quarantine_discovery, "retry_exhausted"
                )
                continue
            if retry.next_at <= reference:
                _document, candidate = self._load_manifest(path)
                yield path, candidate

    def release_discovery(self, path: Path) -> None:
        path.unlink(missing_ok=True)
        self.retry_path(path).unlink(missing_ok=True)
        self._fsync(self.deferred)

    def quarantine_untrusted(self, raw: bytes, reason: str) -> None:
        self.ensure()
        digest = sha256_hex(raw)
        # Invalid model output can contain person names or personal mailboxes.
        # Keep only a hash/length receipt; never persist those untrusted bytes.
        payload = canonical_json_bytes(
            {
                "schemaVersion": "1",
                "redacted": True,
                "originalSha256": digest,
                "byteLength": len(raw),
            }
        )
        path = self.quarantine_discovery / f"untrusted-{digest}.json"
        if not path.exists():
            atomic_write_bytes(path, payload + b"\n", mode=0o600)
        self._write_error(path, reason)

    def quarantine_discovery_candidate(self, candidate: Candidate, reason: str) -> None:
        self.ensure()
        canonical = canonical_json_bytes(self._manifest_document(candidate))
        path = self.quarantine_discovery / f"{sha256_hex(canonical)}.json"
        if not path.exists():
            atomic_write_bytes(path, canonical + b"\n", mode=0o600)
        self._write_error(path, reason)

    def record_retry(
        self, path: Path, reason: str, *, now: datetime | None = None
    ) -> bool:
        reference = _now(now)
        sidecar = self.retry_path(path)
        if sidecar.exists():
            previous = self._load_retry(path)
            attempts = previous.attempts + 1
            first = previous.first_at
            expires = previous.expires_at
        else:
            attempts = 1
            first = reference
            expires = reference + RETRY_TTL
        if attempts >= RETRY_MAX_ATTEMPTS or reference >= expires:
            return False
        delay = RETRY_DELAYS[min(attempts - 1, len(RETRY_DELAYS) - 1)]
        next_attempt = reference + timedelta(seconds=delay)
        if reason == "remote_http_429":
            next_utc_day = (reference + timedelta(days=1)).replace(
                hour=0, minute=5, second=0, microsecond=0
            )
            next_attempt = max(next_attempt, next_utc_day)
        next_attempt = min(next_attempt, expires - timedelta(seconds=1))
        record = {
            "schemaVersion": "1",
            "attempts": attempts,
            "firstDeferredAt": _iso(first),
            "lastDeferredAt": _iso(reference),
            "nextAttemptAt": _iso(next_attempt),
            "expiresAt": _iso(expires),
            "reasonCode": reason[:80],
        }
        atomic_write_bytes(sidecar, canonical_json_bytes(record) + b"\n", mode=0o600)
        return True

    def _load_retry(self, path: Path) -> Retry:
        sidecar = self.retry_path(path)
        if (
            sidecar.is_symlink()
            or not sidecar.is_file()
            or sidecar.stat().st_size > 4096
        ):
            raise ValidationError("retry record is unsafe")
        try:
            raw = sidecar.read_bytes()
            value = require_object(json.loads(raw.decode("utf-8")), "retry")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("retry record is not UTF-8 JSON") from exc
        require_exact_keys(
            value,
            required={
                "schemaVersion",
                "attempts",
                "firstDeferredAt",
                "lastDeferredAt",
                "nextAttemptAt",
                "expiresAt",
                "reasonCode",
            },
            field="retry",
        )
        if (
            value["schemaVersion"] != "1"
            or not isinstance(value["attempts"], int)
            or isinstance(value["attempts"], bool)
            or not 1 <= value["attempts"] < RETRY_MAX_ATTEMPTS
            or not isinstance(value["reasonCode"], str)
            or len(value["reasonCode"]) > 80
        ):
            raise ValidationError("retry record shape is invalid")
        first = parse_utc_timestamp(value["firstDeferredAt"], "retry.first")
        last = parse_utc_timestamp(value["lastDeferredAt"], "retry.last")
        next_at = parse_utc_timestamp(value["nextAttemptAt"], "retry.next")
        expires = parse_utc_timestamp(value["expiresAt"], "retry.expires")
        if (
            not first <= last < next_at <= expires
            or raw != canonical_json_bytes(value) + b"\n"
        ):
            raise ValidationError("retry record timeline or canonical form is invalid")
        return Retry(value["attempts"], first, next_at, expires, value["reasonCode"])

    def retry_exhausted(self, path: Path, *, now: datetime | None = None) -> bool:
        sidecar = self.retry_path(path)
        if not sidecar.exists():
            return False
        retry = self._load_retry(path)
        return retry.attempts >= RETRY_MAX_ATTEMPTS or retry.expires_at <= _now(now)

    def bind_or_check_identities(
        self,
        digests: tuple[str, ...],
        receipt_id: str,
        manifest_hash: str,
        status: str,
        *,
        now: datetime | None = None,
    ) -> None:
        if self.identity_key_id is None:
            raise ValidationError("identity key must be bound before index access")
        if (
            not digests
            or any(IDENTITY_DIGEST.fullmatch(value) is None for value in digests)
            or RECEIPT_ID.fullmatch(receipt_id) is None
            or status not in {"pending", "accepted", "uncertain"}
        ):
            raise ValidationError("identity index update is invalid")
        index = self._load_identity_index()
        recorded = _iso(_now(now))
        for digest in digests:
            previous = index["entries"].get(digest)
            if previous and previous["receiptId"] != receipt_id:
                raise ValidationError(
                    "candidate identity already has a durable receipt"
                )
            index["entries"][digest] = {
                "receiptId": receipt_id,
                "researchSha256": manifest_hash,
                "status": status,
                "recordedAt": recorded,
            }
        self._write_identity_index(index)

    def identity_matches(
        self, digests: tuple[str, ...], receipt_id: str
    ) -> tuple[str, ...]:
        index = self._load_identity_index()
        return tuple(
            sorted(
                digest
                for digest in digests
                if digest in index["entries"]
                and index["entries"][digest]["receiptId"] != receipt_id
            )
        )

    def _empty_identity_index(self) -> dict[str, Any]:
        if self.identity_key_id is None:
            raise ValidationError("identity key must be bound before index access")
        return {
            "schemaVersion": "1",
            "keyId": self.identity_key_id,
            "contract": "first-receipt-manual-reconciliation-v1",
            "entries": {},
        }

    def _load_identity_index(self) -> dict[str, Any]:
        expected = self._empty_identity_index()
        if not self.identity_index_path.exists():
            return expected
        if (
            self.identity_index_path.is_symlink()
            or not self.identity_index_path.is_file()
            or self.identity_index_path.stat().st_size > 2 * 1024 * 1024
        ):
            raise ValidationError("identity index is unsafe")
        try:
            raw = self.identity_index_path.read_bytes()
            value = require_object(json.loads(raw.decode("utf-8")), "identity index")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("identity index is not UTF-8 JSON") from exc
        require_exact_keys(
            value,
            required={"schemaVersion", "keyId", "contract", "entries"},
            field="identity index",
        )
        if (
            value["schemaVersion"] != "1"
            or value["keyId"] != self.identity_key_id
            or value["contract"] != "first-receipt-manual-reconciliation-v1"
            or not isinstance(value["entries"], dict)
            or raw != canonical_json_bytes(value) + b"\n"
        ):
            raise ValidationError("identity index binding is invalid")
        for digest, raw_entry in value["entries"].items():
            if IDENTITY_DIGEST.fullmatch(digest) is None:
                raise ValidationError("identity index digest is invalid")
            entry = require_object(raw_entry, "identity entry")
            require_exact_keys(
                entry,
                required={"receiptId", "researchSha256", "status", "recordedAt"},
                field="identity entry",
            )
            if (
                RECEIPT_ID.fullmatch(entry["receiptId"] or "") is None
                or not re.fullmatch(r"[0-9a-f]{64}", entry["researchSha256"] or "")
                or entry["status"] not in {"pending", "accepted", "uncertain"}
            ):
                raise ValidationError("identity index entry is invalid")
            parse_utc_timestamp(entry["recordedAt"], "identity recordedAt")
        return value

    def _write_identity_index(self, index: dict[str, Any]) -> None:
        atomic_write_bytes(
            self.identity_index_path,
            canonical_json_bytes(index) + b"\n",
            mode=0o600,
        )

    def mark_processed(
        self,
        claimed: Claimed,
        envelope: dict[str, Any],
        response_result: dict[str, Any],
        digests: tuple[str, ...],
        *,
        now: datetime | None = None,
    ) -> Path:
        reference = _now(now)
        receipt_id = envelope["receipt"]["receiptId"]
        self.bind_or_check_identities(
            digests, receipt_id, claimed.path.stem, "accepted", now=reference
        )
        receipt = {
            "schemaVersion": "1",
            "outcome": "accepted",
            "processedAt": _iso(reference),
            "expiresAt": _iso(reference + RECEIPT_TTL),
            "researchSha256": claimed.path.stem,
            "receiptId": receipt_id,
            "signedBodySha256": sha256_hex(canonical_json_bytes(envelope)),
            "responseSha256": response_result["responseSha256"],
            "identityDigests": list(digests),
        }
        destination = self.processed / f"{claimed.path.stem}.receipt.json"
        atomic_write_bytes(
            destination, canonical_json_bytes(receipt) + b"\n", mode=0o600
        )
        self._delete_processing_bundle(claimed.path.stem)
        return destination

    def mark_duplicate(
        self,
        claimed: Claimed,
        digests: tuple[str, ...],
        *,
        now: datetime | None = None,
    ) -> Path:
        reference = _now(now)
        receipt = {
            "schemaVersion": "1",
            "outcome": "duplicate",
            "processedAt": _iso(reference),
            "expiresAt": _iso(reference + RECEIPT_TTL),
            "researchSha256": claimed.path.stem,
            "receiptId": None,
            "signedBodySha256": None,
            "responseSha256": None,
            "identityDigests": list(digests),
        }
        destination = self.processed / f"{claimed.path.stem}.receipt.json"
        atomic_write_bytes(
            destination, canonical_json_bytes(receipt) + b"\n", mode=0o600
        )
        self._delete_processing_bundle(claimed.path.stem)
        return destination

    def reconcile_uncertain(
        self,
        receipt_id: str,
        *,
        accepted: bool,
        identity_secret: bytes,
        now: datetime | None = None,
    ) -> dict[str, int]:
        if RECEIPT_ID.fullmatch(receipt_id) is None:
            raise ValidationError("reconciliation receipt ID is invalid")
        self.bind_identity_key(identity_secret)
        matches: list[tuple[Path, dict[str, Any], Candidate, dict[str, Any]]] = []
        for manifest in sorted(self.claimed_quarantine.glob("[0-9a-f]*.json")):
            if MANIFEST.fullmatch(manifest.name) is None:
                continue
            request_path = self.claimed_quarantine / f"{manifest.stem}.request.json"
            if not request_path.exists():
                continue
            document, candidate = self._load_manifest(manifest)
            envelope = self._load_request(request_path)
            if envelope["receipt"]["receiptId"] == receipt_id:
                matches.append((manifest, document, candidate, envelope))
        if len(matches) != 1:
            raise ValidationError(
                "reconciliation requires exactly one uncertain bundle"
            )
        manifest, _document, candidate, envelope = matches[0]
        digests = self.identity_digests(candidate, identity_secret)
        index = self._load_identity_index()
        indexed = {
            digest
            for digest in digests
            if digest in index["entries"]
            and index["entries"][digest]["receiptId"] == receipt_id
            and index["entries"][digest]["status"] == "uncertain"
        }
        if indexed != set(digests):
            raise ValidationError("uncertain identity index does not match the bundle")
        if accepted:
            self.bind_or_check_identities(
                digests, receipt_id, manifest.stem, "accepted", now=now
            )
            reference = _now(now)
            receipt = {
                "schemaVersion": "1",
                "outcome": "reconciled_accepted",
                "processedAt": _iso(reference),
                "expiresAt": _iso(reference + RECEIPT_TTL),
                "researchSha256": manifest.stem,
                "receiptId": receipt_id,
                "signedBodySha256": sha256_hex(canonical_json_bytes(envelope)),
                "responseSha256": None,
                "identityDigests": list(digests),
            }
            atomic_write_bytes(
                self.processed / f"{manifest.stem}.receipt.json",
                canonical_json_bytes(receipt) + b"\n",
                mode=0o600,
            )
            outcome = "accepted"
        else:
            for digest in digests:
                del index["entries"][digest]
            self._write_identity_index(index)
            destination = self.ready / manifest.name
            if destination.exists():
                if destination.read_bytes() != manifest.read_bytes():
                    raise ValidationError(
                        "ready manifest conflicts during reconciliation"
                    )
            else:
                os.replace(manifest, destination)
            outcome = "requeued"
        for suffix in (".json", ".request.json", ".retry.json", ".error.json"):
            (self.claimed_quarantine / f"{manifest.stem}{suffix}").unlink(
                missing_ok=True
            )
        self._fsync(self.claimed_quarantine, self.ready, self.processed, self.state)
        return {outcome: 1}

    def quarantine_claimed(
        self, claimed: Claimed, reason: str, *, uncertain: bool
    ) -> None:
        self.ensure()
        for source in (
            claimed.path,
            self.request_path(claimed.path),
            self.retry_path(claimed.path),
        ):
            if not source.exists():
                continue
            destination = self.claimed_quarantine / source.name
            os.replace(source, destination)
        marker = self.claimed_quarantine / f"{claimed.path.stem}.error.json"
        atomic_write_bytes(
            marker,
            canonical_json_bytes(
                {
                    "schemaVersion": "1",
                    "reasonCode": reason[:80],
                    "uncertainRemoteOutcome": uncertain,
                    "quarantinedAt": _iso(_now()),
                }
            )
            + b"\n",
            mode=0o600,
        )
        self._fsync(self.processing, self.claimed_quarantine)

    def _delete_processing_bundle(self, digest: str) -> None:
        for suffix in (".json", ".request.json", ".retry.json", ".error.json"):
            (self.processing / f"{digest}{suffix}").unlink(missing_ok=True)
        self._fsync(self.processing)

    def open_circuit(self, reason: str, detail_hash: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", detail_hash):
            raise ValidationError("circuit detail must be hash-only")
        if self.circuit_path.exists():
            self.assert_circuit_closed()
        atomic_write_bytes(
            self.circuit_path,
            canonical_json_bytes(
                {
                    "schemaVersion": "1",
                    "openedAt": _iso(_now()),
                    "reasonCode": reason[:80],
                    "detailSha256": detail_hash,
                }
            )
            + b"\n",
            mode=0o600,
        )

    def assert_circuit_closed(self) -> None:
        if self.circuit_path.exists():
            raise ValidationError(
                "global B2B discovery submit circuit is open; reconcile before retry"
            )

    def clear_circuit(self) -> bool:
        if not self.circuit_path.exists():
            return False
        if self.circuit_path.is_symlink() or not self.circuit_path.is_file():
            raise ValidationError("global circuit marker is unsafe")
        self.circuit_path.unlink()
        self._fsync(self.state)
        return True

    def _write_error(self, path: Path, reason: str) -> None:
        marker = path.with_name(f"{path.stem}.error.json")
        atomic_write_bytes(
            marker,
            canonical_json_bytes(
                {
                    "schemaVersion": "1",
                    "reasonCode": reason[:80],
                    "quarantinedAt": _iso(_now()),
                }
            )
            + b"\n",
            mode=0o600,
        )

    def _quarantine_file(self, path: Path, target: Path, reason: str) -> None:
        retry = self.retry_path(path)
        destination = target / path.name
        os.replace(path, destination)
        if retry.exists():
            os.replace(retry, target / retry.name)
        self._write_error(destination, reason)
        self._fsync(path.parent, target)

    def purge(self, scope: str, *, now: datetime | None = None) -> dict[str, int]:
        self.ensure()
        reference = _now(now)
        raw_cutoff = reference.timestamp() - RAW_TTL.total_seconds()
        removed_raw = removed_receipts = removed_temp = 0
        roots: tuple[Path, ...]
        if scope == "discovery":
            roots = (self.ready, self.deferred, self.quarantine_discovery)
        elif scope == "submit":
            roots = (self.claimed_quarantine,)
        else:
            raise ValidationError("purge scope is invalid")
        for root in roots:
            for path in tuple(root.iterdir()):
                if path.is_symlink() or not path.is_file():
                    continue
                if path.stat().st_mtime >= raw_cutoff:
                    continue
                path.unlink(missing_ok=True)
                removed_raw += 1
        if scope == "submit":
            for path in tuple(self.processed.glob("*.receipt.json")):
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.stat().st_size > 32 * 1024
                ):
                    continue
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    expires = parse_utc_timestamp(
                        value["expiresAt"], "receipt.expiresAt"
                    )
                except (
                    OSError,
                    KeyError,
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    ValidationError,
                ):
                    continue
                if expires <= reference:
                    path.unlink()
                    removed_receipts += 1
        for root in roots:
            for path in tuple(root.glob(".*.tmp")):
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and path.stat().st_mtime < raw_cutoff
                ):
                    path.unlink()
                    removed_temp += 1
        return {
            "raw": removed_raw,
            "receipts": removed_receipts,
            "temporary": removed_temp,
        }
