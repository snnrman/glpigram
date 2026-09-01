"""Lead access approval (feature: access requests) — dispatch-level tests.

Full production dispatcher (router order + auth middleware). GLPI is an
AsyncMock; the lead directory resolves from it via find_user_by_login.
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
from bot.glpi.client import TICKET_STATUS_CLOSED
from bot.glpi.models import Ticket
from bot.glpi.models import User as GlpiUser

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("GLPI_API_URL", "http://127.0.0.1/apirest.php")
os.environ.setdefault("GLPI_USER_TOKEN", "u")

from bot.config import Settings  # noqa: E402
from bot.main import build_dispatcher  # noqa: E402

BOT_ID = 42
REQUESTER_ID = 1001
LEAD_ID = 3003
LEAD_GLPI = 19
TICKET = 77
_DATE = datetime(2020, 1, 1, tzinfo=UTC)


class FakeBot:
    id = BOT_ID

    def __init__(self) -> None:
        self.sent: list[tuple[object, str, object]] = []  # (chat, text, markup)
        self.toasts: list[str] = []

    async def __call__(self, method, **kwargs):
        name = type(method).__name__
        if name in ("SendMessage", "EditMessageText"):
            self.sent.append((method.chat_id, method.text, method.reply_markup))
        elif name == "AnswerCallbackQuery" and method.text:
            self.toasts.append(method.text)
        return MagicMock()

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent.append((chat_id, text, reply_markup))
        from types import SimpleNamespace

        return SimpleNamespace(message_id=len(self.sent))

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs):
        self.sent.append((chat_id, text, kwargs.get("reply_markup")))


def _client() -> AsyncMock:
    client = AsyncMock()
    client.create_ticket.return_value = TICKET
    client.create_validation.return_value = 5
    client.add_followup.return_value = 900
    client.find_user_by_login.return_value = GlpiUser(
        id=LEAD_GLPI, name="uliana.metlina", realname="Метлина", firstname="Ульяна"
    )
    return client


@pytest.fixture
async def env(tmp_path):
    repo = Repo(str(tmp_path / "access.sqlite3"))
    await repo.connect()
    now = int(time.time())
    await repo.upsert_link(
        tg_id=REQUESTER_ID, glpi_users_id=8, display_name="Пётр Заявитель", is_tech=False, now=now
    )
    await repo.upsert_link(
        tg_id=LEAD_ID,
        glpi_users_id=LEAD_GLPI,
        display_name="Ульяна Метлина",
        is_tech=False,
        now=now,
    )
    client = _client()
    settings = Settings()
    settings.lead_logins = "uliana.metlina"
    settings.access_category_id = 10
    dp = build_dispatcher(client, repo, settings)
    yield dp, client, repo
    await repo.close()


def _dm(bot, uid, from_id, text):
    msg = Message(
        message_id=uid,
        date=_DATE,
        chat=Chat(id=from_id, type="private"),
        from_user=TgUser(id=from_id, is_bot=False, first_name="U"),
        text=text,
    ).as_(bot)
    return Update(update_id=uid, message=msg)


def _cb(bot, uid, from_id, data):
    msg = Message(
        message_id=uid,
        date=_DATE,
        chat=Chat(id=from_id, type="private"),
        from_user=TgUser(id=BOT_ID, is_bot=True, first_name="bot"),
        text="prompt",
    ).as_(bot)
    cb = CallbackQuery(
        id=str(uid),
        from_user=TgUser(id=from_id, is_bot=False, first_name="U"),
        chat_instance="ci",
        message=msg,
        data=data,
    ).as_(bot)
    return Update(update_id=uid, callback_query=cb)


def _to(bot: FakeBot, chat) -> list[str]:
    return [t for c, t, _ in bot.sent if c == chat and t]


async def _run_dialog_to_sent(dp, bot):
    """Full requester dialog (short flow): one message -> pick lead -> send."""
    await dp.feed_update(bot, _dm(bot, 1, REQUESTER_ID, texts.BTN_ACCESS))
    await dp.feed_update(bot, _dm(bot, 2, REQUESTER_ID, "Jenkins\nнужны права админа"))
    await dp.feed_update(bot, _cb(bot, 3, REQUESTER_ID, f"ac:lead:{LEAD_GLPI}"))
    await dp.feed_update(bot, _cb(bot, 4, REQUESTER_ID, "ac:send"))


# --- requester dialog ---------------------------------------------------------
async def test_full_dialog_creates_ticket_validation_and_dms_lead(env):
    dp, client, repo = env
    bot = FakeBot()
    await _run_dialog_to_sent(dp, bot)

    # ticket carries the structured body and the access category
    client.create_ticket.assert_awaited_once()
    kw = client.create_ticket.await_args.kwargs
    assert kw["name"] == "Доступ: Jenkins"  # title = first line of the request
    assert "нужны права админа" in kw["content"] and "Ульяна Метлина" in kw["content"]
    assert kw["itilcategories_id"] == 10
    assert kw["requester_users_id"] == 8
    # validation attached (the take gate)
    client.create_validation.assert_awaited_once()
    assert client.create_validation.await_args.args[0] == TICKET
    # approval row persisted
    row = await repo.get_approval(TICKET)
    assert row["lead_tg_id"] == LEAD_ID and row["status"] == 0
    # lead got the DM with both buttons
    lead_msgs = _to(bot, LEAD_ID)
    assert lead_msgs and "Jenkins" in lead_msgs[0] and "Пётр Заявитель" in lead_msgs[0]
    kb = next(m for c, t, m in bot.sent if c == LEAD_ID and m)
    data = [b.callback_data for row_ in kb.inline_keyboard for b in row_]
    assert data == [f"ap:ok:{TICKET}", f"ap:no:{TICKET}"]
    # requester told it went for approval
    assert any("отправлен на согласование" in t for t in _to(bot, REQUESTER_ID))


async def test_lead_pick_excludes_self(env):
    dp, client, repo = env
    bot = FakeBot()
    # The LEAD starts an access request — they can't approve themselves.
    await dp.feed_update(bot, _dm(bot, 1, LEAD_ID, texts.BTN_ACCESS))
    await dp.feed_update(bot, _dm(bot, 2, LEAD_ID, "Jenkins"))
    # the only configured lead IS the requester -> empty pick list
    assert any(texts.ACC_NO_LEADS == t for t in _to(bot, LEAD_ID))


# --- the lead answers -----------------------------------------------------------
async def test_lead_approves_one_tap(env):
    dp, client, repo = env
    bot = FakeBot()
    await _run_dialog_to_sent(dp, bot)
    bot.sent.clear()

    await dp.feed_update(bot, _cb(bot, 10, LEAD_ID, f"ap:ok:{TICKET}"))

    # GLPI: validation answered with the lead named in the comment
    client.answer_validation.assert_awaited_once()
    kwargs = client.answer_validation.await_args.kwargs
    assert kwargs["accepted"] is True and "Ульяна Метлина" in kwargs["comment"]
    # followup into the timeline, cursor bumped (no sync echo)
    client.add_followup.assert_awaited_once()
    assert (await repo.get_tracked_ticket(TICKET)).last_followup_id == 900
    # bookkeeping + requester notified
    assert (await repo.get_approval(TICKET))["status"] == 1
    assert any("одобрен" in t for t in _to(bot, REQUESTER_ID))


async def test_lead_rejects_with_mandatory_reason(env):
    dp, client, repo = env
    bot = FakeBot()
    await _run_dialog_to_sent(dp, bot)
    bot.sent.clear()

    await dp.feed_update(bot, _cb(bot, 10, LEAD_ID, f"ap:no:{TICKET}"))
    assert any(texts.ACC_ASK_REJECT_REASON == t for t in _to(bot, LEAD_ID))
    await dp.feed_update(bot, _dm(bot, 11, LEAD_ID, "нет обоснования от руководителя проекта"))

    kwargs = client.answer_validation.await_args.kwargs
    assert kwargs["accepted"] is False and "нет обоснования" in kwargs["comment"]
    client.set_ticket_status.assert_awaited_once_with(TICKET, TICKET_STATUS_CLOSED)
    assert (await repo.get_approval(TICKET))["status"] == -1
    tracked = await repo.get_tracked_ticket(TICKET)
    assert tracked.last_status == TICKET_STATUS_CLOSED and not tracked.active
    # requester sees the reason
    assert any("нет обоснования" in t for t in _to(bot, REQUESTER_ID))


async def test_stranger_cannot_answer(env):
    dp, client, repo = env
    bot = FakeBot()
    await _run_dialog_to_sent(dp, bot)
    bot.sent.clear()

    await dp.feed_update(bot, _cb(bot, 10, REQUESTER_ID, f"ap:ok:{TICKET}"))  # not the lead
    client.answer_validation.assert_not_awaited()
    assert (await repo.get_approval(TICKET))["status"] == 0


async def test_second_answer_is_stale(env):
    dp, client, repo = env
    bot = FakeBot()
    await _run_dialog_to_sent(dp, bot)
    await dp.feed_update(bot, _cb(bot, 10, LEAD_ID, f"ap:ok:{TICKET}"))
    client.answer_validation.reset_mock()

    await dp.feed_update(bot, _cb(bot, 11, LEAD_ID, f"ap:no:{TICKET}"))  # already answered
    client.answer_validation.assert_not_awaited()
    assert (await repo.get_approval(TICKET))["status"] == 1  # first decision stands


# --- the take gate ---------------------------------------------------------------
async def test_take_blocked_while_awaiting_approval(env):
    dp, client, repo = env
    now = int(time.time())
    await repo.upsert_link(
        tg_id=5005, glpi_users_id=9, display_name="Техник", is_tech=True, now=now
    )
    client.get_ticket.return_value = Ticket(
        id=TICKET, name="Доступ: Jenkins", content="c", status=1, urgency=3, global_validation=2
    )
    bot = FakeBot()
    await dp.feed_update(bot, _cb(bot, 1, 5005, f"ta:take:{TICKET}"))
    client.assign_ticket.assert_not_awaited()  # hard gate
    assert any("не согласована" in t for t in bot.toasts)

    # approved -> the same take goes through
    client.get_ticket.return_value = Ticket(
        id=TICKET, name="Доступ: Jenkins", content="c", status=1, urgency=3, global_validation=3
    )
    await dp.feed_update(bot, _cb(bot, 2, 5005, f"ta:take:{TICKET}"))
    client.assign_ticket.assert_awaited_once()


# --- lead auto-suggestion from the org map -------------------------------------
async def test_mapped_lead_lands_on_confirm_in_one_message(env):
    dp, client, repo = env
    await repo.set_user_lead(8, LEAD_GLPI)  # requester glpi=8 -> Метлина
    bot = FakeBot()
    await dp.feed_update(bot, _dm(bot, 1, REQUESTER_ID, texts.BTN_ACCESS))
    await dp.feed_update(bot, _dm(bot, 2, REQUESTER_ID, "Jenkins"))

    # straight to the combined confirm screen — no pick list, no extra steps
    msgs = _to(bot, REQUESTER_ID)
    assert any("Согласует:" in t and "Ульяна Метлина" in t for t in msgs)
    assert not any(texts.ACC_CHOOSE_LEAD == t for t in msgs)
    # one tap sends
    await dp.feed_update(bot, _cb(bot, 3, REQUESTER_ID, "ac:send"))
    client.create_ticket.assert_awaited_once()
    assert any("отправлен на согласование" in t for t in _to(bot, REQUESTER_ID))


async def test_confirm_pick_another_shows_full_list(env):
    dp, client, repo = env
    await repo.set_user_lead(8, LEAD_GLPI)
    bot = FakeBot()
    await dp.feed_update(bot, _dm(bot, 1, REQUESTER_ID, texts.BTN_ACCESS))
    await dp.feed_update(bot, _dm(bot, 2, REQUESTER_ID, "Jenkins"))
    await dp.feed_update(bot, _cb(bot, 3, REQUESTER_ID, "ac:other"))
    assert any(texts.ACC_CHOOSE_LEAD == t for t in _to(bot, REQUESTER_ID))


async def test_unmapped_user_gets_pick_list_as_before(env):
    dp, client, repo = env  # no user_leads row
    bot = FakeBot()
    await dp.feed_update(bot, _dm(bot, 1, REQUESTER_ID, texts.BTN_ACCESS))
    await dp.feed_update(bot, _dm(bot, 2, REQUESTER_ID, "Jenkins"))
    assert any(texts.ACC_CHOOSE_LEAD == t for t in _to(bot, REQUESTER_ID))


async def test_setlead_admin_command(env):
    dp, client, repo = env
    now = int(time.time())
    await repo.upsert_link(
        tg_id=5005, glpi_users_id=9, display_name="Техник", is_tech=True, now=now
    )

    async def _find(login, **kwargs):
        return {
            "petr.zayavitel": GlpiUser(id=8, name="petr.zayavitel", realname="Заявитель"),
            "uliana.metlina": GlpiUser(id=LEAD_GLPI, name="uliana.metlina", realname="Метлина"),
        }.get(login)

    client.find_user_by_login.side_effect = _find
    bot = FakeBot()
    await dp.feed_update(bot, _dm(bot, 1, 5005, "/setlead petr.zayavitel uliana.metlina"))
    assert await repo.get_user_lead(8) == LEAD_GLPI
    assert any("✅" in t for t in _to(bot, 5005))

    # non-tech refused
    await dp.feed_update(bot, _dm(bot, 2, REQUESTER_ID, "/setlead a b"))
    assert texts.TECH_ONLY in _to(bot, REQUESTER_ID)
