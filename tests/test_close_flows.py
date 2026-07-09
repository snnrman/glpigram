"""The two independent close flows must never cross.

1. Requester closes their own ticket from «Мои заявки» — dialog in the
   requester's DM, followup on their behalf, status -> Closed.
2. Technician closes from the group card — dialog in the TECH's DM, solution
   on the tech's behalf, and a group announcement with the solution text.

Runs on the full production dispatcher (router order + auth middleware).
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TgUser

from bot import texts
from bot.db.repo import Repo
from bot.handlers.my_tickets import MyTickets
from bot.handlers.tech_actions import TechAction

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("GLPI_API_URL", "http://127.0.0.1/apirest.php")
os.environ.setdefault("GLPI_USER_TOKEN", "u")

from bot.config import Settings  # noqa: E402
from bot.main import build_dispatcher  # noqa: E402

BOT_ID = 42
TECH_ID = 2002
REQUESTER_ID = 1001
GROUP = -100
TICKET = 5
_DATE = datetime(2020, 1, 1, tzinfo=UTC)


class FakeBot:
    id = BOT_ID

    def __init__(self) -> None:
        self.sent: list[tuple[object, str]] = []  # (chat_id, text)
        self.toasts: list[str] = []
        self.edits: list[tuple[object, object, str]] = []

    async def __call__(self, method, **kwargs):
        name = type(method).__name__
        if name in ("SendMessage", "EditMessageText"):
            self.sent.append((method.chat_id, method.text))
        elif name == "AnswerCallbackQuery" and method.text:
            self.toasts.append(method.text)
        return MagicMock()

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))
        return MagicMock()

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs):
        self.edits.append((chat_id, message_id, text))


def _client() -> AsyncMock:
    client = AsyncMock()
    client.add_solution.return_value = 3
    client.add_followup.return_value = 7
    client.get_ticket_assignees.return_value = []
    return client


@pytest.fixture
async def env(tmp_path):
    repo = Repo(str(tmp_path / "flows.sqlite3"))
    await repo.connect()
    now = int(time.time())
    await repo.upsert_link(
        tg_id=TECH_ID, glpi_users_id=9, display_name="Техник", is_tech=True, now=now
    )
    await repo.upsert_link(
        tg_id=REQUESTER_ID, glpi_users_id=8, display_name="Заявитель", is_tech=False, now=now
    )
    client = _client()
    settings = Settings()
    settings.tech_group_chat_id = GROUP
    dp = build_dispatcher(client, repo, settings)
    yield dp, client, repo
    await repo.close()


def _dm_msg(bot, uid, from_id, text):
    msg = Message(
        message_id=uid,
        date=_DATE,
        chat=Chat(id=from_id, type="private"),
        from_user=TgUser(id=from_id, is_bot=False, first_name="U"),
        text=text,
    ).as_(bot)
    return Update(update_id=uid, message=msg)


def _group_cb(bot, uid, from_id, data):
    msg = Message(
        message_id=uid,
        date=_DATE,
        chat=Chat(id=GROUP, type="supergroup"),
        from_user=TgUser(id=BOT_ID, is_bot=True, first_name="bot"),
        text="карточка",
    ).as_(bot)
    cb = CallbackQuery(
        id=str(uid),
        from_user=TgUser(id=from_id, is_bot=False, first_name="U"),
        chat_instance="ci",
        message=msg,
        data=data,
    ).as_(bot)
    return Update(update_id=uid, callback_query=cb)


def _key(chat_id, user_id):
    return StorageKey(bot_id=BOT_ID, chat_id=chat_id, user_id=user_id)


def test_fsm_states_of_the_two_flows_are_distinct():
    assert TechAction.closing.state == "TechAction:closing"
    assert MyTickets.closing.state == "MyTickets:closing"
    assert TechAction.closing.state != MyTickets.closing.state


async def test_tech_close_prompts_tech_dm_only(env):
    dp, client, _ = env
    bot = FakeBot()
    await dp.feed_update(bot, _group_cb(bot, 1, TECH_ID, f"ta:close:{TICKET}"))

    # prompt in the TECH's DM, with the tech wording
    assert (TECH_ID, texts.tech_ask_solution(TICKET)) in bot.sent
    # nothing to the requester, nothing extra to the group
    assert not any(chat == REQUESTER_ID for chat, _ in bot.sent)
    assert not any(chat == GROUP for chat, _ in bot.sent)
    # tech's DM carries the TECH state; requester has no state at all
    assert await dp.storage.get_state(_key(TECH_ID, TECH_ID)) == TechAction.closing.state
    assert await dp.storage.get_state(_key(REQUESTER_ID, REQUESTER_ID)) is None


async def test_tech_solution_goes_to_glpi_and_group_announcement(env):
    dp, client, _ = env
    bot = FakeBot()
    await dp.feed_update(bot, _group_cb(bot, 1, TECH_ID, f"ta:close:{TICKET}"))
    await dp.feed_update(bot, _dm_msg(bot, 2, TECH_ID, "перезагрузил сервер"))

    # solution on the tech's behalf (named), via add_solution — not the
    # requester branch (no followup, no manual status flip)
    client.add_solution.assert_awaited_once_with(TICKET, "Техник:\nперезагрузил сервер")
    client.add_followup.assert_not_awaited()
    client.set_ticket_status.assert_not_awaited()
    # group announcement with the solution text
    group_msgs = [t for c, t in bot.sent if c == GROUP]
    assert any(
        "Техник предложил решение по заявке №5" in t and "перезагрузил сервер" in t
        for t in group_msgs
    )
    # tech got the confirmation, state cleared
    assert (TECH_ID, texts.TECH_SOLUTION_DONE) in bot.sent
    assert await dp.storage.get_state(_key(TECH_ID, TECH_ID)) is None


async def test_requester_close_stays_in_requester_branch(env):
    dp, client, _ = env
    bot = FakeBot()
    # requester is mid-flow: «Закрыть заявку» already tapped in «Мои заявки»
    await dp.storage.set_state(_key(REQUESTER_ID, REQUESTER_ID), MyTickets.closing.state)
    await dp.storage.set_data(_key(REQUESTER_ID, REQUESTER_ID), {"close_ticket_id": TICKET})

    await dp.feed_update(bot, _dm_msg(bot, 1, REQUESTER_ID, "уже не актуально"))

    # requester branch: followup + explicit Closed status — NOT add_solution
    client.add_followup.assert_awaited_once()
    assert "уже не актуально" in client.add_followup.await_args.args[1]
    client.set_ticket_status.assert_awaited_once()
    client.add_solution.assert_not_awaited()
    # group told it was closed by the requester (not the tech announcement)
    group_msgs = [t for c, t in bot.sent if c == GROUP]
    assert any("закрыта заявителем" in t for t in group_msgs)
    assert not any("закрыл" in t and "Техник" in t for t in group_msgs)


async def test_non_tech_pressing_card_close_is_refused(env):
    dp, client, _ = env
    bot = FakeBot()
    await dp.feed_update(bot, _group_cb(bot, 1, REQUESTER_ID, f"ta:close:{TICKET}"))

    assert texts.TECH_ONLY in bot.toasts  # is_tech guard
    assert bot.sent == []  # no dialog opened anywhere
    assert await dp.storage.get_state(_key(REQUESTER_ID, REQUESTER_ID)) is None


async def _seed_card(repo):
    await repo.save_card(
        ticket_id=TICKET,
        chat_id=GROUP,
        message_id=333,
        title="тест",
        urgency=3,
        requester_name="Заявитель",
        requester_tg_id=REQUESTER_ID,
        attachments_count=0,
        status=1,
        now=0,
    )


async def test_take_appears_once_history_only_no_ping(env):
    """«Взял в работу» lives in the card history (edit) and is NOT ping-replied."""
    dp, client, repo = env
    await _seed_card(repo)
    bot = FakeBot()

    await dp.feed_update(bot, _group_cb(bot, 1, TECH_ID, f"ta:take:{TICKET}"))

    # exactly one appearance: the history line inside the edited card
    assert len(bot.edits) == 1
    assert "🙋 Взял в работу: Техник" in bot.edits[0][2]
    # and NO separate group message about the take
    assert not any(chat == GROUP for chat, _ in bot.sent)


async def test_tech_close_pings_group_once_plus_history(env):
    dp, client, repo = env
    await _seed_card(repo)
    bot = FakeBot()

    await dp.feed_update(bot, _group_cb(bot, 1, TECH_ID, f"ta:close:{TICKET}"))
    bot.edits.clear()
    group_before = [t for c, t in bot.sent if c == GROUP]
    await dp.feed_update(bot, _dm_msg(bot, 2, TECH_ID, "готово"))

    # history line in the card edit...
    assert any("✅ Решение предложено: Техник" in text for _, _, text in bot.edits)
    # ...plus exactly ONE reply ping with the solution
    group_msgs = [t for c, t in bot.sent if c == GROUP]
    assert len(group_msgs) - len(group_before) == 1
    assert "предложил решение" in group_msgs[-1]


async def test_tech_dm_comment_history_only_no_ping(env):
    dp, client, repo = env
    await _seed_card(repo)
    bot = FakeBot()

    await dp.feed_update(bot, _group_cb(bot, 1, TECH_ID, f"ta:comment:{TICKET}"))
    await dp.feed_update(bot, _dm_msg(bot, 2, TECH_ID, "смотрим"))

    assert any("💬 Комментарий (Техник)" in text for _, _, text in bot.edits)
    assert not any(chat == GROUP for chat, _ in bot.sent)  # the team's own comment: no self-ping


async def test_requester_comment_pings_group_once_plus_history(env):
    dp, client, repo = env
    await _seed_card(repo)
    # _resend_detail needs a real-ish ticket
    from bot.glpi.models import Ticket

    client.get_ticket.return_value = Ticket(
        id=TICKET, name="тест", content="c", status=1, urgency=3
    )
    client.list_followups.return_value = []
    bot = FakeBot()
    await dp.storage.set_state(_key(REQUESTER_ID, REQUESTER_ID), MyTickets.commenting.state)
    await dp.storage.set_data(_key(REQUESTER_ID, REQUESTER_ID), {"ticket_id": TICKET})

    await dp.feed_update(bot, _dm_msg(bot, 1, REQUESTER_ID, "всё ещё не работает"))

    # history line in the card edit...
    assert any("💬 Комментарий (Заявитель)" in text for _, _, text in bot.edits)
    # ...plus exactly ONE short reply ping to the group
    group_msgs = [t for c, t in bot.sent if c == GROUP]
    assert group_msgs == [texts.reply_new_comment(TICKET)]


async def test_bot_close_notifies_requester_with_solution_immediately(env):
    dp, client, repo = env
    await _seed_card(repo)
    # the ticket was created via the bot -> the requester is known
    await repo.track_ticket(
        ticket_id=TICKET, requester_tg_id=REQUESTER_ID, requester_glpi_id=8, status=1, now=0
    )
    bot = FakeBot()

    await dp.feed_update(bot, _group_cb(bot, 1, TECH_ID, f"ta:close:{TICKET}"))
    await dp.feed_update(bot, _dm_msg(bot, 2, TECH_ID, "заменил картридж"))

    # the requester gets the solution PROPOSAL right away, with the text
    to_requester = [t for c, t in bot.sent if c == REQUESTER_ID]
    assert to_requester == [
        "✅ По заявке №5 предложено решение — Техник: заменил картридж\n\nПроблема решена?"
    ]
    # the tracked status is bumped: SOLVED, not closed (awaiting confirmation)
    tracked = await repo.get_tracked_ticket(TICKET)
    assert tracked.last_status == 5 and tracked.active
    # and the solver is remembered for a possible return-to-work ping
    assert await repo.get_solver(TICKET) == (TECH_ID, "Техник")


def _dm_cb(bot, uid, from_id, data):
    msg = Message(
        message_id=uid,
        date=_DATE,
        chat=Chat(id=from_id, type="private"),
        from_user=TgUser(id=BOT_ID, is_bot=True, first_name="bot"),
        text="решение предложено",
    ).as_(bot)
    cb = CallbackQuery(
        id=str(uid),
        from_user=TgUser(id=from_id, is_bot=False, first_name="U"),
        chat_instance="ci",
        message=msg,
        data=data,
    ).as_(bot)
    return Update(update_id=uid, callback_query=cb)


async def _solve_via_bot(dp, repo, bot):
    """Common preamble: tracked ticket + card, tech proposes a solution."""
    await _seed_card(repo)
    await repo.track_ticket(
        ticket_id=TICKET, requester_tg_id=REQUESTER_ID, requester_glpi_id=8, status=1, now=0
    )
    await dp.feed_update(bot, _group_cb(bot, 1, TECH_ID, f"ta:close:{TICKET}"))
    await dp.feed_update(bot, _dm_msg(bot, 2, TECH_ID, "заменил картридж"))


async def test_full_cycle_solved_then_confirmed_closes(env):
    dp, client, repo = env
    bot = FakeBot()
    await _solve_via_bot(dp, repo, bot)
    assert (await repo.get_tracked_ticket(TICKET)).last_status == 5  # solved, not closed

    await dp.feed_update(bot, _dm_cb(bot, 3, REQUESTER_ID, f"rs:ok:{TICKET}"))

    client.set_ticket_status.assert_awaited_once_with(TICKET, 6)  # closed on confirm
    tracked = await repo.get_tracked_ticket(TICKET)
    assert tracked.last_status == 6 and not tracked.active
    # requester's prompt edited into a thank-you (buttons gone)
    assert any(c == REQUESTER_ID and "закрыта, спасибо" in t for c, t in bot.sent)
    # card history + group ping
    assert any("👍 Заявитель подтвердил решение" in text for _, _, text in bot.edits)
    assert any(c == GROUP and "подтвердил решение по заявке №5" in t for c, t in bot.sent)


async def test_full_cycle_solved_then_returned_to_work(env):
    dp, client, repo = env
    bot = FakeBot()
    await _solve_via_bot(dp, repo, bot)
    bot.sent.clear()
    bot.edits.clear()

    await dp.feed_update(bot, _dm_cb(bot, 3, REQUESTER_ID, f"rs:back:{TICKET}"))
    assert any(c == REQUESTER_ID and "осталось нерешённым" in t for c, t in bot.sent)
    await dp.feed_update(bot, _dm_msg(bot, 4, REQUESTER_ID, "всё ещё шумит"))

    # the reason lands in GLPI as a requester followup, status back to assigned
    client.add_followup.assert_awaited_once_with(TICKET, "Заявитель:\nвсё ещё шумит")
    client.set_ticket_status.assert_awaited_once_with(TICKET, 2)
    tracked = await repo.get_tracked_ticket(TICKET)
    assert tracked.last_status == 2 and tracked.active
    # the solving tech gets a DM, the group gets the ping, the card the history
    assert any(
        c == TECH_ID and "вернул заявку №5 в работу: всё ещё шумит" in t for c, t in bot.sent
    )
    assert any(c == GROUP and "вернул заявку №5 в работу" in t for c, t in bot.sent)
    assert any("↩️ Возвращена в работу заявителем" in text for _, _, text in bot.edits)
    # requester acknowledged, FSM cleared
    assert any(c == REQUESTER_ID and t == texts.RETURNED_ACK for c, t in bot.sent)
    assert await dp.storage.get_state(_key(REQUESTER_ID, REQUESTER_ID)) is None


async def test_confirm_by_someone_else_is_refused(env):
    dp, client, repo = env
    bot = FakeBot()
    await _solve_via_bot(dp, repo, bot)
    bot.sent.clear()

    # the tech (not the requester) somehow presses the requester's button
    await dp.feed_update(bot, _dm_cb(bot, 3, TECH_ID, f"rs:ok:{TICKET}"))
    client.set_ticket_status.assert_not_awaited()
    assert texts.STALE_BUTTON in bot.toasts
