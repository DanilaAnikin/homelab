from __future__ import annotations

import http.client
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import (
    NOW_DT,
    fetched_document,
    research_candidate,
    research_document,
)

from freio_prospecting.common import atomic_write_bytes, canonical_json_bytes
from freio_prospecting.fetcher import FETCH_TIMEOUT_SECONDS, FetchError
from freio_prospecting.submission import (
    SUBMIT_OVERALL_BUDGET_SECONDS,
    SubmissionWorker,
    SubmitResponse,
)
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

    def run_worker(self, transport, fetcher=None, maximum_batches=10):
        with mock.patch("freio_prospecting.schema.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = NOW_DT
            return SubmissionWorker(
                spool=self.spool,
                endpoint="https://outreach.freio.cz/api/internal/growth-partners/prospect-intake",
                secret=b"s" * 32,
                identity_secret=b"i" * 32,
                transport=transport,
                fetcher=fetcher or RecordingFetcher(),
            ).run(maximum_batches)

    def make_retry_due(self):
        retry = next(self.spool.processing.glob("*.retry.json"))
        value = json.loads(retry.read_text(encoding="utf-8"))
        value["nextAttemptAt"] = value["lastDeferredAt"]
        retry.write_bytes(canonical_json_bytes(value) + b"\n")

    def test_success_moves_batch_to_processed_and_sets_idempotency_receipt(
        self,
    ) -> None:
        transport = RecordingTransport([success_response()])
        counters = self.run_worker(transport)
        self.assertEqual(counters["processed"], 1)
        self.assertFalse(self.ready_path.exists())
        processed = next(self.spool.processed.glob("*.receipt.json"))
        envelope = json.loads(transport.calls[0][1].body)
        request = transport.calls[0][1]
        self.assertEqual(
            request.headers["Idempotency-Key"], envelope["receipt"]["receiptId"]
        )
        self.assertEqual(json.loads(request.body), envelope)
        self.assertIn("fetchReceipt", envelope["items"][0])
        self.assertNotIn("bodyBase64", envelope["items"][0]["fetchReceipt"])
        self.assertLessEqual(len(request.body), 96 * 1024)
        stored = processed.read_text(encoding="utf-8")
        self.assertNotIn("hello@creator.example.cz", stored)
        self.assertNotIn("https://creator.example.cz/contact", stored)
        self.assertFalse(list(self.spool.processed.glob("*.request.json")))

    def test_contract_400_opens_global_circuit_without_consuming_candidate(
        self,
    ) -> None:
        response = SubmitResponse(400, {}, b'{"error":"sensitive server detail"}')
        counters = self.run_worker(RecordingTransport([response]))
        self.assertEqual(counters["deferred"], 1)
        self.assertTrue(self.spool.global_circuit_path.exists())
        self.assertTrue(list(self.spool.processing.glob("[0-9a-f]*.request.json")))
        state = self.spool.global_circuit_path.read_text(encoding="utf-8")
        self.assertNotIn("sensitive server detail", state)
        with self.assertRaisesRegex(Exception, "global submit circuit"):
            self.run_worker(RecordingTransport([success_response()]))
        self.assertFalse(list(self.spool.claimed_quarantine.glob("*.error.json")))

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

    def test_truncated_http_response_is_ambiguous_and_retryable(self) -> None:
        counters = self.run_worker(
            RecordingTransport([http.client.IncompleteRead(b"partial")])
        )
        self.assertEqual(counters["deferred"], 1)
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
        self.make_retry_due()
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

        self.make_retry_due()
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

    def test_cross_run_social_dedupe_skips_changed_evidence_before_post(self) -> None:
        first_transport = RecordingTransport([success_response()])
        self.run_worker(first_transport)
        changed = research_candidate(
            name="Přejmenovaný profil",
            sourceUrl="https://other.example.cz/new-evidence",
            claimedEmail="new@other.example.cz",
        )
        self.spool.enqueue_research(research_document(changed))
        fetcher = RecordingFetcher(
            fetched_document(
                requested_url=changed["sourceUrl"],
                final_url=changed["sourceUrl"],
                email=changed["claimedEmail"],
            )
        )
        transport = RecordingTransport([])
        counters = self.run_worker(transport, fetcher)
        self.assertEqual(counters["duplicates"], 1)
        self.assertEqual(transport.calls, [])
        index = self.spool.identity_index_path.read_text(encoding="utf-8")
        self.assertNotIn("maturita_bez_stresu", index)
        self.assertNotIn("hello@creator.example.cz", index)
        self.assertNotIn("creator.example.cz", index)

    def test_cross_run_normalized_email_dedupe_ignores_new_social_and_source(
        self,
    ) -> None:
        self.run_worker(RecordingTransport([success_response()]))
        changed = research_candidate(
            name="Jiný profil se stejným kontaktem",
            handle="@jiny_profil",
            sourceUrl="https://other.example.cz/contact",
            claimedEmail="HELLO@CREATOR.EXAMPLE.CZ",
        )
        self.spool.enqueue_research(research_document(changed))
        fetcher = RecordingFetcher(
            fetched_document(
                requested_url=changed["sourceUrl"],
                final_url=changed["sourceUrl"],
                email="hello@creator.example.cz",
            )
        )
        transport = RecordingTransport([])
        counters = self.run_worker(transport, fetcher)
        self.assertEqual(counters["duplicates"], 1)
        self.assertEqual(transport.calls, [])

    def test_exact_rediscovery_and_ready_processing_overlap_are_idempotent(
        self,
    ) -> None:
        first = RecordingTransport([TimeoutError("response lost")])
        self.run_worker(first)
        # Discovery can recreate the same content-addressed ready item while
        # its first copy remains submit-private in processing.
        duplicate_ready = self.spool.enqueue_research(research_document())
        self.assertTrue(duplicate_ready.exists())
        self.make_retry_due()
        transport = RecordingTransport([success_response()])
        counters = self.run_worker(transport)
        self.assertEqual((counters["processed"], counters["duplicates"]), (1, 1))
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(list(self.spool.processed.glob("*.receipt.json"))), 1)

    def test_global_404_defers_and_preserves_neighbour(self) -> None:
        second = research_candidate(
            name="Druhý tvůrce",
            handle="@druhy",
            sourceUrl="https://second.example.cz/contact",
            claimedEmail="hello@second.example.cz",
        )
        self.spool.enqueue_research(research_document(second))

        class PerUrlFetcher:
            def fetch(_self, url):
                email = (
                    "hello@second.example.cz"
                    if "second.example.cz" in url
                    else "hello@creator.example.cz"
                )
                return fetched_document(requested_url=url, final_url=url, email=email)

        transport = RecordingTransport([SubmitResponse(404, {}, b"route missing")])
        counters = self.run_worker(transport, PerUrlFetcher())
        self.assertEqual(counters["deferred"], 1)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(
            len(
                [
                    path
                    for path in self.spool.processing.glob("[0-9a-f]*.json")
                    if ".request." not in path.name and ".retry." not in path.name
                ]
            ),
            1,
        )
        self.assertEqual(len(list(self.spool.ready.glob("[0-9a-f]*.json"))), 1)
        self.assertFalse(list(self.spool.claimed_quarantine.glob("*.error.json")))

    def test_slow_transient_source_does_not_block_next_candidate(self) -> None:
        second = research_candidate(
            name="Druhý tvůrce",
            handle="@druhy",
            sourceUrl="https://second.example.cz/contact",
            claimedEmail="hello@second.example.cz",
        )
        second_path = self.spool.enqueue_research(research_document(second))
        first_path = self.ready_path
        first_url = (
            research_document()["candidates"][0]["sourceUrl"]
            if first_path.name < second_path.name
            else second["sourceUrl"]
        )

        class SlowFirstFetcher:
            calls = 0

            def fetch(_self, url):
                _self.calls += 1
                if url == first_url:
                    raise FetchError("timeout", "slow source")
                email = (
                    "hello@second.example.cz"
                    if "second.example.cz" in url
                    else "hello@creator.example.cz"
                )
                return fetched_document(requested_url=url, final_url=url, email=email)

        counters = self.run_worker(
            RecordingTransport([success_response()]), SlowFirstFetcher()
        )
        self.assertEqual((counters["deferred"], counters["processed"]), (1, 1))

    def test_submit_budget_fits_ten_slow_fetch_and_post_pairs(self) -> None:
        self.assertGreaterEqual(
            SUBMIT_OVERALL_BUDGET_SECONDS,
            10 * (FETCH_TIMEOUT_SECONDS + 10),
        )
        self.assertLess(SUBMIT_OVERALL_BUDGET_SECONDS, 240)

    def test_corrupt_identity_index_fails_loud_and_preserves_processing(self) -> None:
        self.spool.ensure()
        self.spool.identity_index_path.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(Exception, "identity index"):
            self.run_worker(RecordingTransport([success_response()]))
        self.assertTrue(list(self.spool.processing.glob("[0-9a-f]*.json")))
        self.assertFalse(list(self.spool.claimed_quarantine.glob("*.error.json")))

    def test_never_sent_expired_source_retry_has_no_uncertain_tombstone(self) -> None:
        fetcher = RecordingFetcher()
        fetcher.fetch = mock.Mock(side_effect=FetchError("timeout", "slow"))
        self.run_worker(RecordingTransport([]), fetcher)
        retry = next(self.spool.processing.glob("*.retry.json"))
        value = json.loads(retry.read_text(encoding="utf-8"))
        value.update(
            {
                "firstDeferredAt": "2026-08-01T00:00:00Z",
                "lastDeferredAt": "2026-08-01T00:00:00Z",
                "nextAttemptAt": "2026-08-01T00:00:00Z",
                "expiresAt": "2026-08-02T00:00:00Z",
            }
        )
        retry.write_bytes(canonical_json_bytes(value) + b"\n")
        counters = self.run_worker(RecordingTransport([success_response()]))
        self.assertEqual(counters["quarantined"], 1)
        self.assertFalse(self.spool.identity_index_path.exists())

    def test_uncertain_reconciliation_accepts_or_clears_and_requeues(self) -> None:
        counters = self.run_worker(
            RecordingTransport([SubmitResponse(409, {}, b"conflict")])
        )
        self.assertEqual(counters["quarantined"], 1)
        index = json.loads(self.spool.identity_index_path.read_text(encoding="utf-8"))
        receipt_id = next(iter(index["entries"].values()))["receiptId"]
        decision = self.spool.reconcile_uncertain_receipt(receipt_id, accepted=False)
        self.assertEqual(decision["requeued"], 1)
        self.assertTrue(list(self.spool.ready.glob("[0-9a-f]*.json")))
        self.assertFalse(
            json.loads(self.spool.identity_index_path.read_text())["entries"]
        )

        # A second conflict can be confirmed accepted after a DB check; raw
        # quarantine is then deleted and the hash-only suppression remains.
        self.run_worker(RecordingTransport([SubmitResponse(409, {}, b"conflict")]))
        index = json.loads(self.spool.identity_index_path.read_text())
        receipt_id = next(iter(index["entries"].values()))["receiptId"]
        decision = self.spool.reconcile_uncertain_receipt(receipt_id, accepted=True)
        self.assertEqual(decision["identities"], len(index["entries"]))
        final_index = json.loads(self.spool.identity_index_path.read_text())
        self.assertTrue(
            all(
                entry["status"] == "accepted"
                for entry in final_index["entries"].values()
            )
        )
        self.assertFalse(list(self.spool.claimed_quarantine.glob("[0-9a-f]*.json")))

    def test_invalid_pending_processing_becomes_operator_reconcilable_uncertain(
        self,
    ) -> None:
        self.run_worker(RecordingTransport([TimeoutError("lost response")]))
        manifest = next(
            path
            for path in self.spool.processing.glob("[0-9a-f]*.json")
            if ".request." not in path.name and ".retry." not in path.name
        )
        manifest.write_text("{}\n", encoding="utf-8")
        self.make_retry_due()
        self.run_worker(RecordingTransport([]))
        index = json.loads(self.spool.identity_index_path.read_text())
        self.assertTrue(
            all(entry["status"] == "uncertain" for entry in index["entries"].values())
        )
        receipt_id = next(iter(index["entries"].values()))["receiptId"]
        result = self.spool.reconcile_uncertain_receipt(receipt_id, accepted=True)
        self.assertGreater(result["identities"], 0)
        self.assertFalse(list(self.spool.claimed_quarantine.glob("[0-9a-f]*.json")))

    def test_not_accepted_reconciliation_resumes_after_index_write_crash(self) -> None:
        self.run_worker(RecordingTransport([SubmitResponse(409, {}, b"conflict")]))
        index = json.loads(self.spool.identity_index_path.read_text())
        receipt_id = next(iter(index["entries"].values()))["receiptId"]

        def crash_on_index(path, data, mode=0o640):
            if path == self.spool.identity_index_path:
                raise OSError("injected index fsync failure")
            return atomic_write_bytes(path, data, mode)

        with mock.patch(
            "freio_prospecting.workflow.atomic_write_bytes",
            side_effect=crash_on_index,
        ):
            with self.assertRaisesRegex(OSError, "injected"):
                self.spool.reconcile_uncertain_receipt(receipt_id, accepted=False)
        self.assertTrue(list(self.spool.state.glob("reconcile-*.json")))
        self.assertTrue(list(self.spool.ready.glob("[0-9a-f]*.json")))
        resumed = self.spool.reconcile_uncertain_receipt(receipt_id, accepted=False)
        self.assertGreater(resumed["identities"], 0)
        self.assertFalse(list(self.spool.state.glob("reconcile-*.json")))


if __name__ == "__main__":
    unittest.main()
