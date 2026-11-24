"""Optional workload notices cannot change ticket-creation success or pilot scope."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import ReplyKeyboardMarkup

from bot import main, texts
from bot.db.repo import Repo
from bot.glpi.models import ITILCategory, Ticket
from bot.handlers.new_ticket import NewTicket
from bot.services.intake import CATEGORY_NAMES, IntakeResult
from bot.texts import intake as intake_texts
from tests.test_access import FakeBot, _cb, _dm
from tests.test_intake_pilot import CATEGORY_IDS, OTHER, PILOT, _settings

GLOBAL_SETTINGS = {
    "ai_intake_pilot_tg_ids": set(),
    "workload_notice_all_users": True,
    "workload_notice_pilot_tg_ids": set(),
}


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
    config_values = {"workload_notice_enabled": True, "workload_notice_pilot_tg_ids": {PILOT}}
    overrides = getattr(request, "param", {})
    config_values.update(
        {"workload_notice_enabled": overrides} if isinstance(overrides, bool) else overrides
    )
    settings = _settings(**config_values)
    dp = main.build_dispatcher(client, repo, settings, intake=intake, workload=workload)
    result = SimpleNamespace(
        dp=dp,
        repo=repo,
        client=client,
        intake=intake,
        workload=workload,
        settings=settings,
        bot=FakeBot(),
    )
    try:
        yield result
    finally:
        await dp.storage.close()
        await dp.fsm.events_isolation.close()
        await repo.close()


async def submit(env, tg=PILOT):
    new_form = env.settings.ai_intake_enabled and (
        not env.settings.ai_intake_pilot_tg_ids or tg in env.settings.ai_intake_pilot_tg_ids
    )
    await env.dp.feed_update(env.bot, _dm(env.bot, 1, tg, "/new"))
    if not new_form:
        await env.dp.feed_update(env.bot, _cb(env.bot, 2, tg, "nt:cat:10"))
        await env.dp.feed_update(env.bot, _cb(env.bot, 3, tg, "nt:urg:3"))
        await env.dp.feed_update(env.bot, _dm(env.bot, 4, tg, "VPN problem"))
    await env.dp.feed_update(env.bot, _dm(env.bot, 5, tg, "VPN keeps disconnecting"))
    if new_form:
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


def _assert_global_intake_confirmation(env, tg, glpi_id):
    env.client.create_ticket.assert_awaited_once_with(
        name="VPN connection problem",
        content="VPN keeps disconnecting",
        urgency=3,
        itilcategories_id=CATEGORY_IDS["network"],
        requester_users_id=glpi_id,
    )
    env.intake.classify.assert_awaited_once_with("VPN keeps disconnecting")
    env.workload.notice_for.assert_awaited_once_with(ticket_id=77, requester_tg_id=tg)
    user_messages = [(text, markup) for chat, text, markup in env.bot.sent if chat == tg]
    assert any(text == intake_texts.description_prompt() for text, _ in user_messages)
    assert all(text != texts.NEW_CHOOSE_CATEGORY for text, _ in user_messages)
    assert all(text != texts.NEW_ENTER_TITLE for text, _ in user_messages)
    assert sum(text == "QUEUE NOTICE" for text, _ in user_messages) == 1
    menus = [markup for _, markup in user_messages if isinstance(markup, ReplyKeyboardMarkup)]
    assert menus
    assert all(
        button.text != texts.BTN_ACCESS for menu in menus for row in menu.keyboard for button in row
    )


@pytest.mark.parametrize("env", [GLOBAL_SETTINGS], indirect=True)
async def test_global_rollout_gives_other_linked_user_one_ai_call_and_workload_hook(env):
    await env.dp.feed_update(env.bot, _dm(env.bot, 0, OTHER, "/start"))
    await submit(env, OTHER)
    _assert_global_intake_confirmation(env, OTHER, 43)
    assert (await env.repo.get_ticket_intake(77))["review_state"] == "pending"


@pytest.mark.parametrize("env", [GLOBAL_SETTINGS], indirect=True)
async def test_global_rollout_includes_user_linked_after_dispatcher_creation(env):
    new_user = 9009
    assert await env.repo.get_by_tg(new_user) is None
    await env.repo.upsert_link(
        tg_id=new_user,
        glpi_users_id=44,
        display_name="New employee",
        is_tech=False,
        now=int(time.time()),
    )
    await env.dp.feed_update(env.bot, _dm(env.bot, 0, new_user, "/start"))
    await submit(env, new_user)
    _assert_global_intake_confirmation(env, new_user, 44)
    assert (await env.repo.get_tracked_ticket(77)).requester_tg_id == new_user


@pytest.mark.parametrize("env", [GLOBAL_SETTINGS], indirect=True)
async def test_global_rollout_does_not_allow_unlinked_user_to_create(env):
    unlinked = 9010
    await submit(env, unlinked)
    env.client.create_ticket.assert_not_awaited()
    env.intake.classify.assert_not_awaited()
    env.workload.notice_for.assert_not_awaited()
    assert any(chat == unlinked and text == texts.NEED_LINK for chat, text, _ in env.bot.sent)
    assert await env.repo.get_tracked_ticket(77) is None


@pytest.mark.parametrize("env", [GLOBAL_SETTINGS], indirect=True)
async def test_global_rollout_rechecks_link_before_submitting_existing_draft(env):
    await env.dp.feed_update(env.bot, _dm(env.bot, 1, OTHER, "/new"))
    await env.dp.feed_update(env.bot, _dm(env.bot, 2, OTHER, "VPN keeps disconnecting"))
    await env.dp.feed_update(env.bot, _cb(env.bot, 3, OTHER, "nt:urg:3"))
    state = FSMContext(env.dp.storage, StorageKey(bot_id=env.bot.id, chat_id=OTHER, user_id=OTHER))
    assert await state.get_state() == NewTicket.attaching
    await env.repo.unlink_tg(OTHER)
    await env.dp.feed_update(env.bot, _cb(env.bot, 4, OTHER, "nt:send"))
    env.client.create_ticket.assert_not_awaited()
    env.intake.classify.assert_not_awaited()
    env.workload.notice_for.assert_not_awaited()
    assert texts.NEED_LINK in env.bot.toasts


@pytest.mark.parametrize(
    "env",
    [
        GLOBAL_SETTINGS
        | {
            "ai_intake_enabled": False,
            "workload_notice_enabled": False,
            "workload_notice_all_users": False,
        }
    ],
    indirect=True,
)
async def test_global_rollback_preserves_missing_metadata_recovery_and_technician_review(env):
    assert env.repo.intake_recovery_filter is None
    assert env.repo.intake_category_ids == CATEGORY_IDS
    assert "intake_review" in {router.name for router in env.dp.sub_routers}
    await env.dp.feed_update(env.bot, _dm(env.bot, 1, OTHER, "/new"))
    state = FSMContext(env.dp.storage, StorageKey(bot_id=env.bot.id, chat_id=OTHER, user_id=OTHER))
    assert await state.get_state() == NewTicket.choosing_category

    # A successful pre-rollback GLPI POST may have lost its metadata write.
    # Keeping [] prevents recovery from narrowing back to the original pilot.
    env.client.get_ticket.return_value = Ticket(
        77, "Existing request", "Created before rollback", 1, 3, CATEGORY_IDS["network"]
    )
    assert await env.repo.get_ticket_intake(77) is None
    await env.dp.feed_update(env.bot, _cb(env.bot, 2, OTHER, "ta:take:77"))
    env.client.assign_ticket.assert_not_awaited()
    recovered = await env.repo.get_ticket_intake(77)
    assert recovered["metadata_recovered"] is True
    assert recovered["review_state"] == "pending"
    await env.dp.feed_update(
        env.bot, _cb(env.bot, 3, OTHER, f"ir:routine:77:{recovered['version']}")
    )
    assert (await env.repo.get_ticket_intake(77))["review_state"] == "routine"
    await env.dp.feed_update(env.bot, _cb(env.bot, 4, OTHER, "ta:take:77"))
    env.client.assign_ticket.assert_awaited_once_with(77, 43)
    env.client.get_ticket_requester.assert_not_awaited()
    env.intake.classify.assert_not_awaited()
    env.workload.notice_for.assert_not_awaited()


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
