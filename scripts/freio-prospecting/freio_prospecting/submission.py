from __future__ import annotations

import hmac
import http.client
import json
import ssl
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Callable, Mapping, Protocol

from .common import ValidationError, canonical_json_bytes, sha256_hex
from .fetcher import (
    FETCHER_VERSION,
    NETWORK_POLICY_VERSION,
    FetchError,
    PublicHTTPSFetcher,
)
from .schema import (
    ResearchCandidate,
    build_signed_intake_request,
    make_idempotency_key,
    make_intake_item,
    validate_signed_intake_request,
)
from .signing import SignedRequest, build_signed_request, normalize_submit_endpoint
from .workflow import ClaimedManifest, Spool


MAX_RESPONSE_BYTES = 256 * 1024
TRANSIENT_STATUSES = frozenset({408, 425, 429})
TRANSIENT_FETCH_CODES = frozenset(
    {"dns_failure", "dns_empty", "network_failure", "timeout"}
)
SUBMIT_OVERALL_BUDGET_SECONDS = 210.0


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


class SubmissionWorker:
    def __init__(
        self,
        *,
        spool: Spool,
        endpoint: str,
        secret: bytes,
        identity_secret: bytes,
        transport: SubmitTransport = https_submit_transport,
        timeout_seconds: float = 10.0,
        signer: Callable[..., SignedRequest] = build_signed_request,
        fetcher: PublicHTTPSFetcher | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        overall_budget_seconds: float = SUBMIT_OVERALL_BUDGET_SECONDS,
    ) -> None:
        self.spool = spool
        self.endpoint = normalize_submit_endpoint(endpoint)
        self.secret = secret
        self.identity_secret = identity_secret
        self.spool.bind_identity_key(identity_secret)
        self.transport = transport
        self.timeout_seconds = timeout_seconds
        self.signer = signer
        self.fetcher = fetcher or PublicHTTPSFetcher()
        self.monotonic = monotonic
        if not 20 <= overall_budget_seconds <= SUBMIT_OVERALL_BUDGET_SECONDS:
            raise ValidationError(
                "submit overall budget must be between 20 and 210 seconds"
            )
        self.overall_budget_seconds = overall_budget_seconds

    def run(self, maximum_batches: int = 10) -> dict[str, int]:
        if not 1 <= maximum_batches <= 100:
            raise ValidationError("maximum_batches must be between 1 and 100")
        counters = {
            "processed": 0,
            "duplicates": 0,
            "quarantined": 0,
            "deferred": 0,
            "recovered": 0,
        }
        deadline = self.monotonic() + self.overall_budget_seconds
        with self.spool.submit_lock():
            self.spool.assert_no_reconciliation_journal()
            self.spool.assert_global_circuit_closed()
            self.spool.purge_expired(scope="submit")
            for _ in range(maximum_batches):
                if self.monotonic() >= deadline:
                    break
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
                if outcome.startswith("deferred"):
                    counters["deferred"] += 1
                elif outcome in {"conflict", "uncertain_remote"}:
                    counters["quarantined"] += 1
                else:
                    counters[outcome] += 1
                if outcome in {
                    "deferred_remote",
                    "deferred_global",
                    "conflict",
                    "uncertain_remote",
                }:
                    # Avoid hammering an unhealthy endpoint within one invocation.
                    break
        return counters

    def _submit_claimed(
        self,
        claimed: ClaimedManifest,
        signed_request: dict[str, Any] | None,
    ) -> str:
        if self.spool.discard_if_processed(claimed):
            return "duplicates"
        if self.spool.retry_exhausted(claimed.path):
            if signed_request is None:
                self.spool.quarantine_claimed(
                    claimed.path,
                    "retry_exhausted",
                    "source evidence retries exhausted before any POST",
                )
                return "quarantined"
            else:
                exhausted_digests = self._identity_digests_from_request(signed_request)
                self._mark_uncertain_and_quarantine(
                    claimed,
                    exhausted_digests,
                    signed_request["receipt"]["receiptId"],
                    "retry_exhausted",
                )
            return "uncertain_remote"
        if signed_request is None:
            try:
                signed_request, identity_digests = self._build_verified_envelope(
                    claimed
                )
                validate_signed_intake_request(signed_request)
            except FetchError as exc:
                if self._is_transient_fetch(exc):
                    if self.spool.defer_claimed(claimed.path, exc.code):
                        return "deferred_source"
                    self.spool.quarantine_claimed(
                        claimed.path, "retry_exhausted", "evidence retries exhausted"
                    )
                    return "quarantined"
                self.spool.quarantine_claimed(
                    claimed.path,
                    "submit_evidence_rejected",
                    f"{exc.code}: {str(exc)[:300]}",
                )
                return "quarantined"
            except ValidationError as exc:
                self.spool.quarantine_claimed(
                    claimed.path,
                    "submit_evidence_rejected",
                    f"{getattr(exc, 'code', 'validation_error')}: {str(exc)[:300]}",
                )
                return "quarantined"
            # Durable spool/index failures are deliberately outside the
            # candidate-validation catch above and therefore fail the unit loud.
            receipt_id = signed_request["receipt"]["receiptId"]
            matches = self.spool.accepted_identity_matches(
                identity_digests, exclude_receipt_id=receipt_id
            )
            if matches:
                self.spool.mark_duplicate(claimed, matches)
                return "duplicates"
            body = self.spool.persist_signed_request(claimed, signed_request)
        else:
            body = canonical_json_bytes(signed_request)
            identity_digests = self._identity_digests_from_request(signed_request)
            receipt_id = signed_request["receipt"]["receiptId"]
            matches = self.spool.accepted_identity_matches(
                identity_digests, exclude_receipt_id=receipt_id
            )
            if matches:
                self.spool.clear_pending_identities(identity_digests, receipt_id)
                self.spool.mark_duplicate(claimed, matches)
                return "duplicates"
        receipt_id = signed_request["receipt"]["receiptId"]
        # Persist the hash-only pending tombstone before the first potentially
        # ambiguous POST. Retries with the same receipt explicitly exclude it.
        self.spool.record_pending_identities(
            identity_digests, receipt_id, claimed.path.stem
        )
        request = self.signer(endpoint=self.endpoint, body=body, secret=self.secret)
        request.headers["Idempotency-Key"] = receipt_id
        try:
            response = self.transport(self.endpoint, request, self.timeout_seconds)
        except ValidationError as exc:
            self.spool.open_global_circuit(
                "transport_protocol_error", sha256_hex(str(exc).encode("utf-8"))
            )
            return "deferred_global"
        except (
            OSError,
            TimeoutError,
            ssl.SSLError,
            urllib.error.URLError,
            http.client.HTTPException,
        ):
            if self.spool.defer_claimed(claimed.path, "submit_network"):
                return "deferred_remote"
            self._mark_uncertain_and_quarantine(
                claimed, identity_digests, receipt_id, "submit_network_uncertain"
            )
            return "uncertain_remote"
        if 200 <= response.status < 300:
            try:
                result = _validated_success(response, len(signed_request["items"]))
            except ValidationError as exc:
                self.spool.open_global_circuit(
                    "invalid_2xx",
                    sha256_hex(str(exc).encode("utf-8")),
                )
                return "deferred_global"
            self.spool.mark_processed(claimed, result, signed_request, identity_digests)
            return "processed"
        if response.status in TRANSIENT_STATUSES or response.status >= 500:
            if self.spool.defer_claimed(claimed.path, f"remote_http_{response.status}"):
                return "deferred_remote"
            self._mark_uncertain_and_quarantine(
                claimed,
                identity_digests,
                receipt_id,
                f"remote_http_{response.status}_uncertain",
            )
            return "uncertain_remote"
        if response.status != 409:
            # Authentication, routing, deployment and unsupported-media errors
            # are global operational failures, never candidate-terminal.
            self.spool.open_global_circuit(
                f"remote_http_{response.status}", sha256_hex(response.body)
            )
            return "deferred_global"
        if response.status == 409:
            self._mark_uncertain_and_quarantine(
                claimed, identity_digests, receipt_id, "remote_conflict"
            )
            return "conflict"

    def _mark_uncertain_and_quarantine(
        self,
        claimed: ClaimedManifest,
        identity_digests: tuple[str, ...],
        receipt_id: str,
        code: str,
    ) -> None:
        self.spool.mark_identities_uncertain(
            identity_digests, receipt_id, claimed.path.stem
        )
        self.spool.quarantine_claimed(
            claimed.path,
            code,
            "remote outcome requires operator reconciliation",
        )

    def _build_verified_envelope(
        self, claimed: ClaimedManifest
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        if len(claimed.candidates) != 1:
            raise ValidationError("worker manifest must contain exactly one candidate")
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
        request = build_signed_intake_request(
            items=items,
            research_manifest_sha256=sha256_hex(canonical_json_bytes(claimed.document)),
        )
        candidate = claimed.candidates[0]
        receipt = request["items"][0]["fetchReceipt"]
        return request, self._identity_digests(
            candidate,
            requested_url=receipt["requestedUrl"],
            final_url=receipt["finalUrl"],
        )

    @staticmethod
    def _is_transient_fetch(error: FetchError) -> bool:
        if error.code in TRANSIENT_FETCH_CODES:
            return True
        if error.code != "http_status":
            return False
        import re

        match = re.search(r"HTTP ([0-9]{3})", str(error))
        if not match:
            return False
        status = int(match.group(1))
        return status in TRANSIENT_STATUSES or status >= 500

    def _identity_digest(self, kind: str, normalized_value: str) -> str:
        index_key = hmac.new(
            self.identity_secret,
            b"freio-prospect-identity-index-key-v1",
            sha256,
        ).digest()
        digest = hmac.new(
            index_key,
            f"{kind}\0{normalized_value}".encode("utf-8"),
            sha256,
        ).hexdigest()
        return f"hmac-sha256:{digest}"

    def _identity_digests(
        self,
        candidate: ResearchCandidate,
        *,
        requested_url: str,
        final_url: str,
    ) -> tuple[str, ...]:
        values: set[tuple[str, str]] = set()
        if candidate.handle and candidate.social_channel:
            values.add(
                (
                    "social",
                    f"{candidate.social_channel}:{candidate.handle.lstrip('@').lower()}",
                )
            )
        if candidate.claimed_email:
            values.add(("email", candidate.claimed_email))
        if not values:
            # Source identity is a fallback only. Directory/list pages may
            # legitimately contain many distinct social/email identities.
            values.add(("source", final_url))
        return tuple(
            sorted(self._identity_digest(kind, value) for kind, value in values)
        )

    def _identity_digests_from_request(
        self, request: dict[str, Any]
    ) -> tuple[str, ...]:
        if len(request.get("items", [])) != 1:
            raise ValidationError("worker request must contain exactly one item")
        item = request["items"][0]
        prospect = item["prospect"]
        receipt = item["fetchReceipt"]
        candidate = ResearchCandidate(
            partner_type=prospect["partnerType"],
            name=prospect["name"],
            platform=prospect.get("platform"),
            handle=prospect.get("handle"),
            social_channel=prospect.get("socialChannel"),
            source_url=receipt["requestedUrl"],
            claimed_email=(item.get("emailCandidate") or {}).get("email"),
            source_key=prospect["researchContext"]["sourceKey"],
            priority=prospect["researchContext"]["priority"],
            category=prospect["researchContext"]["category"],
            personalization_note=prospect["researchContext"].get("personalizationNote"),
            risk_flags=tuple(prospect["researchContext"]["riskFlags"]),
        )
        return self._identity_digests(
            candidate,
            requested_url=receipt["requestedUrl"],
            final_url=receipt["finalUrl"],
        )
