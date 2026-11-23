"""Offline review actions, durable decisions and single-text card rendering."""

from __future__ import annotations

import asyncio
import html
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram import Dispatcher

from bot import texts
from bot.db.repo import Repo
from bot.glpi.client import GlpiClient, GlpiError
from bot.glpi.models import ITILCategory, Ticket
from bot.handlers.intake_review import build_intake_review_router
from bot.handlers.tech_actions import build_tech_actions_router
from bot.services import notify
from bot.services.cards import CardService
from bot.services.intake import CATEGORY_NAMES
from bot.services.intake_snapshot import glpi_plain_text, refresh_intake_snapshot
from bot.services.tech_approval import ticket_digest
from bot.texts import en, ru
from tests import test_tech_approval as legacy_approval
from tests.test_access import FakeBot, _cb
from tests.test_tech_approval import state as approval_state

approval_case = legacy_approval.env

TID, TECH = 901, 2002
IDS = dict(zip(CATEGORY_NAMES, range(101, 108), strict=True))


def decision(**changes):
    data = {
        "title": "Auto title must not repeat",
        "category": "accounts",
        "category_id": IDS["accounts"],
        "environment": "unknown",
        "approval_required": False,
        "reason": "Existing account repair",
        "model": "test-model",
        "prompt_version": "test-v1",
        "status": "ok",
        "original_description": "Repair my existing account; error <denied> & code 401.",
    }
    return data | changes


@pytest.fixture
async def env(tmp_path):
    repo = Repo(str(tmp_path / "intake.sqlite3"))
    await repo.connect()
    repo.intake_category_ids = IDS
    await repo.save_ticket_intake(TID, decision(), 1)
    client = AsyncMock()
    ticket = Ticket(
        TID,
        decision()["title"],
        html.escape(decision()["original_description"]),
        1,
        3,
        IDS["accounts"],
    )
    client.get_ticket.return_value = ticket
    client.list_categories.return_value = [
        ITILCategory(v, CATEGORY_NAMES[k], CATEGORY_NAMES[k]) for k, v in IDS.items()
    ]

    async def set_category(tid, category_id):
        ticket.itilcategories_id = category_id

    client.set_ticket_category.side_effect = set_category
    cards = AsyncMock()
    dp = Dispatcher()
    dp.include_router(
        build_intake_review_router(
            client,
            repo,
            intake=SimpleNamespace(category_id=IDS.__getitem__),
            cards=cards,
        )
    )
    bot = FakeBot()
    link = SimpleNamespace(tg_id=TECH, glpi_users_id=9, is_tech=True, display_name="Technician")
    yield SimpleNamespace(
        repo=repo, client=client, ticket=ticket, cards=cards, dp=dp, bot=bot, link=link
    )
    await dp.storage.close()
    await repo.close()


async def click(env, data, update=1):
    await env.dp.feed_update(env.bot, _cb(env.bot, update, TECH, data), link=env.link)


async def test_routine_review_once_preserves_original_ai_result(env):
    await click(env, f"ir:routine:{TID}:1")
    row = await env.repo.get_ticket_intake(TID)
    assert row["review_state"] == "routine" and row["version"] == 2
    assert row["reviewed_by"] == TECH
    await env.repo.save_ticket_intake(TID, decision(approval_required=True), 10)
    assert (await env.repo.get_ticket_intake(TID))["approval_required"] is False
    await click(env, f"ir:routine:{TID}:1", 2)
    assert texts.INTAKE_REVIEW_STALE in env.bot.toasts
    assert len(await env.repo.list_ticket_intake_events(TID)) == 3
    env.client.set_ticket_status.assert_not_awaited()
    env.client.create_validation.assert_not_awaited()


async def test_nontech_cannot_review_or_change_category(env):
    env.link.is_tech = False
    await click(env, f"ir:routine:{TID}:1")
    await click(env, f"ir:set:{TID}:1:101:network", 2)
    env.client.get_ticket.assert_not_awaited()
    env.client.set_ticket_category.assert_not_awaited()
    assert env.bot.toasts == [texts.TECH_ONLY, texts.TECH_ONLY]


@pytest.mark.parametrize("status", [5, 6])
async def test_closed_and_solved_tickets_cannot_be_reviewed(env, status):
    env.ticket.status = status
    await click(env, f"ir:routine:{TID}:1")
    assert (await env.repo.get_ticket_intake(TID))["review_state"] == "pending"
    assert texts.INTAKE_REVIEW_CLOSED in env.bot.toasts


@pytest.mark.parametrize("kind", ["glpi", "uncertain_post"])
async def test_routine_never_cancels_existing_or_uncertain_approval(env, kind):
    if kind == "glpi":
        env.ticket.global_validation = 2
    else:
        await env.repo.reserve_tech_approval(TID, "test", "{}", 1)
        await env.repo.update_tech_approval(TID, "posting")
    await click(env, f"ir:routine:{TID}:1")
    assert texts.INTAKE_REVIEW_APPROVAL_EXISTS in env.bot.toasts
    assert (await env.repo.get_ticket_intake(TID))["review_state"] == "pending"


async def test_category_change_updates_glpi_and_audit_without_changing_review(env):
    await click(env, f"ir:set:{TID}:1:101:network")
    env.client.set_ticket_category.assert_awaited_once_with(TID, 102)
    row = await env.repo.get_ticket_intake(TID)
    assert (row["category"], row["category_id"], row["version"]) == ("network", 102, 2)
    assert row["initial_category"] == "accounts" and row["review_state"] == "pending"
    assert (await env.repo.list_ticket_intake_events(TID))[-1]["event"] == "category_changed"
    await click(env, f"ir:set:{TID}:1:101:software", 2)
    assert env.client.set_ticket_category.await_count == 1


async def test_external_category_change_invalidates_old_selection(env):
    env.ticket.itilcategories_id = 106
    await click(env, f"ir:set:{TID}:1:101:network")
    env.client.set_ticket_category.assert_not_awaited()
    row = await env.repo.get_ticket_intake(TID)
    assert row["category"] == "purchases" and row["version"] == 2
    assert texts.INTAKE_REVIEW_STALE in env.bot.toasts


async def test_category_must_exist_with_exact_configured_name(env):
    env.client.list_categories.return_value = [ITILCategory(102, "Old unrelated name", "Old")]
    await click(env, f"ir:set:{TID}:1:101:network")
    env.client.set_ticket_category.assert_not_awaited()
    assert texts.INTAKE_CATEGORY_UNAVAILABLE in env.bot.toasts


async def test_daily_budget_atomic_persistent_and_utc_rollover(env):
    result = await asyncio.gather(*(env.repo.reserve_intake_call(100, 3) for _ in range(15)))
    assert sum(result) == 3
    await env.repo.close()
    await env.repo.connect()
    assert not await env.repo.reserve_intake_call(86399, 3)
    assert await env.repo.reserve_intake_call(86400, 3)
    assert (await env.repo.get_ticket_intake(TID))["title"] == decision()["title"]


@pytest.mark.parametrize("module", [ru, en])
def test_single_text_render_keeps_literal_input_not_auto_title_and_fits_telegram(module):
    source = "<error> & Проблема\n\n" + "😀&<" * 1600
    result = module.notify_new_ticket(
        ticket_id=TID,
        title="Never repeat this title",
        description="irrelevant GLPI markup",
        status=1,
        urgency=5,
        url="https://glpi.invalid/ticket/901",
        requester_name="<Requester>",
        requester_tg_id=123,
        history=["event " + "X" * 400] * 10,
        intake=decision(
            original_description=source,
            review_state="pending",
            version=1,
            environment="development",
        ),
    )
    assert "Never repeat this title" not in result
    assert "&lt;error&gt; &amp; Проблема\n\n" in result
    assert "tg://user?id=123" in result
    assert "(прод)" not in result and "(prod)" not in result
    assert len(result.encode("utf-16-le")) // 2 < 4096
    assert result.count("&lt;error&gt;") == 1
    assert "https://glpi.invalid/ticket/901" in result


async def test_first_send_and_card_rerender_keep_review_and_single_text(env):
    row = await env.repo.get_ticket_intake(TID)
    cards = CardService(env.repo, front_base="https://glpi.invalid")
    await notify.notify_new_ticket(
        env.bot,
        -100,
        env.ticket,
        "https://glpi.invalid",
        intake=row,
        requester_name="Requester",
        requester_tg_id=123,
    )
    await cards.register(
        env.ticket,
        chat_id=-100,
        message_id=10,
        requester_name="Requester",
        requester_tg_id=123,
        attachments_count=0,
        now=1,
    )
    await cards.record_event(env.bot, TID, "Taken", status=2)
    for _, text, keyboard in env.bot.sent:
        assert decision()["title"] not in text
        assert "error &lt;denied&gt; &amp; code 401." in text
        assert "tg://user?id=123" in text
        callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        assert f"ir:routine:{TID}:1" in callbacks
        assert f"ir:approval:{TID}:1" in callbacks
        assert f"ir:category:{TID}:1" in callbacks


async def test_set_category_client_only_changes_category(respx_mock):
    base = "https://glpi.invalid/apirest.php"
    respx_mock.get(base + "/initSession").respond(200, json={"session_token": "test"})
    route = respx_mock.put(base + f"/Ticket/{TID}").respond(200, json={"id": TID})
    client = GlpiClient(base, "test", "test", timeout=1)
    try:
        await client.set_ticket_category(TID, 102)
        assert route.call_count == 1
        assert (
            route.calls[0].request.content
            == httpx.Request(
                "PUT", base, json={"input": {"id": TID, "itilcategories_id": 102}}
            ).content
        )
    finally:
        await client.close()


@pytest.mark.parametrize("changed", [False, True])
async def test_review_approval_uses_existing_flow_and_rechecks_version(approval_case, changed):
    dp, client, repo, bot, ticket = approval_case
    tid = ticket.id
    ticket.itilcategories_id = IDS["accounts"]
    await repo.save_ticket_intake(
        tid, decision(title=ticket.name, original_description=ticket.content), 1
    )
    await dp.feed_update(bot, _cb(bot, 1, TECH, f"ir:approval:{tid}:1"))
    data = await approval_state(dp).get_data()
    assert data["intake_review_version"] == 1
    await dp.feed_update(bot, _cb(bot, 2, TECH, f"tap:lead:{data['nonce']}:19"))
    if changed:
        await repo.update_ticket_intake(
            tid,
            expected_version=1,
            actor_tg_id=TECH,
            now=5,
            event="routine",
            review_state="routine",
        )
    data = await approval_state(dp).get_data()
    await dp.feed_update(bot, _cb(bot, 3, TECH, f"tap:send:{data['nonce']}"))
    if changed:
        client.create_validation.assert_not_awaited()
        assert any(texts.INTAKE_REVIEW_STALE in text for _, text, _ in bot.sent)
    else:
        client.create_validation.assert_awaited_once()
        row = await repo.get_ticket_intake(tid)
        assert row["review_state"] == "approval" and row["version"] == 2
        assert (await repo.get_approval(tid))["status"] == 0  # lead has not approved yet
        await dp.feed_update(bot, _cb(bot, 4, TECH, f"ir:approval:{tid}:1"))
        assert client.create_validation.await_count == 1
    client.create_ticket.assert_not_awaited()


@pytest.mark.parametrize("deleted", [True, 1, "1"])
def test_deleted_category_marker_survives_api_parsing(deleted):
    category = ITILCategory.from_api({"id": 1, "name": "Test", "is_deleted": deleted})
    assert category.is_deleted is True
    assert ITILCategory(1, "Test", "Test").is_deleted is False


async def test_cleared_glpi_category_can_be_corrected_after_refresh(env):
    env.ticket.itilcategories_id = None
    await click(env, f"ir:category:{TID}:1")
    row = await env.repo.get_ticket_intake(TID)
    assert row["category_id"] == 0 and row["version"] == 2
    await click(env, f"ir:set:{TID}:2:0:network", 2)
    env.client.set_ticket_category.assert_awaited_once_with(TID, 102)


@pytest.mark.parametrize("review", ["routine", "approval"])
async def test_external_text_refreshes_display_and_retains_original(env, review):
    await env.repo.update_ticket_intake(
        TID, expected_version=1, actor_tg_id=TECH, now=2, event=review, review_state=review
    )
    env.ticket.name = "Manually corrected title"
    env.ticket.content = "<p>New details &lt;error&gt;<br>Second line</p>"
    row = await refresh_intake_snapshot(env.repo, env.ticket)
    assert row["version"] == 3 and row["ai_stale"] is True
    assert row["review_state"] == "pending"
    assert row["approval_recheck"] is (review == "approval")
    assert row["original_description"] == decision()["original_description"]
    assert row["display_description"] == "New details <error>\nSecond line"
    rendered = ru.notify_new_ticket(
        ticket_id=TID,
        title=env.ticket.name,
        status=1,
        url=None,
        description=env.ticket.content,
        intake=row,
    )
    assert "Manually corrected title" in rendered and "New details &lt;error&gt;" in rendered
    assert decision()["original_description"] not in rendered
    unchanged = await refresh_intake_snapshot(env.repo, env.ticket)
    assert unchanged["version"] == 3


async def test_changed_text_refuses_stale_routine_decision(env):
    env.ticket.content = "New content"
    await click(env, f"ir:routine:{TID}:1")
    row = await env.repo.get_ticket_intake(TID)
    assert row["review_state"] == "pending" and row["version"] == 2
    assert texts.INTAKE_REVIEW_STALE in env.bot.toasts
    await click(env, f"ir:routine:{TID}:2", 2)
    assert (await env.repo.get_ticket_intake(TID))["review_state"] == "routine"


@pytest.mark.parametrize("wrapped", [False, True])
def test_glpi_html_canonicalization_preserves_literal_fragments(wrapped):
    original = "Ошибка <denied>\n\nСтрока & 401"
    html_body = "<p>Ошибка &lt;denied&gt;<br><br>Строка &amp; 401</p>"
    if wrapped:
        html_body = html.escape(html_body)
    assert glpi_plain_text(html_body, known_plain=original) == original
    assert glpi_plain_text(html.escape(original), known_plain=original) == original


@pytest.mark.parametrize("reviewed", [False, True])
async def test_missing_metadata_recovery_and_late_ai_result_do_not_erase_review(env, reviewed):
    env.repo.intake_category_ids = IDS
    ticket = Ticket(902, "Manual GLPI title", "Manual content", 1, 3, IDS["accounts"])
    row = await refresh_intake_snapshot(env.repo, ticket)
    assert row["metadata_recovered"] is True and row["review_state"] == "pending"
    if reviewed:
        await env.repo.update_ticket_intake(
            902,
            expected_version=1,
            actor_tg_id=TECH,
            now=3,
            event="routine",
            review_state="routine",
        )
    await env.repo.save_ticket_intake(902, decision(approval_required=True), 4)
    row = await env.repo.get_ticket_intake(902)
    if reviewed:
        assert row["review_state"] == "routine" and row["title_generated"] is False
        events = await env.repo.list_ticket_intake_events(902)
        assert events[-1]["event"] == "late_classification"
    else:
        assert row["approval_required"] is True and row["review_state"] == "pending"
        assert row["version"] == 2  # old recovery buttons invalidated


def test_intake_card_keeps_external_waiting_validation_label():
    text = ru.notify_new_ticket(
        ticket_id=1,
        title="T",
        status=1,
        url=None,
        awaiting_approval=True,
        intake=decision(review_state="pending"),
    )
    assert ru.CARD_AWAITING_APPROVAL in text


def test_cleared_description_falls_back_to_existing_title():
    data = decision(review_state="pending", display_description="", ai_stale=True)
    text = ru.notify_new_ticket(ticket_id=1, title=data["title"], status=1, url=None, intake=data)
    assert data["title"] in text


async def test_explicit_failed_approval_resets_review_and_allows_retry(approval_case):
    dp, client, repo, bot, ticket = approval_case
    ticket.itilcategories_id = IDS["accounts"]
    await repo.save_ticket_intake(
        ticket.id, decision(title=ticket.name, original_description=ticket.content), 1
    )
    await dp.feed_update(bot, _cb(bot, 1, TECH, f"ir:approval:{ticket.id}:1"))
    data = await approval_state(dp).get_data()
    await dp.feed_update(bot, _cb(bot, 2, TECH, f"tap:lead:{data['nonce']}:19"))
    reads_at_failure = []

    async def fail_and_make_reads_unavailable(*args, **kwargs):
        reads_at_failure.append(client.get_ticket.await_count)
        client.get_ticket.side_effect = GlpiError("GLPI read unavailable")
        raise GlpiError("rejected", response=httpx.Response(400))

    client.create_validation.side_effect = fail_and_make_reads_unavailable
    await dp.feed_update(bot, _cb(bot, 3, TECH, f"tap:send:{data['nonce']}"))
    row = await repo.get_ticket_intake(ticket.id)
    assert row["review_state"] == "pending" and row["version"] == 3
    assert client.get_ticket.await_count == reads_at_failure[0]
    assert (await repo.get_tech_approval(ticket.id))["phase"] == "failed"
    client.get_ticket.side_effect = None
    await dp.feed_update(bot, _cb(bot, 4, TECH, f"ir:approval:{ticket.id}:3"))
    assert (await approval_state(dp).get_data())["intake_review_version"] == 3
    assert client.create_validation.await_count == 1


async def test_cancelled_before_post_resets_review_without_touching_validation(approval_case):
    dp, client, repo, bot, ticket = approval_case
    ticket.itilcategories_id = IDS["accounts"]
    await repo.save_ticket_intake(
        ticket.id, decision(title=ticket.name, original_description=ticket.content), 1
    )
    payload = dict(
        ticket_id=ticket.id,
        tech_tg_id=TECH,
        lead_glpi_id=19,
        digest=ticket_digest(ticket),
        requester_glpi_id=8,
    )
    await repo.reserve_tech_approval(ticket.id, "prepared", json.dumps(payload), 1)
    ticket.content = "Content changed before any POST"
    assert await dp["tech_approvals"]._advance(bot, ticket.id) == "cancelled"
    row = await repo.get_ticket_intake(ticket.id)
    assert row["review_state"] == "pending"
    client.create_validation.assert_not_awaited()
    assert (await repo.get_tech_approval(ticket.id))["phase"] == "cancelled"


async def test_old_keyboard_after_late_metadata_refreshes_card_without_glpi_read(env):
    await env.repo.update_ticket_intake(
        TID, expected_version=1, actor_tg_id=TECH, now=5, event="routine", review_state="routine"
    )
    env.client.get_ticket.side_effect = GlpiError("unavailable")
    await click(env, f"ir:category:{TID}:1")
    env.cards.record_event.assert_awaited_once_with(env.bot, TID, None)
    env.client.get_ticket.assert_not_awaited()
    assert texts.INTAKE_REVIEW_STALE in env.bot.toasts


async def test_old_approval_keyboard_refreshes_card_before_glpi_read(approval_case):
    dp, client, repo, bot, ticket = approval_case
    await repo.save_ticket_intake(ticket.id, decision(), 1)
    await repo.update_ticket_intake(
        ticket.id,
        expected_version=1,
        actor_tg_id=TECH,
        now=5,
        event="routine",
        review_state="routine",
    )
    cards = AsyncMock()
    dp["tech_approvals"].cards = cards
    client.get_ticket.side_effect = GlpiError("unavailable")
    await dp.feed_update(bot, _cb(bot, 1, TECH, f"ir:approval:{ticket.id}:1"))
    cards.record_event.assert_awaited_once_with(bot, ticket.id, None)
    client.get_ticket.assert_not_awaited()
    assert texts.INTAKE_REVIEW_STALE in bot.toasts


async def _approved_before_edit(env, *, claude=False):
    common = dict(
        validation_id=55,
        lead_glpi_id=19,
        lead_tg_id=3003,
        lead_name="Lead",
        requester_tg_id=1001,
        now=2,
    )
    if claude:
        await env.repo.create_claude_approval(
            TID, **common, requester_glpi_id=8, request_text="Original", members=[]
        )
    else:
        await env.repo.create_approval(TID, **common)
    await env.repo.set_approval_status(TID, status=1)
    await env.repo.update_ticket_intake(
        TID,
        expected_version=1,
        actor_tg_id=TECH,
        now=3,
        event="approval_requested",
        review_state="approval",
    )
    env.ticket.global_validation = 3
    env.ticket.content = "Changed after the original approval"
    return await refresh_intake_snapshot(env.repo, env.ticket)


async def test_edited_granted_ticket_blocks_work_until_explicit_technician_recheck(env):
    env.dp.include_router(build_tech_actions_router(env.client, repo=env.repo, cards=env.cards))
    row = await _approved_before_edit(env)
    assert row["review_state"] == "pending" and row["approval_recheck"] is True
    await click(env, f"ta:take:{TID}")
    env.client.assign_ticket.assert_not_awaited()
    await click(env, f"ir:routine:{TID}:{row['version']}", 2)
    row = await env.repo.get_ticket_intake(TID)
    assert row["review_state"] == "routine"
    assert (await env.repo.list_ticket_intake_events(TID))[-1]["event"] == "routine_recheck"
    await click(env, f"ta:take:{TID}", 3)
    env.client.assign_ticket.assert_awaited_once()
    env.client.create_validation.assert_not_awaited()
    env.client.answer_validation.assert_not_awaited()
    assert (await env.repo.get_approval(TID))["status"] == 1


@pytest.mark.parametrize("cause", ["waiting", "rejected", "ledger_pending", "posting", "claude"])
async def test_explicit_recheck_never_bypasses_unfinished_or_rejected_approval(env, cause):
    row = await _approved_before_edit(env, claude=cause == "claude")
    if cause == "waiting":
        env.ticket.global_validation = 2
    elif cause == "rejected":
        env.ticket.global_validation = 4
    elif cause == "ledger_pending":
        await env.repo.set_approval_status(TID, status=0)
    elif cause == "posting":
        await env.repo.reserve_tech_approval(TID, "test", "{}", 4)
        await env.repo.update_tech_approval(TID, "posting")
    await click(env, f"ir:routine:{TID}:{row['version']}")
    assert (await env.repo.get_ticket_intake(TID))["review_state"] == "pending"
    assert texts.INTAKE_REVIEW_APPROVAL_EXISTS in env.bot.toasts
    env.client.create_validation.assert_not_awaited()


async def test_completed_claude_approval_permits_explicit_recheck(env):
    row = await _approved_before_edit(env, claude=True)
    await env.repo.finish_claude(TID, accepted=True)
    await click(env, f"ir:routine:{TID}:{row['version']}")
    assert (await env.repo.get_ticket_intake(TID))["review_state"] == "routine"


async def test_approval_recovery_does_not_restore_review_after_edited_text(approval_case):
    dp, client, repo, bot, ticket = approval_case
    ticket.itilcategories_id = IDS["accounts"]
    await repo.save_ticket_intake(
        ticket.id, decision(title=ticket.name, original_description=ticket.content), 1
    )
    await repo.create_approval(
        ticket.id,
        validation_id=55,
        lead_glpi_id=19,
        lead_tg_id=3003,
        lead_name="Lead",
        requester_tg_id=1001,
        now=1,
    )
    await repo.set_approval_status(ticket.id, status=1)
    await repo.update_ticket_intake(
        ticket.id,
        expected_version=1,
        actor_tg_id=TECH,
        now=2,
        event="approval_requested",
        review_state="approval",
    )
    payload = dict(
        ticket_id=ticket.id,
        tech_tg_id=TECH,
        lead_glpi_id=19,
        digest=ticket_digest(ticket),
        requester_glpi_id=8,
    )
    await repo.reserve_tech_approval(ticket.id, "registered", json.dumps(payload), 1)
    await repo.update_tech_approval(ticket.id, "registered", 55)
    ticket.global_validation = 3
    ticket.content = "Changed after approval"
    row = await refresh_intake_snapshot(repo, ticket)
    assert row["review_state"] == "pending" and row["approval_recheck"] is True
    assert await dp["tech_approvals"]._advance(bot, ticket.id) == "done"
    assert (await repo.get_ticket_intake(ticket.id))["review_state"] == "pending"
