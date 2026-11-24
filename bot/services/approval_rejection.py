"""Finish explicit lead refusals without repeating a decision of unknown outcome."""

from __future__ import annotations

import html
import logging
import secrets
import time

from .. import texts
from ..glpi.client import GlpiError
from . import notify

log = logging.getLogger(__name__)


class ApprovalRejectionService:
    def __init__(self, client, repo, *, cards=None, front_base=None):
        self.client, self.repo, self.cards = client, repo, cards
        self.front_base = front_base

    async def reject(self, bot, approval, reason):
        """Caller holds the ticket lock and has verified the deciding lead."""
        tid = approval["ticket_id"]
        saved = await self.repo.get_rejection_closure(tid)
        if saved is not None and saved["phase"] != "cancelled":
            result = await self.finish(bot, tid)
            if result != "waiting":
                return result
            # Only another explicit lead action may retry a PUT whose exact
            # validation is freshly confirmed still waiting. Background recovery
            # never writes the decision again. Keep the original reason/token.
            ticket = await self.client.get_ticket(tid)
            if ticket is None or ticket.status in (5, 6) or ticket.global_validation != 2:
                await self.repo.set_rejection_closure_phase(tid, "cancelled")
                return "cancelled"
            token, reason = saved["token"], saved["reason"]
        else:
            token = secrets.token_hex(16)
            if not await self.repo.reserve_rejection_closure(
                approval, reason, token, int(time.time())
            ):
                return "pending"
        try:
            await self.client.answer_validation(
                approval["validation_id"],
                accepted=False,
                comment=texts.acc_validation_comment_rejected(approval["lead_name"], reason)
                + f"\n[TG rejection:{token}]",
            )
        except GlpiError as exc:
            if exc.response is not None and exc.response.status_code in (400, 401, 403, 404, 422):
                await self.repo.set_rejection_closure_phase(tid, "cancelled")
            raise
        await self.repo.set_rejection_closure_phase(tid, "refused")
        return await self.finish(bot, tid, acknowledged=True)

    async def finish(self, bot, tid, *, acknowledged=False, notify_lead=False):
        """Resume only our recorded refusal of the same ticket and validation."""
        saved = await self.repo.get_rejection_closure(tid)
        if saved is None or saved["phase"] in ("done", "cancelled"):
            return saved["phase"] if saved else "cancelled"
        approval = await self.repo.get_approval(tid)
        if (
            approval is None
            or approval["status"] not in (0, -1)
            or any(
                approval[key] != saved[key]
                for key in ("validation_id", "lead_glpi_id", "lead_tg_id")
            )
            or await self.repo.get_claude_approval(tid) is not None
        ):
            await self.repo.set_rejection_closure_phase(tid, "cancelled")
            return "cancelled"
        if not acknowledged:
            evidence = await self.client.get_validation_decision(saved["validation_id"])
            if evidence["id"] != saved["validation_id"] or evidence["tickets_id"] != tid:
                raise GlpiError("Rejection recovery validation identity mismatch")
            marker = f"[TG rejection:{saved['token']}]"
            if evidence["status"] != 4 or marker not in html.unescape(
                evidence["comment_validation"]
            ):
                # A waiting validation may mean the PUT never reached GLPI.
                # Do not blindly repeat it, or adopt somebody else's refusal.
                if evidence["status"] in (3, 4):
                    await self.repo.set_rejection_closure_phase(tid, "cancelled")
                    return "cancelled"
                return "waiting" if evidence["status"] == 2 else "pending"
            await self.repo.set_rejection_closure_phase(tid, "refused")
            ticket = await self.client.get_ticket(tid)
            if ticket is None or ticket.status == 5 or ticket.global_validation != 4:
                await self.repo.set_rejection_closure_phase(tid, "cancelled")
                return "cancelled"
            closed = ticket.status == 6
        else:
            closed = False
        if not closed:
            await self.client.set_ticket_status(tid, 6)
        await self.repo.set_approval_status(tid, status=-1)
        await self.repo.set_ticket_status(tid, status=6, active=False)
        # The committed outcome survives failures in best-effort Telegram/history.
        await self.repo.set_rejection_closure_phase(tid, "done")
        lead, reason = approval["lead_name"], saved["reason"]
        manual = await self.repo.get_tech_approval(tid) is not None
        try:
            followup_id = await self.client.add_followup(
                tid,
                texts.acc_validation_comment_rejected(lead, reason)
                if manual
                else texts.acc_followup_rejected(lead, reason),
            )
            await self.repo.set_ticket_followup_cursor(tid, followup_id)
        except Exception as exc:
            log.warning("rejection_followup_failed error_type=%s", type(exc).__name__)
        url = f"{self.front_base}/front/ticket.form.php?id={tid}" if self.front_base else None
        if approval["requester_tg_id"]:
            await notify.send_text(
                bot,
                approval["requester_tg_id"],
                texts.tech_approval_decision(tid, lead, False, reason, url)
                if manual
                else texts.acc_requester_rejected(tid, lead, reason, url),
            )
        if notify_lead:
            current = await self.repo.get_by_glpi(approval["lead_glpi_id"])
            if current is not None and current.tg_id == approval["lead_tg_id"]:
                await notify.send_text(
                    bot, current.tg_id, texts.acc_decision_rejected(lead, reason)
                )
        if self.cards:
            await self.cards.record_event(bot, tid, texts.hist_rejected(lead), status=6)
        return "done"

    async def tick(self, bot):
        for tid in await self.repo.pending_rejection_closures():
            try:
                async with self.repo.ticket_action_lock(tid):
                    await self.finish(bot, tid, notify_lead=True)
            except Exception as exc:
                log.warning(
                    "rejection_recovery_pending ticket=%s error_type=%s", tid, type(exc).__name__
                )
