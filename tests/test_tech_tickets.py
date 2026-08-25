"""«👨‍💻 В работе»: the tech's assigned-tickets list and detail view.

Dispatch tests run on the full production dispatcher (router order + auth
middleware), same harness as test_stats.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TgUser

from bot import texts
from bot.db.repo import Repo
from bot.glpi.models import Ticket, TicketSummary

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("GLPI_API_URL", "http://127.0.0.1/apirest.php")
os.environ.setdefault("GLPI_USER_TOKEN", "u")

from bot.config import Settings  # noqa: E402
from bot.main import build_dispatcher  # noqa: E402

BOT_ID = 42
TECH_ID = 2002
TECH_GLPI_ID = 9
USER_ID = 1001
_DATE = datetime(2020, 1, 1, tzinfo=UTC)

_SUMMARIES = [
    TicketSummary(id=5, title="Принтер", status=2),
    TicketSummary(id=8, title="ВПН", status=4),
    TicketSummary(id=7, title="Почта <b>x</b>", status=5),  # solved -> waiting group
]


class FakeBot:
    id = BOT_ID

    def __init__(self) -> None:
        self.sent: list[tuple[object, str, object]] = []
        self.edits: list[tuple[str, object]] = []  # (text, reply_markup)
        self.toasts: list[str] = []

    async def __call__(self, method, **kwargs):
        name = type(method).__name__
        if name == "SendMessage":
            self.sent.append((method.chat_id, method.text, method.reply_markup))
        elif name == "EditMessageText":
            self.edits.append((method.text, method.reply_markup))
        elif name == "AnswerCallbackQuery" and method.text:
            self.toasts.append(method.text)
        return MagicMock()

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent.append((chat_id, text, reply_markup))
        return MagicMock()


def _client() -> AsyncMock:
    client = AsyncMock()
    client.search_tech_open_tickets.return_value = list(_SUMMARIES)
    client.get_ticket.return_value = Ticket(id=5, name="Принтер", content="c", status=2, urgency=3)
    client.list_followups.return_value = []
    client.get_ticket_assignees.return_value = ["Техник"]
    client.get_ticket_requester.return_value = (8, "Олег Заявитель")
    return client


@pytest.fixture
async def env(tmp_path):
    repo = Repo(str(tmp_path / "tt.sqlite3"))
    await repo.connect()
    now = int(time.time())
    await repo.upsert_link(
        tg_id=TECH_ID, glpi_users_id=TECH_GLPI_ID, display_name="Техник", is_tech=True, now=now
    )
    await repo.upsert_link(
        tg_id=USER_ID, glpi_users_id=8, display_name="Юзер", is_tech=False, now=now
    )
    client = _client()
    dp = build_dispatcher(client, repo, Settings())
    yield dp, client
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


def _dm_cb(bot, uid, from_id, data):
    msg = Message(
        message_id=uid,
        date=_DATE,
        chat=Chat(id=from_id, type="private"),
        from_user=TgUser(id=BOT_ID, is_bot=True, first_name="bot"),
        text="список",
    ).as_(bot)
    cb = CallbackQuery(
        id=str(uid),
        from_user=TgUser(id=from_id, is_bot=False, first_name="U"),
        chat_instance="ci",
        message=msg,
        data=data,
    ).as_(bot)
    return Update(update_id=uid, callback_query=cb)


async def test_tech_sees_two_groups_and_buttons(env):
    dp, client = env
    bot = FakeBot()
    await dp.feed_update(bot, _dm_msg(bot, 1, TECH_ID, texts.BTN_TECH_TICKETS))
    client.search_tech_open_tickets.assert_awaited_once_with(TECH_GLPI_ID)
    chat, text, kb = bot.sent[-1]
    assert chat == TECH_ID
    assert "№5" in text and "№8" in text and "№7" in text
    # solved goes to the waiting group, after the in-work section
    assert text.index("№7") > text.index("Ждут подтверждения")
    assert text.index("№5") < text.index("Ждут подтверждения")
    assert "<b>x</b>" not in text  # titles escaped
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert data == ["tt:open:5", "tt:open:8", "tt:open:7"]  # in-work first


async def test_empty_list_says_no_assigned_tickets(env):
    dp, client = env
    client.search_tech_open_tickets.return_value = []
    bot = FakeBot()
    await dp.feed_update(bot, _dm_msg(bot, 1, TECH_ID, texts.BTN_TECH_TICKETS))
    assert bot.sent[-1][1] == texts.TECH_TICKETS_EMPTY


async def test_regular_user_is_refused_and_glpi_not_called(env):
    dp, client = env
    bot = FakeBot()
    await dp.feed_update(bot, _dm_msg(bot, 1, USER_ID, texts.BTN_TECH_TICKETS))
    assert bot.sent[-1][1] == texts.TECH_ONLY
    client.search_tech_open_tickets.assert_not_called()


async def test_open_detail_has_reply_and_close_for_in_work(env):
    dp, client = env
    bot = FakeBot()
    await dp.feed_update(bot, _dm_cb(bot, 1, TECH_ID, "tt:open:5"))
    text, kb = bot.edits[-1]
    assert "Принтер" in text and texts.ticket_status_label(2) in text
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert data == ["ta:comment:5", "ta:close:5", "ta:handoff:5", "tt:list"]


async def test_solved_detail_hides_close(env):
    dp, client = env
    client.get_ticket.return_value = Ticket(id=7, name="Почта", content="c", status=5, urgency=3)
    bot = FakeBot()
    await dp.feed_update(bot, _dm_cb(bot, 1, TECH_ID, "tt:open:7"))
    _text, kb = bot.edits[-1]
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert data == ["ta:comment:7", "tt:list"]


async def test_back_button_re_renders_the_list(env):
    dp, client = env
    bot = FakeBot()
    await dp.feed_update(bot, _dm_cb(bot, 1, TECH_ID, "tt:list"))
    text, kb = bot.edits[-1]
    assert "№5" in text
    assert kb.inline_keyboard[0][0].callback_data == "tt:open:5"


async def test_detail_close_button_reaches_the_tech_close_flow(env):
    # The ta:close callback from the detail view starts the same DM solution
    # dialog as the group card (tech_actions router handles it).
    dp, client = env
    bot = FakeBot()
    await dp.feed_update(bot, _dm_cb(bot, 1, TECH_ID, "ta:close:5"))
    assert any(texts.tech_ask_solution(5) == text for _chat, text, _kb in bot.sent)


# --- «📥 Все заявки»: the untaken queue in the tech menu -----------------------
async def test_all_tickets_lists_unassigned_for_tech(env):
    dp, client = env
    client.search_unassigned_tickets.return_value = [
        TicketSummary(id=74, title="Не работает VPN", status=1),
        TicketSummary(id=73, title="Принтер <b>x</b>", status=1),
    ]
    bot = FakeBot()
    await dp.feed_update(bot, _dm_msg(bot, 1, TECH_ID, texts.BTN_ALL_TICKETS))
    client.search_unassigned_tickets.assert_awaited_once()
    chat, text, kb = bot.sent[-1]
    assert chat == TECH_ID
    assert "№74" in text and "№73" in text
    assert "<b>x</b>" not in text  # titles escaped
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert data == ["tt:openu:74", "tt:openu:73"]


async def test_all_tickets_empty_message(env):
    dp, client = env
    client.search_unassigned_tickets.return_value = []
    bot = FakeBot()
    await dp.feed_update(bot, _dm_msg(bot, 1, TECH_ID, texts.BTN_ALL_TICKETS))
    assert bot.sent[-1][1] == texts.ALL_TICKETS_EMPTY


async def test_all_tickets_refused_for_regular_user(env):
    dp, client = env
    bot = FakeBot()
    await dp.feed_update(bot, _dm_msg(bot, 1, USER_ID, texts.BTN_ALL_TICKETS))
    assert bot.sent[-1][1] == texts.TECH_ONLY
    client.search_unassigned_tickets.assert_not_called()


async def test_unassigned_detail_has_take_and_back_to_all(env):
    dp, client = env
    client.get_ticket.return_value = Ticket(
        id=74, name="Не работает VPN", content="c", status=1, urgency=3
    )
    client.get_ticket_assignees.return_value = []
    bot = FakeBot()
    await dp.feed_update(bot, _dm_cb(bot, 1, TECH_ID, "tt:openu:74"))
    text, kb = bot.edits[-1]
    assert "Не работает VPN" in text
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    # Full-width Take first; no handoff (nobody to hand off from); back to tt:all
    assert data == ["ta:take:74", "ta:comment:74", "ta:close:74", "tt:all"]


async def test_back_from_unassigned_detail_re_renders_queue(env):
    dp, client = env
    client.search_unassigned_tickets.return_value = [
        TicketSummary(id=74, title="Не работает VPN", status=1),
    ]
    bot = FakeBot()
    await dp.feed_update(bot, _dm_cb(bot, 1, TECH_ID, "tt:all"))
    text, kb = bot.edits[-1]
    assert "№74" in text
    assert kb.inline_keyboard[0][0].callback_data == "tt:openu:74"


# --- detail views show the requester (who filed the ticket) --------------------
async def test_detail_shows_requester(env):
    dp, client = env
    bot = FakeBot()
    await dp.feed_update(bot, _dm_cb(bot, 1, TECH_ID, "tt:open:5"))
    text, _kb = bot.edits[-1]
    assert "Заявитель: Олег Заявитель" in text


async def test_unassigned_detail_shows_requester(env):
    dp, client = env
    client.get_ticket.return_value = Ticket(id=74, name="VPN", content="c", status=1, urgency=3)
    client.get_ticket_assignees.return_value = []
    bot = FakeBot()
    await dp.feed_update(bot, _dm_cb(bot, 1, TECH_ID, "tt:openu:74"))
    text, _kb = bot.edits[-1]
    assert "Заявитель: Олег Заявитель" in text


async def test_detail_survives_requester_lookup_failure(env):
    from bot.glpi.client import GlpiError

    dp, client = env
    client.get_ticket_requester.side_effect = GlpiError("boom")
    bot = FakeBot()
    await dp.feed_update(bot, _dm_cb(bot, 1, TECH_ID, "tt:open:5"))
    text, _kb = bot.edits[-1]
    assert "Принтер" in text  # detail still renders
    assert "Заявитель:" not in text  # just without the requester row
