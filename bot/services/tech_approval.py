"""Durable, explicitly technician-initiated approval of an existing ticket."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import secrets
import time

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import texts
from ..glpi.client import GlpiError
from . import notify
from .access_target import classify_access_target

log = logging.getLogger(__name__)


class ApprovalUnavailable(Exception):
    pass


def ticket_digest(ticket) -> str:
    return hashlib.sha256(f"{ticket.name}\n{ticket.content}".encode()).hexdigest()


def request_summary(ticket) -> str:
    body = re.sub(r"<[^>]*>", " ", html.unescape(ticket.content or ""))
    result = f"{ticket.name}\n\n{html.unescape(body)}".strip()[:1800]
    while len(html.escape(result)) > 2400:
        result = result[:-100]
    return result


class TechApprovalService:
    def __init__(self, client, repo, claude, *, cards=None, front_base=None):
        self.client = client
        self.repo = repo
        self.claude = claude
        self.cards = cards
        self.front_base = front_base

    def url(self, ticket_id):
        return (
            f"{self.front_base}/front/ticket.form.php?id={ticket_id}" if self.front_base else None
        )

    async def prepare(self, ticket_id):
        previous = await self.repo.get_tech_approval(ticket_id)
        if await self.repo.get_approval(ticket_id) or (
            previous is not None and previous["phase"] != "failed"
        ):
            raise ApprovalUnavailable(texts.TECH_APPROVAL_EXISTS)
        ticket = await self.client.get_ticket(ticket_id)
        if ticket is None or ticket.status in (5, 6):
            raise ApprovalUnavailable(texts.TECH_APPROVAL_CLOSED)
        if ticket.global_validation not in (0, 1):
            raise ApprovalUnavailable(texts.TECH_APPROVAL_EXISTS)
        requester = await self.client.get_ticket_requester(ticket_id)
        if requester is None:
            raise ApprovalUnavailable(texts.TECH_APPROVAL_NO_REQUESTER)
        uid, name = requester
        link = await self.repo.get_by_glpi(uid)
        return {
            "ticket_id": ticket_id,
            "digest": ticket_digest(ticket),
            "request": request_summary(ticket),
            "service": classify_access_target(
                f"{ticket.name}\n{html.unescape(ticket.content or '')}"
            ),
            "requester_glpi_id": uid,
            "requester_name": name,
            "requester_tg_id": link.tg_id if link else 0,
        }

    async def submit(self, bot, data, tech):
        tid = data["ticket_id"]
        if not tech.is_tech:
            raise ApprovalUnavailable(texts.TECH_ONLY)
        async with self.repo.ticket_action_lock(tid):
            # Re-check the original ticket, requester and recipient at confirmation time.
            current = await self.prepare(tid)
            if any(
                current[k] != data[k] for k in ("digest", "requester_glpi_id", "requester_tg_id")
            ):
                raise ApprovalUnavailable(texts.TECH_APPROVAL_CHANGED)
            lead = await self.repo.get_by_glpi(data["lead_glpi_id"])
            user = await self.client.get_user(data["lead_glpi_id"])
            if (
                lead is None
                or user is None
                or not user.is_usable
                or lead.tg_id != data["lead_tg_id"]
                or lead.glpi_users_id == data["requester_glpi_id"]
            ):
                raise ApprovalUnavailable(texts.ACC_NO_LEADS)
            payload = {**data, "tech_name": tech.display_name, "tech_tg_id": tech.tg_id}
            if data["service"] == "claude":
                payload["members"] = [
                    m
                    for m in await self.claude.available_members(data["requester_tg_id"])
                    if m[0] != data["requester_glpi_id"]
                ]
                if not payload["members"]:
                    raise ApprovalUnavailable(texts.CLAUDE_APPROVERS_UNAVAILABLE)
            await self.repo.reserve_tech_approval(
                tid,
                secrets.token_hex(16),
                json.dumps(payload, ensure_ascii=False),
                int(time.time()),
            )
            try:
                return await self._advance(bot, tid)
            except Exception:
                log.warning("tech_approval_registration_pending ticket=%s", tid, exc_info=True)
                return "uncertain"

    async def _advance(self, bot, tid):
        row = await self.repo.get_tech_approval(tid)
        data = json.loads(row["payload"])
        ticket = await self.client.get_ticket(tid)
        if ticket is None or ticket.status in (5, 6):
            await self.repo.update_tech_approval(tid, "cancelled")
            return "cancelled"
        marker = f"[TG tech approval:{row['token']}]"
        validation_id = row["validation_id"]
        if validation_id is None:
            if row["phase"] == "prepared":
                requester = await self.client.get_ticket_requester(tid)
                if (
                    ticket.global_validation not in (0, 1)
                    or ticket_digest(ticket) != data["digest"]
                    or requester is None
                    or requester[0] != data["requester_glpi_id"]
                ):
                    await self.repo.update_tech_approval(tid, "cancelled")
                    return "cancelled"
                # Persist BEFORE the non-idempotent request. On a timeout or restart
                # only a matching existing validation may be recovered.
                await self.repo.update_tech_approval(tid, "posting")
                try:
                    validation_id = await self.client.create_validation(
                        tid,
                        comment=texts.tech_approval_audit(data["tech_name"], data["lead_name"])
                        + "\n"
                        + marker,
                    )
                except GlpiError as exc:
                    if exc.response is not None and exc.response.status_code in (
                        400,
                        403,
                        404,
                        422,
                    ):
                        await self.repo.update_tech_approval(tid, "failed")
                        return "failed"
                    log.warning("tech_approval_post_uncertain ticket=%s", tid)
                    return "uncertain"
            else:
                validation_id = await self.client.find_validation(tid, marker)
                if validation_id is None:
                    return "uncertain"
            await self.repo.update_tech_approval(tid, "posting", validation_id)
        approval = await self.repo.get_approval(tid)
        if approval is None:
            now = int(time.time())
            common = dict(
                validation_id=validation_id,
                lead_glpi_id=data["lead_glpi_id"],
                lead_tg_id=data["lead_tg_id"],
                lead_name=data["lead_name"],
                requester_tg_id=data["requester_tg_id"],
                now=now,
            )
            tracked = await self.repo.get_tracked_ticket(tid)
            if data["service"] == "claude":
                await self.repo.create_claude_approval(
                    tid,
                    **common,
                    requester_glpi_id=data["requester_glpi_id"],
                    request_text=data["request"],
                    members=[tuple(m) for m in data["members"]],
                )
                if tracked is None:
                    await self.repo.set_ticket_status(tid, status=ticket.status, active=True)
            else:
                if tracked is None and data["requester_tg_id"]:
                    await self.repo.track_ticket(
                        ticket_id=tid,
                        requester_tg_id=data["requester_tg_id"],
                        requester_glpi_id=data["requester_glpi_id"],
                        status=ticket.status,
                        now=now,
                    )
                await self.repo.create_approval(tid, **common)
            if self.cards:
                await self.cards.record_event(
                    bot, tid, texts.tech_approval_history(data["tech_name"], data["lead_name"])
                )
        elif approval["validation_id"] != validation_id:
            log.error("tech_approval_conflict ticket=%s", tid)
            return "uncertain"
        elif approval["status"] != 0:
            await self.repo.update_tech_approval(tid, "done")
            return "done"
        await self.repo.update_tech_approval(tid, "registered")
        if data["service"] == "claude":
            await self.claude.send_pending(bot, tid)
        else:
            recipient = await self.repo.get_by_glpi(data["lead_glpi_id"])
            user = await self.client.get_user(data["lead_glpi_id"])
            if (
                recipient is None
                or recipient.tg_id != data["lead_tg_id"]
                or user is None
                or not user.is_usable
            ):
                return "delivery_pending"
            approval = await self.repo.get_approval(tid)
            if approval["dm_message_id"] is None:
                buttons = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=texts.BTN_ACC_APPROVE, callback_data=f"ap:ok:{tid}"
                            ),
                            InlineKeyboardButton(
                                text=texts.BTN_ACC_REJECT, callback_data=f"ap:no:{tid}"
                            ),
                        ]
                    ]
                )
                sent = await notify.send_text(
                    bot,
                    data["lead_tg_id"],
                    texts.tech_approval_lead_prompt(
                        tid,
                        data["requester_name"],
                        data["requester_tg_id"] or None,
                        data["request"],
                        data["tech_name"],
                        self.url(tid),
                    ),
                    reply_markup=buttons,
                )
                if sent:
                    await self.repo.set_approval_dm(tid, sent.message_id)
        approval = await self.repo.get_approval(tid)
        if approval["dm_message_id"] is None:
            return "delivery_pending"
        if data["requester_tg_id"]:
            await notify.send_text(
                bot,
                data["requester_tg_id"],
                texts.tech_approval_sent(tid, data["lead_name"], self.url(tid)),
            )
        await self.repo.update_tech_approval(tid, "done")
        return "done"

    async def tick(self, bot):
        for tid in await self.repo.pending_tech_approvals():
            try:
                async with self.repo.ticket_action_lock(tid):
                    await self._advance(bot, tid)
            except Exception:
                log.warning("tech_approval_recovery_pending ticket=%s", tid, exc_info=True)
