from __future__ import annotations

import re
import sys
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit
from uuid import UUID


LEGACY_ROOT = Path(__file__).resolve().parents[2] / "freio-prospecting"
if str(LEGACY_ROOT) not in sys.path:
    sys.path.insert(0, str(LEGACY_ROOT))

from freio_prospecting.common import (  # noqa: E402
    MAX_JSON_BYTES,
    ValidationError,
    canonical_json_bytes,
    require_exact_keys,
    require_object,
    require_text,
    sha256_hex,
)
from freio_prospecting.fetcher import (  # noqa: E402
    FetchError,
    FetchedDocument,
    NETWORK_POLICY_VERSION,
    normalize_public_https_url,
)


MAX_CANDIDATES = 10
MAX_SIGNED_BODY_BYTES = 64 * 1024
FETCHER_VERSION = "b2b-discovery-1.0.0"
LEAD_TYPES = frozenset({"tutoring", "company", "school"})
GENERIC_INBOX_LOCAL_PARTS = frozenset(
    {
        "b2b",
        "business",
        "contact",
        "education",
        "hello",
        "help",
        "helpdesk",
        "info",
        "klienti",
        "kontakt",
        "kurzy",
        "marketing",
        "obchod",
        "office",
        "podpora",
        "pr",
        "recepce",
        "reception",
        "sales",
        "sekretariat",
        "skoleni",
        "spoluprace",
        "support",
        "team",
        "training",
        "zakaznici",
    }
)
LEGAL_FORMS = frozenset(
    {
        "sro",
        "as",
        "zs",
        "zu",
        "ops",
        "druzstvo",
        "vos",
        "ks",
        "nadace",
        "nadacni_fond",
        "prispevkova_organizace",
        "statni_prispevkova_organizace",
        "skolska_pravnicka_osoba",
    }
)
SCHOOL_ONLY_LEGAL_FORMS = frozenset(
    {
        "prispevkova_organizace",
        "statni_prispevkova_organizace",
        "skolska_pravnicka_osoba",
    }
)
LEGAL_FORM_PATTERNS = {
    "sro": re.compile(
        r"(?:\bs\s*\.\s*r\s*\.\s*o\s*\.|společnost s ručením omezeným)", re.IGNORECASE
    ),
    "as": re.compile(r"(?:\ba\s*\.\s*s\s*\.|akciová společnost)", re.IGNORECASE),
    "zs": re.compile(r"(?:\bz\s*\.\s*s\s*\.|zapsaný spolek)", re.IGNORECASE),
    "zu": re.compile(r"(?:\bz\s*\.\s*[úu]\s*\.|zapsaný ústav)", re.IGNORECASE),
    "ops": re.compile(
        r"(?:\bo\s*\.\s*p\s*\.\s*s\s*\.|obecně prospěšná společnost)", re.IGNORECASE
    ),
    "druzstvo": re.compile(r"\bdružstvo\b", re.IGNORECASE),
    "vos": re.compile(
        r"(?:\bv\s*\.\s*o\s*\.\s*s\s*\.|veřejná obchodní společnost)", re.IGNORECASE
    ),
    "ks": re.compile(r"(?:\bk\s*\.\s*s\s*\.|komanditní společnost)", re.IGNORECASE),
    "nadace": re.compile(r"\bnadace\b", re.IGNORECASE),
    "nadacni_fond": re.compile(r"\bnadační fond\b", re.IGNORECASE),
    "prispevkova_organizace": re.compile(r"\bpříspěvková organizace\b", re.IGNORECASE),
    "statni_prispevkova_organizace": re.compile(
        r"\bstátní příspěvková organizace\b", re.IGNORECASE
    ),
    "skolska_pravnicka_osoba": re.compile(
        r"\bškolská právnická osoba\b", re.IGNORECASE
    ),
}
SCHOOL_LEGAL_NAME_MARKER = re.compile(
    r"(?:\bgymn[áa]zium\b"
    r"|\bstřední(?: odborná| průmyslová| zdravotnická| pedagogická| umělecká"
    r"| zemědělská| lesnická| technická| hotelová| soukromá| veřejná)? škola\b"
    r"|\bstřední odborné učiliště\b"
    r"|\bobchodní akademie\b"
    r"|\bkonzervatoř\b)",
    re.IGNORECASE,
)
NATURAL_PERSON_MARKERS = re.compile(
    r"\b(?:osvč|osvc|živnostník|zivnostnik|fyzická osoba|fyzicka osoba)\b",
    re.IGNORECASE,
)
EMAIL = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@"
    r"(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,63}$",
    re.IGNORECASE,
)
ICO = re.compile(r"^[0-9]{8}$")
HASH = re.compile(r"^[0-9a-f]{64}$")
RECEIPT_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,119}$")


@dataclass(frozen=True)
class Candidate:
    lead_type: str
    name: str
    website: str
    ico: str
    legal_form: str
    source_url: str
    legal_entity_source_url: str
    email: str
    category: str | None
    region: str | None
    city: str | None


def _optional_text(
    value: object,
    field: str,
    minimum: int,
    maximum: int,
) -> str | None:
    if value is None:
        return None
    return require_text(value, field, minimum, maximum)


def normalize_email(value: object, field: str) -> str:
    email = require_text(value, field, 3, 320).lower()
    if EMAIL.fullmatch(email) is None:
        raise ValidationError(f"{field} is not a valid email address")
    local = email.rsplit("@", 1)[0]
    if local.startswith(".") or local.endswith(".") or ".." in local:
        raise ValidationError(f"{field} is not a valid email address")
    if local not in GENERIC_INBOX_LOCAL_PARTS:
        raise ValidationError(f"{field} must be an allowlisted generic role inbox")
    return email


def _has_legal_form(value: str, legal_form: str) -> bool:
    pattern = LEGAL_FORM_PATTERNS.get(legal_form)
    if pattern is None or pattern.search(value) is None:
        return False
    if legal_form == "prispevkova_organizace":
        return (
            LEGAL_FORM_PATTERNS["statni_prispevkova_organizace"].search(value) is None
        )
    return True


def is_relevant_secondary_school_legal_name(value: str) -> bool:
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    return SCHOOL_LEGAL_NAME_MARKER.search(normalized) is not None


def _validate_school_scope(
    lead_type: str,
    legal_name: str,
    legal_form: str,
    field: str,
) -> None:
    school_name = is_relevant_secondary_school_legal_name(legal_name)
    if lead_type == "school" and not school_name:
        raise ValidationError(
            f"{field}.name must explicitly identify a relevant secondary school"
        )
    if lead_type != "school" and (school_name or legal_form in SCHOOL_ONLY_LEGAL_FORMS):
        raise ValidationError(
            f"{field}.leadType must be school for a school legal entity"
        )


def is_valid_czech_ico(value: str) -> bool:
    if ICO.fullmatch(value) is None:
        return False
    weighted = sum(int(value[index]) * (8 - index) for index in range(7))
    expected = (11 - (weighted % 11)) % 10
    return int(value[7]) == expected


def _canonical_public_url(value: object, field: str) -> str:
    raw = require_text(value, field, 12, 2000)
    try:
        normalized = normalize_public_https_url(raw).url
    except FetchError as exc:
        raise ValidationError(f"{field} is not a public HTTPS URL: {exc.code}") from exc
    if normalized != raw:
        raise ValidationError(f"{field} must already be canonical")
    return normalized


def _canonical_website(value: object, field: str) -> str:
    website = _canonical_public_url(value, field)
    parsed = urlsplit(website)
    if parsed.path != "/" or parsed.query:
        raise ValidationError(f"{field} must be an HTTPS origin ending in slash")
    return website


def _business_host(url: str) -> str:
    hostname = (urlsplit(url).hostname or "").lower()
    return hostname[4:] if hostname.startswith("www.") else hostname


def candidate_to_json(candidate: Candidate) -> dict[str, Any]:
    lead: dict[str, Any] = {
        "leadType": candidate.lead_type,
        "name": candidate.name,
        "website": candidate.website,
        "ico": candidate.ico,
        "legalForm": candidate.legal_form,
    }
    for key, value in (
        ("category", candidate.category),
        ("region", candidate.region),
        ("city", candidate.city),
    ):
        if value is not None:
            lead[key] = value
    return {
        "lead": lead,
        "contact": {"email": candidate.email},
        "sourceUrl": candidate.source_url,
        "legalEntitySourceUrl": candidate.legal_entity_source_url,
    }


def build_research_document(candidates: Iterable[Candidate]) -> dict[str, Any]:
    return {
        "schemaVersion": "1",
        "candidates": [candidate_to_json(candidate) for candidate in candidates],
    }


def parse_research_document(value: object) -> tuple[Candidate, ...]:
    document = require_object(value, "document")
    require_exact_keys(
        document,
        required={"schemaVersion", "candidates"},
        field="document",
    )
    if document["schemaVersion"] != "1":
        raise ValidationError("schemaVersion must equal 1")
    raw_candidates = document["candidates"]
    if not isinstance(raw_candidates, list) or not 1 <= len(raw_candidates) <= 10:
        raise ValidationError("candidates must contain 1 to 10 items")

    parsed_candidates: list[Candidate] = []
    batch_identities: set[tuple[str, str]] = set()
    for index, raw_candidate in enumerate(raw_candidates):
        field = f"candidates[{index}]"
        raw = require_object(raw_candidate, field)
        require_exact_keys(
            raw,
            required={"lead", "contact", "sourceUrl", "legalEntitySourceUrl"},
            field=field,
        )
        raw_lead = require_object(raw["lead"], f"{field}.lead")
        require_exact_keys(
            raw_lead,
            required={"leadType", "name", "website", "ico", "legalForm"},
            optional={"category", "region", "city"},
            field=f"{field}.lead",
        )
        lead_type = raw_lead["leadType"]
        if not isinstance(lead_type, str) or lead_type not in LEAD_TYPES:
            raise ValidationError(
                f"{field}.lead.leadType must be tutoring, company or school"
            )
        # RED-IZO is deliberately absent: official website evidence is bound
        # to the Czech legal entity through its exact name, form and IČO.
        name = require_text(raw_lead["name"], f"{field}.lead.name", 2, 120)
        website = _canonical_website(raw_lead["website"], f"{field}.lead.website")
        ico = require_text(raw_lead["ico"], f"{field}.lead.ico", 8, 8)
        if not is_valid_czech_ico(ico):
            raise ValidationError(
                f"{field}.lead.ico must satisfy the Czech IČO checksum"
            )
        legal_form = raw_lead["legalForm"]
        if not isinstance(legal_form, str) or legal_form not in LEGAL_FORMS:
            raise ValidationError(f"{field}.lead.legalForm is unsupported")
        if not _has_legal_form(name, legal_form):
            raise ValidationError(
                f"{field}.lead.name must contain the verified legal form"
            )
        if NATURAL_PERSON_MARKERS.search(name):
            raise ValidationError(f"{field}.lead.name identifies a natural person")
        _validate_school_scope(lead_type, name, legal_form, f"{field}.lead")
        category = _optional_text(
            raw_lead.get("category"), f"{field}.lead.category", 1, 120
        )
        region = _optional_text(raw_lead.get("region"), f"{field}.lead.region", 1, 120)
        city = _optional_text(raw_lead.get("city"), f"{field}.lead.city", 1, 160)

        raw_contact = require_object(raw["contact"], f"{field}.contact")
        require_exact_keys(
            raw_contact,
            required={"email"},
            field=f"{field}.contact",
        )
        email = normalize_email(raw_contact["email"], f"{field}.contact.email")
        source_url = _canonical_public_url(raw["sourceUrl"], f"{field}.sourceUrl")
        legal_entity_source_url = _canonical_public_url(
            raw["legalEntitySourceUrl"], f"{field}.legalEntitySourceUrl"
        )
        if _business_host(source_url) != _business_host(website):
            raise ValidationError(
                f"{field}.sourceUrl must be on the candidate's official website host"
            )
        if _business_host(legal_entity_source_url) != _business_host(website):
            raise ValidationError(
                f"{field}.legalEntitySourceUrl must be on the official website host"
            )

        identity = (website, email)
        if identity in batch_identities:
            raise ValidationError("candidate website/email identity is duplicated")
        batch_identities.add(identity)
        parsed_candidates.append(
            Candidate(
                lead_type=lead_type,
                name=name,
                website=website,
                ico=ico,
                legal_form=legal_form,
                source_url=source_url,
                legal_entity_source_url=legal_entity_source_url,
                email=email,
                category=category,
                region=region,
                city=city,
            )
        )
    return tuple(parsed_candidates)


def parse_research_bytes(raw: bytes) -> tuple[dict[str, Any], tuple[Candidate, ...]]:
    if not raw or len(raw) > MAX_JSON_BYTES:
        raise ValidationError("research output is empty or exceeds 96 KiB")
    import json

    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("research output is not strict UTF-8 JSON") from exc
    candidates = parse_research_document(decoded)
    return build_research_document(candidates), candidates


class _VisibleTextParser(HTMLParser):
    _HIDDEN = frozenset({"script", "style", "noscript", "template", "svg"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.lower() in self._HIDDEN:
            self.hidden_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() in self._HIDDEN and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._HIDDEN and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.hidden_depth == 0:
            self.parts.append(data)


def _normalized_visible_text(document: FetchedDocument) -> str:
    try:
        decoded = document.body.decode(document.charset, errors="replace")
    except LookupError as exc:
        raise ValidationError("legal-entity evidence charset is unsupported") from exc
    if document.media_type == "text/html":
        parser = _VisibleTextParser()
        try:
            parser.feed(decoded)
            parser.close()
        except (AssertionError, ValueError) as exc:
            raise ValidationError("legal-entity evidence HTML is invalid") from exc
        decoded = " ".join(parser.parts)
    return " ".join(unicodedata.normalize("NFKC", decoded).split()).casefold()


def validate_legal_entity_evidence(
    candidate: Candidate, document: FetchedDocument
) -> None:
    if _business_host(document.final_url) != _business_host(candidate.website):
        raise ValidationError("legal-entity evidence left the official website host")
    visible = _normalized_visible_text(document)
    legal_name = " ".join(
        unicodedata.normalize("NFKC", candidate.name).split()
    ).casefold()
    if legal_name not in visible:
        raise ValidationError("official evidence does not contain the exact legal name")
    if not _has_legal_form(visible, candidate.legal_form):
        raise ValidationError("official evidence does not contain the legal form")
    ico_pattern = re.compile(
        r"(?<![0-9])" + r"[\s\u00a0]*".join(candidate.ico) + r"(?![0-9])"
    )
    if ico_pattern.search(visible) is None:
        raise ValidationError("official evidence does not contain the exact IČO")


def exact_email_match(candidate: Candidate, document: FetchedDocument) -> str:
    match = next(
        (entry for entry in document.emails if entry.email == candidate.email),
        None,
    )
    if match is None:
        raise ValidationError(
            "claimed email is not exactly present on the public evidence page"
        )
    return "mailto_exact" if "mailto" in match.kinds else "visible_text_exact"


def make_idempotency_key(candidate: Candidate) -> str:
    digest = sha256_hex(
        canonical_json_bytes(
            {"contract": "b2b-discovery-v1", "website": candidate.website}
        )
    )
    return f"b2b-discovery-v1:{digest[:40]}"


def build_verified_envelope(
    candidate: Candidate,
    document: FetchedDocument,
    legal_entity_document: FetchedDocument,
) -> dict[str, Any]:
    match_method = exact_email_match(candidate, document)
    validate_legal_entity_evidence(candidate, legal_entity_document)
    # Store the final page on which the exact address was observed. Redirects
    # remain covered by the shared pinned fetcher's receipt in local logs only;
    # no raw page body is persisted or sent.
    source_url = normalize_public_https_url(document.final_url).url
    if _business_host(source_url) != _business_host(candidate.website):
        raise ValidationError("evidence redirect left the official website host")
    legal_entity_source_url = normalize_public_https_url(
        legal_entity_document.final_url
    ).url
    if _business_host(legal_entity_source_url) != _business_host(candidate.website):
        raise ValidationError("legal-entity evidence left the official website host")
    lead: dict[str, Any] = {
        "leadType": candidate.lead_type,
        "name": candidate.name,
        "website": candidate.website,
        "ico": candidate.ico,
        "legalForm": candidate.legal_form,
    }
    for key, value in (
        ("category", candidate.category),
        ("region", candidate.region),
        ("city", candidate.city),
    ):
        if value is not None:
            lead[key] = value
    evidence: dict[str, Any] = {
        "sourceUrl": source_url,
        "fetchedAt": document.fetched_at,
        "observedAt": document.fetched_at,
        "status": 200,
        "mediaType": document.media_type,
        "bodySha256": document.body_sha256,
        "matchedEmailSha256": sha256_hex(candidate.email.encode("utf-8")),
        "matchMethod": match_method,
        "legalEntitySourceUrl": legal_entity_source_url,
        "legalEntityFetchedAt": legal_entity_document.fetched_at,
        "legalEntityStatus": 200,
        "legalEntityMediaType": legal_entity_document.media_type,
        "legalEntityBodySha256": legal_entity_document.body_sha256,
        "legalEntityNameSha256": sha256_hex(candidate.name.encode("utf-8")),
        "legalEntityIcoSha256": sha256_hex(candidate.ico.encode("ascii")),
        "legalEntityForm": candidate.legal_form,
        "legalEntityMatchMethod": "official_page_exact_name_form_ico",
        "fetcherVersion": FETCHER_VERSION,
        "networkPolicyVersion": NETWORK_POLICY_VERSION,
    }
    evidence["evidenceSha256"] = sha256_hex(canonical_json_bytes(evidence))
    item: dict[str, Any] = {
        "idempotencyKey": make_idempotency_key(candidate),
        "lead": lead,
        "contact": {"email": candidate.email},
        "evidence": evidence,
    }
    items = [item]
    items_hash = sha256_hex(canonical_json_bytes(items))
    envelope = {
        "schemaVersion": "1",
        "receipt": {
            "algorithm": "sha256",
            "itemsSha256": items_hash,
            "receiptId": f"sha256:{items_hash}",
        },
        "items": items,
    }
    validate_intake_envelope(envelope)
    if len(canonical_json_bytes(envelope)) > MAX_SIGNED_BODY_BYTES:
        raise ValidationError("signed B2B discovery body exceeds 64 KiB")
    return envelope


def _require_hash(value: object, field: str) -> str:
    if not isinstance(value, str) or HASH.fullmatch(value) is None:
        raise ValidationError(f"{field} must be lowercase SHA-256 hex")
    return value


def validate_intake_envelope(value: object) -> dict[str, Any]:
    envelope = require_object(value, "intake")
    require_exact_keys(
        envelope,
        required={"schemaVersion", "receipt", "items"},
        field="intake",
    )
    if envelope["schemaVersion"] != "1":
        raise ValidationError("intake.schemaVersion must equal 1")
    receipt = require_object(envelope["receipt"], "intake.receipt")
    require_exact_keys(
        receipt,
        required={"algorithm", "itemsSha256", "receiptId"},
        field="intake.receipt",
    )
    if receipt["algorithm"] != "sha256":
        raise ValidationError("intake receipt algorithm is invalid")
    items_hash = _require_hash(receipt["itemsSha256"], "receipt.itemsSha256")
    receipt_id = receipt["receiptId"]
    if (
        not isinstance(receipt_id, str)
        or RECEIPT_ID.fullmatch(receipt_id) is None
        or receipt_id != f"sha256:{items_hash}"
    ):
        raise ValidationError("intake receipt ID is invalid")
    items = envelope["items"]
    if not isinstance(items, list) or len(items) != 1:
        raise ValidationError("intake must contain exactly one item")
    if sha256_hex(canonical_json_bytes(items)) != items_hash:
        raise ValidationError("intake items hash does not match")
    item = require_object(items[0], "intake.items[0]")
    require_exact_keys(
        item,
        required={"idempotencyKey", "lead", "contact", "evidence"},
        field="intake.items[0]",
    )
    if (
        not isinstance(item["idempotencyKey"], str)
        or IDEMPOTENCY_KEY.fullmatch(item["idempotencyKey"]) is None
    ):
        raise ValidationError("intake idempotency key is invalid")
    lead = require_object(item["lead"], "intake.items[0].lead")
    require_exact_keys(
        lead,
        required={"leadType", "name", "website", "ico", "legalForm"},
        optional={"category", "region", "city"},
        field="intake.items[0].lead",
    )
    if lead["leadType"] not in LEAD_TYPES or "redIzo" in lead:
        raise ValidationError("intake lead type is outside B2B discovery scope")
    website = _canonical_website(lead["website"], "intake.items[0].lead.website")
    legal_name = require_text(lead["name"], "intake.items[0].lead.name", 2, 120)
    ico = require_text(lead["ico"], "intake.items[0].lead.ico", 8, 8)
    if not is_valid_czech_ico(ico):
        raise ValidationError("intake IČO fails the Czech checksum")
    legal_form = lead["legalForm"]
    if not isinstance(legal_form, str) or legal_form not in LEGAL_FORMS:
        raise ValidationError("intake legal form is unsupported")
    if not _has_legal_form(legal_name, legal_form):
        raise ValidationError("intake legal name does not contain its legal form")
    if NATURAL_PERSON_MARKERS.search(legal_name):
        raise ValidationError("intake legal name identifies a natural person")
    _validate_school_scope(
        lead["leadType"],
        legal_name,
        legal_form,
        "intake.items[0].lead",
    )
    contact = require_object(item["contact"], "intake.items[0].contact")
    require_exact_keys(
        contact,
        required={"email"},
        field="intake.items[0].contact",
    )
    normalized_email = normalize_email(
        contact["email"], "intake.items[0].contact.email"
    )
    if contact["email"] != normalized_email:
        raise ValidationError("intake email must already be normalized")
    evidence = require_object(item["evidence"], "intake.items[0].evidence")
    require_exact_keys(
        evidence,
        required={
            "sourceUrl",
            "fetchedAt",
            "observedAt",
            "status",
            "mediaType",
            "bodySha256",
            "matchedEmailSha256",
            "matchMethod",
            "legalEntitySourceUrl",
            "legalEntityFetchedAt",
            "legalEntityStatus",
            "legalEntityMediaType",
            "legalEntityBodySha256",
            "legalEntityNameSha256",
            "legalEntityIcoSha256",
            "legalEntityForm",
            "legalEntityMatchMethod",
            "fetcherVersion",
            "networkPolicyVersion",
            "evidenceSha256",
        },
        field="intake.items[0].evidence",
    )
    source_url = _canonical_public_url(evidence["sourceUrl"], "evidence.sourceUrl")
    legal_entity_source_url = _canonical_public_url(
        evidence["legalEntitySourceUrl"], "evidence.legalEntitySourceUrl"
    )
    if _business_host(source_url) != _business_host(website) or _business_host(
        legal_entity_source_url
    ) != _business_host(website):
        raise ValidationError("intake evidence must remain on the official website")
    if evidence["status"] != 200 or evidence["mediaType"] not in {
        "text/html",
        "text/plain",
    }:
        raise ValidationError("intake evidence HTTP metadata is invalid")
    if evidence["legalEntityStatus"] != 200 or evidence["legalEntityMediaType"] not in {
        "text/html",
        "text/plain",
    }:
        raise ValidationError("intake legal-entity HTTP metadata is invalid")
    for field in (
        "bodySha256",
        "matchedEmailSha256",
        "legalEntityBodySha256",
        "legalEntityNameSha256",
        "legalEntityIcoSha256",
        "evidenceSha256",
    ):
        _require_hash(evidence[field], f"evidence.{field}")
    if evidence["matchedEmailSha256"] != sha256_hex(normalized_email.encode()):
        raise ValidationError("intake evidence email hash is invalid")
    if evidence["matchMethod"] not in {"visible_text_exact", "mailto_exact"}:
        raise ValidationError("intake evidence match method is invalid")
    if evidence["legalEntityNameSha256"] != sha256_hex(legal_name.encode("utf-8")):
        raise ValidationError("intake legal-name evidence hash is invalid")
    if evidence["legalEntityIcoSha256"] != sha256_hex(ico.encode("ascii")):
        raise ValidationError("intake IČO evidence hash is invalid")
    if evidence["legalEntityForm"] != legal_form:
        raise ValidationError("intake legal-form evidence is invalid")
    if evidence["legalEntityMatchMethod"] != "official_page_exact_name_form_ico":
        raise ValidationError("intake legal-entity match method is invalid")
    if evidence["fetcherVersion"] != FETCHER_VERSION:
        raise ValidationError("intake evidence fetcher version is invalid")
    if evidence["networkPolicyVersion"] != NETWORK_POLICY_VERSION:
        raise ValidationError("intake evidence network policy is invalid")
    from freio_prospecting.common import parse_utc_timestamp

    fetched = parse_utc_timestamp(evidence["fetchedAt"], "evidence.fetchedAt")
    observed = parse_utc_timestamp(evidence["observedAt"], "evidence.observedAt")
    legal_entity_fetched = parse_utc_timestamp(
        evidence["legalEntityFetchedAt"], "evidence.legalEntityFetchedAt"
    )
    if abs((observed - fetched).total_seconds()) > 300:
        raise ValidationError("intake evidence timestamps are inconsistent")
    if abs((legal_entity_fetched - fetched).total_seconds()) > 300:
        raise ValidationError("intake legal-entity evidence is not contemporaneous")
    without_hash = {k: v for k, v in evidence.items() if k != "evidenceSha256"}
    if evidence["evidenceSha256"] != sha256_hex(canonical_json_bytes(without_hash)):
        raise ValidationError("intake evidence hash is invalid")
    return envelope


def validate_success_response(raw: bytes, expected_receipt_id: str) -> dict[str, Any]:
    import json

    if not raw or len(raw) > 256 * 1024:
        raise ValidationError("intake success response is empty or too large")
    try:
        value = require_object(json.loads(raw.decode("utf-8")), "response")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("intake success response is not UTF-8 JSON") from exc
    require_exact_keys(value, required={"intake"}, field="response")
    intake = require_object(value["intake"], "response.intake")
    require_exact_keys(
        intake,
        required={"success", "receipt_id", "cached", "count", "results"},
        field="response.intake",
    )
    if (
        intake["success"] is not True
        or intake["receipt_id"] != expected_receipt_id
        or intake["cached"] is not True
        or intake["count"] != 1
        or not isinstance(intake["results"], list)
        or len(intake["results"]) != 1
    ):
        raise ValidationError("intake success response did not confirm one receipt")
    result = require_object(intake["results"][0], "response.intake.results[0]")
    require_exact_keys(
        result,
        required={
            "index",
            "status",
            "lead_id",
            "contact_id",
            "provenance_id",
            "created_lead",
            "created_contact",
        },
        field="response.intake.results[0]",
    )
    if result["index"] != 0 or result["status"] not in {
        "created",
        "deduplicated",
        "replayed",
    }:
        raise ValidationError("intake success result status is invalid")
    for field in ("lead_id", "contact_id", "provenance_id"):
        try:
            UUID(result[field])
        except (ValueError, TypeError) as exc:
            raise ValidationError(
                "intake success result contains an invalid UUID"
            ) from exc
    if not isinstance(result["created_lead"], bool) or not isinstance(
        result["created_contact"], bool
    ):
        raise ValidationError("intake success result contains an invalid flag")
    return {
        "httpStatus": 200,
        "receiptId": expected_receipt_id,
        "responseSha256": sha256_hex(raw),
        "status": result["status"],
    }
