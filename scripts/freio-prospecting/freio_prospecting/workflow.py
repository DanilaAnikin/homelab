from __future__ import annotations

import fcntl
import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .common import (
    MAX_JSON_BYTES,
    SHA256_HEX,
    ValidationError,
    atomic_write_bytes,
    canonical_json_bytes,
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


@dataclass(frozen=True)
class ClaimedManifest:
    path: Path
    document: dict[str, Any]
    candidates: tuple[ResearchCandidate, ...]


@dataclass(frozen=True)
class ProcessingWork:
    claimed: ClaimedManifest
    signed_request: dict[str, Any] | None


class Spool:
    DIRECTORY_MODES = {
        "ready": 0o770,
        "processing": 0o700,
        "processed": 0o700,
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
        self.quarantine = self.root / "quarantine"
        self.untrusted_quarantine = self.quarantine / "untrusted"
        self.claimed_quarantine = self.quarantine / "claimed"
        self.lock_path = self.processing / ".submit.lock"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._require_real_directory(self.root)
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
                    "quarantine/untrusted",
                    "quarantine/claimed",
                }
                and permissions & 0o077
            ):
                raise ValidationError(
                    f"private spool directory exposes group/other access: {directory}"
                )
            if name == "quarantine" and permissions & 0o027:
                raise ValidationError(
                    f"quarantine root has unsafe permissions: {directory}"
                )

    @staticmethod
    def _require_real_directory(path: Path) -> None:
        try:
            stat = path.lstat()
        except OSError as exc:
            raise ValidationError(
                f"cannot inspect spool directory {path}: {exc}"
            ) from exc
        if path.is_symlink() or not path.is_dir():
            raise ValidationError(f"spool path is not a real directory: {path}")
        if stat.st_mode & 0o002:
            raise ValidationError(f"spool directory must not be world-writable: {path}")

    @contextmanager
    def submit_lock(self) -> Iterator[None]:
        self.ensure()
        descriptor = os.open(
            self.lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o640,
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
            except FileNotFoundError:
                continue
            try:
                document, candidates = self._load_and_verify(destination)
            except ValidationError as exc:
                self.quarantine_claimed(destination, "invalid_batch", str(exc))
                continue
            # Replace the discovery-owned inode with a submit-owned private
            # canonical copy. A compromised discovery process may still hold
            # an fd to the old inode, but can no longer mutate the claimed path.
            atomic_write_bytes(
                destination, canonical_json_bytes(document) + b"\n", mode=0o600
            )
            return ClaimedManifest(destination, document, candidates)
        return None

    def processing_next(self) -> ProcessingWork | None:
        self.ensure()
        self._reconcile_completed()
        for path in sorted(self.processing.iterdir(), key=lambda item: item.name):
            if (
                path.is_symlink()
                or not path.is_file()
                or BATCH_FILENAME.fullmatch(path.name) is None
            ):
                continue
            try:
                document, candidates = self._load_and_verify(path)
                claimed = ClaimedManifest(path, document, candidates)
                request_path = self.request_path(claimed)
                if not request_path.exists():
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

    def persist_signed_request(
        self, claimed: ClaimedManifest, request: dict[str, Any]
    ) -> bytes:
        validated = validate_signed_intake_request(request)
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
        if raw != canonical_json_bytes(validated) + b"\n":
            raise ValidationError("persisted request is not canonical")
        return validated

    def mark_processed(
        self,
        claimed: ClaimedManifest,
        result: dict[str, Any],
        signed_request: dict[str, Any],
    ) -> Path:
        validate_signed_intake_request(signed_request)
        destination = self.processed / claimed.path.name
        if destination.exists():
            raise ValidationError("processed artifact already exists")
        request_source = self.request_path(claimed)
        if not request_source.exists():
            raise ValidationError(
                "cannot finish processing without persisted request bytes"
            )
        result_receipt = {
            "processedAt": utc_now_iso(),
            "researchSha256": destination.stem,
            "receiptId": signed_request["receipt"]["receiptId"],
            "signedBodySha256": sha256_hex(canonical_json_bytes(signed_request)),
            "resultSha256": sha256_hex(canonical_json_bytes(result)),
            "result": result,
        }
        # Persist the validated remote success before the first state rename.
        # A crash after this point is completed locally without another POST.
        atomic_write_bytes(
            self.success_path(claimed),
            canonical_json_bytes(result_receipt) + b"\n",
            mode=0o600,
        )
        self._complete_processed(claimed)
        return destination

    def _complete_processed(self, claimed: ClaimedManifest) -> None:
        destination = self.processed / claimed.path.name
        request_source = self.request_path(claimed)
        success_source = self.success_path(claimed)
        request_destination = destination.with_name(f"{destination.stem}.request.json")
        result_destination = destination.with_name(f"{destination.stem}.result.json")
        if claimed.path.exists() and not destination.exists():
            os.rename(claimed.path, destination)
        if request_source.exists() and not request_destination.exists():
            os.rename(request_source, request_destination)
        if success_source.exists() and not result_destination.exists():
            os.rename(success_source, result_destination)
        if (
            not destination.exists()
            or not request_destination.exists()
            or not result_destination.exists()
        ):
            raise ValidationError("could not complete atomic processed transition")

    def _reconcile_completed(self) -> None:
        for success in sorted(self.processing.glob("[0-9a-f]*.success.json")):
            stem = success.name.removesuffix(".success.json")
            if (
                SHA256_HEX.fullmatch(stem) is None
                or success.is_symlink()
                or not success.is_file()
            ):
                continue
            processing_manifest = self.processing / f"{stem}.json"
            processed_manifest = self.processed / f"{stem}.json"
            if processing_manifest.exists():
                try:
                    document, candidates = self._load_and_verify(processing_manifest)
                    self._complete_processed(
                        ClaimedManifest(processing_manifest, document, candidates)
                    )
                except ValidationError as exc:
                    self.quarantine_claimed(
                        processing_manifest,
                        "failed_success_reconciliation",
                        str(exc),
                    )
            elif processed_manifest.exists():
                # Crash after the manifest rename. Reconstruct the claimed path
                # object solely to finish request/result sidecar renames.
                placeholder = ClaimedManifest(processing_manifest, {}, ())
                self._complete_processed(placeholder)

    def quarantine_claimed(self, path: Path, code: str, detail: str) -> Path:
        request_source = path.with_name(f"{path.stem}.request.json")
        success_source = path.with_name(f"{path.stem}.success.json")
        destination = self.claimed_quarantine / path.name
        if destination.exists():
            suffix = sha256_hex(f"{path.name}:{time.time_ns()}".encode("ascii"))[:12]
            destination = self.claimed_quarantine / f"{path.stem}-{suffix}.json"
        os.rename(path, destination)
        if (
            request_source.exists()
            and request_source.is_file()
            and not request_source.is_symlink()
        ):
            request_destination = destination.with_name(
                f"{destination.stem}.request.json"
            )
            if not request_destination.exists():
                os.rename(request_source, request_destination)
        if (
            success_source.exists()
            and success_source.is_file()
            and not success_source.is_symlink()
        ):
            success_destination = destination.with_name(
                f"{destination.stem}.success.json"
            )
            if not success_destination.exists():
                os.rename(success_source, success_destination)
        self._write_sidecar(
            destination,
            "error",
            {"code": code[:80], "detail": detail[:500], "quarantinedAt": utc_now_iso()},
        )
        return destination

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
            # Processing is submit-private. Never return it to discovery-owned
            # ready: a persisted request must be retried byte-for-byte.
            pending += 1
        return pending

    @staticmethod
    def _write_sidecar(artifact: Path, suffix: str, value: dict[str, Any]) -> None:
        sidecar = artifact.with_name(f"{artifact.stem}.{suffix}.json")
        atomic_write_bytes(sidecar, canonical_json_bytes(value) + b"\n")
