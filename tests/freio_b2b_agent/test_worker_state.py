from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.freio_b2b_agent.contract import (
    build_complete_payload,
    parse_claim_response,
    parse_classification,
)
from scripts.freio_b2b_agent.http_client import ApiFailure
from scripts.freio_b2b_agent.runner import Worker
from scripts.freio_b2b_agent.state import StateStore

from test_contract import classification, task_response


class FakeApi:
    def __init__(self, *, task=None, complete_effect=None, state=None) -> None:
        self.task = task
        self.complete_effect = complete_effect
        self.state = state
        self.claim_calls = 0
        self.complete_calls: list[dict[str, object]] = []
        self.observed_phase: str | None = None

    def claim(self):
        self.claim_calls += 1
        return self.task

    def complete(self, payload):
        self.complete_calls.append(payload)
        if self.state is not None:
            raw = json.loads(self.state.completion_path.read_text())
            self.observed_phase = raw["phase"]
        if isinstance(self.complete_effect, BaseException):
            raise self.complete_effect


class FakeClassifier:
    def __init__(self, result=None, effect=None) -> None:
        self.result = result or parse_classification(classification())
        self.effect = effect
        self.calls = 0

    def classify(self, task):
        self.calls += 1
        if isinstance(self.effect, BaseException):
            raise self.effect
        return self.result


class WorkerStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self.temporary.name) / "state")
        self.task = parse_claim_response(task_response())
        assert self.task is not None

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_worker(self, api: FakeApi, classifier: FakeClassifier) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = Worker(state=self.state, api=api, classifier=classifier).run()
        return result, output.getvalue()

    def test_idle_does_not_classify_or_complete(self) -> None:
        api = FakeApi(task=None)
        classifier = FakeClassifier()
        result, output = self.run_worker(api, classifier)
        self.assertEqual(result, 0)
        self.assertEqual(api.claim_calls, 1)
        self.assertEqual(classifier.calls, 0)
        self.assertEqual(api.complete_calls, [])
        self.assertIn('"event":"idle"', output)

    def test_completion_is_durably_inflight_before_api_call(self) -> None:
        api = FakeApi(task=self.task, state=self.state)
        classifier = FakeClassifier()
        result, output = self.run_worker(api, classifier)
        self.assertEqual(result, 0)
        self.assertEqual(api.observed_phase, "inflight")
        self.assertEqual(len(api.complete_calls), 1)
        self.assertFalse(self.state.completion_path.exists())
        self.assertFalse(self.state.uncertain_path.exists())
        success = json.loads(self.state.last_success_path.read_text())
        self.assertEqual(set(success), {"version", "completedAt", "completionSha256"})
        self.assertNotIn(self.task.id, self.state.last_success_path.read_text())
        self.assertNotIn("Škola", self.state.last_success_path.read_text())
        self.assertIn('"event":"completed"', output)

    def test_complete_timeout_scrubs_payload_and_opens_circuit(self) -> None:
        api = FakeApi(
            task=self.task,
            state=self.state,
            complete_effect=ApiFailure("request_timeout_ambiguous"),
        )
        result, output = self.run_worker(api, FakeClassifier())
        self.assertEqual(result, 1)
        self.assertFalse(self.state.completion_path.exists())
        uncertain_text = self.state.uncertain_path.read_text()
        uncertain = json.loads(uncertain_text)
        self.assertEqual(uncertain["reason"], "request_timeout_ambiguous")
        self.assertNotIn("classification", uncertain)
        self.assertNotIn("summary", uncertain_text)
        self.assertIn('"circuitOpen":true', output)

        second_api = FakeApi(task=self.task)
        second_classifier = FakeClassifier()
        second_result, second_output = self.run_worker(second_api, second_classifier)
        self.assertEqual(second_result, 1)
        self.assertEqual(second_api.claim_calls, 0)
        self.assertEqual(second_classifier.calls, 0)
        self.assertIn('"event":"circuit_open"', second_output)

    def test_prepared_state_resumes_without_claim_or_model(self) -> None:
        self.state.ensure()
        payload = build_complete_payload(
            self.task,
            parse_classification(classification()),
        )
        self.state.prepare(payload)
        api = FakeApi(task=self.task, state=self.state)
        classifier = FakeClassifier()
        result, output = self.run_worker(api, classifier)
        self.assertEqual(result, 0)
        self.assertEqual(api.claim_calls, 0)
        self.assertEqual(classifier.calls, 0)
        self.assertEqual(api.complete_calls, [payload])
        self.assertIn('"event":"resuming_prepared"', output)

    def test_interrupted_inflight_becomes_uncertain_without_network(self) -> None:
        self.state.ensure()
        self.state.prepare(
            build_complete_payload(self.task, parse_classification(classification()))
        )
        self.state.mark_inflight()
        api = FakeApi(task=self.task)
        classifier = FakeClassifier()
        result, _ = self.run_worker(api, classifier)
        self.assertEqual(result, 1)
        self.assertEqual(api.claim_calls, 0)
        self.assertEqual(api.complete_calls, [])
        self.assertEqual(classifier.calls, 0)
        uncertain = json.loads(self.state.uncertain_path.read_text())
        self.assertEqual(uncertain["reason"], "worker_restart_inflight")

    def test_operator_reconciliation_only_clears_local_circuit(self) -> None:
        api = FakeApi(
            task=self.task,
            state=self.state,
            complete_effect=ApiFailure("complete_http_503"),
        )
        self.run_worker(api, FakeClassifier())
        with self.state.lock():
            self.state.reconcile("not_completed")
        self.assertFalse(self.state.uncertain_path.exists())
        reconciliation = json.loads(self.state.last_reconciliation_path.read_text())
        self.assertEqual(reconciliation["outcome"], "not_completed")
        self.assertNotIn("classification", reconciliation)


if __name__ == "__main__":
    unittest.main()
