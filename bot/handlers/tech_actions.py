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
from ..db.repo import LinkedUser, Repo
from ..glpi.client import (
    TICKET_STATUS_PROCESSING_ASSIGNED,
    TICKET_STATUS_SOLVED,
    GlpiClient,
    GlpiError,
)
from ..services import attachments, notify
from ..services.cards import CardService

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


def build_tech_actions_router(
    client: GlpiClient,
    *,
    tech_group_chat_id: int | None = None,
    cards: CardService | None = None,
    repo: Repo | None = None,
) -> Router:
    router = Router(name="tech_actions")
    # FSM dialogs live in private chats only; in groups the bot reacts solely
    # to its inline buttons (callbacks are not affected by this filter).
    router.message.filter(F.chat.type == "private")

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
        # Old callbacks carry an InaccessibleMessage (no text/edit) — isinstance,
        # not an is-None check, is the real accessibility test.
        card = cb.message if isinstance(cb.message, Message) else None
        await dm_state.update_data(
            ticket_id=ticket_id,
            card_chat_id=card.chat.id if card else None,
            card_message_id=card.message_id if card else None,
            card_text=card.html_text if card else None,
        )
        await cb.answer()

    # -- Take (immediate) --------------------------------------------------
    @router.callback_query(F.data.startswith("ta:take:"))
    async def on_take(cb: CallbackQuery, link: LinkedUser, bot: Bot) -> None:
        if not link.is_tech:
            await cb.answer(texts.TECH_ONLY, show_alert=True)
            return
        ticket_id = int(cb.data.split(":")[2])
        try:
            await client.assign_ticket(ticket_id, link.glpi_users_id)
        except GlpiError as exc:
            log.exception("tech_take_failed ticket=%s error=%s raw=%s", ticket_id, exc, exc.raw)
            await cb.answer(texts.GLPI_ERROR, show_alert=True)
            return
        # History-only event: taking a ticket is visible in the card edit and
        # needs no group ping (reply pings are reserved for requester comments
        # and closures — events that demand the team's attention).
        handled = cards is not None and await cards.record_event(
            bot,
            ticket_id,
            texts.hist_taken(link.display_name),
            status=TICKET_STATUS_PROCESSING_ASSIGNED,
            taken_by=link.display_name,
        )
        if not handled:
            # No living card for this ticket (pre-feature) -> legacy in-place edit.
            card = cb.message if isinstance(cb.message, Message) else None
            if card is not None:
                try:
                    await card.edit_text(
                        f"{card.html_text}\n\n{texts.tech_card_taken(link.display_name)}",
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
            prompt=texts.tech_ask_comment(ticket_id),
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
            prompt=texts.tech_ask_solution(ticket_id),
        )

    @router.callback_query(F.data.startswith("ta:wait:"))
    async def on_wait(cb: CallbackQuery) -> None:
        # Passive indicator on a solved card — nothing to do, just explain.
        await cb.answer(texts.WAITING_TOAST, show_alert=True)

    # /cancel must precede the state text handlers, or the comment/solution
    # steps would swallow it as content.
    @router.message(StateFilter(TechAction), Command("cancel"))
    async def on_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(texts.TECH_ACTION_CANCELLED)

    # -- DM text handlers --------------------------------------------------
    @router.message(TechAction.commenting, F.text)
    async def on_comment_text(
        message: Message, state: FSMContext, link: LinkedUser, bot: Bot
    ) -> None:
        # GLPI records the followup as the service account; name it in the body.
        content = f"{link.display_name}:\n{message.text.strip()}"
        await _post_comment(message, state, bot, link, content=content)

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
            log.exception("tech_attach_failed ticket=%s error=%s", ticket_id, exc)
            await message.answer(texts.GLPI_ERROR)
            return
        await _post_comment(message, state, bot, link, content=f"{link.display_name}:\n{caption}")

    async def _post_comment(
        message: Message, state: FSMContext, bot: Bot, link: LinkedUser, *, content: str
    ) -> None:
        data = await state.get_data()
        ticket_id = data["ticket_id"]
        try:
            followup_id = await client.add_followup(ticket_id, content)
        except GlpiError as exc:
            log.exception("tech_comment_failed ticket=%s error=%s raw=%s", ticket_id, exc, exc.raw)
            await message.answer(texts.GLPI_ERROR)
            return
        await state.clear()
        await message.answer(texts.TECH_COMMENT_DONE)
        if cards is not None:
            # The team's own comment: history line only, no self-ping.
            await cards.record_event(
                bot,
                ticket_id,
                texts.hist_comment(link.display_name),
                followup_id=followup_id,
            )

    @router.message(TechAction.closing, F.text)
    async def on_solution_text(
        message: Message, state: FSMContext, link: LinkedUser, bot: Bot
    ) -> None:
        data = await state.get_data()
        ticket_id = data["ticket_id"]
        solution = message.text.strip()
        # GLPI records the solution as the service account; name the tech in it.
        try:
            await client.add_solution(ticket_id, f"{link.display_name}:\n{solution}")
        except GlpiError as exc:
            log.exception("tech_close_failed ticket=%s error=%s raw=%s", ticket_id, exc, exc.raw)
            await message.answer(texts.GLPI_ERROR)
            return
        await state.clear()
        await message.answer(texts.TECH_SOLUTION_DONE)
        # ITIL cycle: the ticket is SOLVED (not closed). Ask the REQUESTER to
        # confirm or return it to work, right now with the actual solution text;
        # bump the tracked status so the sync loop stays silent, and remember
        # who solved it for the return-to-work ping.
        if repo is not None:
            await repo.set_solver(ticket_id, tg_id=message.from_user.id, name=link.display_name)
            tracked = await repo.get_tracked_ticket(ticket_id)
            if tracked is not None:
                await notify.send_text(
                    bot,
                    tracked.requester_tg_id,
                    texts.solution_proposed(
                        ticket_id=ticket_id, tech_name=link.display_name, solution=solution
                    ),
                    reply_markup=notify.solution_confirm_keyboard(ticket_id),
                )
                await repo.set_ticket_status(ticket_id, status=TICKET_STATUS_SOLVED, active=True)
        # The solution text (with confirm/return buttons) goes ONLY to the
        # requester — the group gets just a history line on the card, no
        # solution body and no ping (it doesn't need the team's attention).
        handled = cards is not None and await cards.record_event(
            bot,
            ticket_id,
            texts.hist_solved(link.display_name),
            status=TICKET_STATUS_SOLVED,
        )
        if not handled:
            # No living card (pre-feature ticket) -> legacy in-place edit only.
            await _mark_card_solved(bot, data, link.display_name)

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
