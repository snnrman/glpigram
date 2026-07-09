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
from bot.glpi.models import Document, Followup, Ticket
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
        self.photos: list[tuple[int, list[str]]] = []  # (chat_id, [filenames])
        self.edits: list[tuple[int, int, str]] = []  # (chat_id, message_id, text)

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))
        # notify._send returns the sent Message so the living card can remember
        # its id — hand back a minimal stand-in.
        from types import SimpleNamespace

        return SimpleNamespace(message_id=len(self.sent))

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs):
        self.edits.append((chat_id, message_id, text))

    async def send_photo(self, chat_id, photo, **kwargs):
        self.photos.append((chat_id, [photo.filename]))

    async def send_media_group(self, chat_id, media, **kwargs):
        self.photos.append((chat_id, [m.media.filename for m in media]))


class FakeClient:
    def __init__(
        self, *, recent=None, tickets=None, followups=None, requesters=None, documents=None
    ) -> None:
        self.recent = recent or []
        self.tickets = tickets or {}
        self.followups = followups or {}
        self.requesters = requesters or {}  # ticket_id -> (glpi_id, name)
        self.documents = documents or {}  # ticket_id -> list[Document]
        self.blobs = {}  # document_id -> bytes
        self.solutions = {}  # ticket_id -> (tech_name, text)

    async def list_recent_tickets(self, *, limit=100):
        return list(self.recent)

    async def get_ticket(self, ticket_id):
        return self.tickets.get(ticket_id)

    async def list_followups(self, ticket_id):
        return list(self.followups.get(ticket_id, []))

    async def get_ticket_requester(self, ticket_id):
        return self.requesters.get(ticket_id)

    async def list_ticket_documents(self, ticket_id):
        return list(self.documents.get(ticket_id, []))

    async def download_document(self, document_id):
        return self.blobs.get(document_id, b"IMAGEBYTES")

    async def get_user(self, user_id):
        return None  # card history then shows an author-less comment line

    async def get_ticket_solution(self, ticket_id):
        return self.solutions.get(ticket_id)  # (tech_name, text) or None


@pytest.fixture
async def repo(tmp_path):
    r = Repo(str(tmp_path / "sync.sqlite3"))
    await r.connect()
    yield r
    await r.close()


def _service(bot, client, repo, *, now=WORKING, quiet_min_urgency=4, front_base=None, **kw):
    return SyncService(
        bot,
        client,
        repo,
        tech_group_chat_id=TECH_CHAT,
        schedule=_SCHEDULE,
        quiet_min_urgency=quiet_min_urgency,
        interval=45,
        front_base=front_base,
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
    assert "👤 " in text
    assert "Иван Петров" in text
    assert "tg://user?id=555" in text


async def test_new_ticket_card_requester_not_linked_no_mention(repo):
    bot = FakeBot()
    client = FakeClient(recent=[_ticket(2)], requesters={2: (42, "Пётр")})
    await repo.set_cursor("last_ticket_id", 1)
    svc = _service(bot, client, repo)

    await svc._poll_new_tickets()
    text = bot.sent[0][1]
    assert "👤 Пётр" in text
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
    assert any("Заявка №2" in m for m in msgs)  # the card itself
    assert await repo.list_deferred() == []


async def test_high_urgency_offhours_sent_immediately(repo):
    bot = FakeBot()
    high = _ticket(3, status=1, urgency=4)  # >= QUIET_MIN_URGENCY
    client = FakeClient(recent=[high])
    await repo.set_cursor("last_ticket_id", 2)

    await _service(bot, client, repo, now=OFFHOURS_NIGHT).tick()
    assert any("Заявка №3" in m for m in _tech(bot))
    assert await repo.list_deferred() == []


# --- restart survival (cursors are the dedup mechanism) ----------------------
async def test_restart_does_not_resend_new_ticket_cards(repo):
    bot = FakeBot()
    client = FakeClient(recent=[_ticket(2), _ticket(1)])
    await repo.set_cursor("last_ticket_id", 1)

    await _service(bot, client, repo).tick()
    assert len(_tech(bot)) == 1  # ticket 2 announced once

    # "Restart": a brand-new service over the same repo sees the same GLPI page.
    bot.sent.clear()
    svc2 = _service(bot, client, repo)
    await svc2._seed_cursor()  # run() would call this; must be a no-op now
    await svc2.tick()
    assert _tech(bot) == []  # cursor persisted -> no duplicates


async def test_restart_does_not_resend_forwarded_followups(repo):
    followups = [Followup(id=1, tickets_id=20, content="ответ", users_id=99)]
    client = FakeClient(tickets={20: _ticket(20, status=1)}, followups={20: followups})
    await repo.track_ticket(
        ticket_id=20, requester_tg_id=REQUESTER_TG, requester_glpi_id=42, status=1, now=0
    )
    bot = FakeBot()
    await _service(bot, client, repo)._poll_tracked_tickets()
    assert len(bot.sent) == 1  # forwarded once

    bot.sent.clear()
    await _service(bot, client, repo)._poll_tracked_tickets()  # new instance = restart
    assert bot.sent == []  # followup cursor persisted


async def test_restart_does_not_resend_status_change(repo):
    client = FakeClient(tickets={10: _ticket(10, status=2)})
    await repo.track_ticket(
        ticket_id=10, requester_tg_id=REQUESTER_TG, requester_glpi_id=42, status=1, now=0
    )
    bot = FakeBot()
    await _service(bot, client, repo)._poll_tracked_tickets()
    assert len(bot.sent) == 1

    bot.sent.clear()
    await _service(bot, client, repo)._poll_tracked_tickets()
    assert bot.sent == []  # last_status persisted


async def test_deferred_queue_survives_restart(repo):
    low = _ticket(2, status=1, urgency=2)
    client = FakeClient(recent=[low], tickets={2: low})
    await repo.set_cursor("last_ticket_id", 1)
    bot = FakeBot()

    # Queued off-hours by one instance...
    await _service(bot, client, repo, now=OFFHOURS_SAT).tick()
    assert len(await repo.list_deferred()) == 1

    # ...flushed by a different instance after a restart, exactly once.
    bot.sent.clear()
    client.recent = []
    await _service(bot, client, repo, now=MONDAY_OPEN).tick()
    assert any("Заявка №2" in m for m in _tech(bot))
    assert await repo.list_deferred() == []

    bot.sent.clear()
    await _service(bot, client, repo, now=MONDAY_OPEN).tick()
    assert _tech(bot) == []  # queue drained, no re-flush


async def test_self_close_suppresses_echo(repo):
    """Requester closes own ticket -> cursors bumped -> sync sends nothing."""
    reason = Followup(id=5, tickets_id=30, content="сам закрыл", users_id=42)
    client = FakeClient(tickets={30: _ticket(30, status=6)}, followups={30: [reason]})
    await repo.track_ticket(
        ticket_id=30, requester_tg_id=REQUESTER_TG, requester_glpi_id=42, status=1, now=0
    )
    # what the /tickets close flow records:
    await repo.set_ticket_followup_cursor(30, 5)
    await repo.set_ticket_status(30, status=6, active=False)

    bot = FakeBot()
    await _service(bot, client, repo)._poll_tracked_tickets()
    assert bot.sent == []  # neither the status change nor the reason echoed


async def test_flush_skips_remind_for_taken_ticket(repo):
    # Deferred overnight, but a tech took the ticket before morning -> drop it.
    taken = _ticket(9, status=2)
    client = FakeClient(tickets={9: taken})
    await repo.enqueue_deferred("remind", 9, 0)
    bot = FakeBot()

    await _service(bot, client, repo, now=MONDAY_OPEN).tick()
    assert not any("напоминает" in m.lower() for m in _tech(bot))
    assert await repo.list_deferred() == []  # consumed, not retried


class DownBot:
    """Telegram is down: every send fails."""

    async def send_message(self, chat_id, text, **kwargs):
        raise RuntimeError("tg down")


async def test_flush_keeps_queue_when_telegram_down(repo):
    # Reviewer finding 5.5: a failed send must NOT drop the queued card.
    low = _ticket(2, status=1, urgency=2)
    client = FakeClient(tickets={2: low})
    await repo.enqueue_deferred("new", 2, 0)

    await _service(DownBot(), client, repo, now=MONDAY_OPEN).tick()
    assert len(await repo.list_deferred()) == 1  # still queued, not lost

    # Telegram is back: the next tick delivers header + card exactly once.
    bot = FakeBot()
    await _service(bot, client, repo, now=MONDAY_OPEN).tick()
    msgs = _tech(bot)
    assert any("нерабочее время поступило" in m for m in msgs)
    assert any("Заявка №2" in m for m in msgs)
    assert await repo.list_deferred() == []


async def test_new_ticket_card_shows_urgency(repo):
    bot = FakeBot()
    client = FakeClient(recent=[_ticket(2, urgency=4)])
    await repo.set_cursor("last_ticket_id", 1)

    await _service(bot, client, repo)._poll_new_tickets()
    text = bot.sent[0][1]
    assert "🔴 <b>Высокая срочность</b>" in text


async def test_new_ticket_card_layout(repo):
    """Pin the card format: №+urgency head, ONE blank line, compact 📝/👤/🔗 body."""
    bot = FakeBot()
    client = FakeClient(
        recent=[_ticket(36, name="тест", urgency=3)], requesters={36: (42, "Олег Каленский")}
    )
    await repo.set_cursor("last_ticket_id", 35)

    await _service(bot, client, repo, front_base="https://glpi.local")._poll_new_tickets()
    text = bot.sent[0][1]
    blocks = text.split("\n\n")
    assert len(blocks) == 2  # exactly one blank line in the whole card
    assert blocks[0] == "🆕 <b>Заявка №36</b>\n🟡 <b>Средняя срочность</b>"
    body = blocks[1].split("\n")
    assert body[0] == "📝 тест"  # title NOT bold
    assert body[1].startswith("👤 ")
    assert body[2] == (
        '🔗 <a href="https://glpi.local/front/ticket.form.php?id=36">Открыть в GLPI</a>'
    )
    assert "Статус" not in text  # implied by 🆕 for a New ticket


# --- attachments on the new-ticket card ---------------------------------------
def _doc(doc_id, filename, mime, size=1000):
    return Document(id=doc_id, filename=filename, mime=mime, filesize=size)


async def test_card_sends_images_and_counts_all_attachments(repo):
    bot = FakeBot()
    docs = [
        _doc(1, "a.jpg", "image/jpeg"),
        _doc(2, "b.png", "image/png"),
        _doc(3, "spec.pdf", "application/pdf"),  # not an image -> counted only
    ]
    client = FakeClient(recent=[_ticket(2)], documents={2: docs})
    await repo.set_cursor("last_ticket_id", 1)

    await _service(bot, client, repo)._poll_new_tickets()
    assert "📎 Вложений: 3" in bot.sent[0][1]  # every attachment counted in-card
    assert bot.photos == [(TECH_CHAT, ["a.jpg", "b.png"])]  # images as a media group


async def test_card_without_attachments_unchanged(repo):
    bot = FakeBot()
    client = FakeClient(recent=[_ticket(2)])
    await repo.set_cursor("last_ticket_id", 1)

    await _service(bot, client, repo)._poll_new_tickets()
    assert "📎" not in bot.sent[0][1]
    assert bot.photos == []


async def test_oversized_image_stays_behind_the_link(repo):
    bot = FakeBot()
    docs = [_doc(1, "huge.jpg", "image/jpeg", size=25 * 1024 * 1024)]
    client = FakeClient(recent=[_ticket(2)], documents={2: docs})
    await repo.set_cursor("last_ticket_id", 1)

    await _service(bot, client, repo)._poll_new_tickets()
    assert "📎 Вложений: 1" in bot.sent[0][1]  # still counted
    assert bot.photos == []  # too big for sendPhoto -> not uploaded


async def test_document_lookup_failure_does_not_cost_the_card(repo):
    bot = FakeBot()
    client = FakeClient(recent=[_ticket(2)])

    async def boom(ticket_id):
        from bot.glpi.client import GlpiHTTPError

        raise GlpiHTTPError("GLPI down")

    client.list_ticket_documents = boom
    await repo.set_cursor("last_ticket_id", 1)

    await _service(bot, client, repo)._poll_new_tickets()
    assert any("Заявка №2" in m for m in _tech(bot))  # card still delivered
    assert await repo.get_cursor("last_ticket_id") == 2


async def test_repeated_tick_sends_no_duplicate_images(repo):
    bot = FakeBot()
    docs = [_doc(1, "a.jpg", "image/jpeg")]
    client = FakeClient(recent=[_ticket(2)], documents={2: docs})
    await repo.set_cursor("last_ticket_id", 1)
    svc = _service(bot, client, repo)

    await svc.tick()
    await svc.tick()  # same GLPI page again
    assert len(bot.photos) == 1  # images ride the card; cursor dedup covers both


# --- living card registration ------------------------------------------------
async def test_card_registered_at_send_time(repo):
    bot = FakeBot()
    client = FakeClient(recent=[_ticket(2, name="тест", urgency=3)])
    await repo.set_cursor("last_ticket_id", 1)

    await _service(bot, client, repo)._poll_new_tickets()
    card = await repo.get_card(2)
    assert card is not None
    assert card.chat_id == TECH_CHAT and card.message_id == 1  # first send
    assert card.title == "тест" and card.urgency == 3


async def test_deferred_card_gets_message_id_only_in_the_morning(repo):
    low = _ticket(2, status=1, urgency=2)
    client = FakeClient(recent=[low], tickets={2: low})
    await repo.set_cursor("last_ticket_id", 1)
    bot = FakeBot()

    # Saturday: queued, NOT sent -> no card row yet (spec item 5).
    await _service(bot, client, repo, now=OFFHOURS_SAT).tick()
    assert await repo.get_card(2) is None

    # Monday: the flush actually sends it -> the row appears with the real id.
    client.recent = []
    await _service(bot, client, repo, now=MONDAY_OPEN).tick()
    card = await repo.get_card(2)
    assert card is not None and card.message_id > 0


async def test_sync_status_change_updates_card_and_history(repo):
    bot = FakeBot()
    client = FakeClient(recent=[_ticket(10)], tickets={10: _ticket(10, status=2)})
    await repo.set_cursor("last_ticket_id", 9)
    await repo.track_ticket(
        ticket_id=10, requester_tg_id=REQUESTER_TG, requester_glpi_id=42, status=1, now=0
    )
    svc = _service(bot, client, repo)
    await svc._poll_new_tickets()  # card registered (status New)
    await svc._poll_tracked_tickets()  # GLPI says: now assigned

    card = await repo.get_card(10)
    assert card.status == 2
    import json as _json

    assert any("🔄" in line for line in _json.loads(card.history))
    # the card message was edited with the history block
    assert any("── История ──" in text for _, _, text in bot.edits)


async def test_sync_followup_edits_card_without_group_ping(repo):
    followups = [Followup(id=1, tickets_id=20, content="ответ", users_id=99)]
    client = FakeClient(
        recent=[_ticket(20)], tickets={20: _ticket(20, status=1)}, followups={20: followups}
    )
    await repo.set_cursor("last_ticket_id", 19)
    await repo.track_ticket(
        ticket_id=20, requester_tg_id=REQUESTER_TG, requester_glpi_id=42, status=1, now=0
    )
    bot = FakeBot()
    svc = _service(bot, client, repo)
    await svc._poll_new_tickets()
    await svc._poll_tracked_tickets()

    import json as _json

    card = await repo.get_card(20)
    assert any("💬 Комментарий" in line for line in _json.loads(card.history))
    # a comment by the team (sync filters out the requester's own) is
    # history-only: no self-ping into the group
    assert not any(c == TECH_CHAT and "Новый комментарий" in t for c, t in bot.sent)


# --- solution text reaches the requester ---------------------------------------
async def test_web_solved_notifies_requester_with_solution_text(repo):
    bot = FakeBot()
    client = FakeClient(tickets={10: _ticket(10, status=5)})
    client.solutions[10] = (42, "Техник", "почищен кэш принтера")
    await repo.track_ticket(
        ticket_id=10, requester_tg_id=REQUESTER_TG, requester_glpi_id=42, status=1, now=0
    )

    await _service(bot, client, repo)._poll_tracked_tickets()
    to_requester = [t for c, t in bot.sent if c == REQUESTER_TG]
    assert to_requester == [
        "✅ По заявке №10 предложено решение — Техник: почищен кэш принтера\n\nПроблема решена?"
    ]  # the ITIL proposal with buttons, not a bare status line


async def test_web_solved_without_solution_still_asks_for_confirmation(repo):
    bot = FakeBot()
    client = FakeClient(tickets={10: _ticket(10, status=5)})  # no solution recorded
    await repo.track_ticket(
        ticket_id=10, requester_tg_id=REQUESTER_TG, requester_glpi_id=42, status=1, now=0
    )

    await _service(bot, client, repo)._poll_tracked_tickets()
    to_requester = [t for c, t in bot.sent if c == REQUESTER_TG]
    assert len(to_requester) == 1 and "Проблема решена?" in to_requester[0]


async def test_solved_to_closed_transition_is_a_plain_status_change(repo):
    # 5 -> 6: the solution was already delivered when it became solved.
    bot = FakeBot()
    client = FakeClient(tickets={10: _ticket(10, status=6)})
    client.solutions[10] = (42, "Техник", "почищен кэш")
    await repo.track_ticket(
        ticket_id=10, requester_tg_id=REQUESTER_TG, requester_glpi_id=42, status=1, now=0
    )
    await repo.set_ticket_status(10, status=5, active=True)  # already solved & notified

    await _service(bot, client, repo)._poll_tracked_tickets()
    to_requester = [t for c, t in bot.sent if c == REQUESTER_TG]
    assert len(to_requester) == 1 and "статус изменён" in to_requester[0]
    assert "почищен кэш" not in to_requester[0]


async def test_bot_closed_ticket_not_renotified_by_sync(repo):
    # The bot-close path bumps last_status to 5 right after notifying the
    # requester itself -> the next sync tick must stay silent.
    bot = FakeBot()
    client = FakeClient(tickets={10: _ticket(10, status=5)})
    client.solutions[10] = (42, "Техник", "решение")
    await repo.track_ticket(
        ticket_id=10, requester_tg_id=REQUESTER_TG, requester_glpi_id=42, status=1, now=0
    )
    await repo.set_ticket_status(10, status=5, active=True)  # what on_solution_text does

    await _service(bot, client, repo)._poll_tracked_tickets()
    assert [t for c, t in bot.sent if c == REQUESTER_TG] == []  # no duplicate
