"""Optional workload notices cannot change ticket-creation success or pilot scope."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from bot import main, texts
from bot.db.repo import Repo
from bot.glpi.models import ITILCategory
from bot.services.intake import CATEGORY_NAMES, IntakeResult
from tests.test_access import FakeBot, _cb, _dm
from tests.test_intake_pilot import CATEGORY_IDS, OTHER, PILOT, _settings


@pytest.fixture
async def env(tmp_path, request):
    repo = Repo(str(tmp_path / "workload-dispatch.sqlite3"))
    await repo.connect()
    for tg, glpi in ((PILOT, 42), (OTHER, 43)):
        await repo.upsert_link(
            tg_id=tg,
            glpi_users_id=glpi,
            display_name="Requester",
            is_tech=True,
            now=int(time.time()),
        )
    client = AsyncMock()
    client.list_categories.return_value = [
        ITILCategory(10, "Old accounts", "Old accounts"),
        *[ITILCategory(CATEGORY_IDS[k], name, name) for k, name in CATEGORY_NAMES.items()],
    ]
    client.create_ticket.return_value = 77
    intake = MagicMock()
    intake.category_id.side_effect = CATEGORY_IDS.__getitem__
    intake.classify = AsyncMock(
        return_value=IntakeResult(
            title="VPN connection problem",
            category="network",
            environment="unknown",
            approval_required=False,
            reason="Connection failure",
        )
    )
    workload = SimpleNamespace(notice_for=AsyncMock(return_value="QUEUE NOTICE"))
    settings = _settings(
        workload_notice_enabled=getattr(request, "param", True),
        workload_notice_pilot_tg_ids={PILOT},
    )
    dp = main.build_dispatcher(client, repo, settings, intake=intake, workload=workload)
    result = SimpleNamespace(dp=dp, repo=repo, client=client, workload=workload, bot=FakeBot())
    try:
        yield result
    finally:
        await dp.storage.close()
        await dp.fsm.events_isolation.close()
        await repo.close()


async def submit(env, tg=PILOT):
    await env.dp.feed_update(env.bot, _dm(env.bot, 1, tg, "/new"))
    if tg == OTHER:
        await env.dp.feed_update(env.bot, _cb(env.bot, 2, tg, "nt:cat:10"))
        await env.dp.feed_update(env.bot, _cb(env.bot, 3, tg, "nt:urg:3"))
        await env.dp.feed_update(env.bot, _dm(env.bot, 4, tg, "VPN problem"))
    await env.dp.feed_update(env.bot, _dm(env.bot, 5, tg, "VPN keeps disconnecting"))
    if tg == PILOT:
        await env.dp.feed_update(env.bot, _cb(env.bot, 6, tg, "nt:urg:3"))
    await env.dp.feed_update(env.bot, _cb(env.bot, 7, tg, "nt:send"))


async def test_creation_confirmed_before_workload_query_and_own_id_passed(env):
    async def notice(**kwargs):
        assert any(
            text == texts.ticket_created(77, "https://glpi.invalid/front/ticket.form.php?id=77")
            for chat, text, _ in env.bot.sent
            if chat == PILOT
        )
        return "QUEUE NOTICE"

    env.workload.notice_for.side_effect = notice
    await submit(env)
    env.workload.notice_for.assert_awaited_once_with(ticket_id=77, requester_tg_id=PILOT)
    assert any(text == "QUEUE NOTICE" for _, text, _ in env.bot.sent)
    env.client.create_ticket.assert_awaited_once()


async def test_nonpilot_creates_legacy_ticket_without_workload_access(env):
    await submit(env, OTHER)
    env.client.create_ticket.assert_awaited_once()
    env.workload.notice_for.assert_not_awaited()
    assert not any(text == "QUEUE NOTICE" for _, text, _ in env.bot.sent)


@pytest.mark.parametrize("env", [False], indirect=True)
async def test_disabled_feature_does_not_call_injected_service(env):
    await submit(env)
    env.workload.notice_for.assert_not_awaited()
    env.client.create_ticket.assert_awaited_once()


@pytest.mark.parametrize("failure", ["lookup", "telegram"])
async def test_notice_failure_keeps_success_and_cannot_repeat_creation(env, failure):
    if failure == "lookup":
        env.workload.notice_for.side_effect = RuntimeError("queue unavailable")
    else:

        class FailingNoticeBot(FakeBot):
            async def __call__(self, method, **kwargs):
                if getattr(method, "text", None) == "QUEUE NOTICE":
                    raise RuntimeError("optional message failed")
                return await super().__call__(method, **kwargs)

        env.bot = FailingNoticeBot()
    await submit(env)
    state = FSMContext(env.dp.storage, StorageKey(bot_id=env.bot.id, chat_id=PILOT, user_id=PILOT))
    assert await state.get_state() is None
    assert not any(text == texts.GLPI_ERROR for _, text, _ in env.bot.sent)
    assert any(
        text == texts.ticket_created(77, "https://glpi.invalid/front/ticket.form.php?id=77")
        for _, text, _ in env.bot.sent
    )
    await env.dp.feed_update(env.bot, _cb(env.bot, 8, PILOT, "nt:send"))
    env.client.create_ticket.assert_awaited_once()
