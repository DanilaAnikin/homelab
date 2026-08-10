from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKER_ROOT = ROOT / "scripts" / "freio-b2b-discovery"
LEGACY_ROOT = ROOT / "scripts" / "freio-prospecting"
for path in (WORKER_ROOT, LEGACY_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from freio_b2b_discovery.model import Candidate  # noqa: E402
from freio_prospecting.common import canonical_json_bytes, sha256_hex  # noqa: E402
from freio_prospecting.fetcher import (  # noqa: E402
    EmailEvidence,
    FetchedDocument,
)


TRANSPORT_SECRET = b"1" * 64
IDENTITY_SECRET = b"2" * 64
ENDPOINT = "https://outreach.freio.cz/api/internal/b2b-agent/prospect-intake"


def candidate(**overrides: object) -> Candidate:
    values: dict[str, object] = {
        "lead_type": "tutoring",
        "name": "Doučování Příklad s.r.o.",
        "website": "https://www.doucovani.cz/",
        "ico": "12345679",
        "legal_form": "sro",
        "source_url": "https://www.doucovani.cz/kontakt",
        "legal_entity_source_url": "https://www.doucovani.cz/kontakt",
        "email": "kontakt@doucovani.cz",
        "category": "doučování",
        "region": "Praha",
        "city": "Praha",
    }
    values.update(overrides)
    return Candidate(**values)  # type: ignore[arg-type]


def research_document(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "lead": {
            "leadType": "tutoring",
            "name": "Doučování Příklad s.r.o.",
            "website": "https://www.doucovani.cz/",
            "ico": "12345679",
            "legalForm": "sro",
            "category": "doučování",
            "region": "Praha",
            "city": "Praha",
        },
        "contact": {"email": "kontakt@doucovani.cz"},
        "sourceUrl": "https://www.doucovani.cz/kontakt",
        "legalEntitySourceUrl": "https://www.doucovani.cz/kontakt",
    }
    item.update(overrides)
    return {"schemaVersion": "1", "candidates": [item]}


def research_bytes(**overrides: object) -> bytes:
    return canonical_json_bytes(research_document(**overrides))


def fetched_document(
    *,
    email: str = "kontakt@doucovani.cz",
    final_url: str = "https://www.doucovani.cz/kontakt",
    kinds: tuple[str, ...] = ("visible_text",),
    legal_name: str = "Doučování Příklad s.r.o.",
    ico: str = "12345679",
) -> FetchedDocument:
    body = f"{legal_name} IČO: {ico} Kontakt: {email}".encode()
    return FetchedDocument(
        requested_url="https://www.doucovani.cz/kontakt",
        final_url=final_url,
        fetched_at="2026-08-10T17:00:00Z",
        status=200,
        media_type="text/html",
        charset="utf-8",
        body=body,
        body_sha256=sha256_hex(body),
        redirect_count=0,
        emails=(EmailEvidence(email, kinds),),
    )


def success_response(receipt_id: str, status: str = "created") -> bytes:
    return json.dumps(
        {
            "intake": {
                "success": True,
                "receipt_id": receipt_id,
                "cached": True,
                "count": 1,
                "results": [
                    {
                        "index": 0,
                        "status": status,
                        "lead_id": "00000000-0000-4000-8000-000000000001",
                        "contact_id": "00000000-0000-4000-8000-000000000002",
                        "provenance_id": "00000000-0000-4000-8000-000000000003",
                        "created_lead": status == "created",
                        "created_contact": status == "created",
                    }
                ],
            }
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
