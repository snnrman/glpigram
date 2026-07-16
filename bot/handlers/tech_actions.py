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
import re

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
from .new_ticket import main_menu_keyboard

log = logging.getLogger(__name__)


class TechAction(StatesGroup):
    commenting = State()
    closing = State()


_TAG_RE = re.compile(r"<[^>]+>")


def _is_reminder(message: Message) -> bool:
    """The message is an unassigned-tickets reminder (matched on its header).

    Compares the plain message text against the tag-stripped header, so it works
    regardless of how Telegram splits the bold formatting into entities.
    """
    header = _TAG_RE.sub("", texts.UNASSIGNED_HEADER)
    return (message.text or "").startswith(header)


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


async def _drop_prompt_buttons(cb: CallbackQuery) -> None:
    """Best-effort removal of the prompt's inline keyboard after a cancel."""
    if isinstance(cb.message, Message):
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception as exc:  # noqa: BLE001 - the prompt may be old/deleted
            log.debug("cancel_markup_cleanup_failed error=%s", exc)


def build_tech_actions_router(
    client: GlpiClient,
    *,
    tech_group_chat_id: int | None = None,
    cards: CardService | None = None,
    repo: Repo | None = None,
    ticket_front_base: str | None = None,
) -> Router:
    router = Router(name="tech_actions")
    # FSM dialogs live in private chats only; in groups the bot reacts solely
    # to its inline buttons (callbacks are not affected by this filter).
    router.message.filter(F.chat.type == "private")

    def _ticket_url(ticket_id: int) -> str | None:
        if not ticket_front_base:
            return None
        return f"{ticket_front_base}/front/ticket.form.php?id={ticket_id}"

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
            await bot.send_message(tech_id, prompt, reply_markup=notify.dialog_cancel_keyboard())
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
        # One handler for both entry points — the new-ticket card and the
        # unassigned-tickets reminder both fire `ta:take:{id}`, so taking a
        # ticket gives identical feedback wherever the button lives.
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
        name = link.display_name
        msg = cb.message if isinstance(cb.message, Message) else None
        # (2) Reflect the take in the ticket's living card (status + history),
        # addressed by its stored message id — so it updates even when the take
        # came from the reminder. History-only: no group ping (reserved for
        # requester comments/closures).
        handled = cards is not None and await cards.record_event(
            bot,
            ticket_id,
            texts.hist_taken(name),
            status=TICKET_STATUS_PROCESSING_ASSIGNED,
            taken_by=name,
        )
        # (1) Give feedback on the message the button was actually pressed under:
        # an unassigned reminder -> mark that ticket taken there (show the taker,
        # drop its button); a legacy card with no living-card row -> edit it in
        # place. A living card pressed directly is already covered above.
        if msg is not None and _is_reminder(msg):
            await notify.mark_unassigned_taken(msg, ticket_id, name)
        elif not handled and msg is not None:
            try:
                await msg.edit_text(
                    f"{msg.html_text}\n\n{texts.tech_card_taken(name)}",
                    reply_markup=_card_keyboard_after_take(ticket_id),
                )
            except Exception as exc:  # noqa: BLE001 - editing is best-effort
                log.warning("tech_card_edit_failed ticket=%s error=%s", ticket_id, exc)
        # (3) Notify the requester that work has started (naming the tech), and
        # (4) advance the tracked status so the sync loop neither re-notifies the
        # requester with a generic status change nor lists the ticket as
        # unassigned again (it is now assigned in GLPI too).
        if repo is not None:
            tracked = await repo.get_tracked_ticket(ticket_id)
            if tracked is not None:
                await notify.notify_taken(
                    bot,
                    tracked.requester_tg_id,
                    ticket_id=ticket_id,
                    tech_name=name,
                    url=_ticket_url(ticket_id),
                )
                try:
                    await repo.set_ticket_status(
                        ticket_id, status=TICKET_STATUS_PROCESSING_ASSIGNED, active=True
                    )
                except Exception:  # noqa: BLE001 - dedup bookkeeping must not fail the take
                    log.exception("tech_take_status_bump_failed ticket=%s", ticket_id)
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

    # -- Handoff: reassign the ticket to another technician -----------------
    def _handoff_keyboard(ticket_id: int, techs) -> InlineKeyboardMarkup:
        rows = [
            [
                InlineKeyboardButton(
                    text=t.display_name, callback_data=f"ta:hto:{ticket_id}:{t.glpi_users_id}"
                )
            ]
            for t in techs
        ]
        rows.append([InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="ta:hx")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @router.callback_query(F.data.startswith("ta:handoff:"))
    async def on_handoff(cb: CallbackQuery, link: LinkedUser, bot: Bot) -> None:
        if not link.is_tech:
            await cb.answer(texts.TECH_ONLY, show_alert=True)
            return
        if repo is None:
            await cb.answer(texts.GLPI_ERROR, show_alert=True)
            return
        ticket_id = int(cb.data.split(":")[2])
        techs = await repo.list_techs()
        if not techs:
            await cb.answer(texts.HANDOFF_NO_TECHS, show_alert=True)
            return
        # The pick dialog lives in the pressing tech's DM, not in the group.
        try:
            await bot.send_message(
                cb.from_user.id,
                texts.handoff_pick(ticket_id),
                reply_markup=_handoff_keyboard(ticket_id, techs),
            )
        except Exception as exc:  # noqa: BLE001 - tech hasn't opened a DM with the bot
            log.warning("handoff_dm_failed tech=%s error=%s", cb.from_user.id, exc)
            await cb.answer(texts.TECH_DM_FAILED, show_alert=True)
            return
        await cb.answer()

    @router.callback_query(F.data == "ta:hx")
    async def on_handoff_cancel(cb: CallbackQuery) -> None:
        card = cb.message if isinstance(cb.message, Message) else None
        if card is not None:
            try:
                await card.edit_text(texts.HANDOFF_CANCELLED)
            except Exception as exc:  # noqa: BLE001 - editing is best-effort
                log.warning("handoff_cancel_edit_failed error=%s", exc)
        await cb.answer()

    @router.callback_query(F.data.startswith("ta:hto:"))
    async def on_handoff_pick(cb: CallbackQuery, link: LinkedUser, bot: Bot) -> None:
        if not link.is_tech:
            await cb.answer(texts.TECH_ONLY, show_alert=True)
            return
        _, _, tid, uid = cb.data.split(":")
        ticket_id, new_glpi_id = int(tid), int(uid)
        target = await repo.get_by_glpi(new_glpi_id) if repo is not None else None
        if target is None:
            await cb.answer(texts.HANDOFF_TARGET_GONE, show_alert=True)
            return
        try:
            prev_names = await client.get_ticket_assignees(ticket_id)
        except GlpiError:
            prev_names = []
        try:
            await client.reassign_ticket(ticket_id, new_glpi_id)
        except GlpiError as exc:
            log.exception("handoff_failed ticket=%s error=%s raw=%s", ticket_id, exc, exc.raw)
            await cb.answer(texts.GLPI_ERROR, show_alert=True)
            return
        ticket = None
        try:
            ticket = await client.get_ticket(ticket_id)
        except GlpiError:
            pass
        # New executor's DM (skip a self-handoff — the presser knows already).
        if target.tg_id != cb.from_user.id:
            await notify.send_text(
                bot,
                target.tg_id,
                texts.handoff_to_new(
                    ticket_id,
                    ticket.name if ticket else "",
                    ticket.urgency or None if ticket else None,
                ),
            )
        # Requester's DM (bot-created tickets are tracked).
        tracked = await repo.get_tracked_ticket(ticket_id)
        if tracked is not None:
            await notify.send_text(
                bot,
                tracked.requester_tg_id,
                texts.handoff_to_requester(ticket_id, target.display_name),
            )
        # Card: history line + assignee in the header (no group ping).
        if cards is not None:
            await cards.record_event(
                bot,
                ticket_id,
                texts.hist_handoff(", ".join(prev_names) or None, target.display_name),
                status=TICKET_STATUS_PROCESSING_ASSIGNED,
                taken_by=target.display_name,
            )
        pick_msg = cb.message if isinstance(cb.message, Message) else None
        if pick_msg is not None:
            try:
                await pick_msg.edit_text(texts.handoff_done(ticket_id, target.display_name))
            except Exception as exc:  # noqa: BLE001 - editing is best-effort
                log.warning("handoff_done_edit_failed error=%s", exc)
        await cb.answer()

    # /cancel must precede the state text handlers, or the comment/solution
    # steps would swallow it as content.
    @router.message(StateFilter(TechAction), Command("cancel"))
    async def on_cancel(message: Message, state: FSMContext, link: LinkedUser) -> None:
        await state.clear()
        await message.answer(
            texts.DIALOG_CANCELLED, reply_markup=main_menu_keyboard(is_tech=link.is_tech)
        )

    @router.callback_query(StateFilter(TechAction), F.data == "dlg:cancel")
    async def on_cancel_button(cb: CallbackQuery, state: FSMContext, link: LinkedUser) -> None:
        await state.clear()
        await _drop_prompt_buttons(cb)
        if isinstance(cb.message, Message):
            await cb.message.answer(
                texts.DIALOG_CANCELLED, reply_markup=main_menu_keyboard(is_tech=link.is_tech)
            )
        await cb.answer()

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
        # The file is linked to the ticket, so the sync loop (which only forwards
        # followup-linked docs) won't carry it to the requester — send it here.
        if repo is not None:
            tracked = await repo.get_tracked_ticket(ticket_id)
            if tracked is not None:
                await notify.send_attachments(
                    bot,
                    tracked.requester_tg_id,
                    [(pending.filename, pending.mime, content)],
                )

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
