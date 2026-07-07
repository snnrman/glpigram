"""The living tech-group card: register, edit-on-event, dedup, limits."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from bot import texts
from bot.db.repo import Repo
from bot.glpi.models import Ticket
from bot.services.cards import CardService

CHAT = -100
MSG_ID = 777
TICKET = 5
_NOW = datetime(2026, 7, 6, 9, 15)


class FakeBot:
    def __init__(self) -> None:
        self.edits: list[tuple[int, int, str, object]] = []
        self.replies: list[tuple[int, str, object]] = []

    async def edit_message_text(self, text, chat_id=None, message_id=None, reply_markup=None):
        self.edits.append((chat_id, message_id, text, reply_markup))

    async def send_message(self, chat_id, text, reply_parameters=None, **kwargs):
        self.replies.append((chat_id, text, reply_parameters))
        return SimpleNamespace(message_id=1)


@pytest.fixture
async def repo(tmp_path):
    r = Repo(str(tmp_path / "cards.sqlite3"))
    await r.connect()
    yield r
    await r.close()


def _service(repo) -> CardService:
    return CardService(repo, front_base="https://glpi.local", now_provider=lambda: _NOW)


async def _register(repo, cards, *, status=1) -> None:
    ticket = Ticket(id=TICKET, name="тест", content="c", status=status, urgency=3)
    await cards.register(
        ticket,
        chat_id=CHAT,
        message_id=MSG_ID,
        requester_name="Олег",
        requester_tg_id=555,
        attachments_count=0,
        now=0,
    )


async def test_event_edits_card_with_history_and_time(repo):
    cards, bot = _service(repo), FakeBot()
    await _register(repo, cards)

    ok = await cards.record_event(bot, TICKET, texts.hist_taken("Техник"), status=2)
    assert ok
    chat, msg_id, text, kb = bot.edits[0]
    assert (chat, msg_id) == (CHAT, MSG_ID)
    assert "── История ──" in text
    assert "🙋 Взял в работу: Техник · 09:15" in text
    assert "Статус: ⚙️ В работе (назначена)" in text  # header updated
    # taken -> the Take button is gone, Reply/Close remain
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert data == [f"ta:comment:{TICKET}", f"ta:close:{TICKET}"]


async def test_reply_ping_is_a_reply_to_the_card(repo):
    cards, bot = _service(repo), FakeBot()
    await _register(repo, cards)

    await cards.record_event(bot, TICKET, texts.hist_comment("Олег"), reply="💬 пинг")
    chat, text, reply_params = bot.replies[0]
    assert chat == CHAT
    assert text == "💬 пинг"
    assert reply_params.message_id == MSG_ID  # edit doesn't notify; the reply does


async def test_closed_card_loses_all_buttons(repo):
    cards, bot = _service(repo), FakeBot()
    await _register(repo, cards)

    await cards.record_event(bot, TICKET, texts.hist_closed("Техник"), status=5)
    assert bot.edits[0][3] is None  # no keyboard on a solved/closed card


async def test_no_card_returns_false_for_legacy_fallback(repo):
    cards, bot = _service(repo), FakeBot()
    assert await cards.record_event(bot, 999, "строка") is False
    assert bot.edits == [] and bot.replies == []


async def test_status_dedup_skips_known_state(repo):
    cards, bot = _service(repo), FakeBot()
    await _register(repo, cards)
    # button action records the taken state...
    await cards.record_event(bot, TICKET, texts.hist_taken("Техник"), status=2, taken_by="Техник")
    # ...then the sync loop notices the same status -> must not duplicate
    await cards.record_event(
        bot, TICKET, texts.hist_status(2), status=2, skip_if_status_unchanged=True
    )
    card = await repo.get_card(TICKET)
    history = json.loads(card.history)
    assert len(history) == 1  # only the "taken" line
    assert len(bot.edits) == 1  # and only one edit (no flood on no-ops)


async def test_followup_dedup_between_direct_and_sync_paths(repo):
    cards, bot = _service(repo), FakeBot()
    await _register(repo, cards)
    # direct hook (tech commented via the bot) records followup 7...
    await cards.record_event(bot, TICKET, texts.hist_comment("Техник"), followup_id=7)
    # ...the sync loop later sees the same followup id -> skip
    await cards.record_event(bot, TICKET, texts.hist_comment("Техник"), followup_id=7, reply="x")
    card = await repo.get_card(TICKET)
    assert len(json.loads(card.history)) == 1
    assert bot.replies == []  # no second ping either


async def test_history_capped_at_ten(repo):
    cards, bot = _service(repo), FakeBot()
    await _register(repo, cards)
    for i in range(13):
        await cards.record_event(bot, TICKET, f"событие {i}")
    card = await repo.get_card(TICKET)
    history = json.loads(card.history)
    assert len(history) == 10
    assert history[0].startswith("событие 3")  # oldest trimmed
    assert history[-1].startswith("событие 12")


async def test_edit_failure_self_heals_on_next_event(repo):
    cards = _service(repo)
    await _register(repo, cards)

    class BrokenBot(FakeBot):
        async def edit_message_text(self, *a, **k):
            raise RuntimeError("flood limit")

    # a failed edit must not raise and must still persist the state...
    assert await cards.record_event(BrokenBot(), TICKET, "первое событие")
    # ...so the next (successful) edit renders the full history
    bot = FakeBot()
    await cards.record_event(bot, TICKET, "второе событие")
    assert "первое событие" in bot.edits[0][2]
    assert "второе событие" in bot.edits[0][2]
