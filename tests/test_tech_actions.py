"""Light coverage for the tech-actions module (handlers tested minimally).

The substantive GLPI logic lives in the client (see test_client.py); here we
just pin the button wiring and the card text escaping.
"""

from __future__ import annotations

import io
import re
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Update
from aiogram.types import User as TgUser

from bot import texts
from bot.db.repo import LinkedUser, Repo
from bot.glpi.client import TICKET_STATUS_NEW, TICKET_STATUS_PROCESSING_ASSIGNED
from bot.handlers.tech_actions import (
    TechAction,
    _card_keyboard_after_take,
    build_tech_actions_router,
)
from bot.services import notify
from bot.services.cards import CardService


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


# --- Take from the unassigned reminder gives the same feedback as the card ----
GROUP = -100
CARD_MSG = 999
REMINDER_MSG = 555


class GroupBot:
    id = BOT_ID

    def __init__(self) -> None:
        self.sent: list[tuple[object, str]] = []  # send_message (chat, text)
        self.card_edits: list[tuple[object, object, str]] = []  # edit_message_text
        self.msg_edits: list[tuple[str, object]] = []  # message.edit_text -> EditMessageText
        self.toasts: list[str] = []

    async def __call__(self, method, **kwargs):
        name = type(method).__name__
        if name == "EditMessageText":
            self.msg_edits.append(
                (getattr(method, "text", None), getattr(method, "reply_markup", None))
            )
        elif name == "AnswerCallbackQuery":
            self.toasts.append(getattr(method, "text", None))
        return MagicMock()

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))
        return MagicMock()

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs):
        self.card_edits.append((chat_id, message_id, text))
        return MagicMock()


def _reminder_message(bot) -> Message:
    header_plain = re.sub(r"<[^>]+>", "", texts.UNASSIGNED_HEADER)
    text = header_plain + "\n" + texts.unassigned_line(TICKET, "Принтер", 3)
    return Message(
        message_id=REMINDER_MSG,
        date=_DATE,
        chat=Chat(id=GROUP, type="supergroup"),
        from_user=TgUser(id=BOT_ID, is_bot=True, first_name="bot"),
        text=text,
        reply_markup=notify.unassigned_take_keyboard([TICKET]),
    ).as_(bot)


def _reminder_take_cb(bot) -> Update:
    cb = CallbackQuery(
        id="1",
        from_user=TgUser(id=TECH_TG, is_bot=False, first_name="T"),
        chat_instance="ci",
        message=_reminder_message(bot),
        data=f"ta:take:{TICKET}",
    ).as_(bot)
    return Update(update_id=1, callback_query=cb)


async def test_take_from_reminder_matches_card_take(repo):
    # A tracked ticket that has a living card in the group (as after creation).
    await repo.track_ticket(
        ticket_id=TICKET,
        requester_tg_id=REQUESTER_TG,
        requester_glpi_id=8,
        status=TICKET_STATUS_NEW,
        now=0,
    )
    await repo.save_card(
        ticket_id=TICKET,
        chat_id=GROUP,
        message_id=CARD_MSG,
        title="Принтер",
        description="не печатает",
        urgency=3,
        requester_name="Заявитель",
        requester_tg_id=REQUESTER_TG,
        attachments_count=0,
        status=TICKET_STATUS_NEW,
        now=0,
    )
    client = AsyncMock()
    cards = CardService(repo, front_base="https://glpi.local", now_provider=lambda: _DATE)
    router = build_tech_actions_router(
        client,
        tech_group_chat_id=GROUP,
        cards=cards,
        repo=repo,
        ticket_front_base="https://glpi.local",
    )
    router.callback_query.middleware(_inject_tech)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    bot = GroupBot()

    await dp.feed_update(bot, _reminder_take_cb(bot))

    # assigned in GLPI
    client.assign_ticket.assert_awaited_once_with(TICKET, 1)
    # (1) reminder message edited: taker shown, its Take button removed
    assert bot.msg_edits, "reminder was not edited"
    edited_text, edited_kb = bot.msg_edits[-1]
    assert "Техник" in edited_text and "🙋" in edited_text
    assert edited_kb is None  # the only take button was dropped
    # (2) living card edited (by its stored id) with the take in its history
    assert any(
        mid == CARD_MSG and "Взял в работу: Техник" in text for _, mid, text in bot.card_edits
    )
    # (3) requester notified that work started, naming the technician
    assert any(chat == REQUESTER_TG and "Техник" in text for chat, text in bot.sent)
    assert any(chat == REQUESTER_TG and "начата работа" in text for chat, text in bot.sent)
    # (4) tracked status advanced so the sync loop won't re-notify / re-remind
    tracked = await repo.get_tracked_ticket(TICKET)
    assert tracked.last_status == TICKET_STATUS_PROCESSING_ASSIGNED
