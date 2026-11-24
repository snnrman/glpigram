"""Legacy users cannot enter the pilot through category callbacks or old drafts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from bot import texts
from bot.glpi.client import GlpiError
from bot.glpi.models import ITILCategory
from bot.handlers.new_ticket import NewTicket, build_new_ticket_router
from bot.texts import intake as intake_texts
from tests.test_new_ticket import (
    BOT_ID,
    CHAT,
    FakeBot,
    _cb_update,
    _fake_link,
    _last_text,
    _text_update,
)

pytestmark = pytest.mark.asyncio

CATEGORIES = [
    ITILCategory(8, "Legacy", "Legacy"),
    ITILCategory(9, "Deleted", "Deleted", is_deleted=True),
    ITILCategory(10, "Reserved", "Reserved"),
    ITILCategory(100, "Intake", "Intake"),
]
DRAFT = {
    "category_id": 8,
    "category_name": "Legacy",
    "title": "A draft",
    "description": "Keep this description",
    "urgency": 3,
    "attachments": [],
}


def _harness(**restrictions):
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp["link"] = _fake_link()
    client, cache, repo = AsyncMock(), AsyncMock(), AsyncMock()
    cache.get.return_value = CATEGORIES
    client.list_categories.return_value = CATEGORIES
    client.create_ticket.return_value = 999
    dp.include_router(build_new_ticket_router(client, cache, repo, **restrictions))
    ctx = FSMContext(storage, StorageKey(bot_id=BOT_ID, chat_id=CHAT, user_id=CHAT))
    return SimpleNamespace(dp=dp, bot=FakeBot(), ctx=ctx, client=client, cache=cache, repo=repo)


def _restricted_harness():
    return _harness(allowed_category_ids={8, 9, 10}, excluded_category_ids={10, 100})


@pytest.mark.parametrize("entry", ["command", "offer"])
async def test_legacy_category_menu_excludes_pilot_and_deleted_categories(entry):
    h = _restricted_harness()
    if entry == "command":
        await h.dp.feed_update(h.bot, _text_update(h.bot, 1, "/new"))
    else:
        await h.dp.feed_update(h.bot, _text_update(h.bot, 1, DRAFT["description"]))
        await h.dp.feed_update(h.bot, _cb_update(h.bot, 2, "nt:offer:yes"))
    keyboard = next(markup for _, markup in reversed(h.bot.sent) if markup is not None)
    assert [row[0].callback_data for row in keyboard.inline_keyboard] == ["nt:cat:8", "nt:cancel"]


@pytest.mark.parametrize("category", ["garbage", "0", "-1", "999", "9", "10", "100"])
async def test_forged_or_unavailable_category_callback_cannot_replace_draft(category):
    h = _restricted_harness()
    await h.ctx.set_state(NewTicket.choosing_category)
    await h.ctx.set_data(DRAFT)
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 1, f"nt:cat:{category}"))
    assert await h.ctx.get_state() == NewTicket.choosing_category
    assert await h.ctx.get_data() == DRAFT
    assert _last_text(h.bot)
    h.client.create_ticket.assert_not_awaited()


async def test_category_cache_failure_keeps_category_selection_and_draft():
    h = _restricted_harness()
    h.cache.get.side_effect = GlpiError("temporarily unavailable")
    await h.ctx.set_state(NewTicket.choosing_category)
    await h.ctx.set_data(DRAFT)
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 1, "nt:cat:8"))
    assert await h.ctx.get_state() == NewTicket.choosing_category
    assert await h.ctx.get_data() == DRAFT
    assert _last_text(h.bot) == texts.NEW_NO_CATEGORIES


async def test_existing_allowed_category_advances_dialog():
    h = _restricted_harness()
    await h.ctx.set_state(NewTicket.choosing_category)
    await h.ctx.set_data({"description": DRAFT["description"]})
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 1, "nt:cat:8"))
    assert await h.ctx.get_state() == NewTicket.choosing_urgency
    assert await h.ctx.get_data() == {
        "description": DRAFT["description"],
        "category_id": 8,
        "category_name": "Legacy",
    }


@pytest.mark.parametrize("category_id", [100, 10, 999, 0, -1, True, "8", None])
@pytest.mark.parametrize("send", ["callback", "text"])
async def test_reserved_or_invalid_fsm_category_cannot_reach_post(category_id, send):
    h = _restricted_harness()
    draft = {**DRAFT, "category_id": category_id}
    await h.ctx.set_state(NewTicket.attaching)
    await h.ctx.set_data(draft)
    update = (
        _cb_update(h.bot, 1, "nt:send") if send == "callback" else _text_update(h.bot, 1, "готово")
    )
    await h.dp.feed_update(h.bot, update)
    h.client.create_ticket.assert_not_awaited()
    h.repo.track_ticket.assert_not_awaited()
    h.repo.save_ticket_intake.assert_not_awaited()
    assert await h.ctx.get_state() == NewTicket.attaching
    assert await h.ctx.get_data() == draft
    assert _last_text(h.bot) == intake_texts.category_unavailable()


@pytest.mark.parametrize("failure", ["missing", "deleted", "network"])
async def test_submit_checks_fresh_categories_and_preserves_draft_on_failure(failure):
    h = _restricted_harness()
    if failure == "network":
        h.client.list_categories.side_effect = GlpiError("unavailable")
    elif failure == "deleted":
        h.client.list_categories.return_value = [ITILCategory(8, "Legacy", "Legacy", True)]
    else:
        h.client.list_categories.return_value = []
    await h.ctx.set_state(NewTicket.attaching)
    await h.ctx.set_data(DRAFT)
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 1, "nt:send"))
    h.client.list_categories.assert_awaited_once()
    h.cache.get.assert_not_awaited()
    h.client.create_ticket.assert_not_awaited()
    assert await h.ctx.get_state() == NewTicket.attaching
    assert await h.ctx.get_data() == DRAFT
    assert _last_text(h.bot) == intake_texts.category_unavailable()


async def test_retry_after_temporary_validation_failure_retains_text_and_attachments(monkeypatch):
    h = _restricted_harness()
    h.client.list_categories.side_effect = [GlpiError("unavailable"), CATEGORIES]
    draft = {**DRAFT, "attachments": [{"file_id": "F", "filename": "f.jpg", "mime": "image/jpeg"}]}
    monkeypatch.setattr(
        "bot.handlers.new_ticket.attachments.download", AsyncMock(return_value=b"img")
    )
    await h.ctx.set_state(NewTicket.attaching)
    await h.ctx.set_data(draft)
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 1, "nt:send"))
    assert await h.ctx.get_data() == draft
    h.client.create_ticket.assert_not_awaited()
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 2, "nt:send"))
    h.client.create_ticket.assert_awaited_once_with(
        name=DRAFT["title"],
        content=DRAFT["description"],
        urgency=3,
        itilcategories_id=8,
        requester_users_id=7,
    )
    h.client.attach_document_to_ticket.assert_awaited_once()
    h.repo.save_ticket_intake.assert_not_awaited()
    assert await h.ctx.get_state() is None


@pytest.mark.parametrize("categories", [None, []])
async def test_free_text_offer_validation_failure_preserves_original_description(categories):
    h = _restricted_harness()
    if categories is None:
        h.cache.get.side_effect = GlpiError("unavailable")
    else:
        h.cache.get.return_value = categories
    await h.dp.feed_update(h.bot, _text_update(h.bot, 1, DRAFT["description"]))
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 2, "nt:offer:yes"))
    assert await h.ctx.get_state() is None
    assert await h.ctx.get_data() == {"description": DRAFT["description"]}
    assert _last_text(h.bot) == texts.NEW_NO_CATEGORIES


async def test_replaced_dialog_is_not_posted_after_slow_category_validation():
    h = _restricted_harness()
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_lookup():
        started.set()
        await release.wait()
        return CATEGORIES

    h.client.list_categories.side_effect = slow_lookup
    await h.ctx.set_state(NewTicket.attaching)
    await h.ctx.set_data(DRAFT)
    sending = asyncio.create_task(h.dp.feed_update(h.bot, _cb_update(h.bot, 1, "nt:send")))
    await started.wait()
    await h.ctx.set_state(NewTicket.choosing_category)
    await h.ctx.set_data({"description": "A different request"})
    release.set()
    await sending
    h.client.create_ticket.assert_not_awaited()
    assert await h.ctx.get_data() == {"description": "A different request"}


async def test_unrestricted_legacy_submission_keeps_existing_no_lookup_behavior():
    h = _harness()
    await h.ctx.set_state(NewTicket.attaching)
    await h.ctx.set_data(DRAFT)
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 1, "nt:send"))
    h.client.create_ticket.assert_awaited_once()
    h.client.list_categories.assert_not_awaited()


async def test_empty_allowlist_denies_every_category():
    h = _harness(allowed_category_ids=set())
    await h.ctx.set_state(NewTicket.attaching)
    await h.ctx.set_data(DRAFT)
    await h.dp.feed_update(h.bot, _cb_update(h.bot, 1, "nt:send"))
    h.client.create_ticket.assert_not_awaited()
    assert await h.ctx.get_data() == DRAFT
