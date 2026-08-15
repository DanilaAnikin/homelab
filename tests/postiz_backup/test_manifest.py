from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from argparse import Namespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "postiz-backup-manifest.py"
SPEC = importlib.util.spec_from_file_location("postiz_backup_manifest", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
manifest_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manifest_module)


class UploadManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.uploads = self.base / "uploads"
        self.uploads.mkdir(mode=0o755)
        (self.uploads / "2026").mkdir(mode=0o755)
        (self.uploads / "2026" / "alpha.png").write_bytes(b"alpha")
        (self.uploads / "video.mp4").write_bytes(b"video-data")
        for path in self.uploads.rglob("*"):
            os.chmod(path, 0o755 if path.is_dir() else 0o644)
        self.manifest = self.base / "manifest.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def scan(self) -> dict[str, object]:
        manifest_module.command_scan(
            Namespace(
                root=str(self.uploads),
                timestamp="20260814T120000Z",
                output=str(self.manifest),
                expected_uid=os.getuid(),
                expected_gid=os.getgid(),
                max_files=100,
                max_bytes=1024,
            )
        )
        return json.loads(self.manifest.read_text(encoding="utf-8"))

    def test_scan_is_canonical_and_source_sealed(self) -> None:
        value = self.scan()
        self.assertEqual(value["schema"], manifest_module.UPLOAD_SCHEMA)
        self.assertEqual(value["file_count"], 2)
        self.assertEqual(value["total_bytes"], 15)
        self.assertEqual([entry["path"] for entry in value["entries"]], ["2026/alpha.png", "video.mp4"])
        self.assertEqual(stat.S_IMODE(self.manifest.stat().st_mode), 0o600)
        manifest_module.command_verify_source(
            Namespace(
                root=str(self.uploads),
                manifest=str(self.manifest),
                expected_uid=os.getuid(),
                expected_gid=os.getgid(),
                max_files=100,
                max_bytes=1024,
            )
        )

    def test_source_mutation_is_detected_after_scan(self) -> None:
        self.scan()
        target = self.uploads / "video.mp4"
        target.write_bytes(b"changed")
        os.chmod(target, 0o644)
        with self.assertRaises(manifest_module.ContractError):
            manifest_module.command_verify_source(
                Namespace(
                    root=str(self.uploads),
                    manifest=str(self.manifest),
                    expected_uid=os.getuid(),
                    expected_gid=os.getgid(),
                    max_files=100,
                    max_bytes=1024,
                )
            )

    def test_unsafe_name_symlink_and_mode_are_rejected(self) -> None:
        bad = self.uploads / "unsafe name.png"
        bad.write_bytes(b"bad")
        os.chmod(bad, 0o644)
        with self.assertRaises(manifest_module.ContractError):
            self.scan()
        bad.unlink()
        link = self.uploads / "link.png"
        link.symlink_to("video.mp4")
        with self.assertRaises(manifest_module.ContractError):
            self.scan()
        link.unlink()
        outside = self.base / "outside"
        outside.mkdir()
        directory_link = self.uploads / "directory-link"
        directory_link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(manifest_module.ContractError):
            self.scan()
        directory_link.unlink()
        os.chmod(self.uploads / "video.mp4", 0o666)
        with self.assertRaises(manifest_module.ContractError):
            self.scan()

    def test_exact_cipher_and_restored_trees_are_verified(self) -> None:
        value = self.scan()
        cipher_root = self.base / "cipher"
        restored_root = self.base / "restored"
        cipher_root.mkdir()
        restored_root.mkdir(mode=0o755)
        # tempfile's parent umask may narrow mkdir(mode); the restore contract is exact.
        os.chmod(restored_root, 0o755)
        for entry in value["entries"]:
            digest = entry["sha256"]
            cipher = cipher_root / digest[:2] / f"{digest}.enc"
            cipher.parent.mkdir(exist_ok=True)
            cipher.write_bytes(b"0" * manifest_module._cipher_size(entry["size"]))
            restored = restored_root / entry["path"]
            restored.parent.mkdir(parents=True, exist_ok=True)
            if restored.parent != restored_root:
                os.chmod(restored.parent, 0o755)
            restored.write_bytes((self.uploads / entry["path"]).read_bytes())
            os.chmod(restored, 0o644)
        manifest_module.command_verify_cipher_tree(
            Namespace(manifest=str(self.manifest), root=str(cipher_root))
        )
        manifest_module.command_verify_restored(
            Namespace(
                manifest=str(self.manifest),
                root=str(restored_root),
                expected_uid=os.getuid(),
                expected_gid=os.getgid(),
            )
        )
        (restored_root / "video.mp4").write_bytes(b"tampered")
        os.chmod(restored_root / "video.mp4", 0o644)
        with self.assertRaises(manifest_module.ContractError):
            manifest_module.command_verify_restored(
                Namespace(
                    manifest=str(self.manifest),
                    root=str(restored_root),
                    expected_uid=os.getuid(),
                    expected_gid=os.getgid(),
                )
            )


class ArchiveContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_offline_runtime_allowlist_matches_archive_contract_exactly(self) -> None:
        offline = (ROOT / "self-healing/postiz-offline-verify.sh").read_text(encoding="utf-8")
        matched = re.search(r"expected_config='([^']+)'\nactual_config=", offline)
        self.assertIsNotNone(matched)
        assert matched is not None
        self.assertEqual(set(matched.group(1).splitlines()), manifest_module.EXPECTED_CONFIG_MEMBERS)

    def _config_archive(
        self,
        *,
        extra: bool = False,
        env_mode: int = 0o600,
        oversized_member: str | None = None,
    ) -> tuple[Path, str, str]:
        archive = self.base / "config.tar.gz"
        payloads = {name: b"fixture\n" for name in manifest_module.EXPECTED_CONFIG_MEMBERS}
        payloads.update(
            {
                "etc/homelab/postiz-backup-source-revision": b"a" * 40 + b"\n",
                "srv/postiz/postiz.env": b"DATABASE_URL=private\nJWT_SECRET=private\n",
                "srv/postiz/docker-compose.yml": b"services: {}\n",
                "srv/postiz/Dockerfile.patch": b"FROM pinned@sha256:abc\n",
                "srv/postiz/schedule-week.py": b"#!/usr/bin/env python3\n",
            }
        )
        if extra:
            payloads["srv/postiz/unexpected"] = b"x"
        if oversized_member is not None:
            payloads[oversized_member] = b"x" * (
                manifest_module.MAX_CONFIG_ARCHIVE_MEMBER_BYTES + 1
            )
        with tarfile.open(archive, "w:gz") as bundle:
            for name, content in payloads.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.uid = 0
                info.gid = 0
                if name.endswith("postiz.env"):
                    info.mode = env_mode
                elif name in manifest_module.EXPECTED_EXECUTABLE_CONFIG_MEMBERS:
                    info.mode = 0o755
                else:
                    info.mode = 0o644
                bundle.addfile(info, io.BytesIO(content))
        return (
            archive,
            hashlib.sha256(payloads["srv/postiz/docker-compose.yml"]).hexdigest(),
            hashlib.sha256(payloads["srv/postiz/Dockerfile.patch"]).hexdigest(),
        )

    def test_config_archive_is_exact_and_hash_bound(self) -> None:
        archive, compose_sha, dockerfile_sha = self._config_archive()
        manifest_module.command_verify_config_archive(
            Namespace(
                archive=str(archive),
                compose_sha256=compose_sha,
                dockerfile_sha256=dockerfile_sha,
            )
        )
        with self.assertRaises(manifest_module.ContractError):
            manifest_module.command_verify_config_archive(
                Namespace(
                    archive=str(archive),
                    compose_sha256="0" * 64,
                    dockerfile_sha256=dockerfile_sha,
                )
            )

    def test_config_archive_rejects_extra_member_and_weak_env_mode(self) -> None:
        archive, _, _ = self._config_archive(extra=True)
        with self.assertRaises(manifest_module.ContractError):
            manifest_module.command_verify_config_archive(
                Namespace(archive=str(archive), compose_sha256=None, dockerfile_sha256=None)
            )
        archive, _, _ = self._config_archive(env_mode=0o644)
        with self.assertRaises(manifest_module.ContractError):
            manifest_module.command_verify_config_archive(
                Namespace(archive=str(archive), compose_sha256=None, dockerfile_sha256=None)
            )

    def test_config_archive_rejects_compressible_expanded_member_bomb(self) -> None:
        archive, _, _ = self._config_archive(oversized_member="srv/postiz/postiz.env")
        self.assertLess(archive.stat().st_size, 1024 * 1024)
        with self.assertRaises(manifest_module.ContractError):
            manifest_module.command_verify_config_archive(
                Namespace(archive=str(archive), compose_sha256=None, dockerfile_sha256=None)
            )

    def test_physical_archive_is_streamed_and_member_bounded(self) -> None:
        archive = self.base / "physical.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            for name, content in (
                ("PG_VERSION", b"17\n"),
                ("backup_manifest", b"{}\n"),
            ):
                info = tarfile.TarInfo(name)
                info.size = len(content)
                bundle.addfile(info, io.BytesIO(content))
        manifest_module.command_verify_physical_archive(
            Namespace(archive=str(archive), max_bytes=1024, max_members=2)
        )

        over_member_cap = self.base / "physical-too-many.tar.gz"
        with tarfile.open(over_member_cap, "w:gz") as bundle:
            for name, content in (
                ("PG_VERSION", b"17\n"),
                ("backup_manifest", b"{}\n"),
                ("base/1", b"x"),
            ):
                info = tarfile.TarInfo(name)
                info.size = len(content)
                bundle.addfile(info, io.BytesIO(content))
        with self.assertRaises(manifest_module.ContractError):
            manifest_module.command_verify_physical_archive(
                Namespace(archive=str(over_member_cap), max_bytes=1024, max_members=2)
            )

    def test_compose_runtime_binds_list_root_hash_image_environment_and_fenced_name(self) -> None:
        services = sorted(manifest_module.JOURNAL_SERVICES)
        compose = {
            "name": "postiz",
            "services": {
                service: {
                    "container_name": service,
                    "image": f"fixture/{service}:pinned",
                    "environment": {"GENERATION": "g1"},
                    "networks": (
                        {"dokploy-network": None, "postiz-internal": None}
                        if service == "postiz"
                        else {"postiz-internal": None}
                    ),
                }
                for service in services
            },
            "networks": {
                "dokploy-network": {
                    "name": "dokploy-network",
                    "external": True,
                },
                "postiz-internal": {
                    "name": "postiz_postiz-internal",
                    "driver": "bridge",
                },
            },
            "volumes": {},
        }
        compose["services"]["postiz"]["depends_on"] = copy.deepcopy(
            manifest_module.POSTIZ_NO_DEPS_DEPENDENCIES
        )
        no_deps_compose = copy.deepcopy(compose)
        del no_deps_compose["services"]["postiz"]["depends_on"]
        hashes = {service: hashlib.sha256(service.encode()).hexdigest() for service in services}
        no_deps_hash = hashlib.sha256(b"postiz-no-deps").hexdigest()
        resolved_hashes = {**hashes, "postiz": no_deps_hash}
        container_ids = {
            service: hashlib.sha256(f"container:{service}".encode()).hexdigest()
            for service in services
        }
        image_ids = {
            service: f"sha256:{hashlib.sha256(f'image:{service}'.encode()).hexdigest()}"
            for service in services
        }
        images = [
            {
                "Id": image_ids[service],
                "Config": {"Env": ["IMAGE_DEFAULT=allowed-default"]},
            }
            for service in services
        ]
        network_ids = {
            "dokploy-network": "u02n58elmfvy9ykgyek8m0g23",
            "postiz_postiz-internal": hashlib.sha256(
                b"network:postiz-internal"
            ).hexdigest(),
        }
        network_members = {
            "dokploy-network": {
                "lb-dokploy-network": {
                    "Name": "dokploy-network-endpoint",
                    "EndpointID": hashlib.sha256(b"overlay-lb-endpoint").hexdigest(),
                    "MacAddress": "02:42:0a:00:01:06",
                    "IPv4Address": "10.0.1.6/24",
                    "IPv6Address": "",
                }
            },
            "postiz_postiz-internal": {},
        }
        containers = []
        for index, service in enumerate(services, 10):
            name = "/postiz-postgres-backup-fenced" if service == "postiz-postgres" else f"/{service}"
            running = service == "postiz-postgres"
            network_names = (
                ("dokploy-network", "postiz_postiz-internal")
                if service == "postiz"
                else ("postiz_postiz-internal",)
            )
            attachments = {}
            for network_name in network_names:
                overlay = network_name == "dokploy-network"
                endpoint_id = hashlib.sha256(
                    f"endpoint:{network_name}:{service}".encode()
                ).hexdigest()
                ip_address = "10.0.1.99" if overlay else f"10.77.0.{index}"
                mac_address = (
                    "02:42:0a:00:01:63"
                    if overlay
                    else f"02:42:0a:4d:00:{index:02x}"
                )
                attachments[network_name] = {
                    "Aliases": [service, service],
                    "NetworkID": network_ids[network_name],
                    "EndpointID": endpoint_id if running else "",
                    "Gateway": (
                        "" if overlay or not running else "10.77.0.1"
                    ),
                    "IPAddress": ip_address if running else "",
                    "IPPrefixLen": 24 if running else 0,
                    "IPv6Gateway": "",
                    "GlobalIPv6Address": "",
                    "GlobalIPv6PrefixLen": 0,
                    "MacAddress": mac_address if running else "",
                }
                if running:
                    network_members[network_name][container_ids[service]] = {
                        "Name": name.removeprefix("/"),
                        "EndpointID": endpoint_id,
                        "MacAddress": mac_address,
                        "IPv4Address": f"{ip_address}/24",
                        "IPv6Address": "",
                    }
            containers.append(
                {
                    "Id": container_ids[service],
                    "Image": image_ids[service],
                    "Name": name,
                    "State": {
                        "Status": "running" if running else "exited",
                        "Running": running,
                        "Paused": False,
                        "Restarting": False,
                        "Dead": False,
                        "ExitCode": 0,
                        "FinishedAt": (
                            manifest_module.DOCKER_ZERO_TIME
                            if running
                            else "2026-08-15T08:30:00.123456789Z"
                        ),
                    },
                    "Config": {
                        "Image": f"fixture/{service}:pinned",
                        "Env": ["GENERATION=g1", "IMAGE_DEFAULT=allowed-default"],
                        "Labels": {
                            "com.docker.compose.project": "postiz",
                            "com.docker.compose.service": service,
                            "com.docker.compose.config-hash": (
                                no_deps_hash if service == "postiz" else hashes[service]
                            ),
                            "com.docker.compose.depends_on": "" if service == "postiz" else "fixture",
                        },
                    },
                    "HostConfig": {"PortBindings": {}},
                    "Mounts": [],
                    "NetworkSettings": {"Networks": attachments},
                }
            )
        networks = [
            {
                "Name": network_name,
                "Id": network_ids[network_name],
                "Driver": "overlay" if network_name == "dokploy-network" else "bridge",
                "Scope": "swarm" if network_name == "dokploy-network" else "local",
                "Internal": False,
                "Attachable": network_name == "dokploy-network",
                "Ingress": False,
                "ConfigOnly": False,
                "ConfigFrom": {"Network": ""},
                "EnableIPv4": True,
                "EnableIPv6": False,
                "Options": (
                    {"com.docker.network.driver.overlay.vxlanid_list": "4097"}
                    if network_name == "dokploy-network"
                    else {}
                ),
                "IPAM": {
                    "Driver": "default",
                    "Options": None,
                    "Config": [
                        {
                            "Subnet": (
                                "10.0.1.0/24"
                                if network_name == "dokploy-network"
                                else "10.77.0.0/24"
                            ),
                            "Gateway": (
                                "10.0.1.1"
                                if network_name == "dokploy-network"
                                else "10.77.0.1"
                            ),
                        }
                    ],
                },
                "Containers": network_members[network_name],
            }
            for network_name in ("dokploy-network", "postiz_postiz-internal")
        ]
        compose_path = self.base / "compose.json"
        no_deps_compose_path = self.base / "compose-no-deps.json"
        container_path = self.base / "containers.json"
        image_path = self.base / "images.json"
        network_path = self.base / "networks.json"
        hash_path = self.base / "hashes.txt"
        resolved_hash_path = self.base / "resolved-hashes.txt"
        no_deps_hash_path = self.base / "no-deps-hash.txt"
        compose_path.write_text(json.dumps(compose))
        no_deps_compose_path.write_text(json.dumps(no_deps_compose))
        container_path.write_text(json.dumps(containers))
        image_path.write_text(json.dumps(images))
        network_path.write_text(json.dumps(networks))
        hash_path.write_text("".join(f"{service} {hashes[service]}\n" for service in services))
        resolved_hash_path.write_text(
            "".join(f"{service} {resolved_hashes[service]}\n" for service in services)
        )
        no_deps_hash_path.write_text(f"postiz {no_deps_hash}\n")
        args = Namespace(
            compose_json=str(compose_path),
            compose_hashes=str(hash_path),
            resolved_compose_hashes=str(resolved_hash_path),
            postiz_no_deps_compose_json=str(no_deps_compose_path),
            postiz_no_deps_hash=str(no_deps_hash_path),
            container_json=str(container_path),
            image_inspect_json=str(image_path),
            network_inspect_json=str(network_path),
            expected_image=[f"{service}|{image_ids[service]}" for service in services],
            runtime_state="writer-fenced",
        )
        manifest_module.command_verify_compose_runtime(args)
        containers[0]["Config"]["Labels"]["com.docker.compose.config-hash"] = "0" * 64
        container_path.write_text(json.dumps(containers))
        with self.assertRaises(manifest_module.ContractError):
            manifest_module.command_verify_compose_runtime(args)

    def test_content_addressed_docker_archive(self) -> None:
        layer_stream = io.BytesIO()
        with tarfile.open(fileobj=layer_stream, mode="w") as layer_bundle:
            directory = tarfile.TarInfo("usr/share/postiz")
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o755
            layer_bundle.addfile(directory)
            payload = b"canonical-layer-payload"
            file_info = tarfile.TarInfo("usr/share/postiz/recovery.txt")
            file_info.mode = 0o644
            file_info.size = len(payload)
            layer_bundle.addfile(file_info, io.BytesIO(payload))
        layer = layer_stream.getvalue()
        layer_digest = hashlib.sha256(layer).hexdigest()
        config = json.dumps(
            {
                "architecture": "amd64",
                "created": "2026-08-14T12:00:00Z",
                "config": {"Cmd": ["/usr/local/bin/postiz"]},
                "os": "linux",
                "rootfs": {"type": "layers", "diff_ids": [f"sha256:{layer_digest}"]},
            },
            separators=(",", ":"),
        ).encode()
        image_hex = hashlib.sha256(config).hexdigest()
        manifest = json.dumps(
            [{"Config": f"{image_hex}.json", "RepoTags": None, "Layers": ["layer.tar"]}],
            separators=(",", ":"),
        ).encode()
        archive = self.base / "image.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            for name, content in (("manifest.json", manifest), (f"{image_hex}.json", config), ("layer.tar", layer)):
                info = tarfile.TarInfo(name)
                info.mode = 0o600
                info.size = len(content)
                bundle.addfile(info, io.BytesIO(content))
        byte_stats = self.base / "image-bytes.txt"
        inode_stats = self.base / "image-inodes.txt"
        manifest_module.command_verify_image_archive(
            Namespace(
                archive=str(archive),
                image_id=f"sha256:{image_hex}",
                uncompressed_bytes_output=str(byte_stats),
                uncompressed_inodes_output=str(inode_stats),
            )
        )
        self.assertEqual(int(byte_stats.read_text()), len(layer))
        self.assertEqual(int(inode_stats.read_text()), 2)
        with self.assertRaises(manifest_module.ContractError):
            manifest_module.command_verify_image_archive(
                Namespace(archive=str(archive), image_id=f"sha256:{'0' * 64}")
            )

        corrupt = self.base / "corrupt-image.tar.gz"
        with tarfile.open(corrupt, "w:gz") as bundle:
            for name, content in (
                ("manifest.json", manifest),
                (f"{image_hex}.json", config),
                ("layer.tar", b"tampered-layer"),
            ):
                info = tarfile.TarInfo(name)
                info.mode = 0o600
                info.size = len(content)
                bundle.addfile(info, io.BytesIO(content))
        with self.assertRaises(manifest_module.ContractError):
            manifest_module.command_verify_image_archive(
                Namespace(archive=str(corrupt), image_id=f"sha256:{image_hex}")
            )

        tagged = self.base / "tagged-image.tar.gz"
        tagged_manifest = json.dumps(
            [{
                "Config": f"{image_hex}.json",
                "RepoTags": ["postiz:mutable"],
                "Layers": ["layer.tar"],
            }],
            separators=(",", ":"),
        ).encode()
        with tarfile.open(tagged, "w:gz") as bundle:
            for name, content in (
                ("manifest.json", tagged_manifest),
                (f"{image_hex}.json", config),
                ("layer.tar", layer),
            ):
                info = tarfile.TarInfo(name)
                info.mode = 0o600
                info.size = len(content)
                bundle.addfile(info, io.BytesIO(content))
        with self.assertRaises(manifest_module.ContractError):
            manifest_module.command_verify_image_archive(
                Namespace(archive=str(tagged), image_id=f"sha256:{image_hex}")
            )

        legacy_metadata = json.dumps(
            {
                "id": "f" * 64,
                "created": "2026-08-14T12:00:00Z",
                "container_config": {},
                "config": {
                    "Cmd": ["/usr/local/bin/postiz"],
                    "Entrypoint": None,
                    "OpenStdin": False,
                },
                "architecture": "amd64",
                "os": "linux",
            },
            separators=(",", ":"),
        ).encode()
        legacy_digest = hashlib.sha256(legacy_metadata).hexdigest()
        oci_manifest = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {
                    "mediaType": "application/vnd.oci.image.config.v1+json",
                    "digest": f"sha256:{image_hex}",
                    "size": len(config),
                },
                "layers": [{
                    "mediaType": "application/vnd.oci.image.layer.v1.tar",
                    "digest": f"sha256:{layer_digest}",
                    "size": len(layer),
                }],
            },
            separators=(",", ":"),
        ).encode()
        oci_manifest_digest = hashlib.sha256(oci_manifest).hexdigest()
        index = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [{
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": f"sha256:{oci_manifest_digest}",
                    "size": len(oci_manifest),
                }],
            },
            separators=(",", ":"),
        ).encode()
        descriptor = {
            "mediaType": "application/vnd.oci.image.layer.v1.tar",
            "digest": f"sha256:{layer_digest}",
            "size": len(layer),
        }
        hybrid_manifest = json.dumps(
            [{
                "Config": f"blobs/sha256/{image_hex}",
                "RepoTags": None,
                "Layers": [f"blobs/sha256/{layer_digest}"],
                "LayerSources": {f"sha256:{layer_digest}": descriptor},
            }],
            separators=(",", ":"),
        ).encode()
        hybrid = self.base / "docker29-hybrid.tar.gz"
        hybrid_payloads = {
            "blobs/sha256/" + image_hex: config,
            "blobs/sha256/" + layer_digest: layer,
            "blobs/sha256/" + legacy_digest: legacy_metadata,
            "blobs/sha256/" + oci_manifest_digest: oci_manifest,
            "index.json": index,
            "manifest.json": hybrid_manifest,
            "oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
        }
        with tarfile.open(hybrid, "w:gz") as bundle:
            for name in ("blobs", "blobs/sha256"):
                info = tarfile.TarInfo(name)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                bundle.addfile(info)
            for name, content in hybrid_payloads.items():
                info = tarfile.TarInfo(name)
                info.mode = 0o644
                info.size = len(content)
                bundle.addfile(info, io.BytesIO(content))
        manifest_module.command_verify_image_archive(
            Namespace(archive=str(hybrid), image_id=f"sha256:{image_hex}")
        )

        poisoned_hybrid = self.base / "docker29-hybrid-poisoned.tar.gz"
        with tarfile.open(poisoned_hybrid, "w:gz") as bundle:
            for name in ("blobs", "blobs/sha256"):
                info = tarfile.TarInfo(name)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                bundle.addfile(info)
            for name, content in {
                **hybrid_payloads,
                "blobs/sha256/" + hashlib.sha256(b"{}").hexdigest(): b"{}",
            }.items():
                info = tarfile.TarInfo(name)
                info.mode = 0o644
                info.size = len(content)
                bundle.addfile(info, io.BytesIO(content))
        with self.assertRaises(manifest_module.ContractError):
            manifest_module.command_verify_image_archive(
                Namespace(archive=str(poisoned_hybrid), image_id=f"sha256:{image_hex}")
            )

    def test_tree_archive_preserves_root_only_mode_and_exact_restore(self) -> None:
        root = self.base / "seasonal"
        root.mkdir(mode=0o700)
        nested = root / "release"
        nested.mkdir(mode=0o700)
        payload = nested / "receipt.json"
        payload.write_bytes(b'{"ok":true}\n')
        os.chmod(payload, 0o600)
        archive = self.base / "seasonal.tar.gz"
        outside = self.base / "outside"
        outside.mkdir()
        linked = root / "linked-directory"
        linked.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(manifest_module.ContractError):
            manifest_module.command_seal_tree_archive(
                Namespace(
                    root=str(root),
                    prefix="seasonal-releases",
                    output=str(archive),
                    expected_uid=os.getuid(),
                    expected_gid=os.getgid(),
                    max_bytes=1024,
                    max_members=10,
                )
            )
        linked.unlink()
        manifest_module.command_seal_tree_archive(
            Namespace(
                root=str(root),
                prefix="seasonal-releases",
                output=str(archive),
                expected_uid=os.getuid(),
                expected_gid=os.getgid(),
                max_bytes=1024,
                max_members=10,
            )
        )
        args = Namespace(
            archive=str(archive), prefix="seasonal-releases", max_bytes=1024, max_members=10
        )
        manifest_module.command_verify_tree_archive(args)
        with self.assertRaises(manifest_module.ContractError):
            manifest_module.command_verify_tree_archive(
                Namespace(
                    archive=str(archive),
                    prefix="seasonal-releases",
                    max_bytes=1024,
                    max_members=1,
                )
            )
        with tarfile.open(archive, "r:gz") as bundle:
            tree_manifest = json.loads(bundle.extractfile("freio-tree-manifest.json").read())
            self.assertEqual(tree_manifest["root_mode"], 0o700)
            self.assertTrue(all(member.uid == 0 and member.gid == 0 for member in bundle.getmembers()))
            restored_parent = self.base / "restored-seasonal"
            restored_parent.mkdir()
            bundle.extractall(restored_parent)
        manifest_module.command_verify_tree_restored(
            Namespace(
                archive=str(archive),
                prefix="seasonal-releases",
                root=str(restored_parent / "seasonal-releases"),
                expected_uid=os.getuid(),
                expected_gid=os.getgid(),
                max_bytes=1024,
                max_members=10,
            )
        )


class CommitAndPolicyContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @unittest.skipIf(os.geteuid() == 0, "non-privileged cleanup CLI fixture")
    def test_workspace_cleanup_cli_reaps_normal_directory_without_following_symlink(self) -> None:
        os.chmod(self.base, 0o700)
        state = self.base / "state"
        run = self.base / "run"
        state.mkdir(mode=0o700)
        run.mkdir(mode=0o700)
        lock = run / "nightly-workspace.lock"
        lock.write_bytes(b"")
        os.chmod(lock, 0o600)
        stale = state / "nightly.A1b2C3"
        stale.mkdir(mode=0o700)
        (stale / "plaintext-secret").write_text("fixture", encoding="utf-8")
        outside = self.base / "outside"
        outside.write_text("must remain", encoding="utf-8")
        (stale / "outside-link").symlink_to(outside)
        completed = subprocess.run(
            [
                shutil.which("bash") or "/bin/bash",
                str(ROOT / "scripts/postiz-backup-workspace-cleanup.sh"),
                "--scope",
                "nightly",
                "--test-root",
                str(self.base),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(stale.exists())
        self.assertEqual(outside.read_text(encoding="utf-8"), "must remain")
        inherited = state / "nightly.Z9y8X7"
        inherited.mkdir(mode=0o700)
        (inherited / "plaintext-secret").write_text("fixture", encoding="utf-8")
        held = subprocess.run(
            [
                shutil.which("bash") or "/bin/bash",
                "-c",
                'exec 7<>"$1"; flock -n 7; exec "$2" --scope nightly '
                '--lock-held-fd 7 --test-root "$3"',
                "held-cleanup",
                str(lock),
                str(ROOT / "scripts/postiz-backup-workspace-cleanup.sh"),
                str(self.base),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(held.returncode, 0, held.stderr)
        self.assertFalse(inherited.exists())

    def test_restore_journal_is_exact_and_durable(self) -> None:
        journal = self.base / "restore-journal.json"
        manifest_module.command_write_restore_journal(
            Namespace(
                timestamp="20260814T120000Z",
                run_id="Ab12z9",
                output=str(journal),
            )
        )
        value = manifest_module._validate_restore_journal(
            json.loads(journal.read_text(encoding="utf-8"))
        )
        self.assertEqual(
            value["work_directory"],
            "/var/lib/homelab-backup/postiz-restore.Ab12z9",
        )
        self.assertEqual(set(value["containers"]), manifest_module.RESTORE_JOURNAL_ROLES)
        self.assertEqual(stat.S_IMODE(journal.stat().st_mode), 0o600)
        value["containers"]["logical-primary"] = "unrelated-container"
        with self.assertRaises(manifest_module.ContractError):
            manifest_module._validate_restore_journal(value)

    def test_generic_restore_journal_is_exact_and_durable(self) -> None:
        journal = self.base / "generic-restore-journal.json"
        manifest_module.command_write_generic_restore_journal(
            Namespace(
                timestamp="20260814T120000Z",
                run_id="Z9ab12",
                output=str(journal),
            )
        )
        value = manifest_module._validate_generic_restore_journal(
            json.loads(journal.read_text(encoding="utf-8"))
        )
        self.assertEqual(
            value["work_directory"],
            "/var/lib/homelab-backup/restore-generic.Z9ab12",
        )
        self.assertEqual(value["container"], "generic-restore-Z9ab12-postgres")
        self.assertEqual(stat.S_IMODE(journal.stat().st_mode), 0o600)
        value["container"] = "pg-restore-drill"
        with self.assertRaises(manifest_module.ContractError):
            manifest_module._validate_generic_restore_journal(value)

    def test_shell_candidate_selection_preserves_authenticated_cipher_basename(self) -> None:
        key = self.base / "key"
        key.write_bytes(b"correct horse battery staple\n")
        os.chmod(key, 0o600)
        candidate = self.base / "candidate"
        primary = candidate / "primary"
        dr = candidate / "dr"
        primary.mkdir(parents=True)
        dr.mkdir(parents=True)
        cipher = primary / "recovery-set.json.enc"
        cipher.write_bytes(b"Salted__" + os.urandom(96))
        context = "postiz-recovery-set:20260814T120000Z"
        script = r'''
set -Eeuo pipefail
helper=$1
python=$2
key=$3
primary=$4
dr=$5
context=$6
"$python" "$helper" write-auth-record \
  --cipher "$primary/recovery-set.json.enc" --key-file "$key" \
  --context "$context" --output "$primary/COMMITTED.hmac.json"
cp "$primary/recovery-set.json.enc" "$dr/recovery-set.json.enc"
cp "$primary/COMMITTED.hmac.json" "$dr/COMMITTED.hmac.json"
cmp -s "$primary/recovery-set.json.enc" "$dr/recovery-set.json.enc"
cmp -s "$primary/COMMITTED.hmac.json" "$dr/COMMITTED.hmac.json"
for root in "$primary" "$dr"; do
  "$python" "$helper" verify-auth-record \
    --cipher "$root/recovery-set.json.enc" \
    --record "$root/COMMITTED.hmac.json" --key-file "$key" \
    --expected-context "$context"
done
cp "$primary/recovery-set.json.enc" "$primary/primary-marker.enc"
if "$python" "$helper" verify-auth-record \
    --cipher "$primary/primary-marker.enc" \
    --record "$primary/COMMITTED.hmac.json" --key-file "$key" \
    --expected-context "$context" >/dev/null 2>&1; then
  exit 91
fi
printf 'Salted__different-authenticated-ciphertext' > "$dr/recovery-set.json.enc"
"$python" "$helper" write-auth-record \
  --cipher "$dr/recovery-set.json.enc" --key-file "$key" \
  --context "$context" --output "$dr/COMMITTED.hmac.json"
if cmp -s "$primary/COMMITTED.hmac.json" "$dr/COMMITTED.hmac.json" && \
   cmp -s "$primary/recovery-set.json.enc" "$dr/recovery-set.json.enc" && \
   "$python" "$helper" verify-auth-record \
     --cipher "$primary/recovery-set.json.enc" \
     --record "$primary/COMMITTED.hmac.json" --key-file "$key" \
     --expected-context "$context" && \
   "$python" "$helper" verify-auth-record \
     --cipher "$dr/recovery-set.json.enc" \
     --record "$dr/COMMITTED.hmac.json" --key-file "$key" \
     --expected-context "$context"; then
  exit 92
fi
'''
        completed = subprocess.run(
            [
                shutil.which("bash") or "/bin/bash",
                "-c",
                script,
                "candidate-selection",
                str(MODULE_PATH),
                sys.executable,
                str(key),
                str(primary),
                str(dr),
                context,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_authenticated_commit_rejects_bitflip_truncation_wrong_key_and_replay(self) -> None:
        key = self.base / "key"
        wrong_key = self.base / "wrong-key"
        key.write_bytes(b"correct horse battery staple\n")
        wrong_key.write_bytes(b"another passphrase\n")
        os.chmod(key, 0o600)
        os.chmod(wrong_key, 0o600)
        cipher = self.base / "recovery-set.json.enc"
        original = b"Salted__" + os.urandom(96)
        cipher.write_bytes(original)
        record = self.base / "COMMITTED.hmac.json"
        context = "postiz-recovery-set:20260814T120000Z"
        manifest_module.command_write_auth_record(
            Namespace(cipher=str(cipher), key_file=str(key), context=context, output=str(record))
        )

        def verify(key_path: Path = key, expected: str = context) -> None:
            manifest_module.command_verify_auth_record(
                Namespace(
                    cipher=str(cipher),
                    record=str(record),
                    key_file=str(key_path),
                    expected_context=expected,
                )
            )

        verify()
        tampered = bytearray(original)
        tampered[20] ^= 1
        cipher.write_bytes(tampered)
        with self.assertRaises(manifest_module.ContractError):
            verify()
        cipher.write_bytes(original[:-1])
        with self.assertRaises(manifest_module.ContractError):
            verify()
        cipher.write_bytes(original)
        with self.assertRaises(manifest_module.ContractError):
            verify(wrong_key)
        with self.assertRaises(manifest_module.ContractError):
            verify(key, "postiz-recovery-set:20260815T120000Z")

    def storage_policy(self) -> dict[str, object]:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        def stamp(value: datetime) -> str:
            return value.strftime("%Y%m%dT%H%M%SZ")

        def remote(name: str, lock_seconds: int, lifecycle_seconds: int, account: str) -> dict[str, object]:
            return {
                "remote": name,
                "account_id_sha256": account,
                "credential": {
                    "bucket_only": True,
                    "cross_bucket_denied": True,
                    "object_read_write_includes_delete": True,
                    "policy_admin_denied": True,
                },
                "locks": {
                    "recovery_sets_max_age_seconds": lock_seconds,
                    "upload_manifests_max_age_seconds": lock_seconds,
                    "upload_blobs": "indefinite",
                    "docker_images": "indefinite",
                },
                "lifecycle": {
                    "recovery_sets_delete_after_seconds": lifecycle_seconds,
                    "upload_manifests_delete_after_seconds": lifecycle_seconds,
                    "upload_blobs_delete_after_seconds": None,
                    "docker_images_delete_after_seconds": None,
                    "multipart_abort_after_seconds": 7 * 86400,
                },
                "admin_evidence": {
                    "lock_semantic_sha256": "e" * 64,
                    "lifecycle_semantic_sha256": "f" * 64,
                },
            }

        account = "a" * 64
        verified = now - timedelta(minutes=1)
        return {
            "schema": manifest_module.STORAGE_POLICY_SCHEMA,
            "source_sha256": "d" * 64,
            "verified_at": stamp(verified),
            "expires_at": stamp(verified + timedelta(minutes=15)),
            "primary": remote(
                "r2postiz:homelab-backups/postiz", 30 * 86400, 31 * 86400, account
            ),
            "dr": remote(
                "r2drpostiz:homelab-backups-dr/postiz", 90 * 86400, 91 * 86400, account
            ),
            "failure_domain": {
                "provider": "cloudflare-r2",
                "independent_accounts": False,
                "accepted_correlated_admin_risk": True,
            },
        }

    def test_storage_policy_requires_bucket_locks_lifecycle_and_truthful_scope(self) -> None:
        policy = self.base / "policy.json"
        value = self.storage_policy()
        policy.write_text(json.dumps(value), encoding="utf-8")
        manifest_module.command_verify_storage_policy(
            Namespace(policy=str(policy), historical=False)
        )
        value["primary"]["locks"]["recovery_sets_max_age_seconds"] = 30 * 86400 - 1
        policy.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(manifest_module.ContractError):
            manifest_module.command_verify_storage_policy(
                Namespace(policy=str(policy), historical=False)
            )
        value = self.storage_policy()
        value["primary"]["locks"]["recovery_sets_max_age_seconds"] = 30 * 86400 + 1
        policy.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(manifest_module.ContractError):
            manifest_module.command_verify_storage_policy(
                Namespace(policy=str(policy), historical=False)
            )
        value = self.storage_policy()
        value["primary"]["credential"]["object_read_write_includes_delete"] = False
        policy.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(manifest_module.ContractError):
            manifest_module.command_verify_storage_policy(
                Namespace(policy=str(policy), historical=False)
            )

    def test_live_policy_attestation_rejects_missing_broad_and_weak_rules(self) -> None:
        source = self.base / "source.json"
        runtime_credential = {
            "bucket_only": True,
            "cross_bucket_denied": True,
            "object_read_write_includes_delete": True,
            "policy_admin_denied": True,
        }
        def remote(bucket: str, name: str) -> dict[str, object]:
            account_id = "a" * 32
            return {
                "account_id": account_id,
                "bucket": bucket,
                "jurisdiction": "default",
                "bucket_token_resource": (
                    f"com.cloudflare.edge.r2.bucket.{account_id}_default_{bucket}"
                ),
                "remote": name,
                "policy_token_file": "/srv/homelab/secrets/cloudflare-r2-policy-read-token.txt",
                "runtime_credential": runtime_credential,
            }
        source.write_text(
            json.dumps(
                {
                    "schema": manifest_module.STORAGE_POLICY_SOURCE_SCHEMA,
                    "provider": "cloudflare-r2",
                    "primary": remote(
                        "homelab-backups", "r2postiz:homelab-backups/postiz"
                    ),
                    "dr": remote(
                        "homelab-backups-dr", "r2drpostiz:homelab-backups-dr/postiz"
                    ),
                    "failure_domain": {
                        "provider": "cloudflare-r2",
                        "independent_accounts": False,
                        "accepted_correlated_admin_risk": True,
                    },
                }
            ),
            encoding="utf-8",
        )

        def envelope(rules: list[dict[str, object]]) -> dict[str, object]:
            return {"success": True, "errors": [], "messages": [], "result": {"rules": rules}}

        def lock_rules(days: int) -> list[dict[str, object]]:
            return [
                {
                    "id": "recovery",
                    "enabled": True,
                    "prefix": "postiz/recovery-sets/",
                    "condition": {"type": "Age", "maxAgeSeconds": days * 86400},
                },
                {
                    "id": "manifests",
                    "enabled": True,
                    "prefix": "postiz/uploads/manifests/",
                    "condition": {"type": "Age", "maxAgeSeconds": days * 86400},
                },
                {
                    "id": "blobs",
                    "enabled": True,
                    "prefix": "postiz/uploads/blobs/sha256/",
                    "condition": {"type": "Indefinite"},
                },
                {
                    "id": "images",
                    "enabled": True,
                    "prefix": "postiz/images/sha256/",
                    "condition": {"type": "Indefinite"},
                },
            ]

        def lifecycle_rules(
            days: int, *, cloudflare_live_shape: bool = False
        ) -> list[dict[str, object]]:
            rules: list[dict[str, object]] = [
                {
                    "id": "Default Multipart Abort Rule",
                    "enabled": True,
                    "conditions": {"prefix": ""},
                    "abortMultipartUploadsTransition": {
                        "condition": {"type": "Age", "maxAge": 7 * 86400}
                    },
                },
                {
                    "id": "delete-recovery",
                    "enabled": True,
                    "conditions": {"prefix": "postiz/recovery-sets/"},
                    "deleteObjectsTransition": {
                        "condition": {"type": "Age", "maxAge": days * 86400}
                    },
                },
                {
                    "id": "delete-manifests",
                    "enabled": True,
                    "conditions": {"prefix": "postiz/uploads/manifests/"},
                    "deleteObjectsTransition": {
                        "condition": {"type": "Age", "maxAge": days * 86400}
                    },
                },
            ]
            if cloudflare_live_shape:
                rules[0]["conditions"] = {}
                for rule in rules:
                    rule.setdefault("abortMultipartUploadsTransition", None)
                    rule.setdefault("deleteObjectsTransition", None)
                    rule["storageClassTransitions"] = None
            return rules

        paths: dict[str, Path] = {}
        for label, lock_days, lifecycle_days in (("primary", 30, 31), ("dr", 90, 91)):
            paths[f"{label}_lock"] = self.base / f"{label}-lock.json"
            paths[f"{label}_lifecycle"] = self.base / f"{label}-lifecycle.json"
            paths[f"{label}_lock"].write_text(
                json.dumps(envelope(lock_rules(lock_days))), encoding="utf-8"
            )
            paths[f"{label}_lifecycle"].write_text(
                json.dumps(envelope(lifecycle_rules(lifecycle_days))), encoding="utf-8"
            )
        output = self.base / "attestation.json"
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        def attest() -> None:
            manifest_module.command_attest_storage_policy(
                Namespace(
                    source=str(source),
                    timestamp=timestamp,
                    output=str(output),
                    **{name: str(path) for name, path in paths.items()},
                )
            )

        attest()
        manifest_module.command_verify_storage_policy(
            Namespace(policy=str(output), historical=False)
        )
        first = json.loads(output.read_text(encoding="utf-8"))
        for label, lifecycle_days in (("primary", 31), ("dr", 91)):
            paths[f"{label}_lifecycle"].write_text(
                json.dumps(
                    envelope(
                        lifecycle_rules(
                            lifecycle_days, cloudflare_live_shape=True
                        )
                    )
                ),
                encoding="utf-8",
            )
        attest()
        live_shaped = json.loads(output.read_text(encoding="utf-8"))
        for label in ("primary", "dr"):
            self.assertEqual(
                first[label]["admin_evidence"]["lifecycle_semantic_sha256"],
                live_shaped[label]["admin_evidence"]["lifecycle_semantic_sha256"],
            )
        manifest_module.command_verify_storage_policy(
            Namespace(policy=str(output), historical=False)
        )
        primary_lock = json.loads(paths["primary_lock"].read_text(encoding="utf-8"))
        primary_lock["result"]["rules"].reverse()
        paths["primary_lock"].write_text(json.dumps(primary_lock), encoding="utf-8")
        attest()
        reordered = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(
            first["primary"]["admin_evidence"]["lock_semantic_sha256"],
            reordered["primary"]["admin_evidence"]["lock_semantic_sha256"],
        )
        del primary_lock["result"]["rules"][0]
        paths["primary_lock"].write_text(json.dumps(primary_lock), encoding="utf-8")
        with self.assertRaises(manifest_module.ContractError):
            attest()
        primary_lock = envelope(lock_rules(30))
        primary_lock["result"]["rules"].append(
            {
                "id": "broad",
                "enabled": True,
                "prefix": "postiz/",
                "condition": {"type": "Indefinite"},
            }
        )
        paths["primary_lock"].write_text(json.dumps(primary_lock), encoding="utf-8")
        with self.assertRaises(manifest_module.ContractError):
            attest()
        paths["primary_lock"].write_text(
            json.dumps(envelope(lock_rules(31))), encoding="utf-8"
        )
        with self.assertRaises(manifest_module.ContractError):
            attest()
        paths["primary_lock"].write_text(
            json.dumps(envelope(lock_rules(30))), encoding="utf-8"
        )
        paths["primary_lifecycle"].write_text(
            json.dumps(envelope(lifecycle_rules(30))), encoding="utf-8"
        )
        with self.assertRaises(manifest_module.ContractError):
            attest()
        invalid_empty_conditions = lifecycle_rules(31, cloudflare_live_shape=True)
        invalid_empty_conditions[1]["conditions"] = {}
        paths["primary_lifecycle"].write_text(
            json.dumps(envelope(invalid_empty_conditions)), encoding="utf-8"
        )
        with self.assertRaises(manifest_module.ContractError):
            attest()
        invalid_explicit_prefix = lifecycle_rules(31, cloudflare_live_shape=True)
        invalid_explicit_prefix[1]["conditions"] = {"prefix": 7}
        paths["primary_lifecycle"].write_text(
            json.dumps(envelope(invalid_explicit_prefix)), encoding="utf-8"
        )
        with self.assertRaises(manifest_module.ContractError):
            attest()
        nonempty_storage = lifecycle_rules(31, cloudflare_live_shape=True)
        nonempty_storage.append(
            {
                "id": "unrelated-storage-transition",
                "enabled": True,
                "conditions": {"prefix": "unrelated/"},
                "storageClassTransitions": [
                    {"condition": {"type": "Age", "maxAge": 86400}}
                ],
            }
        )
        paths["primary_lifecycle"].write_text(
            json.dumps(envelope(nonempty_storage)), encoding="utf-8"
        )
        with self.assertRaises(manifest_module.ContractError):
            attest()
        invalid_storage = lifecycle_rules(31, cloudflare_live_shape=True)
        invalid_storage[1]["storageClassTransitions"] = {}
        paths["primary_lifecycle"].write_text(
            json.dumps(envelope(invalid_storage)), encoding="utf-8"
        )
        with self.assertRaises(manifest_module.ContractError):
            attest()

    def test_rclone_runtime_remotes_are_bound_to_attested_accounts(self) -> None:
        source = self.base / "source-binding.json"
        credential = {
            "bucket_only": True,
            "cross_bucket_denied": True,
            "object_read_write_includes_delete": True,
            "policy_admin_denied": True,
        }
        value = {
            "schema": manifest_module.STORAGE_POLICY_SOURCE_SCHEMA,
            "provider": "cloudflare-r2",
            "primary": {
                "account_id": "a" * 32,
                "bucket": "homelab-backups",
                "jurisdiction": "default",
                "bucket_token_resource": (
                    f"com.cloudflare.edge.r2.bucket.{'a' * 32}_default_homelab-backups"
                ),
                "remote": "r2postiz:homelab-backups/postiz",
                "policy_token_file": "/srv/homelab/secrets/policy-primary.txt",
                "runtime_credential": credential,
            },
            "dr": {
                "account_id": "b" * 32,
                "bucket": "homelab-backups-dr",
                "jurisdiction": "default",
                "bucket_token_resource": (
                    f"com.cloudflare.edge.r2.bucket.{'b' * 32}_default_homelab-backups-dr"
                ),
                "remote": "r2drpostiz:homelab-backups-dr/postiz",
                "policy_token_file": "/srv/homelab/secrets/policy-dr.txt",
                "runtime_credential": credential,
            },
            "failure_domain": {
                "provider": "cloudflare-r2",
                "independent_accounts": True,
                "accepted_correlated_admin_risk": False,
            },
        }
        source.write_text(json.dumps(value), encoding="utf-8")
        config = self.base / "rclone.conf"

        def write_config(primary_endpoint: str, env_auth: str = "false") -> None:
            config.write_text(
                "\n".join(
                    (
                        "[r2postiz]",
                        "type = s3",
                        "provider = Cloudflare",
                        f"endpoint = {primary_endpoint}",
                        f"env_auth = {env_auth}",
                        "access_key_id = primary-access-key",
                        "secret_access_key = primary-secret",
                        "[r2drpostiz]",
                        "type = s3",
                        "provider = Cloudflare",
                        f"endpoint = https://{'b' * 32}.r2.cloudflarestorage.com",
                        "env_auth = false",
                        "access_key_id = dr-access-key",
                        "secret_access_key = dr-secret",
                        "",
                    )
                ),
                encoding="utf-8",
            )
            os.chmod(config, 0o600)

        write_config(f"https://{'a' * 32}.r2.cloudflarestorage.com")
        manifest_module.command_verify_rclone_source(
            Namespace(source=str(source), rclone_config=str(config))
        )
        value["primary"]["bucket_token_resource"] = value["primary"][
            "bucket_token_resource"
        ].replace("_default_", "_eu_")
        source.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(manifest_module.ContractError):
            manifest_module.command_verify_rclone_source(
                Namespace(source=str(source), rclone_config=str(config))
            )
        value["primary"]["bucket_token_resource"] = value["primary"][
            "bucket_token_resource"
        ].replace("_eu_", "_default_")
        source.write_text(json.dumps(value), encoding="utf-8")
        write_config(f"https://{'c' * 32}.r2.cloudflarestorage.com")
        with self.assertRaises(manifest_module.ContractError):
            manifest_module.command_verify_rclone_source(
                Namespace(source=str(source), rclone_config=str(config))
            )
        write_config(f"https://{'a' * 32}.r2.cloudflarestorage.com", "true")
        with self.assertRaises(manifest_module.ContractError):
            manifest_module.command_verify_rclone_source(
                Namespace(source=str(source), rclone_config=str(config))
            )

    def test_recovery_set_has_exact_four_databases_and_physical_consistency(self) -> None:
        timestamp = "20260814T120000Z"
        names = {
            "physical_cluster": f"postiz_postgres_cluster_{timestamp}.tar.gz.enc",
            "capture_evidence": f"postiz_capture_{timestamp}.evidence.json.enc",
            "globals": f"globals_postiz-postgres_{timestamp}.sql.enc",
            "database_postiz": f"db_postiz-postgres_postiz_{timestamp}.dump.enc",
            "database_temporal": f"db_postiz-postgres_temporal_{timestamp}.dump.enc",
            "database_temporal_visibility": f"db_postiz-postgres_temporal_visibility_{timestamp}.dump.enc",
            "database_insights": f"db_postiz-postgres_insights_{timestamp}.dump.enc",
            "runtime_config": f"postiz_config_{timestamp}.tar.gz.enc",
            "config_volume": f"postiz_config_volume_{timestamp}.tar.gz.enc",
            "redis": f"postiz_redis_{timestamp}.rdb.enc",
            "artifacts": f"postiz_artifacts_{timestamp}.json.enc",
            "operator_state": f"postiz_operator_state_{timestamp}.json.enc",
            "storage_policy": f"postiz_storage_policy_{timestamp}.json.enc",
        }
        paths = {}
        for key_name, filename in names.items():
            path = self.base / filename
            path.write_bytes(key_name.encode())
            paths[key_name] = str(path)
        output = self.base / "recovery.json"
        manifest_module.command_write_recovery_set(
            Namespace(timestamp=timestamp, output=str(output), **paths)
        )
        value = json.loads(output.read_text())
        self.assertEqual(value["consistency"]["kind"], "writer-fenced-physical-cluster")
        self.assertEqual(
            set(value["payloads"]["databases"]),
            {"postiz", "temporal", "temporal_visibility", "insights"},
        )
        self.assertNotIn("postgres", value["payloads"]["databases"])
        self.assertTrue(all("cipher_bytes" in item for item in value["payloads"]["databases"].values()))

    def test_seasonal_policy_requires_exact_roles_and_canonical_inventory(self) -> None:
        digest = hashlib.sha256(b"artifact").hexdigest()

        def root(path: str, roles: set[str]) -> dict[str, object]:
            return {
                "path": path,
                "root_mode": 0o700,
                "files": {"release/artifact.bin": {"sha256": digest, "size": 8, "mode": 0o600}},
                "roles": {name: ["release/artifact.bin"] for name in sorted(roles)},
            }

        value = {
            "schema": manifest_module.SEASONAL_POLICY_SCHEMA,
            "created_at": "20260814T120000Z",
            "state_required": True,
            "release_id": "seasonal-release-1",
            "roots": {
                "seasonal_releases": root(
                    manifest_module.OPERATOR_STATE_PATHS["seasonal_releases"],
                    manifest_module.SEASONAL_ROLE_SETS["seasonal_releases"],
                ),
                "seasonal_anchor_replacement": root(
                    manifest_module.OPERATOR_STATE_PATHS["seasonal_anchor_replacement"],
                    manifest_module.SEASONAL_ROLE_SETS["seasonal_anchor_replacement"],
                ),
            },
        }
        manifest_module._validate_seasonal_policy(value, verify_sources=False)
        del value["roots"]["seasonal_releases"]["roles"]["installed_saga"]
        with self.assertRaises(manifest_module.ContractError):
            manifest_module._validate_seasonal_policy(value, verify_sources=False)


if __name__ == "__main__":
    unittest.main()
