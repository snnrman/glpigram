"""Message rendering + sending for the sync loop (feature 4).

Kept separate from :mod:`bot.services.sync` so the polling logic stays free of
Telegram formatting. Every send is best-effort: a blocked user or a transient
Telegram error is logged, never raised, so one bad recipient can't stall the
loop.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramMigrateToChat, TelegramRetryAfter
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

from .. import texts
from ..glpi.models import Followup, Ticket

log = logging.getLogger(__name__)

# Never sleep longer than this on a Telegram flood wait; give up instead so the
# caller can keep the item queued and retry on its own schedule.
_MAX_RETRY_AFTER = 60


def tech_ticket_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    """Action buttons on the tech-group card.

    Secondary actions share the top row; the primary action ("Take") sits alone
    on the bottom row, full-width — the biggest, most tappable button.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.BTN_TECH_COMMENT, callback_data=f"ta:comment:{ticket_id}"
                ),
                InlineKeyboardButton(
                    text=texts.BTN_TECH_CLOSE, callback_data=f"ta:close:{ticket_id}"
                ),
            ],
            [InlineKeyboardButton(text=texts.BTN_TECH_TAKE, callback_data=f"ta:take:{ticket_id}")],
        ]
    )


async def _send(bot: Bot, chat_id: int, text: str, **kwargs) -> Message | None:
    """Best-effort send. Honours one Telegram flood-wait.

    Returns the sent Message (truthy) so callers can remember its id for the
    living card; None (falsy) on failure — callers that must not lose messages
    (the quiet-hours flush) keep the item queued then.
    """
    for attempt in (1, 2):
        try:
            return await bot.send_message(chat_id, text, **kwargs)
        except TelegramRetryAfter as exc:
            # Flood limit. Wait it out once if reasonable, otherwise report
            # failure so the caller can retry later instead of dropping data.
            if attempt == 1 and exc.retry_after <= _MAX_RETRY_AFTER:
                log.warning("notify_flood_wait chat=%s seconds=%s", chat_id, exc.retry_after)
                await asyncio.sleep(exc.retry_after)
                continue
            log.warning("notify_flood_giveup chat=%s seconds=%s", chat_id, exc.retry_after)
            return None
        except TelegramMigrateToChat as exc:
            # Group upgraded to a supergroup: the configured id is dead. Loud —
            # every group notification is lost until the operator fixes .env.
            log.critical(
                "chat_migrated old=%s new=%s — update TECH_GROUP_CHAT_ID in the .env!",
                chat_id,
                exc.migrate_to_chat_id,
            )
            return None
        except Exception as exc:  # noqa: BLE001 - aiogram raises many send errors
            log.warning("notify_send_failed chat=%s error=%s", chat_id, exc)
            return None
    return None  # pragma: no cover - loop always returns


async def send_text(
    bot: Bot, chat_id: int, text: str, *, reply_markup: InlineKeyboardMarkup | None = None
) -> Message | None:
    """Best-effort plain message (e.g. the deferred-batch header)."""
    return await _send(bot, chat_id, text, reply_markup=reply_markup)


async def safe_edit(cb: CallbackQuery, text: str, reply_markup=None) -> bool:
    """Edit the callback's message, tolerating Telegram edge cases.

    Returns False (never raises) when the message is inaccessible (older
    callback), deleted, or past Telegram's 48-hour edit limit — callers use it
    after the action already happened, so the edit must stay cosmetic.
    """
    msg = cb.message
    if not isinstance(msg, Message):  # None or InaccessibleMessage
        return False
    try:
        await msg.edit_text(text, reply_markup=reply_markup)
        return True
    except Exception as exc:  # noqa: BLE001 - deleted / >48h / not modified
        log.warning("edit_failed chat=%s msg=%s error=%s", msg.chat.id, msg.message_id, exc)
        return False


async def notify_new_ticket(
    bot: Bot,
    chat_id: int,
    ticket: Ticket,
    url: str | None,
    *,
    requester_name: str | None = None,
    requester_tg_id: int | None = None,
    attachments_count: int = 0,
    history: list[str] | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message | None:
    return await _send(
        bot,
        chat_id,
        texts.notify_new_ticket(
            ticket_id=ticket.id,
            title=ticket.name,
            status=ticket.status,
            url=url,
            urgency=ticket.urgency or None,  # 0 = not provided by GLPI
            requester_name=requester_name,
            requester_tg_id=requester_tg_id,
            attachments_count=attachments_count,
            history=history,
        ),
        reply_markup=reply_markup if reply_markup is not None else tech_ticket_keyboard(ticket.id),
    )


async def send_photos(bot: Bot, chat_id: int, photos: list[tuple[str, bytes]]) -> bool:
    """Best-effort image delivery under a card: one photo or a media group."""
    if not photos:
        return True
    try:
        if len(photos) == 1:
            name, data = photos[0]
            await bot.send_photo(chat_id, BufferedInputFile(data, filename=name))
        else:
            media = [
                InputMediaPhoto(media=BufferedInputFile(data, filename=name))
                for name, data in photos[:10]  # Telegram media-group limit
            ]
            await bot.send_media_group(chat_id, media)
        return True
    except Exception as exc:  # noqa: BLE001 - images are auxiliary to the card
        log.warning("notify_photos_failed chat=%s count=%s error=%s", chat_id, len(photos), exc)
        return False


async def notify_status_change(bot: Bot, tg_id: int, ticket: Ticket, url: str | None) -> None:
    await _send(
        bot,
        tg_id,
        texts.notify_status_change(
            ticket_id=ticket.id, title=ticket.name, status=ticket.status, url=url
        ),
    )


async def notify_followup(
    bot: Bot, tg_id: int, ticket: Ticket, followup: Followup, url: str | None
) -> None:
    await _send(
        bot,
        tg_id,
        texts.notify_followup(
            ticket_id=ticket.id, title=ticket.name, body=followup.content, url=url
        ),
    )


async def notify_closed_by_requester(
    bot: Bot, chat_id: int, ticket_id: int, reason: str | None, assignees: list[str]
) -> None:
    """Tell the tech group a requester closed their own ticket (feature 3)."""
    await _send(
        bot,
        chat_id,
        texts.notify_closed_by_requester(ticket_id=ticket_id, reason=reason, assignees=assignees),
    )


def tech_ticket_keyboard_taken(ticket_id: int) -> InlineKeyboardMarkup:
    """Card buttons once taken: Take is gone; Reply/Close plus Reassign."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.BTN_TECH_COMMENT, callback_data=f"ta:comment:{ticket_id}"
                ),
                InlineKeyboardButton(
                    text=texts.BTN_TECH_CLOSE, callback_data=f"ta:close:{ticket_id}"
                ),
            ],
            [InlineKeyboardButton(text=texts.BTN_HANDOFF, callback_data=f"ta:handoff:{ticket_id}")],
        ]
    )


def tech_ticket_keyboard_solved(ticket_id: int) -> InlineKeyboardMarkup:
    """Solved card: Reply stays; Close becomes a passive "awaiting confirmation"."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.BTN_TECH_COMMENT, callback_data=f"ta:comment:{ticket_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.BTN_TECH_WAITING, callback_data=f"ta:wait:{ticket_id}"
                )
            ],
        ]
    )


def unassigned_take_keyboard(ticket_ids: list[int]) -> InlineKeyboardMarkup:
    """One full-width Take button per unattended ticket in the summary."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.btn_take_ticket(tid), callback_data=f"ta:take:{tid}")]
            for tid in ticket_ids
        ]
    )


def solution_confirm_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    """Requester's prompt under a proposed solution: confirm or return to work."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.BTN_CONFIRM_SOLUTION, callback_data=f"rs:ok:{ticket_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.BTN_RETURN_TO_WORK, callback_data=f"rs:back:{ticket_id}"
                )
            ],
        ]
    )


async def notify_reminder(
    bot: Bot, chat_id: int, ticket_id: int, title: str, hours_ago: int | None
) -> Message | None:
    """Requester nudge to the tech group, with the standard action buttons."""
    return await _send(
        bot,
        chat_id,
        texts.notify_reminder(ticket_id=ticket_id, title=title, hours_ago=hours_ago),
        reply_markup=tech_ticket_keyboard(ticket_id),
    )
