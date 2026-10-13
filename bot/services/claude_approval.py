"""Persistent two-stage Claude approval; ordinary access approvals stay unchanged."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import texts
from ..db.repo import Repo
from ..glpi.client import TICKET_STATUS_CLOSED, GlpiClient, GlpiError
from ..schedule import WorkSchedule
from . import notify
from .cards import CardService

log = logging.getLogger(__name__)
CLAUDE_POLICY_MARKER = "[Claude: lead + designated approver]"


def decision_keyboard(ticket_id: int, role: str) -> InlineKeyboardMarkup:
    prefix = "ap" if role == "lead" else "ce"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.BTN_ACC_APPROVE, callback_data=f"{prefix}:ok:{ticket_id}"
                ),
                InlineKeyboardButton(
                    text=texts.BTN_ACC_REJECT, callback_data=f"{prefix}:no:{ticket_id}"
                ),
            ]
        ]
    )


class ClaudeApprovalService:
    def __init__(
        self,
        client: GlpiClient,
        repo: Repo,
        *,
        approver_ids: str = "",
        front_base: str | None = None,
        cards: CardService | None = None,
        schedule: WorkSchedule,
        reminder_hours: float = 4,
    ):
        self.client = client
        self.repo = repo
        self.front_base = front_base
        self.cards = cards
        self.schedule = schedule
        self.reminder_seconds = reminder_hours * 3600
        self._locks: dict[int, asyncio.Lock] = {}
        try:
            self.approver_ids = list(
                dict.fromkeys(int(x.strip()) for x in approver_ids.split(",") if x.strip())
            )
        except ValueError:
            self.approver_ids = []
            log.error("claude_approvers_invalid_config")

    def _lock(self, ticket_id: int) -> asyncio.Lock:
        return self._locks.setdefault(ticket_id, asyncio.Lock())

    def url(self, ticket_id: int) -> str | None:
        return (
            f"{self.front_base}/front/ticket.form.php?id={ticket_id}" if self.front_base else None
        )

    async def available_members(self, requester_tg_id: int) -> list[tuple[int, int, str]]:
        members = []
        for glpi_id in self.approver_ids:
            user = await self.client.get_user(glpi_id)
            link = await self.repo.get_by_glpi(glpi_id)
            if user is not None and user.is_usable and link and link.tg_id != requester_tg_id:
                members.append((glpi_id, link.tg_id, user.display_name))
        return members

    async def authorise(self, ticket_id: int, role: str, tg_id: int):
        row = await self.repo.get_claude_approval(ticket_id)
        expected = "lead" if role == "lead" else "extra"
        if row is None or row["stage"] != expected or tg_id == row["requester_tg_id"]:
            return None
        link = await self.repo.get_by_tg(tg_id)
        if link is None or link.glpi_users_id == row["requester_glpi_id"]:
            return None
        if role == "lead":
            glpi_id, name = row["lead_glpi_id"], row["lead_name"]
            if link.glpi_users_id != glpi_id:
                return None
        else:
            member = next(
                (
                    m
                    for m in await self.repo.claude_members(ticket_id)
                    if m["glpi_id"] == link.glpi_users_id
                ),
                None,
            )
            if member is None:
                return None
            glpi_id, name = member["glpi_id"], member["name"]
        try:
            user = await self.client.get_user(glpi_id)
            ticket = await self.client.get_ticket(ticket_id)
        except GlpiError:
            log.warning("claude_authorisation_api_failed ticket=%s", ticket_id)
            return None
        if user is None or not user.is_usable or ticket is None:
            return None
        if ticket.status == TICKET_STATUS_CLOSED or ticket.global_validation != 2:
            return None
        return row, name

    async def _lead_has_service_authority(self, row) -> bool:
        # The GLPI identities belong to this request's original recipient set.
        return any(
            m["glpi_id"] == row["lead_glpi_id"]
            for m in await self.repo.claude_members(row["ticket_id"])
        )

    async def approve(self, bot: Bot, ticket_id: int, role: str, tg_id: int) -> str:
        async with self._lock(ticket_id):
            actor = await self.authorise(ticket_id, role, tg_id)
            if actor is None:
                return "stale"
            row, name = actor
            now = int(time.time())
            covers_service = role == "lead" and await self._lead_has_service_authority(row)
            stage = "extra" if role == "lead" and not covers_service else "approve_pending"
            if not await self.repo.transition_claude(
                ticket_id,
                expected=row["stage"],
                stage=stage,
                now=now,
                actor_tg_id=tg_id,
                actor_name=name,
                lead_covers_service=covers_service,
            ):
                return "stale"
            if role == "lead":
                await self._ensure_lead_audit(bot, await self.repo.get_claude_approval(ticket_id))
                if stage == "extra":
                    await self._send_pending(bot, await self.repo.get_claude_approval(ticket_id))
                    return "lead_approved"
            return await self._finalise(bot, await self.repo.get_claude_approval(ticket_id))

    async def reject(self, bot: Bot, ticket_id: int, role: str, tg_id: int, reason: str) -> str:
        async with self._lock(ticket_id):
            actor = await self.authorise(ticket_id, role, tg_id)
            if actor is None or not reason.strip():
                return "stale"
            row, name = actor
            if not await self.repo.transition_claude(
                ticket_id,
                expected=row["stage"],
                stage="reject_pending",
                now=int(time.time()),
                actor_tg_id=tg_id,
                actor_name=name,
                reason=reason.strip(),
            ):
                return "stale"
            return await self._finalise(bot, await self.repo.get_claude_approval(ticket_id))

    async def _finalise(self, bot: Bot, row) -> str:
        ticket_id = row["ticket_id"]
        accepted = row["stage"] == "approve_pending"
        decision = texts.claude_final_decision(
            row["lead_name"],
            row["actor_name"],
            accepted,
            row["reason"],
            lead_covers_service=bool(row["lead_covers_service"]),
        )
        try:
            if not row["validation_done"]:
                await self.client.answer_validation(
                    row["validation_id"], accepted=accepted, comment=decision
                )
                await self.repo.mark_claude_validation_done(ticket_id)
            if not accepted:
                await self.client.set_ticket_status(ticket_id, TICKET_STATUS_CLOSED)
            await self.repo.finish_claude(ticket_id, accepted=accepted)
        except GlpiError:
            log.warning("claude_finalisation_retry ticket=%s", ticket_id)
            return "retry"
        await self._notify_complete(bot, await self.repo.get_claude_approval(ticket_id))
        return "approved" if accepted else "rejected"

    async def _followup(self, ticket_id: int, content: str) -> bool:
        try:
            fid = await self.client.add_followup(ticket_id, content)
            await self.repo.set_ticket_followup_cursor(ticket_id, fid)
            return True
        except GlpiError:
            log.warning("claude_audit_retry ticket=%s", ticket_id)
            return False

    async def _ensure_lead_audit(self, bot: Bot, row) -> bool:
        if row["lead_approved_at"] == 0 or row["lead_audit_done"]:
            return True
        note = texts.claude_lead_record(
            row["lead_name"], covers_service=bool(row["lead_covers_service"])
        )
        if not await self._followup(row["ticket_id"], note):
            return False
        if self.cards:
            await self.cards.record_event(bot, row["ticket_id"], note)
        await self.repo.mark_claude_lead_audited(row["ticket_id"])
        return True

    async def _notify_complete(self, bot: Bot, row) -> None:
        if not await self._ensure_lead_audit(bot, row):
            return
        accepted = row["stage"] == "approved"
        decision = texts.claude_final_decision(
            row["lead_name"],
            row["actor_name"],
            accepted,
            row["reason"],
            lead_covers_service=bool(row["lead_covers_service"]),
        )
        if not row["audit_done"]:
            if await self._followup(row["ticket_id"], decision):
                if self.cards:
                    kwargs = {} if accepted else {"status": TICKET_STATUS_CLOSED}
                    await self.cards.record_event(bot, row["ticket_id"], decision, **kwargs)
                await self.repo.mark_claude_audited(row["ticket_id"])
            else:
                return
        # Removing both sets of buttons is cosmetic; authorisation reads the
        # persistent stage even if an edit fails or an old message is replayed.
        targets = [(row["lead_glpi_id"], row["lead_tg_id"], row["dm_message_id"])]
        targets += [
            (m["glpi_id"], m["tg_id"], m["dm_message_id"])
            for m in await self.repo.claude_members(row["ticket_id"])
        ]
        for glpi_id, chat_id, message_id in targets:
            recipient = await self._active_link(glpi_id)
            if recipient is None or recipient.tg_id != chat_id:
                continue
            if message_id is None:
                continue
            try:
                await bot.edit_message_text(
                    text=texts.claude_completion(
                        row["ticket_id"], decision, self.url(row["ticket_id"])
                    ),
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=None,
                )
            except Exception:
                log.warning("claude_decision_edit_failed ticket=%s", row["ticket_id"])
        recipient = await self._active_link(row["requester_glpi_id"])
        if recipient is None:
            return
        sent = await notify.send_text(
            bot,
            recipient.tg_id,
            texts.claude_completion(row["ticket_id"], decision, self.url(row["ticket_id"])),
        )
        if sent is not None:
            await self.repo.mark_claude_notified(row["ticket_id"])

    def _reminder_due(self, row, now: datetime, last: int) -> bool:
        if not self.schedule.is_working(now):
            return False
        anchor = max(row["stage_started_at"], last)
        return (
            self.schedule.working_seconds_between(datetime.fromtimestamp(anchor, tz=UTC), now)
            >= self.reminder_seconds
        )

    async def _active_link(self, glpi_id: int):
        link = await self.repo.get_by_glpi(glpi_id)
        if link is None:
            return None
        try:
            user = await self.client.get_user(glpi_id)
        except GlpiError:
            return None
        return link if user is not None and user.is_usable else None

    async def _send_pending(self, bot: Bot, row) -> None:
        now = datetime.now(UTC)
        link = await self.repo.get_by_glpi(row["requester_glpi_id"])
        requester = link.display_name if link else texts.CLAUDE_REQUESTER_FALLBACK
        if row["stage"] == "lead":
            recipient = await self._active_link(row["lead_glpi_id"])
            if recipient is None:
                return
            if recipient.tg_id != row["lead_tg_id"]:
                await self.repo.refresh_claude_recipient(
                    row["ticket_id"], row["lead_glpi_id"], recipient.tg_id, lead=True
                )
                row = await self.repo.get_claude_approval(row["ticket_id"])
            if row["dm_message_id"] is not None and not self._reminder_due(
                row, now, row["reminded_at"]
            ):
                return
            prompt = texts.acc_lead_prompt(
                ticket_id=row["ticket_id"],
                requester=requester,
                request=row["request_text"],
                url=self.url(row["ticket_id"]),
                requester_tg_id=link.tg_id if link else None,
            )
            sent = await notify.send_text(
                bot,
                row["lead_tg_id"],
                prompt
                + "\n\n"
                + (
                    texts.CLAUDE_CONFIRM_COVERED
                    if await self._lead_has_service_authority(row)
                    else texts.CLAUDE_CONFIRM_EXTRA
                ),
                reply_markup=decision_keyboard(row["ticket_id"], "lead"),
            )
            if sent:
                await self.repo.set_approval_dm(row["ticket_id"], sent.message_id)
                await self.repo.set_approval_reminded(row["ticket_id"], int(now.timestamp()))
        elif row["stage"] == "extra":
            for member in await self.repo.claude_members(row["ticket_id"]):
                recipient = await self._active_link(member["glpi_id"])
                if recipient is None:
                    continue
                if recipient.tg_id != member["tg_id"]:
                    await self.repo.refresh_claude_recipient(
                        row["ticket_id"], member["glpi_id"], recipient.tg_id, lead=False
                    )
                    member = dict(member)
                    member.update(tg_id=recipient.tg_id, dm_message_id=None, notified_at=0)
                if member["dm_message_id"] is not None and not self._reminder_due(
                    row, now, member["notified_at"]
                ):
                    continue
                sent = await notify.send_text(
                    bot,
                    member["tg_id"],
                    texts.claude_extra_prompt(
                        row["ticket_id"],
                        row["lead_name"],
                        row["request_text"],
                        self.url(row["ticket_id"]),
                        requester=requester,
                        requester_tg_id=link.tg_id if link else None,
                    ),
                    reply_markup=decision_keyboard(row["ticket_id"], "extra"),
                )
                if sent:
                    await self.repo.set_claude_member_dm(
                        row["ticket_id"],
                        member["glpi_id"],
                        message_id=sent.message_id,
                        now=int(now.timestamp()),
                    )

    async def send_pending(self, bot: Bot, ticket_id: int) -> None:
        async with self._lock(ticket_id):
            row = await self.repo.get_claude_approval(ticket_id)
            if row is not None:
                await self._send_pending(bot, row)

    async def tick(self, bot: Bot) -> None:
        for snapshot in await self.repo.list_claude_approvals():
            ticket_id = snapshot["ticket_id"]
            try:
                async with self._lock(ticket_id):
                    row = await self.repo.get_claude_approval(ticket_id)
                    await self._ensure_lead_audit(bot, row)
                    if row["stage"] in ("approve_pending", "reject_pending"):
                        await self._finalise(bot, row)
                    elif row["stage"] in ("approved", "rejected") and not row["notification_done"]:
                        await self._notify_complete(bot, row)
                    elif row["stage"] in ("lead", "extra"):
                        ticket = await self.client.get_ticket(ticket_id)
                        if (
                            ticket is None
                            or ticket.status == TICKET_STATUS_CLOSED
                            or ticket.global_validation != 2
                        ):
                            await self.repo.cancel_claude(ticket_id)
                        elif (
                            row["stage"] == "extra"
                            and row["lead_approved_at"] > 0
                            and await self._lead_has_service_authority(row)
                        ):
                            if await self.repo.transition_claude(
                                ticket_id,
                                expected="extra",
                                stage="approve_pending",
                                now=int(time.time()),
                                actor_tg_id=row["actor_tg_id"],
                                actor_name=row["lead_name"],
                                lead_covers_service=True,
                            ):
                                await self._finalise(
                                    bot, await self.repo.get_claude_approval(ticket_id)
                                )
                        else:
                            await self._send_pending(bot, row)
            except Exception:
                log.exception("claude_sync_failed ticket=%s", ticket_id)
