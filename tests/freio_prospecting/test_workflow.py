from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
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
            )
        sidecar = destination.with_name(f"{destination.stem}.result.json")
        receipt = json.loads(sidecar.read_text(encoding="utf-8"))
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
                self.spool, "_complete_processed", side_effect=RuntimeError("crash")
            ):
                with self.assertRaises(RuntimeError):
                    self.spool.mark_processed(
                        claimed,
                        {"httpStatus": 200, "acceptedCount": 1},
                        request,
                    )
            self.assertTrue(self.spool.success_path(claimed).exists())
            self.assertIsNone(self.spool.processing_next())
        processed = self.spool.processed / claimed.path.name
        self.assertTrue(processed.exists())
        self.assertTrue(processed.with_name(f"{processed.stem}.request.json").exists())
        self.assertTrue(processed.with_name(f"{processed.stem}.result.json").exists())

    def test_rejects_world_writable_spool(self) -> None:
        self.root.mkdir()
        self.root.chmod(0o777)
        with self.assertRaisesRegex(ValidationError, "world-writable"):
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


if __name__ == "__main__":
    unittest.main()
