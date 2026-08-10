from __future__ import annotations

import http.client
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .model import (
    build_verified_envelope,
    validate_intake_envelope,
    validate_success_response,
)
from .signing import SignedRequest, build_signed_request, normalize_endpoint
from .state import Claimed, Spool

import sys


LEGACY_ROOT = Path(__file__).resolve().parents[2] / "freio-prospecting"
if str(LEGACY_ROOT) not in sys.path:
    sys.path.insert(0, str(LEGACY_ROOT))

from freio_prospecting.common import (  # noqa: E402
    ValidationError,
    canonical_json_bytes,
    sha256_hex,
)
from freio_prospecting.fetcher import (  # noqa: E402
    FetchError,
    PublicHTTPSFetcher,
)


MAX_RESPONSE_BYTES = 256 * 1024
OVERALL_BUDGET_SECONDS = 150.0
TRANSIENT_HTTP = frozenset({408, 425, 429})
TRANSIENT_FETCH = frozenset({"dns_failure", "dns_empty", "network_failure", "timeout"})


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
    normalized = normalize_endpoint(endpoint)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    outbound = urllib.request.Request(
        normalized,
        data=request.body,
        headers=request.headers,
        method="POST",
    )
    try:
        response = opener.open(outbound, timeout=timeout)
    except urllib.error.HTTPError as exc:
        body = exc.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValidationError("intake error response exceeded 256 KiB") from exc
        return SubmitResponse(exc.code, dict(exc.headers.items()), body)
    with response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValidationError("intake response exceeded 256 KiB")
        return SubmitResponse(response.status, dict(response.headers.items()), body)


def _is_transient_fetch(error: FetchError) -> bool:
    if error.code in TRANSIENT_FETCH:
        return True
    if error.code != "http_status":
        return False
    import re

    match = re.search(r"HTTP ([0-9]{3})", str(error))
    if not match:
        return False
    status = int(match.group(1))
    return status in TRANSIENT_HTTP or status >= 500


class SubmissionWorker:
    def __init__(
        self,
        *,
        spool: Spool,
        endpoint: str,
        transport_secret: bytes,
        identity_secret: bytes,
        transport: SubmitTransport = https_submit_transport,
        fetcher: PublicHTTPSFetcher | None = None,
        signer: Callable[..., SignedRequest] = build_signed_request,
        timeout_seconds: float = 10.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.spool = spool
        self.endpoint = normalize_endpoint(endpoint)
        if transport_secret == identity_secret:
            raise ValidationError("transport and identity HMAC credentials must differ")
        self.transport_secret = transport_secret
        self.identity_secret = identity_secret
        self.spool.bind_identity_key(identity_secret)
        self.transport = transport
        self.fetcher = fetcher or PublicHTTPSFetcher()
        self.signer = signer
        self.timeout_seconds = timeout_seconds
        self.monotonic = monotonic

    def run(self, maximum_batches: int = 5) -> dict[str, int]:
        if not 1 <= maximum_batches <= 5:
            raise ValidationError("maximum B2B discovery batches must be 1 to 5")
        counters = {
            "processed": 0,
            "duplicates": 0,
            "deferred": 0,
            "quarantined": 0,
            "uncertain": 0,
        }
        deadline = self.monotonic() + OVERALL_BUDGET_SECONDS
        with self.spool.submit_lock():
            self.spool.assert_circuit_closed()
            self.spool.purge("submit")
            for _ in range(maximum_batches):
                if self.monotonic() >= deadline:
                    break
                claimed = self.spool.claim_next()
                if claimed is None:
                    break
                outcome = self._submit(claimed)
                counters[outcome] += 1
                if outcome in {"deferred", "uncertain"}:
                    break
        return counters

    def _submit(self, claimed: Claimed) -> str:
        envelope = claimed.envelope
        if self.spool.retry_exhausted(claimed.path):
            if envelope is None:
                self.spool.quarantine_claimed(
                    claimed, "source_retry_exhausted", uncertain=False
                )
                return "quarantined"
            return self._uncertain(claimed, envelope, "remote_retry_exhausted")

        if envelope is None:
            try:
                document = self.fetcher.fetch(claimed.candidate.source_url)
                legal_entity_document = (
                    document
                    if claimed.candidate.legal_entity_source_url
                    == claimed.candidate.source_url
                    else self.fetcher.fetch(claimed.candidate.legal_entity_source_url)
                )
                envelope = build_verified_envelope(
                    claimed.candidate,
                    document,
                    legal_entity_document,
                )
                body = self.spool.persist_request(claimed, envelope)
            except FetchError as exc:
                if _is_transient_fetch(exc) and self.spool.record_retry(
                    claimed.path, f"source_{exc.code}"
                ):
                    return "deferred"
                self.spool.quarantine_claimed(
                    claimed, "source_evidence_rejected", uncertain=False
                )
                return "quarantined"
            except ValidationError:
                self.spool.quarantine_claimed(
                    claimed, "source_evidence_rejected", uncertain=False
                )
                return "quarantined"
        else:
            validate_intake_envelope(envelope)
            body = canonical_json_bytes(envelope)

        receipt_id = envelope["receipt"]["receiptId"]
        digests = self.spool.identity_digests(claimed.candidate, self.identity_secret)
        matches = self.spool.identity_matches(digests, receipt_id)
        if matches:
            self.spool.mark_duplicate(claimed, matches)
            return "duplicates"

        # Persist a PII-free pending tombstone before the first operation whose
        # remote commit can become ambiguous.
        self.spool.bind_or_check_identities(
            digests, receipt_id, claimed.path.stem, "pending"
        )
        request = self.signer(
            endpoint=self.endpoint,
            body=body,
            secret=self.transport_secret,
            receipt_id=receipt_id,
        )
        try:
            response = self.transport(self.endpoint, request, self.timeout_seconds)
        except ValidationError as exc:
            self.spool.open_circuit(
                "transport_protocol_error", sha256_hex(str(exc).encode("utf-8"))
            )
            return "deferred"
        except (
            OSError,
            TimeoutError,
            ssl.SSLError,
            urllib.error.URLError,
            http.client.HTTPException,
        ):
            if self.spool.record_retry(claimed.path, "submit_network"):
                return "deferred"
            return self._uncertain(claimed, envelope, "submit_network_uncertain")

        if 200 <= response.status < 300:
            try:
                result = validate_success_response(response.body, receipt_id)
            except ValidationError as exc:
                self.spool.open_circuit(
                    "invalid_2xx", sha256_hex(str(exc).encode("utf-8"))
                )
                return "deferred"
            self.spool.mark_processed(claimed, envelope, result, digests)
            return "processed"

        if response.status in TRANSIENT_HTTP or response.status >= 500:
            reason = f"remote_http_{response.status}"
            if self.spool.record_retry(claimed.path, reason):
                return "deferred"
            return self._uncertain(claimed, envelope, f"{reason}_uncertain")

        if response.status == 409:
            return self._uncertain(claimed, envelope, "remote_conflict")

        # Authentication, disabled DB gates, route drift, unsupported media and
        # all other non-transient failures are global operator problems. Keep
        # the exact request and stop every later candidate.
        self.spool.open_circuit(
            f"remote_http_{response.status}", sha256_hex(response.body)
        )
        return "deferred"

    def _uncertain(
        self, claimed: Claimed, envelope: dict[str, Any], reason: str
    ) -> str:
        receipt_id = envelope["receipt"]["receiptId"]
        digests = self.spool.identity_digests(claimed.candidate, self.identity_secret)
        self.spool.bind_or_check_identities(
            digests, receipt_id, claimed.path.stem, "uncertain"
        )
        self.spool.quarantine_claimed(claimed, reason, uncertain=True)
        return "uncertain"
