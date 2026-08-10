from __future__ import annotations

import copy
import unittest
from datetime import timedelta

from helpers import NOW_DT, batch_envelope, research_candidate, research_document

from freio_prospecting.common import ValidationError
from freio_prospecting.schema import (
    MAX_CANDIDATES,
    make_idempotency_key,
    build_signed_intake_request,
    parse_research_document,
    validate_signed_intake_request,
)


class ResearchSchemaTests(unittest.TestCase):
    def test_text_lengths_match_javascript_utf16_and_surrogates_fail_closed(
        self,
    ) -> None:
        accepted = research_candidate(name="🚀" * 60)
        self.assertEqual(
            parse_research_document(research_document(accepted))[0].name,
            "🚀" * 60,
        )
        with self.assertRaises(ValidationError):
            parse_research_document(
                research_document(research_candidate(name="🚀" * 61))
            )
        with self.assertRaisesRegex(ValidationError, "Unicode scalar"):
            parse_research_document(
                research_document(research_candidate(name="bad\ud800text"))
            )

    def test_parses_strict_valid_candidate(self) -> None:
        parsed = parse_research_document(research_document())
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].claimed_email, "hello@creator.example.cz")
        self.assertEqual(parsed[0].source_url, "https://creator.example.cz/contact")

    def test_rejects_unknown_field_at_each_level(self) -> None:
        documents = []
        top = research_document()
        top["unexpected"] = True
        documents.append(top)
        candidate = research_document()
        candidate["candidates"][0]["unexpected"] = True
        documents.append(candidate)
        context = research_document()
        context["candidates"][0]["researchContext"]["unexpected"] = True
        documents.append(context)
        for document in documents:
            with self.subTest(document=document), self.assertRaises(ValidationError):
                parse_research_document(document)

    def test_unexpected_key_value_is_never_echoed_in_validation_error(self) -> None:
        attacker_key = "leak@example.cz\nFORGED=1\x1b[31m"
        document = research_document()
        document[attacker_key] = True
        with self.assertRaises(ValidationError) as raised:
            parse_research_document(document)
        self.assertEqual(str(raised.exception), "document has 1 unexpected field")
        self.assertNotIn("leak@example.cz", str(raised.exception))
        self.assertNotIn("\n", str(raised.exception))
        self.assertNotIn("\x1b", str(raised.exception))

    def test_rejects_more_than_wave_size(self) -> None:
        document = research_document(
            *(
                research_candidate(name=f"Creator {index}")
                for index in range(MAX_CANDIDATES + 1)
            )
        )
        with self.assertRaises(ValidationError):
            parse_research_document(document)

    def test_requires_handle_and_channel_together(self) -> None:
        for value in (
            research_candidate(socialChannel=None),
            research_candidate(handle=None),
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                parse_research_document(research_document(value))

    def test_rejects_duplicate_or_unknown_risk_flags(self) -> None:
        for flags in (
            ["contact_unconfirmed", "contact_unconfirmed"],
            ["made_up_flag"],
        ):
            value = research_candidate()
            value["researchContext"]["riskFlags"] = flags
            with self.subTest(flags=flags), self.assertRaises(ValidationError):
                parse_research_document(research_document(value))

    def test_rejects_guessed_or_malformed_contact(self) -> None:
        with self.assertRaises(ValidationError):
            parse_research_document(
                research_document(research_candidate(claimedEmail="first@localhost"))
            )

    def test_idempotency_key_is_stable_for_channel_handle(self) -> None:
        first = parse_research_document(research_document())[0]
        moved = parse_research_document(
            research_document(
                research_candidate(sourceUrl="https://other.example.cz/new")
            )
        )[0]
        self.assertEqual(
            make_idempotency_key(first, first.source_url),
            make_idempotency_key(moved, moved.source_url),
        )

    def test_handle_contract_rejects_urls_whitespace_and_multiple_at_signs(
        self,
    ) -> None:
        for handle in (
            "https://instagram.com/creator",
            "two words",
            "@@creator",
            "creator/name",
            "@a",
            "a" * 100,
        ):
            with self.subTest(handle=handle), self.assertRaises(ValidationError):
                parse_research_document(
                    research_document(research_candidate(handle=handle))
                )

    def test_handle_identity_strips_one_at_and_is_case_insensitive(self) -> None:
        with_at = parse_research_document(
            research_document(research_candidate(handle="@Creator.Name"))
        )[0]
        without_at = parse_research_document(
            research_document(research_candidate(handle="creator.name"))
        )[0]
        self.assertEqual(
            make_idempotency_key(with_at, with_at.source_url),
            make_idempotency_key(without_at, without_at.source_url),
        )


class BatchSchemaTests(unittest.TestCase):
    def test_validates_receipt_and_evidence_alignment(self) -> None:
        envelope = validate_signed_intake_request(batch_envelope(), now=NOW_DT)
        self.assertEqual(
            envelope["receipt"]["receiptId"],
            f"sha256:{envelope['receipt']['signedItemsSha256']}",
        )

    def test_detects_payload_tampering(self) -> None:
        envelope = batch_envelope()
        envelope["items"][0]["prospect"]["name"] = "Tampered"
        with self.assertRaisesRegex(ValidationError, "receipt"):
            validate_signed_intake_request(envelope, now=NOW_DT)

    def test_detects_evidence_tampering(self) -> None:
        envelope = batch_envelope()
        envelope["items"][0]["fetchReceipt"]["bodySha256"] = "b" * 64
        with self.assertRaisesRegex(ValidationError, "evidenceSha256"):
            validate_signed_intake_request(envelope, now=NOW_DT)

    def test_detects_item_evidence_identity_mismatch(self) -> None:
        envelope = batch_envelope()
        envelope["items"][0]["fetchReceipt"]["finalUrl"] = "https://other.example.cz/"
        with self.assertRaisesRegex(ValidationError, "email source"):
            validate_signed_intake_request(envelope, now=NOW_DT)

    def test_rejects_stale_or_future_evidence(self) -> None:
        stale = batch_envelope(created_at="2026-07-01T00:00:00Z")
        future = batch_envelope(created_at="2026-08-09T10:06:00Z")
        for value in (stale, future):
            with (
                self.subTest(created=value["items"][0]["fetchReceipt"]["fetchedAt"]),
                self.assertRaises(ValidationError),
            ):
                validate_signed_intake_request(
                    value, now=NOW_DT, maximum_age=timedelta(days=30)
                )

    def test_rejects_tampered_match_method_even_if_field_shape_is_valid(self) -> None:
        envelope = batch_envelope()
        envelope["items"][0]["fetchReceipt"]["matchMethod"] = "visible_text_exact"
        with self.assertRaisesRegex(ValidationError, "evidenceSha256"):
            validate_signed_intake_request(envelope, now=NOW_DT)

    def test_rejects_unknown_batch_field(self) -> None:
        envelope = copy.deepcopy(batch_envelope())
        envelope["authorization"] = "do-not-accept"
        with self.assertRaises(ValidationError):
            validate_signed_intake_request(envelope, now=NOW_DT)

    def test_rejects_any_tampered_top_level_receipt_hash(self) -> None:
        for field in (
            "intakePayloadSha256",
            "evidenceSha256",
            "signedItemsSha256",
            "receiptId",
        ):
            envelope = batch_envelope()
            envelope["receipt"][field] = (
                "sha256:" + "f" * 64 if field == "receiptId" else "f" * 64
            )
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate_signed_intake_request(envelope, now=NOW_DT)

    def test_signed_wire_body_is_bounded_to_96_kib(self) -> None:
        template = batch_envelope()["items"][0]
        items = []
        for index in range(30):
            item = copy.deepcopy(template)
            item["idempotencyKey"] = f"freio-prospect-v1:{index:040x}"
            item["prospect"]["researchContext"]["personalizationNote"] = "x" * 500
            item["fetchReceipt"]["requestedUrl"] = (
                "https://creator.example.cz/" + "x" * 1900 + str(index)
            )
            without_hash = {
                key: value
                for key, value in item["fetchReceipt"].items()
                if key != "evidenceSha256"
            }
            from freio_prospecting.common import canonical_json_bytes, sha256_hex

            item["fetchReceipt"]["evidenceSha256"] = sha256_hex(
                canonical_json_bytes(without_hash)
            )
            items.append(item)
        with self.assertRaisesRegex(ValidationError, "96 KiB"):
            build_signed_intake_request(
                items=items,
                research_manifest_sha256="a" * 64,
            )


if __name__ == "__main__":
    unittest.main()
