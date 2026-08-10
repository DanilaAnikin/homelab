#!/usr/bin/env python3
"""One-request Telegram transport entered through a systemd-activated socket.

The process receives the alert over stdin/socket and Telegram credentials from
systemd's private credential directory. It never accepts a message, token or
chat ID through argv or ordinary environment variables.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import ssl
import stat
import sys
import urllib.parse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import BinaryIO, Protocol, cast

TELEGRAM_HOST = "api.telegram.org"
TELEGRAM_PORT = 443
MAX_MESSAGE_BYTES = 8_192
MAX_TELEGRAM_UTF16_UNITS = 3_500
MAX_RESPONSE_BYTES = 16_384
HTTP_TIMEOUT_SECONDS = 15.0
TOKEN_PATTERN = re.compile(r"^[0-9]{5,16}:[A-Za-z0-9_-]{20,128}$")
CHAT_ID_PATTERN = re.compile(r"^-?[1-9][0-9]{0,19}$")


class Response(Protocol):
    status: int

    def read(self, amt: int | None = None) -> bytes: ...


class Connection(Protocol):
    def request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None: ...

    def getresponse(self) -> Response: ...

    def close(self) -> None: ...


ConnectionFactory = Callable[[], Connection]


class NotifyError(RuntimeError):
    """Expected fail-closed transport error with a non-sensitive code."""


def _read_credential(directory: Path, name: str, maximum: int) -> str:
    path = directory / name
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise NotifyError(f"credential_{name}_unavailable") from exc

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise NotifyError(f"credential_{name}_not_regular")
        try:
            value = os.read(descriptor, maximum + 1)
        except OSError as exc:
            raise NotifyError(f"credential_{name}_unreadable") from exc
    finally:
        os.close(descriptor)

    if len(value) > maximum:
        raise NotifyError(f"credential_{name}_too_large")
    try:
        decoded = value.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise NotifyError(f"credential_{name}_invalid_encoding") from exc
    if not decoded:
        raise NotifyError(f"credential_{name}_empty")
    return decoded


def load_credentials(directory: Path) -> tuple[str, str]:
    token = _read_credential(directory, "telegram-token", 160)
    chat_id = _read_credential(directory, "telegram-chat-id", 24)
    if not TOKEN_PATTERN.fullmatch(token):
        raise NotifyError("credential_telegram-token_invalid")
    if not CHAT_ID_PATTERN.fullmatch(chat_id):
        raise NotifyError("credential_telegram-chat-id_invalid")
    return token, chat_id


def read_message(stream: BinaryIO) -> str:
    try:
        raw = stream.read(MAX_MESSAGE_BYTES + 1)
    except OSError as exc:
        raise NotifyError("message_unreadable") from exc
    if len(raw) > MAX_MESSAGE_BYTES:
        raise NotifyError("message_too_large")
    try:
        message = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise NotifyError("message_invalid_utf8") from exc
    if not message:
        raise NotifyError("message_empty")
    if any(ord(character) < 32 and character not in "\n\r\t" for character in message):
        raise NotifyError("message_invalid_control_character")
    return message


def default_connection_factory() -> Connection:
    context = ssl.create_default_context()
    return cast(
        Connection,
        http.client.HTTPSConnection(
            TELEGRAM_HOST,
            TELEGRAM_PORT,
            timeout=HTTP_TIMEOUT_SECONDS,
            context=context,
        ),
    )


def _truncate_utf16(text: str, maximum_units: int) -> str:
    units = 0
    result: list[str] = []
    for character in text:
        character_units = 2 if ord(character) > 0xFFFF else 1
        if units + character_units > maximum_units:
            break
        result.append(character)
        units += character_units
    return "".join(result)


def send_message(
    token: str,
    chat_id: str,
    message: str,
    *,
    connection_factory: ConnectionFactory = default_connection_factory,
) -> None:
    telegram_text = _truncate_utf16(f"🏠 homelab: {message}", MAX_TELEGRAM_UTF16_UNITS)
    body = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": telegram_text,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    path = f"/bot{token}/sendMessage"
    try:
        connection = connection_factory()
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        raise NotifyError("telegram_transport_failure") from exc
    try:
        connection.request(
            "POST",
            path,
            body=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len(body)),
                "User-Agent": "homelab-telegram-notify/1.0",
            },
        )
        response = connection.getresponse()
        response_body = response.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        raise NotifyError("telegram_transport_failure") from exc
    finally:
        try:
            connection.close()
        except OSError:
            pass

    if len(response_body) > MAX_RESPONSE_BYTES:
        raise NotifyError("telegram_response_too_large")
    if response.status != 200:
        raise NotifyError(f"telegram_http_{response.status}")
    try:
        parsed = json.loads(response_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NotifyError("telegram_invalid_response") from exc
    if not isinstance(parsed, dict) or parsed.get("ok") is not True:
        raise NotifyError("telegram_rejected_response")


def main(argv: Sequence[str] | None = None, stdin: BinaryIO | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    source = sys.stdin.buffer if stdin is None else stdin
    if args:
        print("notify-transport: argv rejected", file=sys.stderr)
        print("ERR\n", end="")
        return 64

    credential_directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not credential_directory:
        print("notify-transport: credentials unavailable", file=sys.stderr)
        print("ERR\n", end="")
        return 1

    try:
        token, chat_id = load_credentials(Path(credential_directory))
        message = read_message(source)
        send_message(token, chat_id, message)
    except NotifyError as exc:
        print(f"notify-transport: failed ({exc})", file=sys.stderr)
        print("ERR\n", end="")
        return 1
    except Exception:
        print("notify-transport: failed (unexpected_internal_error)", file=sys.stderr)
        print("ERR\n", end="")
        return 1

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
