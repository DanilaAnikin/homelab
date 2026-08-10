from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import NOW, fetched_document, research_candidate, research_document

from freio_prospecting.common import ValidationError, canonical_json_bytes
from freio_prospecting.discovery import (
    DiscoveryEngine,
    ProcessOutput,
    _run_bounded_command,
    run_claude_discovery,
)
from freio_prospecting.workflow import Spool


class FakeFetcher:
    def __init__(self, documents=None, failures=None):
        self.documents = documents or {}
        self.failures = failures or {}
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        if url in self.failures:
            raise self.failures[url]
        return self.documents.get(
            url, fetched_document(requested_url=url, final_url=url)
        )


class DiscoveryEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.spool = Spool(Path(self.temporary.name) / "spool")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def process(self, document, fetcher=None):
        raw = canonical_json_bytes(document)
        return DiscoveryEngine(
            spool=self.spool,
            fetcher=fetcher or FakeFetcher(),
            now_iso=lambda: NOW,
        ).process(raw)

    def test_exact_email_is_queued_only_as_untrusted_candidate_manifest(self) -> None:
        result = self.process(research_document())
        self.assertEqual((result.accepted, result.rejected), (1, 0))
        manifest = json.loads(result.queued_path.read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["candidates"][0]["claimedEmail"], "hello@creator.example.cz"
        )
        self.assertNotIn("items", manifest)
        self.assertNotIn("evidence", manifest)
        self.assertNotIn("receipt", manifest)

    def test_candidate_without_claimed_email_remains_review_only(self) -> None:
        candidate = research_candidate()
        candidate.pop("claimedEmail")
        candidate["researchContext"]["riskFlags"] = ["contact_unconfirmed"]
        result = self.process(
            research_document(candidate),
            FakeFetcher(
                documents={candidate["sourceUrl"]: fetched_document(email=None)}
            ),
        )
        manifest = json.loads(result.queued_path.read_text(encoding="utf-8"))
        self.assertNotIn("claimedEmail", manifest["candidates"][0])

    def test_claimed_email_must_be_exactly_present(self) -> None:
        fetcher = FakeFetcher(
            documents={
                "https://creator.example.cz/contact": fetched_document(
                    email="someone-else@creator.example.cz"
                )
            }
        )
        with self.assertRaisesRegex(ValidationError, "no candidate survived"):
            self.process(research_document(), fetcher)
        self.assertFalse(list(self.spool.ready.glob("*.json")))
        self.assertTrue(
            list(self.spool.untrusted_quarantine.glob("untrusted-*.error.json"))
        )

    def test_partial_failure_queues_only_valid_candidate_and_quarantines_report(
        self,
    ) -> None:
        second = research_candidate(
            name="Druhý tvůrce",
            handle="@druhy",
            sourceUrl="https://second.example.cz/contact",
            claimedEmail="absent@second.example.cz",
        )
        fetcher = FakeFetcher(
            documents={
                "https://second.example.cz/contact": fetched_document(
                    requested_url="https://second.example.cz/contact",
                    final_url="https://second.example.cz/contact",
                    email="other@second.example.cz",
                )
            }
        )
        result = self.process(research_document(research_candidate(), second), fetcher)
        self.assertEqual((result.accepted, result.rejected), (1, 1))
        manifest = json.loads(result.queued_path.read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["candidates"]), 1)
        self.assertTrue(list(self.spool.untrusted_quarantine.glob("*.error.json")))

    def test_duplicate_identity_is_rejected(self) -> None:
        duplicate = research_candidate(name="Jiný název")
        result = self.process(research_document(research_candidate(), duplicate))
        self.assertEqual((result.accepted, result.rejected), (1, 1))

    def test_invalid_top_level_output_is_preserved_in_quarantine(self) -> None:
        raw = b'{"schemaVersion":"1","candidates":[],"extra":true}'
        with self.assertRaises(ValidationError):
            DiscoveryEngine(
                spool=self.spool, fetcher=FakeFetcher(), now_iso=lambda: NOW
            ).process(raw)
        artifact = next(self.spool.untrusted_quarantine.glob("untrusted-*.json"))
        self.assertEqual(artifact.read_bytes().rstrip(b"\n"), raw)


class ClaudeBoundaryTests(unittest.TestCase):
    def test_claude_process_receives_only_minimal_environment_and_web_tools(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "claude"
            prompt = root / "prompt.txt"
            schema = root / "schema.json"
            token = root / "token"
            home = root / "state"
            binary.write_text("binary", encoding="utf-8")
            prompt.write_text("research", encoding="utf-8")
            schema.write_text("{}", encoding="utf-8")
            token.write_bytes(b"t" * 32)
            for path in (binary, prompt, schema, token):
                path.chmod(0o600)
            binary.chmod(0o700)
            completed = ProcessOutput(
                returncode=0, stdout=b'{"schemaVersion":"1","candidates":[]}'
            )
            with mock.patch(
                "freio_prospecting.discovery._run_bounded_command",
                return_value=completed,
            ) as run:
                output = run_claude_discovery(
                    claude_binary=binary,
                    prompt_path=prompt,
                    schema_path=schema,
                    claude_token_path=token,
                    state_home=home,
                )
            self.assertTrue(output.startswith(b"{"))
            command = run.call_args.args[0]
            environment = run.call_args.kwargs["environment"]
            self.assertEqual(
                command[command.index("--tools") + 1], "WebSearch,WebFetch"
            )
            self.assertNotIn("FREIO_PROSPECTING_HMAC", environment)
            self.assertEqual(
                set(environment),
                {
                    "PATH",
                    "HOME",
                    "LANG",
                    "LC_ALL",
                    "NO_COLOR",
                    "CLAUDE_CODE_OAUTH_TOKEN",
                },
            )
            self.assertEqual(environment["CLAUDE_CODE_OAUTH_TOKEN"], "t" * 32)

    def test_claude_failure_does_not_echo_stderr_or_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {
                name: root / name for name in ("claude", "prompt", "schema", "token")
            }
            for name, path in paths.items():
                path.write_bytes((b"t" * 32) if name == "token" else b"x")
                path.chmod(0o600)
            paths["claude"].chmod(0o700)
            completed = ProcessOutput(returncode=7, stdout=b"")
            with mock.patch(
                "freio_prospecting.discovery._run_bounded_command",
                return_value=completed,
            ):
                with self.assertRaisesRegex(ValidationError, "status 7") as raised:
                    run_claude_discovery(
                        claude_binary=paths["claude"],
                        prompt_path=paths["prompt"],
                        schema_path=paths["schema"],
                        claude_token_path=paths["token"],
                        state_home=root / "home",
                    )
            self.assertNotIn("secret diagnostics", str(raised.exception))

    def test_process_output_pipe_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValidationError, "bounded pipes"):
                _run_bounded_command(
                    [
                        sys.executable,
                        "-c",
                        "import sys; sys.stdout.write('x' * 100000)",
                    ],
                    cwd=Path(directory),
                    environment={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                    timeout_seconds=5,
                )


if __name__ == "__main__":
    unittest.main()
