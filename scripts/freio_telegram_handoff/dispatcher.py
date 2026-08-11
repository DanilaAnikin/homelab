#!/usr/bin/env python3
"""Deliver one opaque Freio owner handoff to Telegram.

The Freio database owns deduplication, leasing, retry attempts and the maximum
of eight delivery attempts. This process handles at most one claimed row per
invocation and never retries a Telegram send after an ambiguous response.
"""

from __future__ import annotations

import errno
import fcntl
import http.client
import json
import os
import re
import secrets
import socket
import ssl
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol


CLAIM_URL = "https://outreach.freio.cz/api/internal/b2b-agent/notifications/claim"
FINALIZE_URL = "https://outreach.freio.cz/api/internal/b2b-agent/notifications/finalize"
TELEGRAM_API_ORIGIN = "https://api.telegram.org"
STATE_DIRECTORY = Path("/var/lib/freio-telegram-handoff")
PRIVATE_STATE_DIRECTORY = Path("/var/lib/private/freio-telegram-handoff")
MAX_RESPONSE_BYTES = 64 * 1024
HTTP_TIMEOUT_SECONDS = 20
API_TIMEOUT_SECONDS = 15
DEFAULT_RETRY_SECONDS = 300
NETWORK_RETRY_SECONDS = 120
MIN_RETRY_SECONDS = 30
MAX_RETRY_SECONDS = 86_400
MESSAGE_HEADING = "Freio outreach"
CANARY_DEEP_LINK = "https://outreach.freio.cz/?section=overview"
CANARY_KIND = "system_canary"
HEARTBEAT_STATUSES = frozenset({"idle", "sent", "retry", "error"})

UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-" r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
ERROR_CODE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,119}$")
TELEGRAM_TOKEN_PATTERN = re.compile(r"^[0-9]{5,16}:[A-Za-z0-9_-]{20,128}$")
TELEGRAM_CHAT_PATTERN = re.compile(r"^-?[1-9][0-9]{0,19}$")
EVENT_KINDS = frozenset(
    {
        "new_inquiry",
        "reply_review",
        "pricing_approval",
        "legal_review",
        "unsubscribe_review",
        "delivery_uncertain",
        "policy_failure",
    }
)
INTENTS = frozenset(
    {
        "interested",
        "not_interested",
        "pricing",
        "demo",
        "question",
        "unsubscribe",
        "bounce",
        "ambiguous",
        "other",
    }
)
PRIORITIES = frozenset({"low", "normal", "high", "urgent"})
ACTION_LABELS = {
    "new_inquiry": "Zkontrolovat novou poptávku a navázat kontakt.",
    "reply_review": "Zkontrolovat odpověď klienta a rozhodnout další krok.",
    "pricing_approval": "Schválit nebo upravit cenovou nabídku.",
    "legal_review": "Provést právní kontrolu před odpovědí.",
    "unsubscribe_review": "Ověřit ukončení dalšího kontaktu.",
    "delivery_uncertain": "Ověřit doručení před další akcí.",
    "policy_failure": "Zkontrolovat blokované pravidlo automatizace.",
}
INTENT_LABELS = {
    "interested": "Klient má zájem.",
    "not_interested": "Klient nemá zájem.",
    "pricing": "Klient potřebuje vyřešit cenu.",
    "demo": "Klient chce call, demo nebo řeší implementaci.",
    "question": "Klient má produktový dotaz.",
    "unsubscribe": "Klient chce ukončit další kontakt.",
    "bounce": "Doručení zprávy selhalo.",
    "ambiguous": "Potřeba klienta není jasná.",
    "other": "Klient má jinou potřebu.",
}
PRIORITY_LABELS = {
    "low": "nízká",
    "normal": "běžná",
    "high": "vysoká",
    "urgent": "urgentní",
}
SAFE_UNAVAILABLE_ERRNOS = frozenset(
    {
        errno.ECONNREFUSED,
        errno.EHOSTDOWN,
        errno.EHOSTUNREACH,
        errno.ENETDOWN,
        errno.ENETUNREACH,
    }
)


class DispatcherFailure(Exception):
    """Base class whose message is intentionally never written to logs."""


class ConfigurationFailure(DispatcherFailure):
    pass


class AlreadyRunning(DispatcherFailure):
    pass


class NetworkUnavailable(DispatcherFailure):
    pass


class AmbiguousTransport(DispatcherFailure):
    pass


class RequestTimedOut(AmbiguousTransport):
    pass


class TlsValidationFailure(DispatcherFailure):
    pass


class InvalidClaimPayload(DispatcherFailure):
    def __init__(
        self,
        notification_id: str | None = None,
        claim_id: str | None = None,
        is_canary: bool = False,
    ) -> None:
        super().__init__("invalid claim payload")
        self.notification_id = notification_id
        self.claim_id = claim_id
        self.is_canary = is_canary


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    truncated: bool = False


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout: int,
    ) -> HttpResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        return None


class UrllibTransport:
    """HTTPS transport with redirects and environment proxies disabled."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )

    @staticmethod
    def _headers(value: Any) -> dict[str, str]:
        if value is None:
            return {}
        return {str(key).lower(): str(item) for key, item in value.items()}

    @staticmethod
    def _read_bounded(stream: Any) -> tuple[bytes, bool]:
        data = stream.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            return data[:MAX_RESPONSE_BYTES], True
        return data, False

    @staticmethod
    def _definitely_unavailable(error: OSError) -> bool:
        return isinstance(error, socket.gaierror) or error.errno in (
            SAFE_UNAVAILABLE_ERRNOS
        )

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout: int,
    ) -> HttpResponse:
        request = urllib.request.Request(
            url=url,
            data=body,
            headers=dict(headers),
            method=method,
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                response_body, truncated = self._read_bounded(response)
                return HttpResponse(
                    status=int(response.status),
                    headers=self._headers(response.headers),
                    body=response_body,
                    truncated=truncated,
                )
        except urllib.error.HTTPError as error:
            response_body, truncated = self._read_bounded(error)
            return HttpResponse(
                status=int(error.code),
                headers=self._headers(error.headers),
                body=response_body,
                truncated=truncated,
            )
        except urllib.error.URLError as error:
            reason = error.reason
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise RequestTimedOut() from None
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise TlsValidationFailure() from None
            if isinstance(reason, OSError) and self._definitely_unavailable(reason):
                raise NetworkUnavailable() from None
            raise AmbiguousTransport() from None
        except (socket.timeout, TimeoutError):
            raise RequestTimedOut() from None
        except ssl.SSLCertVerificationError:
            raise TlsValidationFailure() from None
        except (
            http.client.IncompleteRead,
            http.client.HTTPException,
            http.client.RemoteDisconnected,
            ConnectionResetError,
            ConnectionAbortedError,
            BrokenPipeError,
            ssl.SSLError,
        ):
            raise AmbiguousTransport() from None
        except OSError as error:
            if self._definitely_unavailable(error):
                raise NetworkUnavailable() from None
            raise AmbiguousTransport() from None


@dataclass(frozen=True)
class Credentials:
    telegram_token: str
    telegram_chat_id: str
    freio_machine_secret: str


@dataclass(frozen=True)
class LatestOffer:
    total_czk: int
    currency: str


@dataclass(frozen=True)
class ActionSummary:
    intent: str | None
    offer_count: int
    latest_offer: LatestOffer | None


@dataclass(frozen=True)
class Notification:
    notification_id: str
    claim_id: str
    event_id: str
    kind: str
    priority: str
    action_summary: ActionSummary
    deep_link: str
    is_canary: bool = False


@dataclass(frozen=True)
class Finalization:
    notification_id: str
    claim_id: str
    outcome: str
    provider_message_id: str | None = None
    error_code: str | None = None
    retry_after_seconds: int | None = None
    open_circuit: bool = False
    is_canary: bool = False

    def api_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "notificationId": self.notification_id,
            "claimId": self.claim_id,
            "outcome": self.outcome,
        }
        if self.provider_message_id is not None:
            payload["providerMessageId"] = self.provider_message_id
        if self.error_code is not None:
            payload["errorCode"] = self.error_code
        if self.retry_after_seconds is not None:
            payload["retryAfterSeconds"] = self.retry_after_seconds
        if self.is_canary:
            payload["canary"] = True
        return payload

    def state_payload(self) -> dict[str, Any]:
        return {
            "version": 1,
            "finalization": self.api_payload(),
            "openCircuit": self.open_circuit,
        }

    @classmethod
    def from_state_payload(cls, raw: Any) -> "Finalization":
        if not isinstance(raw, dict) or set(raw) != {
            "version",
            "finalization",
            "openCircuit",
        }:
            raise ConfigurationFailure()
        if raw["version"] != 1 or not isinstance(raw["openCircuit"], bool):
            raise ConfigurationFailure()
        payload = raw["finalization"]
        if not isinstance(payload, dict):
            raise ConfigurationFailure()
        allowed = {
            "notificationId",
            "claimId",
            "outcome",
            "providerMessageId",
            "errorCode",
            "retryAfterSeconds",
            "canary",
        }
        if not {"notificationId", "claimId", "outcome"}.issubset(payload):
            raise ConfigurationFailure()
        if not set(payload).issubset(allowed):
            raise ConfigurationFailure()
        if "canary" in payload and payload["canary"] is not True:
            raise ConfigurationFailure()
        finalization = cls(
            notification_id=_strict_uuid(payload["notificationId"]),
            claim_id=_strict_uuid(payload["claimId"]),
            outcome=payload["outcome"],
            provider_message_id=payload.get("providerMessageId"),
            error_code=payload.get("errorCode"),
            retry_after_seconds=payload.get("retryAfterSeconds"),
            open_circuit=raw["openCircuit"],
            is_canary=payload.get("canary", False),
        )
        _validate_finalization(finalization)
        return finalization


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ConfigurationFailure()
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    del value
    raise ConfigurationFailure()


def _decode_json(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ConfigurationFailure() from None


def _strict_uuid(value: Any) -> str:
    if not isinstance(value, str) or not UUID_PATTERN.fullmatch(value):
        raise ConfigurationFailure()
    return value.lower()


def _validate_finalization(finalization: Finalization) -> None:
    if not isinstance(finalization.is_canary, bool):
        raise ConfigurationFailure()
    if not isinstance(finalization.outcome, str) or finalization.outcome not in {
        "sent",
        "retry",
        "uncertain",
        "dead",
    }:
        raise ConfigurationFailure()
    if finalization.outcome == "sent":
        if (
            not isinstance(finalization.provider_message_id, str)
            or not finalization.provider_message_id.isdigit()
            or len(finalization.provider_message_id) > 500
        ):
            raise ConfigurationFailure()
    elif finalization.provider_message_id is not None:
        raise ConfigurationFailure()
    if finalization.error_code is not None:
        if not isinstance(
            finalization.error_code, str
        ) or not ERROR_CODE_PATTERN.fullmatch(finalization.error_code):
            raise ConfigurationFailure()
    if finalization.outcome == "retry":
        if (
            not isinstance(finalization.retry_after_seconds, int)
            or isinstance(finalization.retry_after_seconds, bool)
            or not MIN_RETRY_SECONDS
            <= finalization.retry_after_seconds
            <= MAX_RETRY_SECONDS
        ):
            raise ConfigurationFailure()
    elif finalization.retry_after_seconds is not None:
        raise ConfigurationFailure()


def _read_json_response(response: HttpResponse) -> Any:
    content_type = response.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        raise ConfigurationFailure()
    if response.truncated or not response.body:
        raise ConfigurationFailure()
    return _decode_json(response.body)


def _canonical_deep_link(event_id: str) -> str:
    return f"https://outreach.freio.cz/?section=conversations&conversation={event_id}"


def _bounded_integer(value: Any, minimum: int, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
        or value > maximum
    ):
        raise ConfigurationFailure()
    return value


def _parse_action_summary(value: Any) -> ActionSummary:
    if not isinstance(value, dict) or set(value) != {
        "intent",
        "offerCount",
        "latestOffer",
    }:
        raise ConfigurationFailure()
    intent = value["intent"]
    if intent is not None and (not isinstance(intent, str) or intent not in INTENTS):
        raise ConfigurationFailure()
    offer_count = _bounded_integer(value["offerCount"], 0, 10_000)
    latest_value = value["latestOffer"]
    latest_offer: LatestOffer | None = None
    if latest_value is not None:
        if not isinstance(latest_value, dict) or set(latest_value) != {
            "totalCzk",
            "currency",
        }:
            raise ConfigurationFailure()
        if latest_value["currency"] != "CZK":
            raise ConfigurationFailure()
        latest_offer = LatestOffer(
            total_czk=_bounded_integer(
                latest_value["totalCzk"],
                0,
                2_147_483_647,
            ),
            currency="CZK",
        )
    if (offer_count == 0) != (latest_offer is None):
        raise ConfigurationFailure()
    return ActionSummary(
        intent=intent,
        offer_count=offer_count,
        latest_offer=latest_offer,
    )


def _parse_notification(value: Any) -> Notification:
    base_keys = {
        "id",
        "claimId",
        "eventId",
        "kind",
        "priority",
        "actionSummary",
        "deepLink",
    }
    if not isinstance(value, dict):
        raise InvalidClaimPayload()
    is_canary = value.get("canary") is True
    expected_keys = base_keys | ({"canary"} if is_canary else set())
    if set(value) != expected_keys:
        raise InvalidClaimPayload(is_canary=is_canary)

    notification_id: str | None = None
    claim_id: str | None = None
    try:
        notification_id = _strict_uuid(value["id"])
        claim_id = _strict_uuid(value["claimId"])
    except ConfigurationFailure:
        raise InvalidClaimPayload(is_canary=is_canary) from None

    try:
        event_id = _strict_uuid(value["eventId"])
        kind = value["kind"]
        priority = value["priority"]
        action_summary = _parse_action_summary(value["actionSummary"])
        if is_canary:
            if (
                event_id != notification_id
                or kind != CANARY_KIND
                or priority != "normal"
                or action_summary
                != ActionSummary(intent=None, offer_count=0, latest_offer=None)
            ):
                raise ConfigurationFailure()
            expected_link = CANARY_DEEP_LINK
        else:
            if not isinstance(kind, str) or kind not in EVENT_KINDS:
                raise ConfigurationFailure()
            if not isinstance(priority, str) or priority not in PRIORITIES:
                raise ConfigurationFailure()
            expected_link = _canonical_deep_link(event_id)
        if value["deepLink"] != expected_link:
            raise ConfigurationFailure()
    except ConfigurationFailure:
        raise InvalidClaimPayload(notification_id, claim_id, is_canary) from None

    return Notification(
        notification_id=notification_id,
        claim_id=claim_id,
        event_id=event_id,
        kind=kind,
        priority=priority,
        action_summary=action_summary,
        deep_link=expected_link,
        is_canary=is_canary,
    )


def parse_claim_response(response: HttpResponse) -> Notification | None:
    if response.status != 200:
        raise ConfigurationFailure()
    value = _read_json_response(response)
    if not isinstance(value, dict) or set(value) != {"notification"}:
        raise InvalidClaimPayload()
    notification = value["notification"]
    return None if notification is None else _parse_notification(notification)


def _read_credential(path: Path, maximum_bytes: int = 1024) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ConfigurationFailure() from None
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size > maximum_bytes:
            raise ConfigurationFailure()
        raw = os.read(descriptor, maximum_bytes + 1)
        if len(raw) > maximum_bytes or os.read(descriptor, 1):
            raise ConfigurationFailure()
    finally:
        os.close(descriptor)
    if raw.endswith(b"\r\n"):
        clean = raw[:-2]
    elif raw.endswith(b"\n"):
        clean = raw[:-1]
    else:
        clean = raw
    if not clean or clean.strip() != clean or b"\n" in clean or b"\r" in clean:
        raise ConfigurationFailure()
    return clean


def load_credentials() -> Credentials:
    directory_value = os.environ.get("CREDENTIALS_DIRECTORY", "")
    directory = Path(directory_value)
    if not directory_value or not directory.is_absolute():
        raise ConfigurationFailure()
    try:
        if directory.is_symlink() or not directory.is_dir():
            raise ConfigurationFailure()
    except OSError:
        raise ConfigurationFailure() from None

    try:
        token = _read_credential(directory / "telegram-token").decode("ascii")
        chat_id = _read_credential(directory / "telegram-chat-id").decode("ascii")
        machine_secret = _read_credential(directory / "freio-machine-secret").decode(
            "ascii"
        )
    except UnicodeDecodeError:
        raise ConfigurationFailure() from None

    if not TELEGRAM_TOKEN_PATTERN.fullmatch(token):
        raise ConfigurationFailure()
    if not TELEGRAM_CHAT_PATTERN.fullmatch(chat_id):
        raise ConfigurationFailure()
    if not 32 <= len(machine_secret) <= 512:
        raise ConfigurationFailure()
    if any(ord(character) < 33 or ord(character) > 126 for character in machine_secret):
        raise ConfigurationFailure()
    return Credentials(token, chat_id, machine_secret)


class StateStore:
    def __init__(self, root: Path = STATE_DIRECTORY) -> None:
        requested_root = Path(os.path.abspath(root))
        if requested_root.is_symlink() and not self._is_systemd_state_symlink(
            requested_root
        ):
            raise ConfigurationFailure()
        # Keep the stable /var/lib path inside the DynamicUser mount namespace.
        # /var/lib/private is the host backing store, not the worker API path.
        self.root = requested_root
        self.pending_path = self.root / "pending-finalize-v1.json"
        self.circuit_path = self.root / "circuit-open-v1.json"
        self.heartbeat_path = self.root / "heartbeat-v1.json"
        self.lock_path = self.root / "dispatcher.lock"

    @staticmethod
    def _is_systemd_state_symlink(path: Path) -> bool:
        return (
            path == STATE_DIRECTORY
            and path.is_symlink()
            and Path(os.path.realpath(path)) == PRIVATE_STATE_DIRECTORY
        )

    def ensure(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink():
            if not self._is_systemd_state_symlink(self.root):
                raise ConfigurationFailure()
            details = self.root.stat()
        else:
            details = self.root.lstat()
        if not stat.S_ISDIR(details.st_mode) or details.st_mode & 0o077:
            raise ConfigurationFailure()

    def _fsync_root(self) -> None:
        descriptor = os.open(
            self.root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _atomic_write(self, destination: Path, value: Any) -> None:
        self.ensure()
        temporary = self.root / (
            f".{destination.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        )
        data = _canonical_json(value) + b"\n"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            written = 0
            while written < len(data):
                written += os.write(descriptor, data[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.replace(temporary, destination)
            self._fsync_root()
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.ensure()
        descriptor = os.open(
            self.lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AlreadyRunning() from None
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def save_pending(self, finalization: Finalization) -> None:
        _validate_finalization(finalization)
        if self.pending_path.exists():
            raise ConfigurationFailure()
        self._atomic_write(self.pending_path, finalization.state_payload())

    def replace_pending(self, finalization: Finalization) -> None:
        _validate_finalization(finalization)
        if not self.pending_path.exists() or self.pending_path.is_symlink():
            raise ConfigurationFailure()
        self._atomic_write(self.pending_path, finalization.state_payload())

    def load_pending(self) -> Finalization | None:
        self.ensure()
        if not self.pending_path.exists():
            return None
        if self.pending_path.is_symlink():
            raise ConfigurationFailure()
        try:
            raw = self.pending_path.read_bytes()
        except OSError:
            raise ConfigurationFailure() from None
        if not raw or len(raw) > MAX_RESPONSE_BYTES:
            raise ConfigurationFailure()
        value = _decode_json(raw)
        return Finalization.from_state_payload(value)

    def clear_pending(self) -> None:
        try:
            self.pending_path.unlink()
        except FileNotFoundError:
            return
        self._fsync_root()

    def circuit_open(self) -> bool:
        self.ensure()
        if not self.circuit_path.exists():
            return False
        if self.circuit_path.is_symlink() or not self.circuit_path.is_file():
            raise ConfigurationFailure()
        return True

    def open_circuit(self, reason: str) -> None:
        if not ERROR_CODE_PATTERN.fullmatch(reason):
            raise ConfigurationFailure()
        if self.circuit_open():
            return
        opened_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._atomic_write(
            self.circuit_path,
            {"version": 1, "reason": reason, "openedAt": opened_at},
        )

    def save_heartbeat(
        self,
        status: str,
        recorded_at: datetime | None = None,
    ) -> None:
        if status not in HEARTBEAT_STATUSES:
            raise ConfigurationFailure()
        timestamp = recorded_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            raise ConfigurationFailure()
        normalized = timestamp.astimezone(timezone.utc).isoformat(timespec="seconds")
        if not normalized.endswith("+00:00"):
            raise ConfigurationFailure()
        self._atomic_write(
            self.heartbeat_path,
            {
                "version": 1,
                "status": status,
                "recordedAt": normalized.removesuffix("+00:00") + "Z",
            },
        )


class FreioClient:
    def __init__(
        self,
        transport: Transport,
        machine_secret: str,
        *,
        canary: bool = False,
    ) -> None:
        self.transport = transport
        self.canary = canary
        self.headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {machine_secret}",
            "Cache-Control": "no-store",
            "User-Agent": "freio-telegram-handoff/1.0",
        }

    def claim(self) -> HttpResponse:
        # The machine route uses the same bounded JSON boundary as finalize.
        # Send the one canonical representation of its strict empty object
        # schema; a zero-byte POST is correctly rejected as unsupported input.
        body = _canonical_json({"canary": True} if self.canary else {})
        return self.transport.request(
            "POST",
            CLAIM_URL,
            {
                **self.headers,
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
            body,
            API_TIMEOUT_SECONDS,
        )

    def finalize(self, finalization: Finalization) -> HttpResponse:
        body = _canonical_json(finalization.api_payload())
        return self.transport.request(
            "POST",
            FINALIZE_URL,
            {
                **self.headers,
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
            body,
            API_TIMEOUT_SECONDS,
        )


def _format_czk(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def build_telegram_text(notification: Notification) -> str:
    if notification.is_canary:
        return "\n".join(
            [
                MESSAGE_HEADING,
                "Akce: Ověřit interní Telegram handoff.",
                "Priorita: běžná",
                "Kontext: systémový canary bez zákaznických dat.",
                f"Freio přehled: {notification.deep_link}",
            ]
        )
    lines = [
        MESSAGE_HEADING,
        f"Akce: {ACTION_LABELS[notification.kind]}",
        f"Priorita: {PRIORITY_LABELS[notification.priority]}",
    ]
    if notification.action_summary.intent is not None:
        lines.append(f"Potřeba: {INTENT_LABELS[notification.action_summary.intent]}")
    context = f"nabídky: {notification.action_summary.offer_count}"
    if notification.action_summary.latest_offer is not None:
        context += (
            " | poslední cena: "
            f"{_format_czk(notification.action_summary.latest_offer.total_czk)} Kč"
        )
    lines.extend(
        [
            f"Kontext: {context}",
            f"Celé vlákno: {notification.deep_link}",
        ]
    )
    return "\n".join(lines)


class TelegramClient:
    def __init__(
        self,
        transport: Transport,
        token: str,
        chat_id: str,
    ) -> None:
        self.transport = transport
        self.url = f"{TELEGRAM_API_ORIGIN}/bot{token}/sendMessage"
        self.chat_id = chat_id

    def send(self, notification: Notification) -> HttpResponse:
        text = build_telegram_text(notification)
        body = urllib.parse.urlencode(
            {
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": "true",
                "protect_content": "true",
            }
        ).encode("ascii")
        return self.transport.request(
            "POST",
            self.url,
            {
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len(body)),
                "User-Agent": "freio-telegram-handoff/1.0",
            },
            body,
            HTTP_TIMEOUT_SECONDS,
        )


def _bounded_retry(value: int | None, default: int) -> int:
    candidate = (
        value if isinstance(value, int) and not isinstance(value, bool) else default
    )
    return min(MAX_RETRY_SECONDS, max(MIN_RETRY_SECONDS, candidate))


def _telegram_rate_limit_retry_after(response: HttpResponse) -> int | None:
    """Return a retry delay only for Telegram's authenticated 429 envelope.

    A bare/intermediary 429 is not enough to prove that sendMessage had no side
    effect. The direct TLS peer must return Telegram's JSON error contract.
    """
    try:
        value = _read_json_response(response)
    except ConfigurationFailure:
        return None
    if (
        not isinstance(value, dict)
        or value.get("ok") is not False
        or value.get("error_code") != 429
    ):
        return None
    parameters = value.get("parameters")
    if parameters is not None and not isinstance(parameters, dict):
        return None
    body_retry: int | None = None
    if isinstance(parameters, dict) and "retry_after" in parameters:
        candidate = parameters["retry_after"]
        if not isinstance(candidate, int) or isinstance(candidate, bool):
            return None
        body_retry = candidate
    header_value = response.headers.get("retry-after", "")
    header_retry = int(header_value) if header_value.isdigit() else None
    return _bounded_retry(
        body_retry if body_retry is not None else header_retry,
        DEFAULT_RETRY_SECONDS,
    )


def _sent_provider_id(response: HttpResponse) -> str:
    if response.status != 200 or response.truncated:
        raise ConfigurationFailure()
    value = _read_json_response(response)
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise ConfigurationFailure()
    result = value.get("result")
    if not isinstance(result, dict):
        raise ConfigurationFailure()
    message_id = result.get("message_id")
    if (
        not isinstance(message_id, int)
        or isinstance(message_id, bool)
        or message_id <= 0
    ):
        raise ConfigurationFailure()
    return str(message_id)


def _telegram_http_finalization(
    notification: Notification,
    response: HttpResponse,
) -> Finalization:
    def build(
        *,
        outcome: str,
        provider_message_id: str | None = None,
        error_code: str | None = None,
        retry_after_seconds: int | None = None,
        open_circuit: bool = False,
    ) -> Finalization:
        return Finalization(
            notification_id=notification.notification_id,
            claim_id=notification.claim_id,
            outcome=outcome,
            provider_message_id=provider_message_id,
            error_code=error_code,
            retry_after_seconds=retry_after_seconds,
            open_circuit=open_circuit,
            is_canary=notification.is_canary,
        )

    if response.status == 200:
        try:
            provider_id = _sent_provider_id(response)
        except ConfigurationFailure:
            return build(
                outcome="uncertain",
                error_code="telegram_invalid_success_response",
                open_circuit=True,
            )
        return build(
            outcome="sent",
            provider_message_id=provider_id,
        )
    if response.status == 429:
        retry_after = _telegram_rate_limit_retry_after(response)
        if retry_after is None:
            return build(
                outcome="uncertain",
                error_code="telegram_invalid_rate_limit_response",
                open_circuit=True,
            )
        return build(
            outcome="retry",
            error_code="telegram_http_429",
            retry_after_seconds=retry_after,
        )
    if response.status in {408, 425} or 500 <= response.status <= 599:
        return build(
            outcome="uncertain",
            error_code=f"telegram_http_{response.status}_ambiguous",
            open_circuit=True,
        )
    if 300 <= response.status <= 499:
        return build(
            outcome="dead",
            error_code=f"telegram_http_{response.status}",
            open_circuit=True,
        )
    return build(
        outcome="uncertain",
        error_code="telegram_unexpected_status",
        open_circuit=True,
    )


def _log(event: str, **fields: str | int | bool) -> None:
    # Callers pass only internal enums/counts. Never pass response bodies,
    # URLs, identifiers, credentials or exception strings here.
    payload: dict[str, str | int | bool] = {"event": event}
    payload.update(fields)
    sys.stdout.write(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")
    sys.stdout.flush()


class Dispatcher:
    def __init__(
        self,
        state: StateStore,
        freio: FreioClient,
        telegram: TelegramClient,
        *,
        canary_mode: bool = False,
    ) -> None:
        self.state = state
        self.freio = freio
        self.telegram = telegram
        self.canary_mode = canary_mode
        self.last_run_status = "error"

    def _finish(self, status: str, exit_code: int) -> int:
        if status not in HEARTBEAT_STATUSES:
            raise ConfigurationFailure()
        self.last_run_status = status
        return exit_code

    @staticmethod
    def _finalization_status(finalization: Finalization) -> str:
        if finalization.outcome == "sent":
            return "sent"
        if finalization.outcome == "retry":
            return "retry"
        return "error"

    def _finalize_pending(self, pending: Finalization) -> bool:
        try:
            response = self.freio.finalize(pending)
        except (NetworkUnavailable, AmbiguousTransport):
            return False
        except TlsValidationFailure:
            self.state.open_circuit("freio_finalize_tls_failure")
            return False

        if response.status != 200 or response.truncated:
            if (
                response.status in {400, 401, 403, 404, 405}
                or 300 <= response.status < 400
            ):
                self.state.open_circuit(f"freio_finalize_http_{response.status}")
            return False
        try:
            value = _read_json_response(response)
        except ConfigurationFailure:
            self.state.open_circuit("freio_finalize_invalid_response")
            return False
        if not isinstance(value, dict) or value.get("success") is not True:
            self.state.open_circuit("freio_finalize_rejected")
            return False

        if pending.open_circuit:
            self.state.open_circuit(pending.error_code or "delivery_protocol_failure")
        self.state.clear_pending()
        return True

    def _persist_and_finalize(self, finalization: Finalization) -> int:
        self.state.save_pending(finalization)
        return self._finalize_saved(finalization)

    def _replace_and_finalize(self, finalization: Finalization) -> int:
        self.state.replace_pending(finalization)
        return self._finalize_saved(finalization)

    def _finalize_saved(self, finalization: Finalization) -> int:
        if not self._finalize_pending(finalization):
            if self.state.circuit_open():
                _log("finalize_circuit_open")
                return self._finish("error", 78)
            _log("finalize_deferred")
            return self._finish("retry", 1)
        if finalization.open_circuit:
            _log("delivery_circuit_open", outcome=finalization.outcome)
            return self._finish("error", 78)
        _log("delivery_finalized", outcome=finalization.outcome)
        return self._finish(self._finalization_status(finalization), 0)

    def _invalid_claim(self, error: InvalidClaimPayload) -> int:
        if error.is_canary != self.canary_mode:
            self.state.open_circuit("freio_claim_mode_mismatch")
            _log("claim_circuit_open")
            return self._finish("error", 78)
        if error.notification_id and error.claim_id:
            return self._persist_and_finalize(
                Finalization(
                    notification_id=error.notification_id,
                    claim_id=error.claim_id,
                    outcome="dead",
                    error_code="freio_invalid_claim_payload",
                    open_circuit=True,
                    is_canary=error.is_canary,
                )
            )
        self.state.open_circuit("freio_invalid_claim_envelope")
        _log("claim_circuit_open")
        return self._finish("error", 78)

    def run(self) -> int:
        if self.state.circuit_open():
            _log("circuit_open")
            return self._finish("error", 78)

        pending = self.state.load_pending()
        if pending is not None:
            if pending.is_canary != self.canary_mode:
                self.state.open_circuit("freio_pending_mode_mismatch")
                _log("finalize_recovery_circuit_open")
                return self._finish("error", 78)
            if not self._finalize_pending(pending):
                if self.state.circuit_open():
                    _log("finalize_recovery_circuit_open")
                    return self._finish("error", 78)
                _log("finalize_recovery_deferred")
                return self._finish("retry", 1)
            if pending.open_circuit:
                _log("finalize_recovered_circuit_open")
                return self._finish("error", 78)
            _log("finalize_recovered")
            return self._finish(self._finalization_status(pending), 0)

        try:
            response = self.freio.claim()
        except (NetworkUnavailable, AmbiguousTransport):
            _log("claim_deferred")
            return self._finish("retry", 1)
        except TlsValidationFailure:
            self.state.open_circuit("freio_claim_tls_failure")
            _log("claim_circuit_open")
            return self._finish("error", 78)

        if response.status != 200:
            if response.status in {408, 425, 429} or 500 <= response.status <= 599:
                _log("claim_deferred", status=response.status)
                return self._finish("retry", 1)
            self.state.open_circuit(f"freio_claim_http_{response.status}")
            _log("claim_circuit_open", status=response.status)
            return self._finish("error", 78)

        try:
            notification = parse_claim_response(response)
        except InvalidClaimPayload as error:
            return self._invalid_claim(error)
        except ConfigurationFailure:
            self.state.open_circuit("freio_claim_invalid_response")
            _log("claim_circuit_open")
            return self._finish("error", 78)

        if notification is None and self.canary_mode:
            self.state.open_circuit("freio_canary_claim_missing")
            _log("claim_circuit_open")
            return self._finish("error", 78)
        if notification is None:
            _log("idle")
            return self._finish("idle", 0)
        if notification.is_canary != self.canary_mode:
            self.state.open_circuit("freio_claim_mode_mismatch")
            _log("claim_circuit_open")
            return self._finish("error", 78)

        # Persist a conservative terminal decision before entering the network
        # call. If the process dies anywhere around sendMessage, recovery can
        # finalize this claim without ever issuing a second Telegram request.
        self.state.save_pending(
            Finalization(
                notification_id=notification.notification_id,
                claim_id=notification.claim_id,
                outcome="uncertain",
                error_code="telegram_attempt_interrupted",
                open_circuit=True,
                is_canary=notification.is_canary,
            )
        )

        try:
            telegram_response = self.telegram.send(notification)
        except RequestTimedOut:
            return self._replace_and_finalize(
                Finalization(
                    notification_id=notification.notification_id,
                    claim_id=notification.claim_id,
                    outcome="uncertain",
                    error_code="telegram_timeout_ambiguous",
                    open_circuit=True,
                    is_canary=notification.is_canary,
                )
            )
        except AmbiguousTransport:
            return self._replace_and_finalize(
                Finalization(
                    notification_id=notification.notification_id,
                    claim_id=notification.claim_id,
                    outcome="uncertain",
                    error_code="telegram_transport_ambiguous",
                    open_circuit=True,
                    is_canary=notification.is_canary,
                )
            )
        except NetworkUnavailable:
            return self._replace_and_finalize(
                Finalization(
                    notification_id=notification.notification_id,
                    claim_id=notification.claim_id,
                    outcome="retry",
                    error_code="telegram_network_unavailable",
                    retry_after_seconds=NETWORK_RETRY_SECONDS,
                    is_canary=notification.is_canary,
                )
            )
        except TlsValidationFailure:
            return self._replace_and_finalize(
                Finalization(
                    notification_id=notification.notification_id,
                    claim_id=notification.claim_id,
                    outcome="dead",
                    error_code="telegram_tls_failure",
                    open_circuit=True,
                    is_canary=notification.is_canary,
                )
            )
        except Exception:
            return self._replace_and_finalize(
                Finalization(
                    notification_id=notification.notification_id,
                    claim_id=notification.claim_id,
                    outcome="uncertain",
                    error_code="telegram_internal_ambiguous",
                    open_circuit=True,
                    is_canary=notification.is_canary,
                )
            )

        return self._replace_and_finalize(
            _telegram_http_finalization(notification, telegram_response)
        )


def run_and_record_heartbeat(dispatcher: Dispatcher) -> int:
    result = dispatcher.run()
    dispatcher.state.save_heartbeat(dispatcher.last_run_status)
    return result


def _best_effort_error_heartbeat(state: StateStore) -> None:
    try:
        state.save_heartbeat("error")
    except Exception:
        # The only caller is already handling a failure. Never shadow its safe
        # generic log event with a second exception or expose a filesystem path.
        pass


def _parse_canary_mode(arguments: list[str]) -> bool:
    if not arguments:
        return False
    if arguments == ["--canary"]:
        return True
    raise ConfigurationFailure()


def main(arguments: list[str] | None = None) -> int:
    try:
        canary = _parse_canary_mode(sys.argv[1:] if arguments is None else arguments)
        state = StateStore()
        with state.lock():
            try:
                credentials = load_credentials()
                transport = UrllibTransport()
                dispatcher = Dispatcher(
                    state=state,
                    freio=FreioClient(
                        transport,
                        credentials.freio_machine_secret,
                        canary=canary,
                    ),
                    telegram=TelegramClient(
                        transport,
                        credentials.telegram_token,
                        credentials.telegram_chat_id,
                    ),
                    canary_mode=canary,
                )
                return run_and_record_heartbeat(dispatcher)
            except ConfigurationFailure:
                _best_effort_error_heartbeat(state)
                raise
            except Exception:
                _best_effort_error_heartbeat(state)
                raise
    except AlreadyRunning:
        _log("already_running")
        return 0
    except ConfigurationFailure:
        _log("configuration_failure")
        return 78
    except Exception:
        # Never serialize an exception: urllib errors may contain a URL with
        # the Telegram bot token or an HTTP response body with private data.
        _log("internal_failure")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
