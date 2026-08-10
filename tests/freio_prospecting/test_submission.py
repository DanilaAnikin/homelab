from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import NOW_DT, fetched_document, research_document

from freio_prospecting.common import canonical_json_bytes
from freio_prospecting.submission import SubmissionWorker, SubmitResponse
from freio_prospecting.workflow import Spool


class RecordingTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, endpoint, request, timeout):
        self.calls.append((endpoint, request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def success_response(count=1):
    body = canonical_json_bytes(
        {
            "intake": {
                "success": True,
                "count": count,
                "results": [
                    {
                        "index": index,
                        "status": "created",
                        "partner_id": "00000000-0000-4000-8000-000000000001",
                        "contact_id": None,
                        "provenance_id": None,
                        "created_partner": True,
                    }
                    for index in range(count)
                ],
            }
        }
    )
    return SubmitResponse(200, {"content-type": "application/json"}, body)


class RecordingFetcher:
    def __init__(self, document=None):
        self.document = document or fetched_document()
        self.calls = []

    def fetch(self, source_url):
        self.calls.append(source_url)
        return self.document


class SubmissionWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.spool = Spool(Path(self.temporary.name) / "spool")
        self.ready_path = self.spool.enqueue_research(research_document())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_worker(self, transport, fetcher=None):
        with mock.patch("freio_prospecting.schema.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = NOW_DT
            return SubmissionWorker(
                spool=self.spool,
                endpoint="https://outreach.freio.cz/api/internal/growth-partners/prospect-intake",
                secret=b"s" * 32,
                transport=transport,
                fetcher=fetcher or RecordingFetcher(),
            ).run()

    def test_success_moves_batch_to_processed_and_sets_idempotency_receipt(
        self,
    ) -> None:
        transport = RecordingTransport([success_response()])
        counters = self.run_worker(transport)
        self.assertEqual(counters["processed"], 1)
        self.assertFalse(self.ready_path.exists())
        processed = next(
            path
            for path in self.spool.processed.glob("[0-9a-f]*.json")
            if ".request." not in path.name and ".result." not in path.name
        )
        envelope = json.loads(
            processed.with_name(f"{processed.stem}.request.json").read_text(
                encoding="utf-8"
            )
        )
        request = transport.calls[0][1]
        self.assertEqual(
            request.headers["Idempotency-Key"], envelope["receipt"]["receiptId"]
        )
        self.assertEqual(json.loads(request.body), envelope)
        self.assertIn("fetchReceipt", envelope["items"][0])
        self.assertNotIn("bodyBase64", envelope["items"][0]["fetchReceipt"])
        self.assertLessEqual(len(request.body), 96 * 1024)

    def test_permanent_4xx_quarantines_without_logging_body(self) -> None:
        response = SubmitResponse(400, {}, b'{"error":"sensitive server detail"}')
        counters = self.run_worker(RecordingTransport([response]))
        self.assertEqual(counters["quarantined"], 1)
        sidecar = next(self.spool.claimed_quarantine.glob("*.error.json"))
        detail = sidecar.read_text(encoding="utf-8")
        self.assertIn("response_sha256", detail)
        self.assertNotIn("sensitive server detail", detail)

    def test_transient_5xx_keeps_exact_request_in_submit_private_processing(
        self,
    ) -> None:
        transport = RecordingTransport([SubmitResponse(503, {}, b"unavailable")])
        counters = self.run_worker(transport)
        self.assertEqual(counters["deferred"], 1)
        self.assertFalse(self.ready_path.exists())
        persisted = next(self.spool.processing.glob("*.request.json"))
        self.assertEqual(
            persisted.read_bytes().rstrip(b"\n"), transport.calls[0][1].body
        )

    def test_network_error_returns_batch_and_stops_invocation(self) -> None:
        counters = self.run_worker(RecordingTransport([OSError("network down")]))
        self.assertEqual(counters["deferred"], 1)
        self.assertFalse(self.ready_path.exists())
        self.assertTrue(list(self.spool.processing.glob("*.request.json")))

    def test_malformed_2xx_is_deferred_not_lost(self) -> None:
        response = SubmitResponse(200, {}, b'{"ok":true}')
        counters = self.run_worker(RecordingTransport([response]))
        self.assertEqual(counters["deferred"], 1)
        self.assertTrue(list(self.spool.processing.glob("*.request.json")))

    def test_count_mismatch_is_deferred(self) -> None:
        response = success_response(count=0)
        counters = self.run_worker(RecordingTransport([response]))
        self.assertEqual(counters["deferred"], 1)
        self.assertTrue(list(self.spool.processing.glob("*.request.json")))

    def test_rate_limit_is_transient_but_conflict_is_quarantined(self) -> None:
        counters = self.run_worker(
            RecordingTransport([SubmitResponse(429, {}, b"slow")])
        )
        self.assertEqual(counters["deferred"], 1)
        # Reset fixture state and assert an idempotency conflict cannot loop forever.
        counters = self.run_worker(
            RecordingTransport([SubmitResponse(409, {}, b"conflict")])
        )
        self.assertEqual(counters["quarantined"], 1)

    def test_submit_identity_refetches_and_does_not_trust_discovery_claim(self) -> None:
        fetcher = RecordingFetcher(
            fetched_document(email="different@creator.example.cz")
        )
        transport = RecordingTransport([success_response()])
        counters = self.run_worker(transport, fetcher)
        self.assertEqual(counters["quarantined"], 1)
        self.assertEqual(fetcher.calls, ["https://creator.example.cz/contact"])
        self.assertEqual(transport.calls, [])

    def test_forged_discovery_receipt_is_rejected_before_fetch_or_sign(self) -> None:
        # A compromised discovery process may write into ready, but ready accepts
        # only the strict candidate schema and canonical content hash.
        forged = {
            "schemaVersion": "1",
            "candidates": research_document()["candidates"],
            "receipt": {"payloadSha256": "0" * 64, "verified": True},
        }
        raw = canonical_json_bytes(forged) + b"\n"
        forged_path = self.spool.ready / f"{'f' * 64}.json"
        forged_path.write_bytes(raw)
        self.ready_path.unlink()
        fetcher = RecordingFetcher()
        transport = RecordingTransport([success_response()])
        counters = self.run_worker(transport, fetcher)
        self.assertEqual(counters["processed"], 0)
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(transport.calls, [])
        self.assertTrue(list(self.spool.claimed_quarantine.glob("*.error.json")))

    def test_lost_response_retry_uses_exact_same_body_hash_and_idempotency_key(
        self,
    ) -> None:
        first_transport = RecordingTransport(
            [TimeoutError("response lost after remote commit")]
        )
        first_fetcher = RecordingFetcher()
        first = self.run_worker(first_transport, first_fetcher)
        self.assertEqual(first["deferred"], 1)
        first_request = first_transport.calls[0][1]
        self.assertEqual(len(first_fetcher.calls), 1)

        second_transport = RecordingTransport([success_response()])
        second_fetcher = RecordingFetcher(
            fetched_document(email="changed-page@creator.example.cz")
        )
        second = self.run_worker(second_transport, second_fetcher)
        self.assertEqual(second["processed"], 1)
        second_request = second_transport.calls[0][1]
        self.assertEqual(second_fetcher.calls, [])
        self.assertEqual(second_request.body, first_request.body)
        self.assertEqual(
            second_request.headers["X-Freio-Prospecting-Content-SHA256"],
            first_request.headers["X-Freio-Prospecting-Content-SHA256"],
        )
        self.assertEqual(
            second_request.headers["Idempotency-Key"],
            first_request.headers["Idempotency-Key"],
        )


if __name__ == "__main__":
    unittest.main()
