from __future__ import annotations

import copy
import unittest

from helpers import (
    candidate,
    fetched_document,
    research_document,
    success_response,
)

from freio_b2b_discovery.model import (
    FETCHER_VERSION,
    build_verified_envelope,
    parse_research_document,
    validate_intake_envelope,
    validate_success_response,
)
from freio_prospecting.common import ValidationError


class ModelContractTests(unittest.TestCase):
    def test_strict_research_schema_accepts_commercial_tutoring(self) -> None:
        parsed = parse_research_document(research_document())
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].lead_type, "tutoring")
        self.assertEqual(parsed[0].email, "kontakt@doucovani.cz")
        self.assertEqual(parsed[0].name, "Doučování Příklad s.r.o.")
        self.assertEqual(parsed[0].legal_form, "sro")

    def test_rejects_school_red_izo_unknown_fields_and_noncanonical_urls(self) -> None:
        invalid_documents = []
        for lead_change in (
            {"leadType": "school"},
            {"redIzo": "600012345"},
            {"name": "Jan Novák OSVČ s.r.o.", "legalForm": "sro"},
            {"name": "Pouhá značka", "legalForm": "sro"},
            {"legalForm": "osvc"},
            {"ico": "12345678"},
            {"website": "https://DOUCOVANI.cz/"},
            {"website": "https://www.doucovani.cz/sluzby"},
        ):
            value = research_document()
            lead = value["candidates"][0]["lead"]  # type: ignore[index]
            lead.update(lead_change)  # type: ignore[union-attr]
            invalid_documents.append(value)
        unknown = research_document(authorization="yes")
        invalid_documents.append(unknown)
        for value in invalid_documents:
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    parse_research_document(value)

    def test_requires_source_on_same_official_host_and_exact_email(self) -> None:
        for field in ("sourceUrl", "legalEntitySourceUrl"):
            with self.subTest(field=field):
                value = research_document(**{field: "https://directory.cz/company/1"})
                with self.assertRaises(ValidationError):
                    parse_research_document(value)
        with self.assertRaises(ValidationError):
            build_verified_envelope(
                candidate(),
                fetched_document(email="other@example.cz"),
                fetched_document(),
            )
        with self.assertRaises(ValidationError):
            build_verified_envelope(
                candidate(),
                fetched_document(final_url="https://redirected.example.cz/contact"),
                fetched_document(),
            )

    def test_accepts_only_allowlisted_generic_role_inboxes(self) -> None:
        for local_part in (
            "info",
            "obchod",
            "sales",
            "contact",
            "office",
            "podpora",
        ):
            with self.subTest(local_part=local_part):
                value = research_document(
                    contact={"email": f"{local_part}@doucovani.cz"}
                )
                self.assertEqual(
                    parse_research_document(value)[0].email,
                    f"{local_part}@doucovani.cz",
                )
        for personal in (
            "jana.novakova@doucovani.cz",
            "reditel@doucovani.cz",
            "owner@doucovani.cz",
            "info+jane@doucovani.cz",
        ):
            with self.subTest(personal=personal):
                with self.assertRaises(ValidationError):
                    parse_research_document(
                        research_document(contact={"email": personal})
                    )
        for person_field in (
            {"email": "info@doucovani.cz", "name": "Jana Nováková"},
            {"email": "info@doucovani.cz", "role": "director"},
        ):
            with self.assertRaises(ValidationError):
                parse_research_document(research_document(contact=person_field))

    def test_requires_official_exact_legal_name_form_and_ico_evidence(self) -> None:
        current = candidate()
        for document in (
            fetched_document(legal_name="Jiná firma s.r.o."),
            fetched_document(legal_name="Doučování Příklad", ico="12345679"),
            fetched_document(ico="87654321"),
            fetched_document(final_url="https://registry.example.cz/company"),
        ):
            with self.subTest(document=document):
                with self.assertRaises(ValidationError):
                    build_verified_envelope(current, fetched_document(), document)

    def test_builds_exact_receiver_envelope_without_authorization_or_send_state(
        self,
    ) -> None:
        envelope = build_verified_envelope(
            candidate(), fetched_document(), fetched_document()
        )
        validate_intake_envelope(envelope)
        self.assertEqual(envelope["schemaVersion"], "1")
        self.assertEqual(envelope["items"][0]["lead"]["leadType"], "tutoring")
        self.assertNotIn("redIzo", envelope["items"][0]["lead"])
        self.assertEqual(
            envelope["items"][0]["contact"], {"email": "kontakt@doucovani.cz"}
        )
        self.assertEqual(envelope["items"][0]["lead"]["legalForm"], "sro")
        self.assertEqual(
            envelope["items"][0]["evidence"]["legalEntityMatchMethod"],
            "official_page_exact_name_form_ico",
        )
        self.assertEqual(
            envelope["items"][0]["evidence"]["fetcherVersion"], FETCHER_VERSION
        )
        serialized = repr(envelope).lower()
        self.assertNotIn("authorization", serialized)
        self.assertNotIn("outreach", serialized)
        self.assertNotIn("consent", serialized)
        self.assertNotIn("director", serialized)
        self.assertNotIn("owner", serialized)

    def test_tampered_receipt_evidence_or_unknown_field_fails(self) -> None:
        envelope = build_verified_envelope(
            candidate(), fetched_document(), fetched_document()
        )
        variants = []
        changed = copy.deepcopy(envelope)
        changed["receipt"]["itemsSha256"] = "0" * 64
        variants.append(changed)
        changed = copy.deepcopy(envelope)
        changed["items"][0]["evidence"]["evidenceSha256"] = "0" * 64
        variants.append(changed)
        changed = copy.deepcopy(envelope)
        changed["items"][0]["contact"]["name"] = "Jana Nováková"
        variants.append(changed)
        changed = copy.deepcopy(envelope)
        changed["items"][0]["contact"]["role"] = "director"
        variants.append(changed)
        changed = copy.deepcopy(envelope)
        changed["items"][0]["evidence"]["legalEntityIcoSha256"] = "0" * 64
        variants.append(changed)
        changed = copy.deepcopy(envelope)
        changed["items"][0]["authorize"] = True
        variants.append(changed)
        for value in variants:
            with self.assertRaises(ValidationError):
                validate_intake_envelope(value)

    def test_validates_exact_non_pii_success_response(self) -> None:
        envelope = build_verified_envelope(
            candidate(), fetched_document(), fetched_document()
        )
        receipt_id = envelope["receipt"]["receiptId"]
        result = validate_success_response(success_response(receipt_id), receipt_id)
        self.assertEqual(result["receiptId"], receipt_id)
        with self.assertRaises(ValidationError):
            validate_success_response(
                success_response("sha256:" + "0" * 64), receipt_id
            )


if __name__ == "__main__":
    unittest.main()
