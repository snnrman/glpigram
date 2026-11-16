from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from bot import texts
from bot.config import Settings
from bot.db.repo import Repo
from bot.glpi.client import GlpiError
from bot.handlers.new_ticket import NewTicket
from bot.main import build_dispatcher
from bot.services.ai_shadow import ShadowClassifier
from tests import test_access as access
from tests.test_ai_shadow import fake_client, row


@pytest.fixture
async def env(tmp_path):
    repo = Repo(str(tmp_path / "dispatch.sqlite3"))
    await repo.connect()
    for tg_id, glpi_id, name in (
        (access.REQUESTER_ID, 8, "Requester Private Name"),
        (access.LEAD_ID, access.LEAD_GLPI, "Lead Private Name"),
    ):
        await repo.upsert_link(
            tg_id=tg_id,
            glpi_users_id=glpi_id,
            display_name=name,
            is_tech=False,
            now=int(time.time()),
        )
    glpi = access._client()
    ai = fake_client()
    shadow = ShadowClassifier(repo, ai)
    settings = Settings(lead_logins="uliana.metlina", access_category_id=10)
    dp = build_dispatcher(glpi, repo, settings, shadow=shadow)
    yield dp, glpi, repo, ai, shadow
    await repo.close()


async def submit_normal(dp, bot):
    ctx = FSMContext(
        storage=dp.storage,
        key=StorageKey(bot_id=bot.id, chat_id=access.REQUESTER_ID, user_id=access.REQUESTER_ID),
    )
    await ctx.set_state(NewTicket.attaching)
    await ctx.set_data(
        {
            "title": "ChatGPT",
            "description": "Нужен доступ",
            "urgency": 3,
            "category_id": 1,
            "attachments": [],
        }
    )
    await dp.feed_update(bot, access._cb(bot, 1, access.REQUESTER_ID, "nt:send"))
    return ctx


async def test_new_ticket_has_same_payload_and_confirmation_before_queued_api_work(env):
    dp, glpi, repo, ai, shadow = env
    bot = access.FakeBot()
    ctx = await submit_normal(dp, bot)
    assert await ctx.get_state() is None
    glpi.create_ticket.assert_awaited_once_with(
        name="ChatGPT",
        content="Нужен доступ",
        urgency=3,
        itilcategories_id=1,
        requester_users_id=8,
    )
    assert any("77" in t for t in access._to(bot, access.REQUESTER_ID))
    ai.classify.assert_not_awaited()
    assert (await row(repo, 77))["state"] == "pending"
    glpi.create_validation.assert_not_awaited()
    glpi.set_ticket_status.assert_not_awaited()
    await shadow.tick()
    glpi.create_validation.assert_not_awaited()
    glpi.set_ticket_status.assert_not_awaited()


async def test_access_keeps_validation_and_lead_message_and_excludes_identity_metadata(env):
    dp, glpi, repo, ai, shadow = env
    bot = access.FakeBot()
    await access._run_dialog_to_sent(dp, bot)
    glpi.create_validation.assert_awaited_once()
    assert access._to(bot, access.LEAD_ID)
    payload = (await row(repo, 77))["payload"]
    assert "Jenkins" in payload
    assert "Private Name" not in payload and "Метлина" not in payload
    ai.classify.assert_not_awaited()
    approval_before = dict(await repo.get_approval(77))
    await shadow.tick()
    assert dict(await repo.get_approval(77)) == approval_before
    glpi.answer_validation.assert_not_awaited()
    # The persistent paid-services menu is deliberately retained in shadow mode.
    assert any(
        texts.BTN_ACCESS == button.text
        for chat, _, markup in bot.sent
        if hasattr(markup, "keyboard")
        for buttons in markup.keyboard
        for button in buttons
    )


async def test_no_ai_job_when_glpi_creation_fails(env):
    dp, glpi, repo, ai, _ = env
    glpi.create_ticket.side_effect = GlpiError("unavailable")
    bot = access.FakeBot()
    await submit_normal(dp, bot)
    assert await row(repo, 77) is None
    assert texts.GLPI_ERROR in access._to(bot, access.REQUESTER_ID)
    ai.classify.assert_not_awaited()


async def test_queue_database_failure_cannot_turn_created_ticket_into_failure(env, monkeypatch):
    dp, glpi, repo, ai, _ = env
    monkeypatch.setattr(repo, "enqueue_ai_shadow", AsyncMock(side_effect=RuntimeError("db")))
    bot = access.FakeBot()
    await submit_normal(dp, bot)
    assert any("77" in t for t in access._to(bot, access.REQUESTER_ID))
    assert texts.GLPI_ERROR not in access._to(bot, access.REQUESTER_ID)
    glpi.create_ticket.assert_awaited_once()
    ai.classify.assert_not_awaited()
