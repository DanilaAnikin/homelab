#!/usr/bin/env python3
# ruff: noqa: E402
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_ROOT))

from freio_prospecting.common import ValidationError  # noqa: E402
from freio_prospecting.signing import (
    load_secret,
    normalize_submit_endpoint,
)  # noqa: E402
from freio_prospecting.submission import SubmissionWorker  # noqa: E402
from freio_prospecting.workflow import Spool  # noqa: E402


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate or submit ready Freio prospect batches.",
    )
    parser.add_argument("--spool", type=Path, required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--max-batches", type=int, default=10)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--validate-only",
        action="store_true",
        help="Inspect ready artifacts without a credential, network call or state transition.",
    )
    mode.add_argument(
        "--send",
        action="store_true",
        help="Explicitly allow HTTPS submission and ready/processed state transitions.",
    )
    parser.add_argument("--secret-file", type=Path)
    return parser.parse_args()


def validate_only(spool: Spool) -> dict[str, int]:
    from freio_prospecting.workflow import BATCH_FILENAME

    spool.ensure()
    counters = {"valid": 0, "invalid": 0}
    for path in sorted(spool.ready.iterdir(), key=lambda item: item.name):
        if (
            path.is_symlink()
            or not path.is_file()
            or BATCH_FILENAME.fullmatch(path.name) is None
        ):
            counters["invalid"] += 1
            continue
        try:
            spool._load_and_verify(path)
        except ValidationError:
            counters["invalid"] += 1
        else:
            counters["valid"] += 1
    return counters


def main() -> int:
    arguments = parse_arguments()
    try:
        endpoint = normalize_submit_endpoint(arguments.endpoint)
        spool = Spool(arguments.spool)
        if arguments.validate_only:
            counters = validate_only(spool)
        else:
            if not arguments.secret_file:
                raise ValidationError("--send requires --secret-file")
            secret = load_secret(arguments.secret_file)
            counters = SubmissionWorker(
                spool=spool,
                endpoint=endpoint,
                secret=secret,
            ).run(arguments.max_batches)
    except ValidationError as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    print(json.dumps({"ok": True, **counters}, sort_keys=True))
    return 1 if arguments.validate_only and counters["invalid"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
