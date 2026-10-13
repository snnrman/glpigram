"""Exercise real dispatcher/router ordering and take gates for the Claude policy."""

from __future__ import annotations

import asyncio
import time

import pytest

from bot import texts
from bot.config import Settings
from bot.db.repo import Repo
from bot.glpi.client import GlpiError
from bot.glpi.models import Ticket
from bot.glpi.models import User as GlpiUser
from bot.main import build_dispatcher
from bot.services.claude_approval import CLAUDE_POLICY_MARKER
from tests.test_access import (
    LEAD_GLPI,
    LEAD_ID,
    REQUESTER_ID,
    TICKET,
    FakeBot,
    _cb,
    _client,
    _dm,
    _to,
)

EXTRA_A, EXTRA_B, TECH = 4100, 4200, 4300


@pytest.fixture
async def flow(tmp_path):
    repo = Repo(str(tmp_path / "dispatch.sqlite3"))
    await repo.connect()
    ids = {REQUESTER_ID: 8, LEAD_ID: LEAD_GLPI, EXTRA_A: 82, EXTRA_B: 54, TECH: 27}
    for tg, glpi in ids.items():
        await repo.upsert_link(
            tg_id=tg,
            glpi_users_id=glpi,
            display_name=f"user-{glpi}",
            is_tech=tg == TECH,
            now=int(time.time()),
        )
    client = _client()
    client.get_user.side_effect = lambda gid: GlpiUser(id=gid, name=f"user-{gid}")
    ticket = Ticket(id=TICKET, name="Access", content="", status=1, urgency=3, global_validation=2)
    client.get_ticket.return_value = ticket

    async def answer_validation(vid, *, accepted, comment):
        ticket.global_validation = 3 if accepted else 4

    async def set_ticket_status(tid, status):
        ticket.status = status

    client.answer_validation.side_effect = answer_validation
    client.set_ticket_status.side_effect = set_ticket_status
    settings = Settings(
        lead_logins="uliana.metlina", access_category_id=10, claude_approver_glpi_ids="82,54"
    )
    dp = build_dispatcher(client, repo, settings)
    bot = FakeBot()
    yield dp, client, repo, bot, ticket
    await repo.close()


async def _begin(flow, request):
    dp, _, _, bot, _ = flow
    await dp.feed_update(bot, _dm(bot, 1, REQUESTER_ID, "/access"))
    await dp.feed_update(bot, _cb(bot, 2, REQUESTER_ID, "ac:go"))
    await dp.feed_update(bot, _dm(bot, 3, REQUESTER_ID, request))


async def _send(flow, request="Нужен доступ к Claude", clarification=None):
    await _begin(flow, request)
    dp, _, _, bot, _ = flow
    if clarification:
        await dp.feed_update(bot, _dm(bot, 4, REQUESTER_ID, clarification))
    await dp.feed_update(bot, _cb(bot, 5, REQUESTER_ID, f"ac:lead:{LEAD_GLPI}"))
    await dp.feed_update(bot, _cb(bot, 6, REQUESTER_ID, "ac:send"))


async def test_claude_lead_only_does_not_grant_validation_or_allow_take(flow):
    dp, client, repo, bot, _ = flow
    await _send(flow)
    assert CLAUDE_POLICY_MARKER in client.create_ticket.call_args.kwargs["content"]
    await dp.feed_update(bot, _cb(bot, 7, LEAD_ID, f"ap:ok:{TICKET}"))
    assert (await repo.get_claude_approval(TICKET))["stage"] == "extra"
    client.answer_validation.assert_not_awaited()
    assert _to(bot, EXTRA_A) and _to(bot, EXTRA_B)
    client.get_ticket.side_effect = GlpiError("unavailable")
    await dp.feed_update(bot, _cb(bot, 8, TECH, f"ta:take:{TICKET}"))
    client.assign_ticket.assert_not_awaited()
    assert texts.CLAUDE_TAKE_BLOCKED in bot.toasts


@pytest.mark.parametrize("extra", [EXTRA_A, EXTRA_B])
async def test_both_steps_grant_and_allow_take(flow, extra):
    dp, client, repo, bot, _ = flow
    await _send(flow)
    await dp.feed_update(bot, _cb(bot, 7, LEAD_ID, f"ap:ok:{TICKET}"))
    await dp.feed_update(bot, _cb(bot, 8, extra, f"ce:ok:{TICKET}"))
    assert (await repo.get_claude_approval(TICKET))["stage"] == "approved"
    client.answer_validation.assert_awaited_once()
    await dp.feed_update(bot, _cb(bot, 9, TECH, f"ta:take:{TICKET}"))
    client.assign_ticket.assert_awaited_once()


async def test_claude_approved_but_live_api_unavailable_still_cannot_take(flow):
    dp, client, _, bot, _ = flow
    await _send(flow)
    await dp.feed_update(bot, _cb(bot, 7, LEAD_ID, f"ap:ok:{TICKET}"))
    await dp.feed_update(bot, _cb(bot, 8, EXTRA_A, f"ce:ok:{TICKET}"))
    client.get_ticket.side_effect = GlpiError("unavailable")
    await dp.feed_update(bot, _cb(bot, 9, TECH, f"ta:take:{TICKET}"))
    client.assign_ticket.assert_not_awaited()


async def test_extra_rejection_closes_and_other_cannot_override(flow):
    dp, client, repo, bot, _ = flow
    await _send(flow)
    await dp.feed_update(bot, _cb(bot, 7, LEAD_ID, f"ap:ok:{TICKET}"))
    await dp.feed_update(bot, _cb(bot, 8, EXTRA_A, f"ce:no:{TICKET}"))
    await dp.feed_update(bot, _dm(bot, 9, EXTRA_A, "Не обоснована необходимость"))
    assert (await repo.get_claude_approval(TICKET))["stage"] == "rejected"
    assert client.answer_validation.call_args.kwargs["accepted"] is False
    client.set_ticket_status.assert_awaited_once_with(TICKET, 6)
    await dp.feed_update(bot, _cb(bot, 10, EXTRA_B, f"ce:ok:{TICKET}"))
    client.answer_validation.assert_awaited_once()
    await dp.feed_update(bot, _cb(bot, 11, TECH, f"ta:take:{TICKET}"))
    client.assign_ticket.assert_not_awaited()


async def test_ordinary_figma_keeps_original_lead_approval(flow):
    dp, client, repo, bot, _ = flow
    await _send(flow, "Нужна Figma для дизайна")
    assert await repo.get_claude_approval(TICKET) is None
    await dp.feed_update(bot, _cb(bot, 7, LEAD_ID, f"ap:ok:{TICKET}"))
    client.answer_validation.assert_awaited_once()
    assert not _to(bot, EXTRA_A) and not _to(bot, EXTRA_B)
    await dp.feed_update(bot, _cb(bot, 8, TECH, f"ta:take:{TICKET}"))
    client.assign_ticket.assert_awaited_once()


@pytest.mark.parametrize("clarification, is_claude", [("Claude", True), ("Google Cloud", False)])
async def test_neutral_clarification_preserves_original_request_and_routes(
    flow, clarification, is_claude
):
    dp, client, repo, bot, _ = flow
    await _begin(flow, "Нужен клауд для работы")
    assert texts.CLAUDE_SERVICE_ASK in _to(bot, REQUESTER_ID)
    assert "claude" not in texts.CLAUDE_SERVICE_ASK.casefold()
    client.create_ticket.assert_not_awaited()
    await dp.feed_update(bot, _dm(bot, 4, REQUESTER_ID, clarification))
    await dp.feed_update(bot, _cb(bot, 5, REQUESTER_ID, f"ac:lead:{LEAD_GLPI}"))
    await dp.feed_update(bot, _cb(bot, 6, REQUESTER_ID, "ac:send"))
    content = client.create_ticket.call_args.kwargs["content"]
    assert "Нужен клауд для работы" in content and clarification in content
    assert (await repo.get_claude_approval(TICKET) is not None) == is_claude


async def test_incidental_claude_mention_clarified_as_figma_has_no_second_stage(flow):
    _, _, repo, _, _ = flow
    await _send(flow, "Нужна Figma, Claude посоветовал её", clarification="Figma")
    assert await repo.get_claude_approval(TICKET) is None


async def test_missing_approver_configuration_blocks_claude_before_ticket_creation(flow):
    dp, client, _, bot, _ = flow
    dp["claude_approvals"].approver_ids = []
    await _send(flow)
    client.create_ticket.assert_not_awaited()
    assert texts.CLAUDE_APPROVERS_UNAVAILABLE in bot.toasts


async def test_orphaned_policy_marker_is_not_an_ordinary_take(flow):
    dp, client, _, bot, ticket = flow
    ticket.content = CLAUDE_POLICY_MARKER
    ticket.global_validation = 3
    await dp.feed_update(bot, _cb(bot, 7, TECH, f"ta:take:{TICKET}"))
    client.assign_ticket.assert_not_awaited()


async def test_replayed_lead_button_during_second_stage_does_not_grant(flow):
    dp, client, repo, bot, _ = flow
    await _send(flow)
    await dp.feed_update(bot, _cb(bot, 7, LEAD_ID, f"ap:ok:{TICKET}"))
    await dp.feed_update(bot, _cb(bot, 8, LEAD_ID, f"ap:ok:{TICKET}"))
    assert (await repo.get_claude_approval(TICKET))["stage"] == "extra"
    client.answer_validation.assert_not_awaited()


async def test_duplicate_submission_creates_only_one_ticket(flow):
    dp, client, _, bot, _ = flow
    await _begin(flow, "Claude для работы")
    await dp.feed_update(bot, _cb(bot, 5, REQUESTER_ID, f"ac:lead:{LEAD_GLPI}"))
    await asyncio.gather(
        *(dp.feed_update(bot, _cb(bot, uid, REQUESTER_ID, "ac:send")) for uid in (6, 7))
    )
    client.create_ticket.assert_awaited_once()
    client.create_validation.assert_awaited_once()


async def test_handoff_is_not_a_way_to_bypass_claude_approvals(flow):
    dp, client, _, bot, _ = flow
    await _send(flow)
    await dp.feed_update(bot, _cb(bot, 7, TECH, f"ta:hto:{TICKET}:27"))
    client.reassign_ticket.assert_not_awaited()
    assert texts.CLAUDE_TAKE_BLOCKED in bot.toasts


async def test_solution_is_not_a_way_to_bypass_claude_approvals(flow):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey

    from bot.handlers.tech_actions import TechAction

    dp, client, _, bot, _ = flow
    await _send(flow)
    state = FSMContext(
        storage=dp.storage, key=StorageKey(bot_id=bot.id, chat_id=TECH, user_id=TECH)
    )
    await state.set_state(TechAction.closing)
    await state.update_data(ticket_id=TICKET)
    await dp.feed_update(bot, _dm(bot, 7, TECH, "Доступ предоставлен"))
    client.add_solution.assert_not_awaited()
    assert texts.CLAUDE_TAKE_BLOCKED in _to(bot, TECH)


async def test_missing_live_ticket_cannot_bypass_completed_claude_gate(flow):
    dp, client, _, bot, _ = flow
    await _send(flow)
    await dp.feed_update(bot, _cb(bot, 7, LEAD_ID, f"ap:ok:{TICKET}"))
    await dp.feed_update(bot, _cb(bot, 8, EXTRA_A, f"ce:ok:{TICKET}"))
    client.get_ticket.return_value = None
    await dp.feed_update(bot, _cb(bot, 9, TECH, f"ta:take:{TICKET}"))
    client.assign_ticket.assert_not_awaited()
