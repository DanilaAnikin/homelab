from __future__ import annotations

import stat
from pathlib import Path

from .contract import ValidationError


def load_private_credential(
    path: Path,
    *,
    label: str,
    minimum: int = 32,
    maximum: int = 4096,
) -> str:
    """Read a systemd LoadCredential file without accepting aliases or whitespace."""
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValidationError(f"cannot inspect {label} credential") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & 0o077
        or not minimum <= metadata.st_size <= maximum + 1
    ):
        raise ValidationError(f"{label} credential is not a private regular file")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError(f"cannot read {label} credential") from exc
    token = raw[:-1] if raw.endswith(b"\n") else raw
    try:
        value = token.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{label} credential is not UTF-8") from exc
    if (
        not minimum <= len(token) <= maximum
        or not value
        or value != value.strip()
        or any(character.isspace() or ord(character) < 0x20 for character in value)
    ):
        raise ValidationError(f"{label} credential has an unsafe value")
    return value
