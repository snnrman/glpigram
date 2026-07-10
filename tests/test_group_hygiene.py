"""In groups the bot reacts ONLY to its inline buttons (and admin commands).

Free text, menu-button texts, /start and FSM dialogs are private-chat only:
a casual remark in the tech group must never trigger the free-text ticket
offer, linking prompts or dialog steps. Runs on the full production
dispatcher (router order + auth middleware + chat-type filters).
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
from bot.handlers.tech_actions import TechAction

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("GLPI_API_URL", "http://127.0.0.1/apirest.php")
os.environ.setdefault("GLPI_USER_TOKEN", "u")

from bot.config import Settings  # noqa: E402
from bot.main import build_dispatcher  # noqa: E402

BOT_ID = 42
TECH_ID = 2002
UNLINKED_ID = 3003
GROUP = -100
TICKET = 5
_DATE = datetime(2020, 1, 1, tzinfo=UTC)


class FakeBot:
    id = BOT_ID

    def __init__(self) -> None:
        self.sent: list[tuple[object, str]] = []
        self.edits: list[str] = []
        self.toasts: list[str] = []

    async def __call__(self, method, **kwargs):
        name = type(method).__name__
        if name == "SendMessage":
            self.sent.append((method.chat_id, method.text))
        elif name == "EditMessageText":
            self.edits.append(method.text)
        elif name == "AnswerCallbackQuery" and method.text:
            self.toasts.append(method.text)
        return MagicMock()

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))
        return MagicMock()

    def silent(self) -> bool:
        return not self.sent and not self.edits and not self.toasts


@pytest.fixture
async def env(tmp_path):
    repo = Repo(str(tmp_path / "hygiene.sqlite3"))
    await repo.connect()
    await repo.upsert_link(
        tg_id=TECH_ID, glpi_users_id=9, display_name="Техник", is_tech=True, now=int(time.time())
    )
    client = AsyncMock()
    client.add_solution.return_value = 3
    settings = Settings()
    settings.tech_group_chat_id = GROUP
    dp = build_dispatcher(client, repo, settings)
    yield dp, client
    await repo.close()


def _msg(bot, uid, from_id, text, *, chat_id, chat_type):
    msg = Message(
        message_id=uid,
        date=_DATE,
        chat=Chat(id=chat_id, type=chat_type),
        from_user=TgUser(id=from_id, is_bot=False, first_name="U"),
        text=text,
    ).as_(bot)
    return Update(update_id=uid, message=msg)


def _group_msg(bot, uid, from_id, text):
    return _msg(bot, uid, from_id, text, chat_id=GROUP, chat_type="supergroup")


def _dm_msg(bot, uid, from_id, text):
    return _msg(bot, uid, from_id, text, chat_id=from_id, chat_type="private")


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


async def test_plain_group_text_gets_no_reaction(env):
    dp, client = env
    bot = FakeBot()
    await dp.feed_update(bot, _group_msg(bot, 1, TECH_ID, "коллеги, кто видел кабель?"))
    assert bot.silent()  # no free-text ticket offer in groups


async def test_unlinked_user_group_text_gets_no_link_nag(env):
    dp, client = env
    bot = FakeBot()
    await dp.feed_update(bot, _group_msg(bot, 1, UNLINKED_ID, "просто реплика"))
    assert bot.silent()  # the auth middleware must not nag the whole group


@pytest.mark.parametrize("text", ["/new", "/start", "/tickets", "/stats"])
async def test_dialog_commands_are_ignored_in_groups(env, text):
    dp, client = env
    bot = FakeBot()
    await dp.feed_update(bot, _group_msg(bot, 1, TECH_ID, text))
    assert bot.silent()


async def test_menu_button_text_in_group_is_ignored(env):
    dp, client = env
    bot = FakeBot()
    await dp.feed_update(bot, _group_msg(bot, 1, TECH_ID, texts.BTN_NEW_TICKET))
    assert bot.silent()


async def test_inline_buttons_still_work_in_the_group(env):
    dp, client = env
    bot = FakeBot()
    await dp.feed_update(bot, _group_cb(bot, 1, TECH_ID, f"ta:take:{TICKET}"))
    client.assign_ticket.assert_awaited_once_with(TICKET, 9)
    assert texts.TECH_TAKEN_TOAST in bot.toasts


async def test_solution_text_is_collected_in_dm_only(env):
    dp, client = env
    bot = FakeBot()
    await dp.feed_update(bot, _group_cb(bot, 1, TECH_ID, f"ta:close:{TICKET}"))
    # the prompt went to the tech's DM and the DM chat carries the state
    assert any(chat == TECH_ID for chat, _ in bot.sent)

    # typing the solution IN THE GROUP does nothing...
    await dp.feed_update(bot, _group_msg(bot, 2, TECH_ID, "перезагрузил сервер"))
    client.add_solution.assert_not_awaited()

    # ...while the same text in the DM completes the flow
    await dp.feed_update(bot, _dm_msg(bot, 3, TECH_ID, "перезагрузил сервер"))
    client.add_solution.assert_awaited_once_with(TICKET, "Техник:\nперезагрузил сервер")


async def test_group_state_never_seeded_for_dialogs(env):
    # even after group interactions, only the DM chat may hold FSM state
    dp, client = env
    bot = FakeBot()
    await dp.feed_update(bot, _group_cb(bot, 1, TECH_ID, f"ta:close:{TICKET}"))
    from aiogram.fsm.storage.base import StorageKey

    group_state = await dp.storage.get_state(
        StorageKey(bot_id=BOT_ID, chat_id=GROUP, user_id=TECH_ID)
    )
    dm_state = await dp.storage.get_state(
        StorageKey(bot_id=BOT_ID, chat_id=TECH_ID, user_id=TECH_ID)
    )
    assert group_state is None
    assert dm_state == TechAction.closing.state
