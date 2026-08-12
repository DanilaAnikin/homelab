from __future__ import annotations

import json
import http.client
import http.server
import os
import socket
import subprocess
import threading
import time
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
COMPOSE_DIR = ROOT / "compose" / "freio-public-fallback"
TRAEFIK = ROOT / "compose" / "traefik" / "freio-public-failover.yml"
CHECK = ROOT / "scripts" / "freio-public-failover-check.sh"
SERVICE = ROOT / "scripts" / "systemd" / "freio-public-failover-check.service"
TIMER = ROOT / "scripts" / "systemd" / "freio-public-failover-check.timer"


class FreioPublicFailoverContractTest(unittest.TestCase):
    def test_traefik_routes_only_apex_and_www_to_health_checked_failover(self) -> None:
        config = yaml.safe_load(TRAEFIK.read_text(encoding="utf-8"))
        routers = config["http"]["routers"]
        services = config["http"]["services"]

        self.assertEqual(
            set(routers),
            {"freio-public-read-gateway", "freio-public-write-gateway"},
        )
        read_router = routers["freio-public-read-gateway"]
        write_router = routers["freio-public-write-gateway"]
        self.assertEqual(
            write_router["rule"], "Host(`freio.cz`) || Host(`www.freio.cz`)"
        )
        self.assertIn("Method(`GET`)", read_router["rule"])
        self.assertEqual(read_router["priority"], 31000)
        self.assertEqual(write_router["priority"], 30000)
        self.assertTrue(
            all(router["entryPoints"] == ["web"] for router in routers.values())
        )
        self.assertTrue(
            all(router["service"] == "freio-public-gateway" for router in routers.values())
        )
        self.assertEqual(read_router["middlewares"], ["freio-public-read-retry"])
        self.assertNotIn("middlewares", write_router)

        retry = config["http"]["middlewares"]["freio-public-read-retry"]["retry"]
        self.assertEqual(retry, {"attempts": 2, "initialInterval": "50ms"})

        self.assertEqual(set(services), {"freio-public-gateway"})
        gateway = services["freio-public-gateway"]["loadBalancer"]
        self.assertEqual(
            gateway["servers"],
            [
                {"url": "http://freio-public-gateway-a:8080"},
                {"url": "http://freio-public-gateway-b:8080"},
            ],
        )
        self.assertTrue(gateway["passHostHeader"])
        self.assertEqual(gateway["healthCheck"]["path"], "/healthz")
        self.assertEqual(gateway["healthCheck"]["interval"], "2s")
        self.assertEqual(gateway["healthCheck"]["timeout"], "1s")

    def test_fallback_container_is_secretless_read_only_and_unpublished(self) -> None:
        compose = yaml.safe_load(
            (COMPOSE_DIR / "docker-compose.yml").read_text(encoding="utf-8")
        )
        self.assertEqual(set(compose["services"]), {"gateway-a", "gateway-b"})
        self.assertEqual(
            {service["container_name"] for service in compose["services"].values()},
            {"freio-public-gateway-a", "freio-public-gateway-b"},
        )
        for service in compose["services"].values():
            self.assertTrue(service["read_only"])
            self.assertEqual(service["user"], "10001:10001")
            self.assertEqual(service["cap_drop"], ["ALL"])
            self.assertEqual(service["security_opt"], ["no-new-privileges:true"])
            self.assertNotIn("ports", service)
            self.assertEqual(
                service["environment"],
                {"PRIMARY_HOST": "freio-xkgrrq", "PRIMARY_PORT": "3000"},
            )
            self.assertNotIn("env_file", service)
            self.assertNotIn("secrets", service)
            self.assertNotIn("volumes", service)
            self.assertEqual(service["networks"], ["dokploy-network"])
        self.assertTrue(compose["networks"]["dokploy-network"]["external"])

    def test_server_is_self_contained_and_write_paths_fail_closed(self) -> None:
        source = (COMPOSE_DIR / "server.mjs").read_text(encoding="utf-8")
        self.assertIn('"X-Freio-Fallback": "static-v1"', source)
        self.assertIn("Záložní režim je aktivní", source)
        self.assertIn('pathname.startsWith("/api/")', source)
        self.assertIn('pathname.startsWith("/_next/")', source)
        self.assertIn('pathname === "/api"', source)
        self.assertIn('pathname === "/_next"', source)
        self.assertIn('(method === "GET" || method === "HEAD")', source)
        self.assertIn('parsed?.scheme === "http"', source)
        self.assertIn('Location: `https://${hostName}${rawTarget}`', source)
        self.assertIn("publicHosts", source)
        self.assertIn('"error":"service_temporarily_unavailable"', source)
        self.assertIn("response.statusCode", source)
        self.assertIn("response.pipe(res)", source)
        self.assertIn("req.pipe(upstream)", source)
        self.assertNotIn("fetch(", source)
        self.assertNotIn("SUPABASE", source)
        self.assertNotIn("STRIPE", source)

    def test_shell_and_systemd_contracts_are_fail_closed(self) -> None:
        subprocess.run(["bash", "-n", str(CHECK)], check=True)
        script = CHECK.read_text(encoding="utf-8")
        self.assertIn("runtime_config_drift", script)
        self.assertIn("fallback_unhealthy", script)
        self.assertIn("x-freio-fallback", script.lower())
        self.assertIn("previous_route", script)
        self.assertIn("persist_route fallback", script)
        self.assertIn("freio-public-gateway-a freio-public-gateway-b", script)
        self.assertIn("exit 2", script)

        service = SERVICE.read_text(encoding="utf-8")
        timer = TIMER.read_text(encoding="utf-8")
        self.assertIn(
            "OnFailure=notify-failure@freio-public-failover-check.service",
            service,
        )
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("StateDirectory=freio-public-failover", service)
        self.assertIn("InaccessiblePaths=-/srv/homelab/secrets", service)
        self.assertIn("OnUnitActiveSec=1min", timer)
        self.assertIn("Persistent=false", timer)

    def test_node_source_is_syntax_valid(self) -> None:
        subprocess.run(
            ["node", "--check", str(COMPOSE_DIR / "server.mjs")],
            check=True,
        )

    def test_gateway_runtime_intercepts_first_failure_without_replaying_writes(self) -> None:
        class ReusableThreadingHTTPServer(http.server.ThreadingHTTPServer):
            allow_reuse_address = True

        calls: list[tuple[str, str]] = []

        class PrimaryHandler(http.server.BaseHTTPRequestHandler):
            def handle_request(self) -> None:
                calls.append((self.command, self.path))
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length:
                    self.rfile.read(content_length)
                if self.path == "/primary-5xx":
                    self.send_response(502)
                    self.end_headers()
                    self.wfile.write(b"primary error")
                    return
                if self.command == "POST":
                    self.send_response(204)
                    self.end_headers()
                    return
                body = b"primary ok"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Freio-Primary-Test", "true")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            do_GET = handle_request
            do_HEAD = handle_request
            do_POST = handle_request

            def log_message(self, _format: str, *_args: object) -> None:
                return

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            primary_port = probe.getsockname()[1]

        primary = ReusableThreadingHTTPServer(
            ("127.0.0.1", primary_port), PrimaryHandler
        )
        primary_thread = threading.Thread(target=primary.serve_forever, daemon=True)
        primary_thread.start()

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            gateway_port = probe.getsockname()[1]

        process = subprocess.Popen(
            ["node", str(COMPOSE_DIR / "server.mjs")],
            env={
                **os.environ,
                "PORT": str(gateway_port),
                "PRIMARY_HOST": "127.0.0.1",
                "PRIMARY_PORT": str(primary_port),
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(process.kill)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", gateway_port, timeout=1
                )
                connection.request("GET", "/healthz")
                response = connection.getresponse()
                response.read()
                connection.close()
                if response.status == 200:
                    break
            except OSError:
                time.sleep(0.05)
        else:
            stderr = process.stderr.read() if process.stderr else ""
            self.fail(f"fallback server did not start: {stderr}")

        def request(
            method: str,
            path: str,
            headers: dict[str, str] | None = None,
        ) -> tuple[int, dict[str, str], str]:
            connection = http.client.HTTPConnection(
                "127.0.0.1", gateway_port, timeout=3
            )
            connection.request(
                method,
                path,
                body=b"test" if method == "POST" else None,
                headers=headers or {},
            )
            response = connection.getresponse()
            body = response.read().decode("utf-8")
            headers = {name.lower(): value for name, value in response.getheaders()}
            connection.close()
            return response.status, headers, body

        def expect_continue_request(path: str) -> bytes:
            with socket.create_connection(("127.0.0.1", gateway_port), timeout=3) as raw:
                raw.sendall(
                    (
                        f"POST {path} HTTP/1.1\r\n"
                        "Host: freio.cz\r\n"
                        "Content-Length: 4\r\n"
                        "Expect: 100-continue\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode("ascii")
                )
                return raw.recv(4096)

        status, headers, body = request("GET", "/pricing")
        self.assertEqual((status, headers.get("x-freio-primary-test"), body), (200, "true", "primary ok"))
        origin_calls = len(calls)
        status, headers, body = request(
            "GET",
            "/pricing?source=plain-http",
            {
                "Host": "www.freio.cz",
                "CF-Visitor": '{"scheme":"http"}',
                "X-Forwarded-Proto": "http",
            },
        )
        self.assertEqual(status, 308)
        self.assertEqual(
            headers.get("location"),
            "https://www.freio.cz/pricing?source=plain-http",
        )
        self.assertEqual(body, "")
        self.assertEqual(len(calls), origin_calls)
        status, headers, body = request(
            "GET",
            "/healthz",
            {
                "Host": "freio.cz",
                "CF-Visitor": '{"scheme":"http"}',
            },
        )
        self.assertEqual(status, 308)
        self.assertEqual(headers.get("location"), "https://freio.cz/healthz")
        self.assertEqual(body, "")
        self.assertEqual(len(calls), origin_calls)
        status, headers, body = request(
            "POST",
            "/api/write",
            {
                "Host": "freio.cz",
                "CF-Visitor": '{"scheme":"http"}',
                "X-Forwarded-Proto": "http",
            },
        )
        self.assertEqual(status, 308)
        self.assertEqual(headers.get("location"), "https://freio.cz/api/write")
        self.assertEqual(body, "")
        self.assertEqual(len(calls), origin_calls)
        status, headers, body = request(
            "GET",
            "/xfp-only",
            {"Host": "freio.cz", "X-Forwarded-Proto": "http"},
        )
        self.assertEqual(status, 308)
        self.assertEqual(headers.get("location"), "https://freio.cz/xfp-only")
        self.assertEqual(body, "")
        self.assertEqual(len(calls), origin_calls)
        status, headers, body = request(
            "GET",
            "/invalid-host",
            {
                "Host": "attacker.invalid",
                "CF-Visitor": '{"scheme":"http"}',
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("invalid_public_host", body)
        self.assertEqual(len(calls), origin_calls)
        status, headers, body = request(
            "GET",
            "//attacker.invalid/path",
            {
                "Host": "freio.cz",
                "CF-Visitor": '{"scheme":"http"}',
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("invalid_request_target", body)
        self.assertEqual(len(calls), origin_calls)
        status, headers, body = request(
            "GET",
            "/invalid-cf-visitor",
            {
                "Host": "freio.cz",
                "CF-Visitor": "not-json",
                "X-Forwarded-Proto": "http",
            },
        )
        self.assertEqual((status, headers.get("x-freio-primary-test"), body), (200, "true", "primary ok"))
        status, headers, body = request(
            "GET",
            "/https-precedence",
            {
                "Host": "freio.cz",
                "CF-Visitor": '{"scheme":"https"}',
                "X-Forwarded-Proto": "http",
            },
        )
        self.assertEqual((status, headers.get("x-freio-primary-test"), body), (200, "true", "primary ok"))
        status, headers, body = request(
            "GET",
            "/http-precedence",
            {
                "Host": "freio.cz",
                "CF-Visitor": '{"scheme":"http"}',
                "X-Forwarded-Proto": "https",
            },
        )
        self.assertEqual(status, 308)
        self.assertEqual(headers.get("location"), "https://freio.cz/http-precedence")
        self.assertEqual(body, "")
        status, _headers, _body = request("POST", "/api/write")
        self.assertEqual(status, 204)
        self.assertEqual(calls.count(("POST", "/api/write")), 1)
        expectation_response = expect_continue_request("/api/expect")
        self.assertTrue(expectation_response.startswith(b"HTTP/1.1 417"))
        self.assertEqual(calls.count(("POST", "/api/expect")), 0)

        with socket.create_connection(("127.0.0.1", gateway_port), timeout=3) as raw:
            raw.sendall(b"GET http://[ HTTP/1.1\r\nHost: freio.cz\r\nConnection: close\r\n\r\n")
            malformed_response = raw.recv(4096)
        self.assertTrue(malformed_response.startswith(b"HTTP/1.1 400"))
        self.assertIsNone(process.poll())

        status, headers, body = request("GET", "/primary-5xx")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("x-freio-fallback"), "static-v1")
        self.assertIn("Záložní režim je aktivní", body)
        status, headers, body = request("POST", "/primary-5xx")
        self.assertEqual(status, 503)
        self.assertEqual(headers.get("x-freio-fallback"), "static-v1")
        self.assertIn("service_temporarily_unavailable", body)
        self.assertEqual(calls.count(("POST", "/primary-5xx")), 1)

        primary.shutdown()
        primary.server_close()
        primary_thread.join(timeout=5)

        for path in ("/", "/pricing", "/robots.txt"):
            status, headers, body = request("GET", path)
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("x-freio-fallback"), "static-v1")
            self.assertIn("Záložní režim je aktivní", body)
        for method, path in (
            ("GET", "/api"),
            ("GET", "/api/test"),
            ("GET", "/%61pi"),
            ("GET", "/api%2Ftest"),
            ("GET", "//api"),
            ("GET", "/api%ZZ"),
            ("GET", "/_next"),
            ("GET", "/_next/static/app.js"),
            ("GET", "/%5fnext"),
            ("GET", "/_next%2Fstatic/app.js"),
            ("POST", "/"),
            ("POST", "/api/write"),
        ):
            status, headers, body = request(method, path)
            self.assertEqual(status, 503)
            self.assertEqual(headers.get("x-freio-fallback"), "static-v1")
            self.assertIn("service_temporarily_unavailable", body)

        primary = ReusableThreadingHTTPServer(
            ("127.0.0.1", primary_port), PrimaryHandler
        )
        primary_thread = threading.Thread(target=primary.serve_forever, daemon=True)
        primary_thread.start()
        status, headers, body = request("GET", "/restored")
        self.assertEqual((status, headers.get("x-freio-primary-test"), body), (200, "true", "primary ok"))
        primary.shutdown()
        primary.server_close()
        primary_thread.join(timeout=5)

        process.terminate()
        process.wait(timeout=5)
        if process.stderr:
            process.stderr.close()

    def test_compose_renders_without_environment(self) -> None:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(COMPOSE_DIR / "docker-compose.yml"),
                "config",
                "--format",
                "json",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        rendered = json.loads(result.stdout)
        self.assertEqual(set(rendered["services"]), {"gateway-a", "gateway-b"})
        for service in rendered["services"].values():
            self.assertEqual(
                service["environment"],
                {"PRIMARY_HOST": "freio-xkgrrq", "PRIMARY_PORT": "3000"},
            )
            self.assertNotIn("ports", service)


if __name__ == "__main__":
    unittest.main()
