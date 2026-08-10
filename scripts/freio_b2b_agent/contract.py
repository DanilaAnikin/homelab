from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable


INTENTS = frozenset(
    {
        "interested",
        "pricing_request",
        "product_question",
        "meeting_request",
        "implementation_request",
        "custom_terms",
        "discount_request",
        "contract_or_legal",
        "privacy_or_security",
        "procurement_or_billing",
        "not_interested",
        "unsubscribe",
        "complaint",
        "automatic_reply",
        "unknown",
    }
)
FAQ_TOPICS = frozenset(
    {"subjects", "classroom", "trial", "scio_relationship", "pricing", "other"}
)
RISK_TAGS = frozenset(
    {
        "prompt_injection",
        "legal_language",
        "personal_data_request",
        "minor_data",
        "security_questionnaire",
        "competitor_or_press",
        "hostile_or_sensitive",
        "ambiguous_identity",
    }
)
LEAD_TYPES = frozenset({"school", "tutoring", "company", "other"})
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
UNSAFE_TEXT_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
SUBJECT_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
MAX_JSON_BYTES = 64 * 1024


class ValidationError(ValueError):
    """Untrusted JSON did not match the exact worker contract."""


@dataclass(frozen=True)
class Task:
    id: str
    claim_id: str
    conversation_id: str
    event_id: str
    lead_type: str
    subject: str
    content: str
    autonomous_turns: int


@dataclass(frozen=True)
class Classification:
    intent: str
    confidence: float
    faq_topic: str | None
    seat_count: int | None
    subject_count: int | None
    summary: str
    risk_tags: tuple[str, ...]

    def api_payload(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "confidence": self.confidence,
            "faqTopic": self.faq_topic,
            "seatCount": self.seat_count,
            "subjectCount": self.subject_count,
            "summary": self.summary,
            "riskTags": list(self.risk_tags),
        }


def _reject_json_constant(value: str) -> None:
    raise ValidationError("JSON contains a non-finite number")


def _unique_object(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("JSON object contains a duplicate field")
        result[key] = value
    return result


def parse_strict_json(raw: bytes, *, label: str) -> Any:
    if not raw or len(raw) > MAX_JSON_BYTES:
        raise ValidationError(f"{label} is empty or exceeds 64 KiB")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{label} is not UTF-8") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{label} is not strict JSON") from exc


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValidationError("value cannot be encoded as canonical JSON") from exc


def _exact_object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValidationError(f"{label} must be an object")
    if set(value) != keys:
        raise ValidationError(f"{label} has an unexpected shape")
    return value


def _uuid(value: Any, label: str) -> str:
    if not isinstance(value, str) or UUID_PATTERN.fullmatch(value) is None:
        raise ValidationError(f"{label} must be a canonical UUID")
    return value


def _text(
    value: Any,
    label: str,
    *,
    minimum: int,
    maximum: int,
    subject: bool = False,
    normalize: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be text")
    candidate = value.strip() if normalize else value
    matcher = SUBJECT_CONTROL_CHARACTERS if subject else UNSAFE_TEXT_CHARACTERS
    try:
        utf16_length = len(candidate.encode("utf-16-le")) // 2
        candidate.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError(f"{label} contains invalid Unicode") from exc
    if not minimum <= utf16_length <= maximum or matcher.search(candidate):
        raise ValidationError(f"{label} has an invalid length or control character")
    return candidate


def parse_claim_response(raw: bytes) -> Task | None:
    root = _exact_object(
        parse_strict_json(raw, label="claim response"), {"task"}, "claim response"
    )
    if root["task"] is None:
        return None
    task = _exact_object(
        root["task"],
        {
            "id",
            "claimId",
            "conversationId",
            "eventId",
            "leadType",
            "subject",
            "content",
            "autonomousTurns",
        },
        "claim response task",
    )
    lead_type = task["leadType"]
    if not isinstance(lead_type, str) or lead_type not in LEAD_TYPES:
        raise ValidationError("task leadType is invalid")
    autonomous_turns = task["autonomousTurns"]
    if (
        not isinstance(autonomous_turns, int)
        or isinstance(autonomous_turns, bool)
        or not 0 <= autonomous_turns <= 20
    ):
        raise ValidationError("task autonomousTurns is invalid")
    return Task(
        id=_uuid(task["id"], "task id"),
        claim_id=_uuid(task["claimId"], "task claimId"),
        conversation_id=_uuid(task["conversationId"], "task conversationId"),
        event_id=_uuid(task["eventId"], "task eventId"),
        lead_type=lead_type,
        subject=_text(
            task["subject"], "task subject", minimum=0, maximum=998, subject=True
        ),
        content=_text(task["content"], "task content", minimum=1, maximum=20_000),
        autonomous_turns=autonomous_turns,
    )


def parse_classification(value: Any) -> Classification:
    item = _exact_object(
        value,
        {
            "intent",
            "confidence",
            "faqTopic",
            "seatCount",
            "subjectCount",
            "summary",
            "riskTags",
        },
        "classification",
    )
    intent = item["intent"]
    if not isinstance(intent, str) or intent not in INTENTS:
        raise ValidationError("classification intent is invalid")
    confidence = item["confidence"]
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not math.isfinite(float(confidence))
        or not 0.0 <= float(confidence) <= 1.0
    ):
        raise ValidationError("classification confidence is invalid")
    faq_topic = item["faqTopic"]
    if faq_topic is not None and (
        not isinstance(faq_topic, str) or faq_topic not in FAQ_TOPICS
    ):
        raise ValidationError("classification faqTopic is invalid")
    seat_count = item["seatCount"]
    if seat_count is not None and (
        not isinstance(seat_count, int)
        or isinstance(seat_count, bool)
        or not 1 <= seat_count <= 1_000_000
    ):
        raise ValidationError("classification seatCount is invalid")
    subject_count = item["subjectCount"]
    if subject_count is not None and (
        not isinstance(subject_count, int)
        or isinstance(subject_count, bool)
        or not 1 <= subject_count <= 7
    ):
        raise ValidationError("classification subjectCount is invalid")
    risk_tags = item["riskTags"]
    if (
        not isinstance(risk_tags, list)
        or len(risk_tags) > len(RISK_TAGS)
        or any(not isinstance(tag, str) or tag not in RISK_TAGS for tag in risk_tags)
        or len(set(risk_tags)) != len(risk_tags)
    ):
        raise ValidationError("classification riskTags is invalid")
    return Classification(
        intent=intent,
        confidence=float(confidence),
        faq_topic=faq_topic,
        seat_count=seat_count,
        subject_count=subject_count,
        summary=_text(
            item["summary"],
            "classification summary",
            minimum=1,
            maximum=800,
            normalize=True,
        ),
        risk_tags=tuple(risk_tags),
    )


def parse_classification_json(raw: bytes) -> Classification:
    return parse_classification(parse_strict_json(raw, label="classifier output"))


def build_complete_payload(
    task: Task, classification: Classification
) -> dict[str, Any]:
    return {
        "taskId": task.id,
        "claimId": task.claim_id,
        "classification": classification.api_payload(),
    }


def validate_complete_payload(value: Any) -> dict[str, Any]:
    item = _exact_object(
        value,
        {"taskId", "claimId", "classification"},
        "complete payload",
    )
    classification = parse_classification(item["classification"])
    return {
        "taskId": _uuid(item["taskId"], "complete taskId"),
        "claimId": _uuid(item["claimId"], "complete claimId"),
        "classification": classification.api_payload(),
    }
