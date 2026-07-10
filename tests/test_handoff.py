"""«🔄 Передать» — reassigning a ticket to another technician.

Dispatch tests run on the full production dispatcher; the pick dialog must
live in the pressing tech's DM, GLPI gets the assignee swap, and all three
notifications (new tech, requester, card history/header) go out.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
import respx
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TgUser

from bot import texts
from bot.db.repo import Repo
from bot.glpi.models import Ticket

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("GLPI_API_URL", "http://127.0.0.1/apirest.php")
os.environ.setdefault("GLPI_USER_TOKEN", "u")

from bot.config import Settings  # noqa: E402
from bot.glpi.client import GlpiClient  # noqa: E402
from bot.main import build_dispatcher  # noqa: E402

BOT_ID = 42
TECH_ID = 2002  # "Техник", glpi 9 — current assignee, presses the button
TECH2_ID = 4004  # "Новый", glpi 11 — handoff target
REQUESTER_ID = 1001  # glpi 8
GROUP = -100
TICKET = 5
CARD_MSG = 777
_DATE = datetime(2020, 1, 1, tzinfo=UTC)


class FakeBot:
    id = BOT_ID

    def __init__(self) -> None:
        self.sent: list[tuple[object, str, object]] = []  # (chat, text, markup)
        self.edits: list[tuple[object, object, str]] = []  # (chat, msg_id, text)
        self.toasts: list[str] = []

    async def __call__(self, method, **kwargs):
        name = type(method).__name__
        if name == "SendMessage":
            self.sent.append((method.chat_id, method.text, method.reply_markup))
        elif name == "EditMessageText":
            self.edits.append((method.chat_id, method.message_id, method.text))
        elif name == "AnswerCallbackQuery" and method.text:
            self.toasts.append(method.text)
        return MagicMock()

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent.append((chat_id, text, reply_markup))
        return MagicMock()

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs):
        self.edits.append((chat_id, message_id, text))


def _client() -> AsyncMock:
    client = AsyncMock()
    client.get_ticket_assignees.return_value = ["Техник"]
    client.get_ticket.return_value = Ticket(
        id=TICKET, name="Принтер", content="c", status=2, urgency=4
    )
    return client


@pytest.fixture
async def env(tmp_path):
    repo = Repo(str(tmp_path / "handoff.sqlite3"))
    await repo.connect()
    now = int(time.time())
    await repo.upsert_link(
        tg_id=TECH_ID, glpi_users_id=9, display_name="Техник", is_tech=True, now=now
    )
    await repo.upsert_link(
        tg_id=TECH2_ID, glpi_users_id=11, display_name="Новый", is_tech=True, now=now
    )
    await repo.upsert_link(
        tg_id=REQUESTER_ID, glpi_users_id=8, display_name="Заявитель", is_tech=False, now=now
    )
    await repo.track_ticket(
        ticket_id=TICKET, requester_tg_id=REQUESTER_ID, requester_glpi_id=8, status=2, now=now
    )
    await repo.save_card(
        ticket_id=TICKET,
        chat_id=GROUP,
        message_id=CARD_MSG,
        title="Принтер",
        urgency=4,
        requester_name="Заявитель",
        requester_tg_id=REQUESTER_ID,
        attachments_count=0,
        status=2,
        now=now,
    )
    client = _client()
    settings = Settings()
    settings.tech_group_chat_id = GROUP
    dp = build_dispatcher(client, repo, settings)
    yield dp, client, repo
    await repo.close()


def _cb(bot, uid, from_id, data, *, chat_id, chat_type):
    msg = Message(
        message_id=uid,
        date=_DATE,
        chat=Chat(id=chat_id, type=chat_type),
        from_user=TgUser(id=BOT_ID, is_bot=True, first_name="bot"),
        text="сообщение",
    ).as_(bot)
    cb = CallbackQuery(
        id=str(uid),
        from_user=TgUser(id=from_id, is_bot=False, first_name="U"),
        chat_instance="ci",
        message=msg,
        data=data,
    ).as_(bot)
    return Update(update_id=uid, callback_query=cb)


def _group_cb(bot, uid, from_id, data):
    return _cb(bot, uid, from_id, data, chat_id=GROUP, chat_type="supergroup")


def _dm_cb(bot, uid, from_id, data):
    return _cb(bot, uid, from_id, data, chat_id=from_id, chat_type="private")


async def test_handoff_button_opens_pick_list_in_dm(env):
    dp, client, _repo = env
    bot = FakeBot()
    await dp.feed_update(bot, _group_cb(bot, 1, TECH_ID, f"ta:handoff:{TICKET}"))
    chat, text, kb = bot.sent[-1]
    assert chat == TECH_ID  # the dialog is in the presser's DM, not the group
    assert text == texts.handoff_pick(TICKET)
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    # alphabetical by display_name («Новый» before «Техник»), cancel last
    assert data == [f"ta:hto:{TICKET}:11", f"ta:hto:{TICKET}:9", "ta:hx"]
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "Новый" in labels and "Техник" in labels


async def test_handoff_is_tech_only(env):
    dp, client, _repo = env
    bot = FakeBot()
    await dp.feed_update(bot, _group_cb(bot, 1, REQUESTER_ID, f"ta:handoff:{TICKET}"))
    assert texts.TECH_ONLY in bot.toasts
    assert bot.sent == []  # no DM dialog


async def test_pick_reassigns_and_notifies_everyone(env):
    dp, client, _repo = env
    bot = FakeBot()
    await dp.feed_update(bot, _dm_cb(bot, 2, TECH_ID, f"ta:hto:{TICKET}:11"))

    client.reassign_ticket.assert_awaited_once_with(TICKET, 11)
    # new executor's DM: ticket number, title, urgency
    new_tech_msgs = [t for c, t, _ in bot.sent if c == TECH2_ID]
    assert any("переназначена" in t and "Принтер" in t and "№5" in t for t in new_tech_msgs)
    assert any(texts.urgency_label(4) in t for t in new_tech_msgs)
    # requester's DM: who handles it now
    req_msgs = [t for c, t, _ in bot.sent if c == REQUESTER_ID]
    assert any("теперь ведёт Новый" in t for t in req_msgs)
    # the card was edited: history line + assignee in the header
    card_edits = [t for c, m, t in bot.edits if (c, m) == (GROUP, CARD_MSG)]
    assert any("🔄 Передано: Техник → Новый" in t for t in card_edits)
    assert any("Исполнитель: Новый" in t for t in card_edits)
    # no group ping for the handoff (history only)
    assert not any(c == GROUP for c, _t, _m in bot.sent)
    # the pick message collapsed into a confirmation
    assert any(t == texts.handoff_done(TICKET, "Новый") for _c, _m, t in bot.edits)


async def test_self_handoff_skips_the_self_notification(env):
    dp, client, _repo = env
    bot = FakeBot()
    await dp.feed_update(bot, _dm_cb(bot, 2, TECH_ID, f"ta:hto:{TICKET}:9"))
    client.reassign_ticket.assert_awaited_once_with(TICKET, 9)
    # the presser IS the target: no "reassigned to you" DM to themselves
    assert not any("переназначена" in t for _c, t, _m in bot.sent)
    # requester still learns the (new) executor
    assert any("теперь ведёт Техник" in t for c, t, _ in bot.sent if c == REQUESTER_ID)


async def test_cancel_button_collapses_the_pick_message(env):
    dp, client, _repo = env
    bot = FakeBot()
    await dp.feed_update(bot, _dm_cb(bot, 2, TECH_ID, "ta:hx"))
    assert any(t == texts.HANDOFF_CANCELLED for _c, _m, t in bot.edits)
    client.reassign_ticket.assert_not_called()


async def test_unlinked_target_is_refused(env):
    dp, client, _repo = env
    bot = FakeBot()
    await dp.feed_update(bot, _dm_cb(bot, 2, TECH_ID, f"ta:hto:{TICKET}:999"))
    assert texts.HANDOFF_TARGET_GONE in bot.toasts
    client.reassign_ticket.assert_not_called()


# -- the GLPI swap itself -------------------------------------------------------
BASE = "http://glpi.local/apirest.php"


async def test_reassign_ticket_swaps_the_assignee_link():
    client = GlpiClient(BASE, "", "u", timeout=1.0)
    try:
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{BASE}/initSession").respond(200, json={"session_token": "tok"})
            router.get(f"{BASE}/Ticket/5/Ticket_User").respond(
                200,
                json=[
                    {"id": 70, "users_id": 8, "type": 1},  # requester -> untouched
                    {"id": 77, "users_id": 9, "type": 2},  # old assignee -> dropped
                ],
            )
            delete = router.delete(f"{BASE}/Ticket_User/77").respond(200, json=True)
            post = router.post(f"{BASE}/Ticket_User").respond(201, json={"id": 78})
            put = router.put(f"{BASE}/Ticket/5").respond(200, json=True)
            await client.reassign_ticket(5, 11)
    finally:
        await client.close()
    assert delete.called
    assert post.called
    import json as _json

    body = _json.loads(post.calls[0].request.content)
    assert body["input"] == {"tickets_id": 5, "users_id": 11, "type": 2}
    assert _json.loads(put.calls[0].request.content)["input"]["status"] == 2


async def test_reassign_skips_post_when_target_already_assigned():
    client = GlpiClient(BASE, "", "u", timeout=1.0)
    try:
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{BASE}/initSession").respond(200, json={"session_token": "tok"})
            router.get(f"{BASE}/Ticket/5/Ticket_User").respond(
                200, json=[{"id": 77, "users_id": 11, "type": 2}]
            )
            post = router.post(f"{BASE}/Ticket_User").respond(201, json={"id": 78})
            router.put(f"{BASE}/Ticket/5").respond(200, json=True)
            await client.reassign_ticket(5, 11)
    finally:
        await client.close()
    assert not post.called  # already the assignee: no duplicate link
