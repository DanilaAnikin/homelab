from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.freio_b2b_agent.contract import ValidationError, parse_classification
from scripts.freio_b2b_agent.credentials import load_private_credential
from scripts.freio_b2b_agent.http_client import (
    CLAIM_URL,
    COMPLETE_URL,
    ApiFailure,
    FreioAgentClient,
    HttpResponse,
)

from test_contract import CLAIM_ID, TASK_ID, classification, task_response


def response(status: int, value: object) -> HttpResponse:
    return HttpResponse(
        status=status,
        headers={"content-type": "application/json; charset=utf-8"},
        body=json.dumps(value, separators=(",", ":")).encode(),
    )


class RecordingTransport:
    def __init__(self, effects: list[HttpResponse]) -> None:
        self.effects = list(effects)
        self.calls: list[tuple[str, str, dict[str, str], bytes, int]] = []

    def request(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body, timeout))
        return self.effects.pop(0)


class HttpAndCredentialTests(unittest.TestCase):
    def test_api_uses_exact_fixed_requests_and_bearer(self) -> None:
        transport = RecordingTransport(
            [
                HttpResponse(
                    200,
                    {"content-type": "application/json"},
                    task_response(),
                ),
                response(200, {"success": True}),
            ]
        )
        client = FreioAgentClient(
            bearer_secret="s" * 64,
            transport=transport,
        )
        task = client.claim()
        assert task is not None
        completion = {
            "taskId": task.id,
            "claimId": task.claim_id,
            "classification": parse_classification(classification()).api_payload(),
        }
        client.complete(completion)

        claim_call, complete_call = transport.calls
        self.assertEqual(claim_call[0:2], ("POST", CLAIM_URL))
        self.assertEqual(claim_call[3], b"{}")
        self.assertEqual(claim_call[2]["Authorization"], "Bearer " + "s" * 64)
        self.assertEqual(complete_call[0:2], ("POST", COMPLETE_URL))
        complete_body = json.loads(complete_call[3])
        self.assertEqual(set(complete_body), {"taskId", "claimId", "classification"})
        self.assertNotIn("workerId", complete_body)
        self.assertNotIn("generatedReply", complete_body["classification"])

    def test_api_rejects_redirect_and_nonexact_success(self) -> None:
        redirect = RecordingTransport([response(302, {"task": None})])
        with self.assertRaises(ApiFailure):
            FreioAgentClient(bearer_secret="s" * 64, transport=redirect).claim()

        extra = RecordingTransport([response(200, {"success": True, "id": TASK_ID})])
        client = FreioAgentClient(bearer_secret="s" * 64, transport=extra)
        with self.assertRaises(ApiFailure):
            client.complete(
                {
                    "taskId": TASK_ID,
                    "claimId": CLAIM_ID,
                    "classification": classification(),
                }
            )

    def test_credentials_must_be_private_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "secret"
            path.write_text("x" * 64)
            path.chmod(0o600)
            self.assertEqual(
                load_private_credential(path, label="test"),
                "x" * 64,
            )
            path.chmod(0o644)
            with self.assertRaises(ValidationError):
                load_private_credential(path, label="test")


if __name__ == "__main__":
    unittest.main()
