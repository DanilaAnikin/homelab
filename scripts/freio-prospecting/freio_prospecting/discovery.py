from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .common import (
    MAX_JSON_BYTES,
    ValidationError,
    canonical_json_bytes,
    sha256_hex,
    utc_now_iso,
)
from .fetcher import FetchError, FetchedDocument, PublicHTTPSFetcher
from .schema import (
    ResearchCandidate,
    build_research_document,
    parse_research_document,
)
from .signing import load_secret
from .workflow import Spool


CLAUDE_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class DiscoveryResult:
    queued_path: Path | None
    accepted: int
    rejected: int
    input_sha256: str


@dataclass(frozen=True)
class ProcessOutput:
    returncode: int
    stdout: bytes


def _run_bounded_command(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
) -> ProcessOutput:
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=environment,
        )
    except OSError as exc:
        raise ValidationError("Claude discovery process could not start") from exc
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    overflow = threading.Event()

    def drain(stream: object, destination: bytearray, maximum: int) -> None:
        if not hasattr(stream, "read"):
            return
        while True:
            chunk = stream.read(8192)  # type: ignore[attr-defined]
            if not chunk:
                return
            if len(destination) + len(chunk) > maximum:
                overflow.set()
                try:
                    process.kill()
                except OSError:
                    pass
                return
            destination.extend(chunk)

    readers = [
        threading.Thread(
            target=drain,
            args=(process.stdout, stdout_buffer, MAX_JSON_BYTES),
            name="freio-claude-stdout",
            daemon=True,
        ),
        threading.Thread(
            target=drain,
            args=(process.stderr, stderr_buffer, 32 * 1024),
            name="freio-claude-stderr",
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        raise ValidationError("Claude discovery process exceeded its deadline") from exc
    finally:
        for reader in readers:
            reader.join(timeout=2)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
    if overflow.is_set():
        raise ValidationError("Claude discovery output exceeded its bounded pipes")
    return ProcessOutput(returncode, bytes(stdout_buffer))


def run_claude_discovery(
    *,
    claude_binary: Path,
    prompt_path: Path,
    schema_path: Path,
    claude_token_path: Path,
    state_home: Path,
    timeout_seconds: int = CLAUDE_TIMEOUT_SECONDS,
) -> bytes:
    try:
        resolved_claude = claude_binary.resolve(strict=True)
    except OSError as exc:
        raise ValidationError("Claude binary could not be resolved") from exc
    if (
        not resolved_claude.is_file()
        or not os.access(resolved_claude, os.X_OK)
        or resolved_claude.stat().st_mode & 0o022
    ):
        raise ValidationError(
            "Claude binary must resolve to an executable not writable by group/others"
        )
    for path, label in ((prompt_path, "prompt"), (schema_path, "schema")):
        if path.is_symlink() or not path.is_file():
            raise ValidationError(f"{label} must be a regular non-symlink file")
    prompt = prompt_path.read_text(encoding="utf-8")
    schema = schema_path.read_text(encoding="utf-8")
    if (
        len(prompt.encode("utf-8")) > 32 * 1024
        or len(schema.encode("utf-8")) > 32 * 1024
    ):
        raise ValidationError("Claude prompt or schema is unexpectedly large")
    try:
        token = load_secret(claude_token_path).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("Claude credential must be UTF-8 text") from exc
    state_home.mkdir(parents=True, exist_ok=True)
    combined_prompt = (
        f"{prompt}\n\nThe exact JSON Schema is below. Return one JSON object and nothing else.\n"
        f"<json-schema>\n{schema}\n</json-schema>\n"
    )
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(state_home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
        "CLAUDE_CODE_OAUTH_TOKEN": token,
    }
    command = [
        str(resolved_claude),
        "--print",
        "--output-format",
        "text",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--tools",
        "WebSearch,WebFetch",
        combined_prompt,
    ]
    completed = _run_bounded_command(
        command,
        cwd=state_home,
        environment=environment,
        timeout_seconds=timeout_seconds,
    )
    if completed.returncode != 0:
        raise ValidationError(
            f"Claude discovery exited with status {completed.returncode}"
        )
    if not completed.stdout or len(completed.stdout) > MAX_JSON_BYTES:
        raise ValidationError("Claude discovery output is empty or exceeds 96 KiB")
    return completed.stdout


def parse_untrusted_json(raw: bytes) -> object:
    if not raw or len(raw) > MAX_JSON_BYTES:
        raise ValidationError("research output is empty or exceeds 96 KiB")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("research output is not strict UTF-8 JSON") from exc


class DiscoveryEngine:
    def __init__(
        self,
        *,
        spool: Spool,
        fetcher: PublicHTTPSFetcher | None = None,
        now_iso: Callable[[], str] = utc_now_iso,
    ) -> None:
        self.spool = spool
        self.fetcher = fetcher or PublicHTTPSFetcher()
        self.now_iso = now_iso

    def process(self, raw: bytes) -> DiscoveryResult:
        input_hash = sha256_hex(raw)
        try:
            research = parse_untrusted_json(raw)
            candidates = parse_research_document(research)
        except ValidationError as exc:
            self.spool.quarantine_untrusted(raw, "invalid_research_schema", str(exc))
            raise
        accepted_candidates: list[ResearchCandidate] = []
        rejected: list[dict[str, str]] = []
        identities: set[tuple[str, str]] = set()
        for index, candidate in enumerate(candidates):
            try:
                document = self.fetcher.fetch(candidate.source_url)
                self._preview_candidate(candidate, document)
                identity = (
                    candidate.social_channel or "source",
                    (
                        candidate.handle.lstrip("@").lower()
                        if candidate.handle
                        else document.final_url
                    ),
                )
                if identity in identities:
                    raise ValidationError("duplicate normalized prospect identity")
                identities.add(identity)
                accepted_candidates.append(candidate)
            except (FetchError, ValidationError) as exc:
                rejected.append(
                    {
                        "index": str(index),
                        "sourceKey": candidate.source_key,
                        "code": getattr(exc, "code", "candidate_rejected"),
                        "detail": str(exc)[:300],
                    }
                )
        queued_path: Path | None = None
        if accepted_candidates:
            # This remains an untrusted candidate manifest. The submit identity
            # independently refetches every source and creates the only trusted
            # evidence receipt immediately before signing.
            manifest = build_research_document(accepted_candidates)
            queued_path = self.spool.enqueue_research(manifest)
        if rejected:
            rejection_report = canonical_json_bytes(
                {
                    "schemaVersion": "1",
                    "inputSha256": input_hash,
                    "rejectedAt": self.now_iso(),
                    "rejections": rejected,
                }
            )
            self.spool.quarantine_untrusted(
                rejection_report,
                "candidate_rejections",
                "one or more candidates failed closed",
            )
        if not accepted_candidates:
            raise ValidationError(
                "no candidate survived deterministic evidence validation"
            )
        return DiscoveryResult(
            queued_path, len(accepted_candidates), len(rejected), input_hash
        )

    @staticmethod
    def _preview_candidate(
        candidate: ResearchCandidate,
        document: FetchedDocument,
    ) -> None:
        if candidate.claimed_email:
            matched = next(
                (
                    entry
                    for entry in document.emails
                    if entry.email == candidate.claimed_email
                ),
                None,
            )
            if matched is None:
                raise ValidationError(
                    "claimed email is not exactly present in visible text or mailto evidence"
                )
