#!/usr/bin/env python3
# ruff: noqa: E402
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_ROOT))

from freio_prospecting.common import MAX_JSON_BYTES, ValidationError  # noqa: E402
from freio_prospecting.discovery import (
    DiscoveryEngine,
    run_claude_discovery,
)  # noqa: E402
from freio_prospecting.workflow import Spool  # noqa: E402


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover and deterministically validate Freio influencer prospects.",
    )
    parser.add_argument("--spool", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input-json", type=Path)
    source.add_argument("--claude", action="store_true")
    parser.add_argument(
        "--claude-bin", type=Path, default=Path("/usr/local/bin/claude")
    )
    parser.add_argument(
        "--claude-token-file",
        type=Path,
        help="A discovery-only Claude credential. Never use the submit HMAC credential here.",
    )
    parser.add_argument(
        "--prompt",
        type=Path,
        default=SCRIPT_ROOT / "prompts" / "discover-v1.txt",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=SCRIPT_ROOT / "schemas" / "candidate-research.schema.json",
    )
    parser.add_argument(
        "--claude-home",
        type=Path,
        default=Path("/var/lib/freio-prospecting/claude-home"),
    )
    return parser.parse_args()


def read_input(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValidationError("input must be a regular non-symlink file")
    if path.stat().st_size > MAX_JSON_BYTES:
        raise ValidationError("input exceeds 96 KiB")
    raw = path.read_bytes()
    if len(raw) > MAX_JSON_BYTES:
        raise ValidationError("input exceeds 96 KiB")
    return raw


def main() -> int:
    arguments = parse_arguments()
    spool = Spool(arguments.spool)
    try:
        if arguments.input_json:
            raw = read_input(arguments.input_json)
        else:
            if not arguments.claude_token_file:
                raise ValidationError("--claude requires --claude-token-file")
            raw = run_claude_discovery(
                claude_binary=arguments.claude_bin,
                prompt_path=arguments.prompt,
                schema_path=arguments.schema,
                claude_token_path=arguments.claude_token_file,
                state_home=arguments.claude_home,
            )
        result = DiscoveryEngine(spool=spool).process(raw)
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
                "accepted": result.accepted,
                "rejected": result.rejected,
                "inputSha256": result.input_sha256,
                "manifestSha256": (
                    result.queued_path.stem if result.queued_path else None
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
