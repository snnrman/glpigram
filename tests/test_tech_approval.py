"""Real dispatcher/SQLite, fake GLPI/Telegram: technician approval safety and recovery."""

import asyncio
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from bot import texts
from bot.config import Settings
from bot.db.repo import Repo
from bot.glpi.client import GlpiError
from bot.glpi.models import Ticket, User
from bot.handlers.tech_actions import TechAction
from bot.handlers.tech_approval import TechApproval
from bot.main import build_dispatcher
from bot.services.tech_approval import ApprovalUnavailable, TechApprovalService
from tests.test_access import FakeBot, _cb, _dm

TID, TECH, REQUESTER, LEAD = 714, 2002, 1001, 3003


@pytest.fixture
async def env(tmp_path):
    repo = Repo(str(tmp_path / "tech-approval.sqlite3"))
    await repo.connect()
    for tg, uid, name, tech in [
        (TECH, 2, "Tech", True),
        (REQUESTER, 8, "Requester", False),
        (LEAD, 19, "Lead", False),
    ]:
        await repo.upsert_link(
            tg_id=tg, glpi_users_id=uid, display_name=name, is_tech=tech, now=int(time.time())
        )
    client = AsyncMock()
    ticket = Ticket(TID, "Доступ к Figma", "Нужен рабочий аккаунт", 2, 3)
    client.get_ticket.return_value = ticket
    client.get_ticket_requester.return_value = (8, "Requester")
    client.find_user_by_login.return_value = User(id=19, name="lead", firstname="Lead")
    client.get_user.return_value = User(id=19, name="lead", firstname="Lead")
    client.find_validation.return_value = None
    client.add_followup.return_value = 900

    async def create(*args, **kwargs):
        ticket.global_validation = 2
        return 123

    client.create_validation.side_effect = create
    settings = Settings(
        _env_file=None,
        telegram_bot_token="x",
        glpi_api_url="https://glpi.invalid/apirest.php",
        glpi_user_token="x",
        lead_logins="lead",
    )
    dp = build_dispatcher(client, repo, settings)
    bot = FakeBot()
    yield dp, client, repo, bot, ticket
    await repo.close()


def state(dp, tg=TECH):
    return FSMContext(storage=dp.storage, key=StorageKey(bot_id=42, chat_id=tg, user_id=tg))


async def confirm(env):
    dp, client, repo, bot, ticket = env
    await dp.feed_update(bot, _cb(bot, 1, TECH, f"ta:approval:{TID}"))
    data = await state(dp).get_data()
    await dp.feed_update(bot, _cb(bot, 2, TECH, f"tap:lead:{data['nonce']}:19"))
    assert await state(dp).get_state() == TechApproval.confirm.state
    client.create_validation.assert_not_awaited()
    return await state(dp).get_data()


async def test_existing_ticket_is_approved_without_recreation_or_reassignment(env):
    dp, client, repo, bot, ticket = env
    await repo.track_ticket(
        ticket_id=TID, requester_tg_id=REQUESTER, requester_glpi_id=8, status=2, now=10
    )
    data = await confirm(env)
    await dp.feed_update(bot, _cb(bot, 3, TECH, f"tap:send:{data['nonce']}"))
    client.create_validation.assert_awaited_once()
    client.create_ticket.assert_not_awaited()
    client.set_ticket_status.assert_not_awaited()
    client.assign_ticket.assert_not_awaited()
    row = await repo.get_approval(TID)
    assert (row["validation_id"], row["lead_tg_id"], row["requester_tg_id"]) == (
        123,
        LEAD,
        REQUESTER,
    )
    assert (await repo.get_tracked_ticket(TID)).last_status == 2
    assert (await repo.get_tech_approval(TID))["phase"] == "done"
    assert any(chat == LEAD and "Figma" in text and "Tech" in text for chat, text, _ in bot.sent)
    # Existing lead-decision handler completes this approval, too.
    await dp.feed_update(bot, _cb(bot, 4, LEAD, f"ap:ok:{TID}"))
    client.answer_validation.assert_awaited_once()
    assert (await repo.get_approval(TID))["status"] == 1
    assert await state(dp).get_state() is None


async def test_nontech_and_self_approval_are_rejected(env):
    dp, client, repo, bot, ticket = env
    await dp.feed_update(bot, _cb(bot, 1, REQUESTER, f"ta:approval:{TID}"))
    assert texts.TECH_ONLY in bot.toasts
    client.get_ticket.assert_not_awaited()
    client.get_ticket_requester.return_value = (19, "Lead")
    await dp.feed_update(bot, _cb(bot, 2, TECH, f"ta:approval:{TID}"))
    client.create_validation.assert_not_awaited()
    assert texts.ACC_NO_LEADS in bot.toasts


@pytest.mark.parametrize("change", ["closed", "changed", "external_approval", "revoked_lead"])
async def test_confirm_rechecks_live_ticket_and_recipient(env, change):
    dp, client, repo, bot, ticket = env
    data = await confirm(env)
    if change == "closed":
        ticket.status = 6
    if change == "changed":
        ticket.content = "Different request"
    if change == "external_approval":
        ticket.global_validation = 2
    if change == "revoked_lead":
        client.get_user.return_value = None
    await dp.feed_update(bot, _cb(bot, 3, TECH, f"tap:send:{data['nonce']}"))
    client.create_validation.assert_not_awaited()
    assert await repo.get_approval(TID) is None


async def test_cancel_stale_button_and_unrelated_draft_make_no_writes(env):
    dp, client, repo, bot, ticket = env
    data = await confirm(env)
    await dp.feed_update(bot, _cb(bot, 3, TECH, f"tap:cancel:{data['nonce']}"))
    await dp.feed_update(bot, _cb(bot, 4, TECH, f"tap:send:{data['nonce']}"))
    await state(dp).set_state(TechAction.commenting)
    await state(dp).set_data({"ticket_id": 99})
    await dp.feed_update(bot, _cb(bot, 5, TECH, f"ta:approval:{TID}"))
    assert await state(dp).get_state() == TechAction.commenting.state
    assert (await state(dp).get_data())["ticket_id"] == 99
    client.create_validation.assert_not_awaited()


async def test_double_confirmation_creates_one_validation(env):
    dp, client, repo, bot, ticket = env
    data = await confirm(env)
    tech = await repo.get_by_tg(TECH)
    service = dp["tech_approvals"]
    results = await asyncio.gather(
        service.submit(bot, data, tech), service.submit(bot, data, tech), return_exceptions=True
    )
    assert results[0] == "done"
    assert isinstance(results[1], ApprovalUnavailable)
    client.create_validation.assert_awaited_once()


async def test_lost_post_response_recovers_after_restart_without_posting_again(env):
    dp, client, repo, bot, ticket = env
    data = await confirm(env)

    async def uncertain(*args, **kwargs):
        ticket.global_validation = 2
        raise GlpiError("response lost")

    client.create_validation.side_effect = uncertain
    service = dp["tech_approvals"]
    assert await service.submit(bot, data, await repo.get_by_tg(TECH)) == "uncertain"
    assert (await repo.get_tech_approval(TID))["phase"] == "posting"
    # Same durable DB, a new coordinator as after a process restart.
    await repo.close()
    await repo.connect()
    restored = TechApprovalService(client, repo, dp["claude_approvals"])
    await restored.tick(bot)
    client.create_validation.assert_awaited_once()
    assert await repo.get_approval(TID) is None
    client.find_validation.return_value = 123
    await restored.tick(bot)
    assert (await repo.get_approval(TID))["validation_id"] == 123
    assert (await repo.get_tech_approval(TID))["phase"] == "done"
    client.create_validation.assert_awaited_once()


async def test_failed_telegram_delivery_is_retried_without_new_validation(env):
    dp, client, repo, bot, ticket = env
    data = await confirm(env)
    original = bot.send_message
    bot.send_message = AsyncMock(side_effect=RuntimeError("unavailable"))
    service = dp["tech_approvals"]
    assert await service.submit(bot, data, await repo.get_by_tg(TECH)) == "delivery_pending"
    bot.send_message = original
    await service.tick(bot)
    assert (await repo.get_tech_approval(TID))["phase"] == "done"
    client.create_validation.assert_awaited_once()


@pytest.mark.parametrize(
    "callback", ["ta:take:714", "ta:close:714", "ta:handoff:714", "ta:hto:714:2"]
)
async def test_pending_approval_blocks_old_action_buttons(env, callback):
    dp, client, repo, bot, ticket = env
    ticket.global_validation = 2
    await dp.feed_update(bot, _cb(bot, 1, TECH, callback))
    client.assign_ticket.assert_not_awaited()
    client.reassign_ticket.assert_not_awaited()
    client.add_solution.assert_not_awaited()
    assert await state(dp).get_state() is None


async def test_approval_started_after_solution_dialog_blocks_submission(env):
    dp, client, repo, bot, ticket = env
    await state(dp).set_state(TechAction.closing)
    await state(dp).set_data({"ticket_id": TID})
    ticket.global_validation = 2
    await dp.feed_update(bot, _dm(bot, 1, TECH, "Done"))
    client.add_solution.assert_not_awaited()


async def test_claude_adds_second_stage_and_preserves_tracked_ticket(env):
    dp, client, repo, bot, ticket = env
    ticket.name = "Claude"
    await repo.track_ticket(
        ticket_id=TID, requester_tg_id=REQUESTER, requester_glpi_id=8, status=2, now=10
    )
    claude = dp["claude_approvals"]
    claude.available_members = AsyncMock(return_value=[(20, 4004, "Service approver")])
    data = await confirm(env)
    assert data["service"] == "claude"
    await dp.feed_update(bot, _cb(bot, 3, TECH, f"tap:send:{data['nonce']}"))
    workflow = await repo.get_claude_approval(TID)
    assert workflow["stage"] == "lead"
    assert workflow["requester_glpi_id"] == 8
    assert len(await repo.claude_members(TID)) == 1
    assert (await repo.get_tracked_ticket(TID)).last_status == 2


async def test_requester_without_telegram_can_still_get_glpi_approval(env):
    dp, client, repo, bot, ticket = env
    client.get_ticket_requester.return_value = (88, "GLPI-only requester")
    data = await confirm(env)
    await dp.feed_update(bot, _cb(bot, 3, TECH, f"tap:send:{data['nonce']}"))
    assert (await repo.get_approval(TID))["requester_tg_id"] == 0
    assert all(chat != 0 for chat, _, _ in bot.sent)
    assert any(chat == LEAD and "GLPI-only requester" in text for chat, text, _ in bot.sent)


async def test_explicit_glpi_rejection_allows_a_fresh_manual_attempt(env):
    dp, client, repo, bot, ticket = env
    data = await confirm(env)
    client.create_validation.side_effect = GlpiError("denied", response=httpx.Response(403))
    service = dp["tech_approvals"]
    tech = await repo.get_by_tg(TECH)
    assert await service.submit(bot, data, tech) == "failed"
    assert await repo.pending_tech_approvals() == []
    client.create_validation.side_effect = None
    client.create_validation.return_value = 124
    assert await service.submit(bot, data, tech) == "done"
    assert (await repo.get_approval(TID))["validation_id"] == 124


async def test_lead_relinked_after_delivery_cannot_decide(env):
    dp, client, repo, bot, ticket = env
    data = await confirm(env)
    await dp.feed_update(bot, _cb(bot, 3, TECH, f"tap:send:{data['nonce']}"))
    await repo.upsert_link(
        tg_id=LEAD,
        glpi_users_id=99,
        display_name="Different account",
        is_tech=False,
        now=int(time.time()),
    )
    await dp.feed_update(bot, _cb(bot, 4, LEAD, f"ap:ok:{TID}"))
    client.answer_validation.assert_not_awaited()
    assert (await repo.get_approval(TID))["status"] == 0


async def test_ambiguous_service_is_clarified_by_technician_without_ai(env):
    dp, client, repo, bot, ticket = env
    ticket.name = "Вернуть ChatGPT после закрытия Claude"
    await dp.feed_update(bot, _cb(bot, 1, TECH, f"ta:approval:{TID}"))
    assert await state(dp).get_state() == TechApproval.service.state
    await dp.feed_update(bot, _dm(bot, 2, TECH, "ChatGPT"))
    data = await state(dp).get_data()
    assert data["service"] == "other"
    assert await state(dp).get_state() == TechApproval.lead.state
    client.create_validation.assert_not_awaited()


async def test_command_suggests_requesters_lead_and_cancel_is_safe(env):
    dp, client, repo, bot, ticket = env
    await repo.set_user_lead(8, 19)
    await dp.feed_update(bot, _dm(bot, 1, TECH, "/approval 714"))
    assert await state(dp).get_state() == TechApproval.confirm.state
    assert (await state(dp).get_data())["lead_glpi_id"] == 19
    await dp.feed_update(bot, _dm(bot, 2, TECH, "/cancel"))
    assert await state(dp).get_state() is None
    client.create_validation.assert_not_awaited()


async def test_manual_rejection_closes_original_ticket_with_reason(env):
    dp, client, repo, bot, ticket = env
    data = await confirm(env)
    await dp.feed_update(bot, _cb(bot, 3, TECH, f"tap:send:{data['nonce']}"))
    await dp.feed_update(bot, _cb(bot, 4, LEAD, f"ap:no:{TID}"))
    await dp.feed_update(bot, _dm(bot, 5, LEAD, "Нет бюджета"))
    assert client.answer_validation.await_args.kwargs["accepted"] is False
    client.set_ticket_status.assert_awaited_once_with(TID, 6)
    assert (await repo.get_approval(TID))["status"] == -1
    assert any(chat == REQUESTER and "Нет бюджета" in text for chat, text, _ in bot.sent)


async def test_reservation_blocks_take_even_before_glpi_has_created_validation(env):
    dp, client, repo, bot, ticket = env
    data = await confirm(env)
    client.create_validation.side_effect = GlpiError("connection lost")
    await dp["tech_approvals"].submit(bot, data, await repo.get_by_tg(TECH))
    assert ticket.global_validation == 1
    await dp.feed_update(bot, _cb(bot, 4, TECH, f"ta:take:{TID}"))
    client.assign_ticket.assert_not_awaited()
    assert texts.TECH_APPROVAL_BLOCKED in bot.toasts
