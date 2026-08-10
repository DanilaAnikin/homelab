from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from .contract import Classification, Task, parse_classification


# This classifier is intentionally conservative.  It recognizes only bounded,
# high-signal cases that the server can answer with audited templates.  Every
# unknown, sensitive or negotiation-like message is handed to the owner.


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return " ".join(
        "".join(
            character
            for character in normalized
            if not unicodedata.combining(character)
        )
        .casefold()
        .split()
    )


def _contains(text: str, phrases: Iterable[str]) -> bool:
    return any(phrase in text for phrase in phrases)


def _classification(
    *,
    intent: str,
    confidence: float,
    summary: str,
    faq_topic: str | None = None,
    seat_count: int | None = None,
    subject_count: int | None = None,
    risk_tags: Iterable[str] = (),
) -> Classification:
    return parse_classification(
        {
            "intent": intent,
            "confidence": confidence,
            "faqTopic": faq_topic,
            "seatCount": seat_count,
            "subjectCount": subject_count,
            "summary": summary,
            "riskTags": list(dict.fromkeys(risk_tags)),
        }
    )


def _bounded_number(raw: str, maximum: int) -> int | None:
    try:
        value = int(raw.replace(" ", ""))
    except ValueError:
        return None
    return value if 1 <= value <= maximum else None


def _extract_seat_count(text: str) -> int | None:
    unit = (
        r"(?:student(?:u|y|um)?|zaku|zak(?:u|y|um)?|licenc(?:e|i)|"
        r"uzivatelu|uzivatele|ucitelu|ucitele|zamestnancu|zamestnance|"
        r"lidi|osob|mist)"
    )
    for pattern in (
        rf"\b([1-9][0-9 ]{{0,8}})\s*{unit}\b",
        rf"\b{unit}\s*(?:pro|je|:)?\s*([1-9][0-9 ]{{0,8}})\b",
    ):
        match = re.search(pattern, text)
        if match:
            return _bounded_number(match.group(1), 1_000_000)
    return None


def _extract_subject_count(text: str) -> int | None:
    match = re.search(
        r"\b([1-7])\s*(?:predmet(?:u|y|y)?|subject(?:s)?)\b",
        text,
    )
    if match:
        return int(match.group(1))

    subject_groups = (
        ("matematika", "matematiku", "math"),
        ("osp", "obecne studijni predpoklady", "obecnych studijnich predpokladu"),
        ("zsv", "zaklady spolecenskych ved", "zakladu spolecenskych ved"),
        ("biologie", "biologii", "biology"),
        ("chemie", "chemii", "chemistry"),
        ("anglictina", "anglictinu", "anglicky jazyk", "english"),
        ("nemcina", "nemcinu", "nemecky jazyk", "german"),
    )
    count = sum(1 for aliases in subject_groups if _contains(text, aliases))
    return count or None


class LocalRuleClassifier:
    """PII-local classifier with a small, audited Czech/English rule set."""

    def classify(self, task: Task) -> Classification:
        text = _fold(f"{task.subject}\n{task.content}")
        seat_count = _extract_seat_count(text)
        subject_count = _extract_subject_count(text)

        if _contains(
            text,
            (
                "ignore previous instructions",
                "ignore all instructions",
                "system prompt",
                "developer message",
                "jailbreak",
                "prompt injection",
                "odhal systemovy prompt",
                "ignoruj predchozi instrukce",
                "zapomen na predchozi instrukce",
            ),
        ):
            return _classification(
                intent="unknown",
                confidence=1.0,
                summary="Zpráva obsahuje znaky manipulace automatizace; je nutná kontrola vlastníka.",
                risk_tags=("prompt_injection",),
            )

        if _contains(
            text,
            (
                "odhlasit",
                "odhlaste",
                "odstrante me",
                "nepreji si dalsi",
                "neposilejte dalsi",
                "unsubscribe",
                "remove me from",
                "stop emailing",
            ),
        ):
            return _classification(
                intent="unsubscribe",
                confidence=1.0,
                summary="Kontakt výslovně požádal o ukončení e-mailové komunikace.",
            )

        if _contains(
            text,
            (
                "nemame zajem",
                "nemam zajem",
                "bez zajmu",
                "nabidku nevyuzijeme",
                "not interested",
                "no interest",
                "we will pass",
            ),
        ):
            return _classification(
                intent="not_interested",
                confidence=1.0,
                summary="Kontakt jednoznačně odmítl nabídku.",
            )

        if _contains(
            text,
            (
                "automatic reply",
                "automaticka odpoved",
                "mimo kancelar",
                "out of office",
                "jsem na dovolene",
                "i am away",
                "delivery status notification",
                "undeliverable",
            ),
        ):
            return _classification(
                intent="automatic_reply",
                confidence=1.0,
                summary="Jde o automatickou odpověď nebo doručovací zprávu.",
            )

        if _contains(
            text,
            (
                "gdpr",
                "ochrana osobnich udaju",
                "zpracovani osobnich udaju",
                "data processing agreement",
                "dpa",
                "privacy policy",
                "bezpecnostni dotaznik",
                "security questionnaire",
                "penetracni test",
                "iso 27001",
            ),
        ):
            tags = ["personal_data_request"]
            if _contains(
                text,
                (
                    "bezpecnostni dotaznik",
                    "security questionnaire",
                    "penetracni test",
                    "iso 27001",
                ),
            ):
                tags.append("security_questionnaire")
            return _classification(
                intent="privacy_or_security",
                confidence=1.0,
                summary="Kontakt otevřel téma soukromí nebo bezpečnosti; je nutná kontrola vlastníka.",
                risk_tags=tags,
            )

        if _contains(
            text,
            (
                "smlouv",
                "obchodni podminky",
                "pravni oddeleni",
                "pravnik",
                "legal team",
                "contract",
                "liability",
                "odpovednost za skodu",
            ),
        ):
            return _classification(
                intent="contract_or_legal",
                confidence=1.0,
                summary="Kontakt požaduje smluvní nebo právní řešení; je nutná kontrola vlastníka.",
                risk_tags=("legal_language",),
            )

        if _contains(
            text,
            (
                "faktur",
                "objednavk",
                "purchase order",
                "procurement",
                "dodavatel",
                "splatnost",
                "billing",
                "dic",
                "ico",
            ),
        ):
            return _classification(
                intent="procurement_or_billing",
                confidence=1.0,
                summary="Kontakt řeší objednávku, dodavatele nebo fakturaci; je nutná akce vlastníka.",
            )

        if _contains(
            text,
            (
                "stiznost",
                "reklamac",
                "nespokojen",
                "problem s vasi sluzbou",
                "complaint",
                "unacceptable",
            ),
        ):
            return _classification(
                intent="complaint",
                confidence=1.0,
                summary="Zpráva je stížnost nebo reklamace; vyžaduje rychlou reakci vlastníka.",
                risk_tags=("hostile_or_sensitive",),
            )

        if _contains(
            text,
            (
                "sleva",
                "slevu",
                "levneji",
                "lepsi cenu",
                "discount",
                "lower price",
                "price reduction",
            ),
        ):
            return _classification(
                intent="discount_request",
                confidence=1.0,
                summary="Kontakt žádá změnu nebo snížení ceny; rozhodnutí patří vlastníkovi.",
                seat_count=seat_count,
                subject_count=subject_count,
            )

        if _contains(
            text,
            (
                "individualni podminky",
                "vlastni podminky",
                "specialni podminky",
                "custom terms",
                "individual terms",
                "sla",
                "exkluziv",
            ),
        ):
            return _classification(
                intent="custom_terms",
                confidence=1.0,
                summary="Kontakt požaduje individuální podmínky; rozhodnutí patří vlastníkovi.",
            )

        if _contains(
            text,
            (
                "schuzk",
                "videohovor",
                "telefonat",
                "zavolat",
                "zavolejte",
                "domluvit call",
                "book a call",
                "meeting",
                "calendar",
                "demo ukazk",
                "ukazat na demu",
            ),
        ):
            return _classification(
                intent="meeting_request",
                confidence=1.0,
                summary="Kontakt žádá schůzku, hovor nebo živou ukázku; vlastník má navázat.",
            )

        if _contains(
            text,
            (
                "nasadit",
                "nasazeni",
                "implementac",
                "onboarding",
                "integrac",
                "nastavit ucet",
                "zalozit ucet",
                "spustit pro nas",
                "rollout",
                "setup",
            ),
        ):
            return _classification(
                intent="implementation_request",
                confidence=1.0,
                summary="Kontakt chce zahájit nasazení nebo nastavení služby; vlastník má navázat.",
            )

        if _contains(text, ("scio", "scio test")):
            return _classification(
                intent="product_question",
                confidence=1.0,
                faq_topic="scio_relationship",
                summary="Kontakt se ptá na vztah Freio a Scio.",
            )

        if _contains(
            text,
            (
                "zkusebni",
                "na zkousku",
                "trial",
                "vyzkouset zdarma",
                "testovaci pristup",
            ),
        ):
            return _classification(
                intent="product_question",
                confidence=1.0,
                faq_topic="trial",
                summary="Kontakt se ptá na vyzkoušení služby.",
            )

        if _contains(
            text,
            ("ucebna", "trida", "skupina studentu", "classroom", "skupinova vyuka"),
        ) and _contains(text, ("?", "jak", "lze", "muz", "funguje", "podporuje")):
            return _classification(
                intent="product_question",
                confidence=0.99,
                faq_topic="classroom",
                summary="Kontakt se ptá na práci s třídou nebo skupinou.",
            )

        if _contains(
            text,
            (
                "jake predmety",
                "ktere predmety",
                "co doucujete",
                "subjects",
                "predmety podporuje",
            ),
        ):
            return _classification(
                intent="product_question",
                confidence=1.0,
                faq_topic="subjects",
                summary="Kontakt se ptá na podporované předměty.",
            )

        if _contains(
            text,
            (
                "cestina",
                "cestinu",
                "cesky jazyk",
                "czech language",
                "fyzika",
                "fyziku",
                "physics",
            ),
        ):
            return _classification(
                intent="product_question",
                confidence=1.0,
                faq_topic="other",
                summary="Kontakt se ptá na obor mimo bezpečně podporovaný katalog; má odpovědět vlastník.",
            )

        if _contains(
            text,
            (
                "kolik to stoji",
                "kolik stoji",
                "cenik",
                "cena pro",
                "nabidku pro",
                "cenova nabidka",
                "price",
                "pricing",
                "quote for",
                "how much",
            ),
        ):
            return _classification(
                intent="pricing_request",
                confidence=1.0,
                summary="Kontakt žádá standardní cenu; rozsah byl vytěžen jen z explicitních údajů.",
                seat_count=seat_count,
                subject_count=subject_count,
            )

        if _contains(
            text,
            (
                "mame zajem",
                "mam zajem",
                "zajima nas",
                "zajima me",
                "radi bychom",
                "rad bych",
                "chceme freio",
                "we are interested",
                "i am interested",
                "we would like",
            ),
        ):
            return _classification(
                intent="interested",
                confidence=0.99,
                summary="Kontakt projevil zájem; rozsah byl vytěžen jen z explicitních údajů.",
                seat_count=seat_count,
                subject_count=subject_count,
            )

        return _classification(
            intent="unknown",
            confidence=0.0,
            summary="Zpráva neodpovídá žádnému bezpečně podporovanému scénáři; má ji posoudit vlastník.",
        )
