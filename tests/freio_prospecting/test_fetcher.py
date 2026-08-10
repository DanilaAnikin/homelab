from __future__ import annotations

import unittest
import time
from unittest import mock

from helpers import PACKAGE_ROOT  # noqa: F401

from freio_prospecting.fetcher import (
    FetchError,
    PublicHTTPSFetcher,
    RawResponse,
    _pinned_https_request,
    extract_email_evidence,
    normalize_public_https_url,
    validate_resolved_addresses,
)


class URLValidationTests(unittest.TestCase):
    def test_normalizes_host_port_path_and_fragment_policy(self) -> None:
        value = normalize_public_https_url("https://EXAMPLE.cz:443/contact?q=1")
        self.assertEqual(value.url, "https://example.cz/contact?q=1")
        self.assertEqual(value.target, "/contact?q=1")

    def test_rejects_unsafe_url_forms(self) -> None:
        unsafe = (
            "http://example.cz/",
            "https://user:pass@example.cz/",
            "https://example.cz:8443/",
            "https://127.0.0.1/",
            "https://[::1]/",
            "https://localhost/",
            "https://service.internal/",
            "https://example.cz/path#fragment",
            "https://example.cz\\@evil.cz/",
        )
        for value in unsafe:
            with self.subTest(value=value), self.assertRaises(FetchError):
                normalize_public_https_url(value)

    def test_rejects_any_non_public_dns_answer(self) -> None:
        for addresses in (
            ["127.0.0.1"],
            ["10.0.0.1"],
            ["169.254.1.1"],
            ["192.0.2.4"],
            ["::1"],
            ["fc00::1"],
            ["2001:db8::1"],
            ["224.0.0.1"],
            ["ff02::1"],
            ["93.184.216.34", "10.0.0.1"],
        ):
            with self.subTest(addresses=addresses), self.assertRaises(
                FetchError
            ) as raised:
                validate_resolved_addresses(addresses)
            self.assertEqual(raised.exception.code, "non_public_ip")

    def test_deduplicates_and_deterministically_sorts_public_dns(self) -> None:
        self.assertEqual(
            validate_resolved_addresses(["2606:4700:4700::1111", "1.1.1.1", "1.1.1.1"]),
            ("1.1.1.1", "2606:4700:4700::1111"),
        )


class EvidenceExtractionTests(unittest.TestCase):
    def test_extracts_only_visible_text_and_mailto(self) -> None:
        html = """
        <html><script>hidden@evil.cz</script><style>.x{content:'css@evil.cz'}</style>
        <body>Kontakt: Visible@Example.cz
        <a href="mailto:Sales%40Example.cz?subject=Hello">napište nám</a></body></html>
        """
        found = {
            entry.email: entry.kinds
            for entry in extract_email_evidence(html, "text/html")
        }
        self.assertEqual(found["visible@example.cz"], ("visible_text",))
        self.assertEqual(found["sales@example.cz"], ("mailto",))
        self.assertNotIn("hidden@evil.cz", found)
        self.assertNotIn("css@evil.cz", found)

    def test_merges_visible_and_mailto_evidence(self) -> None:
        html = '<a href="mailto:hello@example.cz">hello@example.cz</a>'
        self.assertEqual(
            extract_email_evidence(html, "text/html")[0].kinds,
            ("mailto", "visible_text"),
        )

    def test_plain_text_does_not_accept_malformed_email(self) -> None:
        found = extract_email_evidence(
            ".bad@example.cz good@example.cz bad..dots@example.cz a@localhost",
            "text/plain",
        )
        self.assertEqual(tuple(entry.email for entry in found), ("good@example.cz",))


class FetchFlowTests(unittest.TestCase):
    def test_redirect_is_revalidated_and_final_page_extracted(self) -> None:
        calls: list[tuple[str, tuple[str, ...]]] = []

        def resolver(host: str, _port: int) -> tuple[str, ...]:
            return ("93.184.216.34",) if host == "first.example.cz" else ("1.1.1.1",)

        def transport(url, addresses, _timeout, _maximum):
            calls.append((url.url, tuple(addresses)))
            if url.hostname == "first.example.cz":
                return RawResponse(
                    302, {"location": "https://second.example.cz/contact"}, b""
                )
            return RawResponse(
                200,
                {"content-type": "text/html; charset=utf-8"},
                b"Contact final@example.cz",
            )

        result = PublicHTTPSFetcher(resolver=resolver, transport=transport).fetch(
            "https://first.example.cz/start"
        )
        self.assertEqual(result.final_url, "https://second.example.cz/contact")
        self.assertEqual(result.emails[0].email, "final@example.cz")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][1], ("1.1.1.1",))

    def test_redirect_to_private_dns_fails_before_second_request(self) -> None:
        transports = 0

        def resolver(host: str, _port: int):
            return ("93.184.216.34",) if host == "public.example.cz" else ("127.0.0.1",)

        def transport(_url, _addresses, _timeout, _maximum):
            nonlocal transports
            transports += 1
            return RawResponse(302, {"location": "https://private.example.cz/"}, b"")

        with self.assertRaises(FetchError) as raised:
            PublicHTTPSFetcher(resolver=resolver, transport=transport).fetch(
                "https://public.example.cz/"
            )
        self.assertEqual(raised.exception.code, "non_public_ip")
        self.assertEqual(transports, 1)

    def test_redirect_loop_and_limit_fail_closed(self) -> None:
        def transport(url, _addresses, _timeout, _maximum):
            return RawResponse(302, {"location": url.url}, b"")

        with self.assertRaises(FetchError) as raised:
            PublicHTTPSFetcher(
                resolver=lambda *_: ("93.184.216.34",), transport=transport
            ).fetch("https://loop.example.cz/")
        self.assertEqual(raised.exception.code, "redirect_loop")

    def test_rejects_status_content_type_and_oversized_body(self) -> None:
        cases = (
            (RawResponse(404, {"content-type": "text/html"}, b"no"), "http_status"),
            (
                RawResponse(201, {"content-type": "text/html"}, b"created"),
                "http_status",
            ),
            (RawResponse(204, {"content-type": "text/html"}, b""), "http_status"),
            (
                RawResponse(200, {"content-type": "application/json"}, b"{}"),
                "invalid_content_type",
            ),
            (
                RawResponse(200, {"content-type": "text/plain"}, b"x" * 11),
                "body_too_large",
            ),
        )
        for response, code in cases:
            with self.subTest(code=code), self.assertRaises(FetchError) as raised:
                PublicHTTPSFetcher(
                    resolver=lambda *_: ("93.184.216.34",),
                    transport=lambda *_args, response=response: response,
                    maximum_body_bytes=10,
                ).fetch("https://source.example.cz/")
            self.assertEqual(raised.exception.code, code)

    def test_rejects_unknown_charset(self) -> None:
        response = RawResponse(
            200, {"content-type": "text/plain; charset=no-such-codec"}, b"hello"
        )
        with self.assertRaises(FetchError) as raised:
            PublicHTTPSFetcher(
                resolver=lambda *_: ("93.184.216.34",),
                transport=lambda *_: response,
            ).fetch("https://source.example.cz/")
        self.assertEqual(raised.exception.code, "invalid_charset")

    def test_rejects_compression_duplicate_headers_and_ambiguous_length(self) -> None:
        cases = (
            (
                RawResponse(
                    200,
                    {"content-type": "text/plain", "content-encoding": "gzip"},
                    b"gzip",
                ),
                "encoded_body",
            ),
            (
                RawResponse(
                    200, {"content-type": "text/plain"}, b"ok", ("content-type",)
                ),
                "duplicate_header",
            ),
            (
                RawResponse(
                    200,
                    {
                        "content-type": "text/plain",
                        "content-length": "2",
                        "transfer-encoding": "chunked",
                    },
                    b"ok",
                ),
                "ambiguous_length",
            ),
        )
        for response, code in cases:
            with self.subTest(code=code), self.assertRaises(FetchError) as raised:
                PublicHTTPSFetcher(
                    resolver=lambda *_: ("93.184.216.34",),
                    transport=lambda *_args, response=response: response,
                ).fetch("https://source.example.cz/")
            self.assertEqual(raised.exception.code, code)

    def test_accepts_bounded_dechunked_body_and_rejects_length_mismatch(self) -> None:
        accepted = RawResponse(
            200,
            {"content-type": "text/plain", "transfer-encoding": "chunked"},
            b"hello@example.cz",
        )
        result = PublicHTTPSFetcher(
            resolver=lambda *_: ("93.184.216.34",),
            transport=lambda *_: accepted,
        ).fetch("https://source.example.cz/")
        self.assertEqual(result.body, b"hello@example.cz")
        mismatch = RawResponse(
            200,
            {"content-type": "text/plain", "content-length": "99"},
            b"short",
        )
        with self.assertRaises(FetchError) as raised:
            PublicHTTPSFetcher(
                resolver=lambda *_: ("93.184.216.34",), transport=lambda *_: mismatch
            ).fetch("https://source.example.cz/")
        self.assertEqual(raised.exception.code, "invalid_length")

    def test_dns_resolution_is_inside_ten_second_style_deadline(self) -> None:
        def slow_resolver(*_args):
            time.sleep(0.1)
            return ("93.184.216.34",)

        with self.assertRaises(FetchError) as raised:
            PublicHTTPSFetcher(
                resolver=slow_resolver,
                transport=lambda *_: self.fail(
                    "transport must not run after DNS timeout"
                ),
                timeout_seconds=0.01,
            ).fetch("https://source.example.cz/")
        self.assertEqual(raised.exception.code, "timeout")

    def test_transport_pins_ip_but_keeps_original_tls_hostname(self) -> None:
        class Response:
            status = 200

            @staticmethod
            def getheaders():
                return [("Content-Type", "text/plain"), ("Content-Length", "2")]

            @staticmethod
            def read(_maximum):
                return b"ok"

        class Connection:
            hosts = []

            def __init__(self, host, **kwargs):
                self.host = host
                self.hosts.append(host)
                self.kwargs = kwargs
                self._create_connection = None

            def request(self, *_args, **_kwargs):
                self._create_connection((self.host, 443), 1.0, None)

            @staticmethod
            def getresponse():
                return Response()

            @staticmethod
            def close():
                return None

        normalized = normalize_public_https_url("https://creator.example.cz/contact")
        with mock.patch(
            "freio_prospecting.fetcher.http.client.HTTPSConnection", Connection
        ), mock.patch(
            "freio_prospecting.fetcher.socket.create_connection",
            return_value=mock.Mock(),
        ) as create_connection:
            response = _pinned_https_request(normalized, ("1.1.1.1",), 2.0, 1024)
        self.assertEqual(response.body, b"ok")
        self.assertEqual(Connection.hosts, ["creator.example.cz"])
        self.assertEqual(create_connection.call_args.args[0], ("1.1.1.1", 443))


if __name__ == "__main__":
    unittest.main()
