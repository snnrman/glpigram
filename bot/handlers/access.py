"""«🔑 Доступ» — access requests approved by a lead (feature: lead approval).

Flow:
    requester: system -> what/why -> duration -> pick a lead -> confirm
    -> ticket + GLPI TicketValidation (global_validation = waiting, the take
       gate) -> DM to the lead with Approve/Reject buttons
    lead: one tap to approve; rejecting asks for a mandatory reason
    -> the bot answers the validation (naming the lead in the comment — GLPI
       refuses answering on behalf of another user, verified live), adds a
       followup, updates the living card and notifies the requester.

The lead list comes from config (`LEAD_LOGINS`, AD logins) and is resolved
against GLPI + bot links with a TTL cache — composition changes need only an
.env edit and restart.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import texts
from ..cache import TTLValue
from ..db.repo import LinkedUser, Repo
from ..glpi.client import TICKET_STATUS_CLOSED, TICKET_STATUS_NEW, GlpiClient, GlpiError
from ..services import notify
from ..services.cards import CardService
from .new_ticket import main_menu_keyboard

log = logging.getLogger(__name__)


class AccessRequest(StatesGroup):
    entering_system = State()
    entering_details = State()
    choosing_duration = State()
    entering_until = State()
    choosing_lead = State()
    confirming = State()


class LeadReject(StatesGroup):
    entering_reason = State()  # in the lead's DM


@dataclass(slots=True)
class Lead:
    glpi_id: int
    name: str
    tg_id: int | None  # None = lead not linked to the bot (hidden from the pick)


def build_lead_directory(client: GlpiClient, repo: Repo, logins: list[str], ttl: int) -> TTLValue:
    """TTL-cached resolver: configured AD logins -> Lead entries."""

    async def _load() -> list[Lead]:
        leads: list[Lead] = []
        for login in logins:
            try:
                user = await client.find_user_by_login(login)
            except GlpiError as exc:
                log.warning("lead_resolve_failed login=%s error=%s", login, exc)
                continue
            if user is None:
                log.warning("lead_login_unknown login=%s", login)
                continue
            link = await repo.get_by_glpi(user.id)
            leads.append(
                Lead(glpi_id=user.id, name=user.display_name, tg_id=link.tg_id if link else None)
            )
        return leads

    return TTLValue(_load, ttl)


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="ac:cancel")]]
    )


def _duration_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=texts.BTN_ACC_PERMANENT, callback_data="ac:dur:perm"),
                InlineKeyboardButton(text=texts.BTN_ACC_TEMPORARY, callback_data="ac:dur:temp"),
            ],
            [InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="ac:cancel")],
        ]
    )


def _leads_kb(leads: list[Lead]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=lead.name, callback_data=f"ac:lead:{lead.glpi_id}")]
        for lead in leads
    ]
    rows.append([InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="ac:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=texts.BTN_ACC_SEND, callback_data="ac:send"),
                InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="ac:cancel"),
            ]
        ]
    )


def approval_kb(ticket_id: int) -> InlineKeyboardMarkup:
    """The lead's DM buttons."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.BTN_ACC_APPROVE, callback_data=f"ap:ok:{ticket_id}"
                ),
                InlineKeyboardButton(text=texts.BTN_ACC_REJECT, callback_data=f"ap:no:{ticket_id}"),
            ]
        ]
    )


def build_access_router(
    client: GlpiClient,
    repo: Repo,
    lead_directory: TTLValue,
    *,
    ticket_front_base: str | None = None,
    access_category_id: int | None = None,
    cards: CardService | None = None,
) -> Router:
    router = Router(name="access")
    router.message.filter(F.chat.type == "private")

    def _url(ticket_id: int) -> str | None:
        if not ticket_front_base:
            return None
        return f"{ticket_front_base}/front/ticket.form.php?id={ticket_id}"

    # --- requester dialog ---------------------------------------------------
    @router.message(Command("access"))
    @router.message(F.text == texts.BTN_ACCESS)
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(AccessRequest.entering_system)
        await message.answer(texts.ACC_ASK_SYSTEM, reply_markup=_cancel_kb())

    @router.message(StateFilter(AccessRequest), Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext, link: LinkedUser) -> None:
        await state.clear()
        await message.answer(
            texts.NEW_CANCELLED, reply_markup=main_menu_keyboard(is_tech=link.is_tech)
        )

    @router.callback_query(F.data == "ac:cancel")
    async def on_cancel(cb: CallbackQuery, state: FSMContext, link: LinkedUser) -> None:
        await state.clear()
        await notify.safe_edit(cb, texts.NEW_CANCELLED)
        await cb.answer()

    @router.message(AccessRequest.entering_system, F.text)
    async def on_system(message: Message, state: FSMContext) -> None:
        await state.update_data(system=message.text.strip())
        await state.set_state(AccessRequest.entering_details)
        await message.answer(texts.ACC_ASK_DETAILS, reply_markup=_cancel_kb())

    @router.message(AccessRequest.entering_details, F.text)
    async def on_details(message: Message, state: FSMContext) -> None:
        await state.update_data(details=message.text.strip())
        await state.set_state(AccessRequest.choosing_duration)
        await message.answer(texts.ACC_ASK_DURATION, reply_markup=_duration_kb())

    @router.callback_query(AccessRequest.choosing_duration, F.data.startswith("ac:dur:"))
    async def on_duration(cb: CallbackQuery, state: FSMContext) -> None:
        if cb.data.endswith(":perm"):
            await state.update_data(duration=texts.ACC_DURATION_PERMANENT)
            await _open_lead_step(cb, state)
        else:
            await state.set_state(AccessRequest.entering_until)
            await notify.safe_edit(cb, texts.ACC_ASK_UNTIL, reply_markup=_cancel_kb())
        await cb.answer()

    @router.message(AccessRequest.entering_until, F.text)
    async def on_until(message: Message, state: FSMContext) -> None:
        await state.update_data(duration=message.text.strip())
        try:
            leads = await _pickable_leads(message.from_user.id)
        except GlpiError as exc:
            log.warning("access_leads_failed error=%s", exc)
            leads = []
        if not leads:
            await state.clear()
            await message.answer(texts.ACC_NO_LEADS)
            return
        await state.set_state(AccessRequest.choosing_lead)
        await message.answer(texts.ACC_CHOOSE_LEAD, reply_markup=_leads_kb(leads))

    async def _pickable_leads(requester_tg_id: int) -> list:
        # Only linked leads can answer in TG; self-approval is excluded.
        leads = await lead_directory.get()
        return [x for x in leads if x.tg_id is not None and x.tg_id != requester_tg_id]

    async def _open_lead_step(cb: CallbackQuery, state: FSMContext) -> None:
        try:
            leads = await _pickable_leads(cb.from_user.id)
        except GlpiError as exc:
            log.warning("access_leads_failed error=%s", exc)
            leads = []
        if not leads:
            await state.clear()
            await notify.safe_edit(cb, texts.ACC_NO_LEADS)
            return
        await state.set_state(AccessRequest.choosing_lead)
        await notify.safe_edit(cb, texts.ACC_CHOOSE_LEAD, reply_markup=_leads_kb(leads))

    @router.callback_query(AccessRequest.choosing_lead, F.data.startswith("ac:lead:"))
    async def on_lead(cb: CallbackQuery, state: FSMContext) -> None:
        lead_id = int(cb.data.rsplit(":", 1)[1])
        leads = await _pickable_leads(cb.from_user.id)
        lead = next((x for x in leads if x.glpi_id == lead_id), None)
        if lead is None:
            await cb.answer(texts.STALE_BUTTON, show_alert=True)
            return
        await state.update_data(
            lead_glpi_id=lead.glpi_id, lead_name=lead.name, lead_tg_id=lead.tg_id
        )
        data = await state.get_data()
        await state.set_state(AccessRequest.confirming)
        await notify.safe_edit(
            cb,
            texts.acc_confirm_summary(data["system"], data["details"], data["duration"], lead.name),
            reply_markup=_confirm_kb(),
        )
        await cb.answer()

    @router.callback_query(AccessRequest.confirming, F.data == "ac:send")
    async def on_send(cb: CallbackQuery, state: FSMContext, link: LinkedUser, bot: Bot) -> None:
        data = await state.get_data()
        await notify.safe_edit(cb, texts.NEW_CREATING)
        await cb.answer()
        try:
            ticket_id = await client.create_ticket(
                name=texts.acc_ticket_title(data["system"]),
                content=texts.acc_ticket_content(
                    link.display_name,
                    data["system"],
                    data["details"],
                    data["duration"],
                    data["lead_name"],
                ),
                urgency=3,
                itilcategories_id=access_category_id,
                requester_users_id=link.glpi_users_id,
            )
            validation_id = await client.create_validation(
                ticket_id,
                comment=f"Согласующий лид: {data['lead_name']} (ответ через Telegram-бота)",
            )
        except GlpiError as exc:
            log.exception("access_create_failed error=%s raw=%s", exc, exc.raw)
            await cb.message.answer(texts.GLPI_ERROR)
            await state.clear()
            return
        now = int(time.time())
        try:
            await repo.track_ticket(
                ticket_id=ticket_id,
                requester_tg_id=cb.from_user.id,
                requester_glpi_id=link.glpi_users_id,
                status=TICKET_STATUS_NEW,
                now=now,
            )
            await repo.create_approval(
                ticket_id,
                validation_id=validation_id,
                lead_glpi_id=data["lead_glpi_id"],
                lead_tg_id=data["lead_tg_id"],
                lead_name=data["lead_name"],
                requester_tg_id=cb.from_user.id,
                now=now,
            )
        except Exception:  # noqa: BLE001 - bookkeeping must not fail the request
            log.exception("access_bookkeeping_failed ticket=%s", ticket_id)
        await state.clear()

        # DM the lead right away (personal message — not gated by quiet hours).
        msg = await notify.send_text(
            bot,
            data["lead_tg_id"],
            texts.acc_lead_prompt(
                ticket_id=ticket_id,
                requester=link.display_name,
                system=data["system"],
                details=data["details"],
                duration=data["duration"],
                url=_url(ticket_id),
            ),
            reply_markup=approval_kb(ticket_id),
        )
        if msg:
            await repo.set_approval_dm(ticket_id, msg.message_id)
        await cb.message.answer(
            texts.acc_sent(ticket_id, data["lead_name"], _url(ticket_id)),
            reply_markup=main_menu_keyboard(is_tech=link.is_tech),
        )

    # --- the lead answers -----------------------------------------------------
    async def _own_pending(cb: CallbackQuery, ticket_id: int):
        row = await repo.get_approval(ticket_id)
        if row is None or row["status"] != 0 or row["lead_tg_id"] != cb.from_user.id:
            await cb.answer(texts.ACC_ANSWERED_STALE, show_alert=True)
            return None
        return row

    @router.callback_query(F.data.startswith("ap:ok:"))
    async def on_approve(cb: CallbackQuery, bot: Bot) -> None:
        ticket_id = int(cb.data.rsplit(":", 1)[1])
        row = await _own_pending(cb, ticket_id)
        if row is None:
            return
        lead = row["lead_name"]
        try:
            await client.answer_validation(
                row["validation_id"],
                accepted=True,
                comment=texts.acc_validation_comment_approved(lead),
            )
        except GlpiError as exc:
            log.exception(
                "access_approve_failed ticket=%s error=%s raw=%s", ticket_id, exc, exc.raw
            )
            await cb.answer(texts.GLPI_ERROR, show_alert=True)
            return
        await repo.set_approval_status(ticket_id, status=1)
        await _add_followup_quiet(ticket_id, texts.acc_followup_approved(lead))
        # Feedback: the lead's DM, the requester, the living card.
        if isinstance(cb.message, Message):
            await notify.safe_edit(
                cb, texts.acc_lead_answered(texts.acc_decision_approved(lead), cb.message.html_text)
            )
        await cb.answer(texts.BTN_ACC_APPROVE)
        await notify.send_text(
            bot,
            row["requester_tg_id"],
            texts.acc_requester_approved(ticket_id, lead, _url(ticket_id)),
        )
        if cards is not None:
            await cards.record_event(bot, ticket_id, texts.hist_approved(lead))

    @router.callback_query(F.data.startswith("ap:no:"))
    async def on_reject(cb: CallbackQuery, state: FSMContext) -> None:
        ticket_id = int(cb.data.rsplit(":", 1)[1])
        if await _own_pending(cb, ticket_id) is None:
            return
        await state.set_state(LeadReject.entering_reason)
        await state.update_data(reject_ticket_id=ticket_id)
        await cb.message.answer(
            texts.ACC_ASK_REJECT_REASON, reply_markup=notify.dialog_cancel_keyboard()
        )
        await cb.answer()

    @router.message(LeadReject.entering_reason, F.text)
    async def on_reject_reason(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        ticket_id = data["reject_ticket_id"]
        row = await repo.get_approval(ticket_id)
        if row is None or row["status"] != 0 or row["lead_tg_id"] != message.from_user.id:
            await state.clear()
            await message.answer(texts.ACC_ANSWERED_STALE)
            return
        reason = message.text.strip()
        lead = row["lead_name"]
        try:
            await client.answer_validation(
                row["validation_id"],
                accepted=False,
                comment=texts.acc_validation_comment_rejected(lead, reason),
            )
            await client.set_ticket_status(ticket_id, TICKET_STATUS_CLOSED)
        except GlpiError as exc:
            log.exception("access_reject_failed ticket=%s error=%s raw=%s", ticket_id, exc, exc.raw)
            await message.answer(texts.GLPI_ERROR)
            return
        await state.clear()
        await repo.set_approval_status(ticket_id, status=-1)
        await repo.set_ticket_status(ticket_id, status=TICKET_STATUS_CLOSED, active=False)
        await _add_followup_quiet(ticket_id, texts.acc_followup_rejected(lead, reason))
        await message.answer(texts.acc_decision_rejected(lead, reason))
        await notify.send_text(
            bot,
            row["requester_tg_id"],
            texts.acc_requester_rejected(ticket_id, lead, reason, _url(ticket_id)),
        )
        if cards is not None:
            await cards.record_event(
                bot, ticket_id, texts.hist_rejected(lead), status=TICKET_STATUS_CLOSED
            )

    async def _add_followup_quiet(ticket_id: int, text: str) -> None:
        """Followup into the GLPI timeline; cursor bumped so sync won't echo it."""
        try:
            followup_id = await client.add_followup(ticket_id, text)
            await repo.set_ticket_followup_cursor(ticket_id, followup_id)
        except Exception:  # noqa: BLE001 - the followup is auxiliary to the decision
            log.warning("access_followup_failed ticket=%s", ticket_id)

    return router
