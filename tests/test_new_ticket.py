"""/new attachments step (feature 6) — dispatch-level regression test.

Reproduces the bug where per-file confirmations had no inline keyboard, and
verifies the "готово" text fallback finishes the step.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from aiogram import Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Update
from aiogram.types import User as TgUser

from bot import texts
from bot.db.repo import LinkedUser
from bot.handlers.new_ticket import NewTicket, build_new_ticket_router

pytestmark = pytest.mark.asyncio

BOT_ID = 42
CHAT = 1001


def _fake_link(is_tech: bool = False) -> LinkedUser:
    """Bare test dispatchers have no auth middleware -> inject `link` as workflow data."""
    return LinkedUser(
        tg_id=CHAT, glpi_users_id=7, display_name="U", is_tech=is_tech, linked_at=0, checked_at=0
    )


_DATE = datetime(2020, 1, 1, tzinfo=UTC)


class FakeBot:
    """Minimal stand-in: aiogram calls ``await bot(SendMessage(...))`` to send."""

    id = BOT_ID

    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []

    async def __call__(self, method, **kwargs):
        self.sent.append((getattr(method, "text", None), getattr(method, "reply_markup", None)))
        return MagicMock()


def _bind(message: Message, bot: FakeBot) -> Message:
    return message.as_(bot)


def _photo_update(bot: FakeBot, uid: int, media_group_id: str | None = None) -> Update:
    photo = PhotoSize(
        file_id=f"F{uid}", file_unique_id=f"U{uid}", width=90, height=90, file_size=1000
    )
    msg = _bind(
        Message(
            message_id=uid,
            date=_DATE,
            chat=Chat(id=CHAT, type="private"),
            from_user=TgUser(id=CHAT, is_bot=False, first_name="U"),
            photo=[photo],
            media_group_id=media_group_id,
        ),
        bot,
    )
    return Update(update_id=uid, message=msg)


def _text_update(bot: FakeBot, uid: int, text: str) -> Update:
    msg = _bind(
        Message(
            message_id=uid,
            date=_DATE,
            chat=Chat(id=CHAT, type="private"),
            from_user=TgUser(id=CHAT, is_bot=False, first_name="U"),
            text=text,
        ),
        bot,
    )
    return Update(update_id=uid, message=msg)


def _last_text(bot: FakeBot) -> str | None:
    """Last message carrying text (skips cb.answer(), which has text=None)."""
    return next((t for t, _ in reversed(bot.sent) if t is not None), None)


def _cb_update(bot: FakeBot, uid: int, data: str) -> Update:
    msg = _bind(
        Message(
            message_id=uid,
            date=_DATE,
            chat=Chat(id=CHAT, type="private"),
            from_user=TgUser(id=CHAT, is_bot=False, first_name="U"),
            text="prompt",
        ),
        bot,
    )
    cb = CallbackQuery(
        id=str(uid),
        from_user=TgUser(id=CHAT, is_bot=False, first_name="U"),
        chat_instance="ci",
        message=msg,
        data=data,
    ).as_(bot)
    return Update(update_id=uid, callback_query=cb)


async def _attaching_dispatcher(storage: MemoryStorage, ctx: FSMContext) -> Dispatcher:
    dp = Dispatcher(storage=storage)
    dp["link"] = _fake_link()
    dp.include_router(build_new_ticket_router(MagicMock(), MagicMock(), MagicMock()))
    await ctx.set_state(NewTicket.attaching)
    await ctx.set_data(
        {
            "category_id": 1,
            "category_name": "C",
            "urgency": 3,
            "title": "T",
            "description": "D",
            "attachments": [{"file_id": "F", "filename": "f.jpg", "mime": "image/jpeg"}],
        }
    )
    return dp


async def test_two_attachments_then_done_word_completes():
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp["link"] = _fake_link()
    # client/category_cache/repo aren't touched during the attaching step.
    dp.include_router(build_new_ticket_router(MagicMock(), MagicMock(), MagicMock()))
    bot = FakeBot()

    ctx = FSMContext(storage=storage, key=StorageKey(bot_id=BOT_ID, chat_id=CHAT, user_id=CHAT))
    await ctx.set_state(NewTicket.attaching)
    await ctx.set_data(
        {
            "category_id": 1,
            "category_name": "C",
            "urgency": 3,
            "title": "T",
            "description": "D",
            "attachments": [],
        }
    )

    await dp.feed_update(bot, _photo_update(bot, 1))
    await dp.feed_update(bot, _photo_update(bot, 2))

    # Both files were recorded...
    data = await ctx.get_data()
    assert len(data["attachments"]) == 2
    # ...and EVERY per-file confirmation carried an inline keyboard (the bug).
    assert bot.sent, "no messages were sent"
    assert all(reply_markup is not None for _, reply_markup in bot.sent)

    # "готово" (any case) finishes the step even without pressing the button.
    await dp.feed_update(bot, _text_update(bot, 3, "Готово"))
    assert await ctx.get_state() == NewTicket.confirming
    last_text, last_kb = bot.sent[-1]
    assert "Вложений:</b> 2" in last_text  # confirm summary counts both files
    assert last_kb is not None  # confirm keyboard present


async def test_attach_cancel_asks_confirmation_and_back_keeps_state():
    storage = MemoryStorage()
    ctx = FSMContext(storage=storage, key=StorageKey(bot_id=BOT_ID, chat_id=CHAT, user_id=CHAT))
    dp = await _attaching_dispatcher(storage, ctx)
    bot = FakeBot()

    # Отмена -> a confirmation question, ticket NOT cancelled yet.
    await dp.feed_update(bot, _cb_update(bot, 10, "nt:attach_cancel"))
    assert _last_text(bot) == texts.ATTACH_CANCEL_CONFIRM
    assert await ctx.get_state() == NewTicket.attaching
    assert len((await ctx.get_data())["attachments"]) == 1  # files preserved

    # "Нет" -> back to the attach prompt, still attaching.
    await dp.feed_update(bot, _cb_update(bot, 11, "nt:attach_back"))
    assert _last_text(bot) == texts.NEW_ATTACH_PROMPT
    assert await ctx.get_state() == NewTicket.attaching


async def test_attach_cancel_confirmed_aborts_ticket():
    storage = MemoryStorage()
    ctx = FSMContext(storage=storage, key=StorageKey(bot_id=BOT_ID, chat_id=CHAT, user_id=CHAT))
    dp = await _attaching_dispatcher(storage, ctx)
    bot = FakeBot()

    await dp.feed_update(bot, _cb_update(bot, 12, "nt:attach_cancel"))
    # "Да, отменить" -> ticket creation aborted, state cleared.
    await dp.feed_update(bot, _cb_update(bot, 13, "nt:cancel"))
    assert _last_text(bot) == texts.NEW_CANCELLED
    assert await ctx.get_state() is None


# --- FSM robustness: cancel at every step, garbage input ---------------------
_STEP_DATA = {
    "category_id": 1,
    "category_name": "C",
    "urgency": 3,
    "title": "T",
    "description": "D",
    "attachments": [],
}


async def _dispatcher_at(state, data=None):
    """Dispatcher + FSM context pre-seeded into an arbitrary /new step."""
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp["link"] = _fake_link()
    dp.include_router(build_new_ticket_router(MagicMock(), MagicMock(), MagicMock()))
    ctx = FSMContext(storage=storage, key=StorageKey(bot_id=BOT_ID, chat_id=CHAT, user_id=CHAT))
    await ctx.set_state(state)
    await ctx.set_data(dict(_STEP_DATA if data is None else data))
    return dp, ctx


@pytest.mark.parametrize(
    "step",
    [
        NewTicket.choosing_category,
        NewTicket.choosing_urgency,
        NewTicket.entering_title,
        NewTicket.entering_description,
        NewTicket.confirming,
    ],
)
async def test_cancel_button_works_at_every_step(step):
    dp, ctx = await _dispatcher_at(step)
    bot = FakeBot()
    await dp.feed_update(bot, _cb_update(bot, 1, "nt:cancel"))
    assert _last_text(bot) == texts.NEW_CANCELLED
    assert await ctx.get_state() is None


@pytest.mark.parametrize(
    "step",
    [
        NewTicket.choosing_category,
        NewTicket.entering_title,
        NewTicket.entering_description,
        NewTicket.attaching,
        NewTicket.confirming,
    ],
)
async def test_cancel_command_works_at_every_step(step):
    dp, ctx = await _dispatcher_at(step)
    bot = FakeBot()
    await dp.feed_update(bot, _text_update(bot, 1, "/cancel"))
    assert _last_text(bot) == texts.NEW_CANCELLED
    assert await ctx.get_state() is None


async def test_photo_instead_of_title_is_rejected_state_kept():
    dp, ctx = await _dispatcher_at(NewTicket.entering_title)
    bot = FakeBot()
    await dp.feed_update(bot, _photo_update(bot, 1))
    assert _last_text(bot) == texts.NEW_EXPECT_TEXT
    assert await ctx.get_state() == NewTicket.entering_title  # still waiting


async def test_photo_instead_of_description_is_rejected_state_kept():
    dp, ctx = await _dispatcher_at(NewTicket.entering_description)
    bot = FakeBot()
    await dp.feed_update(bot, _photo_update(bot, 1))
    assert _last_text(bot) == texts.NEW_EXPECT_TEXT
    assert await ctx.get_state() == NewTicket.entering_description


async def test_too_long_title_rejected_state_kept():
    dp, ctx = await _dispatcher_at(NewTicket.entering_title)
    bot = FakeBot()
    await dp.feed_update(bot, _text_update(bot, 1, "x" * 251))
    assert _last_text(bot) == texts.NEW_TITLE_TOO_LONG
    assert await ctx.get_state() == NewTicket.entering_title


async def test_random_text_while_attaching_prompts_with_keyboard():
    dp, ctx = await _dispatcher_at(NewTicket.attaching)
    bot = FakeBot()
    await dp.feed_update(bot, _text_update(bot, 1, "просто текст"))
    assert _last_text(bot) == texts.ATTACH_UNSUPPORTED
    # the reply must carry the Готово/Отмена keyboard (regression of the old bug)
    assert bot.sent[-1][1] is not None
    assert await ctx.get_state() == NewTicket.attaching


@pytest.mark.parametrize(
    "step",
    [NewTicket.choosing_category, NewTicket.choosing_urgency, NewTicket.confirming],
)
async def test_text_on_button_steps_prompts_use_buttons(step):
    dp, ctx = await _dispatcher_at(step)
    bot = FakeBot()
    await dp.feed_update(bot, _text_update(bot, 1, "какой-то текст"))
    assert _last_text(bot) == texts.USE_BUTTONS
    assert await ctx.get_state() == step  # dialog not disturbed


async def test_album_stores_all_photos_but_confirms_once():
    # An album arrives as N messages sharing media_group_id: no reply spam.
    dp, ctx = await _dispatcher_at(NewTicket.attaching)
    bot = FakeBot()
    await dp.feed_update(bot, _photo_update(bot, 1, media_group_id="alb1"))
    await dp.feed_update(bot, _photo_update(bot, 2, media_group_id="alb1"))
    await dp.feed_update(bot, _photo_update(bot, 3, media_group_id="alb1"))

    assert len((await ctx.get_data())["attachments"]) == 3  # all stored
    confirmations = [t for t, _ in bot.sent if t and "Вложение добавлено" in t]
    assert len(confirmations) == 1  # confirmed once per album

    # a separate single photo afterwards is confirmed normally
    await dp.feed_update(bot, _photo_update(bot, 4))
    confirmations = [t for t, _ in bot.sent if t and "Вложение добавлено" in t]
    assert len(confirmations) == 2
