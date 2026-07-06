"""/tickets — the linked user's open tickets, detail view, and add-comment
(feature 3).

Flow:
    /tickets (or the "📋 Мои заявки" button) -> list of open tickets, one button
    each -> tap -> detail (status, assignee, last 5 public followups) -> "add
    comment" -> FSM -> a followup is created on behalf of the requester.

Gated by :class:`AuthMiddleware`, so ``link`` (the requester) is always injected.

A comment added here is authored by the service account (legacy API limitation),
so the sync loop would otherwise echo it back to the requester. We therefore
advance that ticket's followup cursor past the new followup — see the note in
``on_comment_text``.
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import texts
from ..db.repo import LinkedUser, Repo
from ..glpi.client import (
    OPEN_TICKET_STATUSES,
    TICKET_STATUS_CLOSED,
    GlpiClient,
    GlpiError,
)
from ..glpi.models import TicketSummary
from ..services import attachments, notify

log = logging.getLogger(__name__)

_MAX_FOLLOWUPS = 5


class MyTickets(StatesGroup):
    commenting = State()
    closing = State()


def _list_keyboard(summaries: list[TicketSummary]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=texts.btn_open_ticket(s.id, s.title), callback_data=f"mt:open:{s.id}"
            )
        ]
        for s in summaries
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _detail_keyboard(ticket_id: int, *, closable: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=texts.BTN_MYT_COMMENT, callback_data=f"mt:comment:{ticket_id}")]
    ]
    if closable:
        rows.append(
            [InlineKeyboardButton(text=texts.BTN_MYT_CLOSE, callback_data=f"mt:close:{ticket_id}")]
        )
    rows.append([InlineKeyboardButton(text=texts.BTN_MYT_BACK, callback_data="mt:list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _close_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.BTN_MYT_CLOSE_NO_COMMENT, callback_data="mt:close_empty"
                )
            ]
        ]
    )


def build_my_tickets_router(
    client: GlpiClient,
    repo: Repo,
    *,
    tech_group_chat_id: int | None = None,
    ticket_front_base: str | None = None,
) -> Router:
    router = Router(name="my_tickets")

    async def _render_list(link: LinkedUser) -> tuple[str, InlineKeyboardMarkup | None]:
        summaries = await client.search_user_open_tickets(link.glpi_users_id)
        if not summaries:
            return texts.MY_TICKETS_EMPTY, None
        return texts.MY_TICKETS_HEADER, _list_keyboard(summaries)

    async def _render_detail(ticket_id: int) -> tuple[str, bool]:
        """Return (rendered detail text, whether the requester may close it)."""
        ticket = await client.get_ticket(ticket_id)
        if ticket is None:
            return texts.GLPI_ERROR, False
        assignees = await client.get_ticket_assignees(ticket_id)
        followups = await client.list_followups(ticket_id)
        # Last N public followups, chronological, with resolved author names.
        recent = [f for f in followups if not f.is_private]
        recent.sort(key=lambda f: f.id)
        recent = recent[-_MAX_FOLLOWUPS:]
        name_cache: dict[int, str] = {}
        lines: list[str] = []
        for f in recent:
            author = await _author_name(f.users_id, name_cache)
            lines.append(texts.followup_line(author, f.content))
        text = texts.ticket_detail(
            ticket_id=ticket_id,
            title=ticket.name,
            status=ticket.status,
            assignee=", ".join(assignees) if assignees else None,
            followups=lines,
        )
        return text, ticket.status in OPEN_TICKET_STATUSES

    async def _author_name(user_id: int, cache: dict[int, str]) -> str | None:
        if not user_id:
            return None
        if user_id in cache:
            return cache[user_id]
        try:
            user = await client.get_user(user_id)
        except GlpiError:
            user = None
        name = user.display_name if user else None
        cache[user_id] = name or ""
        return name

    # -- list --------------------------------------------------------------
    @router.message(Command("tickets"))
    async def cmd_tickets(message: Message, state: FSMContext, link: LinkedUser) -> None:
        await state.clear()
        await _answer_list(message, link)

    @router.message(F.text == texts.BTN_MY_TICKETS)
    async def btn_tickets(message: Message, state: FSMContext, link: LinkedUser) -> None:
        await state.clear()
        await _answer_list(message, link)

    async def _answer_list(message: Message, link: LinkedUser) -> None:
        try:
            text, kb = await _render_list(link)
        except GlpiError as exc:
            log.warning("my_tickets_list_failed error=%s", exc)
            await message.answer(texts.GLPI_ERROR)
            return
        await message.answer(text, reply_markup=kb)

    @router.callback_query(F.data == "mt:list")
    async def on_back_to_list(cb: CallbackQuery, link: LinkedUser) -> None:
        try:
            text, kb = await _render_list(link)
        except GlpiError as exc:
            log.warning("my_tickets_list_failed error=%s", exc)
            await cb.answer(texts.GLPI_ERROR, show_alert=True)
            return
        await cb.message.edit_text(text, reply_markup=kb)
        await cb.answer()

    # -- detail ------------------------------------------------------------
    @router.callback_query(F.data.startswith("mt:open:"))
    async def on_open(cb: CallbackQuery) -> None:
        ticket_id = int(cb.data.split(":")[2])
        try:
            text, closable = await _render_detail(ticket_id)
        except GlpiError as exc:
            log.warning("my_tickets_detail_failed id=%s error=%s", ticket_id, exc)
            await cb.answer(texts.GLPI_ERROR, show_alert=True)
            return
        await cb.message.edit_text(
            text, reply_markup=_detail_keyboard(ticket_id, closable=closable)
        )
        await cb.answer()

    # -- add comment -------------------------------------------------------
    @router.callback_query(F.data.startswith("mt:comment:"))
    async def on_comment(cb: CallbackQuery, state: FSMContext) -> None:
        ticket_id = int(cb.data.split(":")[2])
        await state.set_state(MyTickets.commenting)
        await state.update_data(ticket_id=ticket_id)
        await cb.message.answer(texts.myt_ask_comment(ticket_id))
        await cb.answer()

    @router.message(MyTickets.commenting, F.text)
    async def on_comment_text(message: Message, state: FSMContext, link: LinkedUser) -> None:
        data = await state.get_data()
        content = f"{link.display_name}:\n{message.text.strip()}"
        await _finish_comment(message, state, data["ticket_id"], content=content)

    @router.message(MyTickets.commenting, F.photo | F.document)
    async def on_comment_file(
        message: Message, state: FSMContext, link: LinkedUser, bot: Bot
    ) -> None:
        pending = attachments.extract(message)
        if pending is None:
            await message.answer(texts.ATTACH_UNSUPPORTED)
            return
        if attachments.too_large(pending):
            await message.answer(texts.ATTACH_TOO_LARGE)
            return
        data = await state.get_data()
        ticket_id = data["ticket_id"]
        caption = (message.caption or "").strip() or texts.COMMENT_ATTACHMENT_PLACEHOLDER
        try:
            content = await attachments.download(bot, pending.file_id)
            await client.attach_document_to_ticket(
                ticket_id, pending.filename, content, mime=pending.mime
            )
        except Exception as exc:  # noqa: BLE001 - download or upload failure
            log.error("my_tickets_attach_failed id=%s error=%s", ticket_id, exc)
            await message.answer(texts.GLPI_ERROR)
            return
        await _finish_comment(message, state, ticket_id, content=f"{link.display_name}:\n{caption}")

    async def _finish_comment(
        message: Message, state: FSMContext, ticket_id: int, *, content: str
    ) -> None:
        try:
            followup_id = await client.add_followup(ticket_id, content)
        except GlpiError as exc:
            log.error("my_tickets_comment_failed id=%s error=%s raw=%s", ticket_id, exc, exc.raw)
            await message.answer(texts.GLPI_ERROR)
            return
        await state.clear()
        # The followup is service-account authored, so the sync loop can't tell
        # it from a technician's and would echo it back. Advance the cursor past
        # it so the requester isn't notified of their own comment.
        try:
            await repo.set_ticket_followup_cursor(ticket_id, followup_id)
        except Exception:  # noqa: BLE001 - untracked ticket / DB hiccup is non-fatal
            log.exception("my_tickets_cursor_bump_failed id=%s", ticket_id)
        await message.answer(texts.MYT_COMMENT_DONE)
        await _resend_detail(message, ticket_id)

    async def _resend_detail(message: Message, ticket_id: int) -> None:
        try:
            text, closable = await _render_detail(ticket_id)
        except GlpiError as exc:
            log.warning("my_tickets_detail_failed id=%s error=%s", ticket_id, exc)
            return
        await message.answer(text, reply_markup=_detail_keyboard(ticket_id, closable=closable))

    @router.message(MyTickets.commenting)
    async def on_comment_not_text(message: Message) -> None:
        await message.answer(texts.ATTACH_UNSUPPORTED)

    # -- close own ticket --------------------------------------------------
    @router.callback_query(F.data.startswith("mt:close:"))
    async def on_close_start(cb: CallbackQuery, state: FSMContext) -> None:
        ticket_id = int(cb.data.split(":")[2])
        await state.set_state(MyTickets.closing)
        await state.update_data(close_ticket_id=ticket_id)
        await cb.message.answer(texts.MYT_CLOSE_PROMPT, reply_markup=_close_prompt_keyboard())
        await cb.answer()

    async def _do_close(ticket_id: int, reason: str | None, link: LinkedUser, bot: Bot) -> bool:
        """Add the reason followup (if any), close the ticket, notify the techs.

        No confirmation step: any text is the reason, the button closes with none.
        Returns False on GLPI failure so the caller can keep the dialog open.
        """
        # Assignees fetched before closing so the tech-group note can mention them.
        try:
            assignees = await client.get_ticket_assignees(ticket_id)
        except GlpiError:
            assignees = []
        followup_id: int | None = None
        try:
            if reason:
                followup_id = await client.add_followup(
                    ticket_id, texts.close_followup_body(link.display_name, reason)
                )
            await client.set_ticket_status(ticket_id, TICKET_STATUS_CLOSED)
        except GlpiError as exc:
            log.error("my_tickets_close_failed id=%s error=%s raw=%s", ticket_id, exc, exc.raw)
            return False
        # Suppress the sync-loop echo of the requester's own close: advance the
        # followup cursor past the reason and stop watching the (now closed) ticket.
        try:
            if followup_id is not None:
                await repo.set_ticket_followup_cursor(ticket_id, followup_id)
            await repo.set_ticket_status(ticket_id, status=TICKET_STATUS_CLOSED, active=False)
        except Exception:  # noqa: BLE001 - untracked ticket / DB hiccup is non-fatal
            log.exception("my_tickets_close_cursor_bump_failed id=%s", ticket_id)
        if tech_group_chat_id is not None:
            await notify.notify_closed_by_requester(
                bot, tech_group_chat_id, ticket_id, reason, assignees
            )
        return True

    @router.message(MyTickets.closing, F.text)
    async def on_close_reason(
        message: Message, state: FSMContext, link: LinkedUser, bot: Bot
    ) -> None:
        ticket_id = (await state.get_data())["close_ticket_id"]
        if await _do_close(ticket_id, message.text.strip(), link, bot):
            await state.clear()
            await message.answer(texts.MYT_CLOSE_DONE)
        else:
            await message.answer(texts.GLPI_ERROR)  # stay in state so the user can retry

    @router.callback_query(MyTickets.closing, F.data == "mt:close_empty")
    async def on_close_empty(
        cb: CallbackQuery, state: FSMContext, link: LinkedUser, bot: Bot
    ) -> None:
        ticket_id = (await state.get_data())["close_ticket_id"]
        await cb.answer()
        if await _do_close(ticket_id, None, link, bot):
            await state.clear()
            await cb.message.edit_text(texts.MYT_CLOSE_DONE)
        else:
            await cb.message.edit_text(texts.GLPI_ERROR)

    @router.message(MyTickets.closing)
    async def on_close_not_text(message: Message) -> None:
        await message.answer(texts.MYT_CLOSE_PROMPT, reply_markup=_close_prompt_keyboard())

    @router.message(StateFilter(MyTickets), Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(texts.NEW_CANCELLED)

    return router
