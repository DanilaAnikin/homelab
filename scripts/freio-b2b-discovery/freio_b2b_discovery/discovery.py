from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .model import (
    Candidate,
    exact_email_match,
    parse_research_bytes,
    validate_legal_entity_evidence,
)
from .state import Spool


LEGACY_ROOT = Path(__file__).resolve().parents[2] / "freio-prospecting"
if str(LEGACY_ROOT) not in sys.path:
    sys.path.insert(0, str(LEGACY_ROOT))

from freio_prospecting.common import MAX_JSON_BYTES, ValidationError  # noqa: E402
from freio_prospecting.fetcher import (  # noqa: E402
    FETCH_TIMEOUT_SECONDS,
    FetchError,
    FetchedDocument,
    PublicHTTPSFetcher,
)


OVERALL_BUDGET_SECONDS = 540.0
MINIMUM_FETCH_BUDGET_SECONDS = (2 * FETCH_TIMEOUT_SECONDS) + 1
CLAUDE_TIMEOUT_SECONDS = 360
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
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
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
        "uniqueItems",
    }
)


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
            chunk = stream.read(8192)
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

    threads = [
        threading.Thread(
            target=drain,
            args=(process.stdout, stdout_buffer, MAX_JSON_BYTES),
            name="freio-b2b-discovery-claude-stdout",
            daemon=True,
        ),
        threading.Thread(
            target=drain,
            args=(process.stderr, stderr_buffer, MAX_STDERR_BYTES),
            name="freio-b2b-discovery-claude-stderr",
            daemon=True,
        ),
        threading.Thread(
            target=feed_stdin,
            name="freio-b2b-discovery-claude-stdin",
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _kill_process_group(process)
        process.wait()
        raise ValidationError("Claude discovery process exceeded its deadline") from exc
    finally:
        for thread in threads:
            thread.join(timeout=2)
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
    if overflow.is_set():
        raise ValidationError("Claude discovery output exceeded a bounded pipe")
    if stdin_failed.is_set():
        raise ValidationError("Claude discovery did not consume its bounded input")
    return ProcessOutput(returncode=returncode, stdout=bytes(stdout_buffer))


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


def _load_claude_oauth_token(path: Path) -> str:
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
    claude_oauth_token_path: Path,
    state_home: Path,
    timeout_seconds: int = CLAUDE_TIMEOUT_SECONDS,
) -> bytes:
    if not 1 <= timeout_seconds <= CLAUDE_TIMEOUT_SECONDS:
        raise ValidationError(
            f"Claude timeout must be between 1 and {CLAUDE_TIMEOUT_SECONDS} seconds"
        )
    try:
        resolved_claude = claude_binary.resolve(strict=True)
    except OSError as exc:
        raise ValidationError("Claude binary could not be resolved") from exc
    metadata = resolved_claude.stat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or not os.access(resolved_claude, os.X_OK)
        or metadata.st_mode & 0o022
    ):
        raise ValidationError(
            "Claude binary must be executable and not group/other writable"
        )

    prompt = _read_bounded_text(prompt_path, "prompt", MAX_PROMPT_BYTES)
    schema = _read_bounded_text(schema_path, "schema", MAX_SCHEMA_BYTES)
    structured_schema = _prepare_structured_schema(schema)
    oauth_token = _load_claude_oauth_token(claude_oauth_token_path)
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
        # Předplatné (Claude Code CLI), NE API klíč — ANTHROPIC_API_KEY by
        # účtoval per-token z kreditu. Do prostředí jde jen tenhle token.
        "CLAUDE_CODE_OAUTH_TOKEN": oauth_token,
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


@dataclass(frozen=True)
class DiscoveryResult:
    accepted: int
    rejected: int
    deferred: int
    manifest_hashes: tuple[str, ...]


def _official_host(value: str) -> str:
    hostname = (urlsplit(value).hostname or "").lower()
    return hostname[4:] if hostname.startswith("www.") else hostname


def _validate_preview(candidate: Candidate, document: FetchedDocument) -> None:
    if _official_host(document.final_url) != _official_host(candidate.website):
        raise ValidationError("preview evidence redirect left the official website")
    exact_email_match(candidate, document)


def _transient(error: FetchError) -> bool:
    if error.code in TRANSIENT_FETCH_CODES:
        return True
    if error.code != "http_status":
        return False
    import re

    matched = re.search(r"HTTP ([0-9]{3})", str(error))
    if not matched:
        return False
    status = int(matched.group(1))
    return status in {408, 425, 429} or status >= 500


class DiscoveryEngine:
    def __init__(
        self,
        *,
        spool: Spool,
        fetcher: PublicHTTPSFetcher | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.spool = spool
        self.fetcher = fetcher or PublicHTTPSFetcher()
        self.monotonic = monotonic

    def _fetch_and_validate(self, candidate: Candidate) -> None:
        contact_document = self.fetcher.fetch(candidate.source_url)
        _validate_preview(candidate, contact_document)
        legal_document = (
            contact_document
            if candidate.legal_entity_source_url == candidate.source_url
            else self.fetcher.fetch(candidate.legal_entity_source_url)
        )
        validate_legal_entity_evidence(candidate, legal_document)

    def retry_due(
        self, *, deadline_monotonic: float, maximum: int = 10
    ) -> DiscoveryResult:
        accepted = rejected = deferred = 0
        hashes: list[str] = []
        for path, candidate in self.spool.due_discovery():
            if accepted + rejected + deferred >= maximum:
                break
            if deadline_monotonic - self.monotonic() < MINIMUM_FETCH_BUDGET_SECONDS:
                break
            try:
                self._fetch_and_validate(candidate)
                queued = self.spool.enqueue(candidate)
                self.spool.release_discovery(path)
                hashes.append(queued.stem)
                accepted += 1
            except FetchError as exc:
                if _transient(exc) and self.spool.record_retry(path, exc.code):
                    deferred += 1
                else:
                    self.spool.release_discovery(path)
                    self.spool.quarantine_discovery_candidate(
                        candidate, "preview_evidence_rejected"
                    )
                    rejected += 1
            except ValidationError:
                self.spool.release_discovery(path)
                self.spool.quarantine_discovery_candidate(
                    candidate, "preview_evidence_rejected"
                )
                rejected += 1
        return DiscoveryResult(accepted, rejected, deferred, tuple(hashes))

    def process(self, raw: bytes, *, deadline_monotonic: float) -> DiscoveryResult:
        try:
            _document, candidates = parse_research_bytes(raw)
        except ValidationError:
            self.spool.quarantine_untrusted(raw, "invalid_research_schema")
            raise
        accepted = rejected = deferred = 0
        hashes: list[str] = []
        for candidate in candidates:
            if deadline_monotonic - self.monotonic() < MINIMUM_FETCH_BUDGET_SECONDS:
                was_deferred = self.spool.defer_discovery(candidate, "overall_budget")
                if was_deferred:
                    deferred += 1
                else:
                    rejected += 1
                continue
            try:
                self._fetch_and_validate(candidate)
                queued = self.spool.enqueue(candidate)
                hashes.append(queued.stem)
                accepted += 1
            except FetchError as exc:
                if _transient(exc) and self.spool.defer_discovery(candidate, exc.code):
                    deferred += 1
                else:
                    self.spool.quarantine_discovery_candidate(
                        candidate, "preview_evidence_rejected"
                    )
                    rejected += 1
            except ValidationError:
                self.spool.quarantine_discovery_candidate(
                    candidate, "preview_evidence_rejected"
                )
                rejected += 1
        if accepted == 0 and deferred == 0:
            raise ValidationError(
                "no B2B candidate survived deterministic evidence validation"
            )
        return DiscoveryResult(accepted, rejected, deferred, tuple(hashes))
