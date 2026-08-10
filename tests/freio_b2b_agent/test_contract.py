from __future__ import annotations

import json
import unittest

from scripts.freio_b2b_agent.contract import (
    FAQ_TOPICS,
    INTENTS,
    RISK_TAGS,
    ValidationError,
    build_complete_payload,
    parse_claim_response,
    parse_classification,
    parse_classification_json,
)


TASK_ID = "00000000-0000-4000-8000-000000000101"
CLAIM_ID = "00000000-0000-4000-8000-000000000102"
CONVERSATION_ID = "00000000-0000-4000-8000-000000000103"
EVENT_ID = "00000000-0000-4000-8000-000000000104"


def task_response(**overrides: object) -> bytes:
    task: dict[str, object] = {
        "id": TASK_ID,
        "claimId": CLAIM_ID,
        "conversationId": CONVERSATION_ID,
        "eventId": EVENT_ID,
        "leadType": "school",
        "subject": "Dotaz na pilot",
        "content": "Máme zájem o pilot pro 120 studentů a 7 předmětů.",
        "autonomousTurns": 1,
    }
    task.update(overrides)
    return json.dumps(
        {"task": task}, ensure_ascii=False, separators=(",", ":")
    ).encode()


def classification(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "intent": "interested",
        "confidence": 0.99,
        "faqTopic": "trial",
        "seatCount": 120,
        "subjectCount": 7,
        "summary": "Škola má zájem o pilot pro 120 studentů.",
        "riskTags": [],
    }
    value.update(overrides)
    return value


class ContractTests(unittest.TestCase):
    def test_claim_accepts_only_exact_task_or_null(self) -> None:
        self.assertIsNone(parse_claim_response(b'{"task":null}'))
        task = parse_claim_response(task_response())
        assert task is not None
        self.assertEqual(task.id, TASK_ID)
        self.assertEqual(task.claim_id, CLAIM_ID)
        self.assertEqual(task.lead_type, "school")
        self.assertEqual(task.autonomous_turns, 1)

        with self.assertRaises(ValidationError):
            parse_claim_response(b'{"task":null,"workerId":"forbidden"}')
        with self.assertRaises(ValidationError):
            parse_claim_response(task_response(generatedReply="forbidden"))
        with self.assertRaises(ValidationError):
            parse_claim_response(task_response(id="not-a-uuid"))
        with self.assertRaises(ValidationError):
            parse_claim_response(task_response(autonomousTurns=True))

    def test_duplicate_keys_and_nonfinite_numbers_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            parse_claim_response(b'{"task":null,"task":null}')
        raw = json.dumps(classification()).replace("0.99", "NaN").encode()
        with self.assertRaises(ValidationError):
            parse_classification_json(raw)

    def test_task_content_limit_is_20_000_utf16_units(self) -> None:
        task = parse_claim_response(task_response(content="🙂" * 10_000))
        assert task is not None
        self.assertEqual(len(task.content), 10_000)

        with self.assertRaises(ValidationError):
            parse_claim_response(task_response(content="🙂" * 10_001))

    def test_task_subject_limit_is_998_utf16_units_without_normalization(self) -> None:
        hidden_opt_out = "A" * 520 + " unsubscribe"
        task = parse_claim_response(task_response(subject=hidden_opt_out))
        assert task is not None
        self.assertEqual(task.subject, hidden_opt_out)

        emoji_subject = parse_claim_response(task_response(subject="🙂" * 499))
        assert emoji_subject is not None
        self.assertEqual(emoji_subject.subject, "🙂" * 499)

        for invalid_subject in ("🙂" * 500, "safe\nunsafe", "safe\u0085unsafe"):
            with self.subTest(invalid_subject=repr(invalid_subject[:20])):
                with self.assertRaises(ValidationError):
                    parse_claim_response(task_response(subject=invalid_subject))

    def test_every_intent_and_allowed_risk_tag_matches_the_contract(self) -> None:
        for intent in INTENTS:
            parsed = parse_classification(classification(intent=intent))
            self.assertEqual(parsed.intent, intent)
        parsed = parse_classification(classification(riskTags=sorted(RISK_TAGS)))
        self.assertEqual(set(parsed.risk_tags), RISK_TAGS)

        # The local classifier and HTTP boundary now share the parser constants;
        # there is no external model schema that can drift from this contract.
        self.assertEqual(len(INTENTS), 15)
        self.assertEqual(
            FAQ_TOPICS,
            {"subjects", "classroom", "trial", "scio_relationship", "pricing", "other"},
        )
        self.assertEqual(len(RISK_TAGS), 8)

    def test_classifier_rejects_reply_fields_and_invalid_bounds(self) -> None:
        for invalid in [
            classification(generatedReply="Dobrý den"),
            classification(confidence=True),
            classification(confidence=1.01),
            classification(subjectCount=8),
            classification(seatCount=0),
            classification(summary=""),
            classification(riskTags=["prompt_injection", "prompt_injection"]),
            classification(riskTags=["made_up"]),
        ]:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValidationError):
                    parse_classification(invalid)

        self.assertEqual(
            len(parse_classification(classification(summary="🙂" * 400)).summary),
            400,
        )
        with self.assertRaises(ValidationError):
            parse_classification(classification(summary="🙂" * 401))

    def test_complete_payload_contains_classification_only(self) -> None:
        task = parse_claim_response(task_response())
        assert task is not None
        parsed = parse_classification(classification())
        payload = build_complete_payload(task, parsed)
        self.assertEqual(set(payload), {"taskId", "claimId", "classification"})
        self.assertEqual(
            set(payload["classification"]),
            {
                "intent",
                "confidence",
                "faqTopic",
                "seatCount",
                "subjectCount",
                "summary",
                "riskTags",
            },
        )
        serialized = json.dumps(payload)
        self.assertNotIn("generatedReply", serialized)
        self.assertNotIn("workerId", serialized)


if __name__ == "__main__":
    unittest.main()
