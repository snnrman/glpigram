"""Sync-loop logic (feature 4) against fakes + a real SQLite repo.

Covers the three responsibilities and the no-duplicate-notifications guarantee:
new tickets -> tech group, status changes -> requester, followups by others ->
requester (own/private skipped), and cursor persistence across ticks.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bot.db.repo import Repo
from bot.glpi.models import Followup, Ticket
from bot.schedule import WorkSchedule
from bot.services.sync import SyncService

pytestmark = pytest.mark.asyncio

TECH_CHAT = -1000
REQUESTER_TG = 777

_KGD = ZoneInfo("Europe/Kaliningrad")
_SCHEDULE = WorkSchedule.from_config("09:00-18:00", "1-5", tz_name="Europe/Kaliningrad")
# 2026-07-06 is a Monday.
WORKING = datetime(2026, 7, 6, 12, 0, tzinfo=_KGD)  # Mon 12:00 -> working
OFFHOURS_NIGHT = datetime(2026, 7, 6, 23, 0, tzinfo=_KGD)  # Mon 23:00 -> off
OFFHOURS_SAT = datetime(2026, 7, 11, 12, 0, tzinfo=_KGD)  # Sat -> off
MONDAY_OPEN = datetime(2026, 7, 6, 9, 0, tzinfo=_KGD)  # Mon 09:00 -> working


def _ticket(tid: int, *, status: int = 1, name: str = "t", urgency: int = 3) -> Ticket:
    return Ticket(id=tid, name=name, content="c", status=status, urgency=urgency)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


class FakeClient:
    def __init__(self, *, recent=None, tickets=None, followups=None, requesters=None) -> None:
        self.recent = recent or []
        self.tickets = tickets or {}
        self.followups = followups or {}
        self.requesters = requesters or {}  # ticket_id -> (glpi_id, name)

    async def list_recent_tickets(self, *, limit=100):
        return list(self.recent)

    async def get_ticket(self, ticket_id):
        return self.tickets.get(ticket_id)

    async def list_followups(self, ticket_id):
        return list(self.followups.get(ticket_id, []))

    async def get_ticket_requester(self, ticket_id):
        return self.requesters.get(ticket_id)


@pytest.fixture
async def repo(tmp_path):
    r = Repo(str(tmp_path / "sync.sqlite3"))
    await r.connect()
    yield r
    await r.close()


def _service(bot, client, repo, *, now=WORKING, quiet_min_urgency=4, **kw):
    return SyncService(
        bot,
        client,
        repo,
        tech_group_chat_id=TECH_CHAT,
        schedule=_SCHEDULE,
        quiet_min_urgency=quiet_min_urgency,
        interval=45,
        front_base=None,
        now_provider=lambda: now,
        **kw,
    )


def _tech(bot: FakeBot) -> list[str]:
    return [t for c, t in bot.sent if c == TECH_CHAT]


# --- 1. new tickets -> tech group -------------------------------------------
async def test_seed_cursor_skips_history(repo):
    client = FakeClient(recent=[_ticket(5), _ticket(4)])
    svc = _service(FakeBot(), client, repo)
    await svc._seed_cursor()
    assert await repo.get_cursor("last_ticket_id") == 5
    # a fresh seed is a no-op once set
    client.recent = [_ticket(9)]
    await svc._seed_cursor()
    assert await repo.get_cursor("last_ticket_id") == 5


async def test_new_tickets_notify_group_once(repo):
    bot = FakeBot()
    client = FakeClient(recent=[_ticket(3), _ticket(2), _ticket(1)])
    await repo.set_cursor("last_ticket_id", 1)
    svc = _service(bot, client, repo)

    await svc.tick()
    assert [c for c, _ in bot.sent] == [TECH_CHAT, TECH_CHAT]  # ids 2 and 3
    assert await repo.get_cursor("last_ticket_id") == 3

    # second tick with no newer tickets -> nothing sent again
    bot.sent.clear()
    await svc.tick()
    assert bot.sent == []


async def test_new_ticket_card_shows_requester_and_tg_mention(repo):
    bot = FakeBot()
    client = FakeClient(recent=[_ticket(2)], requesters={2: (42, "Иван Петров")})
    await repo.set_cursor("last_ticket_id", 1)
    # requester 42 is linked in the bot -> card should carry a tg mention
    await repo.upsert_link(tg_id=555, glpi_users_id=42, display_name="Иван", is_tech=False, now=0)
    svc = _service(bot, client, repo)

    await svc._poll_new_tickets()
    assert len(bot.sent) == 1
    text = bot.sent[0][1]
    assert "Автор: " in text
    assert "Иван Петров" in text
    assert "tg://user?id=555" in text


async def test_new_ticket_card_requester_not_linked_no_mention(repo):
    bot = FakeBot()
    client = FakeClient(recent=[_ticket(2)], requesters={2: (42, "Пётр")})
    await repo.set_cursor("last_ticket_id", 1)
    svc = _service(bot, client, repo)

    await svc._poll_new_tickets()
    text = bot.sent[0][1]
    assert "Автор: Пётр" in text
    assert "tg://user" not in text  # unknown requester -> plain name, no mention


# --- 2. status changes -> requester -----------------------------------------
async def test_status_change_notifies_requester_and_closes(repo):
    bot = FakeBot()
    client = FakeClient(tickets={10: _ticket(10, status=2)})
    await repo.track_ticket(
        ticket_id=10, requester_tg_id=REQUESTER_TG, requester_glpi_id=42, status=1, now=0
    )
    svc = _service(bot, client, repo)

    await svc._poll_tracked_tickets()
    assert bot.sent == [(REQUESTER_TG, bot.sent[0][1])]
    assert "статус" in bot.sent[0][1].lower()
    assert (await repo.active_tracked_tickets())[0].last_status == 2

    # ticket becomes CLOSED -> notify once, then stop watching
    bot.sent.clear()
    client.tickets[10] = _ticket(10, status=6)
    await svc._poll_tracked_tickets()
    assert len(bot.sent) == 1
    assert await repo.active_tracked_tickets() == []

    # no longer polled
    bot.sent.clear()
    await svc._poll_tracked_tickets()
    assert bot.sent == []


async def test_no_status_notification_when_unchanged(repo):
    bot = FakeBot()
    client = FakeClient(tickets={10: _ticket(10, status=1)})
    await repo.track_ticket(
        ticket_id=10, requester_tg_id=REQUESTER_TG, requester_glpi_id=42, status=1, now=0
    )
    svc = _service(bot, client, repo)
    await svc._poll_tracked_tickets()
    assert bot.sent == []


# --- 3. followups by others -> requester ------------------------------------
async def test_followups_forward_only_others_public(repo):
    bot = FakeBot()
    followups = [
        Followup(id=1, tickets_id=20, content="мой", users_id=42),  # requester's own
        Followup(id=2, tickets_id=20, content="<p>ответ</p>", users_id=99),  # other -> forward
        Followup(id=3, tickets_id=20, content="секрет", users_id=99, is_private=True),  # private
    ]
    client = FakeClient(tickets={20: _ticket(20, status=1)}, followups={20: followups})
    await repo.track_ticket(
        ticket_id=20, requester_tg_id=REQUESTER_TG, requester_glpi_id=42, status=1, now=0
    )
    svc = _service(bot, client, repo)

    await svc._poll_tracked_tickets()
    assert len(bot.sent) == 1
    chat, text = bot.sent[0]
    assert chat == REQUESTER_TG
    assert "ответ" in text
    # cursor advanced past ALL followups (incl. own/private) -> no re-send
    assert (await repo.active_tracked_tickets())[0].last_followup_id == 3

    bot.sent.clear()
    await svc._poll_tracked_tickets()
    assert bot.sent == []


async def test_deleted_ticket_is_deactivated(repo):
    bot = FakeBot()
    client = FakeClient(tickets={30: None})  # get_ticket returns None (404)
    await repo.track_ticket(
        ticket_id=30, requester_tg_id=REQUESTER_TG, requester_glpi_id=42, status=1, now=0
    )
    svc = _service(bot, client, repo)
    await svc._poll_tracked_tickets()
    assert bot.sent == []
    assert await repo.active_tracked_tickets() == []


# --- quiet hours (off-hours) ------------------------------------------------
async def test_low_urgency_offhours_deferred_then_flushed_next_workday(repo):
    bot = FakeBot()
    low = _ticket(2, status=1, urgency=2)
    client = FakeClient(recent=[low], tickets={2: low})
    await repo.set_cursor("last_ticket_id", 1)

    # Saturday: low-urgency ticket is queued, nothing hits the group.
    await _service(bot, client, repo, now=OFFHOURS_SAT).tick()
    assert _tech(bot) == []
    assert len(await repo.list_deferred()) == 1
    assert await repo.get_cursor("last_ticket_id") == 2  # cursor still advances

    # Monday 09:00: backlog is flushed with a header + the card, queue emptied.
    bot.sent.clear()
    client.recent = []  # nothing newer to poll
    await _service(bot, client, repo, now=MONDAY_OPEN).tick()
    msgs = _tech(bot)
    assert any("нерабочее время поступило" in m for m in msgs)  # batch header
    assert any("Новая заявка №2" in m for m in msgs)  # the card itself
    assert await repo.list_deferred() == []


async def test_high_urgency_offhours_sent_immediately(repo):
    bot = FakeBot()
    high = _ticket(3, status=1, urgency=4)  # >= QUIET_MIN_URGENCY
    client = FakeClient(recent=[high])
    await repo.set_cursor("last_ticket_id", 2)

    await _service(bot, client, repo, now=OFFHOURS_NIGHT).tick()
    assert any("Новая заявка №3" in m for m in _tech(bot))
    assert await repo.list_deferred() == []
