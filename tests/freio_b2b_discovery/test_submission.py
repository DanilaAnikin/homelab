from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from helpers import (
    ENDPOINT,
    IDENTITY_SECRET,
    TRANSPORT_SECRET,
    candidate,
    fetched_document,
    success_response,
)

from freio_b2b_discovery.state import Spool
from freio_b2b_discovery.submission import SubmitResponse, SubmissionWorker


class FakeFetcher:
    def __init__(self, document):  # type: ignore[no-untyped-def]
        self.document = document
        self.calls: list[str] = []

    def fetch(self, url: str):  # type: ignore[no-untyped-def]
        self.calls.append(url)
        if isinstance(self.document, dict):
            return self.document[url]
        return self.document


class RecordingTransport:
    def __init__(self, status: int = 200, body: bytes | None = None) -> None:
        self.status = status
        self.body = body
        self.requests = []

    def __call__(self, _endpoint, request, _timeout):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        body = self.body
        if body is None and 200 <= self.status < 300:
            import json

            receipt = json.loads(request.body)["receipt"]["receiptId"]
            body = success_response(receipt)
        return SubmitResponse(self.status, {}, body or b"{}")


class SubmissionTests(unittest.TestCase):
    def _worker(
        self,
        spool: Spool,
        transport: RecordingTransport,
        *,
        document=None,  # type: ignore[no-untyped-def]
        fetcher=None,  # type: ignore[no-untyped-def]
    ) -> SubmissionWorker:
        return SubmissionWorker(
            spool=spool,
            endpoint=ENDPOINT,
            transport_secret=TRANSPORT_SECRET,
            identity_secret=IDENTITY_SECRET,
            transport=transport,
            fetcher=fetcher or FakeFetcher(document or fetched_document()),
            monotonic=lambda: 0,
        )

    def test_refetches_persists_and_only_posts_exact_hmac_intake(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = Spool(Path(directory) / "spool")
            spool.enqueue(candidate())
            transport = RecordingTransport()
            result = self._worker(spool, transport).run(1)
            self.assertEqual(result["processed"], 1)
            self.assertEqual(len(transport.requests), 1)
            request = transport.requests[0]
            self.assertEqual(request.headers["X-Freio-B2B-Discovery-Version"], "1")
            self.assertTrue(request.headers["Idempotency-Key"].startswith("sha256:"))
            lowered = request.body.lower()
            self.assertNotIn(b"authorization", lowered)
            self.assertNotIn(b"consent", lowered)
            self.assertNotIn(b"outreach", lowered)
            self.assertNotIn(
                b'"name"', lowered.split(b'"contact":', 1)[1].split(b"}", 1)[0]
            )
            self.assertNotIn(b'"role"', lowered)
            self.assertEqual(len(tuple(spool.processing.iterdir())), 0)

    def test_submit_refetches_contact_and_legal_entity_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = Spool(Path(directory) / "spool")
            current = candidate(
                legal_entity_source_url="https://www.doucovani.cz/o-spolecnosti"
            )
            spool.enqueue(current)
            fetcher = FakeFetcher(
                {
                    current.source_url: fetched_document(),
                    current.legal_entity_source_url: fetched_document(
                        final_url=current.legal_entity_source_url
                    ),
                }
            )
            result = self._worker(spool, RecordingTransport(), fetcher=fetcher).run(1)
            self.assertEqual(result["processed"], 1)
            self.assertEqual(
                fetcher.calls,
                [current.source_url, current.legal_entity_source_url],
            )

    def test_identity_duplicate_skips_the_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = Spool(Path(directory) / "spool")
            first = candidate()
            spool.enqueue(first)
            self._worker(spool, RecordingTransport()).run(1)
            changed = candidate(name="Doučování Příklad Akademie s.r.o.")
            spool.enqueue(changed)
            transport = RecordingTransport()
            result = self._worker(
                spool,
                transport,
                document=fetched_document(legal_name=changed.name),
            ).run(1)
            self.assertEqual(result["duplicates"], 1)
            self.assertEqual(transport.requests, [])

    def test_transient_remote_failure_keeps_exact_request_and_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = Spool(Path(directory) / "spool")
            spool.enqueue(candidate())
            transport = RecordingTransport(status=503)
            result = self._worker(spool, transport).run(1)
            self.assertEqual(result["deferred"], 1)
            manifests = [
                path
                for path in spool.processing.glob("*.json")
                if path.name.count(".") == 1
            ]
            self.assertEqual(len(manifests), 1)
            self.assertTrue(spool.request_path(manifests[0]).exists())
            self.assertTrue(spool.retry_path(manifests[0]).exists())

    def test_disabled_or_invalid_success_opens_hash_only_global_circuit(self) -> None:
        for status, body in ((403, b'{"error":"disabled"}'), (200, b'{"bad":true}')):
            with self.subTest(status=status):
                with tempfile.TemporaryDirectory() as directory:
                    spool = Spool(Path(directory) / "spool")
                    spool.enqueue(candidate())
                    result = self._worker(
                        spool, RecordingTransport(status=status, body=body)
                    ).run(1)
                    self.assertEqual(result["deferred"], 1)
                    self.assertTrue(spool.circuit_path.exists())
                    serialized = spool.circuit_path.read_text()
                    self.assertNotIn("kontakt@", serialized)
                    self.assertNotIn("doucovani.cz", serialized)

    def test_conflict_becomes_durable_uncertain_without_retrying(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = Spool(Path(directory) / "spool")
            spool.enqueue(candidate())
            transport = RecordingTransport(status=409, body=b'{"error":"conflict"}')
            result = self._worker(spool, transport).run(1)
            self.assertEqual(result["uncertain"], 1)
            self.assertEqual(len(transport.requests), 1)
            self.assertTrue(any(spool.claimed_quarantine.glob("*.request.json")))


if __name__ == "__main__":
    unittest.main()
