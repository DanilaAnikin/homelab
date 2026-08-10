from __future__ import annotations

import os
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
