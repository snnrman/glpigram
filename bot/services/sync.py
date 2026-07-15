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
from collections.abc import Callable
from datetime import UTC, datetime

from aiogram import Bot

from .. import texts, timeutil
from ..db.repo import Repo, TrackedTicket
from ..glpi.client import (
    TICKET_STATUS_CLOSED,
    TICKET_STATUS_NEW,
    TICKET_STATUS_SOLVED,
    URGENCY_URGENT,
    GlpiClient,
    GlpiError,
)
from ..schedule import WorkSchedule
from . import notify
from .cards import CardService

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
        schedule: WorkSchedule,
        interval: int = 45,
        front_base: str | None = None,
        recent_page: int = 100,
        now_provider: Callable[[], datetime] | None = None,
        cards: CardService | None = None,
        unassigned_remind_hours: float = 2,
        remind_interval_hours: float = 3,
    ) -> None:
        self._bot = bot
        self._client = client
        self._repo = repo
        self._tech_chat = tech_group_chat_id
        self._schedule = schedule
        self._interval = interval
        self._front_base = front_base
        self._recent_page = recent_page
        self._now = now_provider or schedule.now
        self._cards = cards or CardService(repo, front_base=front_base)
        self._unassigned_after = unassigned_remind_hours * 3600
        self._remind_interval = remind_interval_hours * 3600

    def _ticket_url(self, ticket_id: int) -> str | None:
        if not self._front_base:
            return None
        return f"{self._front_base}/front/ticket.form.php?id={ticket_id}"

    async def run(self) -> None:
        """Background entrypoint: seed the cursor, then poll until cancelled."""
        try:
            await self._seed_cursor()
        except Exception:  # noqa: BLE001 - a seed failure must not kill the task
            log.exception("sync_seed_cursor_failed")
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
        # Deliver anything queued overnight first, once work has resumed.
        await self._flush_deferred_if_working()
        # One GLPI page feeds both the announcer and the unassigned reminder.
        recent = await self._client.list_recent_tickets(limit=self._recent_page)
        await self._poll_new_tickets(recent)
        await self._remind_unassigned(recent)
        await self._poll_tracked_tickets()

    # -- 1. new tickets -> tech group (with quiet-hours deferral) ----------
    async def _poll_new_tickets(self, recent: list | None = None) -> None:
        last_seen = await self._repo.get_cursor(_CURSOR_LAST_TICKET) or 0
        if recent is None:
            recent = await self._client.list_recent_tickets(limit=self._recent_page)
        fresh = sorted((t for t in recent if t.id > last_seen), key=lambda t: t.id)
        if not fresh:
            return
        if len(fresh) == self._recent_page:
            # The page was full of new tickets — older new ones may be beyond it.
            log.warning("sync_new_tickets_page_full count=%s", len(fresh))
        working = self._schedule.is_working(self._now())
        for ticket in fresh:
            # Only the dedicated urgent (prod) level breaks through quiet hours;
            # ordinary urgency — including HIGH — waits until the next work day.
            if working or ticket.urgency >= URGENCY_URGENT:
                await self._send_new_card(ticket)
            else:
                # Off-hours, non-urgent: hold until the next work day.
                await self._repo.enqueue_deferred("new", ticket.id, int(time.time()))
                log.info("sync_deferred_new id=%s urgency=%s", ticket.id, ticket.urgency)
        await self._repo.set_cursor(_CURSOR_LAST_TICKET, max(t.id for t in fresh))

    # Telegram's sendPhoto upload cap; larger images stay behind the GLPI link.
    _MAX_IMAGE_BYTES = 10 * 1024 * 1024

    async def _send_new_card(self, ticket) -> bool:
        if self._tech_chat is None:
            return True  # nowhere to send is not a delivery failure
        name, tg_id = await self._requester_card_info(ticket.id)
        docs = await self._ticket_documents(ticket.id)
        msg = await notify.notify_new_ticket(
            self._bot,
            self._tech_chat,
            ticket,
            self._ticket_url(ticket.id),
            requester_name=name,
            requester_tg_id=tg_id,
            attachments_count=len(docs),
        )
        sent = msg is not None
        if sent:
            # Register the living card NOW — the moment of the real send — so
            # deferred (quiet-hours) cards get their message_id in the morning.
            await self._cards.register(
                ticket,
                chat_id=self._tech_chat,
                message_id=msg.message_id,
                requester_name=name,
                requester_tg_id=tg_id,
                attachments_count=len(docs),
                now=int(time.time()),
            )
        if sent and docs:
            # Images ride along under the card; everything else (pdf, oversized)
            # is covered by the 📎 line + the GLPI link. Sent in the same pass as
            # the card, so the existing cursor/queue dedup applies — no repeats.
            await self._send_card_images(ticket.id, docs)
        return sent

    async def _ticket_documents(self, ticket_id: int) -> list:
        """Attached documents, best-effort: a lookup failure must not cost the card."""
        try:
            return await self._client.list_ticket_documents(ticket_id)
        except GlpiError as exc:
            log.warning("sync_documents_lookup_failed id=%s error=%s", ticket_id, exc)
            return []

    async def _send_card_images(self, ticket_id: int, docs: list) -> None:
        photos: list[tuple[str, bytes]] = []
        for doc in docs:
            if not doc.is_image or doc.filesize > self._MAX_IMAGE_BYTES:
                continue
            if len(photos) >= 10:  # Telegram media-group limit
                break
            try:
                content = await self._client.download_document(doc.id)
            except GlpiError as exc:
                log.warning("sync_document_download_failed doc=%s error=%s", doc.id, exc)
                continue
            if len(content) > self._MAX_IMAGE_BYTES:  # filesize meta was missing/wrong
                continue
            photos.append((doc.filename, content))
        if photos:
            await notify.send_photos(self._bot, self._tech_chat, photos)

    # -- deferred (quiet-hours) flush -------------------------------------
    async def _flush_deferred_if_working(self) -> None:
        """Deliver the overnight backlog once work resumes.

        A queue row is deleted only after its notification was actually sent
        (or became moot) — a Telegram flood limit / outage keeps the rest
        queued for the next tick instead of silently dropping cards. The
        header is sent lazily, before the first real card, so a total outage
        doesn't spam a lone header every 45 s.
        """
        if self._tech_chat is None or not self._schedule.is_working(self._now()):
            return
        queued = await self._repo.list_deferred()
        if not queued:
            return
        header_sent = False

        async def _ensure_header() -> bool:
            nonlocal header_sent
            if not header_sent:
                header_sent = await notify.send_text(
                    self._bot, self._tech_chat, texts.deferred_batch_header(len(queued))
                )
            return header_sent

        for row_id, kind, ticket_id in queued:
            try:
                delivered = await self._flush_one(kind, ticket_id, _ensure_header)
            except GlpiError as exc:
                # GLPI hiccup: keep the row, retry next tick.
                log.warning("sync_flush_failed id=%s kind=%s error=%s", ticket_id, kind, exc)
                continue
            if delivered:
                await self._repo.delete_deferred(row_id)
            else:
                # Telegram send failed -> keep the whole tail for the next tick
                # (sending further cards now would only hit the same limit).
                break

    async def _flush_one(self, kind: str, ticket_id: int, ensure_header) -> bool:
        """Deliver one queued item; True when it may leave the queue."""
        ticket = await self._client.get_ticket(ticket_id)
        if ticket is None:  # deleted in GLPI meanwhile -> moot
            return True
        if kind == "remind" and ticket.status != TICKET_STATUS_NEW:
            # Taken/solved overnight -> the nudge is moot, drop it silently.
            log.info("sync_flush_remind_skipped id=%s status=%s", ticket_id, ticket.status)
            return True
        if not await ensure_header():
            return False
        if kind == "remind":
            return await notify.notify_reminder(
                self._bot,
                self._tech_chat,
                ticket_id,
                ticket.name,
                timeutil.hours_since(ticket.date_creation),
            )
        return await self._send_new_card(ticket)

    async def _propose_solution(self, row: TrackedTicket, ticket) -> None:
        """Web-solved ticket: solution text (when any) + confirm/return buttons."""
        author_uid, tech_name, content = 0, None, ""
        try:
            solution = await self._client.get_ticket_solution(ticket.id)
            if solution is not None:
                author_uid, tech_name, content = solution
        except GlpiError as exc:
            log.warning("sync_solution_lookup_failed id=%s error=%s", ticket.id, exc)
        await notify.send_text(
            self._bot,
            row.requester_tg_id,
            texts.solution_proposed(ticket_id=ticket.id, tech_name=tech_name, solution=content),
            reply_markup=notify.solution_confirm_keyboard(ticket.id),
        )
        # Remember the solver so a return-to-work can ping them directly.
        if tech_name:
            solver_link = await self._repo.get_by_glpi(author_uid) if author_uid else None
            await self._repo.set_solver(
                ticket.id, tg_id=solver_link.tg_id if solver_link else None, name=tech_name
            )

    async def _notify_solved(self, row: TrackedTicket, ticket) -> None:
        """Solution text + author when available; plain status change otherwise."""
        solution = None
        try:
            solution = await self._client.get_ticket_solution(ticket.id)
        except GlpiError as exc:
            log.warning("sync_solution_lookup_failed id=%s error=%s", ticket.id, exc)
        if solution is None:
            await notify.notify_status_change(
                self._bot, row.requester_tg_id, ticket, self._ticket_url(ticket.id)
            )
            return
        _, tech_name, content = solution
        await notify.send_text(
            self._bot,
            row.requester_tg_id,
            texts.solved_notice(ticket_id=ticket.id, tech_name=tech_name, solution=content),
        )

    async def _author_name(self, user_id: int, cache: dict) -> str | None:
        if not user_id:
            return None
        if user_id in cache:
            return cache[user_id]
        name = None
        try:
            user = await self._client.get_user(user_id)
            name = user.display_name if user else None
        except GlpiError:
            pass
        cache[user_id] = name
        return name

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
    # -- unassigned-tickets reminder (working hours only) ------------------
    async def _remind_unassigned(self, recent: list | None = None) -> None:
        """One summary about New tickets nobody took, with Take buttons.

        Thresholds count WORKING hours (GLPI dates are UTC -> schedule tz);
        per-ticket anti-spam state lives in SQLite and survives restarts. A
        taken ticket (status != New) simply stops matching and drops out.
        """
        if self._tech_chat is None:
            return
        now = self._now()
        if not self._schedule.is_working(now):
            return
        if recent is None:
            recent = await self._client.list_recent_tickets(limit=self._recent_page)
        now_ts = int(now.timestamp())
        due: list[tuple[int, str, int]] = []
        for ticket in recent:
            if ticket.status != TICKET_STATUS_NEW:
                continue  # taken/solved -> out of the reminder
            created = timeutil.parse_glpi_utc(ticket.date_creation)
            if created is None:
                continue
            age = self._schedule.working_seconds_between(created, now)
            if age < self._unassigned_after:
                continue
            last = await self._repo.get_last_unassigned_remind(ticket.id)
            if last is not None:
                since_last = self._schedule.working_seconds_between(
                    datetime.fromtimestamp(last, tz=UTC), now
                )
                if since_last < self._remind_interval:
                    continue  # anti-spam window still open for this ticket
            due.append((ticket.id, ticket.name, int(age // 3600)))
        if not due:
            return
        due.sort()
        due = due[:10]  # keyboard/message size guard; the rest come next round
        text = (
            texts.UNASSIGNED_HEADER
            + "\n"
            + "\n".join(texts.unassigned_line(tid, title, hours) for tid, title, hours in due)
        )
        msg = await notify.send_text(
            self._bot,
            self._tech_chat,
            text,
            reply_markup=notify.unassigned_take_keyboard([tid for tid, _, _ in due]),
        )
        if msg:
            # Stamp the anti-spam state only for what was actually delivered.
            for tid, _, _ in due:
                await self._repo.set_last_unassigned_remind(tid, now_ts)

    async def _poll_tracked_tickets(self) -> None:
        for row in await self._repo.active_tracked_tickets():
            try:
                await self._sync_ticket(row)
            except GlpiError as exc:
                log.warning("sync_ticket_failed id=%s error=%s", row.ticket_id, exc)
            except Exception:  # noqa: BLE001 - one poisoned ticket must not stop the rest
                log.exception("sync_ticket_crashed id=%s", row.ticket_id)

    async def _sync_ticket(self, row: TrackedTicket) -> None:
        ticket = await self._client.get_ticket(row.ticket_id)
        if ticket is None:
            # Deleted in GLPI -> stop watching it.
            await self._repo.set_ticket_status(row.ticket_id, status=row.last_status, active=False)
            return

        if ticket.status != row.last_status:
            done = (TICKET_STATUS_SOLVED, TICKET_STATUS_CLOSED)
            if ticket.status == TICKET_STATUS_SOLVED and row.last_status not in done:
                # Solved from the GLPI web UI: propose the solution to the
                # requester with confirm/return buttons (ITIL cycle).
                await self._propose_solution(row, ticket)
            elif ticket.status == TICKET_STATUS_CLOSED and row.last_status not in done:
                # Closed outright (no confirmation step possible anymore): the
                # requester still deserves the solution text when there is one.
                await self._notify_solved(row, ticket)
            else:
                await notify.notify_status_change(
                    self._bot, row.requester_tg_id, ticket, self._ticket_url(ticket.id)
                )
            # Reflect it on the living card too; skip when a button action
            # already recorded this exact state (no duplicate history line).
            await self._cards.record_event(
                self._bot,
                row.ticket_id,
                texts.hist_status(ticket.status),
                status=ticket.status,
                skip_if_status_unchanged=True,
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
        author_cache: dict[int, str | None] = {}
        for followup in fresh:
            await notify.notify_followup(
                self._bot, row.requester_tg_id, ticket, followup, self._ticket_url(ticket.id)
            )
            author = await self._author_name(followup.users_id, author_cache)
            # These are comments by others than the requester (the filter above),
            # i.e. the team's own activity: history line only, no self-ping.
            await self._cards.record_event(
                self._bot,
                row.ticket_id,
                texts.hist_comment(author),
                followup_id=followup.id,
            )
        # Advance past every followup seen (own/private included) to avoid rework.
        max_id = max(f.id for f in followups)
        if max_id > row.last_followup_id:
            await self._repo.set_ticket_followup_cursor(row.ticket_id, max_id)


def now_ts() -> int:
    return int(time.time())
