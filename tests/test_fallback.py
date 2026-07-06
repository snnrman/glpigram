"""Fallback layer: stale callbacks get a toast, handler crashes get a reply."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from aiogram import Dispatcher, Router
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TgUser

from bot import texts
from bot.handlers.fallback import build_fallback_router, register_error_handler
from bot.handlers.new_ticket import build_new_ticket_router

BOT_ID = 42
CHAT = 1001
_DATE = datetime(2020, 1, 1, tzinfo=UTC)


class FakeBot:
    id = BOT_ID

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []  # (method class name, fields)

    async def __call__(self, method, **kwargs):
        self.calls.append((type(method).__name__, method.model_dump(exclude_none=True)))
        return MagicMock()


def _msg(bot, uid, text="hi"):
    return Message(
        message_id=uid,
        date=_DATE,
        chat=Chat(id=CHAT, type="private"),
        from_user=TgUser(id=CHAT, is_bot=False, first_name="U"),
        text=text,
    ).as_(bot)


def _cb_update(bot, uid, data):
    cb = CallbackQuery(
        id=str(uid),
        from_user=TgUser(id=CHAT, is_bot=False, first_name="U"),
        chat_instance="ci",
        message=_msg(bot, uid),
        data=data,
    ).as_(bot)
    return Update(update_id=uid, callback_query=cb)


def _dp_with_fallback(*routers) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    for r in routers:
        dp.include_router(r)
    dp.include_router(build_fallback_router())
    register_error_handler(dp)
    return dp


async def test_unknown_callback_gets_stale_toast():
    dp = _dp_with_fallback()
    bot = FakeBot()
    await dp.feed_update(bot, _cb_update(bot, 1, "nonsense:123"))
    answers = [f for name, f in bot.calls if name == "AnswerCallbackQuery"]
    assert answers and answers[0]["text"] == texts.STALE_BUTTON


async def test_wrong_state_button_falls_through_to_stale_toast():
    # A /new urgency button pressed with no FSM state (e.g. after a restart):
    # the state filter rejects it in new_ticket, the fallback must answer.
    dp = _dp_with_fallback(build_new_ticket_router(MagicMock(), MagicMock(), MagicMock()))
    bot = FakeBot()
    await dp.feed_update(bot, _cb_update(bot, 1, "nt:urg:3"))
    answers = [f for name, f in bot.calls if name == "AnswerCallbackQuery"]
    assert answers and answers[0]["text"] == texts.STALE_BUTTON


async def test_handler_exception_replies_generic_error():
    crashing = Router(name="crashing")

    @crashing.message()
    async def boom(message: Message) -> None:
        raise RuntimeError("db exploded")

    dp = _dp_with_fallback(crashing)
    bot = FakeBot()
    await dp.feed_update(bot, Update(update_id=1, message=_msg(bot, 1)))
    sends = [f for name, f in bot.calls if name == "SendMessage"]
    assert sends and sends[0]["text"] == texts.GENERIC_ERROR


async def test_callback_handler_exception_answers_alert():
    crashing = Router(name="crashing")

    @crashing.callback_query()
    async def boom(cb: CallbackQuery) -> None:
        raise RuntimeError("db exploded")

    dp = _dp_with_fallback(crashing)
    bot = FakeBot()
    await dp.feed_update(bot, _cb_update(bot, 1, "x:1"))
    answers = [f for name, f in bot.calls if name == "AnswerCallbackQuery"]
    assert answers and answers[0]["text"] == texts.GENERIC_ERROR
