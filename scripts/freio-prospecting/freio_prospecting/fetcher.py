from __future__ import annotations

import http.client
import ipaddress
import queue
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
from typing import Callable, Mapping, Protocol, Sequence
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from .common import CONTROL_CHARACTERS, sha256_hex, utc_now_iso


MAX_BODY_BYTES = 1024 * 1024
FETCH_TIMEOUT_SECONDS = 10.0
MAX_REDIRECTS = 3
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
ALLOWED_MEDIA_TYPES = frozenset({"text/html", "text/plain"})
CRITICAL_HEADERS = frozenset(
    {
        "content-type",
        "content-length",
        "content-encoding",
        "transfer-encoding",
        "location",
    }
)
FETCHER_VERSION = "1.1.0"
NETWORK_POLICY_VERSION = "public-https-pinned-v1"
BLOCKED_HOST_SUFFIXES = (
    "localhost",
    "local",
    "internal",
    "home",
    "lan",
    "test",
    "example",
    "invalid",
)
EMAIL_PATTERN = re.compile(
    r"(?<![A-Z0-9.!#$%&'*+/=?^_`{|}~-])"
    r"([A-Z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@"
    r"(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,63})"
    r"(?![A-Z0-9.!#$%&'*+/=?^_`{|}~-])",
    re.IGNORECASE,
)


class FetchError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class NormalizedURL:
    url: str
    hostname: str
    port: int
    target: str


@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    duplicate_critical_headers: tuple[str, ...] = ()


@dataclass(frozen=True)
class EmailEvidence:
    email: str
    kinds: tuple[str, ...]


@dataclass(frozen=True)
class FetchedDocument:
    requested_url: str
    final_url: str
    fetched_at: str
    status: int
    media_type: str
    charset: str
    body: bytes
    body_sha256: str
    redirect_count: int
    emails: tuple[EmailEvidence, ...]


class Resolver(Protocol):
    def __call__(self, hostname: str, port: int) -> Sequence[str]: ...


class Transport(Protocol):
    def __call__(
        self,
        url: NormalizedURL,
        addresses: Sequence[str],
        timeout: float,
        maximum_body_bytes: int,
    ) -> RawResponse: ...


def normalize_public_https_url(value: str) -> NormalizedURL:
    if (
        not isinstance(value, str)
        or len(value) < 12
        or len(value) > 2000
        or CONTROL_CHARACTERS.search(value)
        or "\\" in value
    ):
        raise FetchError("invalid_url", "source URL is malformed")
    try:
        parsed = urlsplit(value.strip())
        hostname_unicode = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise FetchError("invalid_url", "source URL is malformed") from exc
    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or not hostname_unicode
        or parsed.fragment
        or port not in (None, 443)
    ):
        raise FetchError(
            "invalid_url", "only credential-free HTTPS URLs on port 443 are allowed"
        )
    try:
        hostname = hostname_unicode.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise FetchError(
            "invalid_host", "hostname cannot be represented safely"
        ) from exc
    if (
        not hostname
        or len(hostname) > 253
        or "." not in hostname
        or any(
            hostname == suffix or hostname.endswith(f".{suffix}")
            for suffix in BLOCKED_HOST_SUFFIXES
        )
    ):
        raise FetchError("invalid_host", "hostname is local or otherwise disallowed")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise FetchError("direct_ip", "direct IP source URLs are disallowed")
    labels = hostname.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or re.fullmatch(r"[a-z0-9-]+", label) is None
        for label in labels
    ):
        raise FetchError("invalid_host", "hostname labels are invalid")
    path = parsed.path or "/"
    target = path + (f"?{parsed.query}" if parsed.query else "")
    normalized = urlunsplit(("https", hostname, path, parsed.query, ""))
    return NormalizedURL(normalized, hostname, 443, target)


def system_resolver(hostname: str, port: int) -> tuple[str, ...]:
    try:
        answers = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise FetchError("dns_failure", "source hostname did not resolve") from exc
    addresses = {answer[4][0].split("%", 1)[0] for answer in answers}
    if not addresses:
        raise FetchError("dns_empty", "source hostname had no A or AAAA answer")
    parsed_addresses = []
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise FetchError(
                "dns_invalid", "resolver returned an invalid address"
            ) from exc
        if not _is_public_unicast(parsed):
            raise FetchError(
                "non_public_ip", "source hostname resolves to a non-public address"
            )
        parsed_addresses.append(parsed)
    # Prefer IPv4 for homelab reachability, then use a deterministic numeric order.
    return tuple(
        str(item)
        for item in sorted(parsed_addresses, key=lambda item: (item.version, int(item)))
    )


def validate_resolved_addresses(addresses: Sequence[str]) -> tuple[str, ...]:
    if not addresses:
        raise FetchError("dns_empty", "source hostname had no A or AAAA answer")
    normalized: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError as exc:
            raise FetchError(
                "dns_invalid", "resolver returned an invalid address"
            ) from exc
        if not _is_public_unicast(parsed):
            raise FetchError(
                "non_public_ip", "source hostname resolves to a non-public address"
            )
        normalized.add(parsed)
    return tuple(
        str(item)
        for item in sorted(normalized, key=lambda item: (item.version, int(item)))
    )


def _is_public_unicast(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    return (
        address.is_global
        and not address.is_multicast
        and not address.is_unspecified
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_private
        and not address.is_reserved
    )


def _pinned_https_request(
    url: NormalizedURL,
    addresses: Sequence[str],
    timeout: float,
    maximum_body_bytes: int,
) -> RawResponse:
    last_error: BaseException | None = None
    deadline = time.monotonic() + timeout
    for address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        connection = http.client.HTTPSConnection(
            url.hostname,
            port=url.port,
            timeout=remaining,
            context=ssl.create_default_context(),
        )

        def pinned_create_connection(
            _address: tuple[str, int],
            timeout: float | None = None,
            source_address: tuple[str, int] | None = None,
        ) -> socket.socket:
            return socket.create_connection(
                (address, url.port), timeout, source_address
            )

        connection._create_connection = pinned_create_connection  # type: ignore[attr-defined]
        try:
            connection.request(
                "GET",
                url.target,
                headers={
                    "Accept": "text/html,text/plain;q=0.9",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                    "User-Agent": "FreioProspectingEvidence/1.0 (+https://freio.cz)",
                },
            )
            response = connection.getresponse()
            header_items = [
                (key.lower(), value.strip()) for key, value in response.getheaders()
            ]
            counts: dict[str, int] = {}
            for key, _value in header_items:
                if key in CRITICAL_HEADERS:
                    counts[key] = counts.get(key, 0) + 1
            duplicates = tuple(
                sorted(key for key, count in counts.items() if count > 1)
            )
            headers = {key: value for key, value in header_items}
            if response.status in REDIRECT_STATUSES:
                return RawResponse(response.status, headers, b"", duplicates)
            encoding = headers.get("content-encoding", "identity").lower()
            if encoding not in ("", "identity"):
                raise FetchError(
                    "encoded_body", "compressed source responses are not accepted"
                )
            content_length = headers.get("content-length")
            if content_length:
                try:
                    declared = int(content_length)
                except ValueError as exc:
                    raise FetchError(
                        "invalid_length", "source returned an invalid Content-Length"
                    ) from exc
                if declared < 0 or declared > maximum_body_bytes:
                    raise FetchError(
                        "body_too_large", "source body exceeds the configured limit"
                    )
            body = response.read(maximum_body_bytes + 1)
            if len(body) > maximum_body_bytes:
                raise FetchError(
                    "body_too_large", "source body exceeds the configured limit"
                )
            return RawResponse(response.status, headers, body, duplicates)
        except FetchError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            last_error = exc
        finally:
            connection.close()
    raise FetchError(
        "network_failure", "could not connect to any validated source address"
    ) from last_error


def _parse_content_type(value: str | None) -> tuple[str, str]:
    if not value:
        raise FetchError("missing_content_type", "source did not provide Content-Type")
    message = Message()
    message["content-type"] = value
    media_type = message.get_content_type().lower()
    if media_type not in ALLOWED_MEDIA_TYPES:
        raise FetchError("invalid_content_type", "source is not HTML or plain text")
    charset = message.get_content_charset() or "utf-8"
    return media_type, charset.lower()


def _valid_email(candidate: str) -> str | None:
    if len(candidate) > 320 or EMAIL_PATTERN.fullmatch(candidate) is None:
        return None
    local, separator, domain = candidate.rpartition("@")
    if not separator or local.startswith(".") or local.endswith(".") or ".." in local:
        return None
    try:
        normalized_domain = domain.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    if len(normalized_domain) > 253:
        return None
    return f"{local.lower()}@{normalized_domain}"


def extract_text_emails(text: str) -> set[str]:
    found: set[str] = set()
    for match in EMAIL_PATTERN.finditer(text):
        normalized = _valid_email(match.group(1))
        if normalized:
            found.add(normalized)
    return found


class _EvidenceHTMLParser(HTMLParser):
    _HIDDEN_TEXT_ELEMENTS = frozenset(
        {"script", "style", "noscript", "template", "svg"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden_depth = 0
        self.visible_parts: list[str] = []
        self.mailto_emails: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered in self._HIDDEN_TEXT_ELEMENTS:
            self.hidden_depth += 1
        if lowered != "a":
            return
        href = next((value for key, value in attrs if key.lower() == "href"), None)
        if not href or not href.lower().startswith("mailto:"):
            return
        address = unquote(href[7:].split("?", 1)[0]).strip()
        normalized = _valid_email(address)
        if normalized:
            self.mailto_emails.add(normalized)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() in self._HIDDEN_TEXT_ELEMENTS and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._HIDDEN_TEXT_ELEMENTS and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.hidden_depth == 0:
            self.visible_parts.append(data)


def extract_email_evidence(text: str, media_type: str) -> tuple[EmailEvidence, ...]:
    evidence: dict[str, set[str]] = {}
    if media_type == "text/html":
        parser = _EvidenceHTMLParser()
        try:
            parser.feed(text)
            parser.close()
        except (AssertionError, ValueError) as exc:
            raise FetchError(
                "invalid_html", "source HTML could not be parsed safely"
            ) from exc
        for email in extract_text_emails(" ".join(parser.visible_parts)):
            evidence.setdefault(email, set()).add("visible_text")
        for email in parser.mailto_emails:
            evidence.setdefault(email, set()).add("mailto")
    else:
        for email in extract_text_emails(text):
            evidence.setdefault(email, set()).add("visible_text")
    return tuple(
        EmailEvidence(email, tuple(sorted(kinds)))
        for email, kinds in sorted(evidence.items())
    )


class PublicHTTPSFetcher:
    """Fetch public evidence with DNS pinning and fail-closed redirect checks."""

    def __init__(
        self,
        *,
        resolver: Resolver = system_resolver,
        transport: Transport = _pinned_https_request,
        timeout_seconds: float = FETCH_TIMEOUT_SECONDS,
        maximum_body_bytes: int = MAX_BODY_BYTES,
        maximum_redirects: int = MAX_REDIRECTS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout_seconds <= 0 or maximum_body_bytes <= 0 or maximum_redirects < 0:
            raise ValueError("fetch limits must be positive")
        self._resolver = resolver
        self._transport = transport
        self._timeout = timeout_seconds
        self._maximum_body = maximum_body_bytes
        self._maximum_redirects = maximum_redirects
        self._clock = clock

    def fetch(self, source_url: str) -> FetchedDocument:
        requested = normalize_public_https_url(source_url).url
        current = requested
        visited: set[str] = set()
        deadline = self._clock() + self._timeout
        for redirect_count in range(self._maximum_redirects + 1):
            normalized = normalize_public_https_url(current)
            if normalized.url in visited:
                raise FetchError("redirect_loop", "source redirect loop detected")
            visited.add(normalized.url)
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise FetchError("timeout", "source fetch exceeded its deadline")
            addresses = validate_resolved_addresses(
                self._resolve_before_deadline(
                    normalized.hostname, normalized.port, remaining
                )
            )
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise FetchError("timeout", "source fetch exceeded its deadline")
            response = self._transport(
                normalized, addresses, remaining, self._maximum_body
            )
            headers = {
                str(key).lower(): str(value).strip()
                for key, value in response.headers.items()
            }
            if response.duplicate_critical_headers:
                raise FetchError(
                    "duplicate_header",
                    "source returned duplicate security-critical headers",
                )
            if "content-length" in headers and "transfer-encoding" in headers:
                raise FetchError(
                    "ambiguous_length",
                    "source returned both Content-Length and Transfer-Encoding",
                )
            transfer_encoding = headers.get("transfer-encoding", "").lower()
            if transfer_encoding not in ("", "chunked"):
                raise FetchError(
                    "invalid_transfer_encoding",
                    "source returned an unsupported Transfer-Encoding",
                )
            content_encoding = headers.get("content-encoding", "identity").lower()
            if content_encoding not in ("", "identity"):
                raise FetchError(
                    "encoded_body", "compressed source responses are not accepted"
                )
            if response.status in REDIRECT_STATUSES:
                if redirect_count >= self._maximum_redirects:
                    raise FetchError(
                        "too_many_redirects", "source exceeded the redirect limit"
                    )
                location = headers.get("location")
                if (
                    not location
                    or CONTROL_CHARACTERS.search(location)
                    or len(location) > 2000
                ):
                    raise FetchError(
                        "invalid_redirect", "source returned an invalid redirect"
                    )
                current = normalize_public_https_url(
                    urljoin(normalized.url, location)
                ).url
                continue
            if response.status != 200:
                raise FetchError(
                    "http_status", f"source returned HTTP {response.status}"
                )
            declared_length = headers.get("content-length")
            if declared_length:
                try:
                    declared = int(declared_length)
                except ValueError as exc:
                    raise FetchError(
                        "invalid_length", "source returned an invalid Content-Length"
                    ) from exc
                if (
                    declared < 0
                    or declared > self._maximum_body
                    or declared != len(response.body)
                ):
                    raise FetchError(
                        "invalid_length",
                        "source Content-Length is unsafe or does not match",
                    )
            if len(response.body) > self._maximum_body:
                raise FetchError(
                    "body_too_large", "source body exceeds the configured limit"
                )
            media_type, charset = _parse_content_type(headers.get("content-type"))
            try:
                text = response.body.decode(charset, errors="replace")
            except LookupError as exc:
                raise FetchError(
                    "invalid_charset", "source declared an unknown charset"
                ) from exc
            return FetchedDocument(
                requested_url=requested,
                final_url=normalized.url,
                fetched_at=utc_now_iso(),
                status=response.status,
                media_type=media_type,
                charset=charset,
                body=response.body,
                body_sha256=sha256_hex(response.body),
                redirect_count=redirect_count,
                emails=extract_email_evidence(text, media_type),
            )
        raise FetchError("too_many_redirects", "source exceeded the redirect limit")

    def _resolve_before_deadline(
        self, hostname: str, port: int, timeout: float
    ) -> Sequence[str]:
        result: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

        def resolve() -> None:
            try:
                value: object = self._resolver(hostname, port)
            except BaseException as exc:  # propagated on the caller thread below
                result.put((False, exc))
            else:
                result.put((True, value))

        thread = threading.Thread(target=resolve, name="freio-public-dns", daemon=True)
        thread.start()
        try:
            success, value = result.get(timeout=timeout)
        except queue.Empty as exc:
            raise FetchError(
                "timeout", "source DNS lookup exceeded its deadline"
            ) from exc
        if not success:
            if isinstance(value, FetchError):
                raise value
            cause = value if isinstance(value, BaseException) else None
            raise FetchError(
                "dns_failure", "source hostname did not resolve"
            ) from cause
        if not isinstance(value, Sequence):
            raise FetchError("dns_invalid", "resolver returned an invalid result")
        return value
