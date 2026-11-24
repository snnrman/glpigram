"""A group card survives failed delivery/cursor writes without known duplicates."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from bot.glpi.client import GlpiError
from tests import test_sync

repo = test_sync.repo


class FailsFirstCard(test_sync.FakeBot):
    def __init__(self):
        super().__init__()
        self.failed = False

    async def send_message(self, chat_id, text, **kwargs):
        if "🆕" in text and not self.failed:
            self.failed = True
            raise RuntimeError("temporary Telegram failure")
        return await super().send_message(chat_id, text, **kwargs)


async def test_daytime_failure_is_durable_and_retried_without_quiet_hours_header(repo):
    ticket = test_sync._ticket(2)
    client = test_sync.FakeClient(recent=[ticket], tickets={2: ticket})
    bot = FailsFirstCard()
    await repo.set_cursor("last_ticket_id", 1)
    await test_sync._service(bot, client, repo)._poll_new_tickets()
    assert await repo.get_cursor("last_ticket_id") == 2
    assert [kind for _, kind, _ in await repo.list_deferred()] == ["card_retry"]

    # A new service instance models restart after the cursor already advanced.
    restarted = test_sync._service(bot, client, repo)
    await restarted._flush_deferred_if_working()
    await restarted._poll_new_tickets()
    assert await repo.list_deferred() == []
    assert len(bot.sent) == 1 and "🆕" in bot.sent[0][1]
    assert await repo.get_card(2) is not None


async def test_urgent_retry_runs_at_night_without_reading_ordinary_backlog(repo):
    urgent = test_sync._ticket(2, urgency=5)
    ordinary = test_sync._ticket(3)
    client = test_sync.FakeClient(recent=[urgent, ordinary], tickets={2: urgent, 3: ordinary})
    bot = FailsFirstCard()
    await repo.set_cursor("last_ticket_id", 1)
    service = test_sync._service(bot, client, repo, now=test_sync.OFFHOURS_NIGHT)
    await service._poll_new_tickets()
    assert {(kind, tid) for _, kind, tid in await repo.list_deferred()} == {
        ("urgent_retry", 2),
        ("new", 3),
    }
    client.get_ticket = AsyncMock(wraps=client.get_ticket)

    await service._flush_deferred_if_working()

    client.get_ticket.assert_awaited_once_with(2)
    assert len(bot.sent) == 1 and "№2" in bot.sent[0][1]
    assert [(kind, tid) for _, kind, tid in await repo.list_deferred()] == [("new", 3)]


async def test_cursor_failure_does_not_resend_an_already_registered_card(repo):
    ticket = test_sync._ticket(2)
    client = test_sync.FakeClient(recent=[ticket], tickets={2: ticket})
    bot = test_sync.FakeBot()
    service = test_sync._service(bot, client, repo)
    await repo.set_cursor("last_ticket_id", 1)
    original = repo.set_cursor
    repo.set_cursor = AsyncMock(side_effect=RuntimeError("cursor write unavailable"))
    with pytest.raises(RuntimeError):
        await service._poll_new_tickets()
    repo.set_cursor = original

    await test_sync._service(bot, client, repo)._poll_new_tickets()

    assert len(bot.sent) == 1
    assert await repo.get_cursor("last_ticket_id") == 2
    assert await repo.list_deferred() == []


async def test_known_telegram_receipt_retries_registration_without_second_send(repo):
    ticket = test_sync._ticket(2)
    client = test_sync.FakeClient(recent=[ticket], tickets={2: ticket})
    bot = test_sync.FakeBot()
    service = test_sync._service(bot, client, repo)
    await repo.set_cursor("last_ticket_id", 1)
    original = repo.save_card
    repo.save_card = AsyncMock(side_effect=RuntimeError("SQLite unavailable"))
    await service._poll_new_tickets()
    assert len(bot.sent) == 1 and await repo.get_card(2) is None
    repo.save_card = original

    await service._flush_deferred_if_working()

    assert len(bot.sent) == 1
    assert (await repo.get_card(2)).message_id == 1
    assert await repo.list_deferred() == []


async def test_failed_live_requester_lookup_retries_instead_of_using_stale_identity(repo):
    ticket = test_sync._ticket(2)
    client = test_sync.FakeClient(recent=[ticket], tickets={2: ticket})
    client.get_ticket_requester = AsyncMock(side_effect=GlpiError("temporary lookup failure"))
    bot = test_sync.FakeBot()
    service = test_sync._service(bot, client, repo, front_base="https://glpi.local")
    await repo.set_cursor("last_ticket_id", 1)
    await repo.track_ticket(ticket_id=2, requester_tg_id=777, requester_glpi_id=38, status=1, now=0)
    await service._poll_new_tickets()
    assert not bot.sent
    client.get_ticket_requester.side_effect = None
    client.get_ticket_requester.return_value = (88, "New <Owner>")

    await service._flush_deferred_if_working()

    assert len(bot.sent) == 1
    assert (
        '<a href="https://glpi.local/front/user.form.php?id=88">New &lt;Owner&gt;</a>'
        in bot.sent[0][1]
    )
    assert "tg://user?id=777" not in bot.sent[0][1]
    assert (await repo.get_card(2)).requester_glpi_id == 88


async def test_concurrent_attempts_share_known_delivery(repo):
    ticket = test_sync._ticket(2)
    client = test_sync.FakeClient(tickets={2: ticket})
    bot = test_sync.FakeBot()
    service = test_sync._service(bot, client, repo)

    assert all(await asyncio.gather(*(service._send_new_card(ticket) for _ in range(3))))

    assert len(bot.sent) == 1


async def test_pending_history_and_registration_commit_together_and_retry_once(repo):
    ticket = test_sync._ticket(2)
    client = test_sync.FakeClient(recent=[ticket], tickets={2: ticket})
    bot = test_sync.FakeBot()
    service = test_sync._service(bot, client, repo)
    await repo.set_cursor("last_ticket_id", 1)
    await service._cards.record_event(bot, 2, "Taken", status=2, taken_by="Technician")
    async with repo._tx() as db:
        await db.execute(
            "CREATE TRIGGER fail_card_merge BEFORE UPDATE ON ticket_cards "
            "WHEN NEW.history <> OLD.history BEGIN SELECT RAISE(ABORT,'merge failed'); END"
        )

    await service._poll_new_tickets()

    assert len(bot.sent) == 1
    assert await repo.get_card(2) is None  # The receipt insert rolled back with the failed merge.
    async with repo._conn.execute(
        "SELECT COUNT(*) FROM card_pending_events WHERE ticket_id=2"
    ) as cursor:
        assert (await cursor.fetchone())[0] == 1
    async with repo._tx() as db:
        await db.execute("DROP TRIGGER fail_card_merge")

    await service._flush_deferred_if_working()
    await service._send_new_card(ticket)

    card = await repo.get_card(2)
    assert card.status == 2 and card.taken_by == "Technician"
    assert card.history.count("Taken") == 1
    assert await repo.pop_pending_card_events(2) == []
    assert await repo.list_deferred() == []
    assert len(bot.sent) == 1
