#!/usr/bin/env python3
# ruff: noqa: E402
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


SCRIPT_ROOT = Path(__file__).resolve().parent
LEGACY_ROOT = SCRIPT_ROOT.parent / "freio-prospecting"
sys.path.insert(0, str(SCRIPT_ROOT))
sys.path.insert(0, str(LEGACY_ROOT))

from freio_b2b_discovery.signing import (  # noqa: E402
    load_private_secret,
    normalize_endpoint,
)
from freio_b2b_discovery.state import MANIFEST, Spool  # noqa: E402
from freio_b2b_discovery.submission import SubmissionWorker  # noqa: E402
from freio_prospecting.common import ValidationError  # noqa: E402


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate or HMAC-submit inert Freio B2B discovery prospects."
    )
    parser.add_argument("--spool", type=Path, required=True)
    parser.add_argument("--endpoint")
    parser.add_argument("--max-batches", type=int, default=5)
    parser.add_argument("--transport-secret-file", type=Path)
    parser.add_argument("--identity-secret-file", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--validate-only", action="store_true")
    mode.add_argument("--send", action="store_true")
    mode.add_argument("--housekeeping", action="store_true")
    mode.add_argument("--clear-global-error", action="store_true")
    mode.add_argument("--reconcile-accepted", metavar="RECEIPT_ID")
    mode.add_argument("--reconcile-not-accepted", metavar="RECEIPT_ID")
    return parser.parse_args(argv)


def _validate_only(spool: Spool) -> dict[str, int]:
    spool.ensure()
    valid = invalid = 0
    for root in (spool.ready, spool.processing):
        for path in sorted(root.glob("[0-9a-f]*.json")):
            if MANIFEST.fullmatch(path.name) is None:
                continue
            try:
                spool._load_manifest(path)
                request = spool.request_path(path)
                if request.exists():
                    spool._load_request(request)
            except ValidationError:
                invalid += 1
            else:
                valid += 1
    return {"valid": valid, "invalid": invalid}


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    arguments = parse_arguments(argv)
    spool = Spool(arguments.spool)
    try:
        if arguments.validate_only:
            if arguments.endpoint is None:
                raise ValidationError("--validate-only requires --endpoint")
            normalize_endpoint(arguments.endpoint)
            result = _validate_only(spool)
            status = 1 if result["invalid"] else 0
        elif arguments.send:
            if (
                arguments.endpoint is None
                or arguments.transport_secret_file is None
                or arguments.identity_secret_file is None
            ):
                raise ValidationError(
                    "--send requires endpoint and both dedicated HMAC credentials"
                )
            transport = load_private_secret(
                arguments.transport_secret_file, "transport"
            )
            identity = load_private_secret(arguments.identity_secret_file, "identity")
            result = SubmissionWorker(
                spool=spool,
                endpoint=arguments.endpoint,
                transport_secret=transport,
                identity_secret=identity,
            ).run(arguments.max_batches)
            status = 0
        else:
            if arguments.identity_secret_file is None:
                raise ValidationError(
                    "offline submit maintenance requires --identity-secret-file"
                )
            identity = load_private_secret(arguments.identity_secret_file, "identity")
            spool.bind_identity_key(identity)
            with spool.submit_lock():
                if arguments.housekeeping:
                    result = spool.purge("submit")
                elif arguments.clear_global_error:
                    result = {"clearedGlobalCircuit": int(spool.clear_circuit())}
                else:
                    receipt_id = (
                        arguments.reconcile_accepted
                        if arguments.reconcile_accepted is not None
                        else arguments.reconcile_not_accepted
                    )
                    result = spool.reconcile_uncertain(
                        receipt_id,
                        accepted=arguments.reconcile_accepted is not None,
                        identity_secret=identity,
                    )
            status = 0
    except ValidationError as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    print(json.dumps({"ok": True, **result}, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
