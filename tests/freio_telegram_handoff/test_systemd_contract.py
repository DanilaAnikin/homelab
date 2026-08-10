from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SERVICE = ROOT / "scripts/systemd/freio-telegram-handoff.service"
TIMER = ROOT / "scripts/systemd/freio-telegram-handoff.timer"
RUNBOOK = ROOT / "docs/freio-telegram-handoff.md"


class SystemdContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service = SERVICE.read_text(encoding="utf-8")
        cls.timer = TIMER.read_text(encoding="utf-8")
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

    def test_timer_is_non_persistent_and_not_self_activating(self) -> None:
        self.assertIn("OnUnitInactiveSec=2min", self.timer)
        self.assertIn("RandomizedDelaySec=15s", self.timer)
        self.assertIn("Persistent=false", self.timer)
        self.assertNotIn("Persistent=true", self.timer)

    def test_runbook_requires_rotation_and_explicit_marker(self) -> None:
        lowered = self.runbook.lower()
        self.assertIn("rotovat produkční telegram bot token", lowered)
        self.assertIn("dispatcher je po instalaci **vypnutý**", lowered)
        self.assertIn("/etc/freio-telegram-handoff/enabled", self.runbook)
        self.assertIn("pending-finalize-v1.json` nikdy ručně nemažte", lowered)
        self.assertIn("`actionsummary` je přesný pii-free objekt", lowered)
        self.assertIn("raw e-mail", lowered)
        self.assertIn("llm shrnutí", lowered)


if __name__ == "__main__":
    unittest.main()
