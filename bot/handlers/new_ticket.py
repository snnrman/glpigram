"""/new — FSM dialog that creates a GLPI ticket.

Flow (CLAUDE.md features 1 & 6):
    category -> urgency -> title -> description -> attachments -> confirm ->
    create ticket -> upload attachments.

Optional photos/documents are collected in the ``attaching`` step and uploaded
to the ticket (as GLPI Documents) after it is created.
"""

from __future__ import annotations

import logging
import time

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from .. import texts
from ..cache import TTLValue
from ..db.repo import LinkedUser, Repo
from ..glpi.client import (
    TICKET_STATUS_NEW,
    URGENCY_HIGH,
    URGENCY_LOW,
    URGENCY_MEDIUM,
    GlpiClient,
    GlpiError,
)
from ..glpi.models import ITILCategory
from ..schedule import WorkSchedule
from ..services import attachments, notify

log = logging.getLogger(__name__)

MAX_TITLE_LEN = 250

_URGENCY_CHOICES = (
    (URGENCY_HIGH, texts.URGENCY_HIGH_LABEL),
    (URGENCY_MEDIUM, texts.URGENCY_MEDIUM_LABEL),
    (URGENCY_LOW, texts.URGENCY_LOW_LABEL),
)


class NewTicket(StatesGroup):
    choosing_category = State()
    choosing_urgency = State()
    entering_title = State()
    entering_description = State()
    attaching = State()
    confirming = State()


def _categories_keyboard(categories: list[ITILCategory]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=c.completename[:60], callback_data=f"nt:cat:{c.id}")]
        for c in categories
    ]
    rows.append([InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="nt:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _urgency_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"nt:urg:{value}")]
        for value, label in _URGENCY_CHOICES
    ]
    rows.append([InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="nt:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=texts.BTN_CONFIRM, callback_data="nt:confirm"),
                InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="nt:cancel"),
            ]
        ]
    )


def _attach_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=texts.BTN_ATTACH_DONE, callback_data="nt:attach_done"),
                InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="nt:attach_cancel"),
            ]
        ]
    )


def _attach_cancel_keyboard() -> InlineKeyboardMarkup:
    """Confirm aborting the whole ticket from the attachments step."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=texts.BTN_ATTACH_CANCEL_YES, callback_data="nt:cancel"),
                InlineKeyboardButton(
                    text=texts.BTN_ATTACH_CANCEL_NO, callback_data="nt:attach_back"
                ),
            ]
        ]
    )


def _offer_keyboard() -> InlineKeyboardMarkup:
    """Inline prompt shown for free text sent outside a dialog."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=texts.BTN_CREATE_TICKET, callback_data="nt:offer:yes"),
                InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="nt:offer:no"),
            ]
        ]
    )


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Persistent reply keyboard shown after /start and every finished dialog."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=texts.BTN_NEW_TICKET),
                KeyboardButton(text=texts.BTN_MY_TICKETS),
            ]
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def build_new_ticket_router(
    client: GlpiClient,
    category_cache: TTLValue[list[ITILCategory]],
    repo: Repo,
    *,
    ticket_front_base: str | None = None,
    schedule: WorkSchedule | None = None,
    quiet_min_urgency: int = 4,
) -> Router:
    """Wire the /new dialog with its GLPI dependencies (closure-injected)."""
    router = Router(name="new_ticket")

    def _quiet_notice(urgency: int) -> str | None:
        """Off-hours note for the requester after creation (None in work hours)."""
        if schedule is None:
            return None
        now = schedule.now()
        if schedule.is_working(now):
            return None
        if urgency >= quiet_min_urgency:
            return texts.QUIET_URGENT_NOTICE
        return texts.quiet_hours_notice(schedule.next_open(now), now)

    def _ticket_url(ticket_id: int) -> str | None:
        if not ticket_front_base:
            return None
        return f"{ticket_front_base}/front/ticket.form.php?id={ticket_id}"

    async def _open_category_step(message: Message, state: FSMContext) -> None:
        """Load categories and enter the choosing_category state, or report failure.

        Any ``description`` already stored in the FSM data is preserved so the
        free-text flow can pre-fill it before this step.
        """
        try:
            categories = await category_cache.get()
        except GlpiError as exc:
            log.warning("new_ticket_categories_failed error=%s", exc)
            await state.clear()
            await message.answer(texts.NEW_NO_CATEGORIES, reply_markup=main_menu_keyboard())
            return
        if not categories:
            await state.clear()
            await message.answer(texts.NEW_NO_CATEGORIES, reply_markup=main_menu_keyboard())
            return
        await state.set_state(NewTicket.choosing_category)
        await message.answer(
            texts.NEW_CHOOSE_CATEGORY, reply_markup=_categories_keyboard(categories)
        )

    @router.message(Command("new"))
    async def cmd_new(message: Message, state: FSMContext) -> None:
        await state.clear()
        await _open_category_step(message, state)

    # Persistent menu buttons work from any state (they restart / route the flow).
    @router.message(F.text == texts.BTN_NEW_TICKET)
    async def btn_new_ticket(message: Message, state: FSMContext) -> None:
        await state.clear()
        await _open_category_step(message, state)

    # /cancel must be registered BEFORE the state text handlers, or the title/
    # description steps would swallow it as content (found by the FSM tests).
    @router.message(StateFilter(NewTicket), Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(texts.NEW_CANCELLED, reply_markup=main_menu_keyboard())

    # Steps that wait for a button press: don't ignore typed text silently.
    @router.message(
        StateFilter(NewTicket.choosing_category, NewTicket.choosing_urgency, NewTicket.confirming)
    )
    async def on_button_step_message(message: Message) -> None:
        await message.answer(texts.USE_BUTTONS)

    @router.callback_query(NewTicket.choosing_category, F.data.startswith("nt:cat:"))
    async def on_category(cb: CallbackQuery, state: FSMContext) -> None:
        cat_id = int(cb.data.rsplit(":", 1)[1])
        # The cache may have expired since the keyboard was shown; the id is
        # already in the callback data, the name is display-only -> fall back
        # instead of letting GlpiError kill the dialog mid-step.
        try:
            categories = await category_cache.get()
        except GlpiError as exc:
            log.warning("new_ticket_category_name_failed error=%s", exc)
            categories = []
        name = next((c.completename for c in categories if c.id == cat_id), str(cat_id))
        await state.update_data(category_id=cat_id, category_name=name)
        await state.set_state(NewTicket.choosing_urgency)
        await cb.message.edit_text(texts.NEW_CHOOSE_URGENCY, reply_markup=_urgency_keyboard())
        await cb.answer()

    @router.callback_query(NewTicket.choosing_urgency, F.data.startswith("nt:urg:"))
    async def on_urgency(cb: CallbackQuery, state: FSMContext) -> None:
        urgency = int(cb.data.rsplit(":", 1)[1])
        await state.update_data(urgency=urgency)
        await state.set_state(NewTicket.entering_title)
        await cb.message.edit_text(texts.NEW_ENTER_TITLE)
        await cb.answer()

    @router.message(NewTicket.entering_title, F.text)
    async def on_title(message: Message, state: FSMContext) -> None:
        title = message.text.strip()
        if len(title) > MAX_TITLE_LEN:
            await message.answer(texts.NEW_TITLE_TOO_LONG)
            return
        await state.update_data(title=title)
        data = await state.get_data()
        if data.get("description"):
            # Description was pre-filled (free-text flow) -> skip to attachments.
            await _open_attach_step(message, state)
            return
        await state.set_state(NewTicket.entering_description)
        await message.answer(texts.NEW_ENTER_DESCRIPTION)

    @router.message(NewTicket.entering_title)
    async def on_title_not_text(message: Message, state: FSMContext) -> None:
        await message.answer(texts.NEW_EXPECT_TEXT)

    @router.message(NewTicket.entering_description, F.text)
    async def on_description(message: Message, state: FSMContext) -> None:
        await state.update_data(description=message.text.strip())
        await _open_attach_step(message, state)

    @router.message(NewTicket.entering_description)
    async def on_description_not_text(message: Message, state: FSMContext) -> None:
        await message.answer(texts.NEW_EXPECT_TEXT)

    async def _open_attach_step(message: Message, state: FSMContext) -> None:
        await state.update_data(attachments=[])
        await state.set_state(NewTicket.attaching)
        await message.answer(texts.NEW_ATTACH_PROMPT, reply_markup=_attach_keyboard())

    async def _confirm_text(state: FSMContext) -> str:
        """Move to the confirming state and render the summary."""
        data = await state.get_data()
        await state.set_state(NewTicket.confirming)
        return texts.confirm_summary(
            data["category_name"],
            data["urgency"],
            data["title"],
            data["description"],
            attachments=len(data.get("attachments", [])),
        )

    @router.message(NewTicket.attaching, F.photo | F.document)
    async def on_attachment(message: Message, state: FSMContext) -> None:
        pending = attachments.extract(message)
        # Every reply in this step keeps the ✅/➡️ keyboard so the dialog is
        # always finishable — that was the bug: confirmations had no button.
        if pending is None:
            await message.answer(texts.ATTACH_UNSUPPORTED, reply_markup=_attach_keyboard())
            return
        if attachments.too_large(pending):
            await message.answer(texts.ATTACH_TOO_LARGE, reply_markup=_attach_keyboard())
            return
        data = await state.get_data()
        files = list(data.get("attachments", []))
        if len(files) >= attachments.MAX_ATTACHMENTS:
            await message.answer(texts.ATTACH_TOO_MANY, reply_markup=_attach_keyboard())
            return
        files.append(
            {"file_id": pending.file_id, "filename": pending.filename, "mime": pending.mime}
        )
        # An album arrives as N separate messages sharing media_group_id —
        # store every photo but confirm only once per album, not N times.
        group_id = message.media_group_id
        same_album = group_id is not None and group_id == data.get("last_album")
        await state.update_data(attachments=files, last_album=group_id)
        if not same_album:
            await message.answer(texts.attach_added(len(files)), reply_markup=_attach_keyboard())

    @router.message(NewTicket.attaching, F.text)
    async def on_attaching_text(message: Message, state: FSMContext) -> None:
        # Text fallback for when the inline keyboard is unavailable.
        if message.text.strip().casefold() == texts.ATTACH_DONE_WORD:
            await message.answer(await _confirm_text(state), reply_markup=_confirm_keyboard())
            return
        await message.answer(texts.ATTACH_UNSUPPORTED, reply_markup=_attach_keyboard())

    @router.message(NewTicket.attaching)
    async def on_attaching_other(message: Message) -> None:
        await message.answer(texts.ATTACH_UNSUPPORTED, reply_markup=_attach_keyboard())

    @router.callback_query(NewTicket.attaching, F.data == "nt:attach_done")
    async def on_attach_done(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.message.edit_text(await _confirm_text(state), reply_markup=_confirm_keyboard())
        await cb.answer()

    @router.callback_query(NewTicket.attaching, F.data == "nt:attach_cancel")
    async def on_attach_cancel(cb: CallbackQuery) -> None:
        # Cancelling the whole ticket is destructive -> confirm first. Files and
        # state are kept until the user actually confirms.
        await cb.message.edit_text(
            texts.ATTACH_CANCEL_CONFIRM, reply_markup=_attach_cancel_keyboard()
        )
        await cb.answer()

    @router.callback_query(NewTicket.attaching, F.data == "nt:attach_back")
    async def on_attach_back(cb: CallbackQuery) -> None:
        await cb.message.edit_text(texts.NEW_ATTACH_PROMPT, reply_markup=_attach_keyboard())
        await cb.answer()

    @router.callback_query(NewTicket.confirming, F.data == "nt:confirm")
    async def on_confirm(cb: CallbackQuery, state: FSMContext, link: LinkedUser, bot: Bot) -> None:
        data = await state.get_data()
        # The user's intent is explicit — create the ticket even if the summary
        # message can't be edited anymore (deleted / >48h old).
        await notify.safe_edit(cb, texts.NEW_CREATING)
        await cb.answer()
        try:
            ticket_id = await client.create_ticket(
                name=data["title"],
                content=data["description"],
                urgency=data["urgency"],
                itilcategories_id=data.get("category_id"),
                # Requester is the linked employee, not the service account.
                requester_users_id=link.glpi_users_id,
            )
        except GlpiError as exc:
            log.exception("new_ticket_create_failed error=%s raw=%s", exc, exc.raw)
            await cb.message.answer(texts.GLPI_ERROR)
            await state.clear()
            return
        # Track it so the sync loop can notify this requester of updates.
        try:
            await repo.track_ticket(
                ticket_id=ticket_id,
                requester_tg_id=cb.from_user.id,
                requester_glpi_id=link.glpi_users_id,
                status=TICKET_STATUS_NEW,
                now=int(time.time()),
            )
        except Exception:  # noqa: BLE001 - tracking must never fail ticket creation
            log.exception("track_ticket_failed ticket_id=%s", ticket_id)

        files = data.get("attachments", [])
        uploaded = await _upload_attachments(bot, ticket_id, files)
        await state.clear()
        await cb.message.answer(
            texts.ticket_created(ticket_id, _ticket_url(ticket_id)),
            reply_markup=main_menu_keyboard(),
        )
        if files and uploaded < len(files):
            await cb.message.answer(texts.attachments_partial_failure(uploaded, len(files)))
        # Off-hours: tell the requester when support will actually see it.
        notice = _quiet_notice(data["urgency"])
        if notice:
            await cb.message.answer(notice)

    async def _upload_attachments(bot: Bot, ticket_id: int, files: list[dict]) -> int:
        """Upload each collected file to the ticket; return how many succeeded."""
        uploaded = 0
        for att in files:
            try:
                content = await attachments.download(bot, att["file_id"])
                await client.attach_document_to_ticket(
                    ticket_id, att["filename"], content, mime=att.get("mime")
                )
                uploaded += 1
            except Exception:  # noqa: BLE001 - one bad file shouldn't sink the rest
                log.warning(
                    "attach_upload_failed ticket=%s file=%s", ticket_id, att.get("filename")
                )
        return uploaded

    # Cancel from any state (inline button).
    @router.callback_query(F.data == "nt:cancel")
    async def on_cancel_cb(cb: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await cb.message.edit_text(texts.NEW_CANCELLED)
        await cb.answer()

    # --- free text outside a dialog: offer to turn it into a ticket ----------
    @router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
    async def on_free_text(message: Message, state: FSMContext) -> None:
        # Stash the text as the ticket description and ask for confirmation.
        await state.update_data(description=message.text.strip())
        await message.answer(texts.FREETEXT_OFFER, reply_markup=_offer_keyboard())

    @router.callback_query(StateFilter(None), F.data == "nt:offer:yes")
    async def on_offer_yes(cb: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        description = data.get("description")
        if not description:
            # Stale prompt (already handled or state reset) — nothing to do.
            await cb.answer()
            return
        try:
            categories = await category_cache.get()
        except GlpiError as exc:
            log.warning("new_ticket_categories_failed error=%s", exc)
            categories = None
        if not categories:
            await state.clear()
            await cb.message.edit_text(texts.NEW_NO_CATEGORIES)
            await cb.answer()
            return
        # Keep the stashed description; continue the FSM from category selection.
        await state.set_state(NewTicket.choosing_category)
        await cb.message.edit_text(
            texts.NEW_CHOOSE_CATEGORY, reply_markup=_categories_keyboard(categories)
        )
        await cb.answer()

    @router.callback_query(StateFilter(None), F.data == "nt:offer:no")
    async def on_offer_no(cb: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await cb.message.edit_text(texts.NEW_CANCELLED)
        await cb.answer()

    return router
