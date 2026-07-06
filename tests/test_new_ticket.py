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
from aiogram.types import Chat, Message, PhotoSize, Update
from aiogram.types import User as TgUser

from bot.handlers.new_ticket import NewTicket, build_new_ticket_router

pytestmark = pytest.mark.asyncio

BOT_ID = 42
CHAT = 1001
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


def _photo_update(bot: FakeBot, uid: int) -> Update:
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


async def test_two_attachments_then_done_word_completes():
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
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
