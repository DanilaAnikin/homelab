from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from unittest import mock

from scripts.freio_telegram_handoff.dispatcher import StateStore
from scripts.freio_telegram_handoff.health import (
    DISPATCH_TIMER_UNIT,
    HealthFailure,
    HealthResult,
    check_health,
    main,
    systemd_timer_active,
)


NOW = datetime(2026, 8, 11, 10, 0, 0, tzinfo=timezone.utc)


def active_dispatch_timer(unit: str) -> bool:
    return unit == DISPATCH_TIMER_UNIT


class HealthCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.marker = self.root / "enabled"
        self.marker.touch(mode=0o000)
        self.marker.chmod(0o000)
        self.state = StateStore(self.root / "state")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def check(
        self,
        *,
        timer_probe: Callable[[str], bool] = active_dispatch_timer,
    ) -> HealthResult:
        return check_health(
            marker=self.marker,
            heartbeat_path=self.state.heartbeat_path,
            state_directory=self.state.root,
            now=NOW,
            timer_probe=timer_probe,
            expected_marker_uid=os.getuid(),
            expected_marker_gid=os.getgid(),
        )

    def test_disabled_marker_does_not_probe_timer_or_state(self) -> None:
        self.marker.unlink()

        def forbidden_probe(unit: str) -> bool:
            del unit
            raise AssertionError("disabled health check must not probe systemd")

        result = self.check(timer_probe=forbidden_probe)

        self.assertEqual(result, HealthResult(event="disabled", healthy=True))

    def test_active_timer_and_fresh_idle_or_sent_heartbeat_are_healthy(self) -> None:
        for status in ("idle", "sent"):
            with self.subTest(status=status):
                self.state.save_heartbeat(status, NOW - timedelta(seconds=90))
                result = self.check()
                self.assertEqual(result.event, "healthy")
                self.assertTrue(result.healthy)
                self.assertEqual(result.status, status)
                self.assertEqual(result.age_seconds, 90)

    def test_inactive_dispatch_timer_fails_before_reading_heartbeat(self) -> None:
        result = self.check(timer_probe=lambda unit: False)

        self.assertEqual(
            result,
            HealthResult(event="timer_inactive", healthy=False),
        )

    def test_retry_and_error_heartbeat_fail_closed(self) -> None:
        for status in ("retry", "error"):
            with self.subTest(status=status):
                self.state.save_heartbeat(status, NOW - timedelta(seconds=30))
                result = self.check()
                self.assertEqual(result.event, f"worker_{status}")
                self.assertFalse(result.healthy)
                self.assertEqual(result.status, status)
                self.assertEqual(result.age_seconds, 30)

    def test_stale_and_future_heartbeat_fail_closed(self) -> None:
        self.state.save_heartbeat("idle", NOW - timedelta(minutes=11))
        stale = self.check()
        self.assertEqual(stale.event, "heartbeat_stale")
        self.assertFalse(stale.healthy)
        self.assertEqual(stale.age_seconds, 660)

        self.state.save_heartbeat("sent", NOW + timedelta(seconds=61))
        future = self.check()
        self.assertEqual(future.event, "heartbeat_future")
        self.assertFalse(future.healthy)
        self.assertEqual(future.age_seconds, -61)

    def test_missing_malformed_or_public_heartbeat_is_rejected(self) -> None:
        self.state.ensure()
        with self.assertRaisesRegex(HealthFailure, "heartbeat_missing"):
            self.check()

        self.state.heartbeat_path.write_text(
            '{"version":1,"status":"idle","recordedAt":"bad"}\n',
            encoding="utf-8",
        )
        self.state.heartbeat_path.chmod(0o600)
        with self.assertRaisesRegex(HealthFailure, "heartbeat_invalid"):
            self.check()

        self.state.save_heartbeat("idle", NOW)
        self.state.heartbeat_path.chmod(0o644)
        with self.assertRaisesRegex(HealthFailure, "heartbeat_invalid"):
            self.check()

    def test_duplicate_keys_extra_fields_and_symlinks_are_rejected(self) -> None:
        self.state.ensure()
        self.state.heartbeat_path.write_text(
            '{"version":1,"status":"idle","status":"sent",'
            '"recordedAt":"2026-08-11T10:00:00Z"}\n',
            encoding="utf-8",
        )
        self.state.heartbeat_path.chmod(0o600)
        with self.assertRaisesRegex(HealthFailure, "heartbeat_invalid"):
            self.check()

        self.state.heartbeat_path.write_text(
            '{"version":true,"status":"idle","recordedAt":"2026-08-11T10:00:00Z"}\n',
            encoding="utf-8",
        )
        self.state.heartbeat_path.chmod(0o600)
        with self.assertRaisesRegex(HealthFailure, "heartbeat_invalid"):
            self.check()

        self.state.heartbeat_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "status": "idle",
                    "recordedAt": "2026-08-11T10:00:00Z",
                    "conversationId": "00000000-0000-4000-8000-000000000001",
                }
            ),
            encoding="utf-8",
        )
        self.state.heartbeat_path.chmod(0o600)
        with self.assertRaisesRegex(HealthFailure, "heartbeat_invalid"):
            self.check()

        target = self.root / "target"
        target.write_text(
            '{"version":1,"status":"idle","recordedAt":"2026-08-11T10:00:00Z"}\n',
            encoding="utf-8",
        )
        target.chmod(0o600)
        self.state.heartbeat_path.unlink()
        self.state.heartbeat_path.symlink_to(target)
        with self.assertRaises(HealthFailure):
            self.check()

    def test_marker_must_be_empty_private_regular_file(self) -> None:
        self.marker.chmod(0o600)
        with self.assertRaisesRegex(HealthFailure, "marker_invalid"):
            self.check()

        self.marker.unlink()
        target = self.root / "marker-target"
        target.touch(mode=0o000)
        target.chmod(0o000)
        self.marker.symlink_to(target)
        with self.assertRaisesRegex(HealthFailure, "marker_invalid"):
            self.check()

        self.marker.unlink()
        self.marker.touch(mode=0o000)
        self.marker.chmod(0o000)
        os.link(self.marker, self.root / "marker-hardlink")
        with self.assertRaisesRegex(HealthFailure, "marker_invalid"):
            self.check()

    def test_systemd_probe_uses_exact_bounded_command(self) -> None:
        completed = mock.Mock(returncode=0)
        with mock.patch(
            "scripts.freio_telegram_handoff.health.subprocess.run",
            return_value=completed,
        ) as run:
            self.assertTrue(systemd_timer_active(DISPATCH_TIMER_UNIT))

        run.assert_called_once_with(
            ["/usr/bin/systemctl", "is-active", "--quiet", DISPATCH_TIMER_UNIT],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
            env={"PATH": "/usr/sbin:/usr/bin"},
        )
        with self.assertRaisesRegex(HealthFailure, "timer_probe_invalid"):
            systemd_timer_active("attacker.service")

    def test_main_logs_only_bounded_operational_fields(self) -> None:
        output = io.StringIO()
        with (
            mock.patch(
                "scripts.freio_telegram_handoff.health.check_health",
                return_value=HealthResult(
                    event="heartbeat_stale",
                    healthy=False,
                    status="retry",
                    age_seconds=900,
                ),
            ),
            contextlib.redirect_stdout(output),
        ):
            result = main()

        self.assertEqual(result, 1)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "ageSeconds": 900,
                "event": "heartbeat_stale",
                "healthy": False,
                "status": "retry",
            },
        )
        for forbidden in ("http", "@", "token", "conversation", "uuid"):
            self.assertNotIn(forbidden, output.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
