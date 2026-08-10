from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from scripts.freio_telegram_handoff.dispatcher import (
    CLAIM_URL,
    FINALIZE_URL,
    MESSAGE_HEADING,
    AmbiguousTransport,
    Credentials,
    Dispatcher,
    Finalization,
    FreioClient,
    HttpResponse,
    NetworkUnavailable,
    RequestTimedOut,
    StateStore,
    TelegramClient,
    UrllibTransport,
    load_credentials,
    parse_claim_response,
)


NOTIFICATION_ID = "00000000-0000-4000-8000-000000000101"
CLAIM_ID = "00000000-0000-4000-8000-000000000102"
EVENT_ID = "00000000-0000-4000-8000-000000000103"
OTHER_EVENT_ID = "00000000-0000-4000-8000-000000000104"
INBOX_LINK = f"https://outreach.freio.cz/?section=inbox&event={EVENT_ID}"
CONVERSATION_LINK = (
    f"https://outreach.freio.cz/?section=conversations&conversation={EVENT_ID}"
)


def response(
    status: int,
    value: object,
    *,
    headers: dict[str, str] | None = None,
    truncated: bool = False,
) -> HttpResponse:
    return HttpResponse(
        status=status,
        headers=headers or {"content-type": "application/json"},
        body=json.dumps(value, separators=(",", ":")).encode("utf-8"),
        truncated=truncated,
    )


def claim_response(
    *,
    kind: str = "reply_review",
    deep_link: str = CONVERSATION_LINK,
    event_id: str = EVENT_ID,
    action_summary: object | None = None,
) -> HttpResponse:
    summary = (
        {
            "intent": "pricing",
            "offerCount": 2,
            "latestOffer": {"totalCzk": 240_000, "currency": "CZK"},
        }
        if action_summary is None
        else action_summary
    )
    return response(
        200,
        {
            "notification": {
                "id": NOTIFICATION_ID,
                "claimId": CLAIM_ID,
                "eventId": event_id,
                "kind": kind,
                "actionSummary": summary,
                "deepLink": deep_link,
            }
        },
    )


def finalize_success() -> HttpResponse:
    return response(200, {"success": True})


def telegram_success(message_id: int = 501) -> HttpResponse:
    return response(200, {"ok": True, "result": {"message_id": message_id}})


class RecordingTransport:
    def __init__(self, effects: list[object]) -> None:
        self.effects = list(effects)
        self.calls: list[tuple[str, str, dict[str, str], bytes, int]] = []

    def request(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body, timeout))
        effect = self.effects.pop(0)
        if isinstance(effect, BaseException):
            raise effect
        return effect


class FakeFreio:
    def __init__(
        self,
        claim_effect: object,
        finalize_effects: list[object] | None = None,
    ) -> None:
        self.claim_effect = claim_effect
        self.finalize_effects = list(finalize_effects or [finalize_success()])
        self.claim_calls = 0
        self.finalizations: list[Finalization] = []

    def claim(self) -> HttpResponse:
        self.claim_calls += 1
        if isinstance(self.claim_effect, BaseException):
            raise self.claim_effect
        return self.claim_effect

    def finalize(self, finalization: Finalization) -> HttpResponse:
        self.finalizations.append(finalization)
        effect = self.finalize_effects.pop(0)
        if isinstance(effect, BaseException):
            raise effect
        return effect


class FakeTelegram:
    def __init__(self, effect: object) -> None:
        self.effect = effect
        self.links: list[str] = []

    def send(self, notification) -> HttpResponse:
        self.links.append(notification.deep_link)
        if isinstance(self.effect, BaseException):
            raise self.effect
        return self.effect


class DispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self.temporary.name) / "state")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_dispatcher(
        self,
        claim_effect: object,
        telegram_effect: object,
        finalize_effects: list[object] | None = None,
    ) -> tuple[int, FakeFreio, FakeTelegram, str]:
        freio = FakeFreio(claim_effect, finalize_effects)
        telegram = FakeTelegram(telegram_effect)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = Dispatcher(self.state, freio, telegram).run()
        return result, freio, telegram, output.getvalue()

    def test_idle_claim_does_not_touch_telegram_or_finalize(self) -> None:
        result, freio, telegram, output = self.run_dispatcher(
            response(200, {"notification": None}),
            telegram_success(),
        )

        self.assertEqual(result, 0)
        self.assertEqual(telegram.links, [])
        self.assertEqual(freio.finalizations, [])
        self.assertIn('"event":"idle"', output)

    def test_success_finalizes_provider_message_id_without_logging_ids(self) -> None:
        result, freio, telegram, output = self.run_dispatcher(
            claim_response(),
            telegram_success(91234),
        )

        self.assertEqual(result, 0)
        self.assertEqual(telegram.links, [CONVERSATION_LINK])
        self.assertEqual(len(freio.finalizations), 1)
        finalization = freio.finalizations[0]
        self.assertEqual(finalization.outcome, "sent")
        self.assertEqual(finalization.provider_message_id, "91234")
        self.assertFalse(self.state.pending_path.exists())
        self.assertNotIn(NOTIFICATION_ID, output)
        self.assertNotIn(EVENT_ID, output)
        self.assertNotIn("outreach.freio.cz", output)

    def test_conversation_link_is_allowed_for_new_inquiry(self) -> None:
        result, freio, telegram, _ = self.run_dispatcher(
            claim_response(kind="new_inquiry", deep_link=CONVERSATION_LINK),
            telegram_success(),
        )

        self.assertEqual(result, 0)
        self.assertEqual(telegram.links, [CONVERSATION_LINK])
        self.assertEqual(freio.finalizations[0].outcome, "sent")

    def test_general_safe_kind_uses_only_the_conversation_contract(self) -> None:
        result, freio, _, _ = self.run_dispatcher(
            claim_response(kind="pricing_approval", deep_link=CONVERSATION_LINK),
            telegram_success(),
        )
        self.assertEqual(result, 0)
        self.assertEqual(freio.finalizations[0].outcome, "sent")

    def test_timeout_is_uncertain_and_is_never_sent_again(self) -> None:
        result, freio, telegram, _ = self.run_dispatcher(
            claim_response(),
            RequestTimedOut(),
        )

        self.assertEqual(result, 0)
        self.assertEqual(telegram.links, [CONVERSATION_LINK])
        self.assertEqual(freio.finalizations[0].outcome, "uncertain")
        self.assertEqual(
            freio.finalizations[0].error_code,
            "telegram_timeout_ambiguous",
        )
        self.assertFalse(self.state.pending_path.exists())

    def test_network_failure_is_a_delayed_retry(self) -> None:
        result, freio, _, _ = self.run_dispatcher(
            claim_response(),
            NetworkUnavailable(),
        )

        self.assertEqual(result, 0)
        finalization = freio.finalizations[0]
        self.assertEqual(finalization.outcome, "retry")
        self.assertEqual(finalization.retry_after_seconds, 120)

    def test_rate_limit_respects_provider_retry_after(self) -> None:
        result, freio, _, _ = self.run_dispatcher(
            claim_response(),
            response(
                429,
                {"ok": False, "parameters": {"retry_after": 731}},
            ),
        )

        self.assertEqual(result, 0)
        finalization = freio.finalizations[0]
        self.assertEqual(finalization.outcome, "retry")
        self.assertEqual(finalization.retry_after_seconds, 731)
        self.assertEqual(finalization.error_code, "telegram_http_429")

    def test_server_error_uses_bounded_backoff(self) -> None:
        result, freio, _, _ = self.run_dispatcher(
            claim_response(),
            response(503, {"ok": False}),
        )

        self.assertEqual(result, 0)
        self.assertEqual(freio.finalizations[0].outcome, "retry")
        self.assertEqual(freio.finalizations[0].retry_after_seconds, 300)

    def test_telegram_auth_failure_is_dead_and_opens_circuit(self) -> None:
        result, freio, _, _ = self.run_dispatcher(
            claim_response(),
            response(401, {"ok": False}),
        )

        self.assertEqual(result, 78)
        self.assertEqual(freio.finalizations[0].outcome, "dead")
        self.assertTrue(self.state.circuit_path.exists())

        blocked_freio = FakeFreio(AssertionError("claim must stay blocked"))
        blocked_telegram = FakeTelegram(AssertionError("send must stay blocked"))
        with contextlib.redirect_stdout(io.StringIO()):
            second = Dispatcher(
                self.state,
                blocked_freio,
                blocked_telegram,
            ).run()
        self.assertEqual(second, 78)
        self.assertEqual(blocked_freio.claim_calls, 0)
        self.assertEqual(blocked_telegram.links, [])

    def test_invalid_deep_link_is_finalized_dead_without_opening_it(self) -> None:
        malicious = (
            "https://outreach.freio.cz/"
            f"?section=inbox&event={EVENT_ID}&email=owner%40example.cz"
        )
        result, freio, telegram, output = self.run_dispatcher(
            claim_response(deep_link=malicious),
            telegram_success(),
        )

        self.assertEqual(result, 78)
        self.assertEqual(telegram.links, [])
        self.assertEqual(freio.finalizations[0].outcome, "dead")
        self.assertEqual(
            freio.finalizations[0].error_code,
            "freio_invalid_claim_payload",
        )
        self.assertNotIn("owner", output)
        self.assertTrue(self.state.circuit_path.exists())

    def test_reply_kind_cannot_be_redirected_to_a_single_inbox_event(self) -> None:
        result, freio, telegram, _ = self.run_dispatcher(
            claim_response(kind="reply_review", deep_link=INBOX_LINK),
            telegram_success(),
        )
        self.assertEqual(result, 78)
        self.assertEqual(telegram.links, [])
        self.assertEqual(freio.finalizations[0].outcome, "dead")

    def test_lost_finalize_response_replays_finalize_without_resending(self) -> None:
        first, first_freio, first_telegram, _ = self.run_dispatcher(
            claim_response(),
            telegram_success(700),
            [AmbiguousTransport()],
        )
        self.assertEqual(first, 1)
        self.assertEqual(first_telegram.links, [CONVERSATION_LINK])
        self.assertTrue(self.state.pending_path.exists())

        recovery_freio = FakeFreio(
            AssertionError("claim must wait for finalize recovery"),
            [finalize_success()],
        )
        recovery_telegram = FakeTelegram(
            AssertionError("Telegram must never be called during recovery")
        )
        with contextlib.redirect_stdout(io.StringIO()):
            recovered = Dispatcher(
                self.state,
                recovery_freio,
                recovery_telegram,
            ).run()

        self.assertEqual(recovered, 0)
        self.assertEqual(recovery_freio.claim_calls, 0)
        self.assertEqual(recovery_telegram.links, [])
        self.assertEqual(len(recovery_freio.finalizations), 1)
        self.assertEqual(
            recovery_freio.finalizations[0].provider_message_id,
            first_freio.finalizations[0].provider_message_id,
        )
        self.assertFalse(self.state.pending_path.exists())

    def test_permanent_finalize_failure_stops_until_manual_recovery(self) -> None:
        first, first_freio, first_telegram, _ = self.run_dispatcher(
            claim_response(),
            telegram_success(701),
            [response(401, {"success": False})],
        )
        self.assertEqual(first, 78)
        self.assertEqual(first_telegram.links, [CONVERSATION_LINK])
        self.assertEqual(len(first_freio.finalizations), 1)
        self.assertTrue(self.state.pending_path.exists())
        self.assertTrue(self.state.circuit_path.exists())

        blocked_freio = FakeFreio(AssertionError("claim must stay blocked"))
        blocked_telegram = FakeTelegram(AssertionError("send must stay blocked"))
        with contextlib.redirect_stdout(io.StringIO()):
            blocked = Dispatcher(
                self.state,
                blocked_freio,
                blocked_telegram,
            ).run()
        self.assertEqual(blocked, 78)
        self.assertEqual(blocked_freio.claim_calls, 0)
        self.assertEqual(blocked_freio.finalizations, [])
        self.assertEqual(blocked_telegram.links, [])

        self.state.circuit_path.unlink()
        recovery_freio = FakeFreio(
            AssertionError("claim must wait for finalize recovery"),
            [finalize_success()],
        )
        recovery_telegram = FakeTelegram(
            AssertionError("Telegram must never be called during recovery")
        )
        with contextlib.redirect_stdout(io.StringIO()):
            recovered = Dispatcher(
                self.state,
                recovery_freio,
                recovery_telegram,
            ).run()
        self.assertEqual(recovered, 0)
        self.assertEqual(recovery_freio.claim_calls, 0)
        self.assertEqual(recovery_telegram.links, [])
        self.assertFalse(self.state.pending_path.exists())

    def test_process_crash_around_send_recovers_uncertain_without_resend(
        self,
    ) -> None:
        first_freio = FakeFreio(claim_response())
        crashing_telegram = FakeTelegram(KeyboardInterrupt())

        with self.assertRaises(KeyboardInterrupt):
            with contextlib.redirect_stdout(io.StringIO()):
                Dispatcher(
                    self.state,
                    first_freio,
                    crashing_telegram,
                ).run()

        self.assertEqual(crashing_telegram.links, [CONVERSATION_LINK])
        self.assertEqual(first_freio.finalizations, [])
        self.assertTrue(self.state.pending_path.exists())

        recovery_freio = FakeFreio(
            AssertionError("claim must wait for finalize recovery"),
            [finalize_success()],
        )
        recovery_telegram = FakeTelegram(
            AssertionError("Telegram must never be called during recovery")
        )
        with contextlib.redirect_stdout(io.StringIO()):
            recovered = Dispatcher(
                self.state,
                recovery_freio,
                recovery_telegram,
            ).run()

        self.assertEqual(recovered, 78)
        self.assertEqual(recovery_freio.claim_calls, 0)
        self.assertEqual(recovery_telegram.links, [])
        self.assertEqual(recovery_freio.finalizations[0].outcome, "uncertain")
        self.assertEqual(
            recovery_freio.finalizations[0].error_code,
            "telegram_attempt_interrupted",
        )
        self.assertTrue(self.state.circuit_path.exists())
        self.assertFalse(self.state.pending_path.exists())


class ProtocolAndCredentialTests(unittest.TestCase):
    def test_only_definitely_pre_send_os_errors_are_retryable(self) -> None:
        self.assertTrue(
            UrllibTransport._definitely_unavailable(
                ConnectionRefusedError(errno.ECONNREFUSED, "refused")
            )
        )
        self.assertFalse(
            UrllibTransport._definitely_unavailable(
                ConnectionAbortedError(errno.ECONNABORTED, "aborted")
            )
        )

    def test_claim_and_finalize_are_exact_post_contracts(self) -> None:
        transport = RecordingTransport(
            [response(200, {"notification": None}), finalize_success()]
        )
        client = FreioClient(transport, "machine-secret-value-for-tests-123456789")

        client.claim()
        client.finalize(
            Finalization(
                notification_id=NOTIFICATION_ID,
                claim_id=CLAIM_ID,
                outcome="retry",
                error_code="telegram_http_503",
                retry_after_seconds=300,
            )
        )

        claim_call, finalize_call = transport.calls
        self.assertEqual(claim_call[0:2], ("POST", CLAIM_URL))
        self.assertEqual(claim_call[3], b"")
        self.assertEqual(
            claim_call[2]["Authorization"],
            "Bearer machine-secret-value-for-tests-123456789",
        )
        self.assertEqual(finalize_call[0:2], ("POST", FINALIZE_URL))
        self.assertEqual(
            json.loads(finalize_call[3]),
            {
                "notificationId": NOTIFICATION_ID,
                "claimId": CLAIM_ID,
                "outcome": "retry",
                "errorCode": "telegram_http_503",
                "retryAfterSeconds": 300,
            },
        )

    def test_telegram_body_contains_only_structured_copy_and_one_allowlisted_url(
        self,
    ) -> None:
        transport = RecordingTransport([telegram_success()])
        client = TelegramClient(
            transport,
            "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijk",
            "-123456789",
        )
        notification = parse_claim_response(
            claim_response(kind="pricing_approval", deep_link=CONVERSATION_LINK)
        )
        self.assertIsNotNone(notification)

        client.send(notification)

        call = transport.calls[0]
        form = urllib.parse.parse_qs(call[3].decode("ascii"), strict_parsing=True)
        self.assertEqual(
            form["text"],
            [
                f"{MESSAGE_HEADING}\n"
                "Akce: Schválit nebo upravit cenovou nabídku.\n"
                "Potřeba: Klient potřebuje vyřešit cenu.\n"
                "Kontext: nabídky: 2 | poslední cena: 240 000 Kč\n"
                f"Celé vlákno: {CONVERSATION_LINK}"
            ],
        )
        self.assertEqual(form["protect_content"], ["true"])
        self.assertEqual(form["disable_web_page_preview"], ["true"])
        self.assertEqual(form["text"][0].count("https://"), 1)
        self.assertNotIn("email", form["text"][0].lower())

    def test_demo_intent_is_mapped_without_model_or_mail_text(self) -> None:
        transport = RecordingTransport([telegram_success()])
        client = TelegramClient(
            transport,
            "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijk",
            "-123456789",
        )
        notification = parse_claim_response(
            claim_response(
                action_summary={
                    "intent": "demo",
                    "offerCount": 0,
                    "latestOffer": None,
                }
            )
        )
        self.assertIsNotNone(notification)

        client.send(notification)

        form = urllib.parse.parse_qs(
            transport.calls[0][3].decode("ascii"),
            strict_parsing=True,
        )
        self.assertIn(
            "Potřeba: Klient chce call, demo nebo řeší implementaci.",
            form["text"][0],
        )
        self.assertNotIn("summary", form["text"][0].lower())

    def test_strict_parser_rejects_extra_or_mismatched_parameters(self) -> None:
        valid = parse_claim_response(claim_response())
        self.assertIsNotNone(valid)

        with self.assertRaises(Exception):
            parse_claim_response(
                claim_response(
                    event_id=OTHER_EVENT_ID,
                    deep_link=CONVERSATION_LINK,
                )
            )
        for bad_summary in (
            {},
            {
                "intent": "pricing",
                "offerCount": 2,
                "latestOffer": {"totalCzk": 240_000, "currency": "CZK"},
                "rawSummary": "private mail content",
            },
            {
                "intent": "pricing",
                "offerCount": 10_001,
                "latestOffer": {"totalCzk": 240_000, "currency": "CZK"},
            },
            {
                "intent": "pricing",
                "offerCount": True,
                "latestOffer": {"totalCzk": 240_000, "currency": "CZK"},
            },
            {
                "intent": "pricing",
                "offerCount": 0,
                "latestOffer": {"totalCzk": 240_000, "currency": "CZK"},
            },
        ):
            with self.subTest(summary=bad_summary):
                with self.assertRaises(Exception):
                    parse_claim_response(claim_response(action_summary=bad_summary))
        with self.assertRaises(Exception):
            parse_claim_response(
                claim_response(kind="raw_llm_summary", deep_link=CONVERSATION_LINK)
            )
        with self.assertRaises(Exception):
            parse_claim_response(
                claim_response(
                    kind="new_inquiry",
                    deep_link=CONVERSATION_LINK + "&extra=1",
                )
            )

        duplicate_key_body = b'{"notification":null,"notification":null}'
        with self.assertRaises(Exception):
            parse_claim_response(
                HttpResponse(
                    status=200,
                    headers={"content-type": "application/json"},
                    body=duplicate_key_body,
                )
            )

    def test_credentials_are_read_from_files_and_never_need_secret_env_values(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "telegram-token").write_text(
                "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijk\n",
                encoding="ascii",
            )
            (root / "telegram-chat-id").write_text("-123456789\n", encoding="ascii")
            (root / "freio-machine-secret").write_text(
                "m" * 64 + "\n", encoding="ascii"
            )
            for credential in root.iterdir():
                credential.chmod(0o400)

            with mock.patch.dict(
                os.environ,
                {"CREDENTIALS_DIRECTORY": directory},
                clear=True,
            ):
                loaded = load_credentials()

        self.assertEqual(
            loaded,
            Credentials(
                "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijk",
                "-123456789",
                "m" * 64,
            ),
        )


if __name__ == "__main__":
    unittest.main()
