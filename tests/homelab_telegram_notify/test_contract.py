from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class DeploymentContractTests(unittest.TestCase):
    def test_compatibility_wrapper_is_stdin_only_and_has_no_credentials(self) -> None:
        wrapper = (ROOT / "self-healing/notify.sh").read_text(encoding="utf-8")
        self.assertIn("if (( $# != 0 )); then", wrapper)
        self.assertIn("exec /usr/local/libexec/homelab-telegram-notify-client", wrapper)
        self.assertNotIn("curl", wrapper)
        self.assertNotIn("TOKEN", wrapper)
        self.assertNotIn("CHAT_ID", wrapper)

    def test_legacy_callers_never_put_message_in_notify_argv(self) -> None:
        sources = [
            *sorted((ROOT / "self-healing").glob("*.sh")),
            *sorted((ROOT / "self-healing").glob("*.py")),
            *sorted((ROOT / "scripts").glob("*.sh")),
            *sorted((ROOT / "scripts/systemd").glob("*notify*.service")),
        ]
        unsafe_shell = re.compile(r'(?:\$NOTIFY|notify\.sh)\s+["\']')
        unsafe_python = re.compile(r"subprocess\.run\(\[NOTIFY\s*,")
        for source in sources:
            content = source.read_text(encoding="utf-8")
            self.assertIsNone(unsafe_shell.search(content), source)
            self.assertIsNone(unsafe_python.search(content), source)

    def test_socket_transport_owns_credentials_and_is_default_off(self) -> None:
        socket_unit = (
            ROOT / "scripts/systemd/homelab-telegram-notify.socket"
        ).read_text(encoding="utf-8")
        service = (ROOT / "scripts/systemd/homelab-telegram-notify@.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("ConditionPathExists=/etc/homelab-telegram/enabled", socket_unit)
        self.assertIn("SocketMode=0660", socket_unit)
        self.assertIn("SocketGroup=telegram-notify", socket_unit)
        self.assertIn("DirectoryMode=0755", socket_unit)
        self.assertIn("Accept=yes", socket_unit)
        self.assertIn(
            "LoadCredential=telegram-token:/etc/homelab-telegram/telegram-token",
            service,
        )
        self.assertIn(
            "LoadCredential=telegram-chat-id:/etc/homelab-telegram/telegram-chat-id",
            service,
        )
        self.assertNotIn("Environment=", service)
        self.assertNotIn("EnvironmentFile=", service)
        self.assertIn("LimitCORE=0", service)
        self.assertEqual(
            [line for line in service.splitlines() if line.startswith("ExecStart=")],
            ["ExecStart=/usr/local/libexec/homelab-telegram-notify-transport"],
        )

    def test_alertmanager_uses_only_file_credentials(self) -> None:
        config = (
            ROOT / "compose/observability/alertmanager/alertmanager.yml"
        ).read_text(encoding="utf-8")
        compose = (ROOT / "compose/observability/docker-compose.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("bot_token_file: /run/credentials/telegram-token", config)
        self.assertIn("chat_id_file: /run/credentials/telegram-chat-id", config)
        self.assertNotRegex(config, r"(?m)^\s+bot_token:\s*")
        self.assertNotRegex(config, r"(?m)^\s+chat_id:\s*")
        self.assertIn("source: /etc/homelab-telegram/telegram-token", compose)
        self.assertIn("target: /run/credentials/telegram-token", compose)
        self.assertIn("source: /etc/homelab-telegram/telegram-chat-id", compose)
        self.assertIn("target: /run/credentials/telegram-chat-id", compose)
        self.assertEqual(compose.count("create_host_path: false"), 2)
        self.assertIn("profiles: [telegram]", compose)
        self.assertIn("cap_drop: [ALL]", compose)
        self.assertIn("security_opt: [no-new-privileges:true]", compose)
        self.assertIn("core: 0", compose)

    def test_freio_dispatcher_reuses_canonical_credentials_without_copy(self) -> None:
        service = (ROOT / "scripts/systemd/freio-telegram-handoff.service").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "LoadCredential=telegram-token:/etc/homelab-telegram/telegram-token",
            service,
        )
        self.assertIn(
            "LoadCredential=telegram-chat-id:/etc/homelab-telegram/telegram-chat-id",
            service,
        )
        self.assertNotIn("/etc/freio-telegram-handoff/telegram-token", service)
        self.assertNotIn("/etc/freio-telegram-handoff/telegram-chat-id", service)


if __name__ == "__main__":
    unittest.main()
