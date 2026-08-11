from __future__ import annotations

import unittest
from pathlib import Path


class SystemdContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[2]
        cls.service = (cls.root / "scripts/systemd/freio-b2b-agent.service").read_text()
        cls.timer = (cls.root / "scripts/systemd/freio-b2b-agent.timer").read_text()
        cls.http_source = (
            cls.root / "scripts/freio_b2b_agent/http_client.py"
        ).read_text()
        cls.local_source = (
            cls.root / "scripts/freio_b2b_agent/local_classifier.py"
        ).read_text()

    def test_service_is_marker_gated_and_uses_only_loadcredential(self) -> None:
        self.assertIn(
            "ConditionPathExists=/etc/freio-b2b-agent/enabled/classifier",
            self.service,
        )
        self.assertIn(
            "LoadCredential=freio-api-bearer:/etc/freio-b2b-agent/api-bearer",
            self.service,
        )
        self.assertNotIn("anthropic", self.service.lower())
        self.assertNotIn("claude", self.service.lower())
        self.assertNotIn("EnvironmentFile=", self.service)
        self.assertNotIn("worker-id", self.service.lower())
        self.assertNotIn("--endpoint", self.service)
        self.assertIn(
            "OnFailure=notify-failure@freio-b2b-agent.service",
            self.service,
        )

    def test_service_has_expected_sandbox(self) -> None:
        for directive in [
            "NoNewPrivileges=true",
            "PrivateDevices=true",
            "PrivateTmp=true",
            "ProtectSystem=strict",
            "ProtectHome=true",
            "ProtectProc=invisible",
            "RestrictNamespaces=true",
            "MemoryDenyWriteExecute=true",
            "CapabilityBoundingSet=",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
            "SystemCallFilter=@system-service",
            "UMask=0077",
        ]:
            self.assertIn(directive, self.service)

    def test_timer_has_no_activation_side_effect(self) -> None:
        self.assertIn("Unit=freio-b2b-agent.service", self.timer)
        self.assertIn("WantedBy=timers.target", self.timer)
        self.assertNotIn("Also=", self.timer)

    def test_network_adapter_is_fixed_and_classifier_is_local(self) -> None:
        self.assertIn("urllib.request.ProxyHandler({})", self.http_source)
        self.assertIn("_NoRedirect()", self.http_source)
        self.assertIn(
            'CLAIM_URL = "https://outreach.freio.cz/api/internal/b2b-agent/tasks/claim"',
            self.http_source,
        )
        self.assertIn(
            'COMPLETE_URL = "https://outreach.freio.cz/api/internal/b2b-agent/tasks/complete"',
            self.http_source,
        )
        self.assertIn("class LocalRuleClassifier", self.local_source)
        self.assertNotIn("subprocess", self.local_source)
        self.assertNotIn("urllib", self.local_source)
        self.assertNotIn("requests", self.local_source)


if __name__ == "__main__":
    unittest.main()
