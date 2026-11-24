"""Release regressions for ordinary approval flows; GLPI and Telegram are fake."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from bot.glpi.client import GlpiClient, GlpiError
from bot.services.tech_approval import TechApprovalService
from tests import test_tech_approval as approval_tests
from tests.test_access import _cb, _dm
from tests.test_handoff import _group_cb

pytestmark = pytest.mark.asyncio
approval_env = approval_tests.env


async def test_rejection_retry_finishes_close_after_validation_was_already_refused(approval_env):
    dp, client, repo, bot, ticket = approval_env
    data = await approval_tests.confirm(approval_env)
    await dp.feed_update(bot, _cb(bot, 3, approval_tests.TECH, f"tap:send:{data['nonce']}"))

    async def refuse_validation(*args, **kwargs):
        assert kwargs["accepted"] is False
        ticket.global_validation = 4
        client.get_validation_decision.return_value = {
            "id": args[0],
            "tickets_id": ticket.id,
            "status": 4,
            "comment_validation": kwargs["comment"],
        }

    close_attempts = 0

    async def close_ticket(ticket_id, status):
        nonlocal close_attempts
        close_attempts += 1
        if close_attempts == 1:
            raise GlpiError("Temporary failure closing an already refused ticket")
        ticket.status = status

    client.answer_validation.side_effect = refuse_validation
    client.set_ticket_status.side_effect = close_ticket
    await dp.feed_update(bot, _cb(bot, 4, approval_tests.LEAD, f"ap:no:{ticket.id}"))
    await dp.feed_update(bot, _dm(bot, 5, approval_tests.LEAD, "Нет бюджета"))
    assert ticket.global_validation == 4 and ticket.status == 2

    # The lead retries after the temporary error. The accepted refusal must
    # finish closing the original ticket, without repeating the decision PUT.
    await dp.feed_update(bot, _dm(bot, 6, approval_tests.LEAD, "Нет бюджета"))
    assert ticket.status == 6, (
        f"Ticket stayed open; close attempts={close_attempts}, "
        f"local approval status={(await repo.get_approval(ticket.id))['status']}"
    )
    client.answer_validation.assert_awaited_once()
    assert (await repo.get_approval(ticket.id))["status"] == -1


async def test_group_approval_prompt_cannot_overwrite_new_private_draft(approval_env):
    dp, client, repo, bot, ticket = approval_env
    prompt_started, release_prompt = asyncio.Event(), asyncio.Event()
    original_send = bot.send_message

    async def slow_prompt(chat_id, text, **kwargs):
        if chat_id == approval_tests.TECH:
            prompt_started.set()
            await release_prompt.wait()
        return await original_send(chat_id, text, **kwargs)

    bot.send_message = slow_prompt
    opening = asyncio.create_task(
        dp.feed_update(
            bot,
            _group_cb(bot, 1, approval_tests.TECH, f"ta:approval:{ticket.id}"),
        )
    )
    draft = "Не работает VPN, сохраните это описание"
    try:
        await asyncio.wait_for(prompt_started.wait(), timeout=2)
        await dp.feed_update(bot, _dm(bot, 2, approval_tests.TECH, draft))
        state = approval_tests.state(dp)
        assert (await state.get_data())["description"] == draft
    finally:
        release_prompt.set()
        await opening

    assert await state.get_state() is None
    assert (await state.get_data())["description"] == draft
    client.create_validation.assert_not_awaited()
    assert await repo.get_approval(ticket.id) is None


async def test_existing_free_text_offer_is_preserved_before_approval_dm(approval_env):
    dp, client, repo, bot, ticket = approval_env
    await dp.feed_update(bot, _dm(bot, 1, approval_tests.TECH, "Мой черновик"))
    before = len(bot.sent)
    await dp.feed_update(bot, _group_cb(bot, 2, approval_tests.TECH, f"ta:approval:{ticket.id}"))
    assert len(bot.sent) == before
    assert await approval_tests.state(dp).get_data() == {"description": "Мой черновик"}
    assert await approval_tests.state(dp).get_state() is None


@pytest.mark.parametrize("failure", ["lost_decision_response", "close_failed"])
async def test_rejection_closure_recovers_after_restart_without_second_decision(
    approval_env, failure
):
    dp, client, repo, bot, ticket = approval_env
    data = await approval_tests.confirm(approval_env)
    await dp.feed_update(bot, _cb(bot, 3, approval_tests.TECH, f"tap:send:{data['nonce']}"))

    async def refuse(validation_id, *, accepted, comment):
        saved = await repo.get_rejection_closure(ticket.id)
        assert saved["phase"] == "posting" and saved["token"] in comment
        ticket.global_validation = 4
        client.get_validation_decision.return_value = {
            "id": validation_id,
            "tickets_id": ticket.id,
            "status": 4,
            "comment_validation": comment,
        }
        if failure == "lost_decision_response":
            raise GlpiError("Response lost after successful PUT")

    client.answer_validation.side_effect = refuse
    client.set_ticket_status.side_effect = GlpiError("Temporary close failure")
    await dp.feed_update(bot, _cb(bot, 4, approval_tests.LEAD, f"ap:no:{ticket.id}"))
    await dp.feed_update(bot, _dm(bot, 5, approval_tests.LEAD, "Нет бюджета"))
    assert not await repo.pending_approvals()  # No reminder may mark this intent moot.
    # A reminder which already fetched its pending list must not race the intent.
    await repo.set_approval_status(ticket.id, status=-2)
    assert (await repo.get_approval(ticket.id))["status"] == 0
    await repo.close()
    await repo.connect()

    async def close(tid, status):
        ticket.status = status

    client.set_ticket_status.side_effect = close
    restored = TechApprovalService(client, repo, dp["claude_approvals"])
    await restored.tick(bot)
    assert ticket.status == 6
    assert (await repo.get_approval(ticket.id))["status"] == -1
    assert (await repo.get_rejection_closure(ticket.id))["phase"] == "done"
    assert not await repo.pending_rejection_closures()
    client.answer_validation.assert_awaited_once()
    before = client.set_ticket_status.await_count
    await restored.tick(bot)
    assert client.set_ticket_status.await_count == before


@pytest.mark.parametrize("evidence_change", ["external_refusal", "other_validation", "other_owner"])
async def test_recovery_never_closes_on_unrelated_refusal_or_changed_owner(
    approval_env, evidence_change
):
    dp, client, repo, bot, ticket = approval_env
    data = await approval_tests.confirm(approval_env)
    await dp.feed_update(bot, _cb(bot, 3, approval_tests.TECH, f"tap:send:{data['nonce']}"))
    client.answer_validation.side_effect = GlpiError("Unknown outcome")
    await dp.feed_update(bot, _cb(bot, 4, approval_tests.LEAD, f"ap:no:{ticket.id}"))
    await dp.feed_update(bot, _dm(bot, 5, approval_tests.LEAD, "Нет бюджета"))
    saved = await repo.get_rejection_closure(ticket.id)
    ticket.global_validation = 4
    client.get_validation_decision.return_value = {
        "id": saved["validation_id"],
        "tickets_id": ticket.id,
        "status": 4,
        "comment_validation": f"[TG rejection:{saved['token']}]",
    }
    if evidence_change == "external_refusal":
        client.get_validation_decision.return_value["comment_validation"] = "External decision"
    elif evidence_change == "other_validation":
        client.get_validation_decision.return_value["id"] += 1
    else:
        async with repo._tx() as db:
            await db.execute(
                "UPDATE access_approvals SET lead_glpi_id=99 WHERE ticket_id=?", (ticket.id,)
            )
    await dp["tech_approvals"].tick(bot)
    client.set_ticket_status.assert_not_awaited()
    client.answer_validation.assert_awaited_once()
    assert (await repo.get_approval(ticket.id))["status"] == 0


async def test_waiting_unknown_outcome_requires_explicit_lead_retry_after_restart(approval_env):
    dp, client, repo, bot, ticket = approval_env
    data = await approval_tests.confirm(approval_env)
    await dp.feed_update(bot, _cb(bot, 3, approval_tests.TECH, f"tap:send:{data['nonce']}"))
    client.answer_validation.side_effect = GlpiError("Request did not reach GLPI")
    await dp.feed_update(bot, _cb(bot, 4, approval_tests.LEAD, f"ap:no:{ticket.id}"))
    await dp.feed_update(bot, _dm(bot, 5, approval_tests.LEAD, "Нет бюджета"))
    saved = await repo.get_rejection_closure(ticket.id)
    client.get_validation_decision.return_value = {
        "id": saved["validation_id"],
        "tickets_id": ticket.id,
        "status": 2,
        "comment_validation": "",
    }
    await approval_tests.state(dp, approval_tests.LEAD).clear()  # Process-local FSM was lost.
    await repo.close()
    await repo.connect()
    await dp["tech_approvals"].tick(bot)
    client.answer_validation.assert_awaited_once()
    # A competing approve click cannot replace the outstanding explicit refusal.
    await dp.feed_update(bot, _cb(bot, 6, approval_tests.LEAD, f"ap:ok:{ticket.id}"))
    client.answer_validation.assert_awaited_once()
    client.answer_validation.side_effect = None
    await dp.feed_update(bot, _cb(bot, 7, approval_tests.LEAD, f"ap:no:{ticket.id}"))
    await dp.feed_update(bot, _dm(bot, 8, approval_tests.LEAD, "Нет бюджета"))
    assert client.answer_validation.await_count == 2
    assert client.answer_validation.await_args.kwargs["accepted"] is False
    assert saved["token"] in client.answer_validation.await_args.kwargs["comment"]
    client.set_ticket_status.assert_awaited_once_with(ticket.id, 6)


@pytest.mark.parametrize("bad", [False, "id", "status", "ticket", "json"])
async def test_exact_validation_decision_reader_rejects_malformed_evidence(bad):
    base = "https://glpi.invalid/apirest.php"
    data = {"id": 55, "tickets_id": 714, "status": 4, "comment_validation": "proof"}
    if bad == "id":
        data["id"] = 56
    if bad == "status":
        data["status"] = True
    if bad == "ticket":
        del data["tickets_id"]
    with respx.mock as api:
        api.get(base + "/initSession").respond(200, json={"session_token": "test"})
        response = (
            httpx.Response(200, text="invalid") if bad == "json" else httpx.Response(200, json=data)
        )
        api.get(base + "/TicketValidation/55").mock(return_value=response)
        client = GlpiClient(base, "test", "test")
        try:
            if bad:
                with pytest.raises(GlpiError):
                    await client.get_validation_decision(55)
            else:
                assert await client.get_validation_decision(55) == data
        finally:
            await client.close()
