#!/usr/bin/env python3
"""Unprivileged stdin-only client for the Homelab Telegram socket."""

from __future__ import annotations

import socket
import sys
from collections.abc import Sequence
from typing import BinaryIO

SOCKET_PATH = "/run/homelab-telegram-notify/notify.sock"
MAX_MESSAGE_BYTES = 8_192
MAX_RESPONSE_BYTES = 64
SOCKET_TIMEOUT_SECONDS = 25.0


def _read_bounded(stream: BinaryIO, limit: int) -> bytes:
    payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise ValueError("message_too_large")
    if not payload.strip():
        raise ValueError("message_empty")
    return payload


def deliver(
    payload: bytes,
    *,
    socket_path: str = SOCKET_PATH,
    socket_factory: type[socket.socket] = socket.socket,
) -> None:
    if not payload or len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError("invalid_message_size")

    client = socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(SOCKET_TIMEOUT_SECONDS)
        client.connect(socket_path)
        client.sendall(payload)
        client.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        response_size = 0
        while response_size < MAX_RESPONSE_BYTES:
            chunk = client.recv(MAX_RESPONSE_BYTES - response_size)
            if not chunk:
                break
            chunks.append(chunk)
            response_size += len(chunk)
            if b"\n" in chunk:
                break
        response = b"".join(chunks)
    finally:
        client.close()

    if response != b"OK\n":
        raise RuntimeError("transport_rejected")


def main(argv: Sequence[str] | None = None, stdin: BinaryIO | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    source = sys.stdin.buffer if stdin is None else stdin

    if args:
        print("notify-client: zpráva je povolena pouze přes stdin", file=sys.stderr)
        return 64

    try:
        payload = _read_bounded(source, MAX_MESSAGE_BYTES)
        deliver(payload)
    except ValueError as exc:
        print(f"notify-client: odmítnuto ({exc})", file=sys.stderr)
        return 64
    except (OSError, RuntimeError):
        print("notify-client: bezpečný transport není dostupný", file=sys.stderr)
        return 1

    print("notify: odesláno")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
