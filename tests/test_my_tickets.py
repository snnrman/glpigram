"""Coverage for the /tickets module: rendering helpers + the self-close flow."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TgUser

from bot import texts
from bot.db.repo import LinkedUser, Repo
from bot.glpi.client import TICKET_STATUS_CLOSED, TICKET_STATUS_NEW, _parse_user_refs
from bot.glpi.models import Ticket, TicketSummary
from bot.handlers.my_tickets import (
    MyTickets,
    _close_prompt_keyboard,
    _detail_keyboard,
    _list_keyboard,
    build_my_tickets_router,
)


def test_router_builds():
    router = build_my_tickets_router(
        MagicMock(), MagicMock(), tech_group_chat_id=-100, ticket_front_base=None
    )
    assert router.name == "my_tickets"


def test_list_keyboard_one_button_per_ticket():
    summaries = [
        TicketSummary(id=5, title="Принтер", status=1),
        TicketSummary(id=6, title="ВПН", status=2),
    ]
    kb = _list_keyboard(summaries)
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert data == ["mt:open:5", "mt:open:6"]


def test_detail_keyboard_new_shows_remind_and_close():
    kb = _detail_keyboard(5, closable=True, remindable=True)
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert data == ["mt:comment:5", "mt:remind:5", "mt:close:5", "mt:list"]


def test_detail_keyboard_taken_hides_remind_and_close():
    kb = _detail_keyboard(5, closable=False, remindable=False)
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert data == ["mt:comment:5", "mt:list"]
    assert "mt:remind:5" not in data and "mt:close:5" not in data


def test_close_prompt_keyboard_has_no_comment_button():
    data = [b.callback_data for row in _close_prompt_keyboard().inline_keyboard for b in row]
    assert data == ["mt:close_empty"]


def test_notify_closed_by_requester_mentions_assignee_and_escapes():
    out = texts.notify_closed_by_requester(
        ticket_id=7, reason="дубль <b>x</b>", assignees=["Иван Петров"]
    )
    assert "№7" in out
    assert "<b>x</b>" not in out  # reason escaped
    assert "Был назначен: Иван Петров" in out


def test_notify_closed_by_requester_without_assignee():
    out = texts.notify_closed_by_requester(ticket_id=7, reason="не актуально", assignees=[])
    assert "Был назначен" not in out


def test_notify_closed_by_requester_without_reason():
    out = texts.notify_closed_by_requester(ticket_id=7, reason=None, assignees=[])
    assert "без комментария" in out.lower()
    assert "Причина" not in out


def test_parse_user_refs_ids_names_and_junk():
    assert _parse_user_refs(None) == ([], None)
    assert _parse_user_refs("0") == ([], None)
    assert _parse_user_refs("42") == ([42], None)
    assert _parse_user_refs(["42", "7"]) == ([42, 7], None)
    assert _parse_user_refs("Иван Петров") == ([], "Иван Петров")
    assert _parse_user_refs(["42", "Иван"]) == ([42], "Иван")


def test_detail_text_escapes_title_and_uses_status_label():
    out = texts.ticket_detail(
        ticket_id=5, title="A <b>x</b>", status=2, assignee=None, followups=[]
    )
    assert "<b>x</b>" not in out  # title escaped
    assert texts.ticket_status_label(2) in out
    assert texts.MYT_NO_FOLLOWUPS in out
    assert texts.MYT_UNASSIGNED in out  # no assignee


def test_followup_line_escapes_and_cleans_html():
    line = texts.followup_line("Иван", "<p>привет &amp; пока</p>")
    assert "<p>" not in line  # tags stripped
    # entity decoded by clean, then re-escaped for safe HTML rendering
    assert "привет &amp; пока" in line
    assert "<b>Иван:</b>" in line


# --- self-close flow (dispatch-level, both paths) ---------------------------
# asyncio_mode=auto (pyproject) runs the async tests without an explicit mark.

BOT_ID = 42
CHAT = 1001
TECH_CHAT = -100
TICKET = 7
_DATE = datetime(2020, 1, 1, tzinfo=UTC)
_LINK = LinkedUser(
    tg_id=CHAT, glpi_users_id=99, display_name="Пётр", is_tech=False, linked_at=0, checked_at=0
)


class FakeBot:
    id = BOT_ID

    def __init__(self) -> None:
        self.sent: list[tuple[object, object]] = []  # (chat_id, text)

    async def __call__(self, method, **kwargs):
        # message.answer / edit_text go through `await bot(SendMessage(...))`.
        self.sent.append((getattr(method, "chat_id", None), getattr(method, "text", None)))
        return MagicMock()

    async def send_message(self, chat_id, text, **kwargs):
        # notify._send calls bot.send_message(...) directly.
        self.sent.append((chat_id, text))
        return MagicMock()


async def _inject_link(handler, event, data):
    data["link"] = _LINK
    return await handler(event, data)


def _make_client() -> AsyncMock:
    client = AsyncMock()
    client.get_ticket_assignees.return_value = ["Иван Петров"]
    client.add_followup.return_value = 555
    client.set_ticket_status.return_value = None
    return client


async def _closing_dispatcher(bot: FakeBot, client: AsyncMock):
    storage = MemoryStorage()
    router = build_my_tickets_router(client, AsyncMock(), tech_group_chat_id=TECH_CHAT)
    router.message.middleware(_inject_link)
    router.callback_query.middleware(_inject_link)
    dp = Dispatcher(storage=storage)
    dp.include_router(router)
    ctx = FSMContext(storage=storage, key=StorageKey(bot_id=BOT_ID, chat_id=CHAT, user_id=CHAT))
    await ctx.set_state(MyTickets.closing)
    await ctx.set_data({"close_ticket_id": TICKET})
    return dp, ctx


def _text_update(bot: FakeBot, uid: int, text: str) -> Update:
    msg = Message(
        message_id=uid,
        date=_DATE,
        chat=Chat(id=CHAT, type="private"),
        from_user=TgUser(id=CHAT, is_bot=False, first_name="U"),
        text=text,
    ).as_(bot)
    return Update(update_id=uid, message=msg)


def _cb_update(bot: FakeBot, uid: int, data: str) -> Update:
    msg = Message(
        message_id=uid,
        date=_DATE,
        chat=Chat(id=CHAT, type="private"),
        from_user=TgUser(id=CHAT, is_bot=False, first_name="U"),
        text="prompt",
    ).as_(bot)
    cb = CallbackQuery(
        id=str(uid),
        from_user=TgUser(id=CHAT, is_bot=False, first_name="U"),
        chat_instance="ci",
        message=msg,
        data=data,
    ).as_(bot)
    return Update(update_id=uid, callback_query=cb)


async def test_close_with_reason_text_immediately_closes():
    bot, client = FakeBot(), _make_client()
    dp, ctx = await _closing_dispatcher(bot, client)

    await dp.feed_update(bot, _text_update(bot, 1, "уже не актуально"))

    # No confirmation step: followup added + ticket closed straight away.
    client.add_followup.assert_awaited_once()
    assert "уже не актуально" in client.add_followup.await_args.args[1]
    client.set_ticket_status.assert_awaited_once_with(TICKET, TICKET_STATUS_CLOSED)
    assert await ctx.get_state() is None
    # tech group notified with the reason
    tech = [t for c, t in bot.sent if c == TECH_CHAT]
    assert tech and "уже не актуально" in tech[-1]


async def test_close_without_comment_button_closes_without_followup():
    bot, client = FakeBot(), _make_client()
    dp, ctx = await _closing_dispatcher(bot, client)

    await dp.feed_update(bot, _cb_update(bot, 2, "mt:close_empty"))

    client.add_followup.assert_not_awaited()  # no reason -> no followup
    client.set_ticket_status.assert_awaited_once_with(TICKET, TICKET_STATUS_CLOSED)
    assert await ctx.get_state() is None
    tech = [t for c, t in bot.sent if c == TECH_CHAT]
    assert tech and "без комментария" in tech[-1].lower()


# --- remind about a ticket (cooldown) ---------------------------------------
@pytest.fixture
async def repo(tmp_path):
    r = Repo(str(tmp_path / "myt.sqlite3"))
    await r.connect()
    yield r
    await r.close()


def _remind_dispatcher(bot: FakeBot, repo: Repo, *, status: int = TICKET_STATUS_NEW):
    client = AsyncMock()
    client.get_ticket.return_value = Ticket(
        id=TICKET,
        name="Печать",
        content="c",
        status=status,
        urgency=3,
        date_creation="2020-01-01 00:00:00",
    )
    router = build_my_tickets_router(
        client, repo, tech_group_chat_id=TECH_CHAT, remind_cooldown_hours=4
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    return dp


def _tech_msgs(bot: FakeBot) -> list[str]:
    return [t for c, t in bot.sent if c == TECH_CHAT]


async def test_remind_sends_then_cooldown_blocks(repo):
    bot = FakeBot()
    dp = _remind_dispatcher(bot, repo)

    await dp.feed_update(bot, _cb_update(bot, 1, f"mt:remind:{TICKET}"))
    assert len(_tech_msgs(bot)) == 1
    assert "напоминает" in _tech_msgs(bot)[0].lower()
    assert texts.MYT_REMIND_SENT in [t for _, t in bot.sent]
    assert await repo.get_last_remind(TICKET) is not None

    # Second tap within the cooldown: nothing new to the group, a toast instead.
    await dp.feed_update(bot, _cb_update(bot, 2, f"mt:remind:{TICKET}"))
    assert len(_tech_msgs(bot)) == 1  # still only the first reminder
    assert any(t and "повторно можно через" in t.lower() for _, t in bot.sent)


async def test_remind_blocked_when_ticket_taken(repo):
    bot = FakeBot()
    dp = _remind_dispatcher(bot, repo, status=2)  # already assigned

    await dp.feed_update(bot, _cb_update(bot, 1, f"mt:remind:{TICKET}"))
    assert _tech_msgs(bot) == []  # nothing sent to the group
    assert await repo.get_last_remind(TICKET) is None
