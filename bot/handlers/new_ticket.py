"""/new — FSM dialog that creates a GLPI ticket.

Flow (CLAUDE.md feature 1):
    category -> urgency -> title -> description -> confirm -> create ticket.

Attachments (optional photos/files) are added in feature 6; the dialog is
structured so a step can slot in before the confirmation.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
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
from ..db.repo import LinkedUser
from ..glpi.client import (
    URGENCY_HIGH,
    URGENCY_LOW,
    URGENCY_MEDIUM,
    GlpiClient,
    GlpiError,
)
from ..glpi.models import ITILCategory

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
    *,
    ticket_front_base: str | None = None,
) -> Router:
    """Wire the /new dialog with its GLPI dependencies (closure-injected)."""
    router = Router(name="new_ticket")

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

    @router.message(F.text == texts.BTN_MY_TICKETS)
    async def btn_my_tickets(message: Message) -> None:
        # Feature 3 not built yet: acknowledge and keep the menu visible.
        await message.answer(texts.MY_TICKETS_SOON, reply_markup=main_menu_keyboard())

    @router.callback_query(NewTicket.choosing_category, F.data.startswith("nt:cat:"))
    async def on_category(cb: CallbackQuery, state: FSMContext) -> None:
        cat_id = int(cb.data.rsplit(":", 1)[1])
        categories = await category_cache.get()
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
            # Description was pre-filled (free-text flow) -> straight to confirm.
            await state.set_state(NewTicket.confirming)
            await message.answer(
                texts.confirm_summary(
                    data["category_name"], data["urgency"], title, data["description"]
                ),
                reply_markup=_confirm_keyboard(),
            )
            return
        await state.set_state(NewTicket.entering_description)
        await message.answer(texts.NEW_ENTER_DESCRIPTION)

    @router.message(NewTicket.entering_title)
    async def on_title_not_text(message: Message, state: FSMContext) -> None:
        await message.answer(texts.NEW_EXPECT_TEXT)

    @router.message(NewTicket.entering_description, F.text)
    async def on_description(message: Message, state: FSMContext) -> None:
        await state.update_data(description=message.text.strip())
        data = await state.get_data()
        await state.set_state(NewTicket.confirming)
        await message.answer(
            texts.confirm_summary(
                data["category_name"], data["urgency"], data["title"], data["description"]
            ),
            reply_markup=_confirm_keyboard(),
        )

    @router.message(NewTicket.entering_description)
    async def on_description_not_text(message: Message, state: FSMContext) -> None:
        await message.answer(texts.NEW_EXPECT_TEXT)

    @router.callback_query(NewTicket.confirming, F.data == "nt:confirm")
    async def on_confirm(cb: CallbackQuery, state: FSMContext, link: LinkedUser) -> None:
        data = await state.get_data()
        await cb.message.edit_text(texts.NEW_CREATING)
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
            log.error("new_ticket_create_failed error=%s raw=%s", exc, exc.raw)
            await cb.message.answer(texts.GLPI_ERROR)
            await state.clear()
            return
        await state.clear()
        await cb.message.answer(
            texts.ticket_created(ticket_id, _ticket_url(ticket_id)),
            reply_markup=main_menu_keyboard(),
        )

    # Cancel from any state (inline button or /cancel command).
    @router.callback_query(F.data == "nt:cancel")
    async def on_cancel_cb(cb: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await cb.message.edit_text(texts.NEW_CANCELLED)
        await cb.answer()

    @router.message(StateFilter(NewTicket), Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(texts.NEW_CANCELLED, reply_markup=main_menu_keyboard())

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
