"""Private technician dialog: existing ticket -> lead -> explicit confirmation."""

from __future__ import annotations

import dataclasses
import secrets

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import texts
from ..glpi.client import GlpiError
from ..services import notify
from ..services.access_target import classify_access_target
from ..services.intake_snapshot import refresh_intake_snapshot
from ..services.tech_approval import ApprovalUnavailable


class TechApproval(StatesGroup):
    service = State()
    lead = State()
    confirm = State()


def build_tech_approval_router(service, lead_directory):
    router = Router(name="tech_approval")
    router.message.filter(F.chat.type == "private")
    repo = service.repo

    def keyboard(data, *, leads=None, confirm=False):
        nonce = data["nonce"]
        rows = []
        if leads is not None:
            rows.extend(
                [
                    [
                        InlineKeyboardButton(
                            text=x.name, callback_data=f"tap:lead:{nonce}:{x.glpi_id}"
                        )
                    ]
                    for x in leads
                ]
            )
        if confirm:
            rows.append(
                [InlineKeyboardButton(text=texts.BTN_ACC_SEND, callback_data=f"tap:send:{nonce}")]
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        text=texts.BTN_ACC_OTHER_LEAD, callback_data=f"tap:other:{nonce}"
                    )
                ]
            )
        rows.append(
            [InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data=f"tap:cancel:{nonce}")]
        )
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def leads_for(data):
        return [
            x
            for x in await lead_directory.get()
            if x.tg_id
            and x.glpi_id != data["requester_glpi_id"]
            and x.tg_id != data["requester_tg_id"]
        ]

    def confirmation(data):
        text = texts.tech_approval_confirm(data["ticket_id"], data["request"], data["lead_name"])
        if data["service"] == "claude":
            text += "\n\n" + (
                texts.CLAUDE_CONFIRM_COVERED
                if data["lead_glpi_id"] in service.claude.approver_ids
                else texts.CLAUDE_CONFIRM_EXTRA
            )
        return text

    async def show_confirm(target, state, lead, *, edit=False):
        await state.update_data(
            lead_glpi_id=lead.glpi_id, lead_tg_id=lead.tg_id, lead_name=lead.name
        )
        await state.set_state(TechApproval.confirm)
        data = await state.get_data()
        text = confirmation(data)
        if edit:
            await notify.safe_edit(target, text, reply_markup=keyboard(data, confirm=True))
        else:
            await target.answer(text, reply_markup=keyboard(data, confirm=True))

    async def pick_lead(target, state, *, edit=False, mapped=True):
        data = await state.get_data()
        leads = await leads_for(data)
        if not leads:
            raise ApprovalUnavailable(texts.ACC_NO_LEADS)
        suggested = await repo.get_user_lead(data["requester_glpi_id"]) if mapped else None
        lead = next((x for x in leads if x.glpi_id == suggested), None)
        if lead:
            await show_confirm(target, state, lead, edit=edit)
            return
        await state.set_state(TechApproval.lead)
        text = texts.tech_approval_pick(data["ticket_id"])
        if edit:
            await notify.safe_edit(target, text, reply_markup=keyboard(data, leads=leads))
        else:
            await target.answer(text, reply_markup=keyboard(data, leads=leads))

    async def begin(
        ticket_id,
        target,
        state,
        link,
        bot,
        *,
        callback=False,
        review_version=None,
    ):
        if not link.is_tech:
            raise ApprovalUnavailable(texts.TECH_ONLY)
        cached = await repo.get_ticket_intake(ticket_id)
        if review_version is not None and (cached is None or cached["version"] != review_version):
            if service.cards:
                await service.cards.record_event(bot, ticket_id, None)
            raise ApprovalUnavailable(texts.INTAKE_REVIEW_STALE)
        async with repo.ticket_action_lock(ticket_id):
            live = await service.client.get_ticket(ticket_id)
            intake = await refresh_intake_snapshot(repo, live) if live else None
        if review_version is not None and (intake is None or intake["version"] != review_version):
            if service.cards:
                await service.cards.record_event(bot, ticket_id, None)
            raise ApprovalUnavailable(texts.INTAKE_REVIEW_STALE)
        data = await service.prepare(ticket_id)
        if intake is not None:
            data["intake_review_version"] = intake["version"]
        data.update(nonce=secrets.token_hex(6))
        # Retarget group callbacks to the tech's personal dialog. Never erase an
        # unrelated comment/rejection draft to start another action.
        key = dataclasses.replace(state.key, chat_id=link.tg_id, user_id=link.tg_id)
        dm = FSMContext(storage=state.storage, key=key)
        if await dm.get_state() is not None:
            raise ApprovalUnavailable(texts.TECH_APPROVAL_BUSY)
        if data["service"] == "unclear":
            next_state = TechApproval.service
            text, markup = texts.CLAUDE_SERVICE_ASK, keyboard(data)
        else:
            leads = await leads_for(data)
            if not leads:
                raise ApprovalUnavailable(texts.ACC_NO_LEADS)
            mapped = await repo.get_user_lead(data["requester_glpi_id"])
            lead = next((x for x in leads if x.glpi_id == mapped), None)
            if lead:
                data.update(lead_glpi_id=lead.glpi_id, lead_tg_id=lead.tg_id, lead_name=lead.name)
                next_state = TechApproval.confirm
                text, markup = confirmation(data), keyboard(data, confirm=True)
            else:
                next_state = TechApproval.lead
                text, markup = texts.tech_approval_pick(ticket_id), keyboard(data, leads=leads)
        try:
            await bot.send_message(link.tg_id, text, reply_markup=markup)
        except Exception as exc:
            raise ApprovalUnavailable(texts.TECH_DM_FAILED) from exc
        await dm.set_data(data)
        await dm.set_state(next_state)
        if callback:
            await target.answer(texts.TECH_APPROVAL_DM)

    @router.callback_query(F.data.startswith("ta:approval:") | F.data.startswith("ir:approval:"))
    async def start(cb: CallbackQuery, state: FSMContext, link, bot):
        try:
            parts = cb.data.split(":")
            intake_action = parts[0] == "ir"
            if len(parts) != (4 if intake_action else 3):
                raise ApprovalUnavailable(texts.INTAKE_REVIEW_STALE)
            await begin(
                int(parts[2]),
                cb,
                state,
                link,
                bot,
                callback=True,
                review_version=int(parts[3]) if intake_action else None,
            )
        except (ValueError, IndexError):
            await cb.answer(texts.INTAKE_REVIEW_STALE, show_alert=True)
        except (ApprovalUnavailable, GlpiError) as exc:
            await cb.answer(
                str(exc) if isinstance(exc, ApprovalUnavailable) else texts.GLPI_ACTION_ERROR,
                show_alert=True,
            )

    @router.message(Command("approval"))
    async def command(message: Message, state: FSMContext, link, bot):
        if not link.is_tech:
            await message.answer(texts.TECH_ONLY)
            return
        args = (message.text or "").split()
        if len(args) != 2 or not args[1].isdigit() or int(args[1]) <= 0:
            await message.answer(texts.TECH_APPROVAL_USAGE)
            return
        try:
            await begin(int(args[1]), message, state, link, bot)
        except (ApprovalUnavailable, GlpiError) as exc:
            await message.answer(
                str(exc) if isinstance(exc, ApprovalUnavailable) else texts.GLPI_ACTION_ERROR
            )

    @router.message(StateFilter(TechApproval), Command("cancel"))
    async def cancel(message: Message, state: FSMContext):
        await state.clear()
        await message.answer(texts.DIALOG_CANCELLED)

    @router.message(TechApproval.service, F.text)
    async def clarify(message: Message, state: FSMContext, link):
        if not link.is_tech:
            await state.clear()
            await message.answer(texts.TECH_ONLY)
            return
        target = message.text.strip()
        category = classify_access_target(target)
        if (
            not target
            or category == "unclear"
            or target.casefold() in {"другой", "другое", "other", "не знаю", "хз"}
        ):
            await message.answer(texts.CLAUDE_SERVICE_ASK)
            return
        data = await state.get_data()
        await state.update_data(
            service=category,
            request=texts.acc_service_clarified(data["request"], target[:100]),
        )
        try:
            await pick_lead(message, state)
        except (ApprovalUnavailable, GlpiError) as exc:
            await state.clear()
            await message.answer(
                str(exc) if isinstance(exc, ApprovalUnavailable) else texts.GLPI_ACTION_ERROR
            )

    @router.callback_query(F.data.startswith("tap:"))
    async def action(cb: CallbackQuery, state: FSMContext, link, bot):
        if not link.is_tech:
            await cb.answer(texts.TECH_ONLY, show_alert=True)
            return
        data = await state.get_data()
        parts = cb.data.split(":")
        current = await state.get_state()
        if (
            not isinstance(cb.message, Message)
            or cb.message.chat.id != link.tg_id
            or len(parts) < 3
            or parts[2] != data.get("nonce")
            or current not in {s.state for s in TechApproval.__all_states__}
        ):
            await cb.answer(texts.STALE_BUTTON, show_alert=True)
            return
        action = parts[1]
        acknowledged = False
        try:
            if action == "cancel":
                await state.clear()
                await notify.safe_edit(cb, texts.DIALOG_CANCELLED)
            elif action == "other":
                await pick_lead(cb, state, edit=True, mapped=False)
            elif action == "lead" and current == TechApproval.lead.state and len(parts) == 4:
                lead = next((x for x in await leads_for(data) if str(x.glpi_id) == parts[3]), None)
                if lead is None:
                    raise ApprovalUnavailable(texts.STALE_BUTTON)
                await show_confirm(cb, state, lead, edit=True)
            elif action == "send" and current == TechApproval.confirm.state:
                # Also revalidate membership in the configured lead directory.
                if not any(
                    x.glpi_id == data["lead_glpi_id"] and x.tg_id == data["lead_tg_id"]
                    for x in await leads_for(data)
                ):
                    raise ApprovalUnavailable(texts.ACC_NO_LEADS)
                await cb.answer()
                acknowledged = True
                result = await service.submit(bot, data, link)
                await state.clear()
                text = (
                    texts.tech_approval_sent(
                        data["ticket_id"], data["lead_name"], service.url(data["ticket_id"])
                    )
                    if result == "done"
                    else texts.TECH_APPROVAL_CANCELLED
                    if result == "cancelled"
                    else texts.GLPI_ACTION_ERROR
                    if result == "failed"
                    else texts.TECH_APPROVAL_PENDING
                )
                await notify.safe_edit(cb, text)
                return
            else:
                raise ApprovalUnavailable(texts.STALE_BUTTON)
            await cb.answer()
        except (ApprovalUnavailable, GlpiError) as exc:
            error = str(exc) if isinstance(exc, ApprovalUnavailable) else texts.GLPI_ACTION_ERROR
            if acknowledged:
                await notify.safe_edit(cb, error)
                await state.clear()
            else:
                await cb.answer(error, show_alert=True)

    return router
