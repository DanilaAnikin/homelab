from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "scripts" / "freio-prospecting"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from freio_prospecting.common import canonical_json_bytes, sha256_hex  # noqa: E402
from freio_prospecting.fetcher import EmailEvidence, FetchedDocument  # noqa: E402
from freio_prospecting.schema import (  # noqa: E402
    build_signed_intake_request,
    make_idempotency_key,
    make_intake_item,
    parse_research_document,
)


NOW = "2026-08-09T10:00:00Z"
NOW_DT = datetime(2026, 8, 9, 10, 0, tzinfo=timezone.utc)


def research_candidate(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "partnerType": "influencer",
        "name": "Maturita bez stresu",
        "platform": "Instagram",
        "handle": "@maturita_bez_stresu",
        "socialChannel": "instagram",
        "sourceUrl": "https://creator.example.cz/contact",
        "claimedEmail": "hello@creator.example.cz",
        "researchContext": {
            "disposition": "ready_for_manual_review",
            "sourceKey": "instagram:maturita_bez_stresu",
            "priority": "A",
            "category": "study tips",
            "personalizationNote": "Publikuje veřejné tipy k maturitě.",
            "riskFlags": [],
        },
    }
    value.update(overrides)
    return value


def research_document(*candidates: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": "1",
        "candidates": list(candidates or (research_candidate(),)),
    }


def fetched_document(
    *,
    requested_url: str = "https://creator.example.cz/contact",
    final_url: str = "https://creator.example.cz/contact",
    email: str | None = "hello@creator.example.cz",
) -> FetchedDocument:
    return FetchedDocument(
        requested_url=requested_url,
        final_url=final_url,
        fetched_at=NOW,
        status=200,
        media_type="text/html",
        charset="utf-8",
        body=(
            body := (
                (f'<a href="mailto:{email}">{email}</a>').encode("utf-8")
                if email
                else b"No public email"
            )
        ),
        body_sha256=sha256_hex(body),
        redirect_count=0,
        emails=(EmailEvidence(email, ("visible_text", "mailto")),) if email else (),
    )


def batch_envelope(
    *,
    candidate_value: dict[str, Any] | None = None,
    created_at: str = NOW,
) -> dict[str, Any]:
    candidate = parse_research_document(
        research_document(candidate_value or research_candidate())
    )[0]
    document = fetched_document()
    key = make_idempotency_key(candidate, document.final_url)
    item = make_intake_item(
        candidate,
        idempotency_key=key,
        evidence_url=document.final_url,
        observed_at=created_at,
        include_email=True,
    )
    fetch_receipt = {
        "requestedUrl": document.requested_url,
        "finalUrl": document.final_url,
        "fetchedAt": created_at,
        "mediaType": document.media_type,
        "charset": document.charset,
        "status": 200,
        "redirectCount": 0,
        "byteLength": len(document.body),
        "bodySha256": document.body_sha256,
        "observedAt": created_at,
        "matchedEmailSha256": sha256_hex(candidate.claimed_email.encode("utf-8")),
        "matchMethod": "mailto_exact",
        "fetcherVersion": "1.1.0",
        "networkPolicyVersion": "public-https-pinned-v1",
    }
    fetch_receipt["evidenceSha256"] = sha256_hex(canonical_json_bytes(fetch_receipt))
    item["fetchReceipt"] = fetch_receipt
    return build_signed_intake_request(
        items=[item],
        research_manifest_sha256=sha256_hex(
            canonical_json_bytes(
                research_document(candidate_value or research_candidate())
            )
        ),
    )
