from __future__ import annotations

import json
from typing import Any, Protocol

from .contract import Classification, Task, ValidationError, build_complete_payload
from .http_client import ApiFailure
from .state import AlreadyRunning, StateStore, UncertainCompletion


class AgentApi(Protocol):
    def claim(self) -> Task | None: ...

    def complete(self, payload: dict[str, Any]) -> None: ...


class Classifier(Protocol):
    def classify(self, task: Task) -> Classification: ...


def emit_event(event: str, **fields: str | int | bool) -> None:
    # Callers pass fixed enums/counters only. Never pass IDs, content or exceptions.
    print(json.dumps({"event": event, **fields}, separators=(",", ":"), sort_keys=True))


class Worker:
    def __init__(
        self, *, state: StateStore, api: AgentApi, classifier: Classifier
    ) -> None:
        self.state = state
        self.api = api
        self.classifier = classifier

    def _complete(self, payload: dict[str, Any]) -> int:
        try:
            inflight = self.state.mark_inflight()
            self.api.complete(inflight)
        except ApiFailure as exc:
            try:
                self.state.mark_uncertain(exc.code)
            except Exception:
                emit_event("state_failure")
                return 1
            emit_event("completion_uncertain", circuitOpen=True, reason=exc.code)
            return 1
        except Exception:
            try:
                self.state.mark_uncertain("complete_internal_ambiguous")
            except Exception:
                emit_event("state_failure")
                return 1
            emit_event(
                "completion_uncertain",
                circuitOpen=True,
                reason="complete_internal_ambiguous",
            )
            return 1
        try:
            self.state.mark_success()
        except Exception:
            # Remote completion succeeded but local cleanup did not. Keeping the
            # inflight file makes the next run stop for operator reconciliation.
            emit_event("local_commit_uncertain", circuitOpen=True)
            return 1
        emit_event("completed")
        return 0

    def run(self) -> int:
        try:
            with self.state.lock():
                try:
                    prepared = self.state.resume_prepared()
                except UncertainCompletion:
                    emit_event("circuit_open", reason="uncertain_completion")
                    return 1
                if prepared is not None:
                    emit_event("resuming_prepared")
                    return self._complete(prepared)

                try:
                    task = self.api.claim()
                except ApiFailure as exc:
                    emit_event("claim_failed", reason=exc.code)
                    return 1
                if task is None:
                    emit_event("idle")
                    return 0

                try:
                    classification = self.classifier.classify(task)
                except ValidationError:
                    emit_event("classification_failed")
                    return 1
                except Exception:
                    emit_event("classification_internal_failure")
                    return 1

                try:
                    payload = build_complete_payload(task, classification)
                    self.state.prepare(payload)
                except Exception:
                    emit_event("spool_prepare_failed")
                    return 1
                return self._complete(payload)
        except AlreadyRunning:
            emit_event("already_running")
            return 0
        except Exception:
            emit_event("worker_internal_failure")
            return 1
