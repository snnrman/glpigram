"""Light coverage for the tech-actions module (handlers tested minimally).

The substantive GLPI logic lives in the client (see test_client.py); here we
just pin the button wiring and the card text escaping.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, PhotoSize, Update
from aiogram.types import User as TgUser

from bot import texts
from bot.db.repo import LinkedUser, Repo
from bot.handlers.tech_actions import (
    TechAction,
    _card_keyboard_after_take,
    build_tech_actions_router,
)


def test_router_builds():
    router = build_tech_actions_router(MagicMock())
    assert router.name == "tech_actions"


def test_after_take_keyboard_drops_take_keeps_comment_and_close():
    kb = _card_keyboard_after_take(5)
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert data == ["ta:comment:5", "ta:close:5"]
    assert all(not d.startswith("ta:take:") for d in data)


def test_card_text_escapes_name():
    # A crafted display name must not inject HTML into the card.
    out = texts.tech_card_taken("A <b>&</b> B")
    assert "<b>" not in out
    assert "&lt;b&gt;" in out and "&amp;" in out


# --- tech's bot-comment attachment reaches the requester (both directions) ----
BOT_ID = 42
TECH_CHAT = -100
TECH_TG = 555
REQUESTER_TG = 777
TICKET = 7
_DATE = datetime(2020, 1, 1, tzinfo=UTC)
_TECH = LinkedUser(
    tg_id=TECH_TG, glpi_users_id=1, display_name="Техник", is_tech=True, linked_at=0, checked_at=0
)


class MediaBot:
    id = BOT_ID

    def __init__(self) -> None:
        self.sent: list[tuple[object, object]] = []
        self.photos: list[tuple[object, str]] = []

    async def __call__(self, method, **kwargs):
        self.sent.append((getattr(method, "chat_id", None), getattr(method, "text", None)))
        return MagicMock()

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))
        return MagicMock()

    async def download(self, file_id, **kwargs):
        return io.BytesIO(b"BYTES")

    async def send_photo(self, chat_id, photo, **kwargs):
        self.photos.append((chat_id, photo.filename))


async def _inject_tech(handler, event, data):
    data["link"] = _TECH
    return await handler(event, data)


def _photo_update(bot, uid: int) -> Update:
    photo = PhotoSize(file_id="F1", file_unique_id="U1", width=90, height=90, file_size=1000)
    msg = Message(
        message_id=uid,
        date=_DATE,
        chat=Chat(id=TECH_TG, type="private"),
        from_user=TgUser(id=TECH_TG, is_bot=False, first_name="T"),
        photo=[photo],
    ).as_(bot)
    return Update(update_id=uid, message=msg)


@pytest.fixture
async def repo(tmp_path):
    r = Repo(str(tmp_path / "ta.sqlite3"))
    await r.connect()
    yield r
    await r.close()


async def test_tech_comment_photo_reaches_requester(repo):
    await repo.track_ticket(
        ticket_id=TICKET, requester_tg_id=REQUESTER_TG, requester_glpi_id=8, status=2, now=0
    )
    client = AsyncMock()
    client.attach_document_to_ticket.return_value = 1
    client.add_followup.return_value = 900
    router = build_tech_actions_router(client, tech_group_chat_id=TECH_CHAT, repo=repo)
    router.message.middleware(_inject_tech)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    ctx = FSMContext(
        storage=dp.storage, key=StorageKey(bot_id=BOT_ID, chat_id=TECH_TG, user_id=TECH_TG)
    )
    await ctx.set_state(TechAction.commenting)
    await ctx.set_data({"ticket_id": TICKET})
    bot = MediaBot()

    await dp.feed_update(bot, _photo_update(bot, 1))

    client.attach_document_to_ticket.assert_awaited_once()
    # forwarded to the requester's DM as a photo
    assert bot.photos and bot.photos[-1][0] == REQUESTER_TG
