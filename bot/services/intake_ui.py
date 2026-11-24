"""Per-update presentation mode; no process-global menu changes across dispatchers."""

from __future__ import annotations

from collections.abc import Collection
from contextvars import ContextVar
from dataclasses import dataclass

from aiogram import BaseMiddleware

single_text_mode: ContextVar[bool] = ContextVar("single_text_mode", default=False)


@dataclass(frozen=True, slots=True)
class IntakeEligibility:
    enabled: bool
    pilot_tg_ids: frozenset[int] = frozenset()

    def allows(self, tg_id: int | None) -> bool:
        """Use Telegram's authenticated sender ID, never a name or GLPI role."""
        return (
            self.enabled
            and type(tg_id) is int
            and tg_id > 0
            and (not self.pilot_tg_ids or tg_id in self.pilot_tg_ids)
        )


_eligibility: ContextVar[IntakeEligibility | None] = ContextVar("intake_eligibility", default=None)


def use_single_text_mode(tg_id: int | None = None) -> bool:
    """Allow a menu sent to another user to follow its recipient's rollout."""
    if tg_id is None:
        return single_text_mode.get()
    eligibility = _eligibility.get()
    return eligibility is not None and eligibility.allows(tg_id)


class IntakeUIMiddleware(BaseMiddleware):
    def __init__(self, enabled: bool, pilot_tg_ids: Collection[int] = ()) -> None:
        self.eligibility = IntakeEligibility(enabled, frozenset(pilot_tg_ids))

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        token = single_text_mode.set(self.eligibility.allows(getattr(user, "id", None)))
        rollout_token = _eligibility.set(self.eligibility)
        try:
            return await handler(event, data)
        finally:
            _eligibility.reset(rollout_token)
            single_text_mode.reset(token)
