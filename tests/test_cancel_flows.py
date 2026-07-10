"""Every text-awaiting FSM state must offer an explicit way out.

Each dialog: enter it → cancel (inline «❌ Отмена» or /cancel) → the state is
cleared, the reply carries the role menu, and GLPI is never touched.
Runs on the full production dispatcher.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, Chat, Message, ReplyKeyboardMarkup, Update
from aiogram.types import User as TgUser

from bot import texts
from bot.db.repo import Repo
from bot.handlers.my_tickets import MyTickets

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
        self.sent: list[tuple[object, str, object]] = []  # (chat, text, markup)
        self.toasts: list[str] = []

    async def __call__(self, method, **kwargs):
        name = type(method).__name__
        if name == "SendMessage":
            self.sent.append((method.chat_id, method.text, method.reply_markup))
        elif name == "AnswerCallbackQuery" and method.text:
            self.toasts.append(method.text)
        return MagicMock()

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent.append((chat_id, text, reply_markup))
        return MagicMock()

    async def edit_message_text(self, *a, **k):
        return MagicMock()


def _glpi_untouched(client: AsyncMock) -> bool:
    """No mutating GLPI call may happen on a cancel."""
    return not any(
        m.called
        for m in (
            client.add_followup,
            client.add_solution,
            client.set_ticket_status,
            client.assign_ticket,
            client.reassign_ticket,
        )
    )


@pytest.fixture
async def env(tmp_path):
    repo = Repo(str(tmp_path / "cancel.sqlite3"))
    await repo.connect()
    now = int(time.time())
    await repo.upsert_link(
        tg_id=TECH_ID, glpi_users_id=9, display_name="Техник", is_tech=True, now=now
    )
    await repo.upsert_link(
        tg_id=REQUESTER_ID, glpi_users_id=8, display_name="Заявитель", is_tech=False, now=now
    )
    await repo.track_ticket(
        ticket_id=TICKET, requester_tg_id=REQUESTER_ID, requester_glpi_id=8, status=5, now=now
    )
    client = AsyncMock()
    settings = Settings()
    settings.tech_group_chat_id = GROUP
    dp = build_dispatcher(client, repo, settings)
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


def _cb(bot, uid, from_id, data, *, chat_id, chat_type):
    msg = Message(
        message_id=uid,
        date=_DATE,
        chat=Chat(id=chat_id, type=chat_type),
        from_user=TgUser(id=BOT_ID, is_bot=True, first_name="bot"),
        text="подсказка",
    ).as_(bot)
    cb = CallbackQuery(
        id=str(uid),
        from_user=TgUser(id=from_id, is_bot=False, first_name="U"),
        chat_instance="ci",
        message=msg,
        data=data,
    ).as_(bot)
    return Update(update_id=uid, callback_query=cb)


def _dm_cb(bot, uid, from_id, data):
    return _cb(bot, uid, from_id, data, chat_id=from_id, chat_type="private")


def _group_cb(bot, uid, from_id, data):
    return _cb(bot, uid, from_id, data, chat_id=GROUP, chat_type="supergroup")


async def _state(dp, chat_id, user_id):
    return await dp.storage.get_state(StorageKey(bot_id=BOT_ID, chat_id=chat_id, user_id=user_id))


def _menu_rows(markup) -> list[list[str]]:
    assert isinstance(markup, ReplyKeyboardMarkup)
    return [[b.text for b in row] for row in markup.keyboard]


# -- tech dialogs (comment / solution), inline cancel ---------------------------
@pytest.mark.parametrize("action", ["comment", "close"])
async def test_tech_dialog_inline_cancel(env, action):
    dp, client = env
    bot = FakeBot()
    await dp.feed_update(bot, _group_cb(bot, 1, TECH_ID, f"ta:{action}:{TICKET}"))
    assert await _state(dp, TECH_ID, TECH_ID) is not None
    # the DM prompt carries the cancel button and mentions it
    _chat, prompt, kb = bot.sent[-1]
    assert "Отмена" in prompt
    assert kb.inline_keyboard[-1][0].callback_data == "dlg:cancel"

    await dp.feed_update(bot, _dm_cb(bot, 2, TECH_ID, "dlg:cancel"))
    assert await _state(dp, TECH_ID, TECH_ID) is None
    chat, text, markup = bot.sent[-1]
    assert (chat, text) == (TECH_ID, texts.DIALOG_CANCELLED)
    assert [texts.BTN_TECH_TICKETS, texts.BTN_STATS] in _menu_rows(markup)  # tech menu
    assert _glpi_untouched(client)


async def test_tech_dialog_cancel_command(env):
    dp, client = env
    bot = FakeBot()
    await dp.feed_update(bot, _group_cb(bot, 1, TECH_ID, f"ta:close:{TICKET}"))
    await dp.feed_update(bot, _dm_msg(bot, 2, TECH_ID, "/cancel"))
    assert await _state(dp, TECH_ID, TECH_ID) is None
    chat, text, markup = bot.sent[-1]
    assert (chat, text) == (TECH_ID, texts.DIALOG_CANCELLED)
    assert [texts.BTN_TECH_TICKETS, texts.BTN_STATS] in _menu_rows(markup)
    assert _glpi_untouched(client)


# -- requester dialogs: comment, close reason, return reason --------------------
@pytest.mark.parametrize(
    ("entry", "state"),
    [
        (f"mt:comment:{TICKET}", MyTickets.commenting),
        (f"mt:close:{TICKET}", MyTickets.closing),
        (f"rs:back:{TICKET}", MyTickets.returning),
    ],
)
async def test_requester_dialog_inline_cancel(env, entry, state):
    dp, client = env
    bot = FakeBot()
    await dp.feed_update(bot, _dm_cb(bot, 1, REQUESTER_ID, entry))
    assert await _state(dp, REQUESTER_ID, REQUESTER_ID) == state.state

    await dp.feed_update(bot, _dm_cb(bot, 2, REQUESTER_ID, "dlg:cancel"))
    assert await _state(dp, REQUESTER_ID, REQUESTER_ID) is None
    chat, text, markup = bot.sent[-1]
    assert (chat, text) == (REQUESTER_ID, texts.DIALOG_CANCELLED)
    rows = _menu_rows(markup)
    assert rows == [[texts.BTN_NEW_TICKET, texts.BTN_MY_TICKETS]]  # non-tech menu
    assert _glpi_untouched(client)


async def test_requester_cancel_command_resets_and_shows_menu(env):
    dp, client = env
    bot = FakeBot()
    await dp.feed_update(bot, _dm_cb(bot, 1, REQUESTER_ID, f"mt:comment:{TICKET}"))
    await dp.feed_update(bot, _dm_msg(bot, 2, REQUESTER_ID, "/cancel"))
    assert await _state(dp, REQUESTER_ID, REQUESTER_ID) is None
    chat, text, markup = bot.sent[-1]
    assert (chat, text) == (REQUESTER_ID, texts.DIALOG_CANCELLED)
    assert isinstance(markup, ReplyKeyboardMarkup)
    assert _glpi_untouched(client)


async def test_close_prompt_mentions_cancel_and_carries_the_button(env):
    dp, client = env
    bot = FakeBot()
    await dp.feed_update(bot, _dm_cb(bot, 1, REQUESTER_ID, f"mt:close:{TICKET}"))
    _chat, prompt, kb = bot.sent[-1]
    assert "Отмена" in prompt
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert data == ["mt:close_empty", "dlg:cancel"]


async def test_cancel_does_not_leak_between_users(env):
    # a cancel by the tech must not clear the requester's dialog
    dp, client = env
    bot = FakeBot()
    await dp.feed_update(bot, _dm_cb(bot, 1, REQUESTER_ID, f"mt:comment:{TICKET}"))
    await dp.feed_update(bot, _group_cb(bot, 2, TECH_ID, f"ta:close:{TICKET}"))
    await dp.feed_update(bot, _dm_cb(bot, 3, TECH_ID, "dlg:cancel"))
    assert await _state(dp, TECH_ID, TECH_ID) is None
    assert await _state(dp, REQUESTER_ID, REQUESTER_ID) == MyTickets.commenting.state
