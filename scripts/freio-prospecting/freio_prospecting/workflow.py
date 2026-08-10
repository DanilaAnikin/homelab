from __future__ import annotations

import fcntl
import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .common import (
    MAX_JSON_BYTES,
    SHA256_HEX,
    ValidationError,
    atomic_write_bytes,
    canonical_json_bytes,
    parse_utc_timestamp,
    require_exact_keys,
    require_object,
    sha256_hex,
    utc_now_iso,
)
from .schema import (
    ResearchCandidate,
    build_research_document,
    parse_research_document,
    validate_signed_intake_request,
)


BATCH_FILENAME = re.compile(r"^([0-9a-f]{64})\.json$")
IDENTITY_DIGEST = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
RECEIPT_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
ATOMIC_TEMP_FILENAME = re.compile(r"^\..+\.[0-9]+(?:\.[0-9a-f]{16})?\.tmp$")
PROCESSING_SIDECAR = re.compile(r"^([0-9a-f]{64})\.(request|retry|success)\.json$")

RAW_ARTIFACT_TTL = timedelta(hours=72)
RETRY_MAX_AGE = timedelta(hours=24)
RETRY_MAX_ATTEMPTS = 5
RETRY_DELAYS_SECONDS = (300, 900, 3600, 10_800)
PROCESSED_RECEIPT_TTL = timedelta(days=30)
IDENTITY_INDEX_CONTRACT = "first-accepted/manual-update-v1"


@dataclass(frozen=True)
class ClaimedManifest:
    path: Path
    document: dict[str, Any]
    candidates: tuple[ResearchCandidate, ...]


@dataclass(frozen=True)
class ProcessingWork:
    claimed: ClaimedManifest
    signed_request: dict[str, Any] | None


@dataclass(frozen=True)
class RetryRecord:
    attempts: int
    first_deferred_at: datetime
    next_attempt_at: datetime
    expires_at: datetime
    reason_code: str


def _as_utc(value: datetime | None = None) -> datetime:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


class Spool:
    DIRECTORY_MODES = {
        "ready": 0o770,
        "processing": 0o700,
        "processed": 0o700,
        "state": 0o700,
        "deferred": 0o750,
        "deferred/untrusted": 0o700,
        "quarantine": 0o750,
        "quarantine/untrusted": 0o700,
        "quarantine/claimed": 0o700,
    }

    def __init__(self, root: Path) -> None:
        # Keep the lexical path so ensure() can reject a symlink instead of
        # silently resolving through it.
        self.root = Path(os.path.abspath(root))
        self.ready = self.root / "ready"
        self.processing = self.root / "processing"
        self.processed = self.root / "processed"
        self.state = self.root / "state"
        self.deferred = self.root / "deferred"
        self.untrusted_deferred = self.deferred / "untrusted"
        self.quarantine = self.root / "quarantine"
        self.untrusted_quarantine = self.quarantine / "untrusted"
        self.claimed_quarantine = self.quarantine / "claimed"
        self.lock_path = self.processing / ".submit.lock"
        self.identity_index_path = self.state / "accepted-identities-v1.json"
        self.global_circuit_path = self.state / "global-submit-circuit-v1.json"
        self.identity_key_id: str | None = None

    def bind_identity_key(self, secret: bytes) -> str:
        if len(secret) < 32:
            raise ValidationError("identity index credential is too short")
        key_id = (
            "v1:" + sha256_hex(b"freio-prospect-identity-key-id-v1\0" + secret)[:16]
        )
        if self.identity_key_id is not None and self.identity_key_id != key_id:
            raise ValidationError("spool is already bound to another identity key")
        self.identity_key_id = key_id
        return key_id

    def ensure(self) -> None:
        self.root.mkdir(mode=0o750, parents=True, exist_ok=True)
        self._require_real_directory(self.root)
        if self.root.lstat().st_mode & 0o022:
            raise ValidationError("spool root must not be writable by group or others")
        for name, mode in self.DIRECTORY_MODES.items():
            directory = self.root / name
            directory.mkdir(mode=mode, exist_ok=True)
            self._require_real_directory(directory)
            permissions = directory.lstat().st_mode & 0o777
            if (
                name
                in {
                    "processing",
                    "processed",
                    "state",
                    "deferred/untrusted",
                    "quarantine/untrusted",
                    "quarantine/claimed",
                }
                and permissions & 0o077
            ):
                raise ValidationError(
                    f"private spool directory exposes group/other access: {directory}"
                )
            if name in {"deferred", "quarantine"} and permissions & 0o027:
                raise ValidationError(f"spool root has unsafe permissions: {directory}")

    @staticmethod
    def _require_real_directory(path: Path) -> None:
        try:
            stat_result = path.lstat()
        except OSError as exc:
            raise ValidationError(
                f"cannot inspect spool directory {path}: {exc}"
            ) from exc
        if path.is_symlink() or not path.is_dir():
            raise ValidationError(f"spool path is not a real directory: {path}")
        if stat_result.st_mode & 0o002:
            raise ValidationError(f"spool directory must not be world-writable: {path}")

    @staticmethod
    def _fsync_directories(*directories: Path) -> None:
        for directory in directories:
            descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

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

    def enqueue_research(self, document: dict[str, Any]) -> Path:
        self.ensure()
        candidates = parse_research_document(document)
        if len(candidates) != 1:
            raise ValidationError(
                "each ready manifest must contain exactly one candidate"
            )
        normalized = build_research_document(candidates)
        canonical = canonical_json_bytes(normalized)
        manifest_hash = sha256_hex(canonical)
        serialized = canonical + b"\n"
        destination = self.ready / f"{manifest_hash}.json"
        if destination.exists():
            try:
                existing = destination.read_bytes()
            except OSError as exc:
                raise ValidationError("cannot inspect existing ready manifest") from exc
            if destination.is_symlink() or existing != serialized:
                raise ValidationError("content-address collision in ready spool")
            return destination
        atomic_write_bytes(destination, serialized)
        return destination

    def quarantine_untrusted(self, raw: bytes, code: str, detail: str) -> Path:
        self.ensure()
        digest = sha256_hex(raw)
        if len(raw) > MAX_JSON_BYTES:
            raw = canonical_json_bytes(
                {
                    "oversized": True,
                    "originalByteLength": len(raw),
                    "originalSha256": digest,
                }
            )
        destination = self.untrusted_quarantine / f"untrusted-{digest}.json"
        if not destination.exists():
            atomic_write_bytes(destination, raw if raw.endswith(b"\n") else raw + b"\n")
        self._write_sidecar(
            destination,
            "error",
            {"code": code[:80], "detail": detail[:500], "quarantinedAt": utc_now_iso()},
        )
        return destination

    def defer_untrusted(
        self,
        document: dict[str, Any],
        code: str,
        *,
        now: datetime | None = None,
    ) -> Path | None:
        self.ensure()
        candidates = parse_research_document(document)
        if len(candidates) != 1:
            raise ValidationError(
                "deferred manifest must contain exactly one candidate"
            )
        canonical = canonical_json_bytes(build_research_document(candidates))
        destination = self.untrusted_deferred / f"{sha256_hex(canonical)}.json"
        if not destination.exists():
            atomic_write_bytes(destination, canonical + b"\n", mode=0o600)
        if not self._record_retry(destination, code, now=now):
            self._quarantine_deferred_untrusted(destination, "retry_exhausted")
            return None
        return destination

    def iter_due_untrusted(
        self, *, now: datetime | None = None
    ) -> Iterator[tuple[Path, dict[str, Any], tuple[ResearchCandidate, ...]]]:
        self.ensure()
        reference = _as_utc(now)
        for path in sorted(self.untrusted_deferred.glob("[0-9a-f]*.json")):
            if BATCH_FILENAME.fullmatch(path.name) is None:
                continue
            try:
                retry = self._load_retry(path)
                if (
                    retry.expires_at <= reference
                    or retry.attempts >= RETRY_MAX_ATTEMPTS
                ):
                    self._quarantine_deferred_untrusted(path, "retry_exhausted")
                    continue
                if retry.next_attempt_at > reference:
                    continue
                document, candidates = self._load_and_verify(path)
            except ValidationError as exc:
                self._quarantine_deferred_untrusted(path, "invalid_deferred", str(exc))
                continue
            yield path, document, candidates

    def retry_untrusted(
        self, path: Path, code: str, *, now: datetime | None = None
    ) -> bool:
        if self._record_retry(path, code, now=now):
            return True
        self._quarantine_deferred_untrusted(path, "retry_exhausted")
        return False

    def release_untrusted(self, path: Path) -> None:
        retry_path = self.retry_path(path)
        path.unlink(missing_ok=True)
        retry_path.unlink(missing_ok=True)
        self._fsync_directories(self.untrusted_deferred)

    def _quarantine_deferred_untrusted(
        self,
        path: Path,
        code: str,
        detail: str = "transient evidence retries exhausted",
    ) -> Path:
        raw = path.read_bytes() if path.exists() else b"{}"
        destination = self.quarantine_untrusted(raw.rstrip(b"\n"), code, detail)
        path.unlink(missing_ok=True)
        self.retry_path(path).unlink(missing_ok=True)
        return destination

    def claim_next(self) -> ClaimedManifest | None:
        self.ensure()
        for source in sorted(self.ready.iterdir(), key=lambda path: path.name):
            if (
                source.is_symlink()
                or not source.is_file()
                or BATCH_FILENAME.fullmatch(source.name) is None
            ):
                continue
            destination = self.processing / source.name
            if destination.exists():
                continue
            try:
                os.rename(source, destination)
                self._fsync_directories(self.ready, self.processing)
            except FileNotFoundError:
                continue
            try:
                document, candidates = self._load_and_verify(destination)
            except ValidationError as exc:
                self.quarantine_claimed(destination, "invalid_batch", str(exc))
                continue
            atomic_write_bytes(
                destination, canonical_json_bytes(document) + b"\n", mode=0o600
            )
            return ClaimedManifest(destination, document, candidates)
        return None

    def processing_next(self, *, now: datetime | None = None) -> ProcessingWork | None:
        self.ensure()
        self._reconcile_completed()
        self._reconcile_quarantine_transitions()
        self._reconcile_orphan_sidecars()
        reference = _as_utc(now)
        for path in sorted(self.processing.iterdir(), key=lambda item: item.name):
            if (
                path.is_symlink()
                or not path.is_file()
                or BATCH_FILENAME.fullmatch(path.name) is None
            ):
                continue
            try:
                retry_path = self.retry_path(path)
                if retry_path.exists():
                    retry = self._load_retry(path)
                    if retry.next_attempt_at > reference:
                        continue
                document, candidates = self._load_and_verify(path)
                claimed = ClaimedManifest(path, document, candidates)
                request_path = self.request_path(claimed)
                if not request_path.exists():
                    atomic_write_bytes(
                        path, canonical_json_bytes(document) + b"\n", mode=0o600
                    )
                    return ProcessingWork(claimed, None)
                signed_request = self._load_signed_request(request_path)
                if signed_request["receipt"]["researchManifestSha256"] != path.stem:
                    raise ValidationError(
                        "persisted request is not bound to its research manifest"
                    )
                return ProcessingWork(claimed, signed_request)
            except ValidationError as exc:
                self.quarantine_claimed(path, "invalid_processing_state", str(exc))
                continue
        return None

    def _load_and_verify(
        self, path: Path
    ) -> tuple[dict[str, Any], tuple[ResearchCandidate, ...]]:
        if path.is_symlink() or not path.is_file():
            raise ValidationError("claimed artifact is not a regular file")
        raw = path.read_bytes()
        if len(raw) > 256 * 1024:
            raise ValidationError("claimed artifact is too large")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("claimed artifact is not valid UTF-8 JSON") from exc
        candidates = parse_research_document(value)
        if len(candidates) != 1:
            raise ValidationError("claimed manifest must contain exactly one candidate")
        document = build_research_document(candidates)
        canonical = canonical_json_bytes(document)
        if raw != canonical + b"\n":
            raise ValidationError("research manifest is not in canonical form")
        match = BATCH_FILENAME.fullmatch(path.name)
        if not match or match.group(1) != sha256_hex(canonical):
            raise ValidationError(
                "artifact filename does not match research content hash"
            )
        return document, candidates

    @staticmethod
    def request_path(claimed: ClaimedManifest) -> Path:
        return claimed.path.with_name(f"{claimed.path.stem}.request.json")

    @staticmethod
    def success_path(claimed: ClaimedManifest) -> Path:
        return claimed.path.with_name(f"{claimed.path.stem}.success.json")

    @staticmethod
    def retry_path(path: Path) -> Path:
        return path.with_name(f"{path.stem}.retry.json")

    def persist_signed_request(
        self, claimed: ClaimedManifest, request: dict[str, Any]
    ) -> bytes:
        validated = validate_signed_intake_request(request)
        if len(validated["items"]) != 1:
            raise ValidationError("worker request must contain exactly one item")
        if validated["receipt"]["researchManifestSha256"] != claimed.path.stem:
            raise ValidationError(
                "signed request is not bound to its research manifest"
            )
        body = canonical_json_bytes(validated)
        destination = self.request_path(claimed)
        if destination.exists():
            existing = destination.read_bytes()
            if destination.is_symlink() or existing != body + b"\n":
                raise ValidationError("persisted signed request collision")
            return body
        atomic_write_bytes(destination, body + b"\n", mode=0o600)
        return body

    @staticmethod
    def _load_signed_request(path: Path) -> dict[str, Any]:
        if path.is_symlink() or not path.is_file():
            raise ValidationError("persisted request is not a regular file")
        raw = path.read_bytes()
        if len(raw) > 96 * 1024 + 1:
            raise ValidationError("persisted request exceeds 96 KiB")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("persisted request is not UTF-8 JSON") from exc
        validated = validate_signed_intake_request(value)
        if len(validated["items"]) != 1:
            raise ValidationError("persisted request must contain exactly one item")
        if raw != canonical_json_bytes(validated) + b"\n":
            raise ValidationError("persisted request is not canonical")
        return validated

    def defer_claimed(
        self, path: Path, code: str, *, now: datetime | None = None
    ) -> bool:
        return self._record_retry(path, code, now=now)

    def retry_exhausted(self, path: Path, *, now: datetime | None = None) -> bool:
        try:
            retry = self._load_retry(path)
        except FileNotFoundError:
            return False
        reference = _as_utc(now)
        return retry.attempts >= RETRY_MAX_ATTEMPTS or retry.expires_at <= reference

    def mark_processed(
        self,
        claimed: ClaimedManifest,
        result: dict[str, Any],
        signed_request: dict[str, Any],
        identity_digests: tuple[str, ...],
        *,
        now: datetime | None = None,
    ) -> Path:
        validate_signed_intake_request(signed_request)
        if len(signed_request["items"]) != 1:
            raise ValidationError("worker success must contain exactly one item")
        self._validate_identity_digests(identity_digests)
        request_source = self.request_path(claimed)
        if not request_source.exists():
            raise ValidationError(
                "cannot finish processing without persisted request bytes"
            )
        reference = _as_utc(now)
        result_receipt = {
            "schemaVersion": "1",
            "outcome": "accepted",
            "processedAt": _iso(reference),
            "expiresAt": _iso(reference + PROCESSED_RECEIPT_TTL),
            "retentionSeconds": int(PROCESSED_RECEIPT_TTL.total_seconds()),
            "researchSha256": claimed.path.stem,
            "receiptId": signed_request["receipt"]["receiptId"],
            "signedBodySha256": sha256_hex(canonical_json_bytes(signed_request)),
            "resultSha256": sha256_hex(canonical_json_bytes(result)),
            "result": result,
            "identityDigests": list(sorted(identity_digests)),
            "identityContract": IDENTITY_INDEX_CONTRACT,
        }
        atomic_write_bytes(
            self.success_path(claimed),
            canonical_json_bytes(result_receipt) + b"\n",
            mode=0o600,
        )
        return self._complete_success(claimed.path.stem)

    def mark_duplicate(
        self,
        claimed: ClaimedManifest,
        matched_identity_digests: tuple[str, ...],
        *,
        now: datetime | None = None,
    ) -> Path:
        self._validate_identity_digests(matched_identity_digests)
        existing = self.processed / f"{claimed.path.stem}.receipt.json"
        if existing.exists():
            self._validate_existing_processed_receipt(existing, claimed.path.stem)
            self._delete_processing_bundle(claimed.path.stem)
            return existing
        reference = _as_utc(now)
        receipt = {
            "schemaVersion": "1",
            "outcome": "duplicate",
            "processedAt": _iso(reference),
            "expiresAt": _iso(reference + PROCESSED_RECEIPT_TTL),
            "retentionSeconds": int(PROCESSED_RECEIPT_TTL.total_seconds()),
            "researchSha256": claimed.path.stem,
            "receiptId": None,
            "signedBodySha256": None,
            "resultSha256": sha256_hex(canonical_json_bytes({"duplicate": True})),
            "result": {"duplicate": True},
            "identityDigests": list(sorted(matched_identity_digests)),
            "identityContract": IDENTITY_INDEX_CONTRACT,
        }
        destination = self._write_processed_receipt(claimed.path.stem, receipt)
        self._delete_processing_bundle(claimed.path.stem)
        return destination

    def discard_if_processed(self, claimed: ClaimedManifest) -> bool:
        existing = self.processed / f"{claimed.path.stem}.receipt.json"
        if not existing.exists():
            return False
        self._validate_existing_processed_receipt(existing, claimed.path.stem)
        self._delete_processing_bundle(claimed.path.stem)
        return True

    @staticmethod
    def _validate_existing_processed_receipt(path: Path, research_hash: str) -> None:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024:
            raise ValidationError("existing processed receipt is unsafe")
        try:
            value = require_object(
                json.loads(path.read_text(encoding="utf-8")), "processed receipt"
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("existing processed receipt is invalid") from exc
        if (
            value.get("schemaVersion") != "1"
            or value.get("researchSha256") != research_hash
            or value.get("outcome") not in {"accepted", "duplicate"}
        ):
            raise ValidationError("existing processed receipt binding is invalid")
        parse_utc_timestamp(value.get("expiresAt"), "processed receipt.expiresAt")

    def accepted_identity_matches(
        self,
        identity_digests: tuple[str, ...],
        *,
        exclude_receipt_id: str | None = None,
    ) -> tuple[str, ...]:
        self._validate_identity_digests(identity_digests)
        index = self._load_identity_index()
        entries = index["entries"]
        return tuple(
            sorted(
                digest
                for digest in identity_digests
                if digest in entries
                and entries[digest]["receiptId"] != exclude_receipt_id
            )
        )

    def record_pending_identities(
        self,
        identity_digests: tuple[str, ...],
        receipt_id: str,
        research_hash: str,
        *,
        now: datetime | None = None,
    ) -> None:
        self._record_identity_status(
            identity_digests,
            receipt_id,
            research_hash,
            "pending",
            _iso(_as_utc(now)),
        )

    def mark_identities_uncertain(
        self,
        identity_digests: tuple[str, ...],
        receipt_id: str,
        research_hash: str,
        *,
        now: datetime | None = None,
    ) -> None:
        self._record_identity_status(
            identity_digests,
            receipt_id,
            research_hash,
            "uncertain",
            _iso(_as_utc(now)),
        )

    def clear_pending_identities(
        self, identity_digests: tuple[str, ...], receipt_id: str
    ) -> None:
        self._validate_identity_digests(identity_digests)
        index = self._load_identity_index()
        entries = index["entries"]
        changed = False
        for digest in identity_digests:
            entry = entries.get(digest)
            if (
                entry
                and entry["receiptId"] == receipt_id
                and entry["status"] == "pending"
            ):
                del entries[digest]
                changed = True
        if changed:
            atomic_write_bytes(
                self.identity_index_path,
                canonical_json_bytes(index) + b"\n",
                mode=0o600,
            )

    def convert_pending_research_to_uncertain(
        self, research_hash: str, *, now: datetime | None = None
    ) -> int:
        if SHA256_HEX.fullmatch(research_hash) is None:
            raise ValidationError("pending research hash is invalid")
        if not self.identity_index_path.exists():
            return 0
        index = self._load_identity_index()
        recorded_at = _iso(_as_utc(now))
        changed = 0
        for entry in index["entries"].values():
            if (
                entry["researchSha256"] == research_hash
                and entry["status"] == "pending"
            ):
                entry["status"] = "uncertain"
                entry["recordedAt"] = recorded_at
                changed += 1
        if changed:
            atomic_write_bytes(
                self.identity_index_path,
                canonical_json_bytes(index) + b"\n",
                mode=0o600,
            )
        return changed

    def reconcile_uncertain_receipt(
        self, receipt_id: str, *, accepted: bool, now: datetime | None = None
    ) -> dict[str, int]:
        """Apply an operator's DB-backed decision and optionally requeue raw input."""
        if RECEIPT_ID.fullmatch(receipt_id) is None:
            raise ValidationError("reconciliation receipt ID is invalid")
        journal_path = self.state / (
            f"reconcile-{sha256_hex(receipt_id.encode('ascii'))}.json"
        )
        existing_journal: dict[str, Any] | None = None
        if journal_path.exists():
            try:
                existing_journal = require_object(
                    json.loads(journal_path.read_text(encoding="utf-8")),
                    "reconciliation journal",
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("reconciliation journal is invalid") from exc
            require_exact_keys(
                existing_journal,
                required={
                    "schemaVersion",
                    "receiptId",
                    "accepted",
                    "identityCount",
                    "researchHashes",
                    "createdAt",
                },
                field="reconciliation journal",
            )
            if (
                existing_journal["schemaVersion"] != "1"
                or existing_journal["receiptId"] != receipt_id
                or existing_journal["accepted"] is not accepted
            ):
                raise ValidationError("reconciliation journal decision mismatch")
        index = self._load_identity_index()
        entries = index["entries"]
        matched = [
            (digest, entry)
            for digest, entry in entries.items()
            if entry["receiptId"] == receipt_id
        ]
        if matched and any(entry["status"] != "uncertain" for _, entry in matched):
            if not (
                existing_journal
                and accepted
                and all(entry["status"] == "accepted" for _, entry in matched)
            ):
                raise ValidationError(
                    "receipt has no exclusively uncertain identity set"
                )
        if not matched and existing_journal is None:
            raise ValidationError("receipt has no exclusively uncertain identity set")
        research_hashes = sorted(
            {entry["researchSha256"] for _, entry in matched}
            or set(existing_journal["researchHashes"] if existing_journal else [])
        )
        identity_count = (
            len(matched)
            if matched
            else int(existing_journal["identityCount"] if existing_journal else 0)
        )
        artifacts: list[tuple[Path, str, bytes]] = []
        for manifest in list(self.claimed_quarantine.glob("[0-9a-f]*.json")):
            if any(
                marker in manifest.name
                for marker in (".request.", ".retry.", ".success.", ".error.")
            ):
                continue
            request_path = manifest.with_name(f"{manifest.stem}.request.json")
            if not request_path.exists():
                continue
            request = self._load_signed_request(request_path)
            if request["receipt"]["receiptId"] != receipt_id:
                continue
            research_hash = request["receipt"]["researchManifestSha256"]
            if accepted:
                artifacts.append((manifest, research_hash, b""))
                continue
            raw = manifest.read_bytes()
            try:
                parsed = json.loads(raw.decode("utf-8"))
                candidates = parse_research_document(parsed)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("reconciliation manifest is invalid") from exc
            canonical = canonical_json_bytes(build_research_document(candidates))
            if len(candidates) != 1 or sha256_hex(canonical) != research_hash:
                raise ValidationError("reconciliation manifest binding is invalid")
            artifacts.append((manifest, research_hash, canonical + b"\n"))
        if not accepted:
            for research_hash in research_hashes:
                matching = [item for item in artifacts if item[1] == research_hash]
                destination = self.ready / f"{research_hash}.json"
                if not matching and not destination.exists():
                    raise ValidationError(
                        "not-accepted reconciliation has no recoverable raw manifest"
                    )
                if destination.exists():
                    expected = matching[0][2] if matching else destination.read_bytes()
                    if destination.read_bytes() != expected:
                        raise ValidationError("reconciliation ready collision")
        if existing_journal is None:
            journal = {
                "schemaVersion": "1",
                "receiptId": receipt_id,
                "accepted": accepted,
                "identityCount": identity_count,
                "researchHashes": research_hashes,
                "createdAt": _iso(_as_utc(now)),
            }
            atomic_write_bytes(
                journal_path, canonical_json_bytes(journal) + b"\n", mode=0o600
            )
        requeued = 0
        for manifest, research_hash, serialized in artifacts:
            if not accepted:
                destination = self.ready / f"{research_hash}.json"
                if not destination.exists():
                    atomic_write_bytes(destination, serialized)
                requeued += 1
            for suffix in (
                ".json",
                ".request.json",
                ".success.json",
                ".retry.json",
                ".error.json",
            ):
                (self.claimed_quarantine / f"{manifest.stem}{suffix}").unlink(
                    missing_ok=True
                )
        if matched:
            if accepted:
                recorded_at = _iso(_as_utc(now))
                for _digest, entry in matched:
                    entry["status"] = "accepted"
                    entry["recordedAt"] = recorded_at
            else:
                for digest, _entry in matched:
                    del entries[digest]
            atomic_write_bytes(
                self.identity_index_path,
                canonical_json_bytes(index) + b"\n",
                mode=0o600,
            )
        journal_path.unlink(missing_ok=True)
        self._fsync_directories(self.state, self.claimed_quarantine, self.ready)
        return {"identities": identity_count, "requeued": requeued}

    def assert_no_reconciliation_journal(self) -> None:
        if any(self.state.glob("reconcile-*.json")):
            raise ValidationError(
                "unfinished operator reconciliation must be resumed before submit"
            )

    def open_global_circuit(self, code: str, evidence_sha256: str) -> None:
        if SHA256_HEX.fullmatch(evidence_sha256) is None:
            raise ValidationError("global circuit evidence hash is invalid")
        marker = {
            "schemaVersion": "1",
            "code": re.sub(r"[^a-z0-9_]", "_", code.lower())[:80],
            "evidenceSha256": evidence_sha256,
            "openedAt": utc_now_iso(),
        }
        if self.global_circuit_path.exists():
            return
        atomic_write_bytes(
            self.global_circuit_path,
            canonical_json_bytes(marker) + b"\n",
            mode=0o600,
        )

    def assert_global_circuit_closed(self) -> None:
        if self.global_circuit_path.exists():
            raise ValidationError(
                "global submit circuit is open; fix and explicitly clear it before retry"
            )

    def clear_global_circuit(self) -> bool:
        existed = self.global_circuit_path.exists()
        self.global_circuit_path.unlink(missing_ok=True)
        if existed:
            self._fsync_directories(self.state)
        return existed

    def _record_accepted_identities(
        self,
        identity_digests: tuple[str, ...],
        receipt_id: str,
        research_hash: str,
        accepted_at: str,
    ) -> None:
        self._record_identity_status(
            identity_digests,
            receipt_id,
            research_hash,
            "accepted",
            accepted_at,
        )

    def _record_identity_status(
        self,
        identity_digests: tuple[str, ...],
        receipt_id: str,
        research_hash: str,
        status: str,
        recorded_at: str,
    ) -> None:
        self._validate_identity_digests(identity_digests)
        if RECEIPT_ID.fullmatch(receipt_id) is None:
            raise ValidationError("accepted receipt ID is invalid")
        if SHA256_HEX.fullmatch(research_hash) is None:
            raise ValidationError("identity research hash is invalid")
        if status not in {"pending", "accepted", "uncertain"}:
            raise ValidationError("identity status is invalid")
        parse_utc_timestamp(recorded_at, "identity recordedAt")
        index = self._load_identity_index()
        entries = index["entries"]
        for digest in identity_digests:
            existing = entries.get(digest)
            if existing is None:
                entries[digest] = {
                    "recordedAt": recorded_at,
                    "receiptId": receipt_id,
                    "researchSha256": research_hash,
                    "status": status,
                }
            elif existing["receiptId"] == receipt_id:
                if existing["researchSha256"] != research_hash:
                    raise ValidationError("identity receipt research binding changed")
                if status in {"accepted", "uncertain"}:
                    existing["status"] = status
                    existing["recordedAt"] = recorded_at
        atomic_write_bytes(
            self.identity_index_path,
            canonical_json_bytes(index) + b"\n",
            mode=0o600,
        )

    def _load_identity_index(self) -> dict[str, Any]:
        if not self.identity_index_path.exists():
            return {
                "schemaVersion": "1",
                "contract": IDENTITY_INDEX_CONTRACT,
                "keyId": self._required_identity_key_id(),
                "entries": {},
            }
        if (
            self.identity_index_path.is_symlink()
            or not self.identity_index_path.is_file()
        ):
            raise ValidationError("identity index is not a regular file")
        raw = self.identity_index_path.read_bytes()
        if len(raw) > 16 * 1024 * 1024:
            raise ValidationError("identity index exceeds 16 MiB")
        try:
            value = require_object(json.loads(raw.decode("utf-8")), "identity index")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("identity index is not UTF-8 JSON") from exc
        require_exact_keys(
            value,
            required={"schemaVersion", "contract", "keyId", "entries"},
            field="identity index",
        )
        if (
            value["schemaVersion"] != "1"
            or value["contract"] != IDENTITY_INDEX_CONTRACT
            or value["keyId"] != self._required_identity_key_id()
        ):
            raise ValidationError("identity index contract is unsupported")
        entries = require_object(value["entries"], "identity index.entries")
        if len(entries) > 100_000:
            raise ValidationError("identity index has too many entries")
        for digest, raw_entry in entries.items():
            if IDENTITY_DIGEST.fullmatch(digest) is None:
                raise ValidationError("identity index contains an invalid digest")
            entry = require_object(raw_entry, "identity index entry")
            require_exact_keys(
                entry,
                required={"recordedAt", "receiptId", "researchSha256", "status"},
                field="identity index entry",
            )
            parse_utc_timestamp(entry["recordedAt"], "identity index entry.recordedAt")
            if (
                entry["status"] not in {"pending", "accepted", "uncertain"}
                or not isinstance(entry["receiptId"], str)
                or RECEIPT_ID.fullmatch(entry["receiptId"]) is None
                or not isinstance(entry["researchSha256"], str)
                or SHA256_HEX.fullmatch(entry["researchSha256"]) is None
            ):
                raise ValidationError("identity index entry receipt is invalid")
        if raw != canonical_json_bytes(value) + b"\n":
            raise ValidationError("identity index is not canonical")
        return value

    @staticmethod
    def _validate_identity_digests(identity_digests: tuple[str, ...]) -> None:
        if (
            not 1 <= len(identity_digests) <= 2
            or len(set(identity_digests)) != len(identity_digests)
            or any(IDENTITY_DIGEST.fullmatch(item) is None for item in identity_digests)
        ):
            raise ValidationError("identity digest set is invalid")

    def _required_identity_key_id(self) -> str:
        if self.identity_key_id is None:
            raise ValidationError("identity index credential is not bound")
        return self.identity_key_id

    def _complete_success(self, research_hash: str) -> Path:
        success_source = self.processing / f"{research_hash}.success.json"
        receipt = self._load_success_marker(success_source, research_hash)
        self._record_accepted_identities(
            tuple(receipt["identityDigests"]),
            receipt["receiptId"],
            receipt["researchSha256"],
            receipt["processedAt"],
        )
        destination = self._write_processed_receipt(research_hash, receipt)
        self._delete_processing_bundle(research_hash)
        return destination

    def _write_processed_receipt(
        self, research_hash: str, receipt: dict[str, Any]
    ) -> Path:
        destination = self.processed / f"{research_hash}.receipt.json"
        serialized = canonical_json_bytes(receipt) + b"\n"
        if destination.exists():
            if destination.is_symlink() or destination.read_bytes() != serialized:
                raise ValidationError("processed receipt collision")
            return destination
        atomic_write_bytes(destination, serialized, mode=0o600)
        return destination

    def _delete_processing_bundle(self, research_hash: str) -> None:
        for suffix in (".json", ".request.json", ".success.json", ".retry.json"):
            (self.processing / f"{research_hash}{suffix}").unlink(missing_ok=True)
        self._fsync_directories(self.processing, self.processed, self.state)

    def _reconcile_completed(self) -> None:
        for success in sorted(self.processing.glob("[0-9a-f]*.success.json")):
            stem = success.name.removesuffix(".success.json")
            if (
                SHA256_HEX.fullmatch(stem) is None
                or success.is_symlink()
                or not success.is_file()
            ):
                continue
            try:
                self._complete_success(stem)
            except ValidationError as exc:
                processing_manifest = self.processing / f"{stem}.json"
                if processing_manifest.exists():
                    self.quarantine_claimed(
                        processing_manifest, "failed_success_reconciliation", str(exc)
                    )
                else:
                    raise

    def _load_success_marker(self, path: Path, research_hash: str) -> dict[str, Any]:
        if path.is_symlink() or not path.is_file():
            raise ValidationError("success marker is not a regular file")
        raw = path.read_bytes()
        if len(raw) > 32 * 1024:
            raise ValidationError("success marker is too large")
        try:
            marker = require_object(json.loads(raw.decode("utf-8")), "success marker")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("success marker is not UTF-8 JSON") from exc
        required = {
            "schemaVersion",
            "outcome",
            "processedAt",
            "expiresAt",
            "retentionSeconds",
            "researchSha256",
            "receiptId",
            "signedBodySha256",
            "resultSha256",
            "result",
            "identityDigests",
            "identityContract",
        }
        require_exact_keys(marker, required=required, field="success marker")
        if marker["schemaVersion"] != "1" or marker["outcome"] != "accepted":
            raise ValidationError("success marker outcome is invalid")
        processed_at = parse_utc_timestamp(
            marker["processedAt"], "success marker.processedAt"
        )
        expires_at = parse_utc_timestamp(
            marker["expiresAt"], "success marker.expiresAt"
        )
        if (
            marker["retentionSeconds"] != int(PROCESSED_RECEIPT_TTL.total_seconds())
            or expires_at - processed_at != PROCESSED_RECEIPT_TTL
            or marker["researchSha256"] != research_hash
            or marker["identityContract"] != IDENTITY_INDEX_CONTRACT
        ):
            raise ValidationError("success marker retention or binding is invalid")
        self._validate_identity_digests(
            tuple(marker["identityDigests"])
            if isinstance(marker["identityDigests"], list)
            else ()
        )
        if (
            not isinstance(marker["receiptId"], str)
            or RECEIPT_ID.fullmatch(marker["receiptId"]) is None
            or not isinstance(marker["signedBodySha256"], str)
            or SHA256_HEX.fullmatch(marker["signedBodySha256"]) is None
            or not isinstance(marker["resultSha256"], str)
            or SHA256_HEX.fullmatch(marker["resultSha256"]) is None
            or marker["resultSha256"]
            != sha256_hex(canonical_json_bytes(marker["result"]))
        ):
            raise ValidationError("success marker hashes are invalid")
        final = self.processed / f"{research_hash}.receipt.json"
        if final.exists():
            if final.read_bytes() != canonical_json_bytes(marker) + b"\n":
                raise ValidationError("final receipt does not match success marker")
            return marker
        request_path = self.processing / f"{research_hash}.request.json"
        if not request_path.exists():
            raise ValidationError("success marker has no persisted request")
        persisted_request = self._load_signed_request(request_path)
        if (
            sha256_hex(canonical_json_bytes(persisted_request))
            != marker["signedBodySha256"]
            or marker["receiptId"] != persisted_request["receipt"]["receiptId"]
        ):
            raise ValidationError("success marker does not match persisted request")
        return marker

    def quarantine_claimed(self, path: Path, code: str, detail: str) -> Path:
        self.convert_pending_research_to_uncertain(path.stem)
        destination = self.claimed_quarantine / path.name
        if destination.exists():
            suffix = sha256_hex(f"{path.name}:{time.time_ns()}".encode("ascii"))[:12]
            destination = self.claimed_quarantine / f"{path.stem}-{suffix}.json"
        transition = self.processing / f"{path.stem}.quarantine.json"
        marker = {
            "schemaVersion": "1",
            "researchSha256": path.stem,
            "targetName": destination.name,
            "code": re.sub(r"[^a-z0-9_]", "_", code.lower())[:80],
            "detailSha256": sha256_hex(detail.encode("utf-8", errors="replace")),
            "quarantinedAt": utc_now_iso(),
        }
        atomic_write_bytes(transition, canonical_json_bytes(marker) + b"\n", mode=0o600)
        return self._finish_quarantine_transition(transition)

    def _reconcile_quarantine_transitions(self) -> None:
        for transition in sorted(self.processing.glob("[0-9a-f]*.quarantine.json")):
            self._finish_quarantine_transition(transition)

    def _finish_quarantine_transition(self, transition: Path) -> Path:
        try:
            marker = require_object(
                json.loads(transition.read_text(encoding="utf-8")),
                "quarantine transition",
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("quarantine transition is invalid") from exc
        require_exact_keys(
            marker,
            required={
                "schemaVersion",
                "researchSha256",
                "targetName",
                "code",
                "detailSha256",
                "quarantinedAt",
            },
            field="quarantine transition",
        )
        research_hash = marker["researchSha256"]
        target_name = marker["targetName"]
        if (
            marker["schemaVersion"] != "1"
            or not isinstance(research_hash, str)
            or SHA256_HEX.fullmatch(research_hash) is None
            or not isinstance(target_name, str)
            or re.fullmatch(f"{research_hash}(?:-[0-9a-f]{{12}})?\\.json", target_name)
            is None
            or not isinstance(marker["detailSha256"], str)
            or SHA256_HEX.fullmatch(marker["detailSha256"]) is None
        ):
            raise ValidationError("quarantine transition binding is invalid")
        parse_utc_timestamp(marker["quarantinedAt"], "quarantine transition time")
        source_manifest = self.processing / f"{research_hash}.json"
        destination = self.claimed_quarantine / target_name
        if source_manifest.exists() and not destination.exists():
            os.rename(source_manifest, destination)
        if not destination.exists():
            raise ValidationError("quarantine transition lost its manifest")
        for suffix in ("request", "success", "retry"):
            source = self.processing / f"{research_hash}.{suffix}.json"
            target = destination.with_name(f"{destination.stem}.{suffix}.json")
            if source.exists() and not target.exists():
                os.rename(source, target)
            elif source.exists() and target.exists():
                if source.read_bytes() != target.read_bytes():
                    raise ValidationError("quarantine sidecar collision")
                source.unlink()
        self._write_sidecar(
            destination,
            "error",
            {
                "code": marker["code"],
                "detail": f"detail_sha256={marker['detailSha256']}",
                "quarantinedAt": marker["quarantinedAt"],
            },
        )
        transition.unlink(missing_ok=True)
        self._fsync_directories(self.processing, self.claimed_quarantine)
        return destination

    def _reconcile_orphan_sidecars(self) -> None:
        for sidecar in sorted(self.processing.iterdir()):
            match = PROCESSING_SIDECAR.fullmatch(sidecar.name)
            if not match or sidecar.is_symlink() or not sidecar.is_file():
                continue
            research_hash, kind = match.groups()
            if (self.processing / f"{research_hash}.json").exists():
                continue
            if (self.processed / f"{research_hash}.receipt.json").exists():
                sidecar.unlink(missing_ok=True)
                continue
            manifests = [
                candidate
                for candidate in self.claimed_quarantine.glob(f"{research_hash}*.json")
                if re.fullmatch(
                    f"{research_hash}(?:-[0-9a-f]{{12}})?\\.json",
                    candidate.name,
                )
            ]
            if len(manifests) != 1:
                raise ValidationError(
                    "orphan processing sidecar requires operator reconciliation"
                )
            destination = manifests[0].with_name(f"{manifests[0].stem}.{kind}.json")
            if (
                destination.exists()
                and destination.read_bytes() != sidecar.read_bytes()
            ):
                raise ValidationError("orphan sidecar collision")
            if destination.exists():
                sidecar.unlink()
            else:
                os.rename(sidecar, destination)
        self._fsync_directories(self.processing, self.claimed_quarantine)

    def _record_retry(
        self, path: Path, code: str, *, now: datetime | None = None
    ) -> bool:
        reference = _as_utc(now)
        try:
            previous = self._load_retry(path)
        except FileNotFoundError:
            previous = None
        attempts = (previous.attempts if previous else 0) + 1
        first = previous.first_deferred_at if previous else reference
        expires = first + RETRY_MAX_AGE
        if attempts >= RETRY_MAX_ATTEMPTS or reference >= expires:
            return False
        delay = RETRY_DELAYS_SECONDS[min(attempts - 1, len(RETRY_DELAYS_SECONDS) - 1)]
        record = {
            "schemaVersion": "1",
            "attempts": attempts,
            "firstDeferredAt": _iso(first),
            "lastDeferredAt": _iso(reference),
            "nextAttemptAt": _iso(reference + timedelta(seconds=delay)),
            "expiresAt": _iso(expires),
            "reasonCode": re.sub(r"[^a-z0-9_]", "_", code.lower())[:80],
        }
        atomic_write_bytes(
            self.retry_path(path), canonical_json_bytes(record) + b"\n", mode=0o600
        )
        return True

    def _load_retry(self, path: Path) -> RetryRecord:
        sidecar = self.retry_path(path)
        if not sidecar.exists():
            raise FileNotFoundError(sidecar)
        if sidecar.is_symlink() or not sidecar.is_file():
            raise ValidationError("retry record is not a regular file")
        raw = sidecar.read_bytes()
        if len(raw) > 4096:
            raise ValidationError("retry record is too large")
        try:
            value = require_object(json.loads(raw.decode("utf-8")), "retry record")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
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
            field="retry record",
        )
        attempts = value["attempts"]
        if (
            value["schemaVersion"] != "1"
            or not isinstance(attempts, int)
            or isinstance(attempts, bool)
            or not 1 <= attempts <= RETRY_MAX_ATTEMPTS
            or not isinstance(value["reasonCode"], str)
            or not re.fullmatch(r"[a-z0-9_]{1,80}", value["reasonCode"])
        ):
            raise ValidationError("retry record shape is invalid")
        first = parse_utc_timestamp(value["firstDeferredAt"], "retry.firstDeferredAt")
        last = parse_utc_timestamp(value["lastDeferredAt"], "retry.lastDeferredAt")
        next_attempt = parse_utc_timestamp(
            value["nextAttemptAt"], "retry.nextAttemptAt"
        )
        expires = parse_utc_timestamp(value["expiresAt"], "retry.expiresAt")
        if last < first or next_attempt < last or expires != first + RETRY_MAX_AGE:
            raise ValidationError("retry record timeline is invalid")
        if raw != canonical_json_bytes(value) + b"\n":
            raise ValidationError("retry record is not canonical")
        return RetryRecord(attempts, first, next_attempt, expires, value["reasonCode"])

    def purge_expired(
        self, *, now: datetime | None = None, scope: str = "all"
    ) -> dict[str, int]:
        """Remove raw artifacts after 72h and bounded hash receipts after 30d."""
        self.ensure()
        if scope not in {"all", "discovery", "submit"}:
            raise ValidationError("purge scope is invalid")
        reference = _as_utc(now)
        raw_threshold = reference.timestamp() - RAW_ARTIFACT_TTL.total_seconds()
        counters = {"raw": 0, "receipts": 0}
        raw_directories: tuple[Path, ...] = ()
        if scope in {"all", "discovery"}:
            raw_directories += (
                self.ready,
                self.untrusted_deferred,
                self.untrusted_quarantine,
            )
        if scope in {"all", "submit"}:
            # Active/ambiguous processing is never blanket-purged: its exact
            # request may have committed remotely. SubmissionWorker first
            # records an uncertain hash-only tombstone before quarantining it.
            raw_directories += (self.claimed_quarantine,)
        temporary_directories = raw_directories
        if scope in {"all", "submit"}:
            temporary_directories += (self.processing, self.processed, self.state)
        for directory in temporary_directories:
            for path in list(directory.iterdir()):
                if (
                    not ATOMIC_TEMP_FILENAME.fullmatch(path.name)
                    or path.is_symlink()
                    or not path.is_file()
                ):
                    continue
                try:
                    expired = path.stat().st_mtime <= raw_threshold
                except FileNotFoundError:
                    continue
                if expired:
                    path.unlink(missing_ok=True)
                    counters["raw"] += 1
        for directory in raw_directories:
            for path in list(directory.iterdir()):
                if path.name.startswith(".") or path.is_symlink() or not path.is_file():
                    continue
                try:
                    expired = path.stat().st_mtime <= raw_threshold
                except FileNotFoundError:
                    continue
                if expired:
                    path.unlink(missing_ok=True)
                    counters["raw"] += 1
        receipt_paths = (
            list(self.processed.glob("*.receipt.json"))
            if scope in {"all", "submit"}
            else []
        )
        for path in receipt_paths:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                expires_at = parse_utc_timestamp(
                    value.get("expiresAt"), "receipt.expiresAt"
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError):
                expires_at = (
                    datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                    + PROCESSED_RECEIPT_TTL
                )
            if expires_at <= reference:
                path.unlink(missing_ok=True)
                counters["receipts"] += 1
        return counters

    def housekeeping(self, *, now: datetime | None = None) -> dict[str, int]:
        """Offline retention/recovery pass; performs no DNS or HTTP operations."""
        self.ensure()
        self.assert_no_reconciliation_journal()
        self._reconcile_completed()
        self._reconcile_quarantine_transitions()
        self._reconcile_orphan_sidecars()
        expired_processing = 0
        if not self.global_circuit_path.exists():
            for path in list(self.processing.glob("[0-9a-f]*.json")):
                if BATCH_FILENAME.fullmatch(path.name) is None:
                    continue
                if self.retry_exhausted(path, now=now):
                    self.quarantine_claimed(
                        path,
                        "retry_expired_housekeeping",
                        "bounded retry age expired during offline housekeeping",
                    )
                    expired_processing += 1
        purged = self.purge_expired(now=now, scope="submit")
        return {
            "expiredProcessing": expired_processing,
            "purgedRaw": purged["raw"],
            "purgedReceipts": purged["receipts"],
        }

    def recover_stale_processing(self, minimum_age_seconds: float = 1800.0) -> int:
        self.ensure()
        threshold = time.time() - minimum_age_seconds
        pending = 0
        for source in sorted(self.processing.iterdir(), key=lambda path: path.name):
            if (
                source.is_symlink()
                or not source.is_file()
                or BATCH_FILENAME.fullmatch(source.name) is None
            ):
                continue
            try:
                if source.stat().st_mtime > threshold:
                    continue
            except FileNotFoundError:
                continue
            pending += 1
        return pending

    @staticmethod
    def _write_sidecar(artifact: Path, suffix: str, value: dict[str, Any]) -> None:
        sidecar = artifact.with_name(f"{artifact.stem}.{suffix}.json")
        atomic_write_bytes(sidecar, canonical_json_bytes(value) + b"\n")
