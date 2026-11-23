"""Mandatory intake review gates for real technician handlers, including stale actions."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TgUser

from bot import texts
from bot.db.repo import Repo
from bot.glpi.models import Ticket
from bot.handlers.tech_actions import TechAction, build_tech_actions_router
from bot.texts import intake as intake_texts
from tests.test_handoff import (
    BOT_ID,
    GROUP,
    TECH2_ID,
    TECH_ID,
    FakeBot,
    _dm_cb,
    _group_cb,
)

TID = 901
CATEGORY_ID = 105
TITLE = "Сбой сборки Jenkins"
DESCRIPTION = "Jenkins не собирает проект."


def _decision():
    return {
        "title": TITLE,
        "original_description": DESCRIPTION,
        "category": "infrastructure",
        "category_id": CATEGORY_ID,
        "environment": "development",
        "approval_required": False,
        "reason": "Сбой сборки разработки.",
        "model": "test-model",
        "prompt_version": "test-v1",
        "status": "ok",
    }


@pytest.fixture
async def env(tmp_path):
    repo = Repo(str(tmp_path / "actions.sqlite3"))
    await repo.connect()
    repo.intake_category_ids = {"infrastructure": CATEGORY_ID}
    await repo.save_ticket_intake(TID, _decision(), 1)
    await repo.upsert_link(
        tg_id=TECH_ID, glpi_users_id=9, display_name="Техник", is_tech=True, now=1
    )
    await repo.upsert_link(
        tg_id=TECH2_ID, glpi_users_id=11, display_name="Другой техник", is_tech=True, now=1
    )
    link = await repo.get_by_tg(TECH_ID)
    ticket = Ticket(TID, TITLE, DESCRIPTION, 1, 5, CATEGORY_ID)
    client = AsyncMock()
    client.get_ticket.return_value = ticket
    client.get_ticket_assignees.return_value = ["Техник"]
    client.add_followup.return_value = 300
    cards = AsyncMock()
    dp = Dispatcher()
    dp.include_router(
        build_tech_actions_router(client, repo=repo, cards=cards, tech_group_chat_id=GROUP)
    )
    ctx = FSMContext(dp.storage, StorageKey(bot_id=BOT_ID, chat_id=TECH_ID, user_id=TECH_ID))
    yield SimpleNamespace(
        repo=repo,
        client=client,
        ticket=ticket,
        cards=cards,
        dp=dp,
        bot=FakeBot(),
        link=link,
        ctx=ctx,
    )
    await dp.storage.close()
    await repo.close()


async def _click(env, action, update=1):
    data = f"ta:{action}:{TID}" if action != "hto" else f"ta:hto:{TID}:11"
    make = _dm_cb if action == "hto" else _group_cb
    await env.dp.feed_update(env.bot, make(env.bot, update, TECH_ID, data), link=env.link)


async def _text(env, content, update=10):
    message = Message(
        message_id=update,
        date=datetime(2026, 11, 24, tzinfo=UTC),
        chat=Chat(id=TECH_ID, type="private"),
        from_user=TgUser(id=TECH_ID, is_bot=False, first_name="Tech"),
        text=content,
    ).as_(env.bot)
    await env.dp.feed_update(env.bot, Update(update_id=update, message=message), link=env.link)


async def _review(env, state="routine"):
    row = await env.repo.get_ticket_intake(TID)
    await env.repo.update_ticket_intake(
        TID,
        expected_version=row["version"],
        actor_tg_id=TECH_ID,
        now=2,
        event=state,
        review_state=state,
    )
    if state == "approval":
        env.ticket.global_validation = 3


def _assert_no_mutation(env):
    env.client.assign_ticket.assert_not_awaited()
    env.client.reassign_ticket.assert_not_awaited()
    env.client.add_solution.assert_not_awaited()


@pytest.mark.parametrize("action", ["take", "close", "handoff", "hto"])
async def test_pending_review_blocks_stale_card_callbacks_server_side(env, action):
    # These callbacks can come from old cards/reminders even when the new
    # keyboard hides their buttons; the decision must be enforced server-side.
    await _click(env, action)
    _assert_no_mutation(env)
    assert intake_texts.review_required() in env.bot.toasts
    assert await env.ctx.get_state() is None
    assert not env.bot.sent


async def test_pending_review_blocks_preexisting_solution_dialog_but_keeps_draft_state(env):
    await env.ctx.set_state(TechAction.closing)
    await env.ctx.set_data({"ticket_id": TID})
    await _text(env, "Исправлено")
    _assert_no_mutation(env)
    assert intake_texts.review_required() in [text for _, text, _ in env.bot.sent]
    assert await env.ctx.get_state() == TechAction.closing
    await _review(env)
    await _text(env, "Исправлено", 11)
    env.client.add_solution.assert_awaited_once_with(TID, "Техник:\nИсправлено")
    assert await env.ctx.get_state() is None


async def test_pending_review_allows_comment_dialog_and_followup(env):
    await _click(env, "comment")
    assert await env.ctx.get_state() == TechAction.commenting
    await _text(env, "Уточните номер проекта")
    env.client.add_followup.assert_awaited_once_with(TID, "Техник:\nУточните номер проекта")
    _assert_no_mutation(env)
    assert (await env.repo.get_ticket_intake(TID))["review_state"] == "pending"


@pytest.mark.parametrize("review", ["routine", "approval"])
@pytest.mark.parametrize("action", ["take", "close", "handoff", "hto", "solution"])
async def test_reviewed_ordinary_or_lead_approved_ticket_allows_actions(env, review, action):
    await _review(env, review)
    if action == "solution":
        await env.ctx.set_state(TechAction.closing)
        await env.ctx.set_data({"ticket_id": TID})
        await _text(env, "Исправлено")
        env.client.add_solution.assert_awaited_once()
    else:
        await _click(env, action)
        if action == "take":
            env.client.assign_ticket.assert_awaited_once_with(TID, 9)
        elif action == "hto":
            env.client.reassign_ticket.assert_awaited_once_with(TID, 11)
        elif action == "close":
            assert await env.ctx.get_state() == TechAction.closing
        else:
            assert any(
                chat == TECH_ID and text == texts.handoff_pick(TID)
                for chat, text, _ in env.bot.sent
            )
            keyboard = env.bot.sent[-1][2]
            assert f"ta:hto:{TID}:11" in [
                b.callback_data for row in keyboard.inline_keyboard for b in row
            ]
    assert intake_texts.review_required() not in env.bot.toasts


@pytest.mark.parametrize("action", ["take", "close", "handoff", "hto", "solution"])
async def test_waiting_for_lead_still_blocks_after_technician_chose_approval(env, action):
    await _review(env, "approval")
    env.ticket.global_validation = 2
    if action == "solution":
        await env.ctx.set_state(TechAction.closing)
        await env.ctx.set_data({"ticket_id": TID})
        await _text(env, "Исправлено")
        assert texts.TECH_APPROVAL_BLOCKED in [text for _, text, _ in env.bot.sent]
    else:
        await _click(env, action)
        assert texts.TECH_APPROVAL_BLOCKED in env.bot.toasts
    _assert_no_mutation(env)


@pytest.mark.parametrize("validation", [0, 1, 4])
@pytest.mark.parametrize("action", ["take", "close", "hto", "solution"])
async def test_approval_review_requires_actual_granted_glpi_validation(env, validation, action):
    await _review(env, "approval")
    env.ticket.global_validation = validation
    if action == "solution":
        await env.ctx.set_state(TechAction.closing)
        await env.ctx.set_data({"ticket_id": TID})
        await _text(env, "Исправлено")
        assert texts.TECH_APPROVAL_BLOCKED in [text for _, text, _ in env.bot.sent]
    else:
        await _click(env, action)
        assert texts.TECH_APPROVAL_BLOCKED in env.bot.toasts
    _assert_no_mutation(env)


@pytest.mark.parametrize("change", ["title", "description"])
@pytest.mark.parametrize("action", ["take", "close", "hto", "solution"])
async def test_live_glpi_text_edit_invalidates_review_before_mutation(env, change, action):
    await _review(env)
    if action == "solution":
        # Opened while the original text was reviewed, then edited elsewhere.
        await _click(env, "close")
        assert await env.ctx.get_state() == TechAction.closing
    if change == "title":
        env.ticket.name = "Новая просьба: купить внешний сервис"
    else:
        env.ticket.content = "Теперь нужна покупка нового платного сервиса."
    if action == "solution":
        await _text(env, "Исправлено")
        assert intake_texts.review_required() in [text for _, text, _ in env.bot.sent]
    else:
        await _click(env, action, 2)
        assert intake_texts.review_required() in env.bot.toasts
    _assert_no_mutation(env)
    row = await env.repo.get_ticket_intake(TID)
    assert row["review_state"] == "pending" and row["ai_stale"] is True
    assert row["original_description"] == DESCRIPTION


async def test_intake_handoff_urgent_development_is_not_labeled_production(env):
    await _review(env)
    await _click(env, "hto")
    target_messages = [text for chat, text, _ in env.bot.sent if chat == TECH2_ID]
    assert len(target_messages) == 1
    assert "🔴 Срочно" in target_messages[0]
    assert "прод" not in target_messages[0].lower()
    assert "№901" in target_messages[0]


async def test_missing_live_ticket_blocks_known_intake(env):
    await _review(env)
    env.client.get_ticket.return_value = None
    await _click(env, "take")
    _assert_no_mutation(env)
    assert texts.GLPI_ACTION_ERROR in env.bot.toasts


async def test_missing_metadata_in_reserved_category_is_recovered_and_requires_review(env):
    # Use a new GLPI ticket in the reserved taxonomy with no bot-side row,
    # e.g. a successful create followed by a bookkeeping failure.
    env.ticket.id = 902
    cb = _group_cb(env.bot, 1, TECH_ID, "ta:take:902")
    await env.dp.feed_update(env.bot, cb, link=env.link)
    _assert_no_mutation(env)
    row = await env.repo.get_ticket_intake(902)
    assert row["review_state"] == "pending" and row["metadata_recovered"] is True
    assert intake_texts.review_required() in env.bot.toasts


async def test_legacy_ticket_without_metadata_keeps_existing_actions_and_handoff_label(env):
    env.ticket.id = 903
    env.ticket.itilcategories_id = 8  # Outside the new intake taxonomy.
    cb = _group_cb(env.bot, 1, TECH_ID, "ta:take:903")
    await env.dp.feed_update(env.bot, cb, link=env.link)
    env.client.assign_ticket.assert_awaited_once_with(903, 9)
    cb = _dm_cb(env.bot, 2, TECH_ID, "ta:hto:903:11")
    await env.dp.feed_update(env.bot, cb, link=env.link)
    env.client.reassign_ticket.assert_awaited_once_with(903, 11)
    target_messages = [text for chat, text, _ in env.bot.sent if chat == TECH2_ID]
    assert any("Срочно (прод)" in text for text in target_messages)
    assert await env.repo.get_ticket_intake(903) is None


def test_intake_handoff_copy_is_escaped_and_has_english_variant(monkeypatch):
    monkeypatch.setattr(texts, "_lang", "en")
    result = intake_texts.handoff_to_new(901, "Title <bad>&", 5)
    assert "Ticket #901" in result
    assert "🔴 Urgent" in result and "prod" not in result
    assert "Title &lt;bad&gt;&amp;" in result
    assert "First choose" in intake_texts.review_required()


@pytest.mark.parametrize("validation", [0, 1, 2, 4])
async def test_rechecked_old_approval_must_still_be_granted_before_action(env, validation):
    await _review(env, "approval")
    row = await env.repo.get_ticket_intake(TID)
    env.ticket.content = DESCRIPTION + " Уточнён адрес сервера."
    await env.repo.refresh_ticket_intake_snapshot(
        TID,
        expected_version=row["version"],
        now=3,
        snapshot={"title": TITLE, "description": env.ticket.content, "approval_recheck": True},
    )
    await _review(env, "routine")  # Technician explicitly checked the previous grant.
    env.ticket.global_validation = validation
    await _click(env, "take")
    _assert_no_mutation(env)
    assert texts.TECH_APPROVAL_BLOCKED in env.bot.toasts
