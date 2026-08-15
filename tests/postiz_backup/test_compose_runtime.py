#!/usr/bin/env python3
"""Executable fail-closed fixtures for the Postiz Compose runtime contract."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "postiz-backup-manifest.py"
SPEC = importlib.util.spec_from_file_location("postiz_backup_manifest_compose", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
manifest_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manifest_module)

# Non-secret hashes recorded from the audited live Compose 5.3.1 generation.
# The model builder below preserves its normalized shape but replaces all values
# that could disclose runtime configuration, so these hashes are witness values
# rather than hashes of the sanitized fixture itself.
LIVE_V53_FULL_HASHES = {
    "postiz": "3f88a262802d14fa0401a71abf751153132250408a3a6d55fdee2cb2842999db",
    "postiz-postgres": "b3c4a7650e0092c0008772bbcf087a8a43479c860d98fe803ade3ba73fa3857d",
    "postiz-redis": "37715cf80ca08fc67bb1d6388d6b468d31c04a2ecee9251b428db0eb5a0c6a92",
    "postiz-temporal": "6316161bfee711bfc46891b418e2f8f54a402ee25bce456ca7b6a55961ba7dd6",
}
LIVE_V53_POSTIZ_NO_DEPS_HASH = (
    "6e0fb11e0ae8b8ab9f6188ee0ead6a66b16835e530284c5413842cac2af40eb1"
)
LIVE_CONTAINER_IDS = {
    service: hashlib.sha256(f"container:{service}".encode()).hexdigest()
    for service in LIVE_V53_FULL_HASHES
}
LIVE_IMAGE_IDS = {
    service: f"sha256:{hashlib.sha256(f'image:{service}'.encode()).hexdigest()}"
    for service in LIVE_V53_FULL_HASHES
}
SERVICE_ORDINAL = {service: index for index, service in enumerate(LIVE_V53_FULL_HASHES, 10)}
NETWORK_IDS = {
    name: hashlib.sha256(f"network:{name}".encode()).hexdigest()
    for name in ("dokploy-network", "postiz_postiz-internal")
}
STOPPED_FINISHED_AT = "2026-08-15T08:30:00.123456789Z"


def network_attachment(service: str, network_name: str) -> dict[str, object]:
    ordinal = SERVICE_ORDINAL[service]
    external = network_name == "dokploy-network"
    third_octet = 31 if external else 30
    ipv4 = f"172.{third_octet}.0.{ordinal}"
    mac = f"02:42:ac:{third_octet:02x}:00:{ordinal:02x}"
    return {
        "Aliases": [service, service],
        "NetworkID": NETWORK_IDS[network_name],
        "EndpointID": hashlib.sha256(f"endpoint:{network_name}:{service}".encode()).hexdigest(),
        "IPAddress": ipv4,
        "IPPrefixLen": 24,
        "GlobalIPv6Address": "",
        "GlobalIPv6PrefixLen": 0,
        "MacAddress": mac,
    }


def live_v53_compose_shape() -> dict[str, object]:
    return {
        "name": "postiz",
        "services": {
            "postiz": {
                "command": None,
                "container_name": "postiz",
                "depends_on": copy.deepcopy(manifest_module.POSTIZ_NO_DEPS_DEPENDENCIES),
                "entrypoint": None,
                "environment": {"GENERATION": "fixture-v5"},
                "image": "fixture.invalid/postiz:1",
                "mem_limit": "1782579200",
                "networks": {"dokploy-network": None, "postiz-internal": None},
                "pull_policy": "never",
                "restart": "unless-stopped",
                "volumes": [
                    {
                        "type": "volume",
                        "source": "postiz-config",
                        "target": "/config",
                        "volume": {},
                    },
                    {
                        "type": "volume",
                        "source": "postiz-uploads",
                        "target": "/uploads",
                        "volume": {},
                    },
                ],
            },
            "postiz-postgres": {
                "command": None,
                "container_name": "postiz-postgres",
                "entrypoint": None,
                "environment": {"GENERATION": "fixture-v5"},
                "image": "fixture.invalid/postgres:17",
                "mem_limit": "536870912",
                "networks": {"postiz-internal": None},
                "restart": "unless-stopped",
                "volumes": [
                    {
                        "type": "volume",
                        "source": "postiz-postgres",
                        "target": "/var/lib/postgresql/data",
                        "volume": {},
                    }
                ],
            },
            "postiz-redis": {
                "command": None,
                "container_name": "postiz-redis",
                "entrypoint": None,
                "environment": {},
                "image": "fixture.invalid/redis:7.2",
                "mem_limit": "268435456",
                "networks": {"postiz-internal": None},
                "restart": "unless-stopped",
                "volumes": [
                    {
                        "type": "volume",
                        "source": "postiz-redis",
                        "target": "/data",
                        "volume": {},
                    }
                ],
            },
            "postiz-temporal": {
                "command": None,
                "container_name": "postiz-temporal",
                "depends_on": {
                    "postiz-postgres": {"condition": "service_started", "required": True}
                },
                "entrypoint": None,
                "environment": {"GENERATION": "fixture-v5"},
                "image": "fixture.invalid/temporal:1.25.2",
                "mem_limit": "1073741824",
                "networks": {"postiz-internal": None},
                "restart": "unless-stopped",
            },
        },
        "networks": {
            "dokploy-network": {
                "name": "dokploy-network",
                "ipam": {},
                "external": True,
            },
            "postiz-internal": {
                "name": "postiz_postiz-internal",
                "driver": "bridge",
                "ipam": {},
            },
        },
        "volumes": {
            "postiz-config": {"name": "postiz_postiz-config"},
            "postiz-postgres": {"name": "postiz_postiz-postgres"},
            "postiz-redis": {"name": "postiz_postiz-redis"},
            "postiz-uploads": {"name": "postiz_postiz-uploads"},
        },
    }


def no_deps_shape(compose: dict[str, object]) -> dict[str, object]:
    value = copy.deepcopy(compose)
    del value["services"]["postiz"]["depends_on"]
    return value


def runtime_images() -> list[dict[str, object]]:
    return [
        {
            "Id": LIVE_IMAGE_IDS[service],
            "Config": {
                "Env": [
                    f"IMAGE_DEFAULT=from-{service}-image",
                    f"GENERATION=from-{service}-image",
                ]
            },
        }
        for service in LIVE_V53_FULL_HASHES
    ]


def runtime_containers(
    compose: dict[str, object], runtime_state: str = "preflight"
) -> list[dict[str, object]]:
    containers: list[dict[str, object]] = []
    networks = compose["networks"]
    volumes = compose["volumes"]
    for service, definition in compose["services"].items():
        running = runtime_state == "preflight" or service == "postiz-postgres"
        environment = {
            "IMAGE_DEFAULT": f"from-{service}-image",
            "GENERATION": f"from-{service}-image",
        }
        environment.update(definition.get("environment", {}))
        actual_networks = {
            networks[source]["name"]: network_attachment(service, networks[source]["name"])
            for source in definition["networks"]
        }
        if not running:
            for attachment in actual_networks.values():
                attachment.update(
                    {
                        "EndpointID": "",
                        "IPAddress": "",
                        "IPPrefixLen": 0,
                        "GlobalIPv6Address": "",
                        "GlobalIPv6PrefixLen": 0,
                        "MacAddress": "",
                    }
                )
        mounts = [
            {
                "Type": "volume",
                "Name": volumes[mount["source"]]["name"],
                "Destination": mount["target"],
                "RW": not mount.get("read_only", False),
            }
            for mount in definition.get("volumes", [])
        ]
        containers.append(
            {
                "Id": LIVE_CONTAINER_IDS[service],
                "Image": LIVE_IMAGE_IDS[service],
                "Name": (
                    "/postiz-postgres-backup-fenced"
                    if runtime_state == "writer-fenced" and service == "postiz-postgres"
                    else f"/{service}"
                ),
                "State": {
                    "Status": "running" if running else "exited",
                    "Running": running,
                    "Paused": False,
                    "Restarting": False,
                    "Dead": False,
                    "ExitCode": 0,
                    "FinishedAt": (
                        manifest_module.DOCKER_ZERO_TIME if running else STOPPED_FINISHED_AT
                    ),
                },
                "Config": {
                    "Image": definition["image"],
                    "Env": [f"{key}={value}" for key, value in environment.items()],
                    "Labels": {
                        "com.docker.compose.project": "postiz",
                        "com.docker.compose.service": service,
                        "com.docker.compose.config-hash": (
                            LIVE_V53_POSTIZ_NO_DEPS_HASH
                            if service == "postiz"
                            else LIVE_V53_FULL_HASHES[service]
                        ),
                        "com.docker.compose.depends_on": (
                            "" if service == "postiz" else "fixture-full-generation"
                        ),
                    },
                },
                "HostConfig": {"PortBindings": {}},
                "Mounts": mounts,
                "NetworkSettings": {"Networks": actual_networks},
            }
        )
    return containers


def runtime_networks(
    compose: dict[str, object], containers: list[dict[str, object]]
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for definition in compose["networks"].values():
        network_name = definition["name"]
        members: dict[str, object] = {}
        for container in containers:
            if not container["State"]["Running"]:
                continue
            attachment = container["NetworkSettings"]["Networks"].get(network_name)
            if attachment is None:
                continue
            members[container["Id"]] = {
                "Name": container["Name"].removeprefix("/"),
                "EndpointID": attachment["EndpointID"],
                "MacAddress": attachment["MacAddress"],
                "IPv4Address": f"{attachment['IPAddress']}/{attachment['IPPrefixLen']}",
                "IPv6Address": attachment["GlobalIPv6Address"],
            }
        if definition.get("external", False):
            external_id = hashlib.sha256(b"external-dokploy-member").hexdigest()
            external_endpoint = hashlib.sha256(b"external-dokploy-endpoint").hexdigest()
            members[external_id] = {
                "Name": "unrelated-external-service",
                "EndpointID": external_endpoint,
                "MacAddress": "02:42:ac:1f:00:fa",
                "IPv4Address": "172.31.0.250/24",
                "IPv6Address": "",
            }
        third_octet = 31 if network_name == "dokploy-network" else 30
        records.append(
            {
                "Name": network_name,
                "Id": NETWORK_IDS[network_name],
                "EnableIPv6": False,
                "IPAM": {
                    "Config": [
                        {
                            "Subnet": f"172.{third_octet}.0.0/24",
                            "Gateway": f"172.{third_octet}.0.1",
                        }
                    ]
                },
                "Containers": members,
            }
        )
    return records


class ComposeRuntimeContractFixtures(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def verify(
        self,
        compose: dict[str, object] | None = None,
        effective: dict[str, object] | None = None,
        containers: list[dict[str, object]] | None = None,
        images: list[dict[str, object]] | None = None,
        networks: list[dict[str, object]] | None = None,
        expected_images: dict[str, str] | None = None,
        full_hashes: dict[str, str] | None = None,
        resolved_hashes: dict[str, str] | None = None,
        effective_hash: str = LIVE_V53_POSTIZ_NO_DEPS_HASH,
        runtime_state: str = "preflight",
    ) -> None:
        if compose is None:
            compose = live_v53_compose_shape()
        if effective is None:
            effective = no_deps_shape(compose)
        if containers is None:
            containers = runtime_containers(compose, runtime_state)
        if images is None:
            images = runtime_images()
        if networks is None:
            networks = runtime_networks(compose, containers)
        if expected_images is None:
            expected_images = LIVE_IMAGE_IDS
        if full_hashes is None:
            full_hashes = LIVE_V53_FULL_HASHES
        if resolved_hashes is None:
            resolved_hashes = {**full_hashes, "postiz": effective_hash}
        compose_path = self.base / "compose.json"
        effective_path = self.base / "compose-no-deps.json"
        containers_path = self.base / "containers.json"
        images_path = self.base / "images.json"
        networks_path = self.base / "networks.json"
        hashes_path = self.base / "hashes.txt"
        resolved_hashes_path = self.base / "resolved-hashes.txt"
        effective_hash_path = self.base / "no-deps-hash.txt"
        compose_path.write_text(json.dumps(compose), encoding="utf-8")
        effective_path.write_text(json.dumps(effective), encoding="utf-8")
        containers_path.write_text(json.dumps(containers), encoding="utf-8")
        images_path.write_text(json.dumps(images), encoding="utf-8")
        networks_path.write_text(json.dumps(networks), encoding="utf-8")
        hashes_path.write_text(
            "".join(f"{service} {full_hashes[service]}\n" for service in sorted(full_hashes)),
            encoding="utf-8",
        )
        resolved_hashes_path.write_text(
            "".join(
                f"{service} {resolved_hashes[service]}\n" for service in sorted(resolved_hashes)
            ),
            encoding="utf-8",
        )
        effective_hash_path.write_text(f"postiz {effective_hash}\n", encoding="utf-8")
        manifest_module.command_verify_compose_runtime(
            Namespace(
                compose_json=str(compose_path),
                compose_hashes=str(hashes_path),
                resolved_compose_hashes=str(resolved_hashes_path),
                postiz_no_deps_compose_json=str(effective_path),
                postiz_no_deps_hash=str(effective_hash_path),
                container_json=str(containers_path),
                image_inspect_json=str(images_path),
                network_inspect_json=str(networks_path),
                expected_image=[
                    f"{service}|{expected_images[service]}"
                    for service in sorted(expected_images)
                ],
                runtime_state=runtime_state,
            )
        )

    def rejected(self, **changes: object) -> None:
        with self.assertRaises(manifest_module.ContractError):
            self.verify(**changes)

    def test_live_docker_compose_v53_shape_uses_full_and_no_deps_hashes(self) -> None:
        self.assertNotEqual(
            LIVE_V53_FULL_HASHES["postiz"], LIVE_V53_POSTIZ_NO_DEPS_HASH
        )
        self.verify()

    def test_image_defaults_are_exact_and_compose_environment_overrides_them(self) -> None:
        compose = live_v53_compose_shape()
        containers = runtime_containers(compose)
        postiz = next(item for item in containers if item["Name"] == "/postiz")
        redis = next(item for item in containers if item["Name"] == "/postiz-redis")
        self.assertIn("IMAGE_DEFAULT=from-postiz-image", postiz["Config"]["Env"])
        self.assertIn("GENERATION=fixture-v5", postiz["Config"]["Env"])
        self.assertIn("GENERATION=from-postiz-redis-image", redis["Config"]["Env"])
        self.verify(compose=compose, containers=containers)

    def test_writer_fenced_state_has_only_postgres_active_network_endpoints(self) -> None:
        compose = live_v53_compose_shape()
        containers = runtime_containers(compose, "writer-fenced")
        networks = runtime_networks(compose, containers)
        postgres = next(item for item in containers if item["Name"].endswith("backup-fenced"))
        self.assertTrue(postgres["State"]["Running"])
        stopped = next(item for item in containers if item["Name"] == "/postiz")
        self.assertFalse(stopped["State"]["Running"])
        self.assertEqual(
            stopped["NetworkSettings"]["Networks"]["dokploy-network"]["IPAddress"], ""
        )
        self.verify(
            compose=compose,
            containers=containers,
            networks=networks,
            runtime_state="writer-fenced",
        )

        stopped["NetworkSettings"]["Networks"]["dokploy-network"]["IPAddress"] = (
            "172.31.0.10"
        )
        self.rejected(
            compose=compose,
            containers=containers,
            networks=networks,
            runtime_state="writer-fenced",
        )

    def test_runtime_state_fields_fail_closed_in_both_capture_phases(self) -> None:
        compose = live_v53_compose_shape()
        for field, value in (
            ("Status", "restarting"),
            ("Running", False),
            ("Paused", True),
            ("Restarting", True),
            ("Dead", True),
            ("ExitCode", 1),
            ("FinishedAt", "not-a-docker-time"),
        ):
            with self.subTest(phase="preflight", field=field):
                containers = runtime_containers(compose)
                networks = runtime_networks(compose, containers)
                postiz = next(item for item in containers if item["Name"] == "/postiz")
                postiz["State"][field] = value
                self.rejected(compose=compose, containers=containers, networks=networks)

        containers = runtime_containers(compose)
        postiz = next(item for item in containers if item["Name"] == "/postiz")
        postiz["State"]["FinishedAt"] = STOPPED_FINISHED_AT
        self.verify(compose=compose, containers=containers)

        for field, value in (
            ("Status", "dead"),
            ("Running", True),
            ("Paused", True),
            ("Restarting", True),
            ("Dead", True),
            ("ExitCode", 137),
            ("FinishedAt", manifest_module.DOCKER_ZERO_TIME),
            ("FinishedAt", "2026-99-99T99:99:99Z"),
        ):
            with self.subTest(phase="writer-fenced", field=field, value=value):
                containers = runtime_containers(compose, "writer-fenced")
                networks = runtime_networks(compose, containers)
                postiz = next(item for item in containers if item["Name"] == "/postiz")
                postiz["State"][field] = value
                self.rejected(
                    compose=compose,
                    containers=containers,
                    networks=networks,
                    runtime_state="writer-fenced",
                )

    def test_network_resource_and_endpoint_identity_uniqueness_fail_closed(self) -> None:
        compose = live_v53_compose_shape()
        compose["networks"]["postiz-internal"]["name"] = "dokploy-network"
        self.rejected(compose=compose)

        compose = live_v53_compose_shape()
        containers = runtime_containers(compose)
        networks = runtime_networks(compose, containers)
        compose["networks"]["unused"] = {
            "name": "postiz_unused",
            "driver": "bridge",
            "ipam": {},
        }
        self.rejected(compose=compose, containers=containers, networks=networks)

        compose = live_v53_compose_shape()
        containers = runtime_containers(compose)
        networks = runtime_networks(compose, containers)
        networks[1]["Id"] = networks[0]["Id"]
        self.rejected(compose=compose, containers=containers, networks=networks)

        containers = runtime_containers(compose)
        networks = runtime_networks(compose, containers)
        postiz = next(item for item in containers if item["Name"] == "/postiz")
        external = next(item for item in networks if item["Name"] == "dokploy-network")
        internal = next(item for item in networks if item["Name"] == "postiz_postiz-internal")
        duplicate_endpoint_id = external["Containers"][postiz["Id"]]["EndpointID"]
        internal["Containers"][postiz["Id"]]["EndpointID"] = duplicate_endpoint_id
        postiz["NetworkSettings"]["Networks"]["postiz_postiz-internal"][
            "EndpointID"
        ] = duplicate_endpoint_id
        self.rejected(compose=compose, containers=containers, networks=networks)

        containers = runtime_containers(compose)
        networks = runtime_networks(compose, containers)
        internal = next(item for item in networks if item["Name"] == "postiz_postiz-internal")
        postgres = next(item for item in containers if item["Name"] == "/postiz-postgres")
        redis = next(item for item in containers if item["Name"] == "/postiz-redis")
        postgres_endpoint = internal["Containers"][postgres["Id"]]
        redis_endpoint = internal["Containers"][redis["Id"]]
        redis_endpoint["IPv4Address"] = postgres_endpoint["IPv4Address"]
        redis_attachment = redis["NetworkSettings"]["Networks"]["postiz_postiz-internal"]
        redis_attachment["IPAddress"] = postgres["NetworkSettings"]["Networks"][
            "postiz_postiz-internal"
        ]["IPAddress"]
        self.rejected(compose=compose, containers=containers, networks=networks)

        containers = runtime_containers(compose)
        networks = runtime_networks(compose, containers)
        internal = next(item for item in networks if item["Name"] == "postiz_postiz-internal")
        postgres = next(item for item in containers if item["Name"] == "/postiz-postgres")
        redis = next(item for item in containers if item["Name"] == "/postiz-redis")
        duplicate_mac = internal["Containers"][postgres["Id"]]["MacAddress"]
        internal["Containers"][redis["Id"]]["MacAddress"] = duplicate_mac
        redis["NetworkSettings"]["Networks"]["postiz_postiz-internal"][
            "MacAddress"
        ] = duplicate_mac
        self.rejected(compose=compose, containers=containers, networks=networks)

    def test_network_ipam_gateway_overlap_and_endpoint_bounds_fail_closed(self) -> None:
        compose = live_v53_compose_shape()

        for gateway in (
            None,
            "not-an-ip",
            "172.30.1.1",
            "172.30.0.0",
            "172.30.0.255",
        ):
            with self.subTest(gateway=gateway):
                containers = runtime_containers(compose)
                networks = runtime_networks(compose, containers)
                internal = next(
                    item for item in networks if item["Name"] == "postiz_postiz-internal"
                )
                if gateway is None:
                    del internal["IPAM"]["Config"][0]["Gateway"]
                else:
                    internal["IPAM"]["Config"][0]["Gateway"] = gateway
                self.rejected(compose=compose, containers=containers, networks=networks)

        containers = runtime_containers(compose)
        networks = runtime_networks(compose, containers)
        internal = next(item for item in networks if item["Name"] == "postiz_postiz-internal")
        postgres = next(item for item in containers if item["Name"] == "/postiz-postgres")
        internal["Containers"][postgres["Id"]]["IPv4Address"] = "not-an-ip"
        self.rejected(compose=compose, containers=containers, networks=networks)

        containers = runtime_containers(compose)
        networks = runtime_networks(compose, containers)
        internal = next(item for item in networks if item["Name"] == "postiz_postiz-internal")
        internal["IPAM"]["Config"][0].update(
            {"Subnet": "172.30.0.0/15", "Gateway": "172.30.0.1"}
        )
        self.rejected(compose=compose, containers=containers, networks=networks)

        for endpoint_ip in ("172.30.0.0", "172.30.0.1", "172.30.0.255"):
            with self.subTest(endpoint_ip=endpoint_ip):
                containers = runtime_containers(compose)
                networks = runtime_networks(compose, containers)
                internal = next(
                    item for item in networks if item["Name"] == "postiz_postiz-internal"
                )
                postgres = next(
                    item for item in containers if item["Name"] == "/postiz-postgres"
                )
                internal["Containers"][postgres["Id"]]["IPv4Address"] = (
                    f"{endpoint_ip}/24"
                )
                postgres["NetworkSettings"]["Networks"]["postiz_postiz-internal"][
                    "IPAddress"
                ] = endpoint_ip
                self.rejected(compose=compose, containers=containers, networks=networks)

        containers = runtime_containers(compose)
        networks = runtime_networks(compose, containers)
        internal = next(item for item in networks if item["Name"] == "postiz_postiz-internal")
        postgres = next(item for item in containers if item["Name"] == "/postiz-postgres")
        ip_address = postgres["NetworkSettings"]["Networks"]["postiz_postiz-internal"][
            "IPAddress"
        ]
        internal["Containers"][postgres["Id"]]["IPv4Address"] = f"{ip_address}/16"
        postgres["NetworkSettings"]["Networks"]["postiz_postiz-internal"][
            "IPPrefixLen"
        ] = 16
        self.rejected(compose=compose, containers=containers, networks=networks)

    def test_source_resolved_and_no_deps_hash_roles_fail_closed(self) -> None:
        resolved = {
            **LIVE_V53_FULL_HASHES,
            "postiz": LIVE_V53_POSTIZ_NO_DEPS_HASH,
        }
        self.rejected(effective_hash="0" * 64, resolved_hashes=resolved)
        self.rejected(
            full_hashes={
                **LIVE_V53_FULL_HASHES,
                "postiz": LIVE_V53_POSTIZ_NO_DEPS_HASH,
            },
            resolved_hashes=resolved,
        )
        self.rejected(
            resolved_hashes={
                **resolved,
                "postiz-redis": "0" * 64,
            }
        )

    def test_label_drift_fails_closed_for_postiz_and_other_three(self) -> None:
        compose = live_v53_compose_shape()
        containers = runtime_containers(compose)
        postiz = next(item for item in containers if item["Name"] == "/postiz")
        postiz["Config"]["Labels"]["com.docker.compose.config-hash"] = (
            LIVE_V53_FULL_HASHES["postiz"]
        )
        self.rejected(compose=compose, containers=containers)

        containers = runtime_containers(compose)
        postiz = next(item for item in containers if item["Name"] == "/postiz")
        postiz["Config"]["Labels"]["com.docker.compose.depends_on"] = (
            "postiz-postgres:service_started:false"
        )
        self.rejected(compose=compose, containers=containers)

        for service in ("postiz-postgres", "postiz-redis", "postiz-temporal"):
            with self.subTest(service=service):
                containers = runtime_containers(compose)
                target = next(item for item in containers if item["Name"] == f"/{service}")
                target["Config"]["Labels"]["com.docker.compose.config-hash"] = "0" * 64
                self.rejected(compose=compose, containers=containers)

    def test_projection_removes_only_exact_postiz_depends_on(self) -> None:
        compose = live_v53_compose_shape()
        source = self.base / "full.json"
        output = self.base / "no-deps.json"
        source.write_text(json.dumps(compose), encoding="utf-8")
        subprocess.run(
            [
                str(MODULE_PATH),
                "write-compose-no-deps-model",
                "--compose-json",
                str(source),
                "--output",
                str(output),
            ],
            check=True,
        )
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), no_deps_shape(compose))
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

        changed_environment = no_deps_shape(compose)
        changed_environment["services"]["postiz"]["environment"]["GENERATION"] = "drift"
        self.rejected(compose=compose, effective=changed_environment)
        changed_other_service = no_deps_shape(compose)
        changed_other_service["services"]["postiz-redis"]["restart"] = "no"
        self.rejected(compose=compose, effective=changed_other_service)
        self.rejected(compose=compose, effective=compose)
        compose["services"]["postiz"]["depends_on"]["postiz-redis"]["required"] = False
        self.rejected(compose=compose, effective=no_deps_shape(compose))

    def test_environment_and_configured_image_drift_fail_closed(self) -> None:
        compose = live_v53_compose_shape()

        containers = runtime_containers(compose)
        postiz = next(item for item in containers if item["Name"] == "/postiz")
        postiz["Config"]["Env"].append("UNEXPECTED=extra")
        self.rejected(compose=compose, containers=containers)

        containers = runtime_containers(compose)
        postiz = next(item for item in containers if item["Name"] == "/postiz")
        postiz["Config"]["Env"].remove("IMAGE_DEFAULT=from-postiz-image")
        self.rejected(compose=compose, containers=containers)

        containers = runtime_containers(compose)
        postiz = next(item for item in containers if item["Name"] == "/postiz")
        postiz["Config"]["Env"][0] = "IMAGE_DEFAULT=runtime-override"
        self.rejected(compose=compose, containers=containers)

        containers = runtime_containers(compose)
        postiz = next(item for item in containers if item["Name"] == "/postiz")
        generation_index = postiz["Config"]["Env"].index("GENERATION=fixture-v5")
        postiz["Config"]["Env"][generation_index] = "GENERATION=from-postiz-image"
        self.rejected(compose=compose, containers=containers)

        containers = runtime_containers(compose)
        postiz = next(item for item in containers if item["Name"] == "/postiz")
        postiz["Config"]["Env"].append("GENERATION=fixture-v5")
        self.rejected(compose=compose, containers=containers)

        images = runtime_images()
        images[0]["Config"]["Env"].append(images[0]["Config"]["Env"][0])
        self.rejected(images=images)

        containers = runtime_containers(compose)
        redis = next(item for item in containers if item["Name"] == "/postiz-redis")
        redis["Config"]["Image"] = "fixture.invalid/redis:drift"
        self.rejected(compose=compose, containers=containers)

    def test_container_and_image_inspect_identity_drift_fail_closed(self) -> None:
        compose = live_v53_compose_shape()
        containers = runtime_containers(compose)
        postiz = next(item for item in containers if item["Name"] == "/postiz")
        postiz["Image"] = f"sha256:{'0' * 64}"
        self.rejected(compose=compose, containers=containers)

        containers = runtime_containers(compose)
        postiz = next(item for item in containers if item["Name"] == "/postiz")
        postiz["Id"] = "bogus"
        self.rejected(compose=compose, containers=containers)

        containers = runtime_containers(compose)
        containers[1]["Id"] = containers[0]["Id"]
        self.rejected(compose=compose, containers=containers)

        containers = runtime_containers(compose)
        containers[0]["Image"], containers[1]["Image"] = (
            containers[1]["Image"],
            containers[0]["Image"],
        )
        self.rejected(compose=compose, containers=containers)

        images = runtime_images()
        images[0]["Id"] = "bogus"
        self.rejected(images=images)

        images = runtime_images()
        images[1]["Id"] = images[0]["Id"]
        self.rejected(images=images)

        self.rejected(images=runtime_images()[:-1])

        images = runtime_images()
        del images[0]["Config"]["Env"]
        self.rejected(images=images)

        expected_images = dict(LIVE_IMAGE_IDS)
        expected_images["postiz"], expected_images["postiz-redis"] = (
            expected_images["postiz-redis"],
            expected_images["postiz"],
        )
        self.rejected(expected_images=expected_images)

        expected_images = dict(LIVE_IMAGE_IDS)
        del expected_images["postiz-temporal"]
        self.rejected(expected_images=expected_images)

    def test_network_endpoint_and_project_drift_fail_closed(self) -> None:
        compose = live_v53_compose_shape()

        containers = runtime_containers(compose)
        postiz = next(item for item in containers if item["Name"] == "/postiz")
        postiz["Config"]["Labels"]["com.docker.compose.project"] = "drift"
        self.rejected(compose=compose, containers=containers)

        containers = runtime_containers(compose)
        networks = runtime_networks(compose, containers)
        postiz = next(item for item in containers if item["Name"] == "/postiz")
        postiz["NetworkSettings"]["Networks"]["dokploy-network"]["Aliases"] = ["postiz"]
        self.rejected(compose=compose, containers=containers, networks=networks)

        containers = runtime_containers(compose)
        networks = runtime_networks(compose, containers)
        postiz = next(item for item in containers if item["Name"] == "/postiz")
        postiz["NetworkSettings"]["Networks"]["dokploy-network"]["IPAddress"] = (
            "172.31.0.99"
        )
        self.rejected(compose=compose, containers=containers, networks=networks)

        containers = runtime_containers(compose)
        networks = runtime_networks(compose, containers)
        postiz = next(item for item in containers if item["Name"] == "/postiz")
        postiz["NetworkSettings"]["Networks"]["dokploy-network"]["IPAddress"] = "10.0.0.9"
        external = next(item for item in networks if item["Name"] == "dokploy-network")
        external["Containers"][postiz["Id"]]["IPv4Address"] = "10.0.0.9/24"
        self.rejected(compose=compose, containers=containers, networks=networks)

        containers = runtime_containers(compose)
        networks = runtime_networks(compose, containers)
        postiz = next(item for item in containers if item["Name"] == "/postiz")
        attachment = postiz["NetworkSettings"]["Networks"]["dokploy-network"]
        attachment["GlobalIPv6Address"] = "fd00::10"
        attachment["GlobalIPv6PrefixLen"] = 64
        external = next(item for item in networks if item["Name"] == "dokploy-network")
        external["Containers"][postiz["Id"]]["IPv6Address"] = "fd00::10/64"
        self.rejected(compose=compose, containers=containers, networks=networks)

        containers = runtime_containers(compose)
        networks = runtime_networks(compose, containers)
        postiz = next(item for item in containers if item["Name"] == "/postiz")
        postiz["NetworkSettings"]["Networks"]["dokploy-network"]["MacAddress"] = (
            "02:42:ac:1f:00:ff"
        )
        self.rejected(compose=compose, containers=containers, networks=networks)

        containers = runtime_containers(compose)
        networks = runtime_networks(compose, containers)
        internal = next(item for item in networks if item["Name"] == "postiz_postiz-internal")
        internal["Containers"][hashlib.sha256(b"unexpected-member").hexdigest()] = {
            "Name": "unexpected-member"
        }
        self.rejected(compose=compose, containers=containers, networks=networks)

    def test_network_mount_and_port_topology_drift_fail_closed(self) -> None:
        compose = live_v53_compose_shape()
        containers = runtime_containers(compose)
        postiz = next(item for item in containers if item["Name"] == "/postiz")
        postiz["NetworkSettings"]["Networks"]["unexpected"] = {"Aliases": ["postiz"]}
        self.rejected(compose=compose, containers=containers)

        containers = runtime_containers(compose)
        postgres = next(item for item in containers if item["Name"] == "/postiz-postgres")
        postgres["Mounts"][0]["Name"] = "unexpected-volume"
        self.rejected(compose=compose, containers=containers)

        containers = runtime_containers(compose)
        temporal = next(item for item in containers if item["Name"] == "/postiz-temporal")
        temporal["HostConfig"]["PortBindings"] = {"7233/tcp": [{"HostPort": "7233"}]}
        self.rejected(compose=compose, containers=containers)

    def test_docker_compose_v5_cli_hashes_source_resolved_and_projection(self) -> None:
        docker = shutil.which("docker")
        if docker is None:
            self.skipTest("docker CLI is unavailable")
        version = subprocess.run(
            [docker, "compose", "version", "--short"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if version.returncode != 0 or not re.fullmatch(r"v?5\.[0-9]+\.[0-9]+", version.stdout.strip()):
            self.skipTest("Docker Compose v5 CLI is unavailable")
        fixture = ROOT / "tests/postiz_backup/fixtures/postiz-compose-v5.yml"
        env_file = ROOT / "tests/postiz_backup/fixtures/postiz-compose-v5.env.example"
        full_path = self.base / "full.json"
        projection_path = self.base / "projection.json"
        effective_path = self.base / "effective.json"
        full_path.write_text(
            subprocess.check_output(
                [
                    docker,
                    "compose",
                    "--env-file",
                    str(env_file),
                    "-f",
                    str(fixture),
                    "config",
                    "--format",
                    "json",
                ],
                text=True,
            ),
            encoding="utf-8",
        )
        manifest_module.command_write_compose_no_deps_model(
            Namespace(compose_json=str(full_path), output=str(projection_path))
        )
        effective_path.write_text(
            subprocess.check_output(
                [docker, "compose", "-f", str(projection_path), "config", "--format", "json"],
                text=True,
            ),
            encoding="utf-8",
        )
        full_hash_lines = subprocess.check_output(
            [
                docker,
                "compose",
                "--env-file",
                str(env_file),
                "-f",
                str(fixture),
                "config",
                "--hash",
                "*",
            ],
            text=True,
        )
        resolved_hash_lines = subprocess.check_output(
            [docker, "compose", "-f", str(full_path), "config", "--hash", "*"],
            text=True,
        )
        effective_hash_line = subprocess.check_output(
            [docker, "compose", "-f", str(projection_path), "config", "--hash", "postiz"],
            text=True,
        )
        full_hashes = dict(line.split() for line in full_hash_lines.splitlines())
        resolved_hashes = dict(line.split() for line in resolved_hash_lines.splitlines())
        effective_service, effective_hash = effective_hash_line.split()
        self.assertEqual(effective_service, "postiz")
        self.assertNotEqual(full_hashes["postiz"], resolved_hashes["postiz"])
        self.assertEqual(resolved_hashes["postiz"], effective_hash)
        for service in manifest_module.JOURNAL_SERVICES - {"postiz"}:
            self.assertEqual(full_hashes[service], resolved_hashes[service])
        full = json.loads(full_path.read_text(encoding="utf-8"))
        effective = json.loads(effective_path.read_text(encoding="utf-8"))
        self.assertEqual(effective, manifest_module._postiz_no_deps_projection(full))


if __name__ == "__main__":
    unittest.main(verbosity=2)
