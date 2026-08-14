from __future__ import annotations

import json
import re
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class PostizBackupContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        def read(relative: str) -> str:
            return (ROOT / relative).read_text(encoding="utf-8")

        cls.nightly = read("scripts/backup.sh")
        cls.frequent = read("scripts/frequent-db-backup.sh")
        cls.capture = read("scripts/postiz-quiesced-capture.sh")
        cls.artifacts = read("scripts/postiz-artifact-backup.sh")
        cls.attester = read("scripts/postiz-r2-policy-attest.sh")
        cls.restore = read("self-healing/postiz-restore-drill.sh")
        cls.offline = read("self-healing/postiz-offline-verify.sh")
        cls.generic_restore = read("self-healing/restore-drill.sh")
        cls.tmpfiles = read("scripts/tmpfiles.d/homelab-backup.conf")
        cls.restore_cleanup_unit = read("scripts/systemd/postiz-restore-cleanup.service")
        cls.workspace_cleanup = read("scripts/postiz-backup-workspace-cleanup.sh")
        cls.workspace_cleanup_unit = read(
            "scripts/systemd/postiz-backup-workspace-cleanup.service"
        )

    def test_frequent_postiz_target_is_exact_and_is_only_pit(self) -> None:
        target = '"postiz-postgres|postiz|postiz temporal temporal_visibility insights"'
        self.assertEqual(self.frequent.count(target), 1)
        self.assertNotIn("postiz-postgres|postiz|", self.nightly)
        self.assertIn("per-DB PIT", self.frequent)
        self.assertIn("primary R2", self.frequent)
        self.assertIn("TS=$(date -u", self.frequent)

    def test_capture_has_real_writer_fence_and_crash_recovery(self) -> None:
        for service in ("postiz", "postiz-temporal", "postiz-redis", "postiz-postgres"):
            self.assertIn(service, self.capture)
        self.assertLess(
            self.capture.index('docker stop --time 30 "${container_ids[postiz]}"'),
            self.capture.index('docker stop --time 30 "${container_ids[postiz-temporal]}"'),
        )
        self.assertLess(
            self.capture.index('redis-cli SAVE'),
            self.capture.index('docker stop --time 30 "${container_ids[postiz-redis]}"'),
        )
        self.assertIn("write-quiesce-journal", self.capture)
        self.assertIn("recover_from_journal", self.capture)
        self.assertIn("MAX_CAPTURE_SECONDS=300", self.capture)
        self.assertIn("--kill-after=5s 295s", self.capture)
        self.assertIn("--supervised-worker", self.capture)
        self.assertIn("final_recovery_rc", self.capture)
        self.assertIn("MAX_RECOVERY_SECONDS=360", self.capture)
        self.assertIn("--address postiz-temporal:7233", self.capture)
        self.assertNotIn("--address 127.0.0.1:7233", self.capture)
        for fragment in (
            "HOME=/nonexistent",
            "--namespace default",
            "--env-file /dev/null",
            "--color never",
            "--log-level never",
            "--output json",
        ):
            self.assertIn(fragment, self.capture)
        self.assertNotIn("HOME=/tmp", self.capture)
        self.assertIn("pg_basebackup", self.capture)
        self.assertIn("-X fetch", self.capture)
        self.assertIn("database writers/connections remain after fence", self.capture)
        self.assertNotIn("datname IN ('postiz'", self.capture)
        self.assertIn("freio-seasonal-anchors.launch.lock", self.capture)
        self.assertIn("freio-seasonal-anchors.engine.lock", self.capture)
        self.assertIn("verify_compose_generation preflight", self.capture)
        self.assertIn("verify_compose_generation writer-fenced", self.capture)
        self.assertIn("config --hash '*'", self.capture)
        self.assertIn("service network set drifted", self.capture)
        self.assertIn("host-published port", self.capture)
        compose_locked = (ROOT / "scripts/postiz-compose-locked.sh").read_text()
        self.assertIn('safe_root_file "$RECOVER" 755', compose_locked)
        self.assertIn('safe_root_file "$COMPOSE" 644', compose_locked)
        self.assertIn('safe_root_file "$ENV_FILE" 600', compose_locked)
        lock_index = compose_locked.index("flock -w 300 9")
        journal_index = compose_locked.index('[[ ! -e "$JOURNAL" && ! -L "$JOURNAL" ]]')
        compose_index = compose_locked.index("exec docker compose")
        self.assertLess(lock_index, journal_index)
        self.assertLess(journal_index, compose_index)
        self.assertIn("flock -u 9", compose_locked)
        self.assertIn("for attempt in 1 2 3", compose_locked)
        self.assertNotIn("Complete any crash recovery before taking", compose_locked)

    def test_compose_mutation_recovers_journal_created_while_waiting(self) -> None:
        compose_locked = (ROOT / "scripts/postiz-compose-locked.sh").read_text()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            run_root = base / "run"
            state_root = base / "state"
            fake_bin = base / "bin"
            for directory in (run_root, state_root, fake_bin):
                directory.mkdir(mode=0o700)
            mutation_lock = run_root / "postiz-mutation.lock"
            journal = state_root / "postiz-quiesce-journal.json"
            recover = base / "postiz-quiesced-capture.sh"
            compose = base / "docker-compose.yml"
            env_file = base / "postiz.env"
            events = base / "events"
            flock_state = base / "flock-state"
            for path, mode in (
                (mutation_lock, 0o600),
                (recover, 0o755),
                (compose, 0o644),
                (env_file, 0o600),
            ):
                path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                path.chmod(mode)

            (fake_bin / "flock").write_text(
                """#!/bin/sh
if [ "$1" = "-u" ]; then
  printf '%s\\n' flock-release >>"$TEST_EVENTS"
  exit 0
fi
printf '%s\\n' flock-acquire >>"$TEST_EVENTS"
if [ ! -e "$TEST_FLOCK_STATE" ]; then
  : >"$TEST_FLOCK_STATE"
  printf '%s\\n' simulated-journal >"$TEST_JOURNAL"
fi
""",
                encoding="utf-8",
            )
            (fake_bin / "timeout").write_text(
                """#!/bin/sh
printf '%s\\n' recover >>"$TEST_EVENTS"
rm -f -- "$TEST_JOURNAL"
""",
                encoding="utf-8",
            )
            (fake_bin / "docker").write_text(
                """#!/bin/sh
printf '%s\\n' docker >>"$TEST_EVENTS"
""",
                encoding="utf-8",
            )
            for command in ("flock", "timeout", "docker"):
                (fake_bin / command).chmod(0o755)

            uid = os.getuid()
            gid = os.getgid()
            transformed = compose_locked
            replacements = {
                "export PATH=/usr/sbin:/usr/bin:/sbin:/bin": (
                    f"export PATH={fake_bin}:/usr/bin:/bin"
                ),
                "readonly RUN_ROOT=/run/homelab-backup": f"readonly RUN_ROOT={run_root}",
                "readonly STATE_ROOT=/var/lib/homelab-backup": (
                    f"readonly STATE_ROOT={state_root}"
                ),
                "readonly RECOVER=/usr/local/sbin/postiz-quiesced-capture.sh": (
                    f"readonly RECOVER={recover}"
                ),
                "readonly COMPOSE=/srv/postiz/docker-compose.yml": (
                    f"readonly COMPOSE={compose}"
                ),
                "readonly ENV_FILE=/srv/postiz/postiz.env": (
                    f"readonly ENV_FILE={env_file}"
                ),
                "((EUID == 0))": "((1))",
                '"0:0:${mode}:1"': f'"{uid}:{gid}:${{mode}}:1"',
                "== 0:0:700": f"== {uid}:{gid}:700",
                "== 0:0:600:1": f"== {uid}:{gid}:600:1",
            }
            for old, new in replacements.items():
                self.assertIn(old, transformed)
                transformed = transformed.replace(old, new)
            wrapper = base / "postiz-compose-locked.sh"
            wrapper.write_text(transformed, encoding="utf-8")
            wrapper.chmod(0o755)
            result = subprocess.run(
                [str(wrapper), "up", "-d"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
                env={
                    **os.environ,
                    "TEST_EVENTS": str(events),
                    "TEST_FLOCK_STATE": str(flock_state),
                    "TEST_JOURNAL": str(journal),
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                events.read_text(encoding="utf-8").splitlines(),
                ["flock-acquire", "flock-release", "recover", "flock-acquire", "docker"],
            )
            self.assertFalse(journal.exists())

    def test_writer_failure_still_reaps_orphaned_redis_parser(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            run_root = base / "run"
            state_root = base / "state"
            fake_bin = base / "bin"
            for directory in (run_root, state_root, fake_bin):
                directory.mkdir(mode=0o700)
            for lock_name in (
                "postiz-mutation.lock",
                "freio-seasonal-anchors.launch.lock",
                "freio-seasonal-anchors.engine.lock",
            ):
                lock = run_root / lock_name
                lock.touch(mode=0o600)
                lock.chmod(0o600)

            timestamp = "20260814T120000Z"
            parser_name = f"postiz-capture-redis-check-{timestamp}"
            parser_id = "a" * 64
            service_ids = {
                "postiz": "b" * 64,
                "postiz-postgres": "c" * 64,
                "postiz-temporal": "d" * 64,
                "postiz-redis": "e" * 64,
            }
            journal = state_root / "postiz-quiesce-journal.json"
            journal.write_text(
                json.dumps(
                    {
                        "schema": "freio.postiz.quiesce-journal.v1",
                        "created_at": timestamp,
                        "phase": "stopped",
                        "containers": {
                            service: {
                                "container_id": container_id,
                                "image_id": f"sha256:{index * 64}",
                                "was_running": True,
                            }
                            for service, container_id, index in (
                                ("postiz", service_ids["postiz"], "1"),
                                ("postiz-postgres", service_ids["postiz-postgres"], "2"),
                                ("postiz-temporal", service_ids["postiz-temporal"], "3"),
                                ("postiz-redis", service_ids["postiz-redis"], "4"),
                            )
                        },
                    }
                ),
                encoding="utf-8",
            )
            journal.chmod(0o600)
            events = base / "events"

            (fake_bin / "flock").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (fake_bin / "timeout").write_text(
                """#!/bin/sh
while [ "${1#--}" != "$1" ]; do shift; done
shift
exec "$@"
""",
                encoding="utf-8",
            )
            (fake_bin / "docker").write_text(
                """#!/bin/sh
case "$1" in
  ps)
    printf '%s|%s\\n' "$TEST_PARSER_ID" "$TEST_PARSER_NAME"
    ;;
  inspect)
    case "$*" in
      *"$TEST_PARSER_ID")
        printf '/%s|%s|redis-check\\n' "$TEST_PARSER_NAME" "$TEST_TIMESTAMP"
        ;;
      *)
        printf '%s\\n' writer-inspect >>"$TEST_EVENTS"
        exit 1
        ;;
    esac
    ;;
  rm)
    printf '%s\\n' parser-reaped >>"$TEST_EVENTS"
    ;;
  *) exit 1 ;;
esac
""",
                encoding="utf-8",
            )
            for command in ("flock", "timeout", "docker"):
                (fake_bin / command).chmod(0o755)

            uid = os.getuid()
            gid = os.getgid()
            transformed = self.capture
            replacements = {
                "export PATH=/usr/sbin:/usr/bin:/sbin:/bin": (
                    f"export PATH={fake_bin}:/usr/bin:/bin"
                ),
                "readonly HELPER=/usr/local/libexec/postiz-backup-manifest.py": (
                    f"readonly HELPER={ROOT / 'scripts/postiz-backup-manifest.py'}"
                ),
                "readonly RUN_ROOT=/run/homelab-backup": f"readonly RUN_ROOT={run_root}",
                "readonly STATE_ROOT=/var/lib/homelab-backup": (
                    f"readonly STATE_ROOT={state_root}"
                ),
                "((EUID == 0))": "((1))",
                '"0:0:${mode}:1"': f'"{uid}:{gid}:${{mode}}:1"',
                '"0:0:${mode}"': f'"{uid}:{gid}:${{mode}}"',
                "== 0:0:600:1": f"== {uid}:{gid}:600:1",
            }
            for old, new in replacements.items():
                self.assertIn(old, transformed)
                transformed = transformed.replace(old, new)
            capture = base / "postiz-quiesced-capture.sh"
            capture.write_text(transformed, encoding="utf-8")
            capture.chmod(0o755)
            result = subprocess.run(
                [str(capture), "--recover-only"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
                env={
                    **os.environ,
                    "TEST_EVENTS": str(events),
                    "TEST_PARSER_ID": parser_id,
                    "TEST_PARSER_NAME": parser_name,
                    "TEST_TIMESTAMP": timestamp,
                },
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("stale writer-fence recovery failed", result.stderr)
            self.assertTrue(events.exists(), f"{result.stdout}\n{result.stderr}")
            self.assertEqual(
                events.read_text(encoding="utf-8").splitlines(),
                ["parser-reaped", "writer-inspect"],
            )
            self.assertTrue(journal.is_file())

    def test_capture_fails_closed_on_database_and_mount_topology(self) -> None:
        exact_inventory = "insights\npostgres\npostiz\ntemporal\ntemporal_visibility"
        self.assertIn(exact_inventory, self.capture)
        self.assertIn("maintenance postgres database now contains user objects", self.capture)
        self.assertIn("persistent mount coverage drifted", self.capture)
        for volume in (
            "postiz_postiz-postgres",
            "postiz_postiz-redis",
            "postiz_postiz-config",
            "postiz_postiz-uploads",
        ):
            self.assertIn(volume, self.capture)
        counts = self.capture.index("add_count cluster_roles")
        restart = self.capture.index('update-quiesce-journal --journal "$JOURNAL" --phase captured')
        self.assertLess(counts, restart)
        self.assertIn("assert_database_identity", self.capture)
        self.assertIn("MAX_CAPTURE_WORKSPACE_BYTES", self.capture)
        self.assertIn("ulimit -f", self.capture)

    def test_complete_set_uses_dedicated_locked_namespace_and_commit_last(self) -> None:
        self.assertIn('POSTIZ_SET_PREFIX="recovery-sets/', self.nightly)
        for option in (
            "--physical-cluster",
            "--capture-evidence",
            "--globals",
            "--database-postiz",
            "--database-temporal",
            "--database-temporal-visibility",
            "--database-insights",
            "--runtime-config",
            "--config-volume",
            "--redis",
            "--artifacts",
            "--operator-state",
            "--storage-policy",
        ):
            self.assertIn(option, self.nightly)
        payload_copy = self.nightly.index('copy "$POSTIZ_PAYLOADS"')
        marker_copy = self.nightly.index('copyto "$POSTIZ_RECOVERY_ENC"')
        commit_copy = self.nightly.index('copyto "$POSTIZ_COMMIT_AUTH"')
        self.assertLess(payload_copy, marker_copy)
        self.assertLess(marker_copy, commit_copy)
        self.assertIn("write-auth-record", self.nightly)
        self.assertIn("verify-auth-record", self.nightly)

    def test_server_lock_truth_and_runtime_credential_truth(self) -> None:
        self.assertIn("r2postiz:homelab-backups/postiz", self.artifacts)
        self.assertIn("r2drpostiz:homelab-backups-dr/postiz", self.artifacts)
        self.assertIn("verify-storage-policy", self.artifacts)
        self.assertIn('"$POLICY_ATTESTER"', self.artifacts)
        self.assertIn('"$POSTIZ_POLICY_ATTESTER"', self.nightly)
        self.assertIn("attest-storage-policy", self.attester)
        self.assertIn("lock", self.attester)
        self.assertIn("lifecycle", self.attester)
        self.assertIn("Bucket Locks", self.artifacts)
        self.assertIn("client-side collision guard", self.artifacts)
        self.assertNotRegex(self.artifacts, r'\b(?:rclone|"\$\{RC\[@\]\}")\s+delete\b')
        self.assertIn("Cloudflare Object\n# Read & Write credentials are delete-capable", self.nightly)

    def test_all_four_running_images_are_pinned_archived_and_loaded(self) -> None:
        image_block = self.artifacts.split("readonly -a IMAGE_SPECS=(", 1)[1].split(")", 1)[0]
        specs = re.findall(r"'(postiz(?:-[a-z]+)?)\|([^']+)'", image_block)
        self.assertEqual({service for service, _ in specs}, {
            "postiz", "postiz-postgres", "postiz-redis", "postiz-temporal"
        })
        for service, configured_ref in specs:
            if service != "postiz":
                self.assertRegex(configured_ref, r"@sha256:[0-9a-f]{64}$")
        self.assertIn("docker image save", self.artifacts)
        self.assertIn(".docker.tar.gz.enc", self.artifacts)
        self.assertNotIn(".oci.tar.gz", self.artifacts)
        self.assertIn("docker image load --input", self.restore)
        self.assertEqual(self.restore.count("verify-image-archive"), 1)
        self.assertIn("archive_uncompressed_inodes", self.restore)
        self.assertIn("Docker image layers exceed expanded inode ceiling", \
                      (ROOT / "scripts/postiz-backup-manifest.py").read_text())
        self.assertIn("LayerSources", (ROOT / "scripts/postiz-backup-manifest.py").read_text())
        self.assertIn("oci-layout", (ROOT / "scripts/postiz-backup-manifest.py").read_text())
        dockerfile = (ROOT / "postiz-Dockerfile.patch").read_text(encoding="utf-8")
        self.assertRegex(dockerfile.splitlines()[2], r"@sha256:[0-9a-f]{64}$")

    def test_restore_is_dual_remote_network_none_and_strict_for_all_databases(self) -> None:
        self.assertIn('restore_one_remote primary "$PRIMARY_ROOT"', self.restore)
        self.assertIn('restore_one_remote dr "$DR_ROOT"', self.restore)
        self.assertGreaterEqual(self.restore.count("--network none"), 8)
        self.assertIn("pg_restore --exit-on-error", self.restore)
        self.assertNotIn("--no-privileges", self.restore)
        self.assertIn("-v ON_ERROR_STOP=1", self.restore)
        self.assertIn("strict globals restore failed", self.restore)
        self.assertIn("POSTGRES_USER=freio_restore_bootstrap", self.restore)
        self.assertIn("pg_verifybackup", self.restore)
        self.assertIn("owner/ACL/extension catalog fingerprint differs", self.restore)
        self.assertIn("role-membership fingerprint differs", self.restore)
        self.assertIn(":/restore/database-$database.dump:ro", self.restore)
        self.assertNotIn("docker cp", self.restore)
        self.assertIn("WAL-consistent physical four-database cluster", self.restore)
        for database in ("postiz", "temporal", "temporal_visibility", "insights"):
            self.assertIn(database, self.restore)
        self.assertNotRegex(self.restore, re.compile(r"docker\s+(?:compose\s+)?(?:stop|restart|down)"))
        self.assertNotIn("postiz_postiz-uploads", self.restore)
        self.assertEqual(self.restore.count("config --no-env-resolution --images"), 2)
        self.assertIn("ignored invalid/replayed common committed-set candidate", self.restore)
        self.assertIn('$candidate_root/primary/recovery-set.json.enc', self.restore)
        self.assertIn('$candidate_root/dr/recovery-set.json.enc', self.restore)
        self.assertNotIn("primary-marker.enc", self.restore)
        self.assertNotIn("dr-marker.enc", self.restore)
        self.assertGreaterEqual(self.restore.count("--expected-context"), 3)
        for resource_limit in ("--memory", "--memory-swap", "--pids-limit", "--cpus"):
            self.assertIn(resource_limit, self.restore)
        self.assertIn("write-restore-journal", self.restore)
        self.assertIn("restore-journal-get", self.restore)
        self.assertIn("freio.postiz.restore-run", self.restore)
        self.assertIn("reap_restore_state", self.restore)
        self.assertIn("--cleanup-only", self.restore)
        self.assertIn("docker ps -a --no-trunc", self.restore)
        self.assertIn("cannot prove restore container presence/absence", self.restore)
        self.assertNotIn('"$container" 2>/dev/null || true', self.restore)
        self.assertIn("ulimit -f $(((max_bytes + 1023) / 1024))", self.restore)
        self.assertIn("ulimit -f $(((source_bytes + 1023) / 1024))", self.restore)

    def test_config_redis_upload_and_seasonal_state_are_really_restored(self) -> None:
        self.assertIn("redis-cli SAVE", self.capture)
        self.assertIn("redis-check-rdb", self.capture)
        self.assertIn("reap_capture_parser", self.capture)
        self.assertIn("freio.postiz.capture-run", self.capture)
        self.assertIn("freio.postiz.capture-role=redis-check", self.capture)
        self.assertIn("timeout --signal=TERM --kill-after=10s 120s", self.capture)
        parser_reap = self.capture.index("reap_capture_parser ||")
        writer_recovery = self.capture.index('for service in "${START_ORDER[@]}"')
        journal_removal = self.capture.index('rm -f -- "$JOURNAL"')
        self.assertLess(parser_reap, writer_recovery)
        self.assertLess(writer_recovery, journal_removal)
        self.assertIn("((parser_recovery_rc == 0)) || return 1", self.capture)
        self.assertIn("restore_redis", self.restore)
        self.assertIn("rdb_last_load_keys_expired", self.restore)
        self.assertIn("redis_rdb_keys", self.capture)
        self.assertNotIn("redis_dbsize", self.capture)
        self.assertIn("verify-tree-restored", self.restore)
        self.assertIn("verify-restored", self.restore)
        self.assertIn("verify-seasonal-policy", self.capture)
        self.assertIn("seasonal-backup-policy.json", self.capture)
        self.assertIn("seasonal-releases", self.offline)
        self.assertIn("seasonal-anchor-replacement", self.offline)
        self.assertIn("root_mode", (ROOT / "scripts/postiz-backup-manifest.py").read_text())
        self.assertEqual(self.offline.count("usr/local/sbin/postiz-compose-locked.sh"), 2)
        self.assertIn("etc/systemd/system/postiz-restore-cleanup.service", self.offline)

    def test_capacity_preflights_cover_bytes_and_inodes(self) -> None:
        capacity_scripts = (
            self.capture,
            self.artifacts,
            self.restore,
            self.generic_restore,
        )
        combined = "\n".join(capacity_scripts)
        self.assertEqual(combined.count("df -B1 --output=avail"), 7)
        self.assertEqual(combined.count("df --output=iavail"), 7)
        self.assertNotIn("df -PB1 --output=avail", combined)
        self.assertNotIn("df -Pi --output=iavail", combined)
        self.assertIn("MAX_RESTORE_PEAK_BYTES", self.restore)
        self.assertIn("upload_transfer_cap + upload_bytes", self.restore)
        self.assertIn('--max-transfer "$upload_transfer_cap"', self.restore)
        self.assertIn("image_total * 2", self.restore)
        self.assertIn("required_inodes", self.restore)
        self.assertIn("peak + image_expanded_total", self.restore)
        self.assertIn("image_inode_total", self.restore)
        self.assertIn("MAX_NEW_CIPHER_BYTES", self.artifacts)
        self.assertIn('--max-transfer "$MAX_SOURCE_BYTES"', self.artifacts)
        self.assertIn("fetch_image_cipher", self.artifacts)
        self.assertIn("ulimit -f $(((MAX_IMAGE_CIPHER_BYTES + 1023) / 1024))", self.artifacts)
        self.assertIn("MAX_RUNTIME_CONFIG_EXPANDED_BYTES", self.capture)
        self.assertIn("MAX_RUNTIME_CONFIG_EXPANDED_BYTES", self.restore)
        helper = (ROOT / "scripts/postiz-backup-manifest.py").read_text()
        self.assertIn("MAX_CONFIG_ARCHIVE_MEMBER_BYTES", helper)
        self.assertIn("MAX_CONFIG_ARCHIVE_EXPANDED_BYTES", helper)
        self.assertIn("MAX_PHYSICAL_ARCHIVE_MEMBERS", helper)
        self.assertNotIn("members = bundle.getmembers()", helper)
        self.assertIn("MAX_PG_SOURCE_INODES=1000000", self.capture)
        self.assertIn("find /var/lib/postgresql/data -xdev -print | wc -l", self.capture)
        self.assertIn("pg_source_inodes + upload_source_inodes", self.capture)

    def test_df_capacity_probe_live_shape_is_positive_numeric(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            for arguments in (
                ("-B1", "--output=avail"),
                ("--output=iavail",),
            ):
                result = subprocess.run(
                    ["/usr/bin/df", *arguments, str(target)],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
                )
                lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
                self.assertEqual(len(lines), 2, result.stdout)
                self.assertRegex(lines[1], r"^[0-9]+$")
                self.assertGreater(int(lines[1]), 0)

    def test_frequent_last_ok_advances_only_after_complete_remote_check(self) -> None:
        remote_check = self.frequent.index('$RC check "$WORK"')
        complete_gate = self.frequent.index("if [[ $FAIL -eq 0 ]]")
        state_move = self.frequent.index('mv -f "$state_tmp" "$STATE"')
        self.assertLess(remote_check, complete_gate)
        self.assertLess(complete_gate, state_move)
        self.assertIn('sync -f "$STATE_ROOT"', self.frequent)

    def test_tmpfiles_and_systemd_sandbox_contract(self) -> None:
        for path in (
            "/run/homelab-backup/postiz-mutation.lock",
            "/run/homelab-backup/postiz-artifact.lock",
            "/run/homelab-backup/postiz-restore.lock",
            "/run/homelab-backup/postiz-policy-attest.lock",
            "/run/homelab-backup/nightly-workspace.lock",
            "/run/homelab-backup/frequent-workspace.lock",
        ):
            self.assertIn(path, self.tmpfiles)
        self.assertIn("d /run/homelab-backup 0700 root root", self.tmpfiles)
        self.assertIn("d /var/lib/freio-content 0700 root root", self.tmpfiles)
        for relative in (
            "scripts/systemd/backup.service",
            "scripts/systemd/frequent-db-backup.service",
            "scripts/systemd/restore-drill.service",
            "scripts/systemd/postiz-quiesce-recover.service",
            "scripts/systemd/postiz-restore-cleanup.service",
            "scripts/systemd/postiz-backup-workspace-cleanup.service",
        ):
            unit = (ROOT / relative).read_text(encoding="utf-8")
            for directive in (
                "User=root", "UMask=0077", "StateDirectory=homelab-backup",
                "NoNewPrivileges=yes", "PrivateTmp=yes", "ProtectSystem=strict",
                "ProtectKernelModules=yes", "RestrictSUIDSGID=yes",
                "PrivateMounts=yes", "ProtectProc=invisible",
                "ProtectClock=yes",
            ):
                self.assertIn(directive, unit)
            if relative == "scripts/systemd/restore-drill.service":
                self.assertIn("ProcSubset=all", unit)
                self.assertNotIn("ProcSubset=pid", unit)
            else:
                self.assertIn("ProcSubset=pid", unit)
        backup_unit = (ROOT / "scripts/systemd/backup.service").read_text()
        self.assertIn("ExecStopPost=/usr/local/sbin/postiz-quiesced-capture.sh --recover-only", backup_unit)
        self.assertIn("TimeoutStopSec=10min", backup_unit)
        recovery_unit = (ROOT / "scripts/systemd/postiz-quiesce-recover.service").read_text()
        self.assertIn("ConditionFileIsExecutable=", recovery_unit)
        self.assertNotIn("ConditionPathIsExecutable=", recovery_unit)
        restore_unit = (ROOT / "scripts/systemd/restore-drill.service").read_text()
        cleanup_command = "/srv/homelab/self-healing/restore-drill.sh --cleanup-only"
        self.assertIn(f"ExecStartPre={cleanup_command}", restore_unit)
        self.assertIn(f"ExecStopPost={cleanup_command}", restore_unit)
        self.assertIn("TimeoutStopSec=15min", restore_unit)
        self.assertIn("WantedBy=multi-user.target", self.restore_cleanup_unit)
        self.assertIn("TimeoutStartSec=12min", self.restore_cleanup_unit)
        self.assertIn(f"ExecStart={cleanup_command}", self.restore_cleanup_unit)
        self.assertIn("Restart=on-failure", self.restore_cleanup_unit)
        self.assertIn("RestartSec=30s", self.restore_cleanup_unit)
        self.assertIn("WantedBy=multi-user.target", self.workspace_cleanup_unit)
        self.assertIn(
            "ExecStart=/usr/local/sbin/postiz-backup-workspace-cleanup.sh --scope all",
            self.workspace_cleanup_unit,
        )

    def test_backup_plaintext_workspaces_have_locked_crash_reapers(self) -> None:
        contracts = (
            (self.nightly, "nightly", "7"),
            (self.frequent, "frequent", "7"),
            (self.artifacts, "artifact", "9"),
            (self.attester, "policy", "9"),
        )
        for script, scope, descriptor in contracts:
            self.assertIn(
                f'--scope {scope} --lock-held-fd {descriptor}',
                script,
            )
            self.assertIn("rm -rf --one-file-system", script)
        for prefix in ("nightly", "frequent", "postiz-artifact", "postiz-policy"):
            self.assertIn(prefix, self.workspace_cleanup)
        self.assertIn("mountpoint -q", self.workspace_cleanup)
        self.assertIn("findmnt -rn -o TARGET", self.workspace_cleanup)
        self.assertIn("rm -rf --one-file-system", self.workspace_cleanup)
        backup_unit = (ROOT / "scripts/systemd/backup.service").read_text()
        frequent_unit = (ROOT / "scripts/systemd/frequent-db-backup.service").read_text()
        self.assertIn("postiz-backup-workspace-cleanup.sh --scope all", backup_unit)
        self.assertIn("postiz-backup-workspace-cleanup.sh --scope frequent", frequent_unit)

    def test_policy_fallback_is_only_for_explicit_transport_unavailability(self) -> None:
        self.assertIn("exit 75", self.attester)
        self.assertIn("policy_attester_rc == 75", self.restore)
        self.assertIn(
            "retention policy, credential scope, or local attestation contract is invalid",
            self.restore,
        )

    def test_secrets_are_not_emitted_or_put_in_process_arguments(self) -> None:
        for script in (
            self.nightly,
            self.artifacts,
            self.attester,
            self.capture,
            self.restore,
            self.offline,
            self.generic_restore,
        ):
            self.assertNotIn("set -x", script)
        self.assertNotRegex(self.nightly, r'curl[^\n]*\$KUMA_URL')
        self.assertIn("curl --disable --config -", self.nightly)
        self.assertIn("curl --disable --config -", self.attester)
        for script in (self.nightly, self.attester):
            self.assertIn("env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C", script)
            self.assertIn('noproxy = "*"', script)
        self.assertNotRegex(self.attester, r"export[^\n]*(?:TOKEN|token)")
        self.assertNotRegex(self.attester, r"curl[^\n]*\$(?:token|TOKEN)")
        self.assertIn("Authorization: Bearer %s", self.attester)
        self.assertIn("verify-rclone-source", self.attester)
        self.assertIn("cross-bucket probe was not an explicit authorization denial", self.attester)
        helper = (ROOT / "scripts/postiz-backup-manifest.py").read_text()
        self.assertNotIn('invalid rclone config structure: {exc}', helper)
        for script in (
            self.nightly,
            self.frequent,
            self.artifacts,
            self.restore,
            self.attester,
            self.generic_restore,
        ):
            self.assertIn("HOME=/nonexistent", script)
        self.assertNotRegex(self.offline, r"(?:cat|sed|grep).*postiz\.env")
        self.assertIn("key names and empty/non-empty", self.offline)

    def test_generic_restore_invokes_strict_postiz_branch(self) -> None:
        self.assertIn("--network none", self.generic_restore)
        self.assertIn('if "$POSTIZ_DRILL"', self.generic_restore)
        self.assertIn("globals + 4 DB + Redis/config/uploads/state + 4 images", self.generic_restore)
        self.assertIn("write-generic-restore-journal", self.generic_restore)
        self.assertIn("generic-restore-journal-get", self.generic_restore)
        self.assertIn("freio.generic.restore-run", self.generic_restore)
        self.assertIn("reap_generic_restore", self.generic_restore)
        self.assertIn("--cleanup-only", self.generic_restore)
        self.assertIn("docker ps -a --no-trunc", self.generic_restore)
        self.assertIn(
            "cannot prove generic restore container presence/absence",
            self.generic_restore,
        )
        self.assertNotIn('"$container" 2>/dev/null || true', self.generic_restore)
        self.assertIn("MAX_GENERIC_CIPHER_BYTES", self.generic_restore)
        self.assertIn("MAX_GENERIC_PLAIN_BYTES", self.generic_restore)
        self.assertIn("--format sp --separator '|'", self.generic_restore)
        self.assertIn("df -B1 --output=avail", self.generic_restore)
        self.assertIn("df --output=iavail", self.generic_restore)
        self.assertNotIn("df -PB1 --output=avail", self.generic_restore)
        self.assertNotIn("df -Pi --output=iavail", self.generic_restore)
        self.assertIn("MemAvailable", self.generic_restore)
        self.assertIn('--tmpfs "/var/lib/postgresql/data:', self.generic_restore)
        self.assertIn('--mount "type=bind,src=$WORK,dst=/restore,readonly"', self.generic_restore)
        self.assertNotIn("docker cp", self.generic_restore)
        self.assertIn("ulimit -f $((MAX_GENERIC_CIPHER_BYTES / 1024))", self.generic_restore)
        self.assertIn("ulimit -f $((MAX_GENERIC_PLAIN_BYTES / 1024))", self.generic_restore)
        self.assertLess(
            self.generic_restore.index("cleanup || die 'generic restore cleanup failed"),
            self.generic_restore.index('if "$POSTIZ_DRILL"'),
        )

    def test_existing_cas_and_full_runtime_generation_are_reverified(self) -> None:
        self.assertIn("verify_remote_blob_set \"$PRIMARY\" primary", self.artifacts)
        self.assertIn("verify_remote_blob_set \"$DR\" dr", self.artifacts)
        self.assertIn("decrypt to its content address", self.artifacts)
        self.assertIn("primary and DR image ciphertexts are not the same verified object", self.artifacts)
        self.assertIn("--runtime-config-archive", self.artifacts)
        self.assertGreaterEqual(self.artifacts.count("verify-config-source"), 2)
        self.assertIn("--files-from \"$blob_list\"", self.artifacts)
        self.assertNotIn('lsf "$remote/$BLOB_PREFIX" --recursive --files-only \\\n+    --format', self.artifacts)


if __name__ == "__main__":
    unittest.main()
