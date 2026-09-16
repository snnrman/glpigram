"""«👨‍💻 В работе» — the technician's own assigned-tickets view.

Lists open tickets where the pressing tech is the assigned technician
(Ticket_User type=2), in two groups: in work (assigned/processing) and
awaiting the requester's confirmation (solved). Tapping a ticket opens a
detail view whose Reply/Close buttons reuse the existing ``ta:`` callbacks
from :mod:`tech_actions` — same DM dialogs, same living-card updates.

Techs only: the menu button is hidden from regular users, but the handlers
re-check ``link.is_tech`` themselves (hiding is not access control).
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import texts
from ..db.repo import LinkedUser, Repo
from ..glpi.client import TICKET_STATUS_NEW, TICKET_STATUS_SOLVED, GlpiClient, GlpiError
from ..glpi.models import TicketSummary

log = logging.getLogger(__name__)

_MAX_FOLLOWUPS = 5


def _list_keyboard(
    summaries: list[TicketSummary], *, open_prefix: str = "tt:open"
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=texts.btn_open_ticket(s.id, s.title), callback_data=f"{open_prefix}:{s.id}"
            )
        ]
        for s in summaries
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _detail_keyboard(
    ticket_id: int, *, status: int, back_data: str = "tt:list"
) -> InlineKeyboardMarkup:
    rows = []
    if status == TICKET_STATUS_NEW:
        # Untaken ticket (the «Все заявки» view): the primary action is Take,
        # full-width — the same ta: callback as on the group card.
        rows.append(
            [InlineKeyboardButton(text=texts.BTN_TECH_TAKE, callback_data=f"ta:take:{ticket_id}")]
        )
    actions = [
        InlineKeyboardButton(text=texts.BTN_TECH_COMMENT, callback_data=f"ta:comment:{ticket_id}")
    ]
    if status != TICKET_STATUS_SOLVED:
        # A solved ticket awaits the requester; re-closing it would only
        # re-propose a solution, so the Close action is hidden there.
        actions.append(
            InlineKeyboardButton(text=texts.BTN_TECH_CLOSE, callback_data=f"ta:close:{ticket_id}")
        )
    tail = [InlineKeyboardButton(text=texts.BTN_MYT_BACK, callback_data=back_data)]
    if status not in (TICKET_STATUS_SOLVED, TICKET_STATUS_NEW):
        # Handoff needs a current assignee — pointless on an untaken ticket.
        tail.insert(
            0,
            InlineKeyboardButton(text=texts.BTN_HANDOFF, callback_data=f"ta:handoff:{ticket_id}"),
        )
    return InlineKeyboardMarkup(inline_keyboard=[*rows, actions, tail])


def build_tech_tickets_router(
    client: GlpiClient, *, repo: Repo | None = None, ticket_front_base: str | None = None
) -> Router:
    router = Router(name="tech_tickets")
    # Dialogs and menu buttons are private-chat only: in groups the bot
    # must not react to free text (callbacks are not affected).
    router.message.filter(F.chat.type == "private")

    def _ticket_url(ticket_id: int) -> str | None:
        if not ticket_front_base:
            return None
        return f"{ticket_front_base}/front/ticket.form.php?id={ticket_id}"

    async def _render_list(link: LinkedUser) -> tuple[str, InlineKeyboardMarkup | None]:
        summaries = await client.search_tech_open_tickets(link.glpi_users_id)
        if not summaries:
            return texts.TECH_TICKETS_EMPTY, None
        in_work = [s for s in summaries if s.status != TICKET_STATUS_SOLVED]
        waiting = [s for s in summaries if s.status == TICKET_STATUS_SOLVED]
        text = texts.tech_tickets_list(
            [(s.id, s.title) for s in in_work], [(s.id, s.title) for s in waiting]
        )
        return text, _list_keyboard(in_work + waiting)

    @router.message(F.text == texts.BTN_TECH_TICKETS)
    async def btn_assigned(message: Message, state: FSMContext, link: LinkedUser) -> None:
        if not link.is_tech:
            await message.answer(texts.TECH_ONLY)
            return
        await state.clear()
        try:
            text, kb = await _render_list(link)
        except GlpiError as exc:
            log.warning("tech_tickets_failed error=%s raw=%s", exc, exc.raw)
            await message.answer(texts.GLPI_ERROR)
            return
        await message.answer(text, reply_markup=kb)

    @router.callback_query(F.data == "tt:list")
    async def cb_back_to_list(cb: CallbackQuery, link: LinkedUser) -> None:
        if not link.is_tech:
            await cb.answer(texts.TECH_ONLY, show_alert=True)
            return
        try:
            text, kb = await _render_list(link)
        except GlpiError as exc:
            log.warning("tech_tickets_failed error=%s raw=%s", exc, exc.raw)
            await cb.answer(texts.GLPI_ERROR, show_alert=True)
            return
        await cb.message.edit_text(text, reply_markup=kb)
        await cb.answer()

    @router.callback_query(F.data.startswith("tt:open:"))
    async def cb_open(cb: CallbackQuery, link: LinkedUser) -> None:
        await _open_detail(cb, link, back_data="tt:list")

    # --- «📥 Все заявки»: the queue nobody has taken yet (techs only) -------
    async def _render_unassigned() -> tuple[str, InlineKeyboardMarkup | None]:
        summaries = await client.search_unassigned_tickets()
        if not summaries:
            return texts.ALL_TICKETS_EMPTY, None
        text = texts.all_tickets_list([(s.id, s.title) for s in summaries])
        return text, _list_keyboard(summaries, open_prefix="tt:openu")

    @router.message(F.text == texts.BTN_ALL_TICKETS)
    async def btn_all_tickets(message: Message, state: FSMContext, link: LinkedUser) -> None:
        if not link.is_tech:
            await message.answer(texts.TECH_ONLY)
            return
        await state.clear()
        try:
            text, kb = await _render_unassigned()
        except GlpiError as exc:
            log.warning("all_tickets_failed error=%s raw=%s", exc, exc.raw)
            await message.answer(texts.GLPI_ERROR)
            return
        await message.answer(text, reply_markup=kb)

    @router.callback_query(F.data == "tt:all")
    async def cb_back_to_unassigned(cb: CallbackQuery, link: LinkedUser) -> None:
        if not link.is_tech:
            await cb.answer(texts.TECH_ONLY, show_alert=True)
            return
        try:
            text, kb = await _render_unassigned()
        except GlpiError as exc:
            log.warning("all_tickets_failed error=%s raw=%s", exc, exc.raw)
            await cb.answer(texts.GLPI_ERROR, show_alert=True)
            return
        await cb.message.edit_text(text, reply_markup=kb)
        await cb.answer()

    @router.callback_query(F.data.startswith("tt:openu:"))
    async def cb_open_unassigned(cb: CallbackQuery, link: LinkedUser) -> None:
        await _open_detail(cb, link, back_data="tt:all")

    async def _open_detail(cb: CallbackQuery, link: LinkedUser, *, back_data: str) -> None:
        if not link.is_tech:
            await cb.answer(texts.TECH_ONLY, show_alert=True)
            return
        ticket_id = int(cb.data.split(":")[2])
        try:
            text, status = await _render_detail(ticket_id)
        except GlpiError as exc:
            log.warning("tech_ticket_detail_failed ticket=%s error=%s", ticket_id, exc)
            await cb.answer(texts.GLPI_ERROR, show_alert=True)
            return
        await cb.message.edit_text(
            text, reply_markup=_detail_keyboard(ticket_id, status=status, back_data=back_data)
        )
        await cb.answer()

    async def _render_detail(ticket_id: int) -> tuple[str, int]:
        """Detail text + status; raises GlpiError upstream on failure."""
        ticket = await client.get_ticket(ticket_id)
        if ticket is None:
            raise GlpiError(f"ticket {ticket_id} not found")
        followups = await client.list_followups(ticket_id)
        recent = sorted((f for f in followups if not f.is_private), key=lambda f: f.id)
        recent = recent[-_MAX_FOLLOWUPS:]
        name_cache: dict[int, str] = {}
        lines = [
            texts.followup_line(await _author_name(f.users_id, name_cache), f.content)
            for f in recent
        ]
        assignees = await client.get_ticket_assignees(ticket_id)
        # Who filed it — essential context for a tech; lookup failure must not
        # cost the whole detail view.
        try:
            req = await client.get_ticket_requester(ticket_id)
        except GlpiError as exc:
            log.warning("tech_ticket_requester_failed ticket=%s error=%s", ticket_id, exc)
            req = None
        # Telegram id of the requester (if they linked the bot) -> clickable
        # profile link, so the tech can write to them directly.
        req_tg = None
        if req and repo is not None:
            link = await repo.get_by_glpi(req[0])
            req_tg = link.tg_id if link else None
        text = texts.ticket_detail(
            ticket_id=ticket_id,
            title=ticket.name,
            description=ticket.content,
            status=ticket.status,
            assignee=", ".join(assignees) if assignees else None,
            followups=lines,
            url=_ticket_url(ticket_id),
            urgency=ticket.urgency or None,
            requester=req[1] if req else None,
            requester_tg_id=req_tg,
        )
        return text, ticket.status

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

    return router
