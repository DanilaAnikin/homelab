from __future__ import annotations

import io
import os
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.homelab_telegram_notify import transport

TOKEN = "12345:" + "A" * 24
CHAT_ID = "-1"


class FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b'{"ok":true}') -> None:
        self.status = status
        self.body = body

    def read(self, maximum: int | None = None) -> bytes:
        if maximum is None:
            return self.body
        return self.body[:maximum]


class FakeConnection:
    def __init__(self, response: FakeResponse | None = None) -> None:
        self.response = response or FakeResponse()
        self.requests: list[tuple[str, str, bytes | None, dict[str, str] | None]] = []
        self.closed = False

    def request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.requests.append((method, url, body, headers))

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


class TransportTests(unittest.TestCase):
    def test_credentials_are_read_from_two_regular_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "telegram-token").write_text(TOKEN + "\n", encoding="ascii")
            (root / "telegram-chat-id").write_text(CHAT_ID + "\n", encoding="ascii")
            self.assertEqual(transport.load_credentials(root), (TOKEN, CHAT_ID))

    def test_symlinked_credential_fails_closed(self) -> None:
        if not hasattr(os, "O_NOFOLLOW"):
            self.skipTest("O_NOFOLLOW unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "actual-token"
            target.write_text(TOKEN, encoding="ascii")
            (root / "telegram-token").symlink_to(target)
            (root / "telegram-chat-id").write_text(CHAT_ID, encoding="ascii")
            with self.assertRaisesRegex(
                transport.NotifyError, "credential_telegram-token_unavailable"
            ):
                transport.load_credentials(root)

    def test_message_is_bounded_utf8_and_rejects_control_characters(self) -> None:
        self.assertEqual(transport.read_message(io.BytesIO("ahoj".encode())), "ahoj")
        with self.assertRaisesRegex(transport.NotifyError, "message_too_large"):
            transport.read_message(io.BytesIO(b"x" * (transport.MAX_MESSAGE_BYTES + 1)))
        with self.assertRaisesRegex(
            transport.NotifyError, "message_invalid_control_character"
        ):
            transport.read_message(io.BytesIO(b"alert\x00hidden"))

    def test_send_is_direct_https_form_post_without_redirect_or_proxy_layer(
        self,
    ) -> None:
        connection = FakeConnection()
        transport.send_message(
            TOKEN,
            CHAT_ID,
            "disk warning",
            connection_factory=lambda: connection,
        )

        self.assertTrue(connection.closed)
        self.assertEqual(len(connection.requests), 1)
        method, path, raw_body, headers = connection.requests[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, f"/bot{TOKEN}/sendMessage")
        self.assertIsNotNone(raw_body)
        form = urllib.parse.parse_qs((raw_body or b"").decode("utf-8"))
        self.assertEqual(form["chat_id"], [CHAT_ID])
        self.assertEqual(form["text"], ["🏠 homelab: disk warning"])
        self.assertEqual(form["disable_web_page_preview"], ["true"])
        self.assertEqual(
            headers and headers.get("Content-Type"),
            "application/x-www-form-urlencoded",
        )

    def test_telegram_text_is_bounded_in_utf16_units(self) -> None:
        connection = FakeConnection()
        transport.send_message(
            TOKEN,
            CHAT_ID,
            "😀" * 4_000,
            connection_factory=lambda: connection,
        )
        raw_body = connection.requests[0][2] or b""
        text = urllib.parse.parse_qs(raw_body.decode("utf-8"))["text"][0]
        units = len(text.encode("utf-16-le")) // 2
        self.assertLessEqual(units, transport.MAX_TELEGRAM_UTF16_UNITS)

    def test_non_200_response_never_includes_provider_body_in_error(self) -> None:
        connection = FakeConnection(FakeResponse(403, b"secret provider detail"))
        with self.assertRaisesRegex(
            transport.NotifyError, "telegram_http_403"
        ) as raised:
            transport.send_message(
                TOKEN,
                CHAT_ID,
                "alert",
                connection_factory=lambda: connection,
            )
        self.assertNotIn("provider detail", str(raised.exception))

    def test_main_failure_logs_no_token_chat_or_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "telegram-token").write_text(TOKEN, encoding="ascii")
            (root / "telegram-chat-id").write_text(CHAT_ID, encoding="ascii")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": temporary}),
                patch.object(
                    transport,
                    "send_message",
                    side_effect=transport.NotifyError("telegram_transport_failure"),
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                result = transport.main([], io.BytesIO(b"private incident"))

        output = stdout.getvalue() + stderr.getvalue()
        self.assertEqual(result, 1)
        self.assertNotIn(TOKEN, output)
        self.assertNotIn(CHAT_ID, output)
        self.assertNotIn("private incident", output)

    def test_unexpected_exception_is_also_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "telegram-token").write_text(TOKEN, encoding="ascii")
            (root / "telegram-chat-id").write_text(CHAT_ID, encoding="ascii")
            stdout = io.StringIO()
            stderr = io.StringIO()
            unsafe_detail = f"{TOKEN} {CHAT_ID} private incident"
            with (
                patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": temporary}),
                patch.object(
                    transport, "send_message", side_effect=RuntimeError(unsafe_detail)
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                result = transport.main([], io.BytesIO(b"private incident"))

        output = stdout.getvalue() + stderr.getvalue()
        self.assertEqual(result, 1)
        self.assertIn("unexpected_internal_error", output)
        self.assertNotIn(TOKEN, output)
        self.assertNotIn(CHAT_ID, output)
        self.assertNotIn("private incident", output)


if __name__ == "__main__":
    unittest.main()
