"""Regression coverage for the persistent two-stage Claude approval policy."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from unittest.mock import AsyncMock

import pytest

from bot.db.repo import Repo
from bot.glpi.client import GlpiError
from bot.glpi.models import Ticket
from bot.glpi.models import User as GlpiUser
from bot.schedule import WorkSchedule
from bot.services.claude_approval import ClaudeApprovalService
from tests.test_access import FakeBot

TICKET = 700
REQUESTER = 900
LEAD = 300
LEAD_GLPI = 19
EXTRA_A, EXTRA_B = 100, 101
EXTRA_A_GLPI, EXTRA_B_GLPI = 82, 54


def _user(glpi_id: int, name: str) -> GlpiUser:
    return GlpiUser(id=glpi_id, name=name, firstname=name, realname="Approver")


@pytest.fixture
async def policy(tmp_path):
    repo = Repo(str(tmp_path / "claude.sqlite3"))
    await repo.connect()
    now = int(time.time())
    for tg, glpi, name in [
        (REQUESTER, 8, "Requester"),
        (LEAD, LEAD_GLPI, "Lead"),
        (EXTRA_A, EXTRA_A_GLPI, "Extra A"),
        (EXTRA_B, EXTRA_B_GLPI, "Extra B"),
    ]:
        await repo.upsert_link(
            tg_id=tg, glpi_users_id=glpi, display_name=name, is_tech=False, now=now
        )
    client = AsyncMock()
    client.get_user.side_effect = lambda gid: _user(gid, f"user-{gid}")
    ticket = Ticket(id=TICKET, name="Claude", content="", status=1, urgency=3, global_validation=2)
    client.get_ticket.return_value = ticket
    client.add_followup.return_value = 44
    svc = ClaudeApprovalService(
        client,
        repo,
        approver_ids=f"{EXTRA_A_GLPI},{EXTRA_B_GLPI}",
        schedule=WorkSchedule.from_config("09:00-18:00", "1-5", tz_name="UTC"),
    )
    await repo.create_claude_approval(
        TICKET,
        validation_id=5,
        lead_glpi_id=LEAD_GLPI,
        lead_tg_id=LEAD,
        lead_name="Lead",
        requester_tg_id=REQUESTER,
        requester_glpi_id=8,
        request_text="Claude access",
        members=[(EXTRA_A_GLPI, EXTRA_A, "Extra A"), (EXTRA_B_GLPI, EXTRA_B, "Extra B")],
        now=now,
    )
    yield svc, client, repo, ticket
    await repo.close()


async def _to_extra(svc, bot, client, repo):
    assert await svc.approve(bot, TICKET, "lead", LEAD) == "lead_approved"
    row = await repo.get_claude_approval(TICKET)
    assert row["stage"] == "extra"
    assert client.answer_validation.await_count == 0


async def test_lead_approval_waits_for_extra_and_cannot_be_taken(policy):
    svc, client, repo, _ = policy
    bot = FakeBot()
    await _to_extra(svc, bot, client, repo)
    notices = [text for chat, text, _ in bot.sent if chat == REQUESTER]
    assert len(notices) == 1
    assert "Lead" in notices[0]
    assert "Extra A" in notices[0] and "Extra B" in notices[0]
    bot.sent.clear()
    await svc.tick(bot)
    assert not any(chat == REQUESTER for chat, _, _ in bot.sent)
    assert client.set_ticket_status.await_count == 0
    assert (await repo.get_approval(TICKET))["status"] == 0
    # The tech action gate permits only the terminal approved stage.
    assert (await repo.get_claude_approval(TICKET))["stage"] != "approved"


@pytest.mark.parametrize("tgid", [EXTRA_A, EXTRA_B])
async def test_either_designated_extra_approver_can_complete(policy, tgid):
    svc, client, repo, _ = policy
    bot = FakeBot()
    await _to_extra(svc, bot, client, repo)
    assert await svc.approve(bot, TICKET, "extra", tgid) == "approved"
    client.answer_validation.assert_awaited_once()
    assert client.answer_validation.await_args.kwargs["accepted"] is True
    assert (await repo.get_claude_approval(TICKET))["stage"] == "approved"
    assert (await repo.get_approval(TICKET))["status"] == 1


@pytest.mark.parametrize("tgid", [EXTRA_A, EXTRA_B])
async def test_any_extra_rejection_wins_and_other_button_becomes_stale(policy, tgid):
    svc, client, repo, _ = policy
    bot = FakeBot()
    await _to_extra(svc, bot, client, repo)
    assert await svc.reject(bot, TICKET, "extra", tgid, "policy reason") == "rejected"
    other = EXTRA_B if tgid == EXTRA_A else EXTRA_A
    assert await svc.approve(bot, TICKET, "extra", other) == "stale"
    assert await svc.reject(bot, TICKET, "extra", other, "late") == "stale"
    assert client.answer_validation.await_count == 1
    assert client.answer_validation.await_args.kwargs["accepted"] is False
    client.set_ticket_status.assert_awaited_once()
    assert (await repo.get_claude_approval(TICKET))["stage"] == "rejected"


async def test_lead_rejection_is_final_without_extra_stage(policy):
    svc, client, repo, _ = policy
    result = await svc.reject(FakeBot(), TICKET, "lead", LEAD, "no approval")
    assert result == "rejected"
    assert (await repo.get_claude_approval(TICKET))["stage"] == "rejected"
    assert await svc.approve(FakeBot(), TICKET, "extra", EXTRA_A) == "stale"
    assert client.answer_validation.await_args.kwargs["accepted"] is False


async def test_outsider_and_requester_cannot_approve(policy):
    svc, client, repo, _ = policy
    bot = FakeBot()
    assert await svc.approve(bot, TICKET, "lead", REQUESTER) == "stale"
    assert await svc.approve(bot, TICKET, "lead", 5555) == "stale"
    assert client.answer_validation.await_count == 0
    assert (await repo.get_claude_approval(TICKET))["stage"] == "lead"


async def test_relink_invalidates_pending_authority(policy):
    svc, client, repo, _ = policy
    now = int(time.time())
    await repo.upsert_link(
        tg_id=LEAD, glpi_users_id=999, display_name="Relinked", is_tech=False, now=now
    )
    assert await svc.approve(FakeBot(), TICKET, "lead", LEAD) == "stale"
    assert (await repo.get_claude_approval(TICKET))["stage"] == "lead"


async def test_glpi_must_still_show_waiting_validation(policy):
    svc, client, repo, ticket = policy
    ticket.global_validation = 3
    assert await svc.approve(FakeBot(), TICKET, "lead", LEAD) == "stale"
    ticket.global_validation = 2
    ticket.status = 6
    assert await svc.approve(FakeBot(), TICKET, "lead", LEAD) == "stale"
    assert client.answer_validation.await_count == 0


async def test_available_members_uses_configured_glpi_ids_and_links(policy):
    svc, client, _, _ = policy
    assert await svc.available_members(REQUESTER) == [
        (EXTRA_A_GLPI, EXTRA_A, "user-82 Approver"),
        (EXTRA_B_GLPI, EXTRA_B, "user-54 Approver"),
    ]
    assert [c.args[0] for c in client.get_user.await_args_list] == [82, 54]


async def test_concurrent_lead_decisions_only_transition_once(policy):
    svc, client, repo, _ = policy
    bot = FakeBot()
    outcomes = await asyncio.gather(
        svc.approve(bot, TICKET, "lead", LEAD),
        svc.reject(bot, TICKET, "lead", LEAD, "no"),
    )
    assert outcomes.count("lead_approved") + outcomes.count("rejected") == 1
    assert outcomes.count("stale") == 1
    row = await repo.get_claude_approval(TICKET)
    assert row["stage"] in {"extra", "rejected"}
    assert client.answer_validation.await_count <= 1


async def test_restart_between_stages_keeps_extra_authority(policy):
    svc, client, repo, _ = policy
    bot = FakeBot()
    await _to_extra(svc, bot, client, repo)
    restarted = ClaudeApprovalService(client, repo, approver_ids="82,54", schedule=svc.schedule)
    assert await restarted.approve(bot, TICKET, "extra", EXTRA_B) == "approved"
    assert (await repo.get_claude_approval(TICKET))["stage"] == "approved"


async def test_glpi_failure_leaves_final_decision_for_tick_retry(policy):
    svc, client, repo, _ = policy
    bot = FakeBot()
    await _to_extra(svc, bot, client, repo)
    client.answer_validation.side_effect = GlpiError("offline")
    assert await svc.approve(bot, TICKET, "extra", EXTRA_A) == "retry"
    assert (await repo.get_claude_approval(TICKET))["stage"] == "approve_pending"
    client.answer_validation.side_effect = None
    client.answer_validation.return_value = None
    await svc.tick(bot)
    assert (await repo.get_claude_approval(TICKET))["stage"] == "approved"
    assert client.answer_validation.await_count == 2  # failed call, then tick retry
    await svc.tick(bot)
    assert client.answer_validation.await_count == 2


async def test_failed_requester_notification_is_retried_by_tick(policy):
    svc, client, repo, _ = policy
    bot = FakeBot()
    await _to_extra(svc, bot, client, repo)
    # Telegram failure is a recoverable delivery failure; final state remains durable.
    bot.send_message = AsyncMock(side_effect=RuntimeError("telegram down"))
    assert await svc.approve(bot, TICKET, "extra", EXTRA_A) == "approved"
    row = await repo.get_claude_approval(TICKET)
    assert row["stage"] == "approved"
    assert row["notification_done"] == 0
    bot.send_message = AsyncMock(return_value=object())
    await svc.tick(bot)
    assert (await repo.get_claude_approval(TICKET))["notification_done"] == 1


async def test_old_callback_after_terminal_decision_is_stale(policy):
    svc, client, repo, _ = policy
    bot = FakeBot()
    await _to_extra(svc, bot, client, repo)
    assert await svc.reject(bot, TICKET, "extra", EXTRA_A, "rejected") == "rejected"
    assert await svc.approve(bot, TICKET, "extra", EXTRA_B) == "stale"
    assert await svc.approve(bot, TICKET, "lead", LEAD) == "stale"


async def test_close_failure_after_validation_is_retried_without_reanswering(policy):
    svc, client, repo, _ = policy
    bot = FakeBot()
    await _to_extra(svc, bot, client, repo)
    client.set_ticket_status.side_effect = [GlpiError("close failed"), None]
    assert await svc.reject(bot, TICKET, "extra", EXTRA_A, "declined") == "retry"
    row = await repo.get_claude_approval(TICKET)
    assert row["stage"] == "reject_pending"
    assert row["validation_done"] == 1
    assert client.answer_validation.await_count == 1

    await svc.tick(bot)
    assert (await repo.get_claude_approval(TICKET))["stage"] == "rejected"
    assert client.answer_validation.await_count == 1
    assert client.set_ticket_status.await_count == 2


async def test_extra_stage_reminders_go_to_both_members_without_reminding_lead(policy):
    svc, client, repo, _ = policy
    bot = FakeBot()
    await _to_extra(svc, bot, client, repo)
    members = await repo.claude_members(TICKET)
    for member in members:
        await repo.set_claude_member_dm(
            TICKET, member["glpi_id"], message_id=member["glpi_id"], now=int(time.time())
        )
    svc._reminder_due = lambda row, now, last: True
    bot.sent.clear()

    await svc.tick(bot)

    assert {chat for chat, _, _ in bot.sent} == {EXTRA_A, EXTRA_B}
    assert len(bot.sent) == 2
    assert all("Lead" in text for _, text, _ in bot.sent)
    assert LEAD not in [chat for chat, _, _ in bot.sent]


async def test_failed_lead_audit_survives_restart_and_tick_recovers_it(policy):
    svc, client, repo, _ = policy
    bot = FakeBot()
    client.add_followup.side_effect = [GlpiError("audit unavailable"), 45]

    assert await svc.approve(bot, TICKET, "lead", LEAD) == "lead_approved"
    row = await repo.get_claude_approval(TICKET)
    assert row["stage"] == "extra"
    assert row["lead_approved_at"] > 0
    assert row["lead_audit_done"] == 0
    assert client.answer_validation.await_count == 0

    db_path = repo._db_path
    await repo.close()
    restarted_repo = Repo(db_path)
    await restarted_repo.connect()
    try:
        restarted = ClaudeApprovalService(
            client, restarted_repo, approver_ids="82,54", schedule=svc.schedule
        )
        await restarted.tick(bot)
        row = await restarted_repo.get_claude_approval(TICKET)
        assert row["stage"] == "extra"
        assert row["lead_approved_at"] > 0
        assert row["lead_audit_done"] == 1
        assert client.answer_validation.await_count == 0
        assert client.add_followup.await_count == 2
    finally:
        await restarted_repo.close()


async def test_legacy_database_migration_preserves_existing_claude_row(policy):
    _, _, repo, _ = policy
    db_path = repo._db_path
    original = await repo.get_claude_approval(TICKET)
    await repo.close()

    # Simulate a deployed database from before the durable lead-audit fields.
    with sqlite3.connect(db_path) as db:
        db.execute("ALTER TABLE claude_approvals RENAME TO claude_approvals_new")
        db.execute(
            "CREATE TABLE claude_approvals ("
            "ticket_id INTEGER PRIMARY KEY, request_text TEXT NOT NULL, "
            "stage TEXT NOT NULL DEFAULT 'lead', stage_started_at INTEGER NOT NULL, "
            "actor_tg_id INTEGER, actor_name TEXT NOT NULL DEFAULT '', "
            "reason TEXT NOT NULL DEFAULT '', validation_done INTEGER NOT NULL DEFAULT 0, "
            "audit_done INTEGER NOT NULL DEFAULT 0, notification_done INTEGER NOT NULL DEFAULT 0)"
        )
        db.execute(
            "INSERT INTO claude_approvals (ticket_id,request_text,stage,stage_started_at) "
            "SELECT ticket_id,request_text,stage,stage_started_at FROM claude_approvals_new"
        )
        db.execute("DROP TABLE claude_approvals_new")

    migrated_repo = Repo(db_path)
    await migrated_repo.connect()
    try:
        migrated = await migrated_repo.get_claude_approval(TICKET)
        assert migrated["request_text"] == original["request_text"]
        assert migrated["stage"] == original["stage"]
        assert migrated["lead_approved_at"] == 0
        assert migrated["lead_audit_done"] == 0
    finally:
        await migrated_repo.close()


async def test_extra_relink_invalidates_old_actor_and_moves_pending_prompt(policy):
    svc, client, repo, _ = policy
    bot = FakeBot()
    await _to_extra(svc, bot, client, repo)
    await repo.upsert_link(
        tg_id=102,
        glpi_users_id=EXTRA_A_GLPI,
        display_name="Extra A relinked",
        is_tech=False,
        now=int(time.time()),
    )
    svc._reminder_due = lambda row, now, last: True
    bot.sent.clear()

    assert await svc.approve(bot, TICKET, "extra", EXTRA_A) == "stale"
    await svc.send_pending(bot, TICKET)
    row_member = next(m for m in await repo.claude_members(TICKET) if m["glpi_id"] == EXTRA_A_GLPI)
    assert row_member["tg_id"] == 102
    assert any(chat == 102 for chat, _, _ in bot.sent)
    assert not any(chat == EXTRA_A for chat, _, _ in bot.sent)
    assert await svc.approve(bot, TICKET, "extra", 102) == "approved"


async def test_lead_relink_keeps_glpi_authority_and_refreshes_dm_endpoint(policy):
    svc, client, repo, _ = policy
    bot = FakeBot()
    await repo.upsert_link(
        tg_id=301,
        glpi_users_id=LEAD_GLPI,
        display_name="Lead relinked",
        is_tech=False,
        now=int(time.time()),
    )
    svc._reminder_due = lambda row, now, last: True
    bot.sent.clear()

    assert await svc.approve(bot, TICKET, "lead", LEAD) == "stale"
    await svc.send_pending(bot, TICKET)
    row = await repo.get_claude_approval(TICKET)
    assert row["lead_tg_id"] == 301
    assert row["dm_message_id"] is not None
    assert any(chat == 301 for chat, _, _ in bot.sent)
    assert not any(chat == LEAD for chat, _, _ in bot.sent)
    assert await svc.approve(bot, TICKET, "lead", 301) == "lead_approved"


@pytest.mark.parametrize("mode", ["unlinked", "inactive"])
async def test_unlinked_or_inactive_extra_approvers_receive_no_pending_message(policy, mode):
    svc, client, repo, _ = policy
    bot = FakeBot()
    await _to_extra(svc, bot, client, repo)
    if mode == "unlinked":
        async with repo._tx() as db:
            await db.execute("DELETE FROM users WHERE glpi_users_id=?", (EXTRA_A_GLPI,))
    else:
        client.get_user.side_effect = lambda gid: _user(gid, f"user-{gid}")
        client.get_user.side_effect = lambda gid: GlpiUser(
            id=gid, name=f"user-{gid}", is_active=(gid != EXTRA_B_GLPI)
        )
    bot.sent.clear()
    svc._reminder_due = lambda row, now, last: True

    await svc.send_pending(bot, TICKET)

    delivered_chats = {chat for chat, _, _ in bot.sent}
    if mode == "unlinked":
        assert EXTRA_A not in delivered_chats
        assert EXTRA_B in delivered_chats
    else:
        assert EXTRA_B not in delivered_chats
        assert EXTRA_A in delivered_chats


async def test_terminal_requester_notification_uses_current_active_link(policy):
    svc, client, repo, _ = policy
    bot = FakeBot()
    await _to_extra(svc, bot, client, repo)
    await repo.upsert_link(
        tg_id=901,
        glpi_users_id=8,
        display_name="Requester relinked",
        is_tech=False,
        now=int(time.time()),
    )
    bot.sent.clear()

    assert await svc.approve(bot, TICKET, "extra", EXTRA_A) == "approved"

    assert any(chat == 901 for chat, _, _ in bot.sent)
    assert not any(chat == REQUESTER for chat, _, _ in bot.sent)
    assert (await repo.get_claude_approval(TICKET))["notification_done"] == 1


async def _make_lead_a_service_member(repo, svc, glpi_id):
    await repo.upsert_link(
        tg_id=LEAD,
        glpi_users_id=glpi_id,
        display_name="Extra A",
        is_tech=False,
        now=int(time.time()),
    )
    async with repo._tx() as db:
        await db.execute(
            "UPDATE access_approvals SET lead_glpi_id=?,lead_tg_id=?,lead_name=? WHERE ticket_id=?",
            (glpi_id, LEAD, "Extra A", TICKET),
        )
    return glpi_id


@pytest.mark.parametrize("lead_glpi_id", [EXTRA_A_GLPI, EXTRA_B_GLPI])
async def test_service_member_lead_approval_alone_completes_from_original_snapshot(
    policy, lead_glpi_id
):
    svc, client, repo, _ = policy
    bot = FakeBot()
    await _make_lead_a_service_member(repo, svc, lead_glpi_id)
    # Current configuration no longer contains the lead, but this ticket's
    # original recipient snapshot still does.
    svc.approver_ids = [999]
    await svc.send_pending(bot, TICKET)
    bot.sent.clear()

    assert await svc.approve(bot, TICKET, "lead", LEAD) == "approved"

    row = await repo.get_claude_approval(TICKET)
    assert row["stage"] == "approved"
    assert row["lead_covers_service"] == 1
    assert row["lead_approved_at"] > 0
    assert client.answer_validation.await_args.kwargs["accepted"] is True
    # Final requester message is allowed; no second-stage decision prompt was sent.
    assert not any(chat in {EXTRA_A, EXTRA_B} for chat, _, _ in bot.sent)


async def test_same_name_lead_with_unrelated_glpi_id_still_requires_extra(policy):
    svc, client, repo, _ = policy
    bot = FakeBot()
    async with repo._tx() as db:
        await db.execute(
            "UPDATE access_approvals SET lead_name='Extra A' WHERE ticket_id=?", (TICKET,)
        )

    assert await svc.approve(bot, TICKET, "lead", LEAD) == "lead_approved"

    row = await repo.get_claude_approval(TICKET)
    assert row["stage"] == "extra"
    assert row["lead_covers_service"] == 0
    assert client.answer_validation.await_count == 0
    assert any(chat in {EXTRA_A, EXTRA_B} for chat, _, _ in bot.sent)


async def test_service_lead_shortcut_survives_glpi_failure_and_restart(policy):
    svc, client, repo, _ = policy
    bot = FakeBot()
    await _make_lead_a_service_member(repo, svc, EXTRA_A_GLPI)
    client.answer_validation.side_effect = GlpiError("offline")

    assert await svc.approve(bot, TICKET, "lead", LEAD) == "retry"
    pending = await repo.get_claude_approval(TICKET)
    assert pending["stage"] == "approve_pending"
    assert pending["lead_covers_service"] == 1
    assert pending["lead_approved_at"] > 0

    db_path = repo._db_path
    await repo.close()
    restarted_repo = Repo(db_path)
    await restarted_repo.connect()
    try:
        client.answer_validation.side_effect = None
        restarted = ClaudeApprovalService(
            client, restarted_repo, approver_ids="54", schedule=svc.schedule
        )
        await restarted.tick(bot)
        row = await restarted_repo.get_claude_approval(TICKET)
        assert row["stage"] == "approved"
        assert row["lead_covers_service"] == 1
        assert client.answer_validation.await_count == 2
    finally:
        await restarted_repo.close()


async def test_old_extra_stage_with_recorded_service_lead_approval_reconciles(policy):
    svc, client, repo, _ = policy
    bot = FakeBot()
    await _make_lead_a_service_member(repo, svc, EXTRA_A_GLPI)
    assert await repo.transition_claude(
        TICKET,
        expected="lead",
        stage="extra",
        now=int(time.time()),
        actor_tg_id=LEAD,
        actor_name="Extra A",
    )
    legacy = await repo.get_claude_approval(TICKET)
    assert legacy["stage"] == "extra"
    assert legacy["lead_approved_at"] > 0
    assert legacy["lead_covers_service"] == 0

    await svc.tick(bot)

    row = await repo.get_claude_approval(TICKET)
    assert row["stage"] == "approved"
    assert row["lead_covers_service"] == 1
    assert client.answer_validation.await_args.kwargs["accepted"] is True


async def test_extra_stage_without_recorded_lead_approval_is_not_autoaccepted(policy):
    svc, client, repo, _ = policy
    bot = FakeBot()
    await _make_lead_a_service_member(repo, svc, EXTRA_A_GLPI)
    async with repo._tx() as db:
        await db.execute(
            "UPDATE claude_approvals SET stage='extra',lead_approved_at=0 WHERE ticket_id=?",
            (TICKET,),
        )

    await svc.tick(bot)

    row = await repo.get_claude_approval(TICKET)
    assert row["stage"] == "extra"
    assert row["lead_covers_service"] == 0
    assert client.answer_validation.await_count == 0
    assert client.set_ticket_status.await_count == 0


async def test_service_member_lead_rejection_remains_final(policy):
    svc, client, repo, _ = policy
    bot = FakeBot()
    await _make_lead_a_service_member(repo, svc, EXTRA_B_GLPI)

    assert await svc.reject(bot, TICKET, "lead", LEAD, "not approved") == "rejected"

    row = await repo.get_claude_approval(TICKET)
    assert row["stage"] == "rejected"
    assert row["lead_covers_service"] == 0
    assert client.answer_validation.await_args.kwargs["accepted"] is False
    client.set_ticket_status.assert_awaited_once()
    assert await svc.approve(bot, TICKET, "extra", EXTRA_A) == "stale"
