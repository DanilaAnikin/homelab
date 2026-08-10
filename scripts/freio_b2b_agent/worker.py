#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_ROOT.parent))

from freio_b2b_agent.credentials import load_private_credential  # noqa: E402
from freio_b2b_agent.http_client import FreioAgentClient  # noqa: E402
from freio_b2b_agent.local_classifier import LocalRuleClassifier  # noqa: E402
from freio_b2b_agent.runner import Worker, emit_event  # noqa: E402
from freio_b2b_agent.state import StateStore  # noqa: E402


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Classify one claimed Freio B2B conversation task.",
    )
    parser.add_argument("--spool", type=Path, required=True)
    parser.add_argument("--api-secret-file", type=Path)
    reconciliation = parser.add_mutually_exclusive_group()
    reconciliation.add_argument("--reconcile-completed", action="store_true")
    reconciliation.add_argument("--reconcile-not-completed", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = parse_arguments(argv)
    state = StateStore(args.spool)

    if args.reconcile_completed or args.reconcile_not_completed:
        try:
            with state.lock():
                state.reconcile(
                    "completed" if args.reconcile_completed else "not_completed"
                )
            emit_event("reconciled")
            return 0
        except Exception:
            emit_event("reconciliation_failed")
            return 1

    if args.api_secret_file is None:
        emit_event("configuration_failure")
        return 78

    try:
        bearer_secret = load_private_credential(
            args.api_secret_file,
            label="Freio B2B API bearer",
            maximum=512,
        )
        classifier = LocalRuleClassifier()
        api = FreioAgentClient(bearer_secret=bearer_secret)
    except Exception:
        emit_event("configuration_failure")
        return 78

    return Worker(state=state, api=api, classifier=classifier).run()


if __name__ == "__main__":
    raise SystemExit(main())
