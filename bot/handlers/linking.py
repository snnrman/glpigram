"""Account linking (feature 2): TG <-> GLPI, AD-login based.

Flow:
    /start (unlinked) -> ask for AD login -> user sends login -> bot finds the
    active GLPI user by ``name`` -> posts a confirmation card into the tech group
    -> a technician taps Confirm/Reject -> mapping stored (or request rejected),
    the user is notified.

Anti-spoofing: the confirmation card lives in the tech group and can only be
acted on from that chat (membership is the trust boundary). Technician rights
(``is_tech``) come from the configured GLPI group, checked at confirm time.

Admin commands (technicians only): ``/link`` and ``/unlink``.

This router is deliberately *not* behind :class:`AuthMiddleware` — unlinked
users must be able to run ``/start`` and complete linking.
"""

from __future__ import annotations

import logging
import time

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .. import texts
from ..db.repo import Repo
from ..glpi.client import GlpiClient, GlpiError
from ..services import notify
from .new_ticket import main_menu_keyboard

log = logging.getLogger(__name__)


class Linking(StatesGroup):
    awaiting_login = State()


def normalize_login(raw: str) -> str:
    """Reduce user input to a bare AD sAMAccountName.

    Accepts ``user``, ``user@domain`` and ``DOMAIN\\user``; returns ``user``.
    """
    login = raw.strip()
    if "\\" in login:  # DOMAIN\user
        login = login.rsplit("\\", 1)[1]
    if "@" in login:  # user@domain
        login = login.split("@", 1)[0]
    return login.strip()


def looks_like_name(raw: str) -> bool:
    """Heuristic: treat input as a full name (not a login) if it has a space or
    any Cyrillic letter. AD logins are single ASCII tokens."""
    text = raw.strip()
    if " " in text:
        return True
    return any("Ѐ" <= ch <= "ӿ" for ch in text)


def _candidate_keyboard(users: list) -> InlineKeyboardMarkup:
    """Self-confirm for one match, or a pick-list (each user) for several."""
    rows: list[list[InlineKeyboardButton]] = []
    if len(users) == 1:
        rows.append(
            [
                InlineKeyboardButton(
                    text=texts.BTN_LINK_ITS_ME, callback_data=f"lk:pick:{users[0].id}"
                )
            ]
        )
        rows.append([InlineKeyboardButton(text=texts.BTN_LINK_NOT_ME, callback_data="lk:name_no")])
    else:
        rows.extend(
            [InlineKeyboardButton(text=u.display_name[:60], callback_data=f"lk:pick:{u.id}")]
            for u in users
        )
        rows.append(
            [InlineKeyboardButton(text=texts.BTN_LINK_NONE_OF_THESE, callback_data="lk:name_no")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _confirm_keyboard(tg_id: int, glpi_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.BTN_LINK_CONFIRM, callback_data=f"lk:ok:{tg_id}:{glpi_id}"
                ),
                InlineKeyboardButton(
                    text=texts.BTN_LINK_REJECT, callback_data=f"lk:no:{tg_id}:{glpi_id}"
                ),
            ]
        ]
    )


def _tg_display(user) -> str:
    """Readable Telegram identity for the confirmation card."""
    if user is None:
        return "?"
    name = user.full_name or ""
    if user.username:
        name = f"{name} (@{user.username})".strip()
    return name or str(user.id)


def build_linking_router(
    repo: Repo,
    client: GlpiClient,
    *,
    tech_group_chat_id: int | None,
    tech_group_id: int | None,
) -> Router:
    router = Router(name="linking")

    async def _resolve_is_tech(glpi_users_id: int) -> bool:
        if tech_group_id is None:
            return False
        try:
            return await client.user_in_group(glpi_users_id, tech_group_id)
        except GlpiError as exc:
            log.warning("is_tech_check_failed glpi_id=%s error=%s", glpi_users_id, exc)
            return False

    async def _require_tech(message: Message) -> bool:
        """True if the message sender is a linked technician; else replies and False."""
        link = await repo.get_by_tg(message.from_user.id)
        if link is None or not link.is_tech:
            await message.answer(texts.TECH_ONLY)
            return False
        return True

    # -- /start + login submission ----------------------------------------
    # /start and the login dialog are private-only; the admin commands
    # (/link, /unlink) stay usable in the tech group (reply-based linking).
    @router.message(CommandStart(), F.chat.type == "private")
    async def cmd_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        link = await repo.get_by_tg(message.from_user.id)
        if link is not None:
            # WELCOME_MESSAGE replaces the greeting for LINKED users only.
            custom = texts.custom_welcome()
            await message.answer(
                custom or texts.START_GREETING,
                reply_markup=main_menu_keyboard(is_tech=link.is_tech),
            )
            return
        # Unlinked users always get the sign-in prompt — a custom welcome about
        # "create tickets" would mislead someone who can't create them yet —
        # and land straight in the linking FSM, no extra taps.
        await state.set_state(Linking.awaiting_login)
        await message.answer(texts.LINK_WELCOME)

    async def _send_link_request(bot: Bot, requester, user) -> bool:
        """Post the admin confirmation card into the tech group. Returns success."""
        if tech_group_chat_id is None:
            return False
        try:
            await bot.send_message(
                tech_group_chat_id,
                texts.link_request(
                    tg_id=requester.id,
                    tg_name=_tg_display(requester),
                    login=user.name,
                    glpi_name=user.display_name,
                    glpi_id=user.id,
                ),
                reply_markup=_confirm_keyboard(requester.id, user.id),
            )
            return True
        except Exception as exc:  # noqa: BLE001 - misconfigured chat id / bot not in group
            log.exception("link_request_send_failed chat=%s error=%s", tech_group_chat_id, exc)
            return False

    @router.message(Linking.awaiting_login, F.text, F.chat.type == "private")
    async def on_login(message: Message, state: FSMContext, bot: Bot) -> None:
        if tech_group_chat_id is None:
            log.error("linking_no_tech_group_chat_configured")
            await state.clear()
            await message.answer(texts.LINK_NO_TECH_GROUP)
            return
        text = message.text.strip()
        if looks_like_name(text):
            await _handle_name_query(message, text)
            return
        # Otherwise treat it as an AD login (exact, single active match).
        login = normalize_login(text)
        try:
            user = await client.find_user_by_login(login)
        except GlpiError as exc:
            log.warning("link_lookup_failed login=%s error=%s", login, exc)
            await message.answer(texts.GLPI_ERROR)
            return
        if user is None:
            await message.answer(texts.LINK_USER_NOT_FOUND)  # stay in state to retry
            return
        if not await _send_link_request(bot, message.from_user, user):
            await state.clear()
            await message.answer(texts.LINK_NO_TECH_GROUP)
            return
        await state.clear()
        await message.answer(texts.LINK_PENDING)

    async def _handle_name_query(message: Message, query: str) -> None:
        """Name path: search by first/last name and offer the matches."""
        try:
            users = await client.search_users_by_name(query)
        except GlpiError as exc:
            log.warning("link_name_lookup_failed error=%s", exc)
            await message.answer(texts.GLPI_ERROR)
            return
        if not users:
            await message.answer(texts.LINK_NAME_NOT_FOUND)  # stay in state, ask for login
            return
        if len(users) == 1:
            await message.answer(
                texts.link_name_pick_one(users[0].display_name),
                reply_markup=_candidate_keyboard(users),
            )
        else:
            await message.answer(texts.LINK_NAME_PICK_MANY, reply_markup=_candidate_keyboard(users))

    @router.callback_query(Linking.awaiting_login, F.data.startswith("lk:pick:"))
    async def on_name_pick(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        glpi_id = int(cb.data.split(":")[2])
        try:
            user = await client.get_user(glpi_id)
        except GlpiError as exc:
            log.warning("link_pick_lookup_failed glpi_id=%s error=%s", glpi_id, exc)
            await cb.answer(texts.GLPI_ERROR, show_alert=True)
            return
        if user is None or not user.is_usable:
            await state.clear()
            await cb.message.edit_text(texts.LINK_USER_NOT_FOUND)
            await cb.answer()
            return
        if not await _send_link_request(bot, cb.from_user, user):
            await state.clear()
            await cb.message.edit_text(texts.LINK_NO_TECH_GROUP)
            await cb.answer()
            return
        await state.clear()
        await cb.message.edit_text(texts.LINK_PENDING)
        await cb.answer()

    @router.callback_query(Linking.awaiting_login, F.data == "lk:name_no")
    async def on_name_none(cb: CallbackQuery) -> None:
        # Keep the linking state; ask for the AD login instead.
        await cb.message.edit_text(texts.LINK_ASK_LOGIN)
        await cb.answer()

    @router.message(Linking.awaiting_login, F.chat.type == "private")
    async def on_login_not_text(message: Message) -> None:
        await message.answer(texts.LINK_ASK_LOGIN)

    # -- tech-group confirmation ------------------------------------------
    @router.callback_query(F.data.startswith("lk:ok:"))
    async def on_confirm(cb: CallbackQuery, bot: Bot) -> None:
        if not _from_tech_group(cb, tech_group_chat_id):
            await cb.answer(texts.CB_TECH_GROUP_ONLY, show_alert=True)
            return
        _, _, tg_id_s, glpi_id_s = cb.data.split(":")
        tg_id, glpi_id = int(tg_id_s), int(glpi_id_s)
        try:
            user = await client.get_user(glpi_id)
        except GlpiError as exc:
            log.warning("link_confirm_lookup_failed glpi_id=%s error=%s", glpi_id, exc)
            await cb.answer(texts.GLPI_ERROR, show_alert=True)
            return
        if user is None or not user.is_usable:
            await notify.safe_edit(cb, texts.LINK_ALREADY_HANDLED)
            await cb.answer()
            return
        is_tech = await _resolve_is_tech(glpi_id)
        await repo.upsert_link(
            tg_id=tg_id,
            glpi_users_id=glpi_id,
            display_name=user.display_name,
            is_tech=is_tech,
            now=int(time.time()),
        )
        # The link now exists: notify the employee FIRST, then update the card.
        # Cards can sit in the group past Telegram's 48h edit limit (a weekend
        # request confirmed on Monday) — a failed edit must not swallow the
        # user notification or make the admin think the confirm failed.
        await _notify_user(bot, tg_id, texts.LINK_CONFIRMED, menu_is_tech=is_tech)
        await notify.safe_edit(
            cb,
            texts.link_request_resolved(
                glpi_name=user.display_name,
                login=user.name,
                approved=True,
                by=_tg_display(cb.from_user),
            ),
        )
        await cb.answer()

    @router.callback_query(F.data.startswith("lk:no:"))
    async def on_reject(cb: CallbackQuery, bot: Bot) -> None:
        if not _from_tech_group(cb, tech_group_chat_id):
            await cb.answer(texts.CB_TECH_GROUP_ONLY, show_alert=True)
            return
        _, _, tg_id_s, glpi_id_s = cb.data.split(":")
        tg_id, glpi_id = int(tg_id_s), int(glpi_id_s)
        login = ""
        try:
            user = await client.get_user(glpi_id)
            login = user.name if user else ""
        except GlpiError:
            pass
        await _notify_user(bot, tg_id, texts.LINK_REJECTED, menu_is_tech=None)
        await notify.safe_edit(
            cb,
            texts.link_request_resolved(
                glpi_name="", login=login, approved=False, by=_tg_display(cb.from_user)
            ),
        )
        await cb.answer()

    # -- admin commands (technicians only) --------------------------------
    @router.message(Command("unlink"))
    async def cmd_unlink(message: Message, command: CommandObject) -> None:
        if not await _require_tech(message):
            return
        if not command.args:
            await message.answer(texts.ADMIN_UNLINK_USAGE)
            return
        login = normalize_login(command.args)
        try:
            user = await client.find_user_by_login(login, active_only=False)
        except GlpiError as exc:
            log.warning("unlink_lookup_failed login=%s error=%s", login, exc)
            await message.answer(texts.GLPI_ERROR)
            return
        removed = await repo.unlink_glpi(user.id) if user else False
        await message.answer(texts.admin_unlink_result(login=login, removed=removed))

    @router.message(Command("link"))
    async def cmd_link(message: Message, command: CommandObject, bot: Bot) -> None:
        if not await _require_tech(message):
            return
        target_tg_id, login = _parse_link_args(message, command.args)
        if target_tg_id is None or not login:
            await message.answer(texts.ADMIN_LINK_USAGE)
            return
        login = normalize_login(login)
        try:
            user = await client.find_user_by_login(login)
        except GlpiError as exc:
            log.warning("link_admin_lookup_failed login=%s error=%s", login, exc)
            await message.answer(texts.GLPI_ERROR)
            return
        if user is None:
            await message.answer(texts.LINK_USER_NOT_FOUND)
            return
        is_tech = await _resolve_is_tech(user.id)
        await repo.upsert_link(
            tg_id=target_tg_id,
            glpi_users_id=user.id,
            display_name=user.display_name,
            is_tech=is_tech,
            now=int(time.time()),
        )
        await message.answer(
            texts.admin_link_ok(tg_id=target_tg_id, glpi_name=user.display_name, login=user.name)
        )
        await _notify_user(bot, target_tg_id, texts.LINK_CONFIRMED, menu_is_tech=is_tech)

    async def _notify_user(bot: Bot, tg_id: int, text: str, *, menu_is_tech: bool | None) -> None:
        """Best-effort private message to the requesting user (they may have blocked the bot).

        ``menu_is_tech``: None -> no menu keyboard, else the role for it.
        """
        try:
            markup = None if menu_is_tech is None else main_menu_keyboard(is_tech=menu_is_tech)
            await bot.send_message(tg_id, text, reply_markup=markup)
        except Exception as exc:  # noqa: BLE001 - aiogram raises many send errors
            log.warning("notify_user_failed tg_id=%s error=%s", tg_id, exc)

    return router


def _from_tech_group(cb: CallbackQuery, tech_group_chat_id: int | None) -> bool:
    return (
        tech_group_chat_id is not None
        and cb.message is not None
        and cb.message.chat.id == tech_group_chat_id
    )


def _parse_link_args(message: Message, args: str | None) -> tuple[int | None, str | None]:
    """Resolve ``/link`` target: reply + ``<login>`` or ``<tg_id> <login>``."""
    parts = args.split() if args else []
    reply = message.reply_to_message
    if reply is not None and reply.from_user is not None and len(parts) >= 1:
        return reply.from_user.id, parts[0]
    if len(parts) >= 2 and parts[0].lstrip("-").isdigit():
        return int(parts[0]), parts[1]
    return None, None
