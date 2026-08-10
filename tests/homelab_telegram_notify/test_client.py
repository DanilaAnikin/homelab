from __future__ import annotations

import io
import socket
import unittest
from unittest.mock import patch

from scripts.homelab_telegram_notify import client


class FakeSocket:
    def __init__(
        self, response: bytes = b"OK\n", response_chunks: list[bytes] | None = None
    ) -> None:
        self.responses = list(response_chunks or [response])
        self.connected_to: str | None = None
        self.payload = b""
        self.timeout: float | None = None
        self.shutdown_mode: int | None = None
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def connect(self, path: str) -> None:
        self.connected_to = path

    def sendall(self, payload: bytes) -> None:
        self.payload += payload

    def shutdown(self, mode: int) -> None:
        self.shutdown_mode = mode

    def recv(self, maximum: int) -> bytes:
        if not self.responses:
            return b""
        response = self.responses.pop(0)
        return response[:maximum]

    def close(self) -> None:
        self.closed = True


class ClientTests(unittest.TestCase):
    def test_deliver_sends_only_socket_payload_and_waits_for_ack(self) -> None:
        fake = FakeSocket()

        def factory(family: int, kind: int) -> FakeSocket:
            self.assertEqual(family, socket.AF_UNIX)
            self.assertEqual(kind, socket.SOCK_STREAM)
            return fake

        client.deliver(b"operational alert", socket_factory=factory)  # type: ignore[arg-type]

        self.assertEqual(fake.connected_to, client.SOCKET_PATH)
        self.assertEqual(fake.payload, b"operational alert")
        self.assertEqual(fake.shutdown_mode, socket.SHUT_WR)
        self.assertTrue(fake.closed)

    def test_deliver_accepts_a_fragmented_success_ack(self) -> None:
        fake = FakeSocket(response_chunks=[b"O", b"K\n"])

        def factory(_family: int, _kind: int) -> FakeSocket:
            return fake

        client.deliver(b"alert", socket_factory=factory)  # type: ignore[arg-type]

    def test_deliver_fails_closed_on_non_success_ack(self) -> None:
        fake = FakeSocket(b"ERR\n")

        def factory(_family: int, _kind: int) -> FakeSocket:
            return fake

        with self.assertRaisesRegex(RuntimeError, "transport_rejected"):
            client.deliver(b"alert", socket_factory=factory)  # type: ignore[arg-type]

    def test_main_rejects_message_in_argv_without_reading_or_connecting(self) -> None:
        with patch.object(client, "deliver") as deliver:
            result = client.main(["secret alert"], io.BytesIO(b"ignored"))
        self.assertEqual(result, 64)
        deliver.assert_not_called()

    def test_main_bounds_stdin_before_connecting(self) -> None:
        oversized = io.BytesIO(b"x" * (client.MAX_MESSAGE_BYTES + 1))
        with patch.object(client, "deliver") as deliver:
            result = client.main([], oversized)
        self.assertEqual(result, 64)
        deliver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
