from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .common import ValidationError, canonical_json_bytes, sha256_hex
from .fetcher import (
    FETCHER_VERSION,
    NETWORK_POLICY_VERSION,
    FetchError,
    PublicHTTPSFetcher,
)
from .schema import (
    build_signed_intake_request,
    make_idempotency_key,
    make_intake_item,
    validate_signed_intake_request,
)
from .signing import SignedRequest, build_signed_request, normalize_submit_endpoint
from .workflow import ClaimedManifest, Spool


MAX_RESPONSE_BYTES = 256 * 1024
TRANSIENT_STATUSES = frozenset({408, 425, 429})


@dataclass(frozen=True)
class SubmitResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class SubmitTransport(Protocol):
    def __call__(
        self, endpoint: str, request: SignedRequest, timeout: float
    ) -> SubmitResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def https_submit_transport(
    endpoint: str, request: SignedRequest, timeout: float
) -> SubmitResponse:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    outbound = urllib.request.Request(
        endpoint,
        data=request.body,
        headers=request.headers,
        method="POST",
    )
    try:
        response = opener.open(outbound, timeout=timeout)
    except urllib.error.HTTPError as exc:
        body = exc.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValidationError("submit error response exceeded 256 KiB") from exc
        return SubmitResponse(exc.code, dict(exc.headers.items()), body)
    with response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValidationError("submit response exceeded 256 KiB")
        return SubmitResponse(response.status, dict(response.headers.items()), body)


def _validated_success(response: SubmitResponse, expected_count: int) -> dict[str, Any]:
    try:
        value = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(
            "submit success response is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict) or set(value) != {"intake"}:
        raise ValidationError("submit success response has an unexpected shape")
    intake = value["intake"]
    if (
        not isinstance(intake, dict)
        or set(intake) != {"success", "count", "results"}
        or intake.get("success") is not True
    ):
        raise ValidationError("submit success response did not confirm intake")
    count = intake.get("count")
    results = intake.get("results")
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or count != expected_count
        or not isinstance(results, list)
        or len(results) != expected_count
    ):
        raise ValidationError("submit success response count does not match the batch")
    for index, result in enumerate(results):
        if not isinstance(result, dict) or set(result) != {
            "index",
            "status",
            "partner_id",
            "contact_id",
            "provenance_id",
            "created_partner",
        }:
            raise ValidationError(
                "submit success response result has an unexpected shape"
            )
        if result["index"] != index or result["status"] not in {
            "created",
            "accepted",
            "replayed",
        }:
            raise ValidationError("submit success response result identity is invalid")
        for field in ("partner_id", "contact_id", "provenance_id"):
            identifier = result[field]
            if identifier is None and field != "partner_id":
                continue
            if not isinstance(identifier, str):
                raise ValidationError(
                    "submit success response contains an invalid UUID"
                )
            try:
                uuid.UUID(identifier)
            except ValueError as exc:
                raise ValidationError(
                    "submit success response contains an invalid UUID"
                ) from exc
        if not isinstance(result["created_partner"], bool):
            raise ValidationError(
                "submit success response contains an invalid created flag"
            )
    return {
        "httpStatus": response.status,
        "responseSha256": sha256_hex(response.body),
        "acceptedCount": count,
    }


def _redacted_error(response: SubmitResponse) -> str:
    return f"remote HTTP {response.status}; response_sha256={sha256_hex(response.body)}"


class SubmissionWorker:
    def __init__(
        self,
        *,
        spool: Spool,
        endpoint: str,
        secret: bytes,
        transport: SubmitTransport = https_submit_transport,
        timeout_seconds: float = 10.0,
        signer: Callable[..., SignedRequest] = build_signed_request,
        fetcher: PublicHTTPSFetcher | None = None,
    ) -> None:
        self.spool = spool
        self.endpoint = normalize_submit_endpoint(endpoint)
        self.secret = secret
        self.transport = transport
        self.timeout_seconds = timeout_seconds
        self.signer = signer
        self.fetcher = fetcher or PublicHTTPSFetcher()

    def run(self, maximum_batches: int = 10) -> dict[str, int]:
        if not 1 <= maximum_batches <= 100:
            raise ValidationError("maximum_batches must be between 1 and 100")
        counters = {"processed": 0, "quarantined": 0, "deferred": 0, "recovered": 0}
        with self.spool.submit_lock():
            for _ in range(maximum_batches):
                processing = self.spool.processing_next()
                if processing is not None:
                    claimed = processing.claimed
                    signed_request = processing.signed_request
                    counters["recovered"] += 1
                else:
                    next_claimed = self.spool.claim_next()
                    signed_request = None
                    if next_claimed is None:
                        break
                    claimed = next_claimed
                outcome = self._submit_claimed(claimed, signed_request)
                counters[outcome] += 1
                if outcome == "deferred":
                    # Avoid hammering an unhealthy endpoint within one invocation.
                    break
        return counters

    def _submit_claimed(
        self,
        claimed: ClaimedManifest,
        signed_request: dict[str, Any] | None,
    ) -> str:
        if signed_request is None:
            try:
                signed_request = self._build_verified_envelope(claimed)
                validate_signed_intake_request(signed_request)
                body = self.spool.persist_signed_request(claimed, signed_request)
            except (FetchError, ValidationError) as exc:
                self.spool.quarantine_claimed(
                    claimed.path,
                    "submit_evidence_rejected",
                    f"{getattr(exc, 'code', 'validation_error')}: {str(exc)[:300]}",
                )
                return "quarantined"
        else:
            body = canonical_json_bytes(signed_request)
        request = self.signer(endpoint=self.endpoint, body=body, secret=self.secret)
        request.headers["Idempotency-Key"] = signed_request["receipt"]["receiptId"]
        try:
            response = self.transport(self.endpoint, request, self.timeout_seconds)
        except (
            OSError,
            TimeoutError,
            ssl.SSLError,
            urllib.error.URLError,
            ValidationError,
        ):
            return "deferred"
        if 200 <= response.status < 300:
            try:
                result = _validated_success(response, len(signed_request["items"]))
            except ValidationError:
                return "deferred"
            self.spool.mark_processed(claimed, result, signed_request)
            return "processed"
        if response.status in TRANSIENT_STATUSES or response.status >= 500:
            return "deferred"
        self.spool.quarantine_claimed(
            claimed.path, "remote_rejected", _redacted_error(response)
        )
        return "quarantined"

    def _build_verified_envelope(self, claimed: ClaimedManifest) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        keys: set[str] = set()
        for candidate in claimed.candidates:
            document = self.fetcher.fetch(candidate.source_url)
            match_method: str | None = None
            if candidate.claimed_email:
                matched = next(
                    (
                        entry
                        for entry in document.emails
                        if entry.email == candidate.claimed_email
                    ),
                    None,
                )
                if matched is None:
                    raise ValidationError(
                        "claimed email is not exactly present during submit-side evidence fetch"
                    )
                match_method = (
                    "mailto_exact"
                    if "mailto" in matched.kinds
                    else "visible_text_exact"
                )
            key = make_idempotency_key(candidate, document.final_url)
            if key in keys:
                raise ValidationError(
                    "submit-side normalized prospect identity is duplicated"
                )
            keys.add(key)
            observed_at = document.fetched_at
            items.append(
                make_intake_item(
                    candidate,
                    idempotency_key=key,
                    evidence_url=document.final_url,
                    observed_at=observed_at,
                    include_email=bool(candidate.claimed_email),
                )
            )
            fetch_receipt: dict[str, Any] = {
                "requestedUrl": document.requested_url,
                "finalUrl": document.final_url,
                "fetchedAt": document.fetched_at,
                "observedAt": observed_at,
                "status": document.status,
                "redirectCount": document.redirect_count,
                "mediaType": document.media_type,
                "charset": document.charset,
                "byteLength": len(document.body),
                "bodySha256": document.body_sha256,
                "matchedEmailSha256": (
                    sha256_hex(candidate.claimed_email.encode("utf-8"))
                    if candidate.claimed_email
                    else None
                ),
                "matchMethod": match_method,
                "fetcherVersion": FETCHER_VERSION,
                "networkPolicyVersion": NETWORK_POLICY_VERSION,
            }
            fetch_receipt["evidenceSha256"] = sha256_hex(
                canonical_json_bytes(fetch_receipt)
            )
            items[-1]["fetchReceipt"] = fetch_receipt
        return build_signed_intake_request(
            items=items,
            research_manifest_sha256=sha256_hex(canonical_json_bytes(claimed.document)),
        )
