from __future__ import annotations

import http.client
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .contract import (
    Task,
    ValidationError,
    canonical_json_bytes,
    parse_claim_response,
    parse_strict_json,
    validate_complete_payload,
)


CLAIM_URL = "https://outreach.freio.cz/api/internal/b2b-agent/tasks/claim"
COMPLETE_URL = "https://outreach.freio.cz/api/internal/b2b-agent/tasks/complete"
API_TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 64 * 1024


class ApiFailure(RuntimeError):
    """A fixed-code API/transport failure safe to report without response data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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
    """TLS-verified HTTPS with environment proxies and redirects disabled."""

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
    def _read(stream: Any) -> tuple[bytes, bool]:
        body = stream.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            return body[:MAX_RESPONSE_BYTES], True
        return body, False

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
                response_body, truncated = self._read(response)
                return HttpResponse(
                    status=int(response.status),
                    headers=self._headers(response.headers),
                    body=response_body,
                    truncated=truncated,
                )
        except urllib.error.HTTPError as error:
            response_body, truncated = self._read(error)
            return HttpResponse(
                status=int(error.code),
                headers=self._headers(error.headers),
                body=response_body,
                truncated=truncated,
            )
        except urllib.error.URLError as error:
            reason = error.reason
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise ApiFailure("tls_validation_failed") from None
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise ApiFailure("request_timeout_ambiguous") from None
            if isinstance(
                reason,
                (socket.gaierror, ConnectionRefusedError, ConnectionAbortedError),
            ):
                raise ApiFailure("network_unavailable") from None
            raise ApiFailure("transport_ambiguous") from None
        except ssl.SSLCertVerificationError:
            raise ApiFailure("tls_validation_failed") from None
        except (socket.timeout, TimeoutError):
            raise ApiFailure("request_timeout_ambiguous") from None
        except (
            http.client.IncompleteRead,
            http.client.RemoteDisconnected,
            ConnectionResetError,
            BrokenPipeError,
            ssl.SSLError,
        ):
            raise ApiFailure("transport_ambiguous") from None
        except OSError:
            raise ApiFailure("network_unavailable") from None


def _is_json_response(response: HttpResponse) -> bool:
    content_type = response.headers.get("content-type", "")
    return content_type.split(";", 1)[0].strip().lower() == "application/json"


class FreioAgentClient:
    def __init__(
        self,
        *,
        bearer_secret: str,
        transport: Transport | None = None,
        timeout_seconds: int = API_TIMEOUT_SECONDS,
    ) -> None:
        if not 3 <= timeout_seconds <= API_TIMEOUT_SECONDS:
            raise ValidationError("API timeout must be between 3 and 15 seconds")
        self.bearer_secret = bearer_secret
        self.transport = transport or UrllibTransport()
        self.timeout_seconds = timeout_seconds

    def _post(self, url: str, body: bytes) -> HttpResponse:
        return self.transport.request(
            "POST",
            url,
            {
                "Accept": "application/json",
                "Authorization": f"Bearer {self.bearer_secret}",
                "Content-Type": "application/json",
                "User-Agent": "freio-b2b-classifier/1",
            },
            body,
            self.timeout_seconds,
        )

    def claim(self) -> Task | None:
        response = self._post(CLAIM_URL, b"{}")
        if response.truncated:
            raise ApiFailure("claim_response_too_large")
        if response.status != 200:
            raise ApiFailure(f"claim_http_{response.status}")
        if not _is_json_response(response):
            raise ApiFailure("claim_content_type_invalid")
        try:
            return parse_claim_response(response.body)
        except ValidationError as exc:
            raise ApiFailure("claim_response_invalid") from exc

    def complete(self, payload: dict[str, Any]) -> None:
        exact_payload = validate_complete_payload(payload)
        response = self._post(COMPLETE_URL, canonical_json_bytes(exact_payload))
        if response.truncated:
            raise ApiFailure("complete_response_too_large")
        if response.status != 200:
            raise ApiFailure(f"complete_http_{response.status}")
        if not _is_json_response(response):
            raise ApiFailure("complete_content_type_invalid")
        try:
            value = parse_strict_json(response.body, label="complete response")
        except ValidationError as exc:
            raise ApiFailure("complete_response_invalid") from exc
        if not isinstance(value, dict) or value != {"success": True}:
            raise ApiFailure("complete_response_invalid")
