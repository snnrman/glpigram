"""GLPI -> Telegram sync loop (feature 4).

Polls GLPI every ``interval`` seconds and, per tick:

1. **New tickets** (``id`` > cursor) -> notify the tech group with action
   buttons, then advance the cursor.
2. **Status changes** on bot-created tickets -> notify the requester.
3. **New followups by others** on those tickets -> forward the text to the
   requester.

Only bot-created tickets (rows in ``bot_tickets``) are watched for #2/#3, since
those are the only ones mapped to a requester's Telegram id. Cursor state lives
in SQLite so restarts don't replay notifications; on a fresh database the ticket
cursor is seeded to the current max id so history isn't blasted to the group.

The loop never dies on an error: each tick — and each ticket within it — is
guarded, logged, and the loop continues (CLAUDE.md).
"""

from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot

from ..db.repo import Repo, TrackedTicket
from ..glpi.client import TICKET_STATUS_CLOSED, GlpiClient, GlpiError
from . import notify

log = logging.getLogger(__name__)

_CURSOR_LAST_TICKET = "last_ticket_id"


class SyncService:
    def __init__(
        self,
        bot: Bot,
        client: GlpiClient,
        repo: Repo,
        *,
        tech_group_chat_id: int | None,
        interval: int = 45,
        front_base: str | None = None,
        recent_page: int = 100,
    ) -> None:
        self._bot = bot
        self._client = client
        self._repo = repo
        self._tech_chat = tech_group_chat_id
        self._interval = interval
        self._front_base = front_base
        self._recent_page = recent_page

    def _ticket_url(self, ticket_id: int) -> str | None:
        if not self._front_base:
            return None
        return f"{self._front_base}/front/ticket.form.php?id={ticket_id}"

    async def run(self) -> None:
        """Background entrypoint: seed the cursor, then poll until cancelled."""
        try:
            await self._seed_cursor()
        except GlpiError as exc:
            log.warning("sync_seed_cursor_failed error=%s", exc)
        log.info("sync_loop_started interval=%ss", self._interval)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - loop must survive any tick failure
                log.exception("sync_tick_failed")
            await asyncio.sleep(self._interval)

    async def _seed_cursor(self) -> None:
        if await self._repo.get_cursor(_CURSOR_LAST_TICKET) is not None:
            return
        recent = await self._client.list_recent_tickets(limit=1)
        max_id = recent[0].id if recent else 0
        await self._repo.set_cursor(_CURSOR_LAST_TICKET, max_id)
        log.info("sync_cursor_seeded last_ticket_id=%s", max_id)

    async def tick(self) -> None:
        await self._poll_new_tickets()
        await self._poll_tracked_tickets()

    # -- 1. new tickets -> tech group -------------------------------------
    async def _poll_new_tickets(self) -> None:
        last_seen = await self._repo.get_cursor(_CURSOR_LAST_TICKET) or 0
        recent = await self._client.list_recent_tickets(limit=self._recent_page)
        fresh = sorted((t for t in recent if t.id > last_seen), key=lambda t: t.id)
        if not fresh:
            return
        if len(fresh) == self._recent_page:
            # The page was full of new tickets — older new ones may be beyond it.
            log.warning("sync_new_tickets_page_full count=%s", len(fresh))
        if self._tech_chat is not None:
            for ticket in fresh:
                name, tg_id = await self._requester_card_info(ticket.id)
                await notify.notify_new_ticket(
                    self._bot,
                    self._tech_chat,
                    ticket,
                    self._ticket_url(ticket.id),
                    requester_name=name,
                    requester_tg_id=tg_id,
                )
        await self._repo.set_cursor(_CURSOR_LAST_TICKET, max(t.id for t in fresh))

    async def _requester_card_info(self, ticket_id: int) -> tuple[str | None, int | None]:
        """Requester name for the card, plus their Telegram id if linked in the bot.

        The requester is a Ticket_User relation (not a ticket column), so it needs
        its own lookup; failure just omits the author rather than losing the card.
        """
        try:
            requester = await self._client.get_ticket_requester(ticket_id)
        except GlpiError as exc:
            log.warning("sync_requester_lookup_failed id=%s error=%s", ticket_id, exc)
            return None, None
        if requester is None:
            return None, None
        glpi_id, name = requester
        link = await self._repo.get_by_glpi(glpi_id)
        return name, (link.tg_id if link else None)

    # -- 2 & 3. tracked tickets: status + followups -----------------------
    async def _poll_tracked_tickets(self) -> None:
        for row in await self._repo.active_tracked_tickets():
            try:
                await self._sync_ticket(row)
            except GlpiError as exc:
                log.warning("sync_ticket_failed id=%s error=%s", row.ticket_id, exc)

    async def _sync_ticket(self, row: TrackedTicket) -> None:
        ticket = await self._client.get_ticket(row.ticket_id)
        if ticket is None:
            # Deleted in GLPI -> stop watching it.
            await self._repo.set_ticket_status(row.ticket_id, status=row.last_status, active=False)
            return

        if ticket.status != row.last_status:
            await notify.notify_status_change(
                self._bot, row.requester_tg_id, ticket, self._ticket_url(ticket.id)
            )
            still_active = ticket.status != TICKET_STATUS_CLOSED
            await self._repo.set_ticket_status(
                row.ticket_id, status=ticket.status, active=still_active
            )

        await self._forward_new_followups(row, ticket)

    async def _forward_new_followups(self, row: TrackedTicket, ticket) -> None:
        followups = await self._client.list_followups(row.ticket_id)
        if not followups:
            return
        fresh = sorted(
            (
                f
                for f in followups
                if f.id > row.last_followup_id
                and not f.is_private
                and f.users_id != row.requester_glpi_id
            ),
            key=lambda f: f.id,
        )
        for followup in fresh:
            await notify.notify_followup(
                self._bot, row.requester_tg_id, ticket, followup, self._ticket_url(ticket.id)
            )
        # Advance past every followup seen (own/private included) to avoid rework.
        max_id = max(f.id for f in followups)
        if max_id > row.last_followup_id:
            await self._repo.set_ticket_followup_cursor(row.ticket_id, max_id)


def now_ts() -> int:
    return int(time.time())
