"""Technician inline-button actions (feature 5).

The sync loop (feature 4) posts a card into the tech group with three buttons:

* **Take** — assign the pressing technician and move the ticket to *processing*;
* **Comment** — add a public followup;
* **Close** — add a solution (the ticket becomes *solved*).

Only linked technicians (``is_tech``) may act; anyone else gets a toast. Comment
and Close need free-text input, which is collected in the technician's **private
chat** via FSM — Telegram group privacy mode hides plain group messages from
bots, and a DM keeps the group uncluttered. The button press seeds the FSM state
for that DM and prompts there; the group card is edited when the work completes.

This router is gated by :class:`AuthMiddleware`, so ``link`` is always injected.
"""

from __future__ import annotations

import dataclasses
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import texts
from ..db.repo import LinkedUser
from ..glpi.client import GlpiClient, GlpiError
from ..services import attachments

log = logging.getLogger(__name__)


class TechAction(StatesGroup):
    commenting = State()
    closing = State()


def _card_keyboard_after_take(ticket_id: int) -> InlineKeyboardMarkup:
    """Card buttons once taken: Take is gone, Comment/Close remain."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.BTN_TECH_COMMENT, callback_data=f"ta:comment:{ticket_id}"
                ),
                InlineKeyboardButton(
                    text=texts.BTN_TECH_CLOSE, callback_data=f"ta:close:{ticket_id}"
                ),
            ]
        ]
    )


def build_tech_actions_router(client: GlpiClient) -> Router:
    router = Router(name="tech_actions")

    async def _start_dm_dialog(
        cb: CallbackQuery,
        state: FSMContext,
        bot: Bot,
        *,
        target_state: State,
        ticket_id: int,
        prompt: str,
    ) -> None:
        """Prompt for text in the technician's DM and seed that chat's FSM state."""
        tech_id = cb.from_user.id
        try:
            await bot.send_message(tech_id, prompt)
        except Exception as exc:  # noqa: BLE001 - tech hasn't opened a DM with the bot
            log.warning("tech_dm_failed tech=%s error=%s", tech_id, exc)
            await cb.answer(texts.TECH_DM_FAILED, show_alert=True)
            return
        # The callback's FSMContext is keyed to the group chat; retarget the DM.
        dm_key = dataclasses.replace(state.key, chat_id=tech_id, user_id=tech_id)
        dm_state = FSMContext(storage=state.storage, key=dm_key)
        await dm_state.set_state(target_state)
        await dm_state.update_data(
            ticket_id=ticket_id,
            card_chat_id=cb.message.chat.id if cb.message else None,
            card_message_id=cb.message.message_id if cb.message else None,
            card_text=cb.message.html_text if cb.message else None,
        )
        await cb.answer()

    # -- Take (immediate) --------------------------------------------------
    @router.callback_query(F.data.startswith("ta:take:"))
    async def on_take(cb: CallbackQuery, link: LinkedUser) -> None:
        if not link.is_tech:
            await cb.answer(texts.TECH_ONLY, show_alert=True)
            return
        ticket_id = int(cb.data.split(":")[2])
        try:
            await client.assign_ticket(ticket_id, link.glpi_users_id)
        except GlpiError as exc:
            log.error("tech_take_failed ticket=%s error=%s raw=%s", ticket_id, exc, exc.raw)
            await cb.answer(texts.GLPI_ERROR, show_alert=True)
            return
        if cb.message is not None:
            try:
                await cb.message.edit_text(
                    f"{cb.message.html_text}\n\n{texts.tech_card_taken(link.display_name)}",
                    reply_markup=_card_keyboard_after_take(ticket_id),
                )
            except Exception as exc:  # noqa: BLE001 - editing is best-effort
                log.warning("tech_card_edit_failed ticket=%s error=%s", ticket_id, exc)
        await cb.answer(texts.TECH_TAKEN_TOAST)

    # -- Comment / Close (collect text in DM) ------------------------------
    @router.callback_query(F.data.startswith("ta:comment:"))
    async def on_comment(cb: CallbackQuery, state: FSMContext, link: LinkedUser, bot: Bot) -> None:
        if not link.is_tech:
            await cb.answer(texts.TECH_ONLY, show_alert=True)
            return
        ticket_id = int(cb.data.split(":")[2])
        await _start_dm_dialog(
            cb,
            state,
            bot,
            target_state=TechAction.commenting,
            ticket_id=ticket_id,
            prompt=texts.TECH_ASK_COMMENT,
        )

    @router.callback_query(F.data.startswith("ta:close:"))
    async def on_close(cb: CallbackQuery, state: FSMContext, link: LinkedUser, bot: Bot) -> None:
        if not link.is_tech:
            await cb.answer(texts.TECH_ONLY, show_alert=True)
            return
        ticket_id = int(cb.data.split(":")[2])
        await _start_dm_dialog(
            cb,
            state,
            bot,
            target_state=TechAction.closing,
            ticket_id=ticket_id,
            prompt=texts.TECH_ASK_SOLUTION,
        )

    # -- DM text handlers --------------------------------------------------
    @router.message(TechAction.commenting, F.text)
    async def on_comment_text(message: Message, state: FSMContext, link: LinkedUser) -> None:
        # GLPI records the followup as the service account; name it in the body.
        content = f"{link.display_name}:\n{message.text.strip()}"
        await _post_comment(message, state, content=content)

    @router.message(TechAction.commenting, F.photo | F.document)
    async def on_comment_file(
        message: Message, state: FSMContext, link: LinkedUser, bot: Bot
    ) -> None:
        pending = attachments.extract(message)
        if pending is None:
            await message.answer(texts.TECH_EXPECT_TEXT)
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
            log.error("tech_attach_failed ticket=%s error=%s", ticket_id, exc)
            await message.answer(texts.GLPI_ERROR)
            return
        await _post_comment(message, state, content=f"{link.display_name}:\n{caption}")

    async def _post_comment(message: Message, state: FSMContext, *, content: str) -> None:
        data = await state.get_data()
        ticket_id = data["ticket_id"]
        try:
            await client.add_followup(ticket_id, content)
        except GlpiError as exc:
            log.error("tech_comment_failed ticket=%s error=%s raw=%s", ticket_id, exc, exc.raw)
            await message.answer(texts.GLPI_ERROR)
            return
        await state.clear()
        await message.answer(texts.TECH_COMMENT_DONE)

    @router.message(TechAction.closing, F.text)
    async def on_solution_text(
        message: Message, state: FSMContext, link: LinkedUser, bot: Bot
    ) -> None:
        data = await state.get_data()
        ticket_id = data["ticket_id"]
        try:
            await client.add_solution(ticket_id, message.text.strip())
        except GlpiError as exc:
            log.error("tech_close_failed ticket=%s error=%s raw=%s", ticket_id, exc, exc.raw)
            await message.answer(texts.GLPI_ERROR)
            return
        await state.clear()
        await message.answer(texts.TECH_SOLUTION_DONE)
        await _mark_card_solved(bot, data, link.display_name)

    @router.message(StateFilter(TechAction), Command("cancel"))
    async def on_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(texts.TECH_ACTION_CANCELLED)

    @router.message(StateFilter(TechAction))
    async def on_expect_text(message: Message) -> None:
        await message.answer(texts.TECH_EXPECT_TEXT)

    async def _mark_card_solved(bot: Bot, data: dict, name: str) -> None:
        """Edit the group card to show the ticket was solved (best-effort)."""
        chat_id, message_id, text = (
            data.get("card_chat_id"),
            data.get("card_message_id"),
            data.get("card_text"),
        )
        if not (chat_id and message_id and text):
            return
        try:
            await bot.edit_message_text(
                f"{text}\n\n{texts.tech_card_solved(name)}",
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=None,
            )
        except Exception as exc:  # noqa: BLE001 - editing is best-effort
            log.warning("tech_card_solve_edit_failed error=%s", exc)

    return router
