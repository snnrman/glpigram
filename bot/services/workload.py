"""A bounded, read-only view of the support queue for an explicitly configured audience."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from datetime import datetime

from ..glpi.models import WorkloadTicket
from ..schedule import WorkSchedule
from ..texts import workload as workload_texts

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WorkloadSnapshot:
    queue_ids: frozenset[int]
    assigned_ids: frozenset[int]
    urgent_unassigned_ids: frozenset[int]
    urgent_assigned_ids: frozenset[int]

    def audit_data(self) -> dict:
        groups = {
            "queue": self.queue_ids,
            "assigned": self.assigned_ids,
            "urgent_unassigned": self.urgent_unassigned_ids,
            "urgent_assigned": self.urgent_assigned_ids,
        }
        return {
            **{f"{name}_count": len(ids) for name, ids in groups.items()},
            **{f"{name}_ids": sorted(ids) for name, ids in groups.items()},
        }


def classify_workload(
    tickets: Iterable[WorkloadTicket],
    *,
    ticket_id: int,
    waiting_ticket_ids: Collection[int],
    excluded_ticket_ids: Collection[int],
    entity_ids: Collection[int],
) -> WorkloadSnapshot:
    """Count distinct active tickets, using actual assignments rather than status labels.

    Duplicate assignment rows belong to one ticket. If duplicate rows disagree
    about eligibility, omit that ticket instead of overstating an uncertain queue.
    """
    omitted = {ticket_id, *waiting_ticket_ids, *excluded_ticket_ids}
    grouped: dict[int, list[WorkloadTicket]] = {}
    for ticket in tickets:
        if (
            ticket.is_deleted
            or ticket.status not in (1, 2)
            or ticket.global_validation in (2, 4)
            or ticket.entity_id not in entity_ids
        ):
            omitted.add(ticket.id)
        grouped.setdefault(ticket.id, []).append(ticket)

    queue: set[int] = set()
    assigned: set[int] = set()
    urgent_unassigned: set[int] = set()
    urgent_assigned: set[int] = set()
    for ident, rows in grouped.items():
        if ident in omitted:
            continue
        has_assignee = any(uid > 0 for row in rows for uid in row.assignee_ids)
        (assigned if has_assignee else queue).add(ident)
        if any(row.urgency == 5 for row in rows):
            (urgent_assigned if has_assignee else urgent_unassigned).add(ident)
    return WorkloadSnapshot(
        frozenset(queue),
        frozenset(assigned),
        frozenset(urgent_unassigned),
        frozenset(urgent_assigned),
    )


class WorkloadService:
    def __init__(
        self,
        client,
        repo,
        settings,
        *,
        schedule: WorkSchedule,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._repo = repo
        self._enabled = settings.workload_notice_enabled
        self._all_users = settings.workload_notice_all_users
        self._pilot_ids = frozenset(settings.workload_notice_pilot_tg_ids)
        self._excluded_ids = frozenset(settings.workload_excluded_ticket_ids)
        self._entity_ids = frozenset(settings.workload_notice_entity_ids)
        self._timeout = settings.workload_notice_timeout
        self._cache_ttl = settings.workload_notice_cache_ttl
        self._schedule = schedule
        self._now = now_provider or schedule.now
        self._refresh_lock = asyncio.Lock()
        self._cached_tickets: tuple[WorkloadTicket, ...] | None = None
        self._captured_at: datetime | None = None

    def _fresh_cache(self) -> bool:
        if self._cached_tickets is None or self._captured_at is None or self._cache_ttl <= 0:
            return False
        age = (self._now() - self._captured_at).total_seconds()
        return 0 <= age <= self._cache_ttl

    async def _snapshot(self) -> tuple[tuple[WorkloadTicket, ...], datetime, bool]:
        if self._fresh_cache():
            return self._cached_tickets, self._captured_at, True
        async with self._refresh_lock:
            # Concurrent requests share the fresh result of the first caller.
            if self._fresh_cache():
                return self._cached_tickets, self._captured_at, True
            # Never return the previous snapshot if the replacement fails.
            self._cached_tickets = None
            self._captured_at = None
            tickets = await self._client.list_workload_tickets(entity_ids=set(self._entity_ids))
            captured_at = self._now()
            self._cached_tickets = tuple(tickets)
            self._captured_at = captured_at
            return self._cached_tickets, captured_at, False

    async def _audit(
        self, *, ticket_id: int, requester_tg_id: int, now: datetime, data: dict, deadline: float
    ) -> None:
        if asyncio.get_running_loop().time() >= deadline:
            return
        try:
            async with asyncio.timeout_at(deadline):
                await self._repo.save_workload_observation(
                    ticket_id, requester_tg_id, int(now.timestamp()), data
                )
        except Exception as exc:
            # Exception messages/tracebacks may contain API or database secrets.
            log.warning("workload_audit_failed error_type=%s", type(exc).__name__)

    async def notice_for(self, *, ticket_id: int, requester_tg_id: int) -> str | None:
        """Return only a supported warning; any unavailable evidence suppresses it.

        An empty allowlist enables nobody unless all-users mode is explicitly
        configured. The operation, including best-effort audit, has one deadline.
        Cancellation from the caller always propagates.
        """
        if (
            not self._enabled
            or type(requester_tg_id) is not int
            or requester_tg_id <= 0
            or (not self._all_users and requester_tg_id not in self._pilot_ids)
        ):
            return None
        now = self._now()
        if not self._schedule.is_working(now):
            return None

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        # Leave a small part of the same deadline for a failure observation.
        audit_reserve = min(0.05, self._timeout / 10)
        notice = None
        try:
            async with asyncio.timeout_at(deadline - audit_reserve):
                tickets, captured_at, cache_hit = await self._snapshot()
                waiting_ids = await self._repo.get_workload_waiting_ticket_ids()
                snapshot = classify_workload(
                    tickets,
                    ticket_id=ticket_id,
                    waiting_ticket_ids=waiting_ids,
                    excluded_ticket_ids=self._excluded_ids,
                    entity_ids=self._entity_ids,
                )
                notice = workload_texts.notice(queue_count=len(snapshot.queue_ids))
                source_ids = {ticket.id for ticket in tickets}
                data = {
                    "status": "ok",
                    "notice_prepared": notice is not None,
                    "snapshot_age_seconds": max(0.0, (self._now() - captured_at).total_seconds()),
                    "cache_hit": cache_hit,
                    "snapshot_ticket_ids": sorted(source_ids),
                    "local_waiting_ticket_ids": sorted(source_ids.intersection(waiting_ids)),
                    "excluded_ticket_ids": sorted(source_ids.intersection(self._excluded_ids)),
                    **snapshot.audit_data(),
                }
        except Exception as exc:
            log.warning("workload_notice_failed error_type=%s", type(exc).__name__)
            notice = None
            data = {"status": "error", "error_type": type(exc).__name__, "notice_prepared": False}
        await self._audit(
            ticket_id=ticket_id,
            requester_tg_id=requester_tg_id,
            now=now,
            data=data,
            deadline=deadline,
        )
        return notice
