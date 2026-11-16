"""Persistent working-time reminder cadence and safe requester actions."""

from datetime import UTC, timedelta
from unittest.mock import AsyncMock

import pytest

from bot.db.repo import Repo
from bot.services import notify
from bot.services.sync import SyncService
from tests.test_sync import _SCHEDULE, REQUESTER_TG, WORKING, FakeBot, FakeClient, _ticket

TID = 714


@pytest.fixture
async def env(tmp_path):
    repo = Repo(str(tmp_path / "reminders.sqlite3"))
    await repo.connect()
    await repo.upsert_link(
        tg_id=REQUESTER_TG, glpi_users_id=38, display_name="Requester", is_tech=False, now=0
    )
    await repo.track_ticket(
        ticket_id=TID, requester_tg_id=REQUESTER_TG, requester_glpi_id=38, status=5, now=0
    )
    ticket = _ticket(TID, status=5, name="Принтер <test>")
    ticket.solvedate = WORKING.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    client = FakeClient(tickets={TID: ticket}, requesters={TID: (38, "Requester")})
    bot = FakeBot()
    clock = [WORKING]
    service = SyncService(
        bot,
        client,
        repo,
        tech_group_chat_id=None,
        schedule=_SCHEDULE,
        now_provider=lambda: clock[0],
    )
    yield repo, client, bot, clock, service, ticket
    await repo.close()


async def tick(env):
    repo, client, bot, clock, service, ticket = env
    await service._sync_ticket(await repo.get_tracked_ticket(TID))


def reminders(bot):
    return [text for chat, text in bot.sent if chat == REQUESTER_TG and "⏰" in text]


async def test_every_30_working_minutes_and_no_repeat_on_ticks(env):
    repo, client, bot, clock, service, ticket = env
    clock[0] = WORKING + timedelta(minutes=29)
    await tick(env)
    assert reminders(bot) == []
    clock[0] += timedelta(minutes=1)
    await tick(env)
    await tick(env)
    assert len(reminders(bot)) == 1
    assert "&lt;test&gt;" in reminders(bot)[0]
    clock[0] += timedelta(minutes=29)
    await tick(env)
    assert len(reminders(bot)) == 1
    clock[0] += timedelta(minutes=1)
    await tick(env)
    assert len(reminders(bot)) == 2


async def test_restart_preserves_last_delivery(env):
    repo, client, bot, clock, service, ticket = env
    clock[0] += timedelta(minutes=30)
    await tick(env)
    await repo.close()
    await repo.connect()
    restored = SyncService(
        bot,
        client,
        repo,
        tech_group_chat_id=None,
        schedule=_SCHEDULE,
        now_provider=lambda: clock[0],
    )
    await restored._sync_ticket(await repo.get_tracked_ticket(TID))
    assert len(reminders(bot)) == 1
    clock[0] += timedelta(minutes=30)
    await restored._sync_ticket(await repo.get_tracked_ticket(TID))
    assert len(reminders(bot)) == 2


async def test_nights_and_weekend_do_not_count_toward_interval(env):
    repo, client, bot, clock, service, ticket = env
    friday = WORKING.replace(day=10, hour=17, minute=45)
    ticket.solvedate = friday.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    for moment in [
        friday.replace(hour=23),
        friday.replace(day=11, hour=12),
        friday.replace(day=12, hour=12),
    ]:
        clock[0] = moment
        await tick(env)
    assert reminders(bot) == []
    clock[0] = friday.replace(day=13, hour=9, minute=14)
    await tick(env)
    assert reminders(bot) == []
    clock[0] += timedelta(minutes=1)
    await tick(env)
    assert len(reminders(bot)) == 1


@pytest.mark.parametrize("status", [2, 6, None])
async def test_stop_when_returned_closed_or_deleted(env, status):
    repo, client, bot, clock, service, ticket = env
    clock[0] += timedelta(minutes=30)
    await tick(env)
    if status is None:
        client.tickets.clear()
    else:
        ticket.status = status
    clock[0] += timedelta(minutes=30)
    await tick(env)
    assert len(reminders(bot)) == 1
    async with repo._conn.execute("SELECT count(*) FROM solution_reminders") as cur:
        assert (await cur.fetchone())[0] == 0


async def test_failed_send_is_not_marked_delivered_and_does_not_burst(env):
    repo, client, bot, clock, service, ticket = env
    clock[0] += timedelta(minutes=30)
    original = bot.send_message
    bot.send_message = AsyncMock(side_effect=RuntimeError("temporary outage"))
    await tick(env)
    await tick(env)
    bot.send_message.assert_awaited_once()
    async with repo._conn.execute(
        "SELECT last_sent_at,last_attempt_at FROM solution_reminders"
    ) as cur:
        row = await cur.fetchone()
    assert row["last_sent_at"] == 0 and row["last_attempt_at"] == int(clock[0].timestamp())
    bot.send_message = original
    clock[0] += timedelta(minutes=30)
    await tick(env)
    assert len(reminders(bot)) == 1


@pytest.mark.parametrize("change", ["requester", "link", "fresh_status"])
async def test_recipient_and_status_rechecked_before_sending(env, change):
    repo, client, bot, clock, service, ticket = env
    clock[0] += timedelta(minutes=30)
    if change == "requester":
        client.requesters[TID] = (99, "Different requester")
    if change == "link":
        await repo.upsert_link(
            tg_id=REQUESTER_TG,
            glpi_users_id=99,
            display_name="Different account",
            is_tech=False,
            now=0,
        )
    if change == "fresh_status":
        client.get_ticket = AsyncMock(return_value=_ticket(TID, status=6))
    await service._remind_solution(ticket)
    assert reminders(bot) == []


async def test_bot_prompt_starts_fresh_wait_and_adopts_glpi_timestamp(env):
    repo, client, bot, clock, service, ticket = env
    await repo.reset_solution_reminder(TID, "", int(WORKING.timestamp()), delivered=True)
    # A bot prompt was just sent even though the GLPI solution predates it.
    ticket.solvedate = (WORKING - timedelta(hours=1)).astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    await tick(env)
    assert reminders(bot) == []
    clock[0] += timedelta(minutes=30)
    await tick(env)
    assert len(reminders(bot)) == 1


async def test_web_solution_initial_notice_is_not_followed_by_immediate_nudge(env):
    repo, client, bot, clock, service, ticket = env
    await repo.set_ticket_status(TID, status=2, active=True)
    clock[0] += timedelta(hours=1)
    await tick(env)
    assert any("предложено решение" in text for _, text in bot.sent)
    assert reminders(bot) == []
    clock[0] += timedelta(minutes=30)
    await tick(env)
    assert len(reminders(bot)) == 1


async def test_unknown_solution_time_waits_from_first_observation(env):
    repo, client, bot, clock, service, ticket = env
    ticket.solvedate = None
    await tick(env)
    assert reminders(bot) == []
    clock[0] += timedelta(minutes=30)
    await tick(env)
    assert len(reminders(bot)) == 1


async def test_two_pollers_cannot_send_duplicate_reminders(env):
    import asyncio

    repo, client, bot, clock, service, ticket = env
    clock[0] += timedelta(minutes=30)
    await asyncio.gather(service._remind_solution(ticket), service._remind_solution(ticket))
    assert len(reminders(bot)) == 1


async def test_new_solution_gets_a_new_timer_and_button_generation(env):
    repo, client, bot, clock, service, ticket = env
    old = notify.solution_confirm_keyboard(TID, solution_key=ticket.solvedate)
    clock[0] += timedelta(minutes=30)
    await tick(env)
    ticket.solvedate = clock[0].astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    await tick(env)
    assert len(reminders(bot)) == 1
    new = notify.solution_confirm_keyboard(TID, solution_key=ticket.solvedate)
    assert old.inline_keyboard[0][0].callback_data != new.inline_keyboard[0][0].callback_data
    clock[0] += timedelta(minutes=30)
    await tick(env)
    assert len(reminders(bot)) == 2


async def test_no_send_when_api_check_crosses_end_of_workday(env):
    repo, client, bot, clock, service, ticket = env
    clock[0] = WORKING.replace(hour=17, minute=59)

    async def requester(_):
        clock[0] = WORKING.replace(hour=18, minute=0)
        return (38, "Requester")

    client.get_ticket_requester = requester
    await tick(env)
    assert reminders(bot) == []
