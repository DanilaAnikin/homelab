from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from helpers import NOW_DT, batch_envelope, research_document

from freio_prospecting.common import ValidationError, canonical_json_bytes
from freio_prospecting.workflow import Spool


class SpoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "spool"
        self.spool = Spool(self.root)
        self.spool.bind_identity_key(b"i" * 32)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def enqueue(self):
        return self.spool.enqueue_research(research_document())

    def test_enqueue_is_atomic_and_idempotent(self) -> None:
        first = self.enqueue()
        second = self.enqueue()
        self.assertEqual(first, second)
        self.assertEqual(len(list(self.spool.ready.glob("*.json"))), 1)
        self.assertTrue(first.read_bytes().endswith(b"\n"))

    def test_ready_manifest_rejects_more_than_one_candidate(self) -> None:
        second = research_document()["candidates"][0].copy()
        second["name"] = "Druhý tvůrce"
        with self.assertRaisesRegex(ValidationError, "exactly one"):
            self.spool.enqueue_research(
                research_document(research_document()["candidates"][0], second)
            )

    def test_claim_moves_artifact_out_of_ready(self) -> None:
        path = self.enqueue()
        claimed = self.spool.claim_next()
        self.assertIsNotNone(claimed)
        self.assertFalse(path.exists())
        self.assertEqual(claimed.path.parent, self.spool.processing)

    def test_invalid_claim_is_quarantined(self) -> None:
        self.spool.ensure()
        path = self.spool.ready / f"{'a' * 64}.json"
        path.write_text("{}", encoding="utf-8")
        self.assertIsNone(self.spool.claim_next())
        self.assertFalse(path.exists())
        self.assertTrue(any(self.spool.claimed_quarantine.glob("*.error.json")))

    def test_mark_processed_records_only_result_hash_and_bounded_result(self) -> None:
        self.enqueue()
        claimed = self.spool.claim_next()
        with mock.patch("freio_prospecting.schema.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = NOW_DT
            self.spool.persist_signed_request(claimed, batch_envelope())
            destination = self.spool.mark_processed(
                claimed,
                {"httpStatus": 200, "acceptedCount": 1},
                batch_envelope(),
                ("hmac-sha256:" + "a" * 64,),
            )
        receipt = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(receipt["result"]["acceptedCount"], 1)
        self.assertEqual(len(receipt["resultSha256"]), 64)

    def test_persisted_request_is_canonical_and_submit_private(self) -> None:
        self.enqueue()
        claimed = self.spool.claim_next()
        with mock.patch("freio_prospecting.schema.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = NOW_DT
            body = self.spool.persist_signed_request(claimed, batch_envelope())
            processing = self.spool.processing_next()
        self.assertEqual(canonical_json_bytes(processing.signed_request), body)
        self.assertEqual(self.spool.request_path(claimed).stat().st_mode & 0o777, 0o600)

    def test_recovers_only_stale_processing(self) -> None:
        self.enqueue()
        claimed = self.spool.claim_next()
        old = time.time() - 3600
        os.utime(claimed.path, (old, old))
        self.assertEqual(self.spool.recover_stale_processing(1800), 1)
        self.assertTrue(claimed.path.exists())
        self.assertFalse((self.spool.ready / claimed.path.name).exists())

    def test_durable_success_marker_finishes_crashed_processed_transition_without_retry(
        self,
    ) -> None:
        self.enqueue()
        claimed = self.spool.claim_next()
        request = batch_envelope()
        with mock.patch("freio_prospecting.schema.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = NOW_DT
            self.spool.persist_signed_request(claimed, request)
            with mock.patch.object(
                self.spool, "_complete_success", side_effect=RuntimeError("crash")
            ):
                with self.assertRaises(RuntimeError):
                    self.spool.mark_processed(
                        claimed,
                        {"httpStatus": 200, "acceptedCount": 1},
                        request,
                        ("hmac-sha256:" + "a" * 64,),
                    )
            self.assertTrue(self.spool.success_path(claimed).exists())
            self.assertIsNone(self.spool.processing_next())
        processed = self.spool.processed / f"{claimed.path.stem}.receipt.json"
        self.assertTrue(processed.exists())
        self.assertFalse(list(self.spool.processing.glob("*.request.json")))

    def test_rejects_world_writable_spool(self) -> None:
        self.root.mkdir()
        self.root.chmod(0o777)
        with self.assertRaisesRegex(ValidationError, "world-writable"):
            self.spool.ensure()

    def test_rejects_group_writable_spool_root(self) -> None:
        self.root.mkdir(mode=0o770)
        self.root.chmod(0o770)
        with self.assertRaisesRegex(ValidationError, "group or others"):
            self.spool.ensure()

    def test_rejects_symlink_directory(self) -> None:
        actual = Path(self.temporary.name) / "actual"
        actual.mkdir()
        link = Path(self.temporary.name) / "link"
        link.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(ValidationError):
            Spool(link).ensure()

    def test_untrusted_quarantine_is_content_addressed(self) -> None:
        destination = self.spool.quarantine_untrusted(
            b'{"bad":true}', "invalid", "bad shape"
        )
        again = self.spool.quarantine_untrusted(b'{"bad":true}', "invalid", "bad shape")
        self.assertEqual(destination, again)
        self.assertTrue(
            destination.with_name(f"{destination.stem}.error.json").exists()
        )

    def test_raw_ttl_purge_removes_email_and_url_but_not_ambiguous_request(
        self,
    ) -> None:
        self.enqueue()
        quarantine = self.spool.quarantine_untrusted(
            canonical_json_bytes(research_document()), "test", "terminal"
        )
        claimed = self.spool.claim_next()
        ready = self.enqueue()
        with mock.patch("freio_prospecting.schema.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = NOW_DT
            self.spool.persist_signed_request(claimed, batch_envelope())
        old = datetime.now(timezone.utc).timestamp() - 4 * 24 * 3600
        for path in (ready, quarantine, claimed.path, self.spool.request_path(claimed)):
            os.utime(path, (old, old))
        self.spool.purge_expired(now=datetime.now(timezone.utc), scope="all")
        self.assertFalse(ready.exists())
        self.assertFalse(quarantine.exists())
        # A request may have committed remotely, so blanket TTL cleanup never
        # destroys its only exact replay artifact before an uncertain tombstone.
        self.assertTrue(claimed.path.exists())
        self.assertTrue(self.spool.request_path(claimed).exists())
        remaining = b"\n".join(
            path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and path.parent != self.spool.processing
        )
        self.assertNotIn(b"hello@creator.example.cz", remaining)
        self.assertNotIn(b"https://creator.example.cz/contact", remaining)

    def test_processed_receipt_has_explicit_ttl_and_is_purged(self) -> None:
        self.enqueue()
        claimed = self.spool.claim_next()
        request = batch_envelope()
        self.spool.persist_signed_request(claimed, request)
        destination = self.spool.mark_processed(
            claimed,
            {"httpStatus": 200, "acceptedCount": 1},
            request,
            ("hmac-sha256:" + "b" * 64,),
            now=NOW_DT,
        )
        receipt = json.loads(destination.read_text())
        self.assertEqual(receipt["retentionSeconds"], 30 * 24 * 3600)
        self.spool.purge_expired(now=NOW_DT + timedelta(days=31), scope="submit")
        self.assertFalse(destination.exists())

    def test_stale_atomic_temps_are_purged_but_submit_lock_is_preserved(self) -> None:
        self.spool.ensure()
        temporary = self.spool.ready / (".candidate.json.1234.0123456789abcdef.tmp")
        temporary.write_text(
            "hello@creator.example.cz https://creator.example.cz/contact",
            encoding="utf-8",
        )
        self.spool.lock_path.write_text("lock", encoding="utf-8")
        old = datetime.now(timezone.utc).timestamp() - 4 * 24 * 3600
        os.utime(temporary, (old, old))
        os.utime(self.spool.lock_path, (old, old))
        self.spool.purge_expired(now=datetime.now(timezone.utc), scope="all")
        self.assertFalse(temporary.exists())
        self.assertTrue(self.spool.lock_path.exists())

    def test_startup_recovers_request_orphaned_by_old_quarantine_crash(self) -> None:
        self.enqueue()
        claimed = self.spool.claim_next()
        self.spool.persist_signed_request(claimed, batch_envelope())
        destination = self.spool.claimed_quarantine / claimed.path.name
        os.rename(claimed.path, destination)
        self.assertTrue(self.spool.request_path(claimed).exists())
        self.assertIsNone(self.spool.processing_next())
        self.assertFalse(self.spool.request_path(claimed).exists())
        self.assertTrue(
            destination.with_name(f"{destination.stem}.request.json").exists()
        )


class HousekeepingUnitTests(unittest.TestCase):
    def test_housekeeping_preserves_discovery_submit_identity_boundary(self) -> None:
        root = Path(__file__).resolve().parents[2] / "scripts" / "systemd"
        discovery = (root / "freio-prospect-discovery-housekeeping.service").read_text()
        submit = (root / "freio-prospect-submit-housekeeping.service").read_text()
        self.assertIn("User=freio-discovery", discovery)
        self.assertIn("User=freio-submit", submit)
        self.assertNotIn("User=root", discovery + submit)
        self.assertIn("RestrictAddressFamilies=AF_UNIX", discovery)
        self.assertIn("RestrictAddressFamilies=AF_UNIX", submit)
        self.assertNotIn("submit-hmac", submit)
        self.assertNotIn("claude-token", discovery + submit)


if __name__ == "__main__":
    unittest.main()
