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
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import (
    CallbackQuery,
    Chat,
    Document,
    ForceReply,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    Update,
)
from aiogram.types import User as TgUser

from bot import texts
from bot.db.repo import Repo
from bot.glpi.client import TICKET_STATUS_CLOSED, TICKET_STATUS_NEW, GlpiError
from bot.glpi.models import Ticket
from bot.glpi.models import User as GlpiUser

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("GLPI_API_URL", "http://127.0.0.1/apirest.php")
os.environ.setdefault("GLPI_USER_TOKEN", "u")

from bot.config import Settings  # noqa: E402
from bot.handlers.access import LeadReject  # noqa: E402
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
        self.delivered: list[tuple[Message, object]] = []
        self.toasts: list[str] = []
        self._message_id = 1000

    def _send(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        self._message_id += 1
        message = Message(
            message_id=self._message_id,
            date=_DATE,
            chat=Chat(id=chat_id, type="private" if chat_id > 0 else "supergroup"),
            from_user=TgUser(id=BOT_ID, is_bot=True, first_name="bot"),
            text=text,
            reply_markup=reply_markup if isinstance(reply_markup, InlineKeyboardMarkup) else None,
        ).as_(self)
        self.delivered.append((message, reply_markup))
        return message

    async def __call__(self, method, **kwargs):
        name = type(method).__name__
        if name == "SendMessage":
            return self._send(method.chat_id, method.text, method.reply_markup)
        if name == "EditMessageText":
            self.sent.append((method.chat_id, method.text, method.reply_markup))
        elif name == "AnswerCallbackQuery" and method.text:
            self.toasts.append(method.text)
        return MagicMock()

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        return self._send(chat_id, text, reply_markup)

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


def _dm(bot, uid, from_id, text, *, reply_to=None, **fields):
    msg = Message(
        message_id=uid,
        date=_DATE,
        chat=Chat(id=from_id, type="private"),
        from_user=TgUser(id=from_id, is_bot=False, first_name="U"),
        text=text,
        reply_to_message=reply_to,
        **fields,
    ).as_(bot)
    return Update(update_id=uid, message=msg)


def _cb(bot, uid, from_id, data, *, message=None):
    msg = message or Message(
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


def _lead_state(dp, bot) -> FSMContext:
    return FSMContext(
        storage=dp.storage,
        key=StorageKey(bot_id=bot.id, chat_id=LEAD_ID, user_id=LEAD_ID),
    )


def _reply_prompt(bot: FakeBot) -> Message:
    return next(
        message
        for message, markup in reversed(bot.delivered)
        if message.chat.id == LEAD_ID and isinstance(markup, ForceReply)
    )


def _cancel_control(bot: FakeBot) -> Message:
    return next(
        message
        for message, markup in reversed(bot.delivered)
        if message.chat.id == LEAD_ID
        and isinstance(markup, InlineKeyboardMarkup)
        and any(
            button.callback_data == "dlg:cancel" for row in markup.inline_keyboard for button in row
        )
    )


def _assert_role_menu(bot: FakeBot, *, is_tech: bool = False) -> None:
    markup = next(
        markup
        for chat, _, markup in reversed(bot.sent)
        if chat == LEAD_ID and isinstance(markup, ReplyKeyboardMarkup)
    )
    buttons = {button.text for row in markup.keyboard for button in row}
    assert texts.BTN_NEW_TICKET in buttons and texts.BTN_ACCESS in buttons
    assert (texts.BTN_ALL_TICKETS if is_tech else texts.BTN_MY_TICKETS) in buttons
    assert (texts.BTN_STATS in buttons) is is_tech


async def _run_dialog_to_sent(dp, bot):
    """Full requester dialog (short flow): one message -> pick lead -> send."""
    await dp.feed_update(bot, _dm(bot, 1, REQUESTER_ID, texts.BTN_ACCESS))
    await dp.feed_update(bot, _cb(bot, 2, REQUESTER_ID, "ac:go"))  # past the notice
    await dp.feed_update(bot, _dm(bot, 3, REQUESTER_ID, "Jenkins\nнужны права админа"))
    await dp.feed_update(bot, _cb(bot, 4, REQUESTER_ID, f"ac:lead:{LEAD_GLPI}"))
    await dp.feed_update(bot, _cb(bot, 5, REQUESTER_ID, "ac:send"))


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
    await dp.feed_update(bot, _cb(bot, 2, LEAD_ID, "ac:go"))
    await dp.feed_update(bot, _dm(bot, 3, LEAD_ID, "Jenkins"))
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
    assert tracked.last_followup_id == 900
    client.add_followup.assert_awaited_once()
    assert client.add_followup.await_args.args[0] == TICKET
    assert "нет обоснования" in client.add_followup.await_args.args[1]
    assert await _lead_state(dp, bot).get_state() is None
    assert await _lead_state(dp, bot).get_data() == {}
    _assert_role_menu(bot)
    # requester sees the reason
    assert any("нет обоснования" in t for t in _to(bot, REQUESTER_ID))


async def test_reject_prompt_opens_reply_input_and_keeps_cancel_control(env):
    dp, client, repo = env
    bot = FakeBot()
    await _run_dialog_to_sent(dp, bot)
    bot.sent.clear()

    await dp.feed_update(bot, _cb(bot, 10, LEAD_ID, f"ap:no:{TICKET}"))

    control = _cancel_control(bot)
    prompt = _reply_prompt(bot)
    assert control.text == texts.ACC_ASK_REJECT_REASON
    assert "/cancel" in control.text
    assert prompt.text == texts.acc_reject_reply_prompt(TICKET)
    assert control.message_id < prompt.message_id  # the last send opens the input field
    chat, _, markup = bot.sent[-1]
    assert chat == LEAD_ID and isinstance(markup, ForceReply)
    assert markup.force_reply is True
    assert markup.input_field_placeholder == texts.ACC_REJECT_REASON_PLACEHOLDER
    assert 1 <= len(markup.input_field_placeholder) <= 64
    state = _lead_state(dp, bot)
    assert await state.get_state() == LeadReject.entering_reason.state
    data = await state.get_data()
    assert data["reject_ticket_id"] == TICKET
    assert data["reject_prompt_id"] == prompt.message_id
    assert (await repo.get_approval(TICKET))["status"] == 0
    client.answer_validation.assert_not_awaited()


async def test_reply_to_force_reply_prompt_dispatches_to_rejection(env):
    dp, client, repo = env
    bot = FakeBot()
    await _run_dialog_to_sent(dp, bot)
    await dp.feed_update(bot, _cb(bot, 10, LEAD_ID, f"ap:no:{TICKET}"))
    prompt = _reply_prompt(bot)
    bot.sent.clear()

    await dp.feed_update(bot, _dm(bot, 11, LEAD_ID, "  Сервис уже оплачен  ", reply_to=prompt))

    client.answer_validation.assert_awaited_once()
    kwargs = client.answer_validation.await_args.kwargs
    assert kwargs["accepted"] is False
    assert "Сервис уже оплачен" in kwargs["comment"]
    assert (await repo.get_approval(TICKET))["status"] == -1
    assert texts.REPLY_NOT_YOUR_TICKET not in _to(bot, LEAD_ID)
    assert client.create_ticket.await_count == 1  # no generic "create ticket from text" flow
    client.add_followup.assert_awaited_once()  # no ordinary reply comment in addition
    assert await _lead_state(dp, bot).get_state() is None
    _assert_role_menu(bot)


@pytest.mark.parametrize("via_command", [True, False], ids=["command", "button"])
@pytest.mark.parametrize("is_tech", [False, True], ids=["employee", "technician"])
async def test_cancel_rejection_keeps_approval_pending_and_restores_menu(env, via_command, is_tech):
    dp, client, repo = env
    bot = FakeBot()
    await _run_dialog_to_sent(dp, bot)
    await repo.set_tech_checked(LEAD_ID, is_tech=is_tech, now=int(time.time()))
    await dp.feed_update(bot, _cb(bot, 10, LEAD_ID, f"ap:no:{TICKET}"))
    prompt = _reply_prompt(bot)
    control = _cancel_control(bot)
    bot.sent.clear()

    update = (
        _dm(bot, 11, LEAD_ID, "/cancel", reply_to=prompt)
        if via_command
        else _cb(bot, 11, LEAD_ID, "dlg:cancel", message=control)
    )
    await dp.feed_update(bot, update)

    assert texts.DIALOG_CANCELLED in _to(bot, LEAD_ID)
    assert await _lead_state(dp, bot).get_state() is None
    assert await _lead_state(dp, bot).get_data() == {}
    assert (await repo.get_approval(TICKET))["status"] == 0
    tracked = await repo.get_tracked_ticket(TICKET)
    assert tracked.last_status == TICKET_STATUS_NEW and tracked.active
    client.answer_validation.assert_not_awaited()
    client.set_ticket_status.assert_not_awaited()
    client.add_followup.assert_not_awaited()
    _assert_role_menu(bot, is_tech=is_tech)

    # Text after cancellation must not unexpectedly finish the previous rejection.
    await dp.feed_update(bot, _dm(bot, 12, LEAD_ID, "Не отклонять, я передумала"))
    client.answer_validation.assert_not_awaited()
    assert (await repo.get_approval(TICKET))["status"] == 0


@pytest.mark.parametrize("kind", ["blank", "document"])
async def test_rejection_requires_nonblank_text_and_reprompts(env, kind):
    dp, client, repo = env
    bot = FakeBot()
    await _run_dialog_to_sent(dp, bot)
    await dp.feed_update(bot, _cb(bot, 10, LEAD_ID, f"ap:no:{TICKET}"))
    original_prompt = _reply_prompt(bot)
    bot.sent.clear()

    update = (
        _dm(bot, 11, LEAD_ID, " \n\t ", reply_to=original_prompt)
        if kind == "blank"
        else _dm(
            bot,
            11,
            LEAD_ID,
            None,
            reply_to=original_prompt,
            document=Document(file_id="f", file_unique_id="u", file_name="reason.txt"),
        )
    )
    await dp.feed_update(bot, update)

    client.answer_validation.assert_not_awaited()
    client.set_ticket_status.assert_not_awaited()
    client.add_followup.assert_not_awaited()
    assert (await repo.get_approval(TICKET))["status"] == 0
    assert texts.ACC_REJECT_REASON_REQUIRED in _to(bot, LEAD_ID)
    prompt = _reply_prompt(bot)
    assert prompt.message_id != original_prompt.message_id
    state = _lead_state(dp, bot)
    assert await state.get_state() == LeadReject.entering_reason.state
    assert (await state.get_data())["reject_prompt_id"] == prompt.message_id

    await dp.feed_update(bot, _dm(bot, 12, LEAD_ID, "Дублирующая заявка", reply_to=prompt))
    client.answer_validation.assert_awaited_once()
    assert (await repo.get_approval(TICKET))["status"] == -1
    _assert_role_menu(bot)


async def test_glpi_failure_keeps_rejection_open_for_retry(env):
    dp, client, repo = env
    bot = FakeBot()
    await _run_dialog_to_sent(dp, bot)
    await dp.feed_update(bot, _cb(bot, 10, LEAD_ID, f"ap:no:{TICKET}"))
    client.answer_validation.side_effect = [GlpiError("temporary failure"), None]
    bot.sent.clear()

    await dp.feed_update(bot, _dm(bot, 11, LEAD_ID, "Нет бюджета", reply_to=_reply_prompt(bot)))

    assert texts.GLPI_ERROR in _to(bot, LEAD_ID)
    assert await _lead_state(dp, bot).get_state() == LeadReject.entering_reason.state
    assert (await repo.get_approval(TICKET))["status"] == 0
    client.set_ticket_status.assert_not_awaited()
    client.add_followup.assert_not_awaited()
    assert not _to(bot, REQUESTER_ID)

    await dp.feed_update(bot, _dm(bot, 12, LEAD_ID, "Нет бюджета"))

    assert client.answer_validation.await_count == 2
    assert all(
        call.kwargs["accepted"] is False for call in client.answer_validation.await_args_list
    )
    client.set_ticket_status.assert_awaited_once_with(TICKET, TICKET_STATUS_CLOSED)
    client.add_followup.assert_awaited_once()
    assert (await repo.get_approval(TICKET))["status"] == -1
    assert (await repo.get_tracked_ticket(TICKET)).last_followup_id == 900
    assert await _lead_state(dp, bot).get_state() is None
    _assert_role_menu(bot)


async def test_approving_while_entering_reason_clears_rejection_state(env):
    dp, client, repo = env
    bot = FakeBot()
    await _run_dialog_to_sent(dp, bot)
    await dp.feed_update(bot, _cb(bot, 10, LEAD_ID, f"ap:no:{TICKET}"))

    await dp.feed_update(bot, _cb(bot, 11, LEAD_ID, f"ap:ok:{TICKET}"))

    assert (await repo.get_approval(TICKET))["status"] == 1
    assert await _lead_state(dp, bot).get_state() is None
    assert await _lead_state(dp, bot).get_data() == {}
    await dp.feed_update(bot, _dm(bot, 12, LEAD_ID, "Поздний текст"))
    client.answer_validation.assert_awaited_once()
    assert client.answer_validation.await_args.kwargs["accepted"] is True
    client.set_ticket_status.assert_not_awaited()


async def test_rejection_of_already_decided_ticket_clears_dialog_without_glpi_write(env):
    dp, client, repo = env
    bot = FakeBot()
    await _run_dialog_to_sent(dp, bot)
    await dp.feed_update(bot, _cb(bot, 10, LEAD_ID, f"ap:no:{TICKET}"))
    await repo.set_approval_status(TICKET, status=1)
    bot.sent.clear()

    await dp.feed_update(bot, _dm(bot, 11, LEAD_ID, "Поздний отказ"))

    assert texts.ACC_ANSWERED_STALE in _to(bot, LEAD_ID)
    assert await _lead_state(dp, bot).get_state() is None
    assert await _lead_state(dp, bot).get_data() == {}
    client.answer_validation.assert_not_awaited()
    client.set_ticket_status.assert_not_awaited()
    client.add_followup.assert_not_awaited()
    assert (await repo.get_approval(TICKET))["status"] == 1
    _assert_role_menu(bot)


async def test_reply_to_previous_rejection_prompt_cannot_reject_another_ticket(env):
    dp, client, repo = env
    bot = FakeBot()
    await _run_dialog_to_sent(dp, bot)
    other_ticket = TICKET + 1
    await repo.track_ticket(
        ticket_id=other_ticket,
        requester_tg_id=REQUESTER_ID,
        requester_glpi_id=8,
        status=TICKET_STATUS_NEW,
        now=int(time.time()),
    )
    await repo.create_approval(
        other_ticket,
        validation_id=6,
        lead_glpi_id=LEAD_GLPI,
        lead_tg_id=LEAD_ID,
        lead_name="Ульяна Метлина",
        requester_tg_id=REQUESTER_ID,
        now=int(time.time()),
    )
    await dp.feed_update(bot, _cb(bot, 10, LEAD_ID, f"ap:no:{TICKET}"))
    previous_prompt = _reply_prompt(bot)
    await dp.feed_update(bot, _cb(bot, 11, LEAD_ID, f"ap:no:{other_ticket}"))
    bot.sent.clear()

    await dp.feed_update(
        bot, _dm(bot, 12, LEAD_ID, "Причина по первой заявке", reply_to=previous_prompt)
    )

    client.answer_validation.assert_not_awaited()
    client.set_ticket_status.assert_not_awaited()
    assert (await repo.get_approval(TICKET))["status"] == 0
    assert (await repo.get_approval(other_ticket))["status"] == 0
    assert texts.ACC_REJECT_REPLY_STALE in _to(bot, LEAD_ID)
    prompt = _reply_prompt(bot)
    data = await _lead_state(dp, bot).get_data()
    assert data["reject_ticket_id"] == other_ticket
    assert data["reject_prompt_id"] == prompt.message_id
    assert prompt.text == texts.acc_reject_reply_prompt(other_ticket)

    await dp.feed_update(bot, _dm(bot, 13, LEAD_ID, "Причина по второй заявке", reply_to=prompt))
    client.answer_validation.assert_awaited_once()
    assert client.answer_validation.await_args.args[0] == 6
    client.set_ticket_status.assert_awaited_once_with(other_ticket, TICKET_STATUS_CLOSED)
    assert (await repo.get_approval(TICKET))["status"] == 0
    assert (await repo.get_approval(other_ticket))["status"] == -1


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
    await dp.feed_update(bot, _cb(bot, 2, REQUESTER_ID, "ac:go"))
    await dp.feed_update(bot, _dm(bot, 3, REQUESTER_ID, "Jenkins"))

    # straight to the combined confirm screen — no pick list, no extra steps
    msgs = _to(bot, REQUESTER_ID)
    assert any("Согласует:" in t and "Ульяна Метлина" in t for t in msgs)
    assert not any(texts.ACC_CHOOSE_LEAD == t for t in msgs)
    # one tap sends
    await dp.feed_update(bot, _cb(bot, 4, REQUESTER_ID, "ac:send"))
    client.create_ticket.assert_awaited_once()
    assert any("отправлен на согласование" in t for t in _to(bot, REQUESTER_ID))


async def test_confirm_pick_another_shows_full_list(env):
    dp, client, repo = env
    await repo.set_user_lead(8, LEAD_GLPI)
    bot = FakeBot()
    await dp.feed_update(bot, _dm(bot, 1, REQUESTER_ID, texts.BTN_ACCESS))
    await dp.feed_update(bot, _cb(bot, 2, REQUESTER_ID, "ac:go"))
    await dp.feed_update(bot, _dm(bot, 3, REQUESTER_ID, "Jenkins"))
    await dp.feed_update(bot, _cb(bot, 4, REQUESTER_ID, "ac:other"))
    assert any(texts.ACC_CHOOSE_LEAD == t for t in _to(bot, REQUESTER_ID))


async def test_unmapped_user_gets_pick_list_as_before(env):
    dp, client, repo = env  # no user_leads row
    bot = FakeBot()
    await dp.feed_update(bot, _dm(bot, 1, REQUESTER_ID, texts.BTN_ACCESS))
    await dp.feed_update(bot, _cb(bot, 2, REQUESTER_ID, "ac:go"))
    await dp.feed_update(bot, _dm(bot, 3, REQUESTER_ID, "Jenkins"))
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


async def test_access_button_shows_new_access_notice_first(env):
    dp, client, repo = env
    bot = FakeBot()
    await dp.feed_update(bot, _dm(bot, 1, REQUESTER_ID, texts.BTN_ACCESS))
    # the notice, not the question — the question comes only after «Продолжить»
    msgs = _to(bot, REQUESTER_ID)
    assert any(texts.ACCESS_WARNING == t for t in msgs)
    assert not any(texts.ACC_ASK_REQUEST == t for t in msgs)

    await dp.feed_update(bot, _cb(bot, 2, REQUESTER_ID, "ac:go"))
    assert any(texts.ACC_ASK_REQUEST == t for t in _to(bot, REQUESTER_ID))
