"""Requester-facing intake flow with mocked GLPI/AI; no external calls."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiogram import Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from bot import texts
from bot.glpi.client import GlpiError
from bot.glpi.models import ITILCategory
from bot.handlers.new_ticket import NewTicket, build_new_ticket_router
from bot.services.intake import CATEGORY_NAMES, IntakeResult, fallback_result
from bot.texts import intake as intake_texts
from tests.test_new_ticket import (
    BOT_ID,
    CHAT,
    FakeBot,
    _cb_update,
    _fake_link,
    _last_text,
    _photo_update,
    _text_update,
)

pytestmark = pytest.mark.asyncio

DESCRIPTION = "  VPN подключается, но офисный ПК недоступен.\nОшибка появилась вчера. 🔎  "
CATEGORY_IDS = {key: 100 + i for i, key in enumerate(CATEGORY_NAMES)}


def _decision() -> IntakeResult:
    return IntakeResult(
        title="Не удаётся подключиться к офисному ПК",
        category="network",
        environment="unknown",
        approval_required=False,
        reason="Проблема удалённого подключения; новый платный доступ не запрошен.",
        model="test-model",
    )


def _harness():
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp["link"] = _fake_link()
    client, repo, shadow = AsyncMock(), AsyncMock(), AsyncMock()
    client.create_ticket.return_value = 901
    client.list_categories.return_value = [
        ITILCategory(CATEGORY_IDS[key], name, name) for key, name in CATEGORY_NAMES.items()
    ]
    intake = SimpleNamespace(
        classify=AsyncMock(return_value=_decision()),
        category_id=MagicMock(side_effect=CATEGORY_IDS.__getitem__),
    )
    cache = MagicMock()
    dp.include_router(build_new_ticket_router(client, cache, repo, shadow=shadow, intake=intake))
    ctx = FSMContext(storage, StorageKey(bot_id=BOT_ID, chat_id=CHAT, user_id=CHAT))
    return SimpleNamespace(
        dp=dp,
        bot=FakeBot(),
        ctx=ctx,
        client=client,
        repo=repo,
        shadow=shadow,
        intake=intake,
        cache=cache,
    )


async def _at_attachments(h, *, description=DESCRIPTION, urgency=3):
    await h.ctx.set_state(NewTicket.attaching)
    await h.ctx.set_data({"description": description, "urgency": urgency, "attachments": []})


async def test_single_description_to_glpi_without_title_or_category_prompt(monkeypatch):
    h = _harness()
    await h.dp.feed_update(h.bot, _text_update(h.bot, 1, "/new"))
    assert await h.ctx.get_state() == NewTicket.entering_description
    assert _last_text(h.bot) == intake_texts.description_prompt()
    await h.dp.feed_update(h.bot, _text_update(h.bot, 2, DESCRIPTION))
    assert await h.ctx.get_state() == NewTicket.choosing_urgency
    keyboard = h.bot.sent[-1][1]
    assert keyboard.inline_keyboard[0][0].text == "🔴 Срочно"
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 3, "nt:urg:4"))
    assert await h.ctx.get_state() == NewTicket.attaching
    await h.dp.feed_update(h.bot, _photo_update(h.bot, 4))
    monkeypatch.setattr(
        "bot.handlers.new_ticket.attachments.download", AsyncMock(return_value=b"png")
    )
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 5, "nt:send"))

    h.intake.classify.assert_awaited_once_with(DESCRIPTION)
    h.client.create_ticket.assert_awaited_once_with(
        name=_decision().title,
        content=DESCRIPTION,
        urgency=4,
        itilcategories_id=CATEGORY_IDS["network"],
        requester_users_id=7,
    )
    h.client.attach_document_to_ticket.assert_awaited_once()
    h.repo.save_ticket_intake.assert_awaited_once()
    ticket_id, data, timestamp = h.repo.save_ticket_intake.await_args.args
    assert ticket_id == 901 and timestamp > 0
    assert data["original_description"] == DESCRIPTION
    assert data["category_id"] == CATEGORY_IDS["network"]
    assert data["status"] == "ok"
    h.shadow.observe.assert_not_awaited()
    h.cache.get.assert_not_called()
    assert await h.ctx.get_state() is None
    sent = [text for text, _ in h.bot.sent]
    assert texts.NEW_ENTER_TITLE not in sent
    assert texts.NEW_CHOOSE_CATEGORY not in sent
    assert texts.ticket_created(901, None) in sent


async def test_free_text_reused_without_retyping():
    h = _harness()
    await h.dp.feed_update(h.bot, _text_update(h.bot, 1, DESCRIPTION))
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 2, "nt:offer:yes"))
    assert await h.ctx.get_state() == NewTicket.choosing_urgency
    assert (await h.ctx.get_data())["description"] == DESCRIPTION
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 3, "nt:urg:2"))
    await h.dp.feed_update(h.bot, _text_update(h.bot, 4, "готово"))
    assert h.client.create_ticket.await_args.kwargs["content"] == DESCRIPTION
    assert h.client.create_ticket.await_args.kwargs["urgency"] == 2
    assert intake_texts.description_prompt() not in [text for text, _ in h.bot.sent]


@pytest.mark.parametrize(
    "entry", [texts.BTN_NEW_TICKET, "/access", texts.BTN_ACCESS, texts.BTN_ACCESS_LEGACY]
)
async def test_old_menu_entries_open_single_description(entry):
    h = _harness()
    await h.dp.feed_update(h.bot, _text_update(h.bot, 1, entry))
    assert await h.ctx.get_state() == NewTicket.entering_description
    assert _last_text(h.bot) == intake_texts.description_prompt()


async def test_urgent_is_independent_of_environment_and_keeps_warning_gate():
    h = _harness()
    h.intake.classify.return_value = IntakeResult(
        title="Сломалась сборка Jenkins",
        category="infrastructure",
        environment="development",
        approval_required=False,
        reason="Сбой сборки разработки.",
    )
    await h.ctx.set_state(NewTicket.choosing_urgency)
    await h.ctx.set_data({"description": "Сломалась сборка Jenkins"})
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 1, "nt:urg:5"))
    assert _last_text(h.bot) == intake_texts.urgent_warning()
    assert "urgency" not in await h.ctx.get_data()
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 2, "nt:urg_decline"))
    assert "urgency" not in await h.ctx.get_data()
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 3, "nt:urg:5"))
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 4, "nt:urg_confirm"))
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 5, "nt:send"))
    assert h.client.create_ticket.await_args.kwargs["urgency"] == 5
    assert h.repo.save_ticket_intake.await_args.args[1]["environment"] == "development"


async def test_unexpected_classifier_error_uses_fallback_without_losing_original():
    h = _harness()
    h.intake.classify.side_effect = RuntimeError("classifier failed")
    await _at_attachments(h)
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 1, "nt:send"))
    kwargs = h.client.create_ticket.await_args.kwargs
    assert kwargs["name"] == fallback_result(DESCRIPTION).title
    assert kwargs["content"] == DESCRIPTION
    assert kwargs["itilcategories_id"] == CATEGORY_IDS["other"]
    assert h.repo.save_ticket_intake.await_args.args[1]["status"] == "fallback"


@pytest.mark.parametrize("failure", ["missing", "renamed", "unavailable", "bad_mapping"])
async def test_invalid_category_preserves_draft_and_does_not_silently_choose_another(failure):
    h = _harness()
    await _at_attachments(h)
    files = [{"file_id": "existing", "filename": "log.txt", "mime": "text/plain"}]
    await h.ctx.update_data(attachments=files)
    if failure == "missing":
        h.client.list_categories.return_value = []
    elif failure == "renamed":
        h.client.list_categories.return_value = [
            ITILCategory(CATEGORY_IDS["network"], "Old", "Old")
        ]
    elif failure == "unavailable":
        h.client.list_categories.side_effect = GlpiError("failed")
    else:
        h.intake.category_id.side_effect = ValueError("bad mapping")
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 1, "nt:send"))
    h.client.create_ticket.assert_not_awaited()
    assert await h.ctx.get_state() == NewTicket.attaching
    data = await h.ctx.get_data()
    assert data["description"] == DESCRIPTION and data["attachments"] == files
    assert _last_text(h.bot) == intake_texts.category_unavailable()


async def test_retry_after_category_lookup_failure_reuses_ai_result():
    h = _harness()
    await _at_attachments(h)
    categories = h.client.list_categories.return_value
    h.client.list_categories.side_effect = [GlpiError("unavailable"), categories]
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 1, "nt:send"))
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 2, "nt:send"))
    h.intake.classify.assert_awaited_once()
    h.client.create_ticket.assert_awaited_once()


async def test_duplicate_submit_and_cancel_while_classifying_do_not_duplicate_or_erase_ticket():
    h = _harness()
    await _at_attachments(h)
    entered, release = asyncio.Event(), asyncio.Event()

    async def classify(_):
        entered.set()
        await release.wait()
        return _decision()

    h.intake.classify.side_effect = classify
    first = asyncio.create_task(h.dp.feed_update(h.bot, _cb_update(h.bot, 1, "nt:send")))
    await entered.wait()
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 2, "nt:send"))
    await h.dp.feed_update(h.bot, _text_update(h.bot, 3, "/cancel"))
    await h.dp.feed_update(h.bot, _text_update(h.bot, 4, "/new"))
    assert await h.ctx.get_state() == NewTicket.submitting
    release.set()
    await first
    h.intake.classify.assert_awaited_once()
    h.client.create_ticket.assert_awaited_once()
    assert await h.ctx.get_state() is None


async def test_replaced_fsm_is_not_overwritten_after_classifier_returns():
    h = _harness()
    await _at_attachments(h)
    entered, release = asyncio.Event(), asyncio.Event()

    async def classify(_):
        entered.set()
        await release.wait()
        return _decision()

    h.intake.classify.side_effect = classify
    pending = asyncio.create_task(h.dp.feed_update(h.bot, _cb_update(h.bot, 1, "nt:send")))
    await entered.wait()
    # Production serializes user updates globally; the router also defends
    # itself if another integration replaces the context during an API call.
    await h.ctx.set_state("OtherDialog:writing")
    await h.ctx.set_data({"other": "keep this"})
    release.set()
    await pending
    h.client.create_ticket.assert_not_awaited()
    h.client.list_categories.assert_not_awaited()
    assert await h.ctx.get_state() == "OtherDialog:writing"
    assert await h.ctx.get_data() == {"other": "keep this"}


async def test_intake_metadata_is_saved_before_tracking_new_ticket():
    h = _harness()
    await _at_attachments(h)
    order = []

    async def created(**kwargs):
        order.append("create")
        return 901

    async def saved(*args):
        order.append("metadata")

    async def tracked(**kwargs):
        order.append("track")

    h.client.create_ticket.side_effect = created
    h.repo.save_ticket_intake.side_effect = saved
    h.repo.track_ticket.side_effect = tracked
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 1, "nt:send"))
    assert order == ["create", "metadata", "track"]


async def test_metadata_write_failure_still_confirms_existing_ticket_and_does_not_repeat_post(
    caplog,
):
    h = _harness()
    await _at_attachments(h)
    h.repo.save_ticket_intake.side_effect = RuntimeError("sqlite unavailable")
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 1, "nt:send"))
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 2, "nt:send"))
    h.client.create_ticket.assert_awaited_once()
    h.repo.track_ticket.assert_awaited_once()
    assert texts.ticket_created(901, None) in [text for text, _ in h.bot.sent]
    assert "ticket_intake_save_failed ticket_id=901" in caplog.text
    assert DESCRIPTION not in caplog.text


@pytest.mark.parametrize("status", [None, 500, 201])
async def test_uncertain_glpi_post_is_not_retried_or_reported_as_definite_failure(status):
    h = _harness()
    await _at_attachments(h)
    response = httpx.Response(status) if status is not None else None
    h.client.create_ticket.side_effect = GlpiError("unknown result", response=response)
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 1, "nt:send"))
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 2, "nt:send"))
    h.client.create_ticket.assert_awaited_once()
    h.repo.save_ticket_intake.assert_not_awaited()
    assert intake_texts.creation_uncertain() in [text for text, _ in h.bot.sent]
    assert texts.GLPI_ERROR not in [text for text, _ in h.bot.sent]


async def test_blank_description_and_invalid_urgency_do_not_advance():
    h = _harness()
    await h.dp.feed_update(h.bot, _text_update(h.bot, 1, "/new"))
    await h.dp.feed_update(h.bot, _text_update(h.bot, 2, " \n "))
    assert await h.ctx.get_state() == NewTicket.entering_description
    await h.dp.feed_update(h.bot, _text_update(h.bot, 3, DESCRIPTION))
    for n, value in enumerate(["0", "99", "bad"], 4):
        await h.dp.feed_update(h.bot, _cb_update(h.bot, n, f"nt:urg:{value}"))
    assert await h.ctx.get_state() == NewTicket.choosing_urgency
    assert "urgency" not in await h.ctx.get_data()
    h.client.create_ticket.assert_not_awaited()


async def test_cancel_single_description_preserves_normal_cancel_semantics():
    h = _harness()
    await h.dp.feed_update(h.bot, _text_update(h.bot, 1, "/new"))
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 2, "nt:cancel"))
    assert await h.ctx.get_state() is None
    assert _last_text(h.bot) == texts.NEW_CANCELLED
    h.intake.classify.assert_not_awaited()


async def test_new_copy_has_english_variant(monkeypatch):
    monkeypatch.setattr(texts, "_lang", "en")
    h = _harness()
    await h.dp.feed_update(h.bot, _text_update(h.bot, 1, "/new"))
    assert _last_text(h.bot).startswith("Describe the problem")
    assert intake_texts.urgent_label() == "🔴 Urgent"
    assert "production" not in intake_texts.urgent_warning().lower()
