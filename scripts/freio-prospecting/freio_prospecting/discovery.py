from __future__ import annotations

import json
import os
import re
import signal
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

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
MAX_STDERR_BYTES = 32 * 1024
MAX_PROMPT_BYTES = 32 * 1024
MAX_SCHEMA_BYTES = 32 * 1024
MAX_CLAUDE_STDIN_BYTES = 96 * 1024
TRANSIENT_FETCH_CODES = frozenset(
    {"dns_failure", "dns_empty", "network_failure", "timeout"}
)
STRUCTURED_OUTPUT_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "$id",
        "$schema",
        "allOf",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "if",
        "maxItems",
        "maxLength",
        "maxProperties",
        "maximum",
        "minItems",
        "minLength",
        "minProperties",
        "minimum",
        "multipleOf",
        "pattern",
        "then",
        "uniqueItems",
    }
)


def _load_claude_secret(path: Path) -> str:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValidationError("cannot inspect Claude OAuth token") from exc
    permissions = stat.S_IMODE(metadata.st_mode)
    credential_directory = os.environ.get("CREDENTIALS_DIRECTORY")
    systemd_delivered = (
        permissions == 0o440
        and credential_directory is not None
        and Path(credential_directory).is_absolute()
        and path.parent == Path(credential_directory)
    )
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or (permissions not in (0o400, 0o600) and not systemd_delivered)
        or not 32 <= metadata.st_size <= 4097
    ):
        raise ValidationError("Claude OAuth token must be a private regular file")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError("Claude OAuth token is not readable") from exc
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if not 32 <= len(raw) <= 4096 or any(byte < 33 or byte > 126 for byte in raw):
        raise ValidationError("Claude OAuth token has an unsafe value")
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValidationError("Claude OAuth token must be ASCII") from exc


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


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass


def _run_bounded_command(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
    stdin_data: bytes,
) -> ProcessOutput:
    if not stdin_data or len(stdin_data) > MAX_CLAUDE_STDIN_BYTES:
        raise ValidationError("Claude discovery input is empty or exceeds 96 KiB")
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=environment,
            start_new_session=True,
        )
    except OSError as exc:
        raise ValidationError("Claude discovery process could not start") from exc
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    overflow = threading.Event()
    stdin_failed = threading.Event()

    def drain(stream: object, destination: bytearray, maximum: int) -> None:
        if not hasattr(stream, "read"):
            return
        while True:
            chunk = stream.read(8192)  # type: ignore[attr-defined]
            if not chunk:
                return
            if len(destination) + len(chunk) > maximum:
                overflow.set()
                _kill_process_group(process)
                return
            destination.extend(chunk)

    def feed_stdin() -> None:
        if process.stdin is None:
            return
        offset = 0
        try:
            while offset < len(stdin_data):
                written = process.stdin.write(stdin_data[offset : offset + 8192])
                if written is None or written <= 0:
                    stdin_failed.set()
                    return
                offset += written
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            stdin_failed.set()
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    workers = [
        threading.Thread(
            target=drain,
            args=(process.stdout, stdout_buffer, MAX_JSON_BYTES),
            name="freio-claude-stdout",
            daemon=True,
        ),
        threading.Thread(
            target=drain,
            args=(process.stderr, stderr_buffer, MAX_STDERR_BYTES),
            name="freio-claude-stderr",
            daemon=True,
        ),
        threading.Thread(
            target=feed_stdin,
            name="freio-claude-stdin",
            daemon=True,
        ),
    ]
    for worker in workers:
        worker.start()
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _kill_process_group(process)
        process.wait()
        raise ValidationError("Claude discovery process exceeded its deadline") from exc
    finally:
        for worker in workers:
            worker.join(timeout=2)
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
    if overflow.is_set():
        raise ValidationError("Claude discovery output exceeded its bounded pipes")
    if stdin_failed.is_set():
        raise ValidationError("Claude discovery did not consume its bounded input")
    return ProcessOutput(returncode, bytes(stdout_buffer))


def _read_bounded_text(path: Path, label: str, maximum: int) -> str:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValidationError(f"cannot inspect Claude {label}") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > maximum
    ):
        raise ValidationError(f"Claude {label} must be a bounded regular file")
    try:
        raw = path.read_bytes()
        value = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValidationError(f"Claude {label} is not readable UTF-8") from exc
    if not raw or len(raw) > maximum:
        raise ValidationError(f"Claude {label} changed outside its size bound")
    return value


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"unsupported JSON constant: {value}")


def _transform_structured_schema(value: Any) -> Any:
    if isinstance(value, list):
        return [_transform_structured_schema(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _transform_structured_schema(item)
            for key, item in value.items()
            if key not in STRUCTURED_OUTPUT_UNSUPPORTED_SCHEMA_KEYS
        }
    return value


def _prepare_structured_schema(raw: str) -> str:
    try:
        schema = json.loads(
            raw,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValidationError("Claude output schema is invalid JSON") from exc
    if not isinstance(schema, dict):
        raise ValidationError("Claude output schema must be an object")
    transformed = _transform_structured_schema(schema)
    try:
        return json.dumps(
            transformed,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError("Claude output schema cannot be transformed") from exc


def _extract_structured_output(raw: bytes) -> bytes:
    try:
        envelope = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValidationError("Claude structured-output envelope is invalid") from exc
    if (
        not isinstance(envelope, dict)
        or envelope.get("is_error") is not False
        or not isinstance(envelope.get("structured_output"), dict)
    ):
        raise ValidationError("Claude structured output was not successful")
    try:
        serialized = json.dumps(
            envelope["structured_output"],
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError("Claude structured output cannot be serialized") from exc
    if not serialized or len(serialized) > MAX_JSON_BYTES:
        raise ValidationError("Claude structured output exceeds its size bound")
    return serialized


def run_claude_discovery(
    *,
    claude_binary: Path,
    prompt_path: Path,
    schema_path: Path,
    auth_mode: str,
    claude_secret_path: Path,
    state_home: Path,
    timeout_seconds: int = CLAUDE_TIMEOUT_SECONDS,
) -> bytes:
    if auth_mode != "api-key":
        raise ValidationError("Claude authentication mode must be api-key")
    if not 1 <= timeout_seconds <= CLAUDE_TIMEOUT_SECONDS:
        raise ValidationError(
            f"Claude timeout must be between 1 and {CLAUDE_TIMEOUT_SECONDS} seconds"
        )
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
    prompt = _read_bounded_text(prompt_path, "prompt", MAX_PROMPT_BYTES)
    schema = _read_bounded_text(schema_path, "schema", MAX_SCHEMA_BYTES)
    structured_schema = _prepare_structured_schema(schema)
    secret = _load_claude_secret(claude_secret_path)
    state_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        state_metadata = state_home.lstat()
    except OSError as exc:
        raise ValidationError("Claude state directory is unavailable") from exc
    if (
        state_home.is_symlink()
        or not stat.S_ISDIR(state_metadata.st_mode)
        or state_metadata.st_mode & 0o077
    ):
        raise ValidationError("Claude state directory must be private")
    combined_prompt = (
        f"{prompt}\n\n"
        "The Claude CLI enforces a structural output schema; the local receiver "
        "enforces every hard constraint above. Return the structured object and "
        "no prose.\n"
    ).encode("utf-8")
    if len(combined_prompt) > MAX_CLAUDE_STDIN_BYTES:
        raise ValidationError("Claude discovery input exceeds 96 KiB")
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(state_home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
        # Předplatné (Claude Code CLI). ANTHROPIC_API_KEY by účtoval
        # per-token z kreditu — tahle farma má jet přes subscription.
        "CLAUDE_CODE_OAUTH_TOKEN": secret,
    }
    command = [
        str(resolved_claude),
        "--print",
        "--model",
        "claude-sonnet-5",
        "--max-budget-usd",
        "1.00",
        "--permission-mode",
        "dontAsk",
        "--output-format",
        "json",
        "--input-format",
        "text",
        "--no-session-persistence",
        "--safe-mode",
        "--strict-mcp-config",
        "--tools",
        "WebSearch,WebFetch",
        "--allowedTools",
        "WebSearch,WebFetch",
        "--json-schema",
        structured_schema,
    ]
    completed = _run_bounded_command(
        command,
        cwd=state_home,
        environment=environment,
        timeout_seconds=timeout_seconds,
        stdin_data=combined_prompt,
    )
    if completed.returncode != 0:
        raise ValidationError(
            f"Claude discovery exited with status {completed.returncode}"
        )
    if not completed.stdout or len(completed.stdout) > MAX_JSON_BYTES:
        raise ValidationError("Claude discovery output is empty or exceeds 96 KiB")
    return _extract_structured_output(completed.stdout)


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
