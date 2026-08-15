#!/usr/bin/env python3
"""Build and verify the small, strict metadata contracts used by Postiz backups.

The program intentionally never reads an environment file as configuration and never
prints file contents.  Its stdout is limited to validated identifiers and aggregate
counts that shell callers can safely consume.
"""

from __future__ import annotations

import argparse
import copy
import configparser
import datetime as dt
import gzip
import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


UPLOAD_SCHEMA = "freio.postiz.upload-manifest.v1"
ARTIFACT_SCHEMA = "freio.postiz.artifact-receipt.v1"
RECOVERY_SCHEMA = "freio.postiz.recovery-set.v2"
TREE_SCHEMA = "freio.postiz.tree-archive.v1"
OPERATOR_STATE_SCHEMA = "freio.postiz.operator-state.v1"
AUTH_SCHEMA = "freio.postiz.authenticated-commit.v1"
QUIESCE_JOURNAL_SCHEMA = "freio.postiz.quiesce-journal.v1"
RESTORE_JOURNAL_SCHEMA = "freio.postiz.restore-journal.v1"
GENERIC_RESTORE_JOURNAL_SCHEMA = "freio.generic.restore-journal.v1"
CAPTURE_SCHEMA = "freio.postiz.quiesced-capture.v1"
STORAGE_POLICY_SCHEMA = "freio.postiz.storage-policy-attestation.v2"
STORAGE_POLICY_SOURCE_SCHEMA = "freio.postiz.storage-policy-source.v1"
SEASONAL_POLICY_SCHEMA = "freio.postiz.seasonal-backup-policy.v1"
MAX_TREE_MANIFEST_BYTES = 16 * 1024**2
MAX_CONFIG_ARCHIVE_MEMBER_BYTES = 16 * 1024**2
MAX_CONFIG_ARCHIVE_EXPANDED_BYTES = 64 * 1024**2
MAX_PHYSICAL_ARCHIVE_MEMBERS = 1_000_000
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TIMESTAMP_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")
DOCKER_FINISHED_AT_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z$"
)
DOCKER_ZERO_TIME = "0001-01-01T00:00:00Z"
ACCOUNT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,62}[a-z0-9]$")
SAFE_PATH_RE = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9._-]*/)*[A-Za-z0-9][A-Za-z0-9._-]*$"
)
SAFE_REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
POSTIZ_NO_DEPS_DEPENDENCIES = {
    "postiz-postgres": {"condition": "service_started", "required": True},
    "postiz-redis": {"condition": "service_started", "required": True},
}
EXPECTED_CONFIG_MEMBERS = {
    "etc/homelab/postiz-backup-source-revision",
    "etc/systemd/system/backup.service",
    "etc/systemd/system/backup.timer",
    "etc/systemd/system/frequent-db-backup.service",
    "etc/systemd/system/frequent-db-backup.timer",
    "etc/systemd/system/postiz-backup-workspace-cleanup.service",
    "etc/systemd/system/postiz-quiesce-recover.service",
    "etc/systemd/system/postiz-restore-cleanup.service",
    "etc/systemd/system/restore-drill.service",
    "etc/systemd/system/restore-drill.timer",
    "etc/tmpfiles.d/homelab-backup.conf",
    "srv/postiz/Dockerfile.patch",
    "srv/postiz/docker-compose.yml",
    "srv/postiz/postiz.env",
    "srv/postiz/schedule-week.py",
    "srv/homelab/self-healing/postiz-offline-verify.sh",
    "srv/homelab/self-healing/postiz-restore-drill.sh",
    "srv/homelab/self-healing/restore-drill.sh",
    "usr/local/bin/frequent-db-backup.sh",
    "usr/local/bin/homelab-backup.sh",
    "usr/local/libexec/postiz-backup-manifest.py",
    "usr/local/sbin/postiz-artifact-backup.sh",
    "usr/local/sbin/postiz-backup-workspace-cleanup.sh",
    "usr/local/sbin/postiz-compose-locked.sh",
    "usr/local/sbin/postiz-quiesced-capture.sh",
    "usr/local/sbin/postiz-r2-policy-attest.sh",
}
EXPECTED_EXECUTABLE_CONFIG_MEMBERS = {
    "srv/postiz/schedule-week.py",
    "srv/homelab/self-healing/postiz-offline-verify.sh",
    "srv/homelab/self-healing/postiz-restore-drill.sh",
    "srv/homelab/self-healing/restore-drill.sh",
    "usr/local/bin/frequent-db-backup.sh",
    "usr/local/bin/homelab-backup.sh",
    "usr/local/libexec/postiz-backup-manifest.py",
    "usr/local/sbin/postiz-artifact-backup.sh",
    "usr/local/sbin/postiz-backup-workspace-cleanup.sh",
    "usr/local/sbin/postiz-compose-locked.sh",
    "usr/local/sbin/postiz-quiesced-capture.sh",
    "usr/local/sbin/postiz-r2-policy-attest.sh",
}


class ContractError(ValueError):
    """The candidate backup violates the recovery contract."""


def _die(message: str) -> None:
    raise ContractError(message)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _atomic_lines(path: Path, lines: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            for line in lines:
                handle.write(line)
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _load_json_value(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        _die(f"not a regular JSON file: {path}")
    if path.stat().st_size > 64 * 1024**2:
        _die("JSON input exceeds byte ceiling")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _die(f"invalid JSON: {exc}")
    return value


def _load_json(path: Path) -> dict[str, Any]:
    value = _load_json_value(path)
    if not isinstance(value, dict):
        _die("JSON root must be an object")
    return value


def _validate_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not TIMESTAMP_RE.fullmatch(value):
        _die("invalid UTC timestamp")
    return value


def _validate_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        _die(f"invalid sha256 for {label}")
    return value


def _validate_safe_path(value: Any, label: str = "path") -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 1024:
        _die(f"invalid {label}")
    if not SAFE_PATH_RE.fullmatch(value):
        _die(f"unsafe {label}")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
        _die(f"unsafe {label}")
    return value


def _validate_remote_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) > 1024 or not SAFE_REMOTE_RE.fullmatch(value):
        _die(f"unsafe {label}")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
        _die(f"unsafe {label}")
    return value


def _checked_root(root: Path, expected_uid: int, expected_gid: int) -> os.stat_result:
    try:
        info = root.lstat()
    except OSError as exc:
        _die(f"cannot stat upload root: {exc}")
    if not stat.S_ISDIR(info.st_mode) or root.is_symlink():
        _die("upload root must be a real directory")
    if info.st_uid != expected_uid or info.st_gid != expected_gid:
        _die("upload root has unexpected owner")
    if stat.S_IMODE(info.st_mode) != 0o755:
        _die("upload root must have mode 0755")
    return info


def _hash_open_file(path: Path) -> tuple[str, os.stat_result]:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        _die(f"cannot open upload file safely: {path}: {exc}")
    digest = hashlib.sha256()
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            _die(f"upload entry is not regular: {path}")
        for chunk in iter(lambda: os.read(fd, 1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")
    if any(getattr(before, name) != getattr(after, name) for name in stable_fields):
        _die(f"upload changed while hashing: {path}")
    return digest.hexdigest(), after


def _source_tuple(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
        info.st_uid,
        info.st_gid,
        stat.S_IMODE(info.st_mode),
    )


def _entry_tuple(entry: dict[str, Any]) -> tuple[int, ...]:
    source = entry.get("source")
    if not isinstance(source, dict):
        _die("manifest entry lacks source seal")
    names = ("dev", "ino", "size", "mtime_ns", "ctime_ns", "nlink", "uid", "gid", "mode")
    values: list[int] = []
    for name in names:
        value = source.get(name)
        if not isinstance(value, int) or value < 0:
            _die(f"invalid source seal field: {name}")
        values.append(value)
    return tuple(values)


def _walk_uploads(root: Path, expected_uid: int, expected_gid: int) -> list[tuple[str, Path]]:
    _checked_root(root, expected_uid, expected_gid)
    files: list[tuple[str, Path]] = []
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        directory_info = directory_path.lstat()
        if not stat.S_ISDIR(directory_info.st_mode) or directory_path.is_symlink():
            _die(f"unsafe upload directory: {directory_path}")
        if directory_info.st_uid != expected_uid or directory_info.st_gid != expected_gid:
            _die(f"upload directory has unexpected owner: {directory_path}")
        if stat.S_IMODE(directory_info.st_mode) != 0o755:
            _die(f"upload directory must have mode 0755: {directory_path}")
        dirnames.sort()
        filenames.sort()
        for name in dirnames:
            child = directory_path / name
            child_info = child.lstat()
            if child.is_symlink() or not stat.S_ISDIR(child_info.st_mode):
                _die(f"upload tree contains a directory symlink: {child}")
        for name in [*dirnames, *filenames]:
            relative = (directory_path / name).relative_to(root).as_posix()
            _validate_safe_path(relative)
        for name in filenames:
            path = directory_path / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or path.is_symlink():
                _die(f"upload entry is not a regular file: {path}")
            if info.st_uid != expected_uid or info.st_gid != expected_gid:
                _die(f"upload file has unexpected owner: {path}")
            if stat.S_IMODE(info.st_mode) != 0o644 or info.st_nlink != 1:
                _die(f"upload file mode/link contract failed: {path}")
            files.append((path.relative_to(root).as_posix(), path))
    files.sort(key=lambda pair: pair[0].encode("utf-8"))
    return files


def _validate_upload_manifest(
    value: dict[str, Any], *, max_files: int = 100_000, max_bytes: int = 16 * 1024**3
) -> list[dict[str, Any]]:
    if set(value) != {"schema", "created_at", "file_count", "total_bytes", "entries"}:
        _die("upload manifest has unexpected fields")
    if value.get("schema") != UPLOAD_SCHEMA:
        _die("unsupported upload manifest schema")
    _validate_timestamp(value.get("created_at"))
    entries = value.get("entries")
    if not isinstance(entries, list):
        _die("upload manifest entries must be a list")
    if len(entries) > max_files:
        _die("upload manifest exceeds file ceiling")
    seen_paths: set[str] = set()
    canonical: list[dict[str, Any]] = []
    total = 0
    for raw in entries:
        if not isinstance(raw, dict) or set(raw) != {"path", "sha256", "size", "mode", "source"}:
            _die("upload manifest entry has unexpected fields")
        path = _validate_safe_path(raw.get("path"))
        if path in seen_paths:
            _die("duplicate upload path")
        seen_paths.add(path)
        digest = _validate_sha(raw.get("sha256"), path)
        size = raw.get("size")
        mode = raw.get("mode")
        if not isinstance(size, int) or size < 0 or not isinstance(mode, int) or mode != 0o644:
            _die("invalid upload size or mode")
        _entry_tuple(raw)
        total += size
        if total > max_bytes:
            _die("upload manifest exceeds byte ceiling")
        canonical.append(raw)
        if digest == "":  # pragma: no cover - guarded by regex, documents non-empty intent
            _die("empty digest")
    if [entry["path"] for entry in canonical] != sorted(
        seen_paths, key=lambda item: item.encode("utf-8")
    ):
        _die("upload entries are not in canonical order")
    if value.get("file_count") != len(canonical) or value.get("total_bytes") != total:
        _die("upload aggregate counts do not match entries")
    return canonical


def command_scan(args: argparse.Namespace) -> None:
    root = Path(args.root)
    files = _walk_uploads(root, args.expected_uid, args.expected_gid)
    if len(files) > args.max_files:
        _die("upload tree exceeds file ceiling")
    entries: list[dict[str, Any]] = []
    total = 0
    for relative, path in files:
        digest, info = _hash_open_file(path)
        total += info.st_size
        if total > args.max_bytes:
            _die("upload tree exceeds byte ceiling")
        entries.append(
            {
                "path": relative,
                "sha256": digest,
                "size": info.st_size,
                "mode": stat.S_IMODE(info.st_mode),
                "source": {
                    "dev": info.st_dev,
                    "ino": info.st_ino,
                    "size": info.st_size,
                    "mtime_ns": info.st_mtime_ns,
                    "ctime_ns": info.st_ctime_ns,
                    "nlink": info.st_nlink,
                    "uid": info.st_uid,
                    "gid": info.st_gid,
                    "mode": stat.S_IMODE(info.st_mode),
                },
            }
        )
    manifest = {
        "schema": UPLOAD_SCHEMA,
        "created_at": _validate_timestamp(args.timestamp),
        "file_count": len(entries),
        "total_bytes": total,
        "entries": entries,
    }
    _validate_upload_manifest(manifest, max_files=args.max_files, max_bytes=args.max_bytes)
    _atomic_json(Path(args.output), manifest)


def command_verify_source(args: argparse.Namespace) -> None:
    manifest = _load_json(Path(args.manifest))
    entries = _validate_upload_manifest(manifest, max_files=args.max_files, max_bytes=args.max_bytes)
    root = Path(args.root)
    current = _walk_uploads(root, args.expected_uid, args.expected_gid)
    if [item[0] for item in current] != [entry["path"] for entry in entries]:
        _die("upload path set changed during backup")
    for (relative, path), entry in zip(current, entries, strict=True):
        info = path.lstat()
        if _source_tuple(info) != _entry_tuple(entry):
            _die(f"upload metadata changed during backup: {relative}")


def command_entries(args: argparse.Namespace) -> None:
    manifest = _load_json(Path(args.manifest))
    for entry in _validate_upload_manifest(manifest):
        print(f"{entry['sha256']}\t{entry['size']}\t{entry['path']}")


def command_summary(args: argparse.Namespace) -> None:
    manifest = _load_json(Path(args.manifest))
    entries = _validate_upload_manifest(manifest)
    print(f"{len(entries)}\t{manifest['total_bytes']}")


def _cipher_size(plain_size: int) -> int:
    return 16 + ((plain_size // 16) + 1) * 16


def command_emit_blob_list(args: argparse.Namespace) -> None:
    manifest = _load_json(Path(args.manifest))
    entries = _validate_upload_manifest(manifest)
    keys = sorted({f"{entry['sha256'][:2]}/{entry['sha256']}.enc" for entry in entries})
    _atomic_lines(Path(args.output), keys)


def command_emit_blob_sizes(args: argparse.Namespace) -> None:
    manifest = _load_json(Path(args.manifest))
    entries = _validate_upload_manifest(manifest)
    sizes: dict[str, int] = {}
    for entry in entries:
        key = f"{entry['sha256'][:2]}/{entry['sha256']}.enc"
        expected = _cipher_size(entry["size"])
        if key in sizes and sizes[key] != expected:
            _die("same plaintext digest has conflicting size")
        sizes[key] = expected
    _atomic_lines(
        Path(args.output),
        (f"{sizes[key]}|{key}" for key in sorted(sizes, key=lambda item: item.encode("utf-8"))),
    )


def command_verify_cipher_tree(args: argparse.Namespace) -> None:
    manifest = _load_json(Path(args.manifest))
    entries = _validate_upload_manifest(manifest)
    expected: dict[str, int] = {}
    for entry in entries:
        key = f"{entry['sha256'][:2]}/{entry['sha256']}.enc"
        expected.setdefault(key, _cipher_size(entry["size"]))
        if expected[key] != _cipher_size(entry["size"]):
            _die("same plaintext digest has conflicting size")
    root = Path(args.root)
    actual: dict[str, int] = {}
    if root.exists():
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            directory_path = Path(directory)
            if directory_path.is_symlink():
                _die("cipher tree contains a symlinked directory")
            dirnames.sort()
            filenames.sort()
            for name in dirnames:
                child = directory_path / name
                if child.is_symlink() or not child.is_dir():
                    _die("cipher tree contains a directory symlink")
            for name in filenames:
                path = directory_path / name
                if path.is_symlink() or not path.is_file():
                    _die("cipher tree contains a non-regular entry")
                relative = path.relative_to(root).as_posix()
                _validate_safe_path(relative, "cipher path")
                actual[relative] = path.stat().st_size
    if actual != expected:
        _die("cipher tree does not exactly match upload manifest")


def command_emit_checksums(args: argparse.Namespace) -> None:
    manifest = _load_json(Path(args.manifest))
    entries = _validate_upload_manifest(manifest)
    _atomic_lines(Path(args.output), (f"{entry['sha256']}  {entry['path']}" for entry in entries))


def command_verify_restored(args: argparse.Namespace) -> None:
    manifest = _load_json(Path(args.manifest))
    entries = _validate_upload_manifest(manifest)
    root = Path(args.root)
    _checked_root(root, args.expected_uid, args.expected_gid)
    actual_paths: list[str] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        if directory_path.is_symlink():
            _die("restored tree contains a symlinked directory")
        directory_info = directory_path.lstat()
        if (
            directory_info.st_uid != args.expected_uid
            or directory_info.st_gid != args.expected_gid
            or stat.S_IMODE(directory_info.st_mode) != 0o755
        ):
            _die("restored upload directory metadata differs")
        dirnames.sort()
        filenames.sort()
        for name in dirnames:
            child = directory_path / name
            if child.is_symlink() or not child.is_dir():
                _die("restored tree contains a directory symlink")
        for name in filenames:
            path = directory_path / name
            if path.is_symlink() or not path.is_file():
                _die("restored tree contains a non-regular entry")
            actual_paths.append(path.relative_to(root).as_posix())
    actual_paths.sort(key=lambda item: item.encode("utf-8"))
    if actual_paths != [entry["path"] for entry in entries]:
        _die("restored upload path set differs from manifest")
    for entry in entries:
        path = root / entry["path"]
        info = path.stat()
        if (
            info.st_uid != args.expected_uid
            or info.st_gid != args.expected_gid
            or info.st_nlink != 1
            or info.st_size != entry["size"]
            or stat.S_IMODE(info.st_mode) != entry["mode"]
        ):
            _die("restored upload metadata differs from manifest")
        if _sha256_file(path) != entry["sha256"]:
            _die("restored upload digest differs from manifest")


REQUIRED_IMAGE_SERVICES = {"postiz", "postiz-postgres", "postiz-redis", "postiz-temporal"}


def _validate_image_record(image: Any) -> dict[str, Any]:
    if not isinstance(image, dict) or set(image) != {
        "service",
        "configured_ref",
        "image_id",
        "archive_key",
        "archive_cipher_sha256",
        "archive_cipher_bytes",
        "archive_uncompressed_bytes",
        "archive_uncompressed_inodes",
    }:
        _die("invalid image receipt")
    service = image.get("service")
    if service not in REQUIRED_IMAGE_SERVICES:
        _die("invalid image service")
    configured_ref = image.get("configured_ref")
    if (
        not isinstance(configured_ref, str)
        or not 1 <= len(configured_ref) <= 512
        or any(character.isspace() for character in configured_ref)
        or configured_ref.startswith("-")
    ):
        _die("invalid configured image reference")
    image_id = image.get("image_id")
    if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        _die("invalid image ID")
    _validate_remote_path(image.get("archive_key"), "image archive key")
    _validate_sha(image.get("archive_cipher_sha256"), "image archive cipher")
    if not isinstance(image.get("archive_cipher_bytes"), int) or not 1 <= image[
        "archive_cipher_bytes"
    ] < 5 * 1024**3:
        _die("invalid image archive size")
    if not isinstance(image.get("archive_uncompressed_bytes"), int) or not 1 <= image[
        "archive_uncompressed_bytes"
    ] <= 32 * 1024**3:
        _die("invalid expanded image size")
    if not isinstance(image.get("archive_uncompressed_inodes"), int) or not 1 <= image[
        "archive_uncompressed_inodes"
    ] <= 1_000_000:
        _die("invalid expanded image inode count")
    return image


def _validate_artifact_receipt(value: dict[str, Any]) -> dict[str, Any]:
    expected = {"schema", "created_at", "uploads", "images", "runtime_config"}
    if set(value) != expected or value.get("schema") != ARTIFACT_SCHEMA:
        _die("invalid artifact receipt schema or fields")
    _validate_timestamp(value.get("created_at"))
    uploads = value.get("uploads")
    images = value.get("images")
    runtime = value.get("runtime_config")
    if not isinstance(uploads, dict) or set(uploads) != {
        "manifest_key",
        "manifest_cipher_sha256",
        "file_count",
        "total_bytes",
    }:
        _die("invalid uploads receipt")
    _validate_remote_path(uploads.get("manifest_key"), "upload manifest key")
    _validate_sha(uploads.get("manifest_cipher_sha256"), "upload manifest cipher")
    if not isinstance(uploads.get("file_count"), int) or not 0 <= uploads["file_count"] <= 100_000:
        _die("invalid upload file count")
    if not isinstance(uploads.get("total_bytes"), int) or not 0 <= uploads["total_bytes"] <= 16 * 1024**3:
        _die("invalid upload byte count")
    if not isinstance(images, list) or len(images) != len(REQUIRED_IMAGE_SERVICES):
        _die("artifact receipt lacks exact service image set")
    services = []
    for image in images:
        services.append(_validate_image_record(image)["service"])
    if services != sorted(REQUIRED_IMAGE_SERVICES) or len(set(services)) != len(services):
        _die("artifact receipt image services are not exact/canonical")
    if not isinstance(runtime, dict) or set(runtime) != {"compose_sha256", "dockerfile_sha256"}:
        _die("invalid runtime config receipt")
    _validate_sha(runtime.get("compose_sha256"), "compose")
    _validate_sha(runtime.get("dockerfile_sha256"), "Dockerfile")
    return value


def command_write_artifact_receipt(args: argparse.Namespace) -> None:
    manifest = _load_json(Path(args.upload_manifest))
    entries = _validate_upload_manifest(manifest)
    if manifest["created_at"] != args.timestamp:
        _die("upload manifest and artifact receipt timestamps differ")
    record_dir = Path(args.image_record_dir)
    if record_dir.is_symlink() or not record_dir.is_dir():
        _die("image record directory is unsafe")
    records = [_validate_image_record(_load_json(path)) for path in sorted(record_dir.glob("*.json"))]
    if {record["service"] for record in records} != REQUIRED_IMAGE_SERVICES:
        _die("image record directory lacks the exact service set")
    records.sort(key=lambda record: record["service"])
    value = {
        "schema": ARTIFACT_SCHEMA,
        "created_at": _validate_timestamp(args.timestamp),
        "uploads": {
            "manifest_key": _validate_remote_path(args.upload_manifest_key, "upload manifest key"),
            "manifest_cipher_sha256": _validate_sha(
                args.upload_manifest_cipher_sha256, "upload manifest cipher"
            ),
            "file_count": len(entries),
            "total_bytes": manifest["total_bytes"],
        },
        "images": records,
        "runtime_config": {
            "compose_sha256": _validate_sha(args.compose_sha256, "compose"),
            "dockerfile_sha256": _validate_sha(args.dockerfile_sha256, "Dockerfile"),
        },
    }
    _validate_artifact_receipt(value)
    _atomic_json(Path(args.output), value)


def command_write_image_record(args: argparse.Namespace) -> None:
    value = {
        "service": args.service,
        "configured_ref": args.configured_ref,
        "image_id": args.image_id,
        "archive_key": args.archive_key,
        "archive_cipher_sha256": args.archive_cipher_sha256,
        "archive_cipher_bytes": args.archive_cipher_bytes,
        "archive_uncompressed_bytes": args.archive_uncompressed_bytes,
        "archive_uncompressed_inodes": args.archive_uncompressed_inodes,
    }
    _validate_image_record(value)
    _atomic_json(Path(args.output), value)


ARTIFACT_KEYS = {
    "created_at": ("created_at",),
    "upload_manifest_key": ("uploads", "manifest_key"),
    "upload_manifest_cipher_sha256": ("uploads", "manifest_cipher_sha256"),
    "upload_file_count": ("uploads", "file_count"),
    "upload_total_bytes": ("uploads", "total_bytes"),
    "compose_sha256": ("runtime_config", "compose_sha256"),
    "dockerfile_sha256": ("runtime_config", "dockerfile_sha256"),
}


def command_artifact_get(args: argparse.Namespace) -> None:
    value = _validate_artifact_receipt(_load_json(Path(args.receipt)))
    path = ARTIFACT_KEYS[args.key]
    current: Any = value
    for component in path:
        current = current[component]
    print(current)


IMAGE_KEYS = {
    "configured_ref": "configured_ref",
    "image_id": "image_id",
    "archive_key": "archive_key",
    "archive_cipher_sha256": "archive_cipher_sha256",
    "archive_cipher_bytes": "archive_cipher_bytes",
    "archive_uncompressed_bytes": "archive_uncompressed_bytes",
    "archive_uncompressed_inodes": "archive_uncompressed_inodes",
}


def command_image_get(args: argparse.Namespace) -> None:
    value = _validate_artifact_receipt(_load_json(Path(args.receipt)))
    for image in value["images"]:
        if image["service"] == args.service:
            print(image[IMAGE_KEYS[args.key]])
            return
    _die("image service is absent from artifact receipt")


OPERATOR_STATE_PATHS = {
    "seasonal_releases": "/var/lib/freio-content/seasonal-releases",
    "seasonal_anchor_replacement": "/var/lib/freio-content/seasonal-anchor-replacement",
}

SEASONAL_POLICY_PATH = Path("/var/lib/freio-content/seasonal-backup-policy.json")
SEASONAL_ROLE_SETS = {
    "seasonal_releases": {
        "installed_saga",
        "installed_launcher",
        "installed_scheduler",
        "render_source",
        "provider_ready_media",
        "release_manifest",
        "release_receipt",
    },
    "seasonal_anchor_replacement": {
        "sealed_bundle",
        "provider_snapshot",
        "terminal_receipt",
    },
}


def _validate_seasonal_policy(
    value: dict[str, Any],
    *,
    verify_sources: bool,
    source_overrides: dict[str, Path] | None = None,
) -> dict[str, Any]:
    if set(value) != {
        "schema",
        "created_at",
        "state_required",
        "release_id",
        "roots",
    } or value.get("schema") != SEASONAL_POLICY_SCHEMA:
        _die("invalid seasonal backup policy")
    _validate_timestamp(value.get("created_at"))
    if value.get("state_required") is not True:
        _die("a present seasonal policy must require state")
    release_id = value.get("release_id")
    if not isinstance(release_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", release_id):
        _die("invalid seasonal release ID")
    roots = value.get("roots")
    if not isinstance(roots, dict) or set(roots) != set(OPERATOR_STATE_PATHS):
        _die("seasonal policy lacks exact roots")
    for name, source_path in OPERATOR_STATE_PATHS.items():
        root_record = roots[name]
        if not isinstance(root_record, dict) or set(root_record) != {
            "path",
            "root_mode",
            "files",
            "roles",
        }:
            _die("invalid seasonal root policy")
        if root_record.get("path") != source_path or root_record.get("root_mode") != 0o700:
            _die("seasonal root path/mode policy differs")
        files = root_record.get("files")
        roles = root_record.get("roles")
        if not isinstance(files, dict) or not files:
            _die("seasonal policy has no file inventory")
        if not isinstance(roles, dict) or set(roles) != SEASONAL_ROLE_SETS[name]:
            _die("seasonal policy role set differs")
        canonical_paths = sorted(files, key=lambda item: item.encode("utf-8"))
        if list(files) != canonical_paths:
            _die("seasonal policy file inventory is not canonical")
        for relative, record in files.items():
            _validate_safe_path(relative, "seasonal policy file")
            if not isinstance(record, dict) or set(record) != {"sha256", "size", "mode"}:
                _die("invalid seasonal policy file record")
            _validate_sha(record.get("sha256"), "seasonal policy file")
            size = record.get("size")
            mode = record.get("mode")
            if (
                not isinstance(size, int)
                or size < 0
                or not isinstance(mode, int)
                or mode < 0
                or mode > 0o777
                or mode & 0o022
            ):
                _die("invalid seasonal policy file metadata")
        for role_paths in roles.values():
            if (
                not isinstance(role_paths, list)
                or not role_paths
                or role_paths != sorted(set(role_paths), key=lambda item: item.encode("utf-8"))
            ):
                _die("seasonal policy role paths are not canonical")
            for relative in role_paths:
                if relative not in files:
                    _die("seasonal policy role references an unsealed file")
        if verify_sources:
            root = source_overrides[name] if source_overrides else Path(source_path)
            root_info = root.lstat()
            if (
                root.is_symlink()
                or not stat.S_ISDIR(root_info.st_mode)
                or root_info.st_uid != 0
                or root_info.st_gid != 0
                or stat.S_IMODE(root_info.st_mode) != 0o700
            ):
                _die("seasonal root is not root-only")
            actual: dict[str, dict[str, Any]] = {}
            for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
                directory_path = Path(directory)
                dirnames.sort()
                filenames.sort()
                info = directory_path.lstat()
                if (
                    directory_path.is_symlink()
                    or not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != 0
                    or info.st_gid != 0
                    or stat.S_IMODE(info.st_mode) & 0o022
                    ):
                        _die("seasonal tree contains an unsafe directory")
                for dirname in dirnames:
                    child = directory_path / dirname
                    child_info = child.lstat()
                    if child.is_symlink() or not stat.S_ISDIR(child_info.st_mode):
                        _die("seasonal tree contains a directory symlink")
                for child_name in [*dirnames, *filenames]:
                    _validate_safe_path((directory_path / child_name).relative_to(root).as_posix())
                for filename in filenames:
                    path = directory_path / filename
                    relative = path.relative_to(root).as_posix()
                    info = path.lstat()
                    if (
                        path.is_symlink()
                        or not stat.S_ISREG(info.st_mode)
                        or info.st_uid != 0
                        or info.st_gid != 0
                        or info.st_nlink != 1
                        or stat.S_IMODE(info.st_mode) & 0o022
                    ):
                        _die("seasonal tree contains an unsafe file")
                    actual[relative] = {
                        "sha256": _sha256_file(path),
                        "size": info.st_size,
                        "mode": stat.S_IMODE(info.st_mode),
                    }
            actual = dict(sorted(actual.items(), key=lambda pair: pair[0].encode("utf-8")))
            if actual != files:
                _die("seasonal tree differs from its exact policy inventory")
    return value


def command_verify_seasonal_policy(args: argparse.Namespace) -> None:
    overrides = None
    if args.seasonal_releases_root or args.seasonal_anchor_replacement_root:
        if not args.seasonal_releases_root or not args.seasonal_anchor_replacement_root:
            _die("both seasonal root overrides are required")
        overrides = {
            "seasonal_releases": Path(args.seasonal_releases_root),
            "seasonal_anchor_replacement": Path(args.seasonal_anchor_replacement_root),
        }
    _validate_seasonal_policy(
        _load_json(Path(args.policy)), verify_sources=True, source_overrides=overrides
    )


def _validate_operator_state(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != {"schema", "created_at", "states", "policy"} or value.get("schema") != OPERATOR_STATE_SCHEMA:
        _die("invalid operator-state receipt")
    _validate_timestamp(value.get("created_at"))
    states = value.get("states")
    if not isinstance(states, dict) or set(states) != set(OPERATOR_STATE_PATHS):
        _die("operator-state receipt lacks exact paths")
    for name, source_path in OPERATOR_STATE_PATHS.items():
        state = states[name]
        if not isinstance(state, dict) or state.get("source_path") != source_path:
            _die("invalid operator-state path")
        status_value = state.get("status")
        if status_value == "absent":
            if set(state) != {"source_path", "status"}:
                _die("absent operator state unexpectedly has an archive")
        elif status_value == "present":
            if set(state) != {"source_path", "status", "archive"}:
                _die("present operator state lacks its archive")
            archive = state["archive"]
            if not isinstance(archive, dict) or set(archive) != {"filename", "cipher_sha256"}:
                _die("invalid operator-state archive")
            filename = _validate_safe_path(archive.get("filename"), "operator-state filename")
            if "/" in filename or not filename.endswith(".enc"):
                _die("invalid operator-state archive filename")
            _validate_sha(archive.get("cipher_sha256"), "operator-state archive")
        else:
            _die("invalid operator-state status")
    policy = value.get("policy")
    if not isinstance(policy, dict) or policy.get("source_path") != str(SEASONAL_POLICY_PATH):
        _die("invalid seasonal policy receipt")
    policy_status = policy.get("status")
    if policy_status == "absent":
        if set(policy) != {"source_path", "status"}:
            _die("absent seasonal policy unexpectedly has an archive")
        if any(state["status"] != "absent" for state in states.values()):
            _die("seasonal roots cannot be present before the policy exists")
    elif policy_status == "present":
        if set(policy) != {"source_path", "status", "archive"}:
            _die("present seasonal policy lacks an archive")
        archive = policy["archive"]
        if not isinstance(archive, dict) or set(archive) != {"filename", "cipher_sha256"}:
            _die("invalid seasonal policy archive")
        filename = _validate_safe_path(archive.get("filename"), "seasonal policy filename")
        if "/" in filename or not filename.endswith(".enc"):
            _die("invalid seasonal policy archive filename")
        _validate_sha(archive.get("cipher_sha256"), "seasonal policy archive")
        if any(state["status"] != "present" for state in states.values()):
            _die("required seasonal roots are not both present")
    else:
        _die("invalid seasonal policy status")
    return value


def command_write_operator_state(args: argparse.Namespace) -> None:
    def state(name: str, status_value: str, archive_text: str | None) -> dict[str, Any]:
        item: dict[str, Any] = {
            "source_path": OPERATOR_STATE_PATHS[name],
            "status": status_value,
        }
        if status_value == "present":
            if not archive_text:
                _die("present operator state requires an archive")
            archive = Path(archive_text)
            if archive.is_symlink() or not archive.is_file() or archive.stat().st_size == 0:
                _die("operator-state archive is missing or unsafe")
            item["archive"] = {
                "filename": archive.name,
                "cipher_sha256": _sha256_file(archive),
            }
        elif archive_text:
            _die("absent operator state must not have an archive")
        return item

    def policy(status_value: str, archive_text: str | None) -> dict[str, Any]:
        item: dict[str, Any] = {
            "source_path": str(SEASONAL_POLICY_PATH),
            "status": status_value,
        }
        if status_value == "present":
            if not archive_text:
                _die("present seasonal policy requires an archive")
            archive = Path(archive_text)
            if archive.is_symlink() or not archive.is_file() or archive.stat().st_size == 0:
                _die("seasonal policy archive is missing or unsafe")
            item["archive"] = {"filename": archive.name, "cipher_sha256": _sha256_file(archive)}
        elif archive_text:
            _die("absent seasonal policy must not have an archive")
        return item

    value = {
        "schema": OPERATOR_STATE_SCHEMA,
        "created_at": _validate_timestamp(args.timestamp),
        "states": {
            "seasonal_releases": state(
                "seasonal_releases", args.seasonal_releases_status, args.seasonal_releases_archive
            ),
            "seasonal_anchor_replacement": state(
                "seasonal_anchor_replacement",
                args.seasonal_anchor_replacement_status,
                args.seasonal_anchor_replacement_archive,
            ),
        },
        "policy": policy(args.policy_status, args.policy_archive),
    }
    _validate_operator_state(value)
    _atomic_json(Path(args.output), value)


def command_operator_state_get(args: argparse.Namespace) -> None:
    value = _validate_operator_state(_load_json(Path(args.receipt)))
    if args.name == "policy":
        state = value["policy"]
    else:
        state = value["states"][args.name]
    if args.key == "status":
        print(state["status"])
        return
    if state["status"] != "present":
        _die("absent operator state has no archive field")
    print(state["archive"]["filename" if args.key == "archive_filename" else "cipher_sha256"])


def _validate_recovery_set(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != {"schema", "created_at", "consistency", "payloads"} or value.get("schema") != RECOVERY_SCHEMA:
        _die("invalid recovery-set schema or fields")
    _validate_timestamp(value.get("created_at"))
    consistency = value.get("consistency")
    if not isinstance(consistency, dict) or set(consistency) != {
        "kind",
        "physical_cluster",
        "capture_evidence",
    } or consistency.get("kind") != "writer-fenced-physical-cluster":
        _die("invalid recovery-set consistency contract")
    payloads_root = value.get("payloads")
    required = {
        "globals",
        "databases",
        "runtime_config",
        "config_volume",
        "redis",
        "artifacts",
        "operator_state",
        "storage_policy",
    }
    if not isinstance(payloads_root, dict) or set(payloads_root) != required:
        _die("invalid recovery-set payload")
    databases = payloads_root["databases"]
    expected_databases = {"postiz", "temporal", "temporal_visibility", "insights"}
    if not isinstance(databases, dict) or set(databases) != expected_databases:
        _die("recovery set lacks the exact Postiz database set")
    payloads = {
        label: payloads_root[label]
        for label in required
        if label != "databases"
    }
    payloads["physical_cluster"] = consistency["physical_cluster"]
    payloads["capture_evidence"] = consistency["capture_evidence"]
    payloads.update({f"database_{name}": item for name, item in databases.items()})
    byte_limits = {
        "globals": 64 * 1024**2,
        "database_postiz": 4 * 1024**3,
        "database_temporal": 4 * 1024**3,
        "database_temporal_visibility": 4 * 1024**3,
        "database_insights": 4 * 1024**3,
        "physical_cluster": 6 * 1024**3,
        "capture_evidence": 64 * 1024**2,
        "runtime_config": 73 * 1024**2,
        "config_volume": 64 * 1024**2,
        "redis": 2 * 1024**3,
        "artifacts": 64 * 1024**2,
        "operator_state": 64 * 1024**2,
        "storage_policy": 4 * 1024**2,
    }
    for label, item in sorted(payloads.items()):
        if not isinstance(item, dict) or set(item) != {
            "filename",
            "cipher_sha256",
            "cipher_bytes",
        }:
            _die(f"invalid recovery-set {label} entry")
        filename = _validate_safe_path(item.get("filename"), f"{label} filename")
        if "/" in filename or not filename.endswith(".enc"):
            _die(f"invalid recovery-set {label} filename")
        _validate_sha(item.get("cipher_sha256"), f"{label} cipher")
        cipher_bytes = item.get("cipher_bytes")
        if not isinstance(cipher_bytes, int) or not 1 <= cipher_bytes <= byte_limits[label]:
            _die(f"invalid recovery-set {label} size")
    return value


def command_write_recovery_set(args: argparse.Namespace) -> None:
    def item(path_text: str) -> dict[str, str | int]:
        path = Path(path_text)
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            _die(f"missing recovery payload: {path}")
        return {
            "filename": path.name,
            "cipher_sha256": _sha256_file(path),
            "cipher_bytes": path.stat().st_size,
        }

    value = {
        "schema": RECOVERY_SCHEMA,
        "created_at": _validate_timestamp(args.timestamp),
        "consistency": {
            "kind": "writer-fenced-physical-cluster",
            "physical_cluster": item(args.physical_cluster),
            "capture_evidence": item(args.capture_evidence),
        },
        "payloads": {
            "globals": item(args.globals),
            "databases": {
                "postiz": item(args.database_postiz),
                "temporal": item(args.database_temporal),
                "temporal_visibility": item(args.database_temporal_visibility),
                "insights": item(args.database_insights),
            },
            "runtime_config": item(args.runtime_config),
            "config_volume": item(args.config_volume),
            "redis": item(args.redis),
            "artifacts": item(args.artifacts),
            "operator_state": item(args.operator_state),
            "storage_policy": item(args.storage_policy),
        },
    }
    _validate_recovery_set(value)
    _atomic_json(Path(args.output), value)


RECOVERY_KEYS = {
    "created_at": ("created_at",),
    "physical_cluster_filename": ("consistency", "physical_cluster", "filename"),
    "physical_cluster_cipher_sha256": ("consistency", "physical_cluster", "cipher_sha256"),
    "physical_cluster_cipher_bytes": ("consistency", "physical_cluster", "cipher_bytes"),
    "capture_evidence_filename": ("consistency", "capture_evidence", "filename"),
    "capture_evidence_cipher_sha256": ("consistency", "capture_evidence", "cipher_sha256"),
    "capture_evidence_cipher_bytes": ("consistency", "capture_evidence", "cipher_bytes"),
    "globals_filename": ("payloads", "globals", "filename"),
    "globals_cipher_sha256": ("payloads", "globals", "cipher_sha256"),
    "globals_cipher_bytes": ("payloads", "globals", "cipher_bytes"),
    "database_postiz_filename": ("payloads", "databases", "postiz", "filename"),
    "database_postiz_cipher_sha256": ("payloads", "databases", "postiz", "cipher_sha256"),
    "database_postiz_cipher_bytes": ("payloads", "databases", "postiz", "cipher_bytes"),
    "database_temporal_filename": ("payloads", "databases", "temporal", "filename"),
    "database_temporal_cipher_sha256": ("payloads", "databases", "temporal", "cipher_sha256"),
    "database_temporal_cipher_bytes": ("payloads", "databases", "temporal", "cipher_bytes"),
    "database_temporal_visibility_filename": (
        "payloads",
        "databases",
        "temporal_visibility",
        "filename",
    ),
    "database_temporal_visibility_cipher_sha256": (
        "payloads",
        "databases",
        "temporal_visibility",
        "cipher_sha256",
    ),
    "database_temporal_visibility_cipher_bytes": (
        "payloads",
        "databases",
        "temporal_visibility",
        "cipher_bytes",
    ),
    "database_insights_filename": ("payloads", "databases", "insights", "filename"),
    "database_insights_cipher_sha256": (
        "payloads",
        "databases",
        "insights",
        "cipher_sha256",
    ),
    "database_insights_cipher_bytes": ("payloads", "databases", "insights", "cipher_bytes"),
    "runtime_config_filename": ("payloads", "runtime_config", "filename"),
    "runtime_config_cipher_sha256": ("payloads", "runtime_config", "cipher_sha256"),
    "runtime_config_cipher_bytes": ("payloads", "runtime_config", "cipher_bytes"),
    "config_volume_filename": ("payloads", "config_volume", "filename"),
    "config_volume_cipher_sha256": ("payloads", "config_volume", "cipher_sha256"),
    "config_volume_cipher_bytes": ("payloads", "config_volume", "cipher_bytes"),
    "redis_filename": ("payloads", "redis", "filename"),
    "redis_cipher_sha256": ("payloads", "redis", "cipher_sha256"),
    "redis_cipher_bytes": ("payloads", "redis", "cipher_bytes"),
    "artifacts_filename": ("payloads", "artifacts", "filename"),
    "artifacts_cipher_sha256": ("payloads", "artifacts", "cipher_sha256"),
    "artifacts_cipher_bytes": ("payloads", "artifacts", "cipher_bytes"),
    "operator_state_filename": ("payloads", "operator_state", "filename"),
    "operator_state_cipher_sha256": ("payloads", "operator_state", "cipher_sha256"),
    "operator_state_cipher_bytes": ("payloads", "operator_state", "cipher_bytes"),
    "storage_policy_filename": ("payloads", "storage_policy", "filename"),
    "storage_policy_cipher_sha256": ("payloads", "storage_policy", "cipher_sha256"),
    "storage_policy_cipher_bytes": ("payloads", "storage_policy", "cipher_bytes"),
}


def command_recovery_get(args: argparse.Namespace) -> None:
    value = _validate_recovery_set(_load_json(Path(args.recovery_set)))
    current: Any = value
    for component in RECOVERY_KEYS[args.key]:
        current = current[component]
    print(current)


def _safe_tar_member(member: tarfile.TarInfo) -> str:
    name = member.name.removeprefix("./")
    pure = PurePosixPath(name)
    if not name or pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
        _die("archive contains an unsafe path")
    if member.issym() or member.islnk() or not member.isfile():
        _die("archive contains a link or non-regular member")
    return name


def _safe_tar_name(member: tarfile.TarInfo) -> str:
    name = member.name.removeprefix("./").rstrip("/")
    pure = PurePosixPath(name)
    if not name or pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
        _die("archive contains an unsafe path")
    if member.issym() or member.islnk():
        _die("archive contains a link")
    return name


def _collect_small_tree(
    root: Path, expected_uid: int, expected_gid: int, max_bytes: int, max_members: int
) -> tuple[
    int,
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, tuple[int, ...]],
]:
    if not 1 <= max_members <= 100_000:
        _die("invalid tree member ceiling")
    root_info = root.lstat()
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or root.is_symlink()
        or root_info.st_uid != expected_uid
        or root_info.st_gid != expected_gid
        or stat.S_IMODE(root_info.st_mode) & 0o022
        or root_info.st_nlink < 1
    ):
        _die("invalid tree root link count")
    directories: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    source_seals: dict[str, tuple[int, ...]] = {"": _source_tuple(root_info)}
    total = 0
    member_count = 0
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        dirnames.sort()
        filenames.sort()
        if directory_path != root:
            relative_dir = directory_path.relative_to(root).as_posix()
            _validate_safe_path(relative_dir)
            info = directory_path.lstat()
            if not stat.S_ISDIR(info.st_mode) or directory_path.is_symlink():
                _die("tree contains an unsafe directory")
            if info.st_uid != expected_uid or info.st_gid != expected_gid or stat.S_IMODE(info.st_mode) & 0o022:
                _die("tree directory owner/mode contract failed")
            directories.append({"path": relative_dir, "mode": stat.S_IMODE(info.st_mode)})
            source_seals[relative_dir + "/"] = _source_tuple(info)
            member_count += 1
            if member_count > max_members:
                _die("tree archive exceeds member ceiling")
        for dirname in dirnames:
            child = directory_path / dirname
            child_info = child.lstat()
            if child.is_symlink() or not stat.S_ISDIR(child_info.st_mode):
                _die("tree contains a directory symlink")
        for name in [*dirnames, *filenames]:
            _validate_safe_path((directory_path / name).relative_to(root).as_posix())
        for name in filenames:
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            flags = os.O_RDONLY | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(path, flags)
            except OSError as exc:
                _die(f"cannot open tree file safely: {exc}")
            try:
                before = os.fstat(fd)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_uid != expected_uid
                    or before.st_gid != expected_gid
                    or stat.S_IMODE(before.st_mode) & 0o022
                    or before.st_nlink != 1
                ):
                    _die("tree file owner/mode/link contract failed")
                digest = hashlib.sha256()
                size = 0
                while True:
                    chunk = os.read(fd, 1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    total += len(chunk)
                    if total > max_bytes:
                        _die("tree archive exceeds byte ceiling")
                    digest.update(chunk)
                after = os.fstat(fd)
            finally:
                os.close(fd)
            if _source_tuple(before) != _source_tuple(after) or size != after.st_size:
                _die("tree file changed while sealing")
            files.append(
                {
                    "path": relative,
                    "mode": stat.S_IMODE(after.st_mode),
                    "size": after.st_size,
                    "sha256": digest.hexdigest(),
                }
            )
            source_seals[relative] = _source_tuple(after)
            member_count += 1
            if member_count > max_members:
                _die("tree archive exceeds member ceiling")
    directories.sort(key=lambda item: item["path"].encode("utf-8"))
    files.sort(key=lambda item: item["path"].encode("utf-8"))

    current_paths: dict[str, tuple[int, ...]] = {"": _source_tuple(root.lstat())}
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        dirnames.sort()
        filenames.sort()
        if directory_path != root:
            current_paths[directory_path.relative_to(root).as_posix() + "/"] = _source_tuple(
                directory_path.lstat()
            )
        for name in filenames:
            path = directory_path / name
            current_paths[path.relative_to(root).as_posix()] = _source_tuple(path.lstat())
    if current_paths != source_seals:
        _die("tree changed while creating archive")
    return stat.S_IMODE(root_info.st_mode), directories, files, source_seals


def command_seal_tree_archive(args: argparse.Namespace) -> None:
    prefix = _validate_safe_path(args.prefix, "archive prefix")
    if "/" in prefix:
        _die("archive prefix must be one path component")
    max_members = getattr(args, "max_members", 10_000)
    root = Path(args.root)
    root_mode, directories, files, source_seals = _collect_small_tree(
        root, args.expected_uid, args.expected_gid, args.max_bytes, max_members
    )
    manifest = {
        "schema": TREE_SCHEMA,
        "prefix": prefix,
        "root_mode": root_mode,
        "directories": directories,
        "files": files,
        "total_bytes": sum(item["size"] for item in files),
    }
    encoded_manifest = json.dumps(
        manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8") + b"\n"
    if len(encoded_manifest) > MAX_TREE_MANIFEST_BYTES:
        _die("tree archive manifest exceeds byte ceiling")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    with tarfile.open(temporary, "w:gz", compresslevel=6) as bundle:
        manifest_info = tarfile.TarInfo("freio-tree-manifest.json")
        manifest_info.size = len(encoded_manifest)
        manifest_info.mode = 0o600
        manifest_info.uid = 0
        manifest_info.gid = 0
        bundle.addfile(manifest_info, io.BytesIO(encoded_manifest))
        root_info = tarfile.TarInfo(prefix)
        root_info.type = tarfile.DIRTYPE
        root_info.mode = root_mode
        root_info.uid = 0
        root_info.gid = 0
        bundle.addfile(root_info)
        for directory in directories:
            info = tarfile.TarInfo(f"{prefix}/{directory['path']}")
            info.type = tarfile.DIRTYPE
            info.mode = directory["mode"]
            info.uid = 0
            info.gid = 0
            bundle.addfile(info)
        for file_entry in files:
            info = tarfile.TarInfo(f"{prefix}/{file_entry['path']}")
            info.size = file_entry["size"]
            info.mode = file_entry["mode"]
            info.uid = 0
            info.gid = 0
            source = root / file_entry["path"]
            flags = os.O_RDONLY | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(source, flags)
            except OSError as exc:
                _die(f"cannot reopen tree file safely: {exc}")
            try:
                before = os.fstat(fd)
                if _source_tuple(before) != source_seals[file_entry["path"]]:
                    _die("tree file changed before archive streaming")
                digest = hashlib.sha256()
                streamed = 0

                class TreeReader:
                    def read(self, size: int = -1) -> bytes:
                        nonlocal streamed
                        chunk = os.read(fd, size if size >= 0 else file_entry["size"] - streamed)
                        streamed += len(chunk)
                        if streamed > file_entry["size"]:
                            _die("tree file grew while archive streaming")
                        digest.update(chunk)
                        return chunk

                bundle.addfile(info, TreeReader())
                after = os.fstat(fd)
                if (
                    streamed != file_entry["size"]
                    or digest.hexdigest() != file_entry["sha256"]
                    or _source_tuple(after) != source_seals[file_entry["path"]]
                ):
                    _die("tree file changed while archive streaming")
            finally:
                os.close(fd)
    current_paths: dict[str, tuple[int, ...]] = {"": _source_tuple(root.lstat())}
    member_count = 0
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        dirnames.sort()
        filenames.sort()
        if directory_path != root:
            current_paths[directory_path.relative_to(root).as_posix() + "/"] = _source_tuple(
                directory_path.lstat()
            )
            member_count += 1
        for name in filenames:
            path = directory_path / name
            current_paths[path.relative_to(root).as_posix()] = _source_tuple(path.lstat())
            member_count += 1
        if member_count > max_members:
            _die("tree archive exceeds member ceiling")
    if current_paths != source_seals:
        _die("tree changed while streaming archive")
    os.chmod(temporary, 0o600)
    os.replace(temporary, output)


def command_verify_tree_archive(args: argparse.Namespace) -> None:
    archive = Path(args.archive)
    max_members = getattr(args, "max_members", 10_000)
    if not 1 <= max_members <= 100_000:
        _die("invalid tree member ceiling")
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            members: list[tarfile.TarInfo] = []
            for member in bundle:
                members.append(member)
                if len(members) > max_members + 2:
                    _die("tree archive exceeds member ceiling")
            by_name = {_safe_tar_name(member): member for member in members}
            if len(by_name) != len(members) or any(
                member.uid != 0 or member.gid != 0 for member in members
            ):
                _die("tree archive path/owner contract differs")
            manifest_member = by_name.get("freio-tree-manifest.json")
            if (
                manifest_member is None
                or not manifest_member.isfile()
                or manifest_member.size > MAX_TREE_MANIFEST_BYTES
            ):
                _die("tree archive lacks its manifest")
            extracted = bundle.extractfile(manifest_member)
            if extracted is None:
                _die("cannot read tree archive manifest")
            manifest_bytes = extracted.read(MAX_TREE_MANIFEST_BYTES + 1)
            if len(manifest_bytes) != manifest_member.size:
                _die("tree archive manifest size differs")
            manifest = json.loads(manifest_bytes)
            if not isinstance(manifest, dict) or set(manifest) != {
                "schema",
                "prefix",
                "root_mode",
                "directories",
                "files",
                "total_bytes",
            }:
                _die("invalid tree archive manifest")
            if manifest.get("schema") != TREE_SCHEMA or manifest.get("prefix") != args.prefix:
                _die("tree archive schema/prefix differs")
            root_mode = manifest.get("root_mode")
            root_member = by_name.get(args.prefix)
            if (
                not isinstance(root_mode, int)
                or root_mode & 0o022
                or root_member is None
                or not root_member.isdir()
                or stat.S_IMODE(root_member.mode) != root_mode
            ):
                _die("tree archive root mode differs")
            directories = manifest.get("directories")
            files = manifest.get("files")
            if not isinstance(directories, list) or not isinstance(files, list):
                _die("invalid tree archive entries")
            if len(directories) + len(files) > max_members:
                _die("tree archive manifest exceeds member ceiling")
            expected_names = {"freio-tree-manifest.json", args.prefix}
            total = 0
            for item in directories:
                if not isinstance(item, dict) or set(item) != {"path", "mode"}:
                    _die("invalid tree directory entry")
                relative = _validate_safe_path(item["path"])
                member = by_name.get(f"{args.prefix}/{relative}")
                if member is None or not member.isdir() or stat.S_IMODE(member.mode) != item["mode"]:
                    _die("tree directory archive entry differs")
                expected_names.add(f"{args.prefix}/{relative}")
            for item in files:
                if not isinstance(item, dict) or set(item) != {"path", "mode", "size", "sha256"}:
                    _die("invalid tree file entry")
                relative = _validate_safe_path(item["path"])
                digest = _validate_sha(item["sha256"], relative)
                size = item.get("size")
                mode = item.get("mode")
                if not isinstance(size, int) or size < 0 or not isinstance(mode, int) or mode & 0o022:
                    _die("invalid tree file metadata")
                member = by_name.get(f"{args.prefix}/{relative}")
                if member is None or not member.isfile() or member.size != size or stat.S_IMODE(member.mode) != mode:
                    _die("tree file archive entry differs")
                handle = bundle.extractfile(member)
                if handle is None:
                    _die("cannot read tree file archive entry")
                actual = hashlib.sha256()
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    actual.update(chunk)
                if actual.hexdigest() != digest:
                    _die("tree file digest differs")
                total += size
                if total > args.max_bytes:
                    _die("tree archive exceeds byte ceiling")
                expected_names.add(f"{args.prefix}/{relative}")
            if total != manifest.get("total_bytes") or set(by_name) != expected_names:
                _die("tree archive contents/totals differ")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, tarfile.TarError) as exc:
        _die(f"invalid tree archive: {exc}")


def command_verify_tree_restored(args: argparse.Namespace) -> None:
    command_verify_tree_archive(args)
    max_members = getattr(args, "max_members", 10_000)
    archive = Path(args.archive)
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            manifest_member = bundle.getmember("freio-tree-manifest.json")
            handle = bundle.extractfile(manifest_member)
            if handle is None or manifest_member.size > MAX_TREE_MANIFEST_BYTES:
                _die("cannot read restored-tree manifest")
            manifest_bytes = handle.read(MAX_TREE_MANIFEST_BYTES + 1)
            if len(manifest_bytes) != manifest_member.size:
                _die("restored-tree manifest size differs")
            manifest = json.loads(manifest_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, tarfile.TarError) as exc:
        _die(f"invalid restored-tree archive: {exc}")
    root = Path(args.root)
    try:
        root_info = root.lstat()
    except OSError as exc:
        _die(f"cannot stat restored tree: {exc}")
    if (
        root.is_symlink()
        or not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != args.expected_uid
        or root_info.st_gid != args.expected_gid
        or stat.S_IMODE(root_info.st_mode) != manifest["root_mode"]
    ):
        _die("restored tree root metadata differs")
    expected_dirs = {item["path"]: item for item in manifest["directories"]}
    expected_files = {item["path"]: item for item in manifest["files"]}
    actual_dirs: set[str] = set()
    actual_files: set[str] = set()
    member_count = 0
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        dirnames.sort()
        filenames.sort()
        if directory_path != root:
            relative_dir = directory_path.relative_to(root).as_posix()
            info = directory_path.lstat()
            expected = expected_dirs.get(relative_dir)
            if (
                directory_path.is_symlink()
                or not stat.S_ISDIR(info.st_mode)
                or expected is None
                or info.st_uid != args.expected_uid
                or info.st_gid != args.expected_gid
                or stat.S_IMODE(info.st_mode) != expected["mode"]
            ):
                _die("restored tree directory metadata differs")
            actual_dirs.add(relative_dir)
            member_count += 1
        for name in filenames:
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            expected = expected_files.get(relative)
            if (
                path.is_symlink()
                or not stat.S_ISREG(info.st_mode)
                or expected is None
                or info.st_uid != args.expected_uid
                or info.st_gid != args.expected_gid
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != expected["mode"]
                or info.st_size != expected["size"]
                or _sha256_file(path) != expected["sha256"]
            ):
                _die("restored tree file metadata differs")
            actual_files.add(relative)
            member_count += 1
        if member_count > max_members:
            _die("restored tree exceeds member ceiling")
    if actual_dirs != set(expected_dirs) or actual_files != set(expected_files):
        _die("restored tree path set differs")


def command_verify_config_archive(args: argparse.Namespace) -> None:
    archive = Path(args.archive)
    names: set[str] = set()
    digests: dict[str, str] = {}
    revision = b""
    expanded_bytes = 0
    try:
        # Streaming mode bounds both metadata and expanded contents before an
        # offline restore extracts this archive onto the host-backed workspace.
        with tarfile.open(archive, "r|gz") as bundle:
            for member in bundle:
                if len(names) >= len(EXPECTED_CONFIG_MEMBERS):
                    _die("config archive exceeds member ceiling")
                name = _safe_tar_member(member)
                if name in names:
                    _die("config archive contains a duplicate member")
                names.add(name)
                if member.size < 0 or member.size > MAX_CONFIG_ARCHIVE_MEMBER_BYTES:
                    _die("runtime config archive member exceeds expanded byte ceiling")
                expanded_bytes += member.size
                if expanded_bytes > MAX_CONFIG_ARCHIVE_EXPANDED_BYTES:
                    _die("runtime config archive exceeds expanded byte ceiling")
                if member.uid != 0 or member.gid != 0:
                    _die("config archive member is not root-owned")
                mode = stat.S_IMODE(member.mode)
                if name == "srv/postiz/postiz.env":
                    if mode != 0o600:
                        _die("postiz.env archive mode is not 0600")
                elif name == "etc/homelab/postiz-backup-source-revision":
                    if mode != 0o644 or member.size != 41:
                        _die("backup source revision archive metadata differs")
                elif name in EXPECTED_EXECUTABLE_CONFIG_MEMBERS:
                    if mode not in {0o750, 0o755}:
                        _die("recovery tooling archive member is not executable/root-safe")
                elif mode & 0o022:
                    _die("runtime config archive member is writable by group/other")
                extracted = bundle.extractfile(member)
                if extracted is None:
                    _die("cannot read config archive member")
                digest = hashlib.sha256()
                actual_size = 0
                revision_chunks: list[bytes] = []
                for chunk in iter(lambda: extracted.read(1024 * 1024), b""):
                    actual_size += len(chunk)
                    if actual_size > member.size:
                        _die("runtime config archive member size differs")
                    digest.update(chunk)
                    if name == "etc/homelab/postiz-backup-source-revision":
                        revision_chunks.append(chunk)
                if actual_size != member.size:
                    _die("runtime config archive member is truncated")
                digests[name] = digest.hexdigest()
                if name == "etc/homelab/postiz-backup-source-revision":
                    revision = b"".join(revision_chunks)
    except (OSError, tarfile.TarError) as exc:
        _die(f"invalid config archive: {exc}")
    if names != EXPECTED_CONFIG_MEMBERS:
        _die("config archive does not contain the exact allowlist")
    if not re.fullmatch(rb"[0-9a-f]{40}\n", revision):
        _die("backup source revision is not one exact Git commit")
    for name, expected in (
        ("srv/postiz/docker-compose.yml", args.compose_sha256),
        ("srv/postiz/Dockerfile.patch", args.dockerfile_sha256),
    ):
        if expected is None:
            continue
        _validate_sha(expected, name)
        if digests[name] != expected:
            _die("runtime config digest differs from artifact receipt")


def command_verify_config_source(args: argparse.Namespace) -> None:
    command_verify_config_archive(
        argparse.Namespace(archive=args.archive, compose_sha256=None, dockerfile_sha256=None)
    )
    archive = Path(args.archive)
    opened: list[tuple[str, Path, int, os.stat_result, bytes]] = []
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            members = {_safe_tar_name(member): member for member in bundle.getmembers()}
            for name in EXPECTED_CONFIG_MEMBERS:
                member = members[name]
                source = Path("/") / name
                flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
                try:
                    descriptor = os.open(source, flags)
                except OSError as exc:
                    _die(f"runtime config source is unavailable: {exc.filename}")
                before = os.fstat(descriptor)
                path_info = source.stat(follow_symlinks=False)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or stat.S_ISLNK(path_info.st_mode)
                    or (before.st_dev, before.st_ino) != (path_info.st_dev, path_info.st_ino)
                    or before.st_nlink != 1
                    or before.st_uid != member.uid
                    or before.st_gid != member.gid
                    or stat.S_IMODE(before.st_mode) != stat.S_IMODE(member.mode)
                    or before.st_size != member.size
                ):
                    _die("runtime config source metadata differs from its writer-fenced archive")
                source_digest = hashlib.sha256()
                while chunk := os.read(descriptor, 1024 * 1024):
                    source_digest.update(chunk)
                after = os.fstat(descriptor)
                if (
                    (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                ):
                    _die("runtime config source changed while it was verified")
                archived = bundle.extractfile(member)
                if archived is None:
                    _die("cannot read runtime config archive member")
                archived_digest = hashlib.sha256()
                for chunk in iter(lambda: archived.read(1024 * 1024), b""):
                    archived_digest.update(chunk)
                if source_digest.digest() != archived_digest.digest():
                    _die("runtime config source bytes differ from its writer-fenced archive")
                opened.append((name, source, descriptor, after, source_digest.digest()))

            # Keep every no-follow descriptor pinned until all members have
            # passed once, then re-hash/re-stat the entire set.  A rolling
            # deploy cannot swap an earlier path while a later path is checked.
            for _name, source, descriptor, sealed, sealed_digest in opened:
                path_info = source.stat(follow_symlinks=False)
                current = os.fstat(descriptor)
                if (
                    stat.S_ISLNK(path_info.st_mode)
                    or (current.st_dev, current.st_ino) != (path_info.st_dev, path_info.st_ino)
                    or (
                        current.st_size,
                        current.st_mtime_ns,
                        current.st_ctime_ns,
                        current.st_uid,
                        current.st_gid,
                        stat.S_IMODE(current.st_mode),
                        current.st_nlink,
                    )
                    != (
                        sealed.st_size,
                        sealed.st_mtime_ns,
                        sealed.st_ctime_ns,
                        sealed.st_uid,
                        sealed.st_gid,
                        stat.S_IMODE(sealed.st_mode),
                        sealed.st_nlink,
                    )
                ):
                    _die("runtime config source generation changed across verification")
                os.lseek(descriptor, 0, os.SEEK_SET)
                second_digest = hashlib.sha256()
                while chunk := os.read(descriptor, 1024 * 1024):
                    second_digest.update(chunk)
                if second_digest.digest() != sealed_digest:
                    _die("runtime config source bytes changed across verification")
    except (OSError, EOFError, gzip.BadGzipFile, KeyError, tarfile.TarError) as exc:
        _die(f"invalid runtime config source/archive binding: {exc}")
    finally:
        for _name, _source, descriptor, _sealed, _digest in opened:
            os.close(descriptor)


def _load_exact_compose_model(path: Path, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    compose = _load_json(path)
    services = compose.get("services") if isinstance(compose, dict) else None
    if compose.get("name") != "postiz" or not isinstance(services, dict) \
            or set(services) != JOURNAL_SERVICES:
        _die(f"{label} lacks the exact Postiz project/service set")
    if any(not isinstance(definition, dict) for definition in services.values()):
        _die(f"{label} has an invalid service definition")
    return compose, services


def _postiz_no_deps_projection(compose: dict[str, Any]) -> dict[str, Any]:
    services = compose.get("services")
    postiz = services.get("postiz") if isinstance(services, dict) else None
    if not isinstance(postiz, dict) or postiz.get("depends_on") != POSTIZ_NO_DEPS_DEPENDENCIES:
        _die("resolved Postiz depends_on differs from the exact Docker Compose v5 shape")
    projection = copy.deepcopy(compose)
    del projection["services"]["postiz"]["depends_on"]
    return projection


def command_write_compose_no_deps_model(args: argparse.Namespace) -> None:
    compose, _services = _load_exact_compose_model(
        Path(args.compose_json), "resolved full Compose runtime"
    )
    _atomic_json(Path(args.output), _postiz_no_deps_projection(compose))


def _load_compose_hashes(path: Path, expected: set[str], label: str) -> dict[str, str]:
    hashes: dict[str, str] = {}
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            parts = raw_line.split()
            if (
                len(parts) != 2
                or parts[0] not in expected
                or parts[0] in hashes
                or not SHA256_RE.fullmatch(parts[1])
            ):
                _die(f"{label} is invalid")
            hashes[parts[0]] = parts[1]
    except (OSError, UnicodeDecodeError) as exc:
        _die(f"cannot read {label}: {exc}")
    if set(hashes) != expected:
        _die(f"{label} set differs")
    return hashes


def _resolved_resource_name(compose: dict[str, Any], section: str, source: Any) -> str:
    resources = compose.get(section)
    definition = resources.get(source) if isinstance(resources, dict) else None
    name = definition.get("name") if isinstance(definition, dict) else None
    if not isinstance(source, str) or not isinstance(name, str) or not name:
        _die(f"resolved Compose {section} resource is invalid")
    return name


def _resolved_network_names(compose: dict[str, Any]) -> dict[str, str]:
    resources = compose.get("networks")
    if not isinstance(resources, dict) or not resources:
        _die("resolved Compose network resource map is invalid")
    names: dict[str, str] = {}
    resolved_names: set[str] = set()
    for source, definition in resources.items():
        name = definition.get("name") if isinstance(definition, dict) else None
        if (
            not isinstance(source, str)
            or not source
            or not isinstance(name, str)
            or not name
            or name in resolved_names
        ):
            _die("resolved Compose network resource names are not bijective/distinct")
        names[source] = name
        resolved_names.add(name)
    return names


def _network_endpoint_ipv4(
    raw_value: Any,
    ipam: tuple[tuple[ipaddress.IPv4Network, ipaddress.IPv4Address], ...],
) -> ipaddress.IPv4Interface:
    try:
        endpoint = ipaddress.ip_interface(raw_value) if isinstance(raw_value, str) else None
    except ValueError as exc:
        _die(f"Docker network endpoint IPv4 evidence is invalid: {exc}")
    matching = (
        [(subnet, gateway) for subnet, gateway in ipam if endpoint.ip in subnet]
        if isinstance(endpoint, ipaddress.IPv4Interface)
        else []
    )
    if (
        not isinstance(endpoint, ipaddress.IPv4Interface)
        or str(endpoint) != raw_value
        or len(matching) != 1
        or endpoint.network != matching[0][0]
        or endpoint.ip in {
            matching[0][0].network_address,
            matching[0][0].broadcast_address,
            matching[0][1],
        }
        or endpoint.ip.is_unspecified
        or endpoint.ip.is_loopback
        or endpoint.ip.is_multicast
    ):
        _die("Docker network endpoint IPv4/prefix is outside usable IPAM hosts")
    return endpoint


def _verify_container_state(state: dict[str, Any], running: bool) -> None:
    expected_status = "running" if running else "exited"
    finished_at = state.get("FinishedAt")
    if (
        state.get("Status") != expected_status
        or state.get("Running") is not running
        or state.get("Paused") is not False
        or state.get("Restarting") is not False
        or state.get("Dead") is not False
        or type(state.get("ExitCode")) is not int
        or state.get("ExitCode") != 0
    ):
        _die("container runtime state is not the exact stable capture phase")
    if running and finished_at == DOCKER_ZERO_TIME:
        return
    if not isinstance(finished_at, str) or not DOCKER_FINISHED_AT_RE.fullmatch(finished_at):
        _die("container Docker FinishedAt is invalid")
    try:
        finished_second = dt.datetime.strptime(finished_at[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        _die(f"container Docker FinishedAt is invalid: {exc}")
    if finished_at == DOCKER_ZERO_TIME or finished_second.year < 1970:
        _die("container Docker FinishedAt is not a valid non-zero exit timestamp")


def _verify_container_topology(
    compose: dict[str, Any],
    service: str,
    definition: dict[str, Any],
    container: dict[str, Any],
    running: bool,
    network_evidence: dict[
        str,
        tuple[
            dict[str, Any],
            tuple[tuple[ipaddress.IPv4Network, ipaddress.IPv4Address], ...],
        ],
    ],
) -> None:
    expected_name = definition.get("container_name")
    if expected_name != service:
        _die("resolved Compose container_name differs from the exact service name")

    compose_networks = definition.get("networks")
    if not isinstance(compose_networks, dict) or not compose_networks:
        _die("resolved Compose service network set is invalid")
    expected_networks: set[str] = set()
    for source, attachment in compose_networks.items():
        if attachment not in (None, {}):
            _die("resolved Compose service has unsupported network attachment options")
        expected_networks.add(_resolved_resource_name(compose, "networks", source))
    network_settings = container.get("NetworkSettings")
    actual_networks = network_settings.get("Networks") \
        if isinstance(network_settings, dict) else None
    if not isinstance(actual_networks, dict) or set(actual_networks) != expected_networks:
        _die("running container network set differs from resolved Compose")
    container_id = container.get("Id")
    container_name = container.get("Name")
    for network_name, network in actual_networks.items():
        aliases = network.get("Aliases") if isinstance(network, dict) else None
        record, ipam = network_evidence[network_name]
        network_id = record.get("Id")
        record_containers = record.get("Containers")
        endpoint = record_containers.get(container_id) \
            if isinstance(record_containers, dict) else None
        endpoint_id = network.get("EndpointID") if isinstance(network, dict) else None
        if aliases != [service, service]:
            _die("running container network aliases differ from resolved Compose")
        if not isinstance(network, dict) or network.get("NetworkID") != network_id:
            _die("container/network inspect network identity differs")
        if not running:
            if (
                endpoint is not None
                or network.get("EndpointID") != ""
                or network.get("IPAddress") != ""
                or network.get("IPPrefixLen") != 0
                or network.get("GlobalIPv6Address") != ""
                or network.get("GlobalIPv6PrefixLen") != 0
                or network.get("MacAddress") != ""
            ):
                _die("stopped container unexpectedly retains an active network endpoint")
            continue
        if (
            not isinstance(endpoint, dict)
            or endpoint.get("Name") != str(container_name).removeprefix("/")
            or not isinstance(endpoint_id, str)
            or not SHA256_RE.fullmatch(endpoint_id)
            or endpoint.get("EndpointID") != endpoint_id
        ):
            _die("container/network inspect endpoint identity differs")
        ipv4 = _network_endpoint_ipv4(endpoint.get("IPv4Address"), ipam)
        if (
            str(ipv4.ip) != network.get("IPAddress")
            or ipv4.network.prefixlen != network.get("IPPrefixLen")
        ):
            _die("container/network inspect IPv4 address/prefix differs")
        if (
            endpoint.get("IPv6Address") != ""
            or network.get("GlobalIPv6Address") != ""
            or network.get("GlobalIPv6PrefixLen") != 0
        ):
            _die("IPv6 is unexpectedly enabled on a Postiz runtime network")
        mac_address = network.get("MacAddress")
        if (
            not isinstance(mac_address, str)
            or not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", mac_address)
            or mac_address == "00:00:00:00:00:00"
            or int(mac_address[:2], 16) & 1
            or endpoint.get("MacAddress") != mac_address
        ):
            _die("container/network inspect MAC address differs or is invalid")

    compose_mounts = definition.get("volumes", [])
    if compose_mounts is None:
        compose_mounts = []
    if not isinstance(compose_mounts, list):
        _die("resolved Compose service volume set is invalid")
    expected_mounts: set[tuple[str, str, str, bool]] = set()
    allowed_mount_fields = {"type", "source", "target", "read_only", "volume"}
    for mount in compose_mounts:
        if not isinstance(mount, dict) or not {"type", "source", "target"} <= set(mount) \
                or set(mount) - allowed_mount_fields:
            _die("resolved Compose volume attachment shape is invalid")
        if mount.get("type") != "volume" or mount.get("volume", {}) != {}:
            _die("resolved Compose service has an unsupported volume attachment")
        target = mount.get("target")
        read_only = mount.get("read_only", False)
        if not isinstance(target, str) or not target.startswith("/") or not isinstance(read_only, bool):
            _die("resolved Compose volume target/options are invalid")
        expected_mounts.add(
            (
                "volume",
                _resolved_resource_name(compose, "volumes", mount.get("source")),
                target,
                not read_only,
            )
        )
    raw_mounts = container.get("Mounts")
    if not isinstance(raw_mounts, list):
        _die("container runtime mount evidence is invalid")
    actual_mounts: set[tuple[str, str, str, bool]] = set()
    for mount in raw_mounts:
        if not isinstance(mount, dict):
            _die("container runtime mount entry is invalid")
        item = (mount.get("Type"), mount.get("Name"), mount.get("Destination"), mount.get("RW"))
        if (
            not all(isinstance(value, str) for value in item[:3])
            or not isinstance(item[3], bool)
            or item in actual_mounts
        ):
            _die("container runtime mount entry is invalid or duplicate")
        actual_mounts.add(item)
    if actual_mounts != expected_mounts:
        _die("running container mount set differs from resolved Compose")

    if definition.get("ports") not in (None, []):
        _die("resolved Compose unexpectedly publishes a host port")
    host_config = container.get("HostConfig")
    port_bindings = host_config.get("PortBindings") if isinstance(host_config, dict) else None
    if not isinstance(host_config, dict) or port_bindings not in (None, {}):
        _die("running container unexpectedly publishes a host port")


def _parse_environment_list(value: Any, label: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, list):
        _die(f"{label} is not a list")
    environment: dict[str, str] = {}
    for item in value:
        if not isinstance(item, str) or "=" not in item:
            _die(f"{label} has an invalid entry")
        key, item_value = item.split("=", 1)
        if not key or key in environment:
            _die(f"{label} has a duplicate/empty key")
        environment[key] = item_value
    return environment


def _compose_environment(definition: dict[str, Any]) -> dict[str, str]:
    value = definition.get("environment", {})
    if value is None:
        value = {}
    if not isinstance(value, dict):
        _die("resolved Compose environment is not canonical")
    environment: dict[str, str] = {}
    for key, item_value in value.items():
        if not isinstance(key, str) or not key:
            _die("resolved Compose environment key is invalid")
        if item_value is None:
            expected_value = ""
        elif isinstance(item_value, str):
            expected_value = item_value
        else:
            _die("resolved Compose environment value is invalid")
        environment[key] = expected_value
    return environment


def _load_expected_service_images(values: list[str]) -> dict[str, str]:
    images: dict[str, str] = {}
    for value in values:
        parts = value.split("|")
        if (
            len(parts) != 2
            or parts[0] not in JOURNAL_SERVICES
            or parts[0] in images
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", parts[1])
            or parts[1] in images.values()
        ):
            _die("expected service/image identity is invalid or duplicate")
        images[parts[0]] = parts[1]
    if set(images) != JOURNAL_SERVICES:
        _die("expected service/image identity set differs")
    return images


def _load_image_inspect_evidence(
    path: Path, expected_images: dict[str, str]
) -> dict[str, dict[str, str]]:
    value = _load_json_value(path)
    if not isinstance(value, list) or len(value) != len(JOURNAL_SERVICES):
        _die("Docker image inspect evidence lacks the exact Postiz image set")
    images: dict[str, dict[str, str]] = {}
    for record in value:
        if not isinstance(record, dict):
            _die("Docker image inspect evidence has an invalid record")
        image_id = record.get("Id")
        config = record.get("Config")
        if (
            not isinstance(image_id, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
            or image_id in images
            or not isinstance(config, dict)
            or "Env" not in config
        ):
            _die("Docker image inspect identity/config evidence is invalid or duplicate")
        images[image_id] = _parse_environment_list(
            config.get("Env"), "Docker image default environment"
        )
    if set(images) != set(expected_images.values()):
        _die("Docker image inspect IDs differ from the expected service/image set")
    return images


def _load_network_inspect_evidence(
    path: Path, expected_names: set[str]
) -> dict[
    str,
    tuple[
        dict[str, Any],
        tuple[tuple[ipaddress.IPv4Network, ipaddress.IPv4Address], ...],
    ],
]:
    value = _load_json_value(path)
    if not isinstance(value, list) or len(value) != len(expected_names):
        _die("Docker network inspect evidence lacks the exact Compose network set")
    networks: dict[
        str,
        tuple[
            dict[str, Any],
            tuple[tuple[ipaddress.IPv4Network, ipaddress.IPv4Address], ...],
        ],
    ] = {}
    network_ids: set[str] = set()
    endpoint_ids: set[str] = set()
    all_subnets: list[ipaddress.IPv4Network] = []
    for record in value:
        if not isinstance(record, dict):
            _die("Docker network inspect evidence has an invalid record")
        name = record.get("Name")
        network_id = record.get("Id")
        containers = record.get("Containers")
        ipam = record.get("IPAM")
        raw_subnets = ipam.get("Config") if isinstance(ipam, dict) else None
        if (
            not isinstance(name, str)
            or name not in expected_names
            or name in networks
            or not isinstance(network_id, str)
            or not SHA256_RE.fullmatch(network_id)
            or network_id in network_ids
            or record.get("EnableIPv6") is not False
            or not isinstance(containers, dict)
            or any(not isinstance(key, str) or not SHA256_RE.fullmatch(key) for key in containers)
            or not isinstance(raw_subnets, list)
            or not raw_subnets
        ):
            _die("Docker network inspect identity/IPAM evidence is invalid or duplicate")
        network_ids.add(network_id)
        ipam_entries: list[tuple[ipaddress.IPv4Network, ipaddress.IPv4Address]] = []
        try:
            for item in raw_subnets:
                raw_subnet = item.get("Subnet") if isinstance(item, dict) else None
                raw_gateway = item.get("Gateway") if isinstance(item, dict) else None
                if not isinstance(raw_subnet, str) or not isinstance(raw_gateway, str):
                    _die("Docker network inspect IPAM subnet evidence is invalid")
                subnet = ipaddress.ip_network(raw_subnet, strict=True)
                gateway = ipaddress.ip_address(raw_gateway)
                if (
                    not isinstance(subnet, ipaddress.IPv4Network)
                    or not isinstance(gateway, ipaddress.IPv4Address)
                    or str(subnet) != raw_subnet
                    or str(gateway) != raw_gateway
                    or subnet.num_addresses < 4
                    or gateway not in subnet
                    or gateway in {subnet.network_address, subnet.broadcast_address}
                    or gateway.is_unspecified
                    or gateway.is_loopback
                    or gateway.is_multicast
                    or any(subnet.overlaps(existing) for existing in all_subnets)
                ):
                    _die(
                        "Docker network inspect IPv4 subnet/Gateway is invalid or overlapping"
                    )
                ipam_entries.append((subnet, gateway))
                all_subnets.append(subnet)
        except ValueError as exc:
            _die(f"Docker network inspect IPAM subnet evidence is invalid: {exc}")
        network_ipv4: set[ipaddress.IPv4Address] = set()
        network_macs: set[str] = set()
        for container_id, endpoint in containers.items():
            endpoint_id = endpoint.get("EndpointID") if isinstance(endpoint, dict) else None
            endpoint_name = endpoint.get("Name") if isinstance(endpoint, dict) else None
            mac_address = endpoint.get("MacAddress") if isinstance(endpoint, dict) else None
            if (
                not isinstance(endpoint_name, str)
                or not endpoint_name
                or not isinstance(endpoint_id, str)
                or not SHA256_RE.fullmatch(endpoint_id)
                or endpoint_id in endpoint_ids
                or not isinstance(mac_address, str)
                or not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", mac_address)
                or mac_address == "00:00:00:00:00:00"
                or int(mac_address[:2], 16) & 1
                or mac_address in network_macs
                or endpoint.get("IPv6Address") != ""
            ):
                _die("Docker network endpoint identity/MAC/IPv6 evidence is invalid")
            ipv4 = _network_endpoint_ipv4(endpoint.get("IPv4Address"), tuple(ipam_entries))
            if ipv4.ip in network_ipv4:
                _die("Docker network endpoint IPv4 address is duplicate")
            endpoint_ids.add(endpoint_id)
            network_macs.add(mac_address)
            network_ipv4.add(ipv4.ip)
        networks[name] = (record, tuple(ipam_entries))
    if set(networks) != expected_names:
        _die("Docker network inspect name set differs from resolved Compose")
    return networks


def command_verify_compose_runtime(args: argparse.Namespace) -> None:
    compose, services = _load_exact_compose_model(
        Path(args.compose_json), "resolved full Compose runtime"
    )
    no_deps_compose, _no_deps_services = _load_exact_compose_model(
        Path(args.postiz_no_deps_compose_json), "resolved Postiz --no-deps Compose runtime"
    )
    if no_deps_compose != _postiz_no_deps_projection(compose):
        _die("resolved Postiz --no-deps model differs by more than depends_on")
    containers = _load_json_value(Path(args.container_json))
    if not isinstance(containers, list) or len(containers) != len(JOURNAL_SERVICES):
        _die("container runtime evidence lacks the exact Postiz service set")
    expected_images = _load_expected_service_images(args.expected_image)
    images = _load_image_inspect_evidence(Path(args.image_inspect_json), expected_images)
    network_names = _resolved_network_names(compose)
    referenced_network_sources: set[str] = set()
    for definition in services.values():
        service_networks = definition.get("networks")
        if not isinstance(service_networks, dict) or not service_networks:
            _die("resolved Compose service network set is invalid")
        for source in service_networks:
            if source not in network_names:
                _die("resolved Compose service references an unknown network resource")
            referenced_network_sources.add(source)
    if referenced_network_sources != set(network_names):
        _die("resolved Compose network resource map is not exactly referenced")
    networks = _load_network_inspect_evidence(
        Path(args.network_inspect_json), set(network_names.values())
    )
    hashes = _load_compose_hashes(
        Path(args.compose_hashes), JOURNAL_SERVICES, "source-full Compose service hash output"
    )
    resolved_hashes = _load_compose_hashes(
        Path(args.resolved_compose_hashes),
        JOURNAL_SERVICES,
        "reparsed resolved Compose service hash output",
    )
    no_deps_hash = _load_compose_hashes(
        Path(args.postiz_no_deps_hash), {"postiz"}, "resolved Postiz --no-deps hash output"
    )["postiz"]
    if resolved_hashes["postiz"] != no_deps_hash:
        _die("Postiz resolved and --no-deps effective Compose hashes differ")
    if hashes["postiz"] == resolved_hashes["postiz"]:
        _die("Postiz source-full and resolved effective Compose hashes unexpectedly match")
    for service in JOURNAL_SERVICES - {"postiz"}:
        if hashes[service] != resolved_hashes[service]:
            _die("non-Postiz source-full and resolved Compose hashes differ")
    expected_running_services = (
        JOURNAL_SERVICES if args.runtime_state == "preflight" else {"postiz-postgres"}
    )
    actual_by_name: dict[str, dict[str, str]] = {}
    container_ids_by_service: dict[str, str] = {}
    image_ids_by_service: dict[str, str] = {}
    container_ids: set[str] = set()
    used_image_ids: set[str] = set()
    for container in containers:
        if not isinstance(container, dict):
            _die("invalid container runtime evidence")
        container_id = container.get("Id")
        image_id = container.get("Image")
        name = container.get("Name")
        config = container.get("Config")
        state = container.get("State")
        raw_environment = config.get("Env") if isinstance(config, dict) else None
        if (
            not isinstance(container_id, str)
            or not SHA256_RE.fullmatch(container_id)
            or container_id in container_ids
            or not isinstance(image_id, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
            or image_id not in images
            or image_id in used_image_ids
            or not isinstance(name, str)
            or not name.startswith("/")
            or not isinstance(state, dict)
        ):
            _die("invalid or duplicate container/image runtime identity")
        container_ids.add(container_id)
        used_image_ids.add(image_id)
        labels = config.get("Labels") if isinstance(config, dict) else None
        service = labels.get("com.docker.compose.service") if isinstance(labels, dict) else None
        expected_name = (
            "/postiz-postgres-backup-fenced"
            if args.runtime_state == "writer-fenced" and service == "postiz-postgres"
            else f"/{service}"
        )
        if service not in JOURNAL_SERVICES or service in actual_by_name or name != expected_name:
            _die("container runtime service/name set differs")
        running = service in expected_running_services
        _verify_container_state(state, running)
        if image_id != expected_images[service]:
            _die("running container image ID differs from the expected pinned service image")
        expected_label_hash = no_deps_hash if service == "postiz" else hashes[service]
        if (
            not isinstance(labels, dict)
            or labels.get("com.docker.compose.project") != "postiz"
            or labels.get("com.docker.compose.service") != service
            or labels.get("com.docker.compose.config-hash") != expected_label_hash
        ):
            _die("running container is not the exact resolved Compose generation")
        if service == "postiz" and labels.get("com.docker.compose.depends_on") != "":
            _die("running Postiz dependency label is not the exact --no-deps generation")
        expected_image = services[service].get("image") if isinstance(services[service], dict) else None
        if not isinstance(expected_image, str) or config.get("Image") != expected_image:
            _die("running container configured image reference differs from Compose")
        _verify_container_topology(
            compose, service, services[service], container, running, networks
        )
        environment = _parse_environment_list(raw_environment, "running container environment")
        actual_by_name[service] = environment
        container_ids_by_service[service] = container_id
        image_ids_by_service[service] = image_id
    if set(actual_by_name) != JOURNAL_SERVICES:
        _die("container runtime service set differs")
    if used_image_ids != set(images):
        _die("container runtime does not bind the exact Docker image inspect set")
    compose_network_definitions = compose.get("networks")
    if not isinstance(compose_network_definitions, dict):
        _die("resolved Compose network definitions are invalid")
    all_container_ids = set(container_ids_by_service.values())
    for source, definition in compose_network_definitions.items():
        network_name = _resolved_resource_name(compose, "networks", source)
        if network_name not in networks:
            continue
        external = definition.get("external", False) if isinstance(definition, dict) else None
        if not isinstance(external, bool):
            _die("resolved Compose network definition is invalid")
        expected_ids = {
            container_ids_by_service[service]
            for service, service_definition in services.items()
            if service in expected_running_services
            and source in service_definition.get("networks", {})
        }
        record_containers = networks[network_name][0].get("Containers")
        actual_ids = set(record_containers) if isinstance(record_containers, dict) else set()
        if actual_ids & all_container_ids != expected_ids:
            _die("Docker network membership differs for the exact Postiz container IDs")
        if not external and actual_ids != expected_ids:
            _die("private Postiz Docker network contains an unexpected container")
    for service, definition in services.items():
        expected_environment = dict(images[image_ids_by_service[service]])
        expected_environment.update(_compose_environment(definition))
        if actual_by_name[service] != expected_environment:
            _die("running container environment differs from image defaults plus Compose")


def command_config_archive_get(args: argparse.Namespace) -> None:
    command_verify_config_archive(
        argparse.Namespace(archive=args.archive, compose_sha256=None, dockerfile_sha256=None)
    )
    member_name = {
        "compose_sha256": "srv/postiz/docker-compose.yml",
        "dockerfile_sha256": "srv/postiz/Dockerfile.patch",
        "source_revision": "etc/homelab/postiz-backup-source-revision",
    }[args.key]
    with tarfile.open(Path(args.archive), "r:gz") as bundle:
        member = bundle.getmember(member_name)
        extracted = bundle.extractfile(member)
        if extracted is None:
            _die("cannot read config archive member")
        if args.key == "source_revision":
            value = extracted.read(42)
            if not re.fullmatch(rb"[0-9a-f]{40}\n", value):
                _die("backup source revision is invalid")
            print(value.decode("ascii").strip())
            return
        digest = hashlib.sha256()
        for chunk in iter(lambda: extracted.read(1024 * 1024), b""):
            digest.update(chunk)
    print(digest.hexdigest())


def command_verify_physical_archive(args: argparse.Namespace) -> None:
    archive = Path(args.archive)
    total = 0
    count = 0
    max_members = getattr(args, "max_members", MAX_PHYSICAL_ARCHIVE_MEMBERS)
    if args.max_bytes < 1 or max_members < 1 or max_members > MAX_PHYSICAL_ARCHIVE_MEMBERS:
        _die("physical Postgres archive ceilings are invalid")
    try:
        # Stream the archive so the member ceiling also bounds verifier memory.  In
        # particular, never materialize an attacker-controlled million-entry list.
        with tarfile.open(archive, "r|gz") as bundle:
            names: set[str] = set()
            version: bytes | None = None
            manifest_is_file = False
            for member in bundle:
                count += 1
                if count > max_members:
                    _die("physical Postgres archive exceeds member ceiling")
                name = _safe_tar_name(member)
                if name in names:
                    _die("physical Postgres archive has duplicate paths")
                names.add(name)
                if not member.isdir() and not member.isfile():
                    _die("physical Postgres archive contains a link/special member")
                if member.isfile():
                    total += member.size
                    if total > args.max_bytes:
                        _die("physical Postgres archive exceeds byte ceiling")
                if name == "PG_VERSION":
                    if not member.isfile() or member.size > 16:
                        _die("physical Postgres archive has an invalid PG_VERSION")
                    version_handle = bundle.extractfile(member)
                    if version_handle is None:
                        _die("physical Postgres archive has an unreadable PG_VERSION")
                    version = version_handle.read(17)
                    if len(version) != member.size:
                        _die("physical Postgres archive PG_VERSION size differs")
                elif name == "backup_manifest":
                    manifest_is_file = member.isfile()
            if "PG_VERSION" not in names or not manifest_is_file:
                _die("physical Postgres archive lacks version/manifest")
            if version is None or version.strip() != b"17":
                _die("physical Postgres archive is not major version 17")
    except (OSError, EOFError, gzip.BadGzipFile, KeyError, tarfile.TarError) as exc:
        _die(f"invalid physical Postgres archive: {exc}")


def _tar_member_bytes(
    bundle: tarfile.TarFile, member: tarfile.TarInfo, ceiling: int, label: str
) -> bytes:
    if not member.isfile() or member.size > ceiling:
        _die(f"{label} is missing or exceeds its byte ceiling")
    handle = bundle.extractfile(member)
    if handle is None:
        _die(f"cannot read {label}")
    content = handle.read(ceiling + 1)
    if len(content) != member.size or len(content) > ceiling:
        _die(f"{label} size differs")
    return content


def _validate_hybrid_legacy_metadata(
    values: list[dict[str, Any]], config: dict[str, Any], layer_count: int
) -> None:
    if len(values) != layer_count:
        _die("Docker hybrid archive lacks its exact legacy metadata chain")
    allowed = {
        "id",
        "parent",
        "comment",
        "created",
        "container",
        "container_config",
        "docker_version",
        "author",
        "config",
        "architecture",
        "variant",
        "os",
        "Size",
    }
    ids: dict[str, dict[str, Any]] = {}
    for value in values:
        if not isinstance(value, dict) or not set(value) <= allowed:
            _die("Docker hybrid legacy metadata fields differ")
        image_id = value.get("id")
        parent = value.get("parent")
        if not isinstance(image_id, str) or not SHA256_RE.fullmatch(image_id) or image_id in ids:
            _die("Docker hybrid legacy metadata ID is invalid or duplicate")
        if parent is not None and (not isinstance(parent, str) or not SHA256_RE.fullmatch(parent)):
            _die("Docker hybrid legacy metadata parent is invalid")
        if not isinstance(value.get("container_config"), dict):
            _die("Docker hybrid legacy container config differs")
        if "config" in value and value["config"] is not None and not isinstance(value["config"], dict):
            _die("Docker hybrid legacy runtime config differs")
        if "created" not in value or value["created"] is not None and not isinstance(value["created"], str):
            _die("Docker hybrid legacy creation time differs")
        for key in ("comment", "container", "docker_version", "author", "architecture", "variant", "os"):
            if key in value and not isinstance(value[key], str):
                _die("Docker hybrid legacy string metadata differs")
        if "Size" in value and (
            not isinstance(value["Size"], int) or isinstance(value["Size"], bool) or value["Size"] < 0
        ):
            _die("Docker hybrid legacy size metadata differs")
        ids[image_id] = value
    roots = [value for value in values if "parent" not in value]
    children: dict[str, str] = {}
    for value in values:
        parent = value.get("parent")
        if parent is None:
            continue
        if parent not in ids or parent in children:
            _die("Docker hybrid legacy parent chain differs")
        children[parent] = value["id"]
    if len(roots) != 1:
        _die("Docker hybrid legacy metadata must have one root")
    visited: set[str] = set()
    current = roots[0]["id"]
    while current not in visited:
        visited.add(current)
        if current not in children:
            break
        current = children[current]
    if len(visited) != layer_count or current in children:
        _die("Docker hybrid legacy metadata is not one exact chain")
    leaf = ids[current]
    for key in (
        "comment",
        "created",
        "container",
        "docker_version",
        "author",
        "architecture",
        "variant",
        "os",
    ):
        if key in config and leaf.get(key) != config[key]:
            _die("Docker hybrid legacy leaf does not bind to the image config")
    sparse_config = config.get("config")
    expanded_config = leaf.get("config")
    if not isinstance(sparse_config, dict) or not isinstance(expanded_config, dict):
        _die("Docker hybrid legacy leaf lacks runtime config binding")

    def zero_value(value: Any) -> bool:
        return value is None or value is False or value == "" or value == 0 or value == [] or value == {}

    for key, value in sparse_config.items():
        if expanded_config.get(key) != value:
            _die("Docker hybrid legacy runtime config differs from image config")
    if any(key not in sparse_config and not zero_value(value) for key, value in expanded_config.items()):
        _die("Docker hybrid legacy runtime config has an unbound nonzero field")


def command_verify_image_archive(args: argparse.Namespace) -> None:
    expected = args.image_id.removeprefix("sha256:")
    if not SHA256_RE.fullmatch(expected):
        _die("invalid expected image ID")
    archive = Path(args.archive)
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            members: dict[str, tarfile.TarInfo] = {}
            archive_member_bytes = 0
            for member in bundle:
                name = _safe_tar_name(member)
                if name in members:
                    _die("Docker archive contains duplicate paths")
                if not member.isdir() and not member.isfile():
                    _die("Docker archive contains a non-file member")
                members[name] = member
                if len(members) > 100_000:
                    _die("Docker archive exceeds member ceiling")
                if member.isfile():
                    archive_member_bytes += member.size
                    if archive_member_bytes > 34 * 1024**3:
                        _die("Docker archive exceeds member-byte ceiling")
            manifest_member = members.get("manifest.json")
            if manifest_member is None:
                _die("Docker archive lacks a bounded manifest")
            manifest_bytes = _tar_member_bytes(
                bundle, manifest_member, 4 * 1024**2, "Docker image manifest"
            )
    except (OSError, EOFError, gzip.BadGzipFile, tarfile.TarError) as exc:
        _die(f"invalid image archive: {exc}")
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _die(f"invalid Docker image manifest: {exc}")
    if not isinstance(manifest, list) or len(manifest) != 1 or not isinstance(manifest[0], dict):
        _die("Docker image archive must contain exactly one image")
    entry = manifest[0]
    legacy_fields = {"Config", "RepoTags", "Layers"}
    hybrid_fields = {*legacy_fields, "LayerSources"}
    if set(entry) not in (legacy_fields, hybrid_fields):
        _die("Docker image manifest fields differ")
    hybrid = set(entry) == hybrid_fields
    if entry.get("RepoTags") not in (None, []):
        _die("content-addressed Docker archive must not carry mutable tags")
    config_name = _validate_safe_path(entry.get("Config"), "Docker config path")
    expected_config_names = {f"{expected}.json"}
    if hybrid:
        expected_config_names = {f"blobs/sha256/{expected}"}
    if config_name not in expected_config_names:
        _die("Docker image archive manifest points at another image")
    layers_raw = entry.get("Layers")
    if not isinstance(layers_raw, list) or not layers_raw:
        _die("Docker image archive has no layer list")
    layers = [_validate_safe_path(item, "Docker layer path") for item in layers_raw]
    if len(layers) != len(set(layers)):
        _die("Docker image archive layer list is not canonical")
    expanded_total = 0
    expanded_inodes = 0
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            member_by_name = {_safe_tar_name(member): member for member in bundle.getmembers()}
            config_member = member_by_name.get(config_name)
            if config_member is None:
                _die("Docker image archive lacks its bounded config")
            config_bytes = _tar_member_bytes(
                bundle, config_member, 16 * 1024**2, "Docker image config"
            )
            if hashlib.sha256(config_bytes).hexdigest() != expected:
                _die("Docker image config digest differs from the image ID")
            try:
                config = json.loads(config_bytes)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                _die(f"invalid Docker image config: {exc}")
            diff_ids = config.get("rootfs", {}).get("diff_ids") if isinstance(config, dict) else None
            if (
                not isinstance(diff_ids, list)
                or len(diff_ids) != len(layers)
                or not all(isinstance(item, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", item) for item in diff_ids)
            ):
                _die("Docker config diff_ids do not match the layer set")

            hybrid_blob_names: set[str] = set()
            if hybrid:
                files = {name for name, member in member_by_name.items() if member.isfile()}
                directories = {name for name, member in member_by_name.items() if member.isdir()}
                if directories != {"blobs", "blobs/sha256"}:
                    _die("Docker hybrid archive directory set differs")
                for name in files:
                    if name in {"manifest.json", "index.json", "oci-layout"}:
                        continue
                    if not re.fullmatch(r"blobs/sha256/[0-9a-f]{64}", name):
                        _die("Docker hybrid archive contains a non-content-addressed blob")
                    hybrid_blob_names.add(name)
                if files != {"manifest.json", "index.json", "oci-layout", *hybrid_blob_names}:
                    _die("Docker hybrid archive file set differs")
                for name in hybrid_blob_names - set(layers):
                    member = member_by_name[name]
                    content = _tar_member_bytes(
                        bundle, member, 16 * 1024**2, "Docker hybrid metadata blob"
                    )
                    if hashlib.sha256(content).hexdigest() != name.rsplit("/", 1)[1]:
                        _die("Docker hybrid metadata blob digest differs from its path")
                layout_member = member_by_name.get("oci-layout")
                index_member = member_by_name.get("index.json")
                if layout_member is None or index_member is None:
                    _die("Docker hybrid archive lacks OCI layout metadata")
                try:
                    layout = json.loads(
                        _tar_member_bytes(bundle, layout_member, 1024, "OCI layout")
                    )
                    index = json.loads(
                        _tar_member_bytes(bundle, index_member, 4 * 1024**2, "OCI index")
                    )
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    _die(f"invalid Docker hybrid OCI metadata: {exc}")
                if layout != {"imageLayoutVersion": "1.0.0"}:
                    _die("Docker hybrid OCI layout version differs")
                if (
                    not isinstance(index, dict)
                    or set(index) != {"schemaVersion", "mediaType", "manifests"}
                    or index.get("schemaVersion") != 2
                    or index.get("mediaType") != "application/vnd.oci.image.index.v1+json"
                    or not isinstance(index.get("manifests"), list)
                    or len(index["manifests"]) != 1
                    or not isinstance(index["manifests"][0], dict)
                    or set(index["manifests"][0]) != {"mediaType", "digest", "size"}
                ):
                    _die("Docker hybrid OCI index differs")
                index_descriptor = index["manifests"][0]
                manifest_digest = index_descriptor.get("digest")
                if (
                    index_descriptor.get("mediaType")
                    != "application/vnd.oci.image.manifest.v1+json"
                    or not isinstance(manifest_digest, str)
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}", manifest_digest)
                    or not isinstance(index_descriptor.get("size"), int)
                    or isinstance(index_descriptor.get("size"), bool)
                ):
                    _die("Docker hybrid OCI manifest descriptor differs")
                oci_manifest_name = f"blobs/sha256/{manifest_digest.removeprefix('sha256:')}"
                oci_manifest_member = member_by_name.get(oci_manifest_name)
                if oci_manifest_member is None or oci_manifest_member.size != index_descriptor["size"]:
                    _die("Docker hybrid OCI manifest size differs")
                oci_manifest_bytes = _tar_member_bytes(
                    bundle, oci_manifest_member, 4 * 1024**2, "OCI image manifest"
                )
                if hashlib.sha256(oci_manifest_bytes).hexdigest() != manifest_digest.removeprefix("sha256:"):
                    _die("Docker hybrid OCI manifest digest differs")
                try:
                    oci_manifest = json.loads(oci_manifest_bytes)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    _die(f"invalid Docker hybrid OCI manifest: {exc}")
                if (
                    not isinstance(oci_manifest, dict)
                    or set(oci_manifest) != {"schemaVersion", "mediaType", "config", "layers"}
                    or oci_manifest.get("schemaVersion") != 2
                    or oci_manifest.get("mediaType")
                    != "application/vnd.oci.image.manifest.v1+json"
                    or not isinstance(oci_manifest.get("config"), dict)
                    or set(oci_manifest["config"]) != {"mediaType", "digest", "size"}
                    or oci_manifest["config"]
                    != {
                        "mediaType": "application/vnd.oci.image.config.v1+json",
                        "digest": f"sha256:{expected}",
                        "size": config_member.size,
                    }
                    or not isinstance(oci_manifest.get("layers"), list)
                    or len(oci_manifest["layers"]) != len(layers)
                ):
                    _die("Docker hybrid OCI image graph differs")
                layer_sources = entry.get("LayerSources")
                if not isinstance(layer_sources, dict) or set(layer_sources) != set(diff_ids):
                    _die("Docker hybrid LayerSources differ from config diff_ids")
                for layer_name, diff_id, descriptor in zip(
                    layers, diff_ids, oci_manifest["layers"], strict=True
                ):
                    layer_member = member_by_name.get(layer_name)
                    if (
                        not re.fullmatch(r"blobs/sha256/[0-9a-f]{64}", layer_name)
                        or layer_name.rsplit("/", 1)[1] != diff_id.removeprefix("sha256:")
                        or layer_member is None
                        or not layer_member.isfile()
                        or not isinstance(descriptor, dict)
                        or set(descriptor) != {"mediaType", "digest", "size"}
                    ):
                        _die("Docker hybrid layer path/descriptor differs")
                    expected_descriptor = {
                        "mediaType": "application/vnd.oci.image.layer.v1.tar",
                        "digest": diff_id,
                        "size": layer_member.size,
                    }
                    if descriptor != expected_descriptor or layer_sources[diff_id] != expected_descriptor:
                        _die("Docker hybrid layer graph differs")
                referenced_blobs = {config_name, oci_manifest_name, *layers}
                legacy_values: list[dict[str, Any]] = []
                for name in sorted(hybrid_blob_names - referenced_blobs):
                    member = member_by_name[name]
                    try:
                        value = json.loads(
                            _tar_member_bytes(
                                bundle, member, 4 * 1024**2, "Docker legacy metadata"
                            )
                        )
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        _die(f"invalid Docker hybrid legacy metadata: {exc}")
                    legacy_values.append(value)
                _validate_hybrid_legacy_metadata(legacy_values, config, len(layers))

            for layer_name, expected_diff_id in zip(layers, diff_ids, strict=True):
                layer_member = member_by_name.get(layer_name)
                if layer_member is None or not layer_member.isfile():
                    _die("Docker archive lacks a referenced layer")
                layer_handle = bundle.extractfile(layer_member)
                if layer_handle is None:
                    _die("cannot read Docker layer")
                prefix = layer_handle.read(2)
                layer_handle.seek(0)
                blob_digest = hashlib.sha256()

                class BlobReader:
                    def read(self, size: int = -1) -> bytes:
                        chunk = layer_handle.read(size)
                        blob_digest.update(chunk)
                        return chunk

                blob_reader = BlobReader()
                stream: Any
                if prefix == b"\x1f\x8b":
                    stream = gzip.GzipFile(fileobj=blob_reader)
                else:
                    stream = blob_reader
                digest = hashlib.sha256()

                class LayerReader:
                    def read(self, size: int = -1) -> bytes:
                        nonlocal expanded_total
                        chunk = stream.read(size)
                        expanded_total += len(chunk)
                        if expanded_total > 32 * 1024**3:
                            _die("Docker image layers exceed expanded byte ceiling")
                        digest.update(chunk)
                        return chunk

                reader = LayerReader()
                try:
                    with tarfile.open(fileobj=reader, mode="r|") as layer_bundle:
                        for layer_entry in layer_bundle:
                            expanded_inodes += 1
                            if expanded_inodes > 1_000_000:
                                _die("Docker image layers exceed expanded inode ceiling")
                            layer_path = layer_entry.name.removeprefix("./").rstrip("/")
                            layer_pure = PurePosixPath(layer_path)
                            if (
                                not layer_path
                                or layer_pure.is_absolute()
                                or ".." in layer_pure.parts
                                or "." in layer_pure.parts
                            ):
                                _die("Docker image layer contains an unsafe path")
                    for _chunk in iter(lambda: reader.read(1024 * 1024), b""):
                        pass
                except (EOFError, gzip.BadGzipFile, tarfile.TarError) as exc:
                    _die(f"invalid Docker layer tar: {exc}")
                if f"sha256:{digest.hexdigest()}" != expected_diff_id:
                    _die("Docker layer digest differs from config rootfs.diff_ids")
                if hybrid and blob_digest.hexdigest() != layer_name.rsplit("/", 1)[1]:
                    _die("Docker hybrid layer blob digest differs from its path")

            if not hybrid:
                referenced = {"manifest.json", config_name, *layers}
                allowed_layer_metadata: set[str] = set()
                for layer_name in layers:
                    layer_parent = PurePosixPath(layer_name).parent
                    if str(layer_parent) != ".":
                        allowed_layer_metadata.add(f"{layer_parent.as_posix()}/VERSION")
                        allowed_layer_metadata.add(f"{layer_parent.as_posix()}/json")
                unexpected_payloads = set()
                for name, member in member_by_name.items():
                    if not member.isfile() or name in referenced:
                        continue
                    if name == "repositories":
                        repositories_bytes = _tar_member_bytes(
                            bundle, member, 1024 * 1024, "Docker repositories metadata"
                        )
                        try:
                            repositories = json.loads(repositories_bytes)
                        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                            _die(f"invalid Docker repositories metadata: {exc}")
                        if repositories != {}:
                            _die("Docker archive repositories metadata can move mutable tags")
                        continue
                    if name in allowed_layer_metadata:
                        content = _tar_member_bytes(
                            bundle, member, 4 * 1024**2, "Docker layer metadata"
                        )
                        if name.endswith("/VERSION"):
                            if content.strip() != b"1.0":
                                _die("Docker layer VERSION metadata differs")
                        else:
                            try:
                                metadata = json.loads(content)
                            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                                _die(f"invalid Docker layer JSON metadata: {exc}")
                            if not isinstance(metadata, dict):
                                _die("Docker layer JSON metadata is not an object")
                        continue
                    unexpected_payloads.add(name)
                if unexpected_payloads:
                    _die("Docker archive contains unreferenced payload files")
    except (OSError, EOFError, gzip.BadGzipFile, tarfile.TarError) as exc:
        _die(f"invalid Docker image archive: {exc}")
    stats_output = getattr(args, "uncompressed_bytes_output", None)
    if stats_output:
        _atomic_lines(Path(stats_output), (str(expanded_total),))
    inode_output = getattr(args, "uncompressed_inodes_output", None)
    if inode_output:
        _atomic_lines(Path(inode_output), (str(expanded_inodes),))


def _read_passphrase(path: Path) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        _die(f"authentication key file is unsafe: {exc}")
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_gid != os.getegid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or not 1 <= info.st_size <= 4096
        ):
            _die("authentication key file owner/mode/size contract failed")
        raw = bytearray()
        while len(raw) <= 4096:
            chunk = os.read(fd, min(4097 - len(raw), 4097))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > 4096 or os.read(fd, 1):
            _die("authentication key file must contain one bounded line")
    finally:
        os.close(fd)
    if raw.count(b"\n") > 1 or (b"\n" in raw and not raw.endswith(b"\n")):
        _die("authentication key file must contain exactly one logical line")
    secret = bytes(raw).rstrip(b"\r\n")
    if not secret or b"\x00" in secret:
        _die("authentication key is empty or invalid")
    return secret


def _commit_mac(key_file: Path, context: str, cipher: Path) -> str:
    secret = _read_passphrase(key_file)
    mac_key = hashlib.pbkdf2_hmac(
        "sha256", secret, b"freio-postiz-etm-marker-v1", 200_000, dklen=32
    )
    digest = hmac.new(mac_key, digestmod=hashlib.sha256)
    digest.update(context.encode("ascii"))
    digest.update(b"\x00")
    digest.update(cipher.name.encode("ascii"))
    digest.update(b"\x00")
    digest.update(str(cipher.stat().st_size).encode("ascii"))
    digest.update(b"\x00")
    with cipher.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_auth_record(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != {
        "schema",
        "context",
        "cipher_filename",
        "cipher_bytes",
        "cipher_sha256",
        "hmac_sha256",
    } or value.get("schema") != AUTH_SCHEMA:
        _die("invalid authenticated commit record")
    context = value.get("context")
    if not isinstance(context, str) or not re.fullmatch(
        r"postiz-recovery-set:[0-9]{8}T[0-9]{6}Z", context
    ):
        _die("invalid authenticated commit context")
    filename = _validate_safe_path(value.get("cipher_filename"), "commit cipher filename")
    if "/" in filename or not filename.endswith(".enc"):
        _die("invalid commit cipher filename")
    size = value.get("cipher_bytes")
    if not isinstance(size, int) or not 1 <= size <= 64 * 1024**2:
        _die("invalid commit cipher size")
    _validate_sha(value.get("cipher_sha256"), "commit cipher")
    _validate_sha(value.get("hmac_sha256"), "commit HMAC")
    return value


def command_write_auth_record(args: argparse.Namespace) -> None:
    cipher = Path(args.cipher)
    if cipher.is_symlink() or not cipher.is_file() or cipher.stat().st_size == 0:
        _die("commit ciphertext is missing or unsafe")
    value = {
        "schema": AUTH_SCHEMA,
        "context": args.context,
        "cipher_filename": cipher.name,
        "cipher_bytes": cipher.stat().st_size,
        "cipher_sha256": _sha256_file(cipher),
        "hmac_sha256": _commit_mac(Path(args.key_file), args.context, cipher),
    }
    _validate_auth_record(value)
    _atomic_json(Path(args.output), value)


def command_verify_auth_record(args: argparse.Namespace) -> None:
    value = _validate_auth_record(_load_json(Path(args.record)))
    cipher = Path(args.cipher)
    if args.expected_context and value["context"] != args.expected_context:
        _die("authenticated commit context is a replay")
    if (
        cipher.is_symlink()
        or not cipher.is_file()
        or cipher.name != value["cipher_filename"]
        or cipher.stat().st_size != value["cipher_bytes"]
        or _sha256_file(cipher) != value["cipher_sha256"]
    ):
        _die("authenticated commit ciphertext metadata differs")
    actual = _commit_mac(Path(args.key_file), value["context"], cipher)
    if not hmac.compare_digest(actual, value["hmac_sha256"]):
        _die("authenticated commit HMAC differs")


JOURNAL_SERVICES = {"postiz", "postiz-postgres", "postiz-temporal", "postiz-redis"}
RESTORE_JOURNAL_ROLES = {
    f"{kind}-{remote}"
    for remote in ("primary", "dr")
    for kind in (
        "logical",
        "physical-extract",
        "physical-verify",
        "physical",
        "redis-check",
        "redis-uid",
        "redis-gid",
        "redis",
        "offline",
    )
}


def _validate_quiesce_journal(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != {"schema", "created_at", "phase", "containers"} or value.get(
        "schema"
    ) != QUIESCE_JOURNAL_SCHEMA:
        _die("invalid quiesce journal")
    _validate_timestamp(value.get("created_at"))
    if value.get("phase") not in {"prepared", "stopping", "stopped", "captured", "restoring"}:
        _die("invalid quiesce journal phase")
    containers = value.get("containers")
    if not isinstance(containers, dict) or set(containers) != JOURNAL_SERVICES:
        _die("quiesce journal lacks exact container set")
    for service, record in containers.items():
        if not isinstance(record, dict) or set(record) != {
            "container_id",
            "image_id",
            "was_running",
        }:
            _die("invalid quiesce journal container")
        if not re.fullmatch(r"[0-9a-f]{64}", str(record.get("container_id"))):
            _die("invalid quiesce journal container ID")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(record.get("image_id"))):
            _die("invalid quiesce journal image ID")
        if record.get("was_running") is not True:
            _die(f"quiesce journal did not capture running pre-state: {service}")
    return value


def command_write_quiesce_journal(args: argparse.Namespace) -> None:
    records: dict[str, dict[str, Any]] = {}
    for raw in args.container:
        parts = raw.split("|")
        if len(parts) != 3:
            _die("invalid quiesce journal container argument")
        service, container_id, image_id = parts
        if service in records:
            _die("duplicate quiesce journal service")
        records[service] = {
            "container_id": container_id,
            "image_id": image_id,
            "was_running": True,
        }
    value = {
        "schema": QUIESCE_JOURNAL_SCHEMA,
        "created_at": _validate_timestamp(args.timestamp),
        "phase": args.phase,
        "containers": records,
    }
    _validate_quiesce_journal(value)
    _atomic_json(Path(args.output), value)


def command_update_quiesce_journal(args: argparse.Namespace) -> None:
    value = _validate_quiesce_journal(_load_json(Path(args.journal)))
    value["phase"] = args.phase
    _validate_quiesce_journal(value)
    _atomic_json(Path(args.journal), value)


def command_journal_get(args: argparse.Namespace) -> None:
    value = _validate_quiesce_journal(_load_json(Path(args.journal)))
    if args.key in {"phase", "created_at"}:
        print(value[args.key])
        return
    if not args.service:
        _die("journal service is required for this key")
    print(value["containers"][args.service][args.key])


def _validate_restore_journal(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != {
        "schema",
        "created_at",
        "run_id",
        "work_directory",
        "containers",
    } or value.get("schema") != RESTORE_JOURNAL_SCHEMA:
        _die("invalid restore journal")
    _validate_timestamp(value.get("created_at"))
    run_id = value.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9]{6}", run_id):
        _die("invalid restore journal run ID")
    if value.get("work_directory") != f"/var/lib/homelab-backup/postiz-restore.{run_id}":
        _die("restore journal work directory differs")
    containers = value.get("containers")
    if not isinstance(containers, dict) or set(containers) != RESTORE_JOURNAL_ROLES:
        _die("restore journal container inventory differs")
    for role, name in containers.items():
        if name != f"postiz-restore-{run_id}-{role}":
            _die("restore journal container name differs")
    return value


def command_write_restore_journal(args: argparse.Namespace) -> None:
    run_id = args.run_id
    value = {
        "schema": RESTORE_JOURNAL_SCHEMA,
        "created_at": _validate_timestamp(args.timestamp),
        "run_id": run_id,
        "work_directory": f"/var/lib/homelab-backup/postiz-restore.{run_id}",
        "containers": {
            role: f"postiz-restore-{run_id}-{role}" for role in sorted(RESTORE_JOURNAL_ROLES)
        },
    }
    _validate_restore_journal(value)
    _atomic_json(Path(args.output), value)


def command_restore_journal_get(args: argparse.Namespace) -> None:
    value = _validate_restore_journal(_load_json(Path(args.journal)))
    if args.key == "work_directory":
        print(value["work_directory"])
    elif args.key == "run_id":
        print(value["run_id"])
    elif args.key == "created_at":
        print(value["created_at"])
    else:
        if not args.role:
            _die("restore journal role is required")
        print(value["containers"][args.role])


def _validate_generic_restore_journal(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != {
        "schema",
        "created_at",
        "run_id",
        "work_directory",
        "container",
    } or value.get("schema") != GENERIC_RESTORE_JOURNAL_SCHEMA:
        _die("invalid generic restore journal")
    _validate_timestamp(value.get("created_at"))
    run_id = value.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9]{6}", run_id):
        _die("invalid generic restore journal run ID")
    if value.get("work_directory") != f"/var/lib/homelab-backup/restore-generic.{run_id}":
        _die("generic restore journal work directory differs")
    if value.get("container") != f"generic-restore-{run_id}-postgres":
        _die("generic restore journal container name differs")
    return value


def command_write_generic_restore_journal(args: argparse.Namespace) -> None:
    run_id = args.run_id
    value = {
        "schema": GENERIC_RESTORE_JOURNAL_SCHEMA,
        "created_at": _validate_timestamp(args.timestamp),
        "run_id": run_id,
        "work_directory": f"/var/lib/homelab-backup/restore-generic.{run_id}",
        "container": f"generic-restore-{run_id}-postgres",
    }
    _validate_generic_restore_journal(value)
    _atomic_json(Path(args.output), value)


def command_generic_restore_journal_get(args: argparse.Namespace) -> None:
    value = _validate_generic_restore_journal(_load_json(Path(args.journal)))
    print(value[args.key])


CAPTURE_COUNT_KEYS = {
    "cluster_roles",
    "cluster_role_memberships",
    "postiz_role_superuser",
    "postiz_role_login",
    "postiz_public_tables",
    "postiz_posts",
    "postiz_integrations",
    "temporal_tables",
    "temporal_executions",
    "temporal_current_executions",
    "temporal_tasks",
    "temporal_schema_versions",
    "visibility_tables",
    "visibility_executions",
    "visibility_schema_versions",
    "insights_tables",
    "insights_post_insights",
    "insights_farm_pr_reviews",
    "insights_farm_pr_triage",
}

CAPTURE_CATALOG_DATABASES = {"postiz", "temporal", "temporal_visibility", "insights"}
CAPTURE_MIGRATION_KEYS = {
    "temporal_schema_version",
    "temporal_visibility_schema_version",
}
CAPTURE_FINGERPRINT_KEYS = {
    "roles": ("roles",),
    "role_memberships": ("role_memberships",),
    **{f"catalog_{name}": ("catalogs", name) for name in CAPTURE_CATALOG_DATABASES},
    **{f"migration_{name}": ("migrations", name) for name in CAPTURE_MIGRATION_KEYS},
}
CAPTURE_CONTENT_SHA_KEYS = {"upload_manifest_sha256", "physical_cluster_sha256"}
CAPTURE_REDIS_STORAGE_KEYS = {
    "redis_root_uid": ("root", "uid"),
    "redis_root_gid": ("root", "gid"),
    "redis_root_mode": ("root", "mode"),
    "redis_rdb_uid": ("rdb", "uid"),
    "redis_rdb_gid": ("rdb", "gid"),
    "redis_rdb_mode": ("rdb", "mode"),
}

CATALOG_FINGERPRINT_SQL = r"""
WITH catalog(kind, object_name, owner_name, acl_text, extra) AS (
  SELECT 'schema', n.nspname, pg_get_userbyid(n.nspowner),
    COALESCE((SELECT string_agg(a::text, ',' ORDER BY a::text)
              FROM unnest(n.nspacl) AS acl_items(a)), ''), ''
  FROM pg_namespace AS n
  WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND n.nspname !~ '^pg_(toast|temp|toast_temp)'
  UNION ALL
  SELECT 'relation:' || c.relkind::text, format('%I.%I', n.nspname, c.relname),
    pg_get_userbyid(c.relowner),
    COALESCE((SELECT string_agg(a::text, ',' ORDER BY a::text)
              FROM unnest(c.relacl) AS acl_items(a)), ''),
    c.relpersistence::text || '|' || c.relreplident::text || '|' ||
      c.relrowsecurity::text || '|' || c.relforcerowsecurity::text
  FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace
  WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND n.nspname !~ '^pg_(toast|temp|toast_temp)'
  UNION ALL
  SELECT 'function:' || p.prokind::text,
    format('%I.%I(%s)', n.nspname, p.proname, pg_get_function_identity_arguments(p.oid)),
    pg_get_userbyid(p.proowner),
    COALESCE((SELECT string_agg(a::text, ',' ORDER BY a::text)
              FROM unnest(p.proacl) AS acl_items(a)), ''),
    CASE WHEN p.prokind IN ('f', 'p') THEN pg_get_functiondef(p.oid)
         ELSE json_build_array(
           p.prosrc, p.probin, p.prorettype::regtype::text,
           p.proargtypes::text, p.provolatile, p.proparallel
         )::text END
  FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace
  WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND n.nspname !~ '^pg_(toast|temp|toast_temp)'
  UNION ALL
  SELECT 'type:' || t.typtype::text, format('%I.%I', n.nspname, t.typname),
    pg_get_userbyid(t.typowner),
    COALESCE((SELECT string_agg(a::text, ',' ORDER BY a::text)
              FROM unnest(t.typacl) AS acl_items(a)), ''),
    t.typcategory::text
  FROM pg_type AS t JOIN pg_namespace AS n ON n.oid = t.typnamespace
  WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND n.nspname !~ '^pg_(toast|temp|toast_temp)'
  UNION ALL
  SELECT 'column', format('%I.%I.%I', n.nspname, c.relname, a.attname),
    pg_get_userbyid(c.relowner), '',
    json_build_array(
      format_type(a.atttypid, a.atttypmod), a.attnotnull, a.attidentity,
      a.attgenerated, pg_get_expr(ad.adbin, ad.adrelid), coll.collname
    )::text
  FROM pg_attribute AS a
  JOIN pg_class AS c ON c.oid = a.attrelid
  JOIN pg_namespace AS n ON n.oid = c.relnamespace
  LEFT JOIN pg_attrdef AS ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
  LEFT JOIN pg_collation AS coll ON coll.oid = a.attcollation AND a.attcollation <> 0
  WHERE a.attnum > 0 AND NOT a.attisdropped
    AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'c')
    AND n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND n.nspname !~ '^pg_(toast|temp|toast_temp)'
  UNION ALL
  SELECT 'constraint:' || con.contype::text,
    COALESCE(format('%I.%I', n.nspname, rel.relname),
             format('%I.%I', tn.nspname, typ.typname)) || ':' || con.conname,
    COALESCE(pg_get_userbyid(rel.relowner), pg_get_userbyid(typ.typowner)), '',
    pg_get_constraintdef(con.oid, true) || '|' || con.convalidated::text || '|' ||
      con.connoinherit::text
  FROM pg_constraint AS con
  LEFT JOIN pg_class AS rel ON rel.oid = con.conrelid
  LEFT JOIN pg_namespace AS n ON n.oid = rel.relnamespace
  LEFT JOIN pg_type AS typ ON typ.oid = con.contypid
  LEFT JOIN pg_namespace AS tn ON tn.oid = typ.typnamespace
  WHERE COALESCE(n.nspname, tn.nspname) NOT IN ('pg_catalog', 'information_schema')
    AND COALESCE(n.nspname, tn.nspname) !~ '^pg_(toast|temp|toast_temp)'
  UNION ALL
  SELECT 'index', format('%I.%I', n.nspname, idx.relname),
    pg_get_userbyid(idx.relowner), '', pg_get_indexdef(i.indexrelid)
  FROM pg_index AS i
  JOIN pg_class AS idx ON idx.oid = i.indexrelid
  JOIN pg_namespace AS n ON n.oid = idx.relnamespace
  WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND n.nspname !~ '^pg_(toast|temp|toast_temp)'
  UNION ALL
  SELECT 'view', format('%I.%I', n.nspname, c.relname),
    pg_get_userbyid(c.relowner), '', pg_get_viewdef(c.oid, true)
  FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace
  WHERE c.relkind IN ('v', 'm')
    AND n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND n.nspname !~ '^pg_(toast|temp|toast_temp)'
  UNION ALL
  SELECT 'trigger', format('%I.%I', n.nspname, t.tgname),
    pg_get_userbyid(c.relowner), '', pg_get_triggerdef(t.oid, true)
  FROM pg_trigger AS t
  JOIN pg_class AS c ON c.oid = t.tgrelid
  JOIN pg_namespace AS n ON n.oid = c.relnamespace
  WHERE NOT t.tgisinternal
    AND n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND n.nspname !~ '^pg_(toast|temp|toast_temp)'
  UNION ALL
  SELECT 'policy', format('%I.%I.%I', n.nspname, c.relname, p.polname),
    pg_get_userbyid(c.relowner), '',
    json_build_array(
      p.polcmd, p.polpermissive,
      (SELECT string_agg(r.rolname, ',' ORDER BY r.rolname)
       FROM unnest(p.polroles) AS policy_roles(role_oid)
       JOIN pg_roles AS r ON r.oid = policy_roles.role_oid),
      pg_get_expr(p.polqual, p.polrelid), pg_get_expr(p.polwithcheck, p.polrelid)
    )::text
  FROM pg_policy AS p
  JOIN pg_class AS c ON c.oid = p.polrelid
  JOIN pg_namespace AS n ON n.oid = c.relnamespace
  WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND n.nspname !~ '^pg_(toast|temp|toast_temp)'
  UNION ALL
  SELECT 'enum', format('%I.%I', n.nspname, t.typname),
    pg_get_userbyid(t.typowner), '',
    json_build_array(e.enumsortorder::text, e.enumlabel)::text
  FROM pg_enum AS e
  JOIN pg_type AS t ON t.oid = e.enumtypid
  JOIN pg_namespace AS n ON n.oid = t.typnamespace
  WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND n.nspname !~ '^pg_(toast|temp|toast_temp)'
  UNION ALL
  SELECT 'sequence', format('%I.%I', n.nspname, c.relname),
    pg_get_userbyid(c.relowner), '',
    json_build_array(
      format_type(s.seqtypid, NULL), s.seqstart, s.seqincrement,
      s.seqmax, s.seqmin, s.seqcache, s.seqcycle
    )::text
  FROM pg_sequence AS s
  JOIN pg_class AS c ON c.oid = s.seqrelid
  JOIN pg_namespace AS n ON n.oid = c.relnamespace
  WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND n.nspname !~ '^pg_(toast|temp|toast_temp)'
  UNION ALL
  SELECT 'extension', e.extname, pg_get_userbyid(e.extowner), '',
    e.extversion || '|' || n.nspname
  FROM pg_extension AS e JOIN pg_namespace AS n ON n.oid = e.extnamespace
  WHERE e.extname <> 'plpgsql'
  UNION ALL
  SELECT 'default_acl:' || d.defaclobjtype::text,
    COALESCE(n.nspname, '') || '|' || r.rolname, r.rolname,
    COALESCE((SELECT string_agg(a::text, ',' ORDER BY a::text)
              FROM unnest(d.defaclacl) AS acl_items(a)), ''), ''
  FROM pg_default_acl AS d
  JOIN pg_roles AS r ON r.oid = d.defaclrole
  LEFT JOIN pg_namespace AS n ON n.oid = d.defaclnamespace
)
SELECT json_build_array(kind, object_name, owner_name, acl_text, extra)::text
FROM catalog
ORDER BY kind, object_name, owner_name, acl_text, extra
""".strip()

ROLE_FINGERPRINT_SQL = r"""
SELECT json_build_array(
  rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin,
  rolreplication, rolbypassrls, rolconnlimit,
  CASE WHEN rolvaliduntil IS NULL THEN NULL ELSE extract(epoch FROM rolvaliduntil)::text END,
  rolconfig
)::text
FROM pg_roles
WHERE rolname <> 'freio_restore_bootstrap'
ORDER BY rolname
""".strip()

ROLE_MEMBERSHIP_FINGERPRINT_SQL = r"""
SELECT json_build_array(
  role_role.rolname, member_role.rolname, grantor_role.rolname,
  membership.admin_option, membership.inherit_option, membership.set_option
)::text
FROM pg_auth_members AS membership
JOIN pg_roles AS role_role ON role_role.oid = membership.roleid
JOIN pg_roles AS member_role ON member_role.oid = membership.member
JOIN pg_roles AS grantor_role ON grantor_role.oid = membership.grantor
WHERE role_role.rolname <> 'freio_restore_bootstrap'
  AND member_role.rolname <> 'freio_restore_bootstrap'
  AND grantor_role.rolname <> 'freio_restore_bootstrap'
ORDER BY role_role.rolname, member_role.rolname, grantor_role.rolname,
  membership.admin_option, membership.inherit_option, membership.set_option
""".strip()

MIGRATION_FINGERPRINT_SQL = {
    "temporal_schema_version": (
        "temporal",
        "SELECT row_to_json(t)::text FROM public.schema_version AS t "
        "ORDER BY row_to_json(t)::text",
    ),
    "temporal_visibility_schema_version": (
        "temporal_visibility",
        "SELECT row_to_json(t)::text FROM public.schema_version AS t "
        "ORDER BY row_to_json(t)::text",
    ),
}


def command_emit_fingerprint_sql(args: argparse.Namespace) -> None:
    if args.kind == "catalog":
        if args.name or args.database:
            _die("catalog fingerprint SQL takes no name/database")
        print(CATALOG_FINGERPRINT_SQL)
    elif args.kind == "roles":
        if args.name or args.database:
            _die("role fingerprint SQL takes no name/database")
        print(ROLE_FINGERPRINT_SQL)
    elif args.kind == "role_memberships":
        if args.name or args.database:
            _die("role-membership fingerprint SQL takes no name/database")
        print(ROLE_MEMBERSHIP_FINGERPRINT_SQL)
    elif args.kind == "migration":
        if not args.name or not args.database:
            _die("migration fingerprint SQL requires name/database")
        expected_database, query = MIGRATION_FINGERPRINT_SQL[args.name]
        if args.database != expected_database:
            _die("migration fingerprint database differs")
        print(query)
    else:
        _die("unsupported fingerprint SQL kind")


def _validate_capture_evidence(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != {
        "schema",
        "created_at",
        "started_epoch",
        "finished_epoch",
        "duration_seconds",
        "writers",
        "writer_fence",
        "database_inventory",
        "maintenance_database",
        "counts",
        "fingerprints",
        "expected_absences",
        "redis_storage",
        "redis_rdb_keys",
        "upload_manifest_sha256",
        "physical_cluster_sha256",
    } or value.get("schema") != CAPTURE_SCHEMA:
        _die("invalid quiesced-capture evidence")
    _validate_timestamp(value.get("created_at"))
    started = value.get("started_epoch")
    finished = value.get("finished_epoch")
    duration = value.get("duration_seconds")
    if (
        not isinstance(started, int)
        or not isinstance(finished, int)
        or not isinstance(duration, int)
        or finished < started
        or duration != finished - started
        or not 0 <= duration <= 300
    ):
        _die("invalid capture duration")
    writers = value.get("writers")
    if not isinstance(writers, dict) or set(writers) != JOURNAL_SERVICES:
        _die("capture evidence lacks exact writer set")
    for record in writers.values():
        if not isinstance(record, dict) or set(record) != {"container_id", "image_id", "restored"}:
            _die("invalid capture writer evidence")
        if not re.fullmatch(r"[0-9a-f]{64}", str(record.get("container_id"))):
            _die("invalid capture writer container ID")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(record.get("image_id"))):
            _die("invalid capture writer image ID")
        if record.get("restored") is not True:
            _die("capture writer was not restored")
    if value.get("writer_fence") != {
        "mechanism": "exact-writers-stopped+postgres-container-name-withheld",
        "postgres_name_restored": True,
        "zero_client_connections_before_capture": True,
        "internal_network_members": [
            "postiz",
            "postiz-postgres",
            "postiz-redis",
            "postiz-temporal",
        ],
    }:
        _die("capture writer-fence evidence differs")
    if value.get("database_inventory") != [
        "insights",
        "postgres",
        "postiz",
        "temporal",
        "temporal_visibility",
    ]:
        _die("database inventory differs from the exact production topology")
    if value.get("maintenance_database") != {"name": "postgres", "user_objects": 0}:
        _die("maintenance database contains user objects")
    counts = value.get("counts")
    if not isinstance(counts, dict) or set(counts) != CAPTURE_COUNT_KEYS:
        _die("capture evidence count set differs")
    if any(not isinstance(item, int) or item < 0 for item in counts.values()):
        _die("capture evidence has invalid counts")
    fingerprints = value.get("fingerprints")
    if not isinstance(fingerprints, dict) or set(fingerprints) != {
        "roles",
        "role_memberships",
        "catalogs",
        "migrations",
    }:
        _die("capture evidence fingerprint set differs")
    _validate_sha(fingerprints.get("roles"), "captured roles")
    _validate_sha(fingerprints.get("role_memberships"), "captured role memberships")
    catalogs = fingerprints.get("catalogs")
    migrations = fingerprints.get("migrations")
    if not isinstance(catalogs, dict) or set(catalogs) != CAPTURE_CATALOG_DATABASES:
        _die("capture evidence catalog fingerprint set differs")
    if not isinstance(migrations, dict) or set(migrations) != CAPTURE_MIGRATION_KEYS:
        _die("capture evidence migration fingerprint set differs")
    for name, digest in {**catalogs, **migrations}.items():
        _validate_sha(digest, f"captured fingerprint {name}")
    if value.get("expected_absences") != {"postiz_prisma_migrations": True}:
        _die("capture evidence expected-absence contract differs")
    redis_storage = value.get("redis_storage")
    if not isinstance(redis_storage, dict) or set(redis_storage) != {"root", "rdb"}:
        _die("capture evidence Redis storage metadata differs")
    for name, metadata in redis_storage.items():
        if not isinstance(metadata, dict) or set(metadata) != {"uid", "gid", "mode"}:
            _die("capture evidence Redis metadata fields differ")
        if (
            not isinstance(metadata["uid"], int)
            or metadata["uid"] < 0
            or not isinstance(metadata["gid"], int)
            or metadata["gid"] < 0
            or not isinstance(metadata["mode"], int)
            or metadata["mode"] < 0
            or metadata["mode"] > 0o777
            or metadata["mode"] & 0o022
        ):
            _die(f"captured Redis {name} metadata is unsafe")
    redis_rdb_keys = value.get("redis_rdb_keys")
    if not isinstance(redis_rdb_keys, int) or redis_rdb_keys < 0:
        _die("invalid captured Redis RDB key count")
    _validate_sha(value.get("upload_manifest_sha256"), "captured upload manifest")
    _validate_sha(value.get("physical_cluster_sha256"), "captured physical cluster")
    return value


def command_write_capture_evidence(args: argparse.Namespace) -> None:
    def redis_metadata(raw: str, label: str) -> dict[str, int]:
        parts = raw.split(":")
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit() \
                or not re.fullmatch(r"[0-7]{3,4}", parts[2]):
            _die(f"invalid captured Redis {label} metadata")
        return {"uid": int(parts[0]), "gid": int(parts[1]), "mode": int(parts[2], 8)}

    writers: dict[str, dict[str, Any]] = {}
    for raw in args.writer:
        parts = raw.split("|")
        if len(parts) != 3:
            _die("invalid capture writer argument")
        service, container_id, image_id = parts
        writers[service] = {"container_id": container_id, "image_id": image_id, "restored": True}
    counts: dict[str, int] = {}
    for raw in args.count:
        name, separator, number = raw.partition("=")
        if not separator or name in counts or not number.isdigit():
            _die("invalid capture count argument")
        counts[name] = int(number)
    catalogs: dict[str, str] = {}
    for raw in args.catalog_fingerprint:
        name, separator, digest = raw.partition("=")
        if not separator or name in catalogs or name not in CAPTURE_CATALOG_DATABASES:
            _die("invalid capture catalog fingerprint argument")
        catalogs[name] = _validate_sha(digest, f"captured catalog {name}")
    migrations: dict[str, str] = {}
    for raw in args.migration_fingerprint:
        name, separator, digest = raw.partition("=")
        if not separator or name in migrations or name not in CAPTURE_MIGRATION_KEYS:
            _die("invalid capture migration fingerprint argument")
        migrations[name] = _validate_sha(digest, f"captured migration {name}")
    value = {
        "schema": CAPTURE_SCHEMA,
        "created_at": _validate_timestamp(args.timestamp),
        "started_epoch": args.started_epoch,
        "finished_epoch": args.finished_epoch,
        "duration_seconds": args.finished_epoch - args.started_epoch,
        "writers": writers,
        "writer_fence": {
            "mechanism": "exact-writers-stopped+postgres-container-name-withheld",
            "postgres_name_restored": True,
            "zero_client_connections_before_capture": True,
            "internal_network_members": [
                "postiz",
                "postiz-postgres",
                "postiz-redis",
                "postiz-temporal",
            ],
        },
        "database_inventory": ["insights", "postgres", "postiz", "temporal", "temporal_visibility"],
        "maintenance_database": {"name": "postgres", "user_objects": args.postgres_user_objects},
        "counts": counts,
        "fingerprints": {
            "roles": _validate_sha(args.role_fingerprint, "captured roles"),
            "role_memberships": _validate_sha(
                args.role_membership_fingerprint, "captured role memberships"
            ),
            "catalogs": dict(sorted(catalogs.items())),
            "migrations": dict(sorted(migrations.items())),
        },
        "expected_absences": {"postiz_prisma_migrations": args.postiz_prisma_migrations_absent},
        "redis_storage": {
            "root": redis_metadata(args.redis_root_metadata, "root"),
            "rdb": redis_metadata(args.redis_rdb_metadata, "RDB"),
        },
        "redis_rdb_keys": args.redis_rdb_keys,
        "upload_manifest_sha256": _sha256_file(Path(args.upload_manifest)),
        "physical_cluster_sha256": _sha256_file(Path(args.physical_cluster)),
    }
    _validate_capture_evidence(value)
    _atomic_json(Path(args.output), value)


def command_capture_get(args: argparse.Namespace) -> None:
    value = _validate_capture_evidence(_load_json(Path(args.evidence)))
    if args.key in CAPTURE_COUNT_KEYS:
        print(value["counts"][args.key])
    elif args.key == "redis_rdb_keys":
        print(value["redis_rdb_keys"])
    elif args.key == "created_at":
        print(value["created_at"])
    elif args.key in CAPTURE_FINGERPRINT_KEYS:
        current: Any = value["fingerprints"]
        for component in CAPTURE_FINGERPRINT_KEYS[args.key]:
            current = current[component]
        print(current)
    elif args.key in CAPTURE_CONTENT_SHA_KEYS:
        print(value[args.key])
    elif args.key in CAPTURE_REDIS_STORAGE_KEYS:
        current: Any = value["redis_storage"]
        for component in CAPTURE_REDIS_STORAGE_KEYS[args.key]:
            current = current[component]
        print(f"{current:o}" if args.key.endswith("_mode") else current)
    else:
        _die("unsupported capture evidence key")


def command_capture_writer_get(args: argparse.Namespace) -> None:
    value = _validate_capture_evidence(_load_json(Path(args.evidence)))
    print(value["writers"][args.service][args.key])


def _parse_policy_time(value: Any, label: str) -> dt.datetime:
    _validate_timestamp(value)
    try:
        return dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        _die(f"invalid {label}")


def _validate_storage_policy_source(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != {"schema", "provider", "primary", "dr", "failure_domain"} or value.get(
        "schema"
    ) != STORAGE_POLICY_SOURCE_SCHEMA or value.get("provider") != "cloudflare-r2":
        _die("invalid storage-policy source")
    expected_remotes = {
        "primary": ("homelab-backups", "r2postiz:homelab-backups/postiz"),
        "dr": ("homelab-backups-dr", "r2drpostiz:homelab-backups-dr/postiz"),
    }
    account_ids: list[str] = []
    for label, (bucket, remote) in expected_remotes.items():
        record = value.get(label)
        if not isinstance(record, dict) or set(record) != {
            "account_id",
            "bucket",
            "jurisdiction",
            "bucket_token_resource",
            "remote",
            "policy_token_file",
            "runtime_credential",
        }:
            _die("invalid storage-policy source remote")
        account_id = record.get("account_id")
        if not isinstance(account_id, str) or not ACCOUNT_ID_RE.fullmatch(account_id):
            _die("invalid Cloudflare account ID")
        if record.get("bucket") != bucket or record.get("remote") != remote:
            _die("storage-policy source endpoint differs")
        expected_resource = f"com.cloudflare.edge.r2.bucket.{account_id}_default_{bucket}"
        if (
            record.get("jurisdiction") != "default"
            or record.get("bucket_token_resource") != expected_resource
        ):
            _die("storage-policy source is not bound to the default-jurisdiction bucket resource")
        token_file = record.get("policy_token_file")
        if not isinstance(token_file, str):
            _die("invalid storage-policy token path")
        token_path = PurePosixPath(token_file)
        if (
            not token_path.is_absolute()
            or token_path.parts[:4] != ("/", "srv", "homelab", "secrets")
            or ".." in token_path.parts
            or not re.fullmatch(r"[A-Za-z0-9._/-]+", token_file)
        ):
            _die("unsafe storage-policy token path")
        if record.get("runtime_credential") != {
            "bucket_only": True,
            "cross_bucket_denied": True,
            "object_read_write_includes_delete": True,
            "policy_admin_denied": True,
        }:
            _die("runtime Postiz credential scope disclosure is false")
        account_ids.append(account_id)
    failure_domain = value.get("failure_domain")
    if not isinstance(failure_domain, dict) or set(failure_domain) != {
        "provider",
        "independent_accounts",
        "accepted_correlated_admin_risk",
    } or failure_domain.get("provider") != "cloudflare-r2":
        _die("invalid storage failure-domain disclosure")
    independent = account_ids[0] != account_ids[1]
    if failure_domain.get("independent_accounts") is not independent:
        _die("storage failure-domain claim is false")
    accepted = failure_domain.get("accepted_correlated_admin_risk")
    if accepted is not (not independent):
        _die("storage correlated-risk disclosure is inconsistent")
    return value


def command_storage_source_get(args: argparse.Namespace) -> None:
    source = _validate_storage_policy_source(_load_json(Path(args.source)))
    print(source[args.remote][args.key])


def _read_private_text(path: Path, max_bytes: int) -> str:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        _die(f"private config file is unsafe: {exc}")
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_gid != os.getegid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or not 1 <= info.st_size <= max_bytes
        ):
            _die("private config owner/mode/size contract failed")
        raw = bytearray()
        while len(raw) <= max_bytes:
            chunk = os.read(fd, min(1024 * 1024, max_bytes + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > max_bytes or os.read(fd, 1):
            _die("private config exceeds byte ceiling")
    finally:
        os.close(fd)
    try:
        return bytes(raw).decode("utf-8")
    except UnicodeDecodeError as exc:
        _die(f"private config is not UTF-8: {exc}")


def command_verify_rclone_source(args: argparse.Namespace) -> None:
    source = _validate_storage_policy_source(_load_json(Path(args.source)))
    parser = configparser.RawConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string(_read_private_text(Path(args.rclone_config), 1024 * 1024))
    except configparser.Error:
        # configparser diagnostics may embed the rejected source line.  That
        # line can contain the secret_access_key, so never surface it.
        _die("invalid rclone config structure")
    access_keys: list[str] = []
    for label in ("primary", "dr"):
        source_record = source[label]
        remote_name = source_record["remote"].split(":", 1)[0]
        if not parser.has_section(remote_name):
            _die(f"rclone config lacks the {label} Postiz remote")
        section = parser[remote_name]
        expected_endpoint = (
            f"https://{source_record['account_id']}.r2.cloudflarestorage.com"
        )
        if section.get("type") != "s3" or section.get("provider") != "Cloudflare":
            _die(f"rclone {label} remote is not Cloudflare S3")
        if section.get("endpoint", "").rstrip("/") != expected_endpoint:
            _die(f"rclone {label} endpoint is not bound to the attested account")
        if section.get("env_auth", "false").strip().lower() not in {"", "false", "0", "no"}:
            _die(f"rclone {label} remote permits ambient credential override")
        access_key = section.get("access_key_id", "")
        secret_key = section.get("secret_access_key", "")
        if not re.fullmatch(r"[A-Za-z0-9._~-]{8,256}", access_key) or not secret_key.strip():
            _die(f"rclone {label} remote lacks bounded static credentials")
        access_keys.append(access_key)
    if access_keys[0] == access_keys[1]:
        _die("primary and DR runtime credentials are not distinct")


def _cloudflare_rules(path: Path, label: str) -> list[dict[str, Any]]:
    envelope = _load_json(path)
    if envelope.get("success") is not True or envelope.get("errors") != []:
        _die(f"Cloudflare {label} API did not return success")
    if not isinstance(envelope.get("messages"), list):
        _die(f"invalid Cloudflare {label} API messages")
    result = envelope.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("rules"), list):
        _die(f"invalid Cloudflare {label} API rules")
    rules = result["rules"]
    if not all(isinstance(rule, dict) for rule in rules):
        _die(f"invalid Cloudflare {label} rule")
    return rules


def _prefixes_overlap(left: str, right: str) -> bool:
    return left.startswith(right) or right.startswith(left)


def _semantic_sha(rules: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        {"rules": sorted(rules, key=lambda rule: rule["id"])},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _verify_lock_response(path: Path, minimum_age: int) -> tuple[dict[str, Any], str]:
    required: dict[str, tuple[str, int | None]] = {
        "postiz/recovery-sets/": ("Age", minimum_age),
        "postiz/uploads/manifests/": ("Age", minimum_age),
        "postiz/uploads/blobs/sha256/": ("Indefinite", None),
        "postiz/images/sha256/": ("Indefinite", None),
    }
    found: dict[str, int | str] = {}
    projection: list[dict[str, Any]] = []
    ids: set[str] = set()
    for rule in _cloudflare_rules(path, "Bucket Lock"):
        rule_id = rule.get("id")
        prefix = rule.get("prefix", "")
        enabled = rule.get("enabled")
        condition = rule.get("condition")
        if (
            not isinstance(rule_id, str)
            or not rule_id
            or len(rule_id) > 256
            or rule_id in ids
            or not isinstance(prefix, str)
            or not isinstance(enabled, bool)
            or not isinstance(condition, dict)
        ):
            _die("invalid or duplicate Bucket Lock rule")
        ids.add(rule_id)
        kind = condition.get("type")
        if kind == "Age":
            if set(condition) != {"type", "maxAgeSeconds"}:
                _die("invalid Bucket Lock age condition")
            age = condition.get("maxAgeSeconds")
            if not isinstance(age, int) or isinstance(age, bool) or age <= 0:
                _die("invalid Bucket Lock age")
            normalized_condition: dict[str, Any] = {"type": kind, "maxAgeSeconds": age}
        elif kind == "Indefinite":
            if set(condition) != {"type"}:
                _die("invalid indefinite Bucket Lock condition")
            age = None
            normalized_condition = {"type": kind}
        elif kind == "Date":
            date = condition.get("date")
            if set(condition) != {"type", "date"} or not isinstance(date, str):
                _die("invalid dated Bucket Lock condition")
            age = None
            normalized_condition = {"type": kind, "date": date}
        else:
            _die("unknown Bucket Lock condition")
        projection.append(
            {"id": rule_id, "prefix": prefix, "enabled": enabled, "condition": normalized_condition}
        )
        overlaps = [target for target in required if _prefixes_overlap(prefix, target)]
        if not overlaps:
            continue
        if prefix not in required or len(overlaps) != 1 or not enabled or prefix in found:
            _die("broad, duplicate, disabled, or extra Postiz Bucket Lock rule")
        expected_kind, expected_age = required[prefix]
        if kind != expected_kind or (expected_age is not None and age != expected_age):
            _die("Postiz Bucket Lock rule differs from the exact retention contract")
        found[prefix] = age if age is not None else "indefinite"
    if set(found) != set(required):
        _die("required Postiz Bucket Lock rule is missing")
    summary = {
        "recovery_sets_max_age_seconds": found["postiz/recovery-sets/"],
        "upload_manifests_max_age_seconds": found["postiz/uploads/manifests/"],
        "upload_blobs": found["postiz/uploads/blobs/sha256/"],
        "docker_images": found["postiz/images/sha256/"],
    }
    return summary, _semantic_sha(projection)


def _normalize_transition(value: Any, label: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"condition"}:
        _die(f"invalid lifecycle {label} transition")
    condition = value.get("condition")
    if not isinstance(condition, dict):
        _die(f"invalid lifecycle {label} condition")
    kind = condition.get("type")
    if kind == "Age":
        age = condition.get("maxAge")
        if set(condition) != {"type", "maxAge"} or not isinstance(age, int) or isinstance(age, bool) \
                or age <= 0:
            _die(f"invalid lifecycle {label} age")
        return {"condition": {"type": kind, "maxAge": age}}
    if kind == "Date":
        date = condition.get("date")
        if set(condition) != {"type", "date"} or not isinstance(date, str):
            _die(f"invalid lifecycle {label} date")
        return {"condition": {"type": kind, "date": date}}
    _die(f"unknown lifecycle {label} condition")


def _verify_lifecycle_response(path: Path, delete_age: int) -> tuple[dict[str, Any], str]:
    required_delete = {
        "postiz/recovery-sets/": delete_age,
        "postiz/uploads/manifests/": delete_age,
    }
    protected = {
        *required_delete,
        "postiz/uploads/blobs/sha256/",
        "postiz/images/sha256/",
    }
    found: dict[str, int] = {}
    default_abort = 0
    projection: list[dict[str, Any]] = []
    ids: set[str] = set()
    for rule in _cloudflare_rules(path, "lifecycle"):
        rule_id = rule.get("id")
        conditions = rule.get("conditions")
        enabled = rule.get("enabled")
        if (
            not isinstance(rule_id, str)
            or not rule_id
            or len(rule_id) > 256
            or rule_id in ids
            or not isinstance(conditions, dict)
            or not isinstance(enabled, bool)
        ):
            _die("invalid or duplicate lifecycle rule")
        ids.add(rule_id)
        implicit_default_prefix = conditions == {}
        if implicit_default_prefix:
            # Cloudflare serializes the provider-owned default multipart-abort
            # rule with an empty conditions object.  Only that exact rule may
            # use the implicit empty prefix; every other rule remains bound to
            # an explicit string prefix below.
            prefix = ""
        elif set(conditions) == {"prefix"} and isinstance(conditions.get("prefix"), str):
            prefix = conditions["prefix"]
        else:
            _die("invalid lifecycle rule conditions")
        abort = _normalize_transition(rule.get("abortMultipartUploadsTransition"), "abort")
        delete = _normalize_transition(rule.get("deleteObjectsTransition"), "delete")
        storage = rule.get("storageClassTransitions", [])
        if storage is None or storage == []:
            normalized_storage: list[dict[str, Any]] = []
        elif not isinstance(storage, list):
            _die("invalid lifecycle storage-class transitions")
        else:
            _die("non-empty lifecycle storage-class transitions are forbidden")
        projection.append(
            {
                "id": rule_id,
                "prefix": prefix,
                "enabled": enabled,
                "abort": abort,
                "delete": delete,
                "storage": normalized_storage,
            }
        )
        overlaps = [target for target in protected if _prefixes_overlap(prefix, target)]
        is_default_abort = (
            prefix == ""
            and enabled
            and abort == {"condition": {"type": "Age", "maxAge": 7 * 86400}}
            and delete is None
            and not storage
        )
        if implicit_default_prefix and not is_default_abort:
            _die("empty lifecycle conditions are valid only for the default abort rule")
        if is_default_abort:
            default_abort += 1
            continue
        if not overlaps:
            continue
        if (
            prefix not in required_delete
            or len(overlaps) != 1
            or not enabled
            or abort is not None
            or storage
            or prefix in found
            or delete != {"condition": {"type": "Age", "maxAge": required_delete[prefix]}}
        ):
            _die("broad, duplicate, disabled, or extra Postiz lifecycle rule")
        found[prefix] = required_delete[prefix]
    if default_abort != 1:
        _die("the default seven-day multipart-abort lifecycle rule must be preserved")
    if set(found) != set(required_delete):
        _die("required Postiz lifecycle delete rule is missing")
    summary = {
        "recovery_sets_delete_after_seconds": found["postiz/recovery-sets/"],
        "upload_manifests_delete_after_seconds": found["postiz/uploads/manifests/"],
        "upload_blobs_delete_after_seconds": None,
        "docker_images_delete_after_seconds": None,
        "multipart_abort_after_seconds": 7 * 86400,
    }
    return summary, _semantic_sha(projection)


def command_attest_storage_policy(args: argparse.Namespace) -> None:
    source_path = Path(args.source)
    source = _validate_storage_policy_source(_load_json(source_path))
    verified_text = _validate_timestamp(args.timestamp)
    verified = _parse_policy_time(verified_text, "storage-policy verification time")
    expires = verified + dt.timedelta(minutes=15)
    response_paths = {
        "primary": (Path(args.primary_lock), Path(args.primary_lifecycle), 30, 31),
        "dr": (Path(args.dr_lock), Path(args.dr_lifecycle), 90, 91),
    }
    output: dict[str, Any] = {
        "schema": STORAGE_POLICY_SCHEMA,
        "source_sha256": _sha256_file(source_path),
        "verified_at": verified_text,
        "expires_at": expires.strftime("%Y%m%dT%H%M%SZ"),
        "failure_domain": source["failure_domain"],
    }
    for label, (lock_path, lifecycle_path, lock_days, lifecycle_days) in response_paths.items():
        locks, lock_sha = _verify_lock_response(lock_path, lock_days * 86400)
        lifecycle, lifecycle_sha = _verify_lifecycle_response(
            lifecycle_path, lifecycle_days * 86400
        )
        source_record = source[label]
        output[label] = {
            "remote": source_record["remote"],
            "account_id_sha256": hashlib.sha256(source_record["account_id"].encode()).hexdigest(),
            "credential": source_record["runtime_credential"],
            "locks": locks,
            "lifecycle": lifecycle,
            "admin_evidence": {
                "lock_semantic_sha256": lock_sha,
                "lifecycle_semantic_sha256": lifecycle_sha,
            },
        }
    _atomic_json(Path(args.output), output)


def command_verify_storage_policy(args: argparse.Namespace) -> None:
    value = _load_json(Path(args.policy))
    if set(value) != {
        "schema",
        "source_sha256",
        "verified_at",
        "expires_at",
        "primary",
        "dr",
        "failure_domain",
    } \
            or value.get("schema") != STORAGE_POLICY_SCHEMA:
        _die("invalid storage-policy attestation")
    _validate_sha(value.get("source_sha256"), "storage-policy source")
    verified = _parse_policy_time(value.get("verified_at"), "storage-policy verification time")
    expires = _parse_policy_time(value.get("expires_at"), "storage-policy expiry")
    now = dt.datetime.now(dt.timezone.utc)
    if verified > now + dt.timedelta(minutes=5) or expires - verified != dt.timedelta(minutes=15):
        _die("storage-policy attestation is stale or has an invalid lifetime")
    if not args.historical and expires <= now:
        _die("storage-policy attestation is stale or has an invalid lifetime")
    expected = {
        "primary": ("r2postiz:homelab-backups/postiz", 30 * 86400, 31 * 86400),
        "dr": ("r2drpostiz:homelab-backups-dr/postiz", 90 * 86400, 91 * 86400),
    }
    account_hashes: list[str] = []
    for label, (remote, exact_lock, exact_lifecycle) in expected.items():
        record = value.get(label)
        if not isinstance(record, dict) or set(record) != {
            "remote",
            "account_id_sha256",
            "credential",
            "locks",
            "lifecycle",
            "admin_evidence",
        }:
            _die("invalid storage-policy remote record")
        if record.get("remote") != remote:
            _die("storage-policy remote differs")
        account_hashes.append(_validate_sha(record.get("account_id_sha256"), "R2 account ID"))
        evidence = record.get("admin_evidence")
        if not isinstance(evidence, dict) or set(evidence) != {
            "lock_semantic_sha256",
            "lifecycle_semantic_sha256",
        }:
            _die("invalid storage-policy admin evidence")
        _validate_sha(evidence.get("lock_semantic_sha256"), "Bucket Lock semantic evidence")
        _validate_sha(evidence.get("lifecycle_semantic_sha256"), "lifecycle semantic evidence")
        if record.get("credential") != {
            "bucket_only": True,
            "cross_bucket_denied": True,
            "object_read_write_includes_delete": True,
            "policy_admin_denied": True,
        }:
            _die("runtime Postiz credential scope disclosure is false")
        locks = record.get("locks")
        if not isinstance(locks, dict) or set(locks) != {
            "recovery_sets_max_age_seconds",
            "upload_manifests_max_age_seconds",
            "upload_blobs",
            "docker_images",
        }:
            _die("invalid storage-policy lock record")
        if (
            not isinstance(locks["recovery_sets_max_age_seconds"], int)
            or locks["recovery_sets_max_age_seconds"] != exact_lock
            or not isinstance(locks["upload_manifests_max_age_seconds"], int)
            or locks["upload_manifests_max_age_seconds"] != exact_lock
            or locks["upload_blobs"] != "indefinite"
            or locks["docker_images"] != "indefinite"
        ):
            _die("server-side bucket locks do not meet the recovery contract")
        lifecycle = record.get("lifecycle")
        if lifecycle != {
            "recovery_sets_delete_after_seconds": exact_lifecycle,
            "upload_manifests_delete_after_seconds": exact_lifecycle,
            "upload_blobs_delete_after_seconds": None,
            "docker_images_delete_after_seconds": None,
            "multipart_abort_after_seconds": 7 * 86400,
        }:
            _die("server-side lifecycle does not follow the lock window")
    failure_domain = value.get("failure_domain")
    if not isinstance(failure_domain, dict) or set(failure_domain) != {
        "provider",
        "independent_accounts",
        "accepted_correlated_admin_risk",
    } or failure_domain.get("provider") != "cloudflare-r2":
        _die("invalid storage failure-domain disclosure")
    independent = account_hashes[0] != account_hashes[1]
    if failure_domain.get("independent_accounts") is not independent:
        _die("storage failure-domain claim is false")
    if independent:
        if failure_domain.get("accepted_correlated_admin_risk") is not False:
            _die("independent account attestation is inconsistent")
    elif failure_domain.get("accepted_correlated_admin_risk") is not True:
        _die("same-account R2 residual risk is not explicitly accepted")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan")
    scan.add_argument("--root", required=True)
    scan.add_argument("--timestamp", required=True)
    scan.add_argument("--output", required=True)
    scan.add_argument("--expected-uid", type=int, default=0)
    scan.add_argument("--expected-gid", type=int, default=0)
    scan.add_argument("--max-files", type=int, default=100_000)
    scan.add_argument("--max-bytes", type=int, default=16 * 1024**3)
    scan.set_defaults(func=command_scan)

    verify_source = subparsers.add_parser("verify-source")
    verify_source.add_argument("--root", required=True)
    verify_source.add_argument("--manifest", required=True)
    verify_source.add_argument("--expected-uid", type=int, default=0)
    verify_source.add_argument("--expected-gid", type=int, default=0)
    verify_source.add_argument("--max-files", type=int, default=100_000)
    verify_source.add_argument("--max-bytes", type=int, default=16 * 1024**3)
    verify_source.set_defaults(func=command_verify_source)

    entries = subparsers.add_parser("entries")
    entries.add_argument("--manifest", required=True)
    entries.set_defaults(func=command_entries)

    summary = subparsers.add_parser("summary")
    summary.add_argument("--manifest", required=True)
    summary.set_defaults(func=command_summary)

    blobs = subparsers.add_parser("emit-blob-list")
    blobs.add_argument("--manifest", required=True)
    blobs.add_argument("--output", required=True)
    blobs.set_defaults(func=command_emit_blob_list)

    blob_sizes = subparsers.add_parser("emit-blob-sizes")
    blob_sizes.add_argument("--manifest", required=True)
    blob_sizes.add_argument("--output", required=True)
    blob_sizes.set_defaults(func=command_emit_blob_sizes)

    cipher = subparsers.add_parser("verify-cipher-tree")
    cipher.add_argument("--manifest", required=True)
    cipher.add_argument("--root", required=True)
    cipher.set_defaults(func=command_verify_cipher_tree)

    checksums = subparsers.add_parser("emit-checksums")
    checksums.add_argument("--manifest", required=True)
    checksums.add_argument("--output", required=True)
    checksums.set_defaults(func=command_emit_checksums)

    restored = subparsers.add_parser("verify-restored")
    restored.add_argument("--manifest", required=True)
    restored.add_argument("--root", required=True)
    restored.add_argument("--expected-uid", type=int, default=0)
    restored.add_argument("--expected-gid", type=int, default=0)
    restored.set_defaults(func=command_verify_restored)

    seal_tree = subparsers.add_parser("seal-tree-archive")
    seal_tree.add_argument("--root", required=True)
    seal_tree.add_argument("--prefix", required=True)
    seal_tree.add_argument("--output", required=True)
    seal_tree.add_argument("--expected-uid", type=int, default=0)
    seal_tree.add_argument("--expected-gid", type=int, default=0)
    seal_tree.add_argument("--max-bytes", type=int, default=16 * 1024 * 1024)
    seal_tree.add_argument("--max-members", type=int, default=10_000)
    seal_tree.set_defaults(func=command_seal_tree_archive)

    verify_tree = subparsers.add_parser("verify-tree-archive")
    verify_tree.add_argument("--archive", required=True)
    verify_tree.add_argument("--prefix", required=True)
    verify_tree.add_argument("--max-bytes", type=int, default=16 * 1024 * 1024)
    verify_tree.add_argument("--max-members", type=int, default=10_000)
    verify_tree.set_defaults(func=command_verify_tree_archive)

    verify_tree_restored = subparsers.add_parser("verify-tree-restored")
    verify_tree_restored.add_argument("--archive", required=True)
    verify_tree_restored.add_argument("--prefix", required=True)
    verify_tree_restored.add_argument("--root", required=True)
    verify_tree_restored.add_argument("--expected-uid", type=int, default=0)
    verify_tree_restored.add_argument("--expected-gid", type=int, default=0)
    verify_tree_restored.add_argument("--max-bytes", type=int, default=16 * 1024 * 1024)
    verify_tree_restored.add_argument("--max-members", type=int, default=10_000)
    verify_tree_restored.set_defaults(func=command_verify_tree_restored)

    receipt = subparsers.add_parser("write-artifact-receipt")
    receipt.add_argument("--timestamp", required=True)
    receipt.add_argument("--upload-manifest", required=True)
    receipt.add_argument("--upload-manifest-key", required=True)
    receipt.add_argument("--upload-manifest-cipher-sha256", required=True)
    receipt.add_argument("--image-record-dir", required=True)
    receipt.add_argument("--compose-sha256", required=True)
    receipt.add_argument("--dockerfile-sha256", required=True)
    receipt.add_argument("--output", required=True)
    receipt.set_defaults(func=command_write_artifact_receipt)

    image_record = subparsers.add_parser("write-image-record")
    image_record.add_argument("--service", choices=sorted(REQUIRED_IMAGE_SERVICES), required=True)
    image_record.add_argument("--configured-ref", required=True)
    image_record.add_argument("--image-id", required=True)
    image_record.add_argument("--archive-key", required=True)
    image_record.add_argument("--archive-cipher-sha256", required=True)
    image_record.add_argument("--archive-cipher-bytes", type=int, required=True)
    image_record.add_argument("--archive-uncompressed-bytes", type=int, required=True)
    image_record.add_argument("--archive-uncompressed-inodes", type=int, required=True)
    image_record.add_argument("--output", required=True)
    image_record.set_defaults(func=command_write_image_record)

    artifact_get = subparsers.add_parser("artifact-get")
    artifact_get.add_argument("--receipt", required=True)
    artifact_get.add_argument("--key", choices=sorted(ARTIFACT_KEYS), required=True)
    artifact_get.set_defaults(func=command_artifact_get)

    image_get = subparsers.add_parser("image-get")
    image_get.add_argument("--receipt", required=True)
    image_get.add_argument("--service", choices=sorted(REQUIRED_IMAGE_SERVICES), required=True)
    image_get.add_argument("--key", choices=sorted(IMAGE_KEYS), required=True)
    image_get.set_defaults(func=command_image_get)

    operator_state = subparsers.add_parser("write-operator-state")
    operator_state.add_argument("--timestamp", required=True)
    operator_state.add_argument("--seasonal-releases-status", choices=("absent", "present"), required=True)
    operator_state.add_argument("--seasonal-releases-archive")
    operator_state.add_argument(
        "--seasonal-anchor-replacement-status", choices=("absent", "present"), required=True
    )
    operator_state.add_argument("--seasonal-anchor-replacement-archive")
    operator_state.add_argument("--policy-status", choices=("absent", "present"), required=True)
    operator_state.add_argument("--policy-archive")
    operator_state.add_argument("--output", required=True)
    operator_state.set_defaults(func=command_write_operator_state)

    operator_get = subparsers.add_parser("operator-state-get")
    operator_get.add_argument("--receipt", required=True)
    operator_get.add_argument(
        "--name", choices=sorted({*OPERATOR_STATE_PATHS, "policy"}), required=True
    )
    operator_get.add_argument(
        "--key", choices=("status", "archive_filename", "archive_cipher_sha256"), required=True
    )
    operator_get.set_defaults(func=command_operator_state_get)

    recovery = subparsers.add_parser("write-recovery-set")
    recovery.add_argument("--timestamp", required=True)
    recovery.add_argument("--physical-cluster", required=True)
    recovery.add_argument("--capture-evidence", required=True)
    recovery.add_argument("--globals", required=True)
    recovery.add_argument("--database-postiz", required=True)
    recovery.add_argument("--database-temporal", required=True)
    recovery.add_argument("--database-temporal-visibility", required=True)
    recovery.add_argument("--database-insights", required=True)
    recovery.add_argument("--runtime-config", required=True)
    recovery.add_argument("--config-volume", required=True)
    recovery.add_argument("--redis", required=True)
    recovery.add_argument("--artifacts", required=True)
    recovery.add_argument("--operator-state", required=True)
    recovery.add_argument("--storage-policy", required=True)
    recovery.add_argument("--output", required=True)
    recovery.set_defaults(func=command_write_recovery_set)

    recovery_get = subparsers.add_parser("recovery-get")
    recovery_get.add_argument("--recovery-set", required=True)
    recovery_get.add_argument("--key", choices=sorted(RECOVERY_KEYS), required=True)
    recovery_get.set_defaults(func=command_recovery_get)

    config = subparsers.add_parser("verify-config-archive")
    config.add_argument("--archive", required=True)
    config.add_argument("--compose-sha256")
    config.add_argument("--dockerfile-sha256")
    config.set_defaults(func=command_verify_config_archive)

    config_source = subparsers.add_parser("verify-config-source")
    config_source.add_argument("--archive", required=True)
    config_source.set_defaults(func=command_verify_config_source)

    compose_no_deps = subparsers.add_parser("write-compose-no-deps-model")
    compose_no_deps.add_argument("--compose-json", required=True)
    compose_no_deps.add_argument("--output", required=True)
    compose_no_deps.set_defaults(func=command_write_compose_no_deps_model)

    compose_runtime = subparsers.add_parser("verify-compose-runtime")
    compose_runtime.add_argument("--compose-json", required=True)
    compose_runtime.add_argument("--compose-hashes", required=True)
    compose_runtime.add_argument("--resolved-compose-hashes", required=True)
    compose_runtime.add_argument("--postiz-no-deps-compose-json", required=True)
    compose_runtime.add_argument("--postiz-no-deps-hash", required=True)
    compose_runtime.add_argument("--container-json", required=True)
    compose_runtime.add_argument("--image-inspect-json", required=True)
    compose_runtime.add_argument("--network-inspect-json", required=True)
    compose_runtime.add_argument("--expected-image", action="append", default=[], required=True)
    compose_runtime.add_argument(
        "--runtime-state", choices=("preflight", "writer-fenced"), required=True
    )
    compose_runtime.set_defaults(func=command_verify_compose_runtime)

    config_get = subparsers.add_parser("config-archive-get")
    config_get.add_argument("--archive", required=True)
    config_get.add_argument(
        "--key", choices=("compose_sha256", "dockerfile_sha256", "source_revision"), required=True
    )
    config_get.set_defaults(func=command_config_archive_get)

    physical = subparsers.add_parser("verify-physical-archive")
    physical.add_argument("--archive", required=True)
    physical.add_argument("--max-bytes", type=int, default=24 * 1024**3)
    physical.add_argument("--max-members", type=int, default=MAX_PHYSICAL_ARCHIVE_MEMBERS)
    physical.set_defaults(func=command_verify_physical_archive)

    image = subparsers.add_parser("verify-image-archive")
    image.add_argument("--archive", required=True)
    image.add_argument("--image-id", required=True)
    image.add_argument("--uncompressed-bytes-output")
    image.add_argument("--uncompressed-inodes-output")
    image.set_defaults(func=command_verify_image_archive)

    seasonal_policy = subparsers.add_parser("verify-seasonal-policy")
    seasonal_policy.add_argument("--policy", required=True)
    seasonal_policy.add_argument("--seasonal-releases-root")
    seasonal_policy.add_argument("--seasonal-anchor-replacement-root")
    seasonal_policy.set_defaults(func=command_verify_seasonal_policy)

    write_auth = subparsers.add_parser("write-auth-record")
    write_auth.add_argument("--cipher", required=True)
    write_auth.add_argument("--key-file", required=True)
    write_auth.add_argument("--context", required=True)
    write_auth.add_argument("--output", required=True)
    write_auth.set_defaults(func=command_write_auth_record)

    verify_auth = subparsers.add_parser("verify-auth-record")
    verify_auth.add_argument("--cipher", required=True)
    verify_auth.add_argument("--record", required=True)
    verify_auth.add_argument("--key-file", required=True)
    verify_auth.add_argument("--expected-context")
    verify_auth.set_defaults(func=command_verify_auth_record)

    write_journal = subparsers.add_parser("write-quiesce-journal")
    write_journal.add_argument("--timestamp", required=True)
    write_journal.add_argument(
        "--phase",
        choices=("prepared", "stopping", "stopped", "captured", "restoring"),
        required=True,
    )
    write_journal.add_argument("--container", action="append", required=True)
    write_journal.add_argument("--output", required=True)
    write_journal.set_defaults(func=command_write_quiesce_journal)

    update_journal = subparsers.add_parser("update-quiesce-journal")
    update_journal.add_argument("--journal", required=True)
    update_journal.add_argument(
        "--phase",
        choices=("prepared", "stopping", "stopped", "captured", "restoring"),
        required=True,
    )
    update_journal.set_defaults(func=command_update_quiesce_journal)

    journal_get = subparsers.add_parser("journal-get")
    journal_get.add_argument("--journal", required=True)
    journal_get.add_argument("--service", choices=sorted(JOURNAL_SERVICES))
    journal_get.add_argument(
        "--key",
        choices=("phase", "created_at", "container_id", "image_id", "was_running"),
        required=True,
    )
    journal_get.set_defaults(func=command_journal_get)

    write_restore_journal = subparsers.add_parser("write-restore-journal")
    write_restore_journal.add_argument("--timestamp", required=True)
    write_restore_journal.add_argument("--run-id", required=True)
    write_restore_journal.add_argument("--output", required=True)
    write_restore_journal.set_defaults(func=command_write_restore_journal)

    restore_journal_get = subparsers.add_parser("restore-journal-get")
    restore_journal_get.add_argument("--journal", required=True)
    restore_journal_get.add_argument("--role", choices=sorted(RESTORE_JOURNAL_ROLES))
    restore_journal_get.add_argument(
        "--key", choices=("work_directory", "run_id", "created_at", "container"), required=True
    )
    restore_journal_get.set_defaults(func=command_restore_journal_get)

    write_generic_restore_journal = subparsers.add_parser("write-generic-restore-journal")
    write_generic_restore_journal.add_argument("--timestamp", required=True)
    write_generic_restore_journal.add_argument("--run-id", required=True)
    write_generic_restore_journal.add_argument("--output", required=True)
    write_generic_restore_journal.set_defaults(func=command_write_generic_restore_journal)

    generic_restore_journal_get = subparsers.add_parser("generic-restore-journal-get")
    generic_restore_journal_get.add_argument("--journal", required=True)
    generic_restore_journal_get.add_argument(
        "--key",
        choices=("created_at", "run_id", "work_directory", "container"),
        required=True,
    )
    generic_restore_journal_get.set_defaults(func=command_generic_restore_journal_get)

    capture = subparsers.add_parser("write-capture-evidence")
    capture.add_argument("--timestamp", required=True)
    capture.add_argument("--started-epoch", type=int, required=True)
    capture.add_argument("--finished-epoch", type=int, required=True)
    capture.add_argument("--writer", action="append", required=True)
    capture.add_argument("--count", action="append", required=True)
    capture.add_argument("--catalog-fingerprint", action="append", required=True)
    capture.add_argument("--migration-fingerprint", action="append", required=True)
    capture.add_argument("--role-fingerprint", required=True)
    capture.add_argument("--role-membership-fingerprint", required=True)
    capture.add_argument("--postiz-prisma-migrations-absent", action="store_true")
    capture.add_argument("--redis-root-metadata", required=True)
    capture.add_argument("--redis-rdb-metadata", required=True)
    capture.add_argument("--postgres-user-objects", type=int, required=True)
    capture.add_argument("--redis-rdb-keys", type=int, required=True)
    capture.add_argument("--upload-manifest", required=True)
    capture.add_argument("--physical-cluster", required=True)
    capture.add_argument("--output", required=True)
    capture.set_defaults(func=command_write_capture_evidence)

    capture_get = subparsers.add_parser("capture-get")
    capture_get.add_argument("--evidence", required=True)
    capture_get.add_argument(
        "--key",
        choices=sorted(
            {
                *CAPTURE_COUNT_KEYS,
                *CAPTURE_FINGERPRINT_KEYS,
                *CAPTURE_CONTENT_SHA_KEYS,
                *CAPTURE_REDIS_STORAGE_KEYS,
                "redis_rdb_keys",
                "created_at",
            }
        ),
        required=True,
    )
    capture_get.set_defaults(func=command_capture_get)

    capture_writer_get = subparsers.add_parser("capture-writer-get")
    capture_writer_get.add_argument("--evidence", required=True)
    capture_writer_get.add_argument("--service", choices=sorted(JOURNAL_SERVICES), required=True)
    capture_writer_get.add_argument("--key", choices=("container_id", "image_id"), required=True)
    capture_writer_get.set_defaults(func=command_capture_writer_get)

    fingerprint_sql = subparsers.add_parser("emit-fingerprint-sql")
    fingerprint_sql.add_argument(
        "--kind", choices=("catalog", "roles", "role_memberships", "migration"), required=True
    )
    fingerprint_sql.add_argument("--name", choices=sorted(CAPTURE_MIGRATION_KEYS))
    fingerprint_sql.add_argument("--database", choices=sorted(CAPTURE_CATALOG_DATABASES))
    fingerprint_sql.set_defaults(func=command_emit_fingerprint_sql)

    storage_policy = subparsers.add_parser("verify-storage-policy")
    storage_policy.add_argument("--policy", required=True)
    storage_policy.add_argument("--historical", action="store_true")
    storage_policy.set_defaults(func=command_verify_storage_policy)

    policy_source = subparsers.add_parser("storage-source-get")
    policy_source.add_argument("--source", required=True)
    policy_source.add_argument("--remote", choices=("primary", "dr"), required=True)
    policy_source.add_argument(
        "--key", choices=("account_id", "bucket", "policy_token_file"), required=True
    )
    policy_source.set_defaults(func=command_storage_source_get)

    rclone_source = subparsers.add_parser("verify-rclone-source")
    rclone_source.add_argument("--source", required=True)
    rclone_source.add_argument("--rclone-config", required=True)
    rclone_source.set_defaults(func=command_verify_rclone_source)

    attest_policy = subparsers.add_parser("attest-storage-policy")
    attest_policy.add_argument("--source", required=True)
    attest_policy.add_argument("--timestamp", required=True)
    attest_policy.add_argument("--primary-lock", required=True)
    attest_policy.add_argument("--primary-lifecycle", required=True)
    attest_policy.add_argument("--dr-lock", required=True)
    attest_policy.add_argument("--dr-lifecycle", required=True)
    attest_policy.add_argument("--output", required=True)
    attest_policy.set_defaults(func=command_attest_storage_policy)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except ContractError as exc:
        print(f"postiz backup contract failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
