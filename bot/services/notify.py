"""Message rendering + sending for the sync loop (feature 4).

Kept separate from :mod:`bot.services.sync` so the polling logic stays free of
Telegram formatting. Every send is best-effort: a blocked user or a transient
Telegram error is logged, never raised, so one bad recipient can't stall the
loop.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import texts
from ..glpi.models import Followup, Ticket

log = logging.getLogger(__name__)


def tech_ticket_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    """Action buttons on the tech-group card. Handlers arrive in feature 5."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.BTN_TECH_TAKE, callback_data=f"ta:take:{ticket_id}"
                ),
                InlineKeyboardButton(
                    text=texts.BTN_TECH_COMMENT, callback_data=f"ta:comment:{ticket_id}"
                ),
                InlineKeyboardButton(
                    text=texts.BTN_TECH_CLOSE, callback_data=f"ta:close:{ticket_id}"
                ),
            ]
        ]
    )


async def _send(bot: Bot, chat_id: int, text: str, **kwargs) -> bool:
    try:
        await bot.send_message(chat_id, text, **kwargs)
        return True
    except Exception as exc:  # noqa: BLE001 - aiogram raises many send errors
        log.warning("notify_send_failed chat=%s error=%s", chat_id, exc)
        return False


async def notify_new_ticket(
    bot: Bot,
    chat_id: int,
    ticket: Ticket,
    url: str | None,
    *,
    requester_name: str | None = None,
    requester_tg_id: int | None = None,
) -> None:
    await _send(
        bot,
        chat_id,
        texts.notify_new_ticket(
            ticket_id=ticket.id,
            title=ticket.name,
            status=ticket.status,
            url=url,
            requester_name=requester_name,
            requester_tg_id=requester_tg_id,
        ),
        reply_markup=tech_ticket_keyboard(ticket.id),
    )


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


async def notify_reminder(
    bot: Bot, chat_id: int, ticket_id: int, title: str, hours_ago: int | None
) -> None:
    """Requester nudge to the tech group, with the standard action buttons."""
    await _send(
        bot,
        chat_id,
        texts.notify_reminder(ticket_id=ticket_id, title=title, hours_ago=hours_ago),
        reply_markup=tech_ticket_keyboard(ticket_id),
    )
