from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import threading
import time
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
from .fetcher import (
    FETCH_TIMEOUT_SECONDS,
    FetchError,
    FetchedDocument,
    PublicHTTPSFetcher,
)
from .schema import (
    ResearchCandidate,
    build_research_document,
    parse_research_document,
)
from .workflow import Spool


CLAUDE_TIMEOUT_SECONDS = 240
DISCOVERY_OVERALL_BUDGET_SECONDS = 540
MINIMUM_FETCH_BUDGET_SECONDS = FETCH_TIMEOUT_SECONDS + 1
TRANSIENT_FETCH_CODES = frozenset(
    {"dns_failure", "dns_empty", "network_failure", "timeout"}
)


def _load_claude_credential(path: Path) -> str:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValidationError("cannot inspect Claude credential") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & 0o077
    ):
        raise ValidationError("Claude credential must be a private regular file")
    if not 32 <= metadata.st_size <= 4096:
        raise ValidationError("Claude credential has an unsafe length")
    try:
        raw = path.read_bytes()
        token = raw[:-1] if raw.endswith(b"\n") else raw
        value = token.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValidationError("Claude credential must be UTF-8 text") from exc
    if not 32 <= len(token) <= 4096 or b"\x00" in token or value != value.strip():
        raise ValidationError("Claude credential has an unsafe value")
    return value


@dataclass(frozen=True)
class DiscoveryResult:
    queued_paths: tuple[Path, ...]
    accepted: int
    rejected: int
    deferred: int
    input_sha256: str

    @property
    def queued_path(self) -> Path | None:
        return self.queued_paths[0] if self.queued_paths else None


@dataclass(frozen=True)
class DeferredRetryResult:
    accepted: int
    rejected: int
    deferred: int


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
    token = _load_claude_credential(claude_token_path)
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
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.spool = spool
        self.fetcher = fetcher or PublicHTTPSFetcher()
        self.now_iso = now_iso
        self.monotonic = monotonic

    def process(
        self, raw: bytes, *, deadline_monotonic: float | None = None
    ) -> DiscoveryResult:
        input_hash = sha256_hex(raw)
        try:
            research = parse_untrusted_json(raw)
            candidates = parse_research_document(research)
        except ValidationError as exc:
            self.spool.quarantine_untrusted(raw, "invalid_research_schema", str(exc))
            raise
        queued_paths: list[Path] = []
        rejected = 0
        deferred = 0
        identities: set[tuple[str, str]] = set()
        for index, candidate in enumerate(candidates):
            candidate_document = build_research_document([candidate])
            try:
                identity_set = self._preview_identities(candidate, candidate.source_url)
                if identities.intersection(identity_set):
                    raise ValidationError("duplicate normalized prospect identity")
                identities.update(identity_set)
                if (
                    deadline_monotonic is not None
                    and deadline_monotonic - self.monotonic()
                    < MINIMUM_FETCH_BUDGET_SECONDS
                ):
                    if self.spool.defer_untrusted(candidate_document, "overall_budget"):
                        deferred += 1
                    else:
                        rejected += 1
                    continue
                document = self.fetcher.fetch(candidate.source_url)
                self._preview_candidate(candidate, document)
                queued_paths.append(self.spool.enqueue_research(candidate_document))
            except FetchError as exc:
                if self._is_transient_fetch(exc):
                    if self.spool.defer_untrusted(candidate_document, exc.code):
                        deferred += 1
                    else:
                        rejected += 1
                else:
                    self._quarantine_candidate(
                        candidate_document, input_hash, index, exc
                    )
                    rejected += 1
            except ValidationError as exc:
                self._quarantine_candidate(candidate_document, input_hash, index, exc)
                rejected += 1
        if not queued_paths and deferred == 0:
            raise ValidationError(
                "no candidate survived deterministic evidence validation"
            )
        return DiscoveryResult(
            tuple(queued_paths), len(queued_paths), rejected, deferred, input_hash
        )

    def retry_deferred(
        self,
        *,
        deadline_monotonic: float | None = None,
        maximum_candidates: int = 30,
    ) -> DeferredRetryResult:
        if not 1 <= maximum_candidates <= 100:
            raise ValidationError("maximum deferred candidates must be 1 to 100")
        accepted = rejected = deferred = 0
        for path, document, candidates in self.spool.iter_due_untrusted():
            if accepted + rejected + deferred >= maximum_candidates:
                break
            if (
                deadline_monotonic is not None
                and deadline_monotonic - self.monotonic() < MINIMUM_FETCH_BUDGET_SECONDS
            ):
                break
            candidate = candidates[0]
            try:
                fetched = self.fetcher.fetch(candidate.source_url)
                self._preview_candidate(candidate, fetched)
                self.spool.enqueue_research(document)
                self.spool.release_untrusted(path)
                accepted += 1
            except FetchError as exc:
                if self._is_transient_fetch(exc):
                    if self.spool.retry_untrusted(path, exc.code):
                        deferred += 1
                    else:
                        rejected += 1
                else:
                    self.spool._quarantine_deferred_untrusted(
                        path, "candidate_rejected", exc.code
                    )
                    rejected += 1
            except ValidationError as exc:
                self.spool._quarantine_deferred_untrusted(
                    path, "candidate_rejected", str(exc)[:300]
                )
                rejected += 1
        return DeferredRetryResult(accepted, rejected, deferred)

    def _quarantine_candidate(
        self,
        document: dict[str, object],
        input_hash: str,
        index: int,
        error: Exception,
    ) -> None:
        raw = canonical_json_bytes(document)
        code = getattr(error, "code", "candidate_rejected")
        # Each artifact contains only one candidate, so a permanent evidence
        # failure can never quarantine or block a valid neighbour.
        self.spool.quarantine_untrusted(
            raw,
            code,
            f"candidate {index}; input_sha256={input_hash}; {str(error)[:200]}",
        )

    @staticmethod
    def _is_transient_fetch(error: FetchError) -> bool:
        if error.code in TRANSIENT_FETCH_CODES:
            return True
        if error.code != "http_status":
            return False
        match = re.search(r"HTTP ([0-9]{3})", str(error))
        if not match:
            return False
        status = int(match.group(1))
        return status in {408, 425, 429} or status >= 500

    @staticmethod
    def _preview_identities(
        candidate: ResearchCandidate, final_url: str
    ) -> set[tuple[str, str]]:
        identities: set[tuple[str, str]] = set()
        if candidate.handle and candidate.social_channel:
            identities.add(
                (
                    "social",
                    f"{candidate.social_channel}:{candidate.handle.lstrip('@').lower()}",
                )
            )
        if candidate.claimed_email:
            identities.add(("email", candidate.claimed_email))
        if not identities:
            identities.add(("source", final_url))
        return identities

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
