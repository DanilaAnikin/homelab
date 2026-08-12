from __future__ import annotations

import json
import subprocess
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

        self.assertEqual(set(routers), {
            "freio-public-failover-apex",
            "freio-public-failover-www",
        })
        self.assertEqual(
            {router["rule"] for router in routers.values()},
            {"Host(`freio.cz`)", "Host(`www.freio.cz`)"},
        )
        self.assertTrue(all(router["priority"] == 30000 for router in routers.values()))
        self.assertTrue(all(router["entryPoints"] == ["web"] for router in routers.values()))

        failover = services["freio-public-failover"]["failover"]
        self.assertEqual(failover["service"], "freio-public-primary")
        self.assertEqual(failover["fallback"], "freio-public-static")
        self.assertEqual(failover["healthCheck"], {})

        primary = services["freio-public-primary"]["loadBalancer"]
        backup = services["freio-public-static"]["loadBalancer"]
        self.assertEqual(primary["servers"], [{"url": "http://freio-xkgrrq:3000"}])
        self.assertEqual(backup["servers"], [{"url": "http://freio-public-fallback:8080"}])
        self.assertEqual(primary["healthCheck"]["path"], "/")
        self.assertEqual(backup["healthCheck"]["path"], "/healthz")
        self.assertEqual(primary["healthCheck"]["interval"], "2s")
        self.assertEqual(backup["healthCheck"]["interval"], "2s")

    def test_fallback_container_is_secretless_read_only_and_unpublished(self) -> None:
        compose = yaml.safe_load(
            (COMPOSE_DIR / "docker-compose.yml").read_text(encoding="utf-8")
        )
        service = compose["services"]["web"]
        self.assertEqual(service["container_name"], "freio-public-fallback")
        self.assertTrue(service["read_only"])
        self.assertEqual(service["user"], "10001:10001")
        self.assertEqual(service["cap_drop"], ["ALL"])
        self.assertEqual(service["security_opt"], ["no-new-privileges:true"])
        self.assertNotIn("ports", service)
        self.assertNotIn("environment", service)
        self.assertNotIn("env_file", service)
        self.assertNotIn("volumes", service)
        self.assertEqual(service["networks"], ["dokploy-network"])
        self.assertTrue(compose["networks"]["dokploy-network"]["external"])

    def test_server_is_self_contained_and_write_paths_fail_closed(self) -> None:
        source = (COMPOSE_DIR / "server.mjs").read_text(encoding="utf-8")
        self.assertIn('"X-Freio-Fallback": "static-v1"', source)
        self.assertIn("Záložní režim je aktivní", source)
        self.assertIn('url.pathname.startsWith("/api/")', source)
        self.assertIn('url.pathname.startsWith("/_next/")', source)
        self.assertIn('(method === "GET" || method === "HEAD")', source)
        self.assertIn('"error":"service_temporarily_unavailable"', source)
        self.assertNotIn("fetch(", source)
        self.assertNotIn("SUPABASE", source)
        self.assertNotIn("STRIPE", source)

    def test_shell_and_systemd_contracts_are_fail_closed(self) -> None:
        subprocess.run(["bash", "-n", str(CHECK)], check=True)
        script = CHECK.read_text(encoding="utf-8")
        self.assertIn("runtime_config_drift", script)
        self.assertIn("fallback_unhealthy", script)
        self.assertIn("x-freio-fallback", script.lower())
        self.assertIn("exit 2", script)

        service = SERVICE.read_text(encoding="utf-8")
        timer = TIMER.read_text(encoding="utf-8")
        self.assertIn(
            "OnFailure=notify-failure@freio-public-failover-check.service",
            service,
        )
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("InaccessiblePaths=-/srv/homelab/secrets", service)
        self.assertIn("OnUnitActiveSec=1min", timer)
        self.assertIn("Persistent=false", timer)

    def test_node_source_is_syntax_valid(self) -> None:
        subprocess.run(
            ["node", "--check", str(COMPOSE_DIR / "server.mjs")],
            check=True,
        )

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
        service = rendered["services"]["web"]
        self.assertNotIn("environment", service)
        self.assertNotIn("ports", service)


if __name__ == "__main__":
    unittest.main()
