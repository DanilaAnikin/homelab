from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/freio-analytics-retention.sh"
SERVICE = ROOT / "scripts/systemd/freio-analytics-retention.service"
TIMER = ROOT / "scripts/systemd/freio-analytics-retention.timer"
BACKUP_TIMER = ROOT / "scripts/systemd/backup.timer"
BACKUP_SCRIPT = ROOT / "scripts/backup.sh"
RUNBOOK = ROOT / "docs/freio-analytics-retention.md"


class FreioAnalyticsRetentionContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = SCRIPT.read_text(encoding="utf-8")
        cls.service = SERVICE.read_text(encoding="utf-8")
        cls.timer = TIMER.read_text(encoding="utf-8")
        cls.backup_timer = BACKUP_TIMER.read_text(encoding="utf-8")
        cls.backup_script = BACKUP_SCRIPT.read_text(encoding="utf-8")
        cls.runbook = RUNBOOK.read_text(encoding="utf-8")

    def test_shell_source_is_valid_and_local_only(self) -> None:
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
        self.assertIn("readonly CONTAINER=supabase-db", self.script)
        self.assertIn("readonly DATABASE=freio", self.script)
        self.assertIn('SET ROLE service_role;', self.script)
        self.assertEqual(2, self.script.count("SET statement_timeout = '10s';"))
        first_timeout = self.script.index("SET statement_timeout = '10s';")
        first_rpc = self.script.index("purge_analytics_events_retention(:batch_size")
        self.assertLess(first_timeout, first_rpc)
        self.assertIn("purge_analytics_events_retention", self.script)
        self.assertIn("docker", self.script)
        for forbidden in ("curl", "wget", "http://", "https://", "token", "secret"):
            self.assertNotIn(forbidden, self.script.lower())

    def test_script_reuses_cutoff_and_fails_on_bounded_backlog(self) -> None:
        self.assertIn("readonly BATCH_SIZE=1000", self.script)
        self.assertIn("readonly MAX_BATCHES=60", self.script)
        self.assertIn("readonly MAX_NO_PROGRESS=5", self.script)
        self.assertIn('returned_cutoff" != "$fixed_cutoff', self.script)
        self.assertIn("locked_or_nonprogressing_backlog", self.script)
        self.assertIn("safety_cap_backlog_remaining", self.script)
        self.assertIn('"has_more":false', self.script)

    def test_service_is_root_only_hardened_and_uses_existing_alerting(self) -> None:
        required = {
            "User=root",
            "Group=root",
            "UMask=0077",
            "StateDirectory=freio-analytics-retention",
            "StateDirectoryMode=0700",
            "ExecStart=/usr/local/sbin/freio-analytics-retention",
            "OnFailure=notify-failure@freio-analytics-retention.service",
            "Before=backup.service",
            "TimeoutStartSec=15min",
            "NoNewPrivileges=true",
            "ProtectSystem=strict",
            "ProtectHome=true",
            "RestrictAddressFamilies=AF_UNIX",
            "CapabilityBoundingSet=",
            "AmbientCapabilities=",
        }
        self.assertTrue(required.issubset(set(self.service.splitlines())))
        self.assertNotIn("EnvironmentFile=", self.service)
        self.assertNotIn("LoadCredential=", self.service)

    def test_timer_is_persistent_and_precedes_verified_backup_schedule(self) -> None:
        self.assertIn("OnCalendar=*-*-* 03:00:00", self.timer)
        self.assertIn("Persistent=true", self.timer)
        self.assertIn("OnCalendar=*-*-* 03:30", self.backup_timer)
        self.assertIn("RandomizedDelaySec=15m", self.backup_timer)
        self.assertIn("Persistent=true", self.backup_timer)

    def test_documentation_matches_verified_encrypted_backup_tails(self) -> None:
        self.assertIn("KEEP_R2_DAYS=30", self.backup_script)
        self.assertIn("KEEP_R2_DR_DAYS=90", self.backup_script)
        self.assertIn("primary R2 nightly", self.runbook)
        self.assertIn("DR R2 bucket", self.runbook)
        self.assertIn("before exposure", self.runbook)

    def test_backup_fails_loud_and_bundles_retention_installation(self) -> None:
        required_paths = (
            "usr/local/sbin/freio-analytics-retention",
            "etc/systemd/system/freio-analytics-retention.service",
            "etc/systemd/system/freio-analytics-retention.timer",
        )
        for path in required_paths:
            self.assertIn(path, self.backup_script)

        tar_failure = self.backup_script.index('echo "!! config tar selhal"; FAIL=1')
        config_section_end = self.backup_script.index(
            "# ── 4) Secrets bundle", tar_failure
        )
        self.assertLess(tar_failure, config_section_end)
        self.assertNotIn("config tar částečně selhal", self.backup_script)

    def test_runbook_orders_install_backup_and_encrypted_bundle_check(self) -> None:
        retention_install = self.runbook.index(
            "/srv/homelab/scripts/freio-analytics-retention.sh"
        )
        units_install = self.runbook.index(
            "/srv/homelab/scripts/systemd/freio-analytics-retention.service"
        )
        backup_install = self.runbook.index("/srv/homelab/scripts/backup.sh")
        manual_backup = self.runbook.index("sudo systemctl start backup.service")
        decrypt_check = self.runbook.index("sudo openssl enc -d -aes-256-cbc")
        self.assertLess(retention_install, backup_install)
        self.assertLess(units_install, backup_install)
        self.assertLess(backup_install, manual_backup)
        self.assertLess(manual_backup, decrypt_check)

        for path in (
            "usr/local/sbin/freio-analytics-retention",
            "etc/systemd/system/freio-analytics-retention.service",
            "etc/systemd/system/freio-analytics-retention.timer",
        ):
            self.assertIn(f"sudo grep -Fx '{path}'", self.runbook)

        self.assertIn("## Operational rollback", self.runbook)
        self.assertIn(
            "sudo systemctl disable --now freio-analytics-retention.timer",
            self.runbook,
        )
        self.assertIn("delete raw rows irreversibly", self.runbook)
        self.assertIn(
            "Keep the installed executable and both unit files", self.runbook
        )


if __name__ == "__main__":
    unittest.main()
