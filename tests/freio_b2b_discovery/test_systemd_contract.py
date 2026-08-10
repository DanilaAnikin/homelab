from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SYSTEMD = ROOT / "scripts" / "systemd"


class SystemdContractTests(unittest.TestCase):
    def _read(self, suffix: str) -> str:
        return (SYSTEMD / f"freio-b2b-discovery-{suffix}").read_text()

    def test_all_services_are_marker_gated_and_hardened(self) -> None:
        for name in (
            "discover.service",
            "submit.service",
            "discover-housekeeping.service",
            "submit-housekeeping.service",
        ):
            with self.subTest(name=name):
                unit = self._read(name)
                self.assertIn(
                    "ConditionPathExists=/etc/freio-b2b-discovery/enabled/", unit
                )
                self.assertIn("NoNewPrivileges=true", unit)
                self.assertIn("ProtectSystem=strict", unit)
                self.assertIn("ProtectHome=true", unit)
                self.assertIn("PrivateDevices=true", unit)
                self.assertIn("CapabilityBoundingSet=\n", unit)
                self.assertIn("SystemCallFilter=@system-service", unit)
                self.assertNotIn("WantedBy=", unit)

    def test_discovery_and_submit_credentials_are_process_separated(self) -> None:
        discovery = self._read("discover.service")
        submit = self._read("submit.service")
        self.assertIn("LoadCredential=anthropic-api-key:", discovery)
        self.assertIn(
            "--anthropic-api-key-file "
            "/run/credentials/freio-b2b-discovery-discover.service/"
            "anthropic-api-key",
            discovery,
        )
        self.assertNotIn("intake-hmac", discovery)
        self.assertNotIn("identity-index-hmac", discovery)
        self.assertIn("LoadCredential=intake-hmac:", submit)
        self.assertIn("LoadCredential=identity-index-hmac:", submit)
        self.assertNotIn("anthropic-api-key", submit)
        self.assertIn("--max-batches 5", submit)
        self.assertIn("--send", submit)
        self.assertIn("${FREIO_B2B_DISCOVERY_ENDPOINT}", submit)

    def test_claude_home_is_ephemeral_and_only_discovery_has_web_tools(self) -> None:
        discovery = self._read("discover.service")
        self.assertIn("RuntimeDirectory=freio-b2b-discovery", discovery)
        self.assertIn("RuntimeDirectoryPreserve=no", discovery)
        self.assertIn("--claude-home /run/freio-b2b-discovery/claude-home", discovery)
        self.assertIn("scripts/freio-prospecting", discovery)

    def test_housekeeping_has_no_ip_network_and_minimum_credentials(self) -> None:
        discovery = self._read("discover-housekeeping.service")
        submit = self._read("submit-housekeeping.service")
        self.assertIn("RestrictAddressFamilies=AF_UNIX", discovery)
        self.assertNotIn("LoadCredential=", discovery)
        self.assertIn("RestrictAddressFamilies=AF_UNIX", submit)
        self.assertIn("LoadCredential=identity-index-hmac:", submit)
        self.assertNotIn("intake-hmac", submit)
        self.assertNotIn("anthropic-api-key", submit)

    def test_timers_are_templates_only_and_point_to_exact_units(self) -> None:
        expected = {
            "discover.timer": "freio-b2b-discovery-discover.service",
            "submit.timer": "freio-b2b-discovery-submit.service",
            "discover-housekeeping.timer": (
                "freio-b2b-discovery-discover-housekeeping.service"
            ),
            "submit-housekeeping.timer": (
                "freio-b2b-discovery-submit-housekeeping.service"
            ),
        }
        for name, unit_name in expected.items():
            with self.subTest(name=name):
                timer = self._read(name)
                self.assertIn(f"Unit={unit_name}", timer)
                self.assertIn("WantedBy=timers.target", timer)
                self.assertNotIn("ConditionPathExists", timer)


if __name__ == "__main__":
    unittest.main()
