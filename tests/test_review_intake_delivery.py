"""Creation-result delivery and strict IDs; all GLPI/Telegram I/O is fake."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from bot import texts
from bot.glpi.client import GlpiDocumentRejected, GlpiError, GlpiHTTPError
from bot.handlers import new_ticket
from bot.schedule import WorkSchedule
from bot.texts import intake as intake_texts
from tests import test_intake_integration
from tests.test_access import REQUESTER_ID, FakeBot, _cb, _dm
from tests.test_client import make_client

intake_env = test_intake_integration.intake_env


async def _ready(dp, bot):
    await dp.feed_update(bot, _dm(bot, 1, REQUESTER_ID, "/new"))
    await dp.feed_update(bot, _dm(bot, 2, REQUESTER_ID, "Офисный ПК недоступен"))
    await dp.feed_update(bot, _cb(bot, 3, REQUESTER_ID, "nt:urg:3"))


def _state(dp, bot):
    return FSMContext(
        storage=dp.storage,
        key=StorageKey(bot_id=bot.id, chat_id=REQUESTER_ID, user_id=REQUESTER_ID),
    )


@pytest.mark.parametrize(
    "body",
    [
        b'{"id":true}',
        b'{"id":1.5}',
        b'{"id":1.0}',
        b'{"id":1e309}',
        b'{"id":9223372036854775808}',
        b'{"id":"9223372036854775808"}',
        b'{"id":"77.0"}',
        b'[{"id":77},{"id":78}]',
    ],
)
async def test_invalid_success_id_never_targets_another_ticket_or_leaves_submission_stuck(
    intake_env, body
):
    dp, bot, client, repo, _, _ = intake_env
    actual_client = make_client()
    actual_client._request = AsyncMock(return_value=httpx.Response(201, content=body))
    client.create_ticket.side_effect = actual_client.create_ticket
    try:
        await _ready(dp, bot)
        await dp.feed_update(bot, _cb(bot, 4, REQUESTER_ID, "nt:send"))
        actual_client._request.assert_awaited_once()
        assert await _state(dp, bot).get_state() is None
        assert any(text == intake_texts.creation_uncertain() for _, text, _ in bot.sent)
        # true/1.5 are not valid ticket IDs, and must not be coerced to existing #1.
        assert await repo.get_ticket_intake(1) is None
        assert await repo.get_tracked_ticket(1) is None
        await dp.feed_update(bot, _cb(bot, 5, REQUESTER_ID, "nt:send"))
        actual_client._request.assert_awaited_once()
    finally:
        await actual_client.close()


@pytest.mark.parametrize("body", [b'{"id":77}', b'{"id":"77"}', b'[{"id":77}]'])
async def test_valid_glpi_id_response_shapes_remain_supported(body):
    client = make_client()
    client._request = AsyncMock(return_value=httpx.Response(201, content=body))
    try:
        assert await client.create_ticket(name="Issue", content="Description", urgency=3) == 77
        client._request.assert_awaited_once()
    finally:
        await client.close()


@pytest.mark.parametrize("delivered_before_failure", [False, True])
async def test_failed_success_confirmation_does_not_tell_requester_to_repeat_creation(
    intake_env, delivered_before_failure
):
    dp, bot, client, repo, _, _ = intake_env
    confirmed = texts.ticket_created(77, "https://glpi.invalid/front/ticket.form.php?id=77")
    original_send = bot._send
    failed = False
    confirmation_attempts = 0

    def temporarily_unavailable(chat_id, text, reply_markup=None):
        nonlocal failed, confirmation_attempts
        if text == confirmed:
            confirmation_attempts += 1
        if text == confirmed and not failed:
            failed = True
            if delivered_before_failure:
                original_send(chat_id, text, reply_markup)
            raise RuntimeError("Telegram confirmation temporarily unavailable")
        return original_send(chat_id, text, reply_markup)

    bot._send = temporarily_unavailable
    await _ready(dp, bot)
    await dp.feed_update(bot, _cb(bot, 4, REQUESTER_ID, "nt:send"))
    assert failed
    assert confirmation_attempts == 2
    client.create_ticket.assert_awaited_once()
    assert await repo.get_tracked_ticket(77) is not None
    assert await _state(dp, bot).get_state() is None
    # The POST succeeded: a generic "try again" encourages a duplicate request.
    delivered = [text for _, text, _ in bot.sent] + bot.toasts
    assert texts.GENERIC_ERROR not in delivered
    assert any("77" in text for text in delivered)
    await dp.feed_update(bot, _cb(bot, 5, REQUESTER_ID, "nt:send"))
    client.create_ticket.assert_awaited_once()


async def test_failed_confirmation_sends_can_fall_back_to_editing_the_original_prompt(intake_env):
    dp, bot, client, _, _, _ = intake_env
    confirmed = texts.ticket_created(77, "https://glpi.invalid/front/ticket.form.php?id=77")
    original_send = bot._send
    attempts = 0

    def no_confirmation_sends(chat_id, text, reply_markup=None):
        nonlocal attempts
        if text == confirmed:
            attempts += 1
            raise RuntimeError("Sending unavailable")
        return original_send(chat_id, text, reply_markup)

    bot._send = no_confirmation_sends
    await _ready(dp, bot)
    await dp.feed_update(bot, _cb(bot, 4, REQUESTER_ID, "nt:send"))
    assert attempts == 2
    assert any(text == confirmed for _, text, _ in bot.sent)
    assert texts.GENERIC_ERROR not in bot.toasts
    assert await _state(dp, bot).get_state() is None
    client.create_ticket.assert_awaited_once()


@pytest.mark.parametrize("entry", ["callback", "text"])
async def test_all_confirmation_delivery_failures_leave_tracking_without_retry_advice(
    intake_env, entry, caplog
):
    dp, _, client, repo, _, _ = intake_env
    confirmed = texts.ticket_created(77, "https://glpi.invalid/front/ticket.form.php?id=77")

    class UnavailableConfirmationBot(FakeBot):
        sends = 0
        edits = 0

        async def __call__(self, method, **kwargs):
            if getattr(method, "text", None) == confirmed:
                if type(method).__name__ == "SendMessage":
                    self.sends += 1
                elif type(method).__name__ == "EditMessageText":
                    self.edits += 1
                raise RuntimeError("private delivery error details")
            return await super().__call__(method, **kwargs)

    bot = UnavailableConfirmationBot()
    await _ready(dp, bot)
    update = (
        _cb(bot, 4, REQUESTER_ID, "nt:send")
        if entry == "callback"
        else _dm(bot, 4, REQUESTER_ID, "готово")
    )
    await dp.feed_update(bot, update)
    assert bot.sends == 2
    assert bot.edits == (1 if entry == "callback" else 0)
    assert await repo.get_tracked_ticket(77) is not None
    assert await _state(dp, bot).get_state() is None
    assert texts.GENERIC_ERROR not in [text for _, text, _ in bot.sent] + bot.toasts
    assert "private delivery error details" not in caplog.text
    assert "new_ticket_result_undelivered ticket_id=77" in caplog.text
    client.create_ticket.assert_awaited_once()


async def test_hanging_confirmation_sends_are_bounded_and_do_not_repeat_creation(
    intake_env, monkeypatch
):
    dp, _, client, _, _, _ = intake_env
    confirmed = texts.ticket_created(77, "https://glpi.invalid/front/ticket.form.php?id=77")
    monkeypatch.setattr(new_ticket, "_POST_CREATE_SEND_TIMEOUT", 0.01)

    class HangingConfirmationBot(FakeBot):
        sends = 0

        async def __call__(self, method, **kwargs):
            if (
                type(method).__name__ == "SendMessage"
                and getattr(method, "text", None) == confirmed
            ):
                self.sends += 1
                await asyncio.Event().wait()
            return await super().__call__(method, **kwargs)

    bot = HangingConfirmationBot()
    await _ready(dp, bot)
    async with asyncio.timeout(0.5):
        await dp.feed_update(bot, _cb(bot, 4, REQUESTER_ID, "nt:send"))
    assert bot.sends == 2
    assert any(text == confirmed for _, text, _ in bot.sent)
    client.create_ticket.assert_awaited_once()


async def test_confirmation_cancellation_propagates_after_clearing_the_draft(intake_env):
    dp, _, client, repo, _, _ = intake_env
    confirmed = texts.ticket_created(77, "https://glpi.invalid/front/ticket.form.php?id=77")

    class CancelledConfirmationBot(FakeBot):
        async def __call__(self, method, **kwargs):
            if getattr(method, "text", None) == confirmed:
                raise asyncio.CancelledError
            return await super().__call__(method, **kwargs)

    bot = CancelledConfirmationBot()
    await _ready(dp, bot)
    with pytest.raises(asyncio.CancelledError):
        await dp.feed_update(bot, _cb(bot, 4, REQUESTER_ID, "nt:send"))
    assert await repo.get_tracked_ticket(77) is not None
    assert await _state(dp, bot).get_state() is None
    client.create_ticket.assert_awaited_once()


async def test_uncertain_post_warning_delivery_cannot_turn_into_generic_retry_advice(intake_env):
    dp, bot, client, _, _, _ = intake_env
    warning = intake_texts.creation_uncertain()
    original_send = bot._send
    attempts = 0

    def no_warning_sends(chat_id, text, reply_markup=None):
        nonlocal attempts
        if text == warning:
            attempts += 1
            raise RuntimeError("Send unavailable")
        return original_send(chat_id, text, reply_markup)

    bot._send = no_warning_sends
    client.create_ticket.side_effect = GlpiHTTPError(
        "No ticket ID", response=httpx.Response(201, content=b"[]")
    )
    await _ready(dp, bot)
    await dp.feed_update(bot, _cb(bot, 4, REQUESTER_ID, "nt:send"))
    assert attempts == 2
    assert any(text == warning for _, text, _ in bot.sent)
    assert texts.GENERIC_ERROR not in bot.toasts
    assert await _state(dp, bot).get_state() is None
    await dp.feed_update(bot, _cb(bot, 5, REQUESTER_ID, "nt:send"))
    client.create_ticket.assert_awaited_once()


@pytest.mark.parametrize("kind", ["rejected_attachment", "missing_attachment", "quiet_hours"])
async def test_optional_message_failure_never_changes_a_successful_confirmation(
    intake_env, monkeypatch, kind
):
    dp, bot, client, _, _, _ = intake_env
    await _ready(dp, bot)
    if kind == "quiet_hours":
        monkeypatch.setattr(WorkSchedule, "is_working", lambda *_: False)
        await _state(dp, bot).update_data(urgency=5)
        optional = intake_texts.quiet_urgent_notice()
    else:
        await _state(dp, bot).update_data(
            attachments=[{"file_id": "test", "filename": "bad.bin", "mime": "application/test"}]
        )
        monkeypatch.setattr(new_ticket.attachments, "download", AsyncMock(return_value=b"test"))
        if kind == "rejected_attachment":
            client.attach_document_to_ticket.side_effect = GlpiDocumentRejected("bad.bin", "type")
            optional = texts.attachments_rejected(["bad.bin"])
        else:
            client.attach_document_to_ticket.side_effect = GlpiError("Upload unavailable")
            optional = texts.attachments_partial_failure(0, 1)
    original_send = bot._send
    attempts = 0

    def unavailable_optional(chat_id, text, reply_markup=None):
        nonlocal attempts
        if text == optional:
            attempts += 1
            raise RuntimeError("Optional delivery unavailable")
        return original_send(chat_id, text, reply_markup)

    bot._send = unavailable_optional
    await dp.feed_update(bot, _cb(bot, 4, REQUESTER_ID, "nt:send"))
    confirmed = texts.ticket_created(77, "https://glpi.invalid/front/ticket.form.php?id=77")
    assert attempts == 1
    assert any(text == confirmed for _, text, _ in bot.sent)
    assert texts.GENERIC_ERROR not in bot.toasts
    assert await _state(dp, bot).get_state() is None
    client.create_ticket.assert_awaited_once()
