from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SERVICE = ROOT / "scripts/systemd/freio-telegram-handoff.service"
TIMER = ROOT / "scripts/systemd/freio-telegram-handoff.timer"
HEALTH_SERVICE = ROOT / "scripts/systemd/freio-telegram-handoff-health.service"
HEALTH_TIMER = ROOT / "scripts/systemd/freio-telegram-handoff-health.timer"
HEALTH_SCRIPT = ROOT / "scripts/freio_telegram_handoff/health.py"
RUNBOOK = ROOT / "docs/freio-telegram-handoff.md"


class SystemdContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service = SERVICE.read_text(encoding="utf-8")
        cls.timer = TIMER.read_text(encoding="utf-8")
        cls.health_service = HEALTH_SERVICE.read_text(encoding="utf-8")
        cls.health_timer = HEALTH_TIMER.read_text(encoding="utf-8")
        cls.health_script = HEALTH_SCRIPT.read_text(encoding="utf-8")
        cls.runbook = RUNBOOK.read_text(encoding="utf-8")

    def test_service_uses_only_systemd_credentials_for_secrets(self) -> None:
        self.assertIn(
            "LoadCredential=telegram-token:/etc/homelab-telegram/telegram-token",
            self.service,
        )
        self.assertIn(
            "LoadCredential=telegram-chat-id:/etc/homelab-telegram/telegram-chat-id",
            self.service,
        )
        self.assertIn(
            "LoadCredential=freio-machine-secret:"
            "/etc/freio-telegram-handoff/freio-machine-secret",
            self.service,
        )
        self.assertNotIn("EnvironmentFile=", self.service)
        self.assertNotIn("Environment=", self.service)
        self.assertEqual(
            [
                line
                for line in self.service.splitlines()
                if line.startswith("ExecStart=")
            ],
            ["ExecStart=/usr/local/libexec/freio-telegram-handoff"],
        )

    def test_service_is_fail_closed_and_hardened(self) -> None:
        required = {
            "DynamicUser=true",
            "UMask=0077",
            "StateDirectory=freio-telegram-handoff",
            "StateDirectoryMode=0700",
            "LimitCORE=0",
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "PrivateDevices=true",
            "ProtectSystem=strict",
            "ProtectHome=true",
            "InaccessiblePaths=-/etc/homelab-telegram "
            "-/srv/frem/telegram-token "
            "-/srv/homelab/secrets",
            "ProtectProc=invisible",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
            "RestrictNamespaces=true",
            "RestrictSUIDSGID=true",
            "SystemCallFilter=@system-service",
            "CapabilityBoundingSet=",
            "AmbientCapabilities=",
        }
        self.assertTrue(required.issubset(set(self.service.splitlines())))
        self.assertIn(
            "ConditionPathExists=/etc/freio-telegram-handoff/enabled",
            self.service,
        )
        self.assertIn(
            "OnFailure=notify-failure@freio-telegram-handoff.service",
            self.service,
        )

    def test_timer_is_non_persistent_and_not_self_activating(self) -> None:
        self.assertIn("OnUnitInactiveSec=2min", self.timer)
        self.assertIn("RandomizedDelaySec=15s", self.timer)
        self.assertIn("Persistent=false", self.timer)
        self.assertNotIn("Persistent=true", self.timer)

    def test_health_service_is_root_private_and_uses_existing_failure_transport(
        self,
    ) -> None:
        required = {
            "ConditionPathExists=/etc/freio-telegram-handoff/enabled",
            "OnFailure=notify-failure@freio-telegram-handoff-health.service",
            "User=root",
            "Group=root",
            "UMask=0077",
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "PrivateDevices=true",
            "PrivateMounts=true",
            "ProtectSystem=strict",
            "ProtectHome=true",
            "ReadOnlyPaths=/etc/freio-telegram-handoff/enabled "
            "-/var/lib/private/freio-telegram-handoff",
            "ProtectProc=invisible",
            "RestrictAddressFamilies=AF_UNIX",
            "RestrictNamespaces=true",
            "SystemCallFilter=@system-service",
            "CapabilityBoundingSet=CAP_DAC_READ_SEARCH",
            "AmbientCapabilities=CAP_DAC_READ_SEARCH",
            "LimitCORE=0",
        }
        self.assertTrue(required.issubset(set(self.health_service.splitlines())))
        self.assertEqual(
            [
                line
                for line in self.health_service.splitlines()
                if line.startswith("ExecStart=")
            ],
            ["ExecStart=/usr/local/libexec/freio-telegram-handoff-health"],
        )
        self.assertNotIn("LoadCredential=", self.health_service)
        self.assertNotIn("Environment=", self.health_service)
        self.assertNotIn("EnvironmentFile=", self.health_service)
        self.assertNotIn("AF_INET", self.health_service)

    def test_health_timer_is_bounded_non_persistent_and_default_off(self) -> None:
        self.assertIn("OnBootSec=12min", self.health_timer)
        self.assertIn("OnUnitInactiveSec=5min", self.health_timer)
        self.assertIn("RandomizedDelaySec=30s", self.health_timer)
        self.assertIn("Persistent=false", self.health_timer)
        self.assertNotIn("Persistent=true", self.health_timer)
        self.assertNotIn("ConditionPathExists", self.health_timer)

    def test_health_contract_contains_only_bounded_operational_state(self) -> None:
        self.assertIn(
            'HEARTBEAT_STATUSES = frozenset({"idle", "sent", "retry", "error"})',
            self.health_script,
        )
        self.assertIn("MAX_HEARTBEAT_AGE_SECONDS = 10 * 60", self.health_script)
        self.assertNotIn("telegram-token", self.health_script)
        self.assertNotIn("telegram-chat-id", self.health_script)
        self.assertNotIn("freio-machine-secret", self.health_script)

    def test_runbook_requires_rotation_and_explicit_marker(self) -> None:
        lowered = self.runbook.lower()
        self.assertIn("rotovat produkční telegram bot token", lowered)
        self.assertIn("dispatcher je po instalaci **vypnutý**", lowered)
        self.assertIn("/etc/freio-telegram-handoff/enabled", self.runbook)
        self.assertIn("pending-finalize-v1.json` nikdy ručně nemažte", lowered)
        self.assertIn("`actionsummary` je přesný pii-free objekt", lowered)
        self.assertIn("`priority` je přesně jeden z enumů", lowered)
        self.assertIn("heartbeat-v1.json", lowered)
        self.assertIn("freio-telegram-handoff-health.timer", lowered)
        self.assertIn("10 minut", lowered)
        self.assertIn("raw e-mail", lowered)
        self.assertIn("llm shrnutí", lowered)


if __name__ == "__main__":
    unittest.main()
