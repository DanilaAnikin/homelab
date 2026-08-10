from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from helpers import fetched_document, research_bytes

from freio_b2b_discovery.discovery import DiscoveryEngine
from freio_b2b_discovery.state import Spool
from freio_prospecting.common import ValidationError
from freio_prospecting.fetcher import FetchError


class FakeFetcher:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[str] = []

    def fetch(self, url: str):  # type: ignore[no-untyped-def]
        self.calls.append(url)
        if isinstance(self.result, dict):
            return self.result[url]
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class DiscoveryTests(unittest.TestCase):
    def test_preview_exact_email_then_enqueues_one_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = Spool(Path(directory) / "spool")
            fetcher = FakeFetcher(fetched_document())
            result = DiscoveryEngine(
                spool=spool, fetcher=fetcher, monotonic=lambda: 0
            ).process(research_bytes(), deadline_monotonic=100)
            self.assertEqual(result.accepted, 1)
            self.assertEqual(result.rejected, 0)
            self.assertEqual(len(tuple(spool.ready.iterdir())), 1)
            self.assertEqual(fetcher.calls, ["https://www.doucovani.cz/kontakt"])

    def test_preview_fetches_separate_official_legal_entity_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = Spool(Path(directory) / "spool")
            legal_url = "https://www.doucovani.cz/o-spolecnosti"
            fetcher = FakeFetcher(
                {
                    "https://www.doucovani.cz/kontakt": fetched_document(),
                    legal_url: fetched_document(final_url=legal_url),
                }
            )
            result = DiscoveryEngine(
                spool=spool, fetcher=fetcher, monotonic=lambda: 0
            ).process(
                research_bytes(legalEntitySourceUrl=legal_url),
                deadline_monotonic=100,
            )
            self.assertEqual(result.accepted, 1)
            self.assertEqual(
                fetcher.calls,
                ["https://www.doucovani.cz/kontakt", legal_url],
            )

    def test_preview_rejects_unverified_legal_entity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = Spool(Path(directory) / "spool")
            fetcher = FakeFetcher(
                fetched_document(legal_name="Pouhá značka bez právní formy")
            )
            with self.assertRaises(ValidationError):
                DiscoveryEngine(
                    spool=spool, fetcher=fetcher, monotonic=lambda: 0
                ).process(research_bytes(), deadline_monotonic=100)
            self.assertEqual(len(tuple(spool.ready.iterdir())), 0)

    def test_preview_rejects_missing_email_without_queueing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = Spool(Path(directory) / "spool")
            fetcher = FakeFetcher(fetched_document(email="other@example.cz"))
            with self.assertRaises(ValidationError):
                DiscoveryEngine(
                    spool=spool, fetcher=fetcher, monotonic=lambda: 0
                ).process(research_bytes(), deadline_monotonic=100)
            self.assertEqual(len(tuple(spool.ready.iterdir())), 0)
            self.assertTrue(any(spool.quarantine_discovery.iterdir()))

    def test_transient_preview_is_durably_deferred_with_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = Spool(Path(directory) / "spool")
            fetcher = FakeFetcher(FetchError("timeout", "bounded timeout"))
            result = DiscoveryEngine(
                spool=spool, fetcher=fetcher, monotonic=lambda: 0
            ).process(research_bytes(), deadline_monotonic=100)
            self.assertEqual(result.deferred, 1)
            manifests = [
                path
                for path in spool.deferred.iterdir()
                if path.name.endswith(".json") and ".retry." not in path.name
            ]
            self.assertEqual(len(manifests), 1)
            self.assertTrue(spool.retry_path(manifests[0]).exists())

    def test_untrusted_unknown_fields_are_quarantined_without_echoing_values(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = Spool(Path(directory) / "spool")
            malicious = research_bytes(executeOutbound="secret@example.cz\nESC")
            with self.assertRaises(ValidationError):
                DiscoveryEngine(spool=spool, monotonic=lambda: 0).process(
                    malicious, deadline_monotonic=100
                )
            markers = tuple(spool.quarantine_discovery.glob("*.error.json"))
            self.assertEqual(len(markers), 1)
            serialized = "\n".join(
                path.read_text(errors="replace")
                for path in spool.quarantine_discovery.iterdir()
            )
            self.assertNotIn("secret@example", serialized)


if __name__ == "__main__":
    unittest.main()
