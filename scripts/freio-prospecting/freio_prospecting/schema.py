from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .common import (
    SHA256_HEX,
    ValidationError,
    canonical_json_bytes,
    parse_utc_timestamp,
    require_exact_keys,
    require_object,
    require_text,
    sha256_hex,
)
from .fetcher import FetchError, normalize_public_https_url


MAX_CANDIDATES = 30
PARTNER_TYPES = frozenset({"influencer", "ambassador"})
SOCIAL_CHANNELS = frozenset({"instagram", "tiktok"})
PRIORITIES = frozenset({"A", "B", "C"})
RISK_FLAGS = frozenset(
    {
        "identity_unconfirmed",
        "audience_fit_unconfirmed",
        "reach_unconfirmed",
        "contact_unconfirmed",
        "paid_deal_likely",
        "competitor_overlap",
        "minor_or_representative_unknown",
    }
)
EMAIL = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@"
    r"(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,63}$",
    re.IGNORECASE,
)
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,119}$")
HANDLE = re.compile(r"^@?[A-Za-z0-9._]{2,99}$")
MAX_SIGNED_BODY_BYTES = 96 * 1024


@dataclass(frozen=True)
class ResearchCandidate:
    partner_type: str
    name: str
    platform: str | None
    handle: str | None
    social_channel: str | None
    source_url: str
    claimed_email: str | None
    source_key: str
    priority: str
    category: str
    personalization_note: str | None
    risk_flags: tuple[str, ...]


def research_candidate_to_json(candidate: ResearchCandidate) -> dict[str, Any]:
    context: dict[str, Any] = {
        "disposition": "ready_for_manual_review",
        "sourceKey": candidate.source_key,
        "priority": candidate.priority,
        "category": candidate.category,
        "riskFlags": list(candidate.risk_flags),
    }
    if candidate.personalization_note:
        context["personalizationNote"] = candidate.personalization_note
    value: dict[str, Any] = {
        "partnerType": candidate.partner_type,
        "name": candidate.name,
        "sourceUrl": candidate.source_url,
        "researchContext": context,
    }
    if candidate.platform:
        value["platform"] = candidate.platform
    if candidate.handle:
        value["handle"] = candidate.handle
    if candidate.social_channel:
        value["socialChannel"] = candidate.social_channel
    if candidate.claimed_email:
        value["claimedEmail"] = candidate.claimed_email
    return value


def build_research_document(candidates: Iterable[ResearchCandidate]) -> dict[str, Any]:
    return {
        "schemaVersion": "1",
        "candidates": [
            research_candidate_to_json(candidate) for candidate in candidates
        ],
    }


def normalize_email(value: object, field: str = "claimedEmail") -> str:
    email = require_text(value, field, 3, 320).lower()
    if EMAIL.fullmatch(email) is None:
        raise ValidationError(f"{field} is not a valid email address")
    local = email.rsplit("@", 1)[0]
    if local.startswith(".") or local.endswith(".") or ".." in local:
        raise ValidationError(f"{field} is not a valid email address")
    return email


def _optional_text(value: object, field: str, minimum: int, maximum: int) -> str | None:
    if value is None:
        return None
    return require_text(value, field, minimum, maximum)


def parse_research_document(value: object) -> tuple[ResearchCandidate, ...]:
    document = require_object(value, "document")
    require_exact_keys(
        document,
        required={"schemaVersion", "candidates"},
        field="document",
    )
    if document["schemaVersion"] != "1":
        raise ValidationError("schemaVersion must equal 1")
    raw_candidates = document["candidates"]
    if (
        not isinstance(raw_candidates, list)
        or not 1 <= len(raw_candidates) <= MAX_CANDIDATES
    ):
        raise ValidationError(f"candidates must contain 1 to {MAX_CANDIDATES} items")
    candidates: list[ResearchCandidate] = []
    for index, raw in enumerate(raw_candidates):
        field = f"candidates[{index}]"
        candidate = require_object(raw, field)
        require_exact_keys(
            candidate,
            required={
                "partnerType",
                "name",
                "sourceUrl",
                "researchContext",
            },
            optional={"platform", "handle", "socialChannel", "claimedEmail"},
            field=field,
        )
        partner_type = candidate["partnerType"]
        if not isinstance(partner_type, str) or partner_type not in PARTNER_TYPES:
            raise ValidationError(f"{field}.partnerType is unsupported")
        name = require_text(candidate["name"], f"{field}.name", 2, 120)
        platform = _optional_text(candidate.get("platform"), f"{field}.platform", 2, 40)
        handle = _optional_text(candidate.get("handle"), f"{field}.handle", 2, 100)
        if handle is not None and HANDLE.fullmatch(handle) is None:
            raise ValidationError(
                f"{field}.handle must be a plain Instagram/TikTok handle"
            )
        social_channel = candidate.get("socialChannel")
        if social_channel is not None and (
            not isinstance(social_channel, str) or social_channel not in SOCIAL_CHANNELS
        ):
            raise ValidationError(f"{field}.socialChannel is unsupported")
        if bool(handle) != bool(social_channel):
            raise ValidationError(
                f"{field}.handle and socialChannel must be supplied together"
            )
        try:
            source_url = normalize_public_https_url(
                require_text(candidate["sourceUrl"], f"{field}.sourceUrl", 12, 2000)
            ).url
        except FetchError as exc:
            raise ValidationError(
                f"{field}.sourceUrl is not a public HTTPS URL: {exc.code}"
            ) from exc
        claimed_email = (
            normalize_email(candidate["claimedEmail"], f"{field}.claimedEmail")
            if "claimedEmail" in candidate
            else None
        )
        context = require_object(
            candidate["researchContext"], f"{field}.researchContext"
        )
        require_exact_keys(
            context,
            required={"disposition", "sourceKey", "priority", "category", "riskFlags"},
            optional={"personalizationNote"},
            field=f"{field}.researchContext",
        )
        if (
            not isinstance(context["disposition"], str)
            or context["disposition"] != "ready_for_manual_review"
        ):
            raise ValidationError(f"{field}.researchContext.disposition is unsupported")
        source_key = require_text(
            context["sourceKey"], f"{field}.researchContext.sourceKey", 3, 120
        )
        priority = context["priority"]
        if not isinstance(priority, str) or priority not in PRIORITIES:
            raise ValidationError(f"{field}.researchContext.priority is unsupported")
        category = require_text(
            context["category"], f"{field}.researchContext.category", 2, 80
        )
        personalization_note = _optional_text(
            context.get("personalizationNote"),
            f"{field}.researchContext.personalizationNote",
            2,
            500,
        )
        risk_flags = context["riskFlags"]
        if (
            not isinstance(risk_flags, list)
            or len(risk_flags) > len(RISK_FLAGS)
            or any(
                not isinstance(flag, str) or flag not in RISK_FLAGS
                for flag in risk_flags
            )
            or len(set(flag for flag in risk_flags if isinstance(flag, str)))
            != len(risk_flags)
        ):
            raise ValidationError(f"{field}.researchContext.riskFlags is invalid")
        candidates.append(
            ResearchCandidate(
                partner_type=partner_type,
                name=name,
                platform=platform,
                handle=handle,
                social_channel=social_channel,
                source_url=source_url,
                claimed_email=claimed_email,
                source_key=source_key,
                priority=priority,
                category=category,
                personalization_note=personalization_note,
                risk_flags=tuple(risk_flags),
            )
        )
    return tuple(candidates)


def make_idempotency_key(candidate: ResearchCandidate, final_url: str) -> str:
    if candidate.handle and candidate.social_channel:
        identity: dict[str, str] = {
            "channel": candidate.social_channel,
            "handle": (
                candidate.handle[1:]
                if candidate.handle.startswith("@")
                else candidate.handle
            ).lower(),
            "partnerType": candidate.partner_type,
        }
    else:
        identity = {
            "sourceUrl": final_url,
            "partnerType": candidate.partner_type,
        }
    return f"freio-prospect-v1:{sha256_hex(canonical_json_bytes(identity))[:40]}"


def make_intake_item(
    candidate: ResearchCandidate,
    *,
    idempotency_key: str,
    evidence_url: str,
    observed_at: str,
    include_email: bool,
) -> dict[str, Any]:
    prospect: dict[str, Any] = {
        "partnerType": candidate.partner_type,
        "name": candidate.name,
        "researchContext": {
            "disposition": "ready_for_manual_review",
            "sourceKey": candidate.source_key,
            "priority": candidate.priority,
            "category": candidate.category,
            "riskFlags": list(candidate.risk_flags),
        },
    }
    if candidate.platform:
        prospect["platform"] = candidate.platform
    if candidate.handle:
        prospect["handle"] = candidate.handle
    if candidate.social_channel:
        prospect["socialChannel"] = candidate.social_channel
    if candidate.personalization_note:
        prospect["researchContext"]["personalizationNote"] = (
            candidate.personalization_note
        )
    item: dict[str, Any] = {"idempotencyKey": idempotency_key, "prospect": prospect}
    if include_email and candidate.claimed_email:
        item["emailCandidate"] = {
            "email": candidate.claimed_email,
            "sourceUrl": evidence_url,
            "observedAt": observed_at,
        }
    return item


def build_signed_intake_request(
    *,
    items: list[dict[str, Any]],
    research_manifest_sha256: str,
) -> dict[str, Any]:
    if SHA256_HEX.fullmatch(research_manifest_sha256) is None:
        raise ValidationError("research manifest SHA-256 is invalid")
    intake_items = [
        {key: value for key, value in item.items() if key != "fetchReceipt"}
        for item in items
    ]
    fetch_receipts = [item.get("fetchReceipt") for item in items]
    intake_hash = sha256_hex(canonical_json_bytes({"items": intake_items}))
    evidence_hash = sha256_hex(canonical_json_bytes(fetch_receipts))
    signed_items_hash = sha256_hex(canonical_json_bytes(items))
    request = {
        "schemaVersion": "1",
        "receipt": {
            "algorithm": "sha256",
            "researchManifestSha256": research_manifest_sha256,
            "intakePayloadSha256": intake_hash,
            "evidenceSha256": evidence_hash,
            "signedItemsSha256": signed_items_hash,
            "receiptId": f"sha256:{signed_items_hash}",
        },
        "items": items,
    }
    if len(canonical_json_bytes(request)) > MAX_SIGNED_BODY_BYTES:
        raise ValidationError("signed intake request exceeds 96 KiB")
    return request


def _validate_intake_item(
    value: object, index: int, *, require_fetch_receipt: bool
) -> None:
    item = require_object(value, f"items[{index}]")
    require_exact_keys(
        item,
        required=(
            {"idempotencyKey", "prospect", "fetchReceipt"}
            if require_fetch_receipt
            else {"idempotencyKey", "prospect"}
        ),
        optional=(
            {"emailCandidate"}
            if require_fetch_receipt
            else {"emailCandidate", "fetchReceipt"}
        ),
        field=f"items[{index}]",
    )
    key = item["idempotencyKey"]
    if not isinstance(key, str) or IDEMPOTENCY_KEY.fullmatch(key) is None:
        raise ValidationError(f"items[{index}].idempotencyKey is invalid")
    prospect = require_object(item["prospect"], f"items[{index}].prospect")
    require_exact_keys(
        prospect,
        required={"partnerType", "name", "researchContext"},
        optional={"platform", "handle", "socialChannel"},
        field=f"items[{index}].prospect",
    )
    # Reuse the strict research parser for the shared prospect fields.
    reconstructed: dict[str, Any] = {
        "schemaVersion": "1",
        "candidates": [
            {
                **prospect,
                "sourceUrl": (
                    item.get("emailCandidate", {}).get("sourceUrl")
                    if isinstance(item.get("emailCandidate"), dict)
                    else "https://evidence.invalid.invalid/"
                ),
            }
        ],
    }
    # No-email intake items intentionally carry no evidence URL in the API body.
    if "emailCandidate" not in item:
        reconstructed["candidates"][0]["sourceUrl"] = "https://no-email.freio.cz/"
    parse_research_document(reconstructed)
    if "emailCandidate" in item:
        email_candidate = require_object(
            item["emailCandidate"], f"items[{index}].emailCandidate"
        )
        require_exact_keys(
            email_candidate,
            required={"email", "sourceUrl", "observedAt"},
            field=f"items[{index}].emailCandidate",
        )
        normalize_email(
            email_candidate["email"], f"items[{index}].emailCandidate.email"
        )
        try:
            normalize_public_https_url(email_candidate["sourceUrl"])
        except (FetchError, TypeError) as exc:
            raise ValidationError(
                f"items[{index}].emailCandidate.sourceUrl is invalid"
            ) from exc
        parse_utc_timestamp(
            email_candidate["observedAt"], f"items[{index}].emailCandidate.observedAt"
        )


def _validate_fetch_receipt(
    receipt_value: object,
    *,
    item: dict[str, Any],
    index: int,
    reference: datetime,
    maximum_age: timedelta,
) -> dict[str, Any]:
    field = f"items[{index}].fetchReceipt"
    receipt = require_object(receipt_value, field)
    require_exact_keys(
        receipt,
        required={
            "requestedUrl",
            "finalUrl",
            "fetchedAt",
            "observedAt",
            "status",
            "redirectCount",
            "mediaType",
            "charset",
            "byteLength",
            "bodySha256",
            "matchedEmailSha256",
            "matchMethod",
            "fetcherVersion",
            "networkPolicyVersion",
            "evidenceSha256",
        },
        field=field,
    )
    for url_field in ("requestedUrl", "finalUrl"):
        try:
            normalize_public_https_url(receipt[url_field])
        except (FetchError, TypeError) as exc:
            raise ValidationError(f"{field}.{url_field} is invalid") from exc
    fetched = parse_utc_timestamp(receipt["fetchedAt"], f"{field}.fetchedAt")
    observed = parse_utc_timestamp(receipt["observedAt"], f"{field}.observedAt")
    if fetched < reference - maximum_age or fetched > reference + timedelta(minutes=5):
        raise ValidationError(f"{field}.fetchedAt is stale or in the future")
    if abs((observed - fetched).total_seconds()) > 300:
        raise ValidationError(f"{field}.observedAt differs from fetchedAt")
    if receipt["status"] != 200:
        raise ValidationError(f"{field}.status must equal 200")
    redirect_count = receipt["redirectCount"]
    if (
        not isinstance(redirect_count, int)
        or isinstance(redirect_count, bool)
        or not 0 <= redirect_count <= 3
    ):
        raise ValidationError(f"{field}.redirectCount is invalid")
    if receipt["mediaType"] not in ("text/html", "text/plain"):
        raise ValidationError(f"{field}.mediaType is invalid")
    require_text(receipt["charset"], f"{field}.charset", 1, 64)
    byte_length = receipt["byteLength"]
    if (
        not isinstance(byte_length, int)
        or isinstance(byte_length, bool)
        or not 0 <= byte_length <= 1024 * 1024
    ):
        raise ValidationError(f"{field}.byteLength is invalid")
    if (
        not isinstance(receipt["bodySha256"], str)
        or SHA256_HEX.fullmatch(receipt["bodySha256"]) is None
    ):
        raise ValidationError(f"{field}.bodySha256 is invalid")
    email_candidate = item.get("emailCandidate")
    if isinstance(email_candidate, dict):
        normalized_email = normalize_email(
            email_candidate["email"], f"items[{index}].emailCandidate.email"
        )
        if receipt["finalUrl"] != email_candidate["sourceUrl"]:
            raise ValidationError(f"{field}.finalUrl does not match email source")
        if receipt["observedAt"] != email_candidate["observedAt"]:
            raise ValidationError(
                f"{field}.observedAt does not match email observation"
            )
        if receipt["matchedEmailSha256"] != sha256_hex(
            normalized_email.encode("utf-8")
        ):
            raise ValidationError(f"{field}.matchedEmailSha256 is invalid")
        if receipt["matchMethod"] not in ("visible_text_exact", "mailto_exact"):
            raise ValidationError(f"{field}.matchMethod is invalid")
    elif (
        receipt["matchedEmailSha256"] is not None or receipt["matchMethod"] is not None
    ):
        raise ValidationError(
            f"{field} has email match data without an email candidate"
        )
    if receipt["fetcherVersion"] != "1.1.0":
        raise ValidationError(f"{field}.fetcherVersion is unsupported")
    if receipt["networkPolicyVersion"] != "public-https-pinned-v1":
        raise ValidationError(f"{field}.networkPolicyVersion is unsupported")
    if (
        not isinstance(receipt["evidenceSha256"], str)
        or SHA256_HEX.fullmatch(receipt["evidenceSha256"]) is None
    ):
        raise ValidationError(f"{field}.evidenceSha256 is invalid")
    without_hash = {
        key: value for key, value in receipt.items() if key != "evidenceSha256"
    }
    if sha256_hex(canonical_json_bytes(without_hash)) != receipt["evidenceSha256"]:
        raise ValidationError(f"{field}.evidenceSha256 does not match")
    return receipt


def validate_signed_intake_request(
    value: object,
    *,
    now: datetime | None = None,
    maximum_age: timedelta = timedelta(days=30),
) -> dict[str, Any]:
    envelope = require_object(value, "request")
    require_exact_keys(
        envelope,
        required={"schemaVersion", "receipt", "items"},
        field="request",
    )
    if envelope["schemaVersion"] != "1":
        raise ValidationError("request.schemaVersion must equal 1")
    body = canonical_json_bytes(envelope)
    if len(body) > MAX_SIGNED_BODY_BYTES:
        raise ValidationError("signed intake request exceeds 96 KiB")
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    items = envelope["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_CANDIDATES:
        raise ValidationError(f"request.items must contain 1 to {MAX_CANDIDATES} items")
    fetch_receipts: list[dict[str, Any]] = []
    intake_items: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        _validate_intake_item(item, index, require_fetch_receipt=True)
        item_object = require_object(item, f"items[{index}]")
        fetch_receipts.append(
            _validate_fetch_receipt(
                item_object["fetchReceipt"],
                item=item_object,
                index=index,
                reference=reference,
                maximum_age=maximum_age,
            )
        )
        intake_items.append(
            {key: value for key, value in item_object.items() if key != "fetchReceipt"}
        )
    keys = [item["idempotencyKey"] for item in items]
    if len(set(keys)) != len(keys):
        raise ValidationError("request.items contains duplicate idempotency keys")
    receipt = require_object(envelope["receipt"], "request.receipt")
    require_exact_keys(
        receipt,
        required={
            "algorithm",
            "researchManifestSha256",
            "intakePayloadSha256",
            "evidenceSha256",
            "signedItemsSha256",
            "receiptId",
        },
        field="request.receipt",
    )
    expected_intake = sha256_hex(canonical_json_bytes({"items": intake_items}))
    expected_evidence = sha256_hex(canonical_json_bytes(fetch_receipts))
    expected_signed_items = sha256_hex(canonical_json_bytes(items))
    if (
        receipt["algorithm"] != "sha256"
        or not isinstance(receipt["researchManifestSha256"], str)
        or SHA256_HEX.fullmatch(receipt["researchManifestSha256"]) is None
        or receipt["intakePayloadSha256"] != expected_intake
        or receipt["evidenceSha256"] != expected_evidence
        or receipt["signedItemsSha256"] != expected_signed_items
        or receipt["receiptId"] != f"sha256:{expected_signed_items}"
    ):
        raise ValidationError("request receipt hash does not match its content")
    return envelope


def iter_item_emails(items: Iterable[dict[str, Any]]) -> Iterable[str]:
    for item in items:
        candidate = item.get("emailCandidate")
        if isinstance(candidate, dict) and isinstance(candidate.get("email"), str):
            yield candidate["email"]
