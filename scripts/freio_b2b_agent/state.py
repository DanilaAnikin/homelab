from __future__ import annotations

import fcntl
import hashlib
import os
import re
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .contract import (
    MAX_JSON_BYTES,
    ValidationError,
    canonical_json_bytes,
    parse_strict_json,
    validate_complete_payload,
)


REASON_CODE = re.compile(r"^[a-z0-9][a-z0-9_]{0,79}$")


class AlreadyRunning(RuntimeError):
    pass


class UncertainCompletion(RuntimeError):
    pass


@dataclass(frozen=True)
class CompletionState:
    phase: str
    payload: dict[str, Any]
    body_sha256: str


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.parent / (f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unlink_durable(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(path.parent)


class StateStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.lock_path = root / ".lock"
        self.completion_path = root / "completion.json"
        self.uncertain_path = root / "uncertain.json"
        self.last_success_path = root / "last-success.json"
        self.last_reconciliation_path = root / "last-reconciliation.json"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            metadata = self.root.lstat()
        except OSError as exc:
            raise ValidationError("cannot inspect classifier spool") from exc
        if (
            self.root.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_mode & 0o077
            or metadata.st_uid != os.geteuid()
        ):
            raise ValidationError(
                "classifier spool must be a private directory owned by the worker"
            )

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.ensure()
        descriptor = os.open(
            self.lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise ValidationError("classifier lock file is unsafe")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AlreadyRunning("classifier worker is already running") from exc
            yield
        finally:
            os.close(descriptor)

    def _read_json(self, path: Path, label: str) -> Any:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ValidationError(f"cannot inspect {label}") from exc
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o077
            or metadata.st_size > MAX_JSON_BYTES
        ):
            raise ValidationError(f"{label} is unsafe")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ValidationError(f"cannot read {label}") from exc
        return parse_strict_json(raw, label=label)

    def _load_completion(self) -> CompletionState | None:
        if not self.completion_path.exists():
            return None
        value = self._read_json(self.completion_path, "completion spool")
        if not isinstance(value, dict) or set(value) != {
            "version",
            "phase",
            "completion",
            "bodySha256",
        }:
            raise ValidationError("completion spool has an unexpected shape")
        if value["version"] != 1 or value["phase"] not in {"prepared", "inflight"}:
            raise ValidationError("completion spool version or phase is invalid")
        payload = validate_complete_payload(value["completion"])
        body_hash = value["bodySha256"]
        expected_hash = sha256_hex(canonical_json_bytes(payload))
        if not isinstance(body_hash, str) or body_hash != expected_hash:
            raise ValidationError("completion spool hash does not match")
        return CompletionState(value["phase"], payload, body_hash)

    def resume_prepared(self) -> dict[str, Any] | None:
        if self.uncertain_path.exists():
            # Validate before reporting the circuit so corrupt state cannot be hidden.
            self._load_uncertain()
            raise UncertainCompletion("an ambiguous completion requires reconciliation")
        state = self._load_completion()
        if state is None:
            return None
        if state.phase == "inflight":
            self.mark_uncertain("worker_restart_inflight")
            raise UncertainCompletion(
                "an interrupted completion requires reconciliation"
            )
        return state.payload

    def prepare(self, payload: dict[str, Any]) -> None:
        if self.uncertain_path.exists() or self.completion_path.exists():
            raise ValidationError("classifier spool already contains unresolved work")
        exact_payload = validate_complete_payload(payload)
        body_hash = sha256_hex(canonical_json_bytes(exact_payload))
        _atomic_write(
            self.completion_path,
            canonical_json_bytes(
                {
                    "version": 1,
                    "phase": "prepared",
                    "completion": exact_payload,
                    "bodySha256": body_hash,
                }
            ),
        )

    def mark_inflight(self) -> dict[str, Any]:
        state = self._load_completion()
        if state is None or state.phase != "prepared":
            raise ValidationError("prepared completion is missing")
        _atomic_write(
            self.completion_path,
            canonical_json_bytes(
                {
                    "version": 1,
                    "phase": "inflight",
                    "completion": state.payload,
                    "bodySha256": state.body_sha256,
                }
            ),
        )
        return state.payload

    def mark_success(self) -> None:
        state = self._load_completion()
        if state is None or state.phase != "inflight":
            raise ValidationError("inflight completion is missing")
        _atomic_write(
            self.last_success_path,
            canonical_json_bytes(
                {
                    "version": 1,
                    "completedAt": utc_now_iso(),
                    "completionSha256": state.body_sha256,
                }
            ),
        )
        _unlink_durable(self.completion_path)

    def mark_uncertain(self, reason: str) -> None:
        if REASON_CODE.fullmatch(reason) is None:
            raise ValidationError("uncertain reason code is invalid")
        state = self._load_completion()
        if state is None:
            raise ValidationError("completion state is missing")
        _atomic_write(
            self.uncertain_path,
            canonical_json_bytes(
                {
                    "version": 1,
                    "recordedAt": utc_now_iso(),
                    "reason": reason,
                    "taskId": state.payload["taskId"],
                    "claimId": state.payload["claimId"],
                    "completionSha256": state.body_sha256,
                }
            ),
        )
        # The potentially identifying summary is scrubbed once automatic action stops.
        _unlink_durable(self.completion_path)

    def _load_uncertain(self) -> dict[str, Any]:
        value = self._read_json(self.uncertain_path, "uncertain completion")
        if not isinstance(value, dict) or set(value) != {
            "version",
            "recordedAt",
            "reason",
            "taskId",
            "claimId",
            "completionSha256",
        }:
            raise ValidationError("uncertain completion has an unexpected shape")
        if (
            value["version"] != 1
            or not isinstance(value["reason"], str)
            or REASON_CODE.fullmatch(value["reason"]) is None
            or not isinstance(value["completionSha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", value["completionSha256"]) is None
        ):
            raise ValidationError("uncertain completion is invalid")
        # Reuse the strict UUID validator without retaining a classification.
        validate_complete_payload(
            {
                "taskId": value["taskId"],
                "claimId": value["claimId"],
                "classification": {
                    "intent": "unknown",
                    "confidence": 0,
                    "faqTopic": None,
                    "seatCount": None,
                    "subjectCount": None,
                    "summary": "reconciliation",
                    "riskTags": [],
                },
            }
        )
        return value

    def reconcile(self, outcome: str) -> None:
        if outcome not in {"completed", "not_completed"}:
            raise ValidationError("reconciliation outcome is invalid")
        uncertain = self._load_uncertain()
        _atomic_write(
            self.last_reconciliation_path,
            canonical_json_bytes(
                {
                    "version": 1,
                    "reconciledAt": utc_now_iso(),
                    "outcome": outcome,
                    "completionSha256": uncertain["completionSha256"],
                }
            ),
        )
        _unlink_durable(self.uncertain_path)
        _unlink_durable(self.completion_path)
