"""The living tech-group card: one message per ticket, edited on every event.

The card is registered at the moment it is actually sent (so deferred/quiet-
hours cards get their ``message_id`` in the morning). Each event re-renders the
whole card from the stored ingredients — a missed edit (flood limit, 48h edit
window) self-heals on the next event. History is capped at the last 10 lines
to stay far away from Telegram's 4096-char message limit.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime

from aiogram import Bot

from .. import texts
from ..db.repo import Repo
from ..glpi.client import TICKET_STATUS_CLOSED, TICKET_STATUS_SOLVED
from ..glpi.models import Ticket
from . import notify

log = logging.getLogger(__name__)

_MAX_HISTORY = 10


class CardService:
    """Stateless helper over the ``ticket_cards`` table (safe to instantiate twice)."""

    def __init__(
        self,
        repo: Repo,
        *,
        front_base: str | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = repo
        self._front_base = front_base
        self._now = now_provider or datetime.now

    def _url(self, ticket_id: int) -> str | None:
        if not self._front_base:
            return None
        return f"{self._front_base}/front/ticket.form.php?id={ticket_id}"

    async def register(
        self,
        ticket: Ticket,
        *,
        chat_id: int,
        message_id: int,
        requester_name: str | None,
        requester_tg_id: int | None,
        attachments_count: int,
        now: int,
    ) -> None:
        """Remember the card right after the real send (deferred-safe)."""
        await self._repo.save_card(
            ticket_id=ticket.id,
            chat_id=chat_id,
            message_id=message_id,
            title=ticket.name,
            urgency=ticket.urgency,
            requester_name=requester_name or "",
            requester_tg_id=requester_tg_id,
            attachments_count=attachments_count,
            status=ticket.status,
            now=now,
        )

    async def record_event(
        self,
        bot: Bot,
        ticket_id: int,
        line: str | None,
        *,
        status: int | None = None,
        taken_by: str | None = None,
        followup_id: int | None = None,
        reply: str | None = None,
        skip_if_status_unchanged: bool = False,
    ) -> bool:
        """Append a history line, update the header state and edit the card.

        Returns False when the ticket has no card (pre-feature tickets) so the
        caller can fall back to its legacy behaviour. ``followup_id`` dedups
        comments recorded both directly (bot actions) and by the sync loop;
        ``skip_if_status_unchanged`` dedups status flips the card already knows
        about (e.g. sync noticing the status a button action just set).
        """
        card = await self._repo.get_card(ticket_id)
        if card is None:
            return False
        if skip_if_status_unchanged and status is not None and card.status == status:
            return True  # the card already reflects this state - nothing new
        if followup_id is not None and followup_id <= card.last_followup_id:
            return True  # comment already recorded via the other path

        history: list[str] = json.loads(card.history)
        if line:
            history.append(f"{line} · {self._now().strftime('%H:%M')}")
            history = history[-_MAX_HISTORY:]
        new_status = status if status is not None else card.status
        new_taken = taken_by if taken_by is not None else card.taken_by
        new_followup = max(card.last_followup_id, followup_id or 0)
        await self._repo.update_card(
            ticket_id,
            status=new_status,
            taken_by=new_taken,
            history=json.dumps(history, ensure_ascii=False),
            last_followup_id=new_followup,
        )

        text = texts.notify_new_ticket(
            ticket_id=ticket_id,
            title=card.title,
            status=new_status,
            url=self._url(ticket_id),
            urgency=card.urgency or None,
            requester_name=card.requester_name or None,
            requester_tg_id=card.requester_tg_id,
            attachments_count=card.attachments_count,
            history=history,
            assignee=new_taken or None,
        )
        try:
            await bot.edit_message_text(
                text,
                chat_id=card.chat_id,
                message_id=card.message_id,
                reply_markup=self._keyboard(ticket_id, new_status),
            )
        except Exception as exc:  # noqa: BLE001 - flood/48h/deleted: next event self-heals
            log.warning("card_edit_failed ticket=%s error=%s", ticket_id, exc)

        if reply:
            # Edits don't notify anyone — ping the group with a short reply.
            await self._reply(bot, card.chat_id, card.message_id, reply)
        return True

    def _keyboard(self, ticket_id: int, status: int):
        """Buttons follow the state (ITIL cycle):

        New -> Reply/Close + full-width Take; taken -> no re-take; solved ->
        Reply + a passive "awaiting confirmation"; closed -> only the GLPI link.
        """
        if status == TICKET_STATUS_CLOSED:
            url = self._url(ticket_id)
            if url is None:
                return None
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            return InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=texts.OPEN_IN_GLPI, url=url)]]
            )
        if status == TICKET_STATUS_SOLVED:
            return notify.tech_ticket_keyboard_solved(ticket_id)
        if status != 1:  # taken / in progress
            return notify.tech_ticket_keyboard_taken(ticket_id)
        return notify.tech_ticket_keyboard(ticket_id)

    @staticmethod
    async def _reply(bot: Bot, chat_id: int, message_id: int, text: str) -> None:
        from aiogram.types import ReplyParameters

        try:
            await bot.send_message(
                chat_id,
                text,
                reply_parameters=ReplyParameters(
                    message_id=message_id, allow_sending_without_reply=True
                ),
            )
        except Exception as exc:  # noqa: BLE001 - the ping is auxiliary
            log.warning("card_reply_failed chat=%s error=%s", chat_id, exc)
