#!/usr/bin/env python3
# ruff: noqa: E402
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


SCRIPT_ROOT = Path(__file__).resolve().parent
LEGACY_ROOT = SCRIPT_ROOT.parent / "freio-prospecting"
sys.path.insert(0, str(SCRIPT_ROOT))
sys.path.insert(0, str(LEGACY_ROOT))

from freio_b2b_discovery.discovery import (  # noqa: E402
    CLAUDE_TIMEOUT_SECONDS,
    OVERALL_BUDGET_SECONDS,
    DiscoveryEngine,
    run_claude_discovery,
)
from freio_b2b_discovery.state import Spool  # noqa: E402
from freio_prospecting.common import MAX_JSON_BYTES, ValidationError  # noqa: E402


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover intake-only Freio B2B prospects from public sources."
    )
    parser.add_argument("--spool", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--input-json", type=Path)
    mode.add_argument("--claude", action="store_true")
    mode.add_argument("--housekeeping", action="store_true")
    parser.add_argument(
        "--claude-bin", type=Path, default=Path("/usr/local/bin/claude")
    )
    parser.add_argument("--claude-oauth-token-file", type=Path)
    parser.add_argument(
        "--claude-home", type=Path, default=Path("/run/freio-b2b-discovery/claude-home")
    )
    parser.add_argument(
        "--prompt", type=Path, default=SCRIPT_ROOT / "prompts" / "discover-v1.txt"
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=SCRIPT_ROOT / "schemas" / "candidate-research-v1.schema.json",
    )
    return parser.parse_args(argv)


def _read_input(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JSON_BYTES:
        raise ValidationError("input must be a bounded regular non-symlink file")
    raw = path.read_bytes()
    if not raw or len(raw) > MAX_JSON_BYTES:
        raise ValidationError("input is empty or exceeds 96 KiB")
    return raw


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    arguments = parse_arguments(argv)
    spool = Spool(arguments.spool)
    try:
        if arguments.housekeeping:
            result = spool.purge("discovery")
            print(json.dumps({"ok": True, **result}, sort_keys=True))
            return 0
        started = time.monotonic()
        deadline = started + OVERALL_BUDGET_SECONDS
        engine = DiscoveryEngine(spool=spool)
        retried = engine.retry_due(deadline_monotonic=deadline)
        if arguments.input_json is not None:
            raw = _read_input(arguments.input_json)
        else:
            if arguments.claude_oauth_token_file is None:
                raise ValidationError("--claude requires --claude-oauth-token-file")
            remaining = deadline - time.monotonic()
            timeout = min(CLAUDE_TIMEOUT_SECONDS, int(remaining - 30))
            if timeout < 1:
                raise ValidationError("discovery budget was exhausted before Claude")
            raw = run_claude_discovery(
                claude_binary=arguments.claude_bin,
                prompt_path=arguments.prompt,
                schema_path=arguments.schema,
                claude_oauth_token_path=arguments.claude_oauth_token_file,
                state_home=arguments.claude_home,
                timeout_seconds=timeout,
            )
        discovered = engine.process(raw, deadline_monotonic=deadline)
    except ValidationError as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "accepted": discovered.accepted,
                "rejected": discovered.rejected,
                "deferred": discovered.deferred,
                "retriedAccepted": retried.accepted,
                "retriedRejected": retried.rejected,
                "retriedDeferred": retried.deferred,
                "manifestSha256s": list(discovered.manifest_hashes),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
