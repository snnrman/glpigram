"""Dispatch-level tests of the account-linking flow (feature 2).

Covers: /start for unlinked users, the login and name paths, the admin
confirm/reject buttons, and the tech-group trust boundary.
"""

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
from bot.db.repo import Repo
from bot.glpi.models import User
from bot.handlers.linking import Linking, build_linking_router

BOT_ID = 42
USER_CHAT = 1001  # the employee's private chat
ADMIN_ID = 2002
TECH_CHAT = -100
GLPI_USER = User(id=77, name="jdoe", firstname="Иван", realname="Петров", is_active=True)
_DATE = datetime(2020, 1, 1, tzinfo=UTC)


class FakeBot:
    id = BOT_ID

    def __init__(self) -> None:
        self.sent: list[tuple[object, str]] = []  # (chat_id, text)
        self.toasts: list[str] = []  # callback answers

    async def __call__(self, method, **kwargs):
        name = type(method).__name__
        if name == "SendMessage":
            self.sent.append((method.chat_id, method.text))
        elif name == "EditMessageText":
            self.sent.append((method.chat_id, method.text))
        elif name == "AnswerCallbackQuery" and method.text:
            self.toasts.append(method.text)
        return MagicMock()

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))
        return MagicMock()


@pytest.fixture
async def repo(tmp_path):
    r = Repo(str(tmp_path / "lk.sqlite3"))
    await r.connect()
    yield r
    await r.close()


def _harness(repo, client):
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp.include_router(
        build_linking_router(repo, client, tech_group_chat_id=TECH_CHAT, tech_group_id=7)
    )
    ctx = FSMContext(
        storage=storage, key=StorageKey(bot_id=BOT_ID, chat_id=USER_CHAT, user_id=USER_CHAT)
    )
    return dp, ctx


def _client():
    client = AsyncMock()
    client.find_user_by_login.return_value = GLPI_USER
    client.search_users_by_name.return_value = [GLPI_USER]
    client.get_user.return_value = GLPI_USER
    client.user_in_group.return_value = False
    return client


def _user_msg(bot, uid, text):
    msg = Message(
        message_id=uid,
        date=_DATE,
        chat=Chat(id=USER_CHAT, type="private"),
        from_user=TgUser(id=USER_CHAT, is_bot=False, first_name="Иван"),
        text=text,
    ).as_(bot)
    return Update(update_id=uid, message=msg)


def _cb(bot, uid, data, *, chat_id, from_id):
    msg = Message(
        message_id=uid,
        date=_DATE,
        chat=Chat(id=chat_id, type="supergroup" if chat_id < 0 else "private"),
        from_user=TgUser(id=BOT_ID, is_bot=True, first_name="bot"),
        text="card",
    ).as_(bot)
    cb = CallbackQuery(
        id=str(uid),
        from_user=TgUser(id=from_id, is_bot=False, first_name="A"),
        chat_instance="ci",
        message=msg,
        data=data,
    ).as_(bot)
    return Update(update_id=uid, callback_query=cb)


def _chat_msgs(bot, chat_id):
    return [t for c, t in bot.sent if c == chat_id]


async def test_start_unlinked_asks_for_credentials(repo):
    dp, ctx = _harness(repo, _client())
    bot = FakeBot()
    await dp.feed_update(bot, _user_msg(bot, 1, "/start"))
    assert _chat_msgs(bot, USER_CHAT) == [texts.LINK_WELCOME]
    assert await ctx.get_state() == Linking.awaiting_login


async def test_login_path_posts_card_to_tech_group(repo):
    dp, ctx = _harness(repo, _client())
    bot = FakeBot()
    await ctx.set_state(Linking.awaiting_login)

    await dp.feed_update(bot, _user_msg(bot, 1, "CORP\\jdoe"))
    tech = _chat_msgs(bot, TECH_CHAT)
    assert tech and "Запрос на привязку" in tech[0]
    assert "jdoe" in tech[0]
    assert texts.LINK_PENDING in _chat_msgs(bot, USER_CHAT)
    assert await ctx.get_state() is None  # dialog finished, waiting for admin
    assert await repo.get_by_tg(USER_CHAT) is None  # NOT linked before confirm


async def test_unknown_login_asks_retry_and_keeps_state(repo):
    client = _client()
    client.find_user_by_login.return_value = None
    dp, ctx = _harness(repo, client)
    bot = FakeBot()
    await ctx.set_state(Linking.awaiting_login)

    await dp.feed_update(bot, _user_msg(bot, 1, "ghost"))
    assert texts.LINK_USER_NOT_FOUND in _chat_msgs(bot, USER_CHAT)
    assert await ctx.get_state() == Linking.awaiting_login  # can retry
    assert _chat_msgs(bot, TECH_CHAT) == []


async def test_name_path_single_candidate_pick_posts_card(repo):
    dp, ctx = _harness(repo, _client())
    bot = FakeBot()
    await ctx.set_state(Linking.awaiting_login)

    await dp.feed_update(bot, _user_msg(bot, 1, "Иван Петров"))
    assert any("Это вы?" in t for t in _chat_msgs(bot, USER_CHAT))
    assert _chat_msgs(bot, TECH_CHAT) == []  # nothing to admins yet

    # user taps "Это я" -> card goes to the tech group
    await dp.feed_update(
        bot, _cb(bot, 2, f"lk:pick:{GLPI_USER.id}", chat_id=USER_CHAT, from_id=USER_CHAT)
    )
    assert any("Запрос на привязку" in t for t in _chat_msgs(bot, TECH_CHAT))
    assert await ctx.get_state() is None


async def test_admin_confirm_links_and_notifies_user(repo):
    dp, _ = _harness(repo, _client())
    bot = FakeBot()

    await dp.feed_update(
        bot,
        _cb(bot, 1, f"lk:ok:{USER_CHAT}:{GLPI_USER.id}", chat_id=TECH_CHAT, from_id=ADMIN_ID),
    )
    link = await repo.get_by_tg(USER_CHAT)
    assert link is not None and link.glpi_users_id == GLPI_USER.id
    assert texts.LINK_CONFIRMED in _chat_msgs(bot, USER_CHAT)
    assert any("Привязка подтверждена" in t for t in _chat_msgs(bot, TECH_CHAT))


async def test_admin_reject_notifies_user_without_linking(repo):
    dp, _ = _harness(repo, _client())
    bot = FakeBot()

    await dp.feed_update(
        bot,
        _cb(bot, 1, f"lk:no:{USER_CHAT}:{GLPI_USER.id}", chat_id=TECH_CHAT, from_id=ADMIN_ID),
    )
    assert await repo.get_by_tg(USER_CHAT) is None
    assert texts.LINK_REJECTED in _chat_msgs(bot, USER_CHAT)


async def test_confirm_outside_tech_group_is_refused(repo):
    """Trust boundary: a forwarded card must not be confirmable elsewhere."""
    dp, _ = _harness(repo, _client())
    bot = FakeBot()

    await dp.feed_update(
        bot,
        _cb(bot, 1, f"lk:ok:{USER_CHAT}:{GLPI_USER.id}", chat_id=-999, from_id=ADMIN_ID),
    )
    assert await repo.get_by_tg(USER_CHAT) is None  # nothing linked
    assert texts.CB_TECH_GROUP_ONLY in bot.toasts


async def test_start_unlinked_ignores_welcome_message(repo, monkeypatch):
    # WELCOME_MESSAGE is for LINKED users only: an unlinked user must always
    # get the sign-in prompt (a "create tickets" welcome would mislead them).
    monkeypatch.setenv("WELCOME_MESSAGE", "Кастомное <b>приветствие</b>.")
    dp, ctx = _harness(repo, _client())
    bot = FakeBot()
    await dp.feed_update(bot, _user_msg(bot, 1, "/start"))
    msgs = _chat_msgs(bot, USER_CHAT)
    assert msgs == [texts.LINK_WELCOME]  # auth text, custom ignored
    assert await ctx.get_state() == Linking.awaiting_login  # straight into linking


async def test_start_linked_uses_welcome_message_when_set(repo, monkeypatch):
    monkeypatch.setenv("WELCOME_MESSAGE", "Своё приветствие")
    await repo.upsert_link(tg_id=USER_CHAT, glpi_users_id=1, display_name="U", is_tech=False, now=0)
    dp, _ = _harness(repo, _client())
    bot = FakeBot()
    await dp.feed_update(bot, _user_msg(bot, 1, "/start"))
    msgs = _chat_msgs(bot, USER_CHAT)
    assert msgs == ["Своё приветствие"]


async def test_start_falls_back_to_default_when_unset(repo, monkeypatch):
    monkeypatch.delenv("WELCOME_MESSAGE", raising=False)
    dp, _ = _harness(repo, _client())
    bot = FakeBot()
    await dp.feed_update(bot, _user_msg(bot, 1, "/start"))
    assert _chat_msgs(bot, USER_CHAT) == [texts.LINK_WELCOME]


async def test_start_linked_without_welcome_uses_default_greeting(repo):
    await repo.upsert_link(tg_id=USER_CHAT, glpi_users_id=1, display_name="U", is_tech=False, now=0)
    dp, ctx = _harness(repo, _client())
    bot = FakeBot()
    await dp.feed_update(bot, _user_msg(bot, 1, "/start"))
    assert _chat_msgs(bot, USER_CHAT) == [texts.START_GREETING]
    assert "🆕 Новая заявка" in texts.START_GREETING  # points at the menu buttons
    assert await ctx.get_state() is None  # no linking FSM for linked users
