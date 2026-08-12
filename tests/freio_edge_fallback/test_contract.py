from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKER_DIR = ROOT / "compose" / "cloudflare" / "freio-edge-fallback"
WORKER = WORKER_DIR / "worker.mjs"
WRANGLER = WORKER_DIR / "wrangler.toml"
NODE_TEST = Path(__file__).with_name("worker.test.mjs")


class FreioEdgeFallbackContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.source = WORKER.read_text(encoding="utf-8")

    def test_worker_is_default_off_and_has_no_bindings_or_routes(self) -> None:
        config = WRANGLER.read_text(encoding="utf-8")

        self.assertRegex(config, r'(?m)^name = "freio-edge-fallback"$')
        self.assertRegex(config, r'(?m)^main = "worker\.mjs"$')
        self.assertRegex(config, r'(?m)^compatibility_date = "2026-08-12"$')
        self.assertRegex(
            config,
            r'(?m)^compatibility_flags = \["global_fetch_private_origin"\]$',
        )
        self.assertRegex(config, r"(?m)^workers_dev = false$")
        self.assertRegex(config, r"(?m)^preview_urls = false$")
        self.assertNotRegex(config, r"(?m)^routes?\s*=")
        self.assertNotRegex(config, r"(?m)^\[vars\]$")
        self.assertNotRegex(config, r"(?m)^account_id\s*=")

    def test_source_has_no_secrets_external_assets_or_request_logging(self) -> None:
        forbidden = (
            "SUPABASE",
            "STRIPE",
            "Authorization",
            "Cookie",
            "console.",
            "env.",
            "fetch(\"http",
            "fetch('http",
            "<script",
            "<img",
            "<link",
        )
        for value in forbidden:
            with self.subTest(value=value):
                self.assertNotIn(value, self.source)

        self.assertNotRegex(
            self.source,
            re.compile(r"(?:token|secret|password|api[_-]?key)\s*[:=]", re.I),
        )

    def test_source_contains_required_fail_closed_guards_and_headers(self) -> None:
        required = (
            'new Set(["freio.cz", "www.freio.cz"])',
            'const HEALTH_PATH = "/__freio-edge-health"',
            "ORIGIN_TIMEOUT_MS = 4_000",
            '"X-Freio-Edge-Fallback"',
            '"Cache-Control": "no-store, max-age=0"',
            '"Retry-After": "20"',
            '"Content-Security-Policy"',
            'pathname === "/api"',
            'pathname === "/_next"',
            'pathname.includes("%")',
            "status >= 500 && status <= 504",
            'destination.protocol = "https:"',
            "status: 308",
            "Exactly one origin attempt",
        )
        for value in required:
            with self.subTest(value=value):
                self.assertIn(value, self.source)

    def test_worker_and_node_test_are_syntax_valid(self) -> None:
        for path in (WORKER, NODE_TEST):
            with self.subTest(path=path.name):
                subprocess.run(
                    ["node", "--check", str(path)],
                    check=True,
                    capture_output=True,
                    text=True,
                )

    def test_node_behavior_suite_passes(self) -> None:
        result = subprocess.run(
            ["node", "--test", str(NODE_TEST)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
