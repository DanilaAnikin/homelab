#!/usr/bin/env python3
"""Fail-closed, PII-free health check for the Freio Telegram dispatcher."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


ENABLED_MARKER = Path("/etc/freio-telegram-handoff/enabled")
STATE_DIRECTORY = Path("/var/lib/freio-telegram-handoff")
PRIVATE_STATE_DIRECTORY = Path("/var/lib/private/freio-telegram-handoff")
HEARTBEAT_PATH = PRIVATE_STATE_DIRECTORY / "heartbeat-v1.json"
DISPATCH_TIMER_UNIT = "freio-telegram-handoff.timer"
SYSTEMCTL = "/usr/bin/systemctl"
HEARTBEAT_STATUSES = frozenset({"idle", "sent", "retry", "error"})
HEALTHY_STATUSES = frozenset({"idle", "sent"})
MAX_HEARTBEAT_BYTES = 512
MAX_HEARTBEAT_AGE_SECONDS = 10 * 60
MAX_FUTURE_SKEW_SECONDS = 60
SYSTEMCTL_TIMEOUT_SECONDS = 5
RFC3339_UTC_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:"
    r"[0-9]{2}:[0-9]{2}Z$"
)


class HealthFailure(RuntimeError):
    """Expected failure carrying only a bounded, non-sensitive event code."""

    def __init__(self, event: str) -> None:
        super().__init__(event)
        self.event = event


@dataclass(frozen=True)
class Heartbeat:
    status: str
    recorded_at: datetime


@dataclass(frozen=True)
class HealthResult:
    event: str
    healthy: bool
    status: str | None = None
    age_seconds: int | None = None


TimerProbe = Callable[[str], bool]


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise HealthFailure("heartbeat_invalid")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    del value
    raise HealthFailure("heartbeat_invalid")


def _decode_json(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HealthFailure("heartbeat_invalid") from None


def _validate_marker(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
) -> bool:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise HealthFailure("marker_invalid") from None
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_nlink != 1
        or stat.S_IMODE(details.st_mode) != 0
        or details.st_uid != expected_uid
        or details.st_gid != expected_gid
        or details.st_size != 0
    ):
        raise HealthFailure("marker_invalid")
    return True


def _read_heartbeat(path: Path, state_directory: Path) -> Heartbeat:
    try:
        directory_details = state_directory.lstat()
    except OSError:
        raise HealthFailure("heartbeat_unavailable") from None
    if (
        state_directory.is_symlink()
        or not stat.S_ISDIR(directory_details.st_mode)
        or stat.S_IMODE(directory_details.st_mode) != 0o700
    ):
        raise HealthFailure("heartbeat_invalid")
    if path.parent != state_directory or path.name != "heartbeat-v1.json":
        raise HealthFailure("heartbeat_invalid")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise HealthFailure("heartbeat_missing") from None
    except OSError:
        raise HealthFailure("heartbeat_unavailable") from None
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_size < 1
            or details.st_size > MAX_HEARTBEAT_BYTES
        ):
            raise HealthFailure("heartbeat_invalid")
        raw = os.read(descriptor, MAX_HEARTBEAT_BYTES + 1)
        if len(raw) > MAX_HEARTBEAT_BYTES or os.read(descriptor, 1):
            raise HealthFailure("heartbeat_invalid")
    finally:
        os.close(descriptor)

    value = _decode_json(raw)
    if not isinstance(value, dict) or set(value) != {
        "version",
        "status",
        "recordedAt",
    }:
        raise HealthFailure("heartbeat_invalid")
    if (
        not isinstance(value["version"], int)
        or isinstance(value["version"], bool)
        or value["version"] != 1
    ):
        raise HealthFailure("heartbeat_invalid")
    status_value = value["status"]
    timestamp_value = value["recordedAt"]
    if (
        not isinstance(status_value, str)
        or status_value not in HEARTBEAT_STATUSES
        or not isinstance(timestamp_value, str)
        or not RFC3339_UTC_PATTERN.fullmatch(timestamp_value)
    ):
        raise HealthFailure("heartbeat_invalid")
    try:
        recorded_at = datetime.strptime(
            timestamp_value,
            "%Y-%m-%dT%H:%M:%SZ",
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        raise HealthFailure("heartbeat_invalid") from None
    return Heartbeat(status=status_value, recorded_at=recorded_at)


def systemd_timer_active(unit: str) -> bool:
    if unit != DISPATCH_TIMER_UNIT:
        raise HealthFailure("timer_probe_invalid")
    try:
        result = subprocess.run(
            [SYSTEMCTL, "is-active", "--quiet", unit],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=SYSTEMCTL_TIMEOUT_SECONDS,
            env={"PATH": "/usr/sbin:/usr/bin"},
        )
    except (OSError, subprocess.SubprocessError):
        raise HealthFailure("timer_probe_failed") from None
    return result.returncode == 0


def check_health(
    *,
    marker: Path = ENABLED_MARKER,
    heartbeat_path: Path = HEARTBEAT_PATH,
    state_directory: Path = PRIVATE_STATE_DIRECTORY,
    now: datetime | None = None,
    timer_probe: TimerProbe = systemd_timer_active,
    expected_marker_uid: int = 0,
    expected_marker_gid: int = 0,
) -> HealthResult:
    if not _validate_marker(
        marker,
        expected_uid=expected_marker_uid,
        expected_gid=expected_marker_gid,
    ):
        return HealthResult(event="disabled", healthy=True)
    if not timer_probe(DISPATCH_TIMER_UNIT):
        return HealthResult(event="timer_inactive", healthy=False)

    heartbeat = _read_heartbeat(heartbeat_path, state_directory)
    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        raise HealthFailure("clock_invalid")
    age = (checked_at.astimezone(timezone.utc) - heartbeat.recorded_at).total_seconds()
    if age < -MAX_FUTURE_SKEW_SECONDS:
        return HealthResult(
            event="heartbeat_future",
            healthy=False,
            status=heartbeat.status,
            age_seconds=int(age),
        )
    bounded_age = max(0, int(age))
    if age > MAX_HEARTBEAT_AGE_SECONDS:
        return HealthResult(
            event="heartbeat_stale",
            healthy=False,
            status=heartbeat.status,
            age_seconds=bounded_age,
        )
    if heartbeat.status not in HEALTHY_STATUSES:
        return HealthResult(
            event=f"worker_{heartbeat.status}",
            healthy=False,
            status=heartbeat.status,
            age_seconds=bounded_age,
        )
    return HealthResult(
        event="healthy",
        healthy=True,
        status=heartbeat.status,
        age_seconds=bounded_age,
    )


def _log(result: HealthResult) -> None:
    payload: dict[str, str | int | bool] = {
        "event": result.event,
        "healthy": result.healthy,
    }
    if result.status is not None:
        payload["status"] = result.status
    if result.age_seconds is not None:
        payload["ageSeconds"] = result.age_seconds
    sys.stdout.write(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")
    sys.stdout.flush()


def main() -> int:
    try:
        result = check_health()
    except HealthFailure as error:
        result = HealthResult(event=error.event, healthy=False)
    except Exception:
        result = HealthResult(event="internal_failure", healthy=False)
    _log(result)
    return 0 if result.healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
