from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import AbstractSet, Any


CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
MAX_JSON_BYTES = 96 * 1024


class ValidationError(ValueError):
    """Raised when untrusted input does not satisfy the worker contract."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return the one canonical JSON encoding used for hashes and signatures."""
    _validate_unicode_scalars(value)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValidationError("value cannot be represented as canonical JSON") from exc


def _validate_unicode_scalars(value: Any) -> None:
    """Reject lone UTF-16 surrogates before they can crash UTF-8 encoding."""
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValidationError("text contains an invalid Unicode scalar value")
        return
    if isinstance(value, list):
        for entry in value:
            _validate_unicode_scalars(entry)
        return
    if isinstance(value, dict):
        for key, entry in value.items():
            _validate_unicode_scalars(key)
            _validate_unicode_scalars(entry)


def utf16_length(value: str) -> int:
    """Match JavaScript/Zod string length, which counts UTF-16 code units."""
    _validate_unicode_scalars(value)
    return len(value.encode("utf-16-le")) // 2


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def parse_utc_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ValidationError(f"{field} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} must include an offset")
    return parsed.astimezone(timezone.utc)


def require_object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValidationError(f"{field} must be an object")
    for key in value:
        _validate_unicode_scalars(key)
    return value


def require_exact_keys(
    value: dict[str, Any],
    *,
    required: set[str],
    optional: AbstractSet[str] = frozenset(),
    field: str,
) -> None:
    keys = set(value)
    missing = required - keys
    unexpected = keys - required - optional
    if missing:
        raise ValidationError(f"{field} is missing: {', '.join(sorted(missing))}")
    if unexpected:
        # Keys originate in untrusted JSON and may contain PII, newlines or
        # terminal escape sequences.  This message is persisted in quarantine
        # metadata and may reach the systemd journal, so expose only a count.
        noun = "field" if len(unexpected) == 1 else "fields"
        raise ValidationError(f"{field} has {len(unexpected)} unexpected {noun}")


def require_text(value: object, field: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be text")
    _validate_unicode_scalars(value)
    normalized = value.strip()
    if not minimum <= utf16_length(normalized) <= maximum or CONTROL_CHARACTERS.search(
        normalized
    ):
        raise ValidationError(f"{field} has an invalid length or control character")
    return normalized


def read_bounded_json(path: Path, maximum: int = MAX_JSON_BYTES) -> Any:
    try:
        stat = path.lstat()
    except OSError as exc:
        raise ValidationError(f"cannot inspect input: {exc}") from exc
    if not path.is_file() or path.is_symlink():
        raise ValidationError("input must be a regular, non-symlink file")
    if stat.st_size > maximum:
        raise ValidationError(f"input exceeds {maximum} bytes")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError(f"cannot read input: {exc}") from exc
    if len(raw) > maximum:
        raise ValidationError(f"input exceeds {maximum} bytes")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("input is not valid UTF-8 JSON") from exc


def atomic_write_bytes(
    path: Path,
    data: bytes,
    mode: int = 0o640,
    *,
    exact_mode: bool = False,
) -> None:
    """Durably replace a file without ever exposing a partial artifact.

    ``exact_mode`` is reserved for deliberate cross-user handoffs. It applies the
    requested permissions to the completed temporary file before the atomic
    rename, without weakening the process-wide umask for any other artifact.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / (f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = None
            handle.write(data)
            handle.flush()
            if exact_mode:
                os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
