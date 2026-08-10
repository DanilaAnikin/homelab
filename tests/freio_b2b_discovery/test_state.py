from __future__ import annotations

import json
import errno
import os
import stat
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from helpers import IDENTITY_SECRET, candidate, fetched_document

from freio_b2b_discovery.model import build_verified_envelope
from freio_b2b_discovery.state import Spool
from freio_prospecting.common import ValidationError, sha256_hex


class StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "spool"
        self.spool = Spool(self.root)
        self.spool.ensure()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_content_addressed_enqueue_claim_and_hash_only_success(self) -> None:
        queued = self.spool.enqueue(candidate())
        self.assertEqual(self.spool.enqueue(candidate()), queued)
        claimed = self.spool.claim_next()
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed.path.parent, self.spool.processing)
        envelope = build_verified_envelope(
            claimed.candidate, fetched_document(), fetched_document()
        )
        body = self.spool.persist_request(claimed, envelope)
        self.assertEqual(
            sha256_hex(body),
            sha256_hex(self.spool.request_path(claimed.path).read_bytes()),
        )
        digests = self.spool.identity_digests(claimed.candidate, IDENTITY_SECRET)
        receipt = self.spool.mark_processed(
            claimed,
            envelope,
            {"responseSha256": "f" * 64},
            digests,
        )
        value = json.loads(receipt.read_text())
        self.assertEqual(value["outcome"], "accepted")
        self.assertNotIn("doucovani.cz", receipt.read_text())
        self.assertNotIn("kontakt@", receipt.read_text())
        self.assertFalse(claimed.path.exists())
        self.assertFalse(self.spool.request_path(claimed.path).exists())

    def test_ready_handoff_is_group_readable_under_private_process_umask(self) -> None:
        previous_umask = os.umask(0o077)
        try:
            queued = self.spool.enqueue(candidate())
        finally:
            os.umask(previous_umask)
        self.assertEqual(stat.S_IMODE(queued.stat().st_mode), 0o640)
        self.assertEqual(queued.stat().st_gid, self.spool.ready.stat().st_gid)
        self.spool._load_manifest(queued)

    def test_cross_mount_claim_fails_closed_without_copying_manifest(self) -> None:
        queued = self.spool.enqueue(candidate())
        with mock.patch(
            "freio_b2b_discovery.state.os.rename",
            side_effect=OSError(errno.EXDEV, "cross-device link"),
        ):
            with self.assertRaisesRegex(ValidationError, "atomic rename"):
                self.spool.claim_next()
        self.assertTrue(queued.exists())
        self.assertFalse(tuple(self.spool.processing.iterdir()))

    def test_identity_index_blocks_a_new_receipt_without_storing_pii(self) -> None:
        first = self.spool.claim_next()
        self.assertIsNone(first)
        self.spool.enqueue(candidate())
        claimed = self.spool.claim_next()
        assert claimed is not None
        digests = self.spool.identity_digests(claimed.candidate, IDENTITY_SECRET)
        self.spool.bind_or_check_identities(
            digests, "sha256:" + "a" * 64, claimed.path.stem, "accepted"
        )
        matches = self.spool.identity_matches(digests, "sha256:" + "b" * 64)
        self.assertEqual(matches, digests)
        serialized = self.spool.identity_index_path.read_text()
        self.assertNotIn("doucovani.cz", serialized)
        self.assertNotIn("kontakt@", serialized)

    def test_retry_is_bounded_and_429_waits_for_next_utc_window(self) -> None:
        self.spool.enqueue(candidate())
        claimed = self.spool.claim_next()
        assert claimed is not None
        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(
            self.spool.record_retry(claimed.path, "remote_http_429", now=now)
        )
        retry = self.spool._load_retry(claimed.path)
        self.assertEqual(retry.attempts, 1)
        self.assertGreaterEqual(
            retry.next_at,
            datetime(2026, 8, 11, 0, 5, tzinfo=timezone.utc),
        )
        self.assertLessEqual(retry.expires_at, now + timedelta(hours=24))

    def test_global_circuit_is_hash_only_and_requires_explicit_clear(self) -> None:
        self.spool.open_circuit("remote_http_403", "a" * 64)
        with self.assertRaises(ValidationError):
            self.spool.assert_circuit_closed()
        serialized = self.spool.circuit_path.read_text()
        self.assertNotIn("email", serialized)
        self.assertTrue(self.spool.clear_circuit())
        self.spool.assert_circuit_closed()

    def test_uncertain_bundle_can_be_reconciled_accepted_or_requeued(self) -> None:
        for accepted in (True, False):
            with self.subTest(accepted=accepted):
                suffix = int(accepted)
                current = candidate(
                    website=f"https://www.doucovani{suffix}.cz/",
                    source_url=f"https://www.doucovani{suffix}.cz/kontakt",
                    legal_entity_source_url=(
                        f"https://www.doucovani{suffix}.cz/kontakt"
                    ),
                    email=f"kontakt@doucovani{suffix}.cz",
                )
                self.spool.enqueue(current)
                claimed = self.spool.claim_next()
                assert claimed is not None
                document = fetched_document(
                    email=current.email,
                    final_url=current.source_url,
                )
                envelope = build_verified_envelope(current, document, document)
                self.spool.persist_request(claimed, envelope)
                receipt_id = envelope["receipt"]["receiptId"]
                digests = self.spool.identity_digests(current, IDENTITY_SECRET)
                self.spool.bind_or_check_identities(
                    digests, receipt_id, claimed.path.stem, "uncertain"
                )
                self.spool.quarantine_claimed(
                    claimed, "remote_conflict", uncertain=True
                )
                result = self.spool.reconcile_uncertain(
                    receipt_id,
                    accepted=accepted,
                    identity_secret=IDENTITY_SECRET,
                )
                self.assertIn("accepted" if accepted else "requeued", result)

    def test_purge_enforces_72_hour_raw_and_30_day_receipt_limits(self) -> None:
        queued = self.spool.enqueue(candidate())
        old = datetime.now(timezone.utc) - timedelta(hours=73)
        os.utime(queued, (old.timestamp(), old.timestamp()))
        result = self.spool.purge("discovery")
        self.assertEqual(result["raw"], 1)
        self.assertFalse(queued.exists())


if __name__ == "__main__":
    unittest.main()
