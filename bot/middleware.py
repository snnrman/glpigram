"""Authorisation middleware: gate business handlers behind account linking.

Attached to the business routers (``new_ticket`` and, later, ``my_tickets`` /
``tech_actions``). The linking router is intentionally *not* gated so unlinked
users can still run ``/start`` and complete linking.

Responsibilities per event:

* resolve the linked user for the sender; unlinked -> answer "please /start"
  and stop propagation;
* auto-unlink: periodically (``recheck_ttl``) re-check the GLPI account is still
  active; if deactivated/deleted, silently unlink and treat as never-linked
  (the offboarding path);
* refresh ``is_tech`` from the configured GLPI group on the same schedule;
* inject the ``LinkedUser`` into handler data as ``link``.

Transient GLPI errors during a re-check never lock a user out: the cached link
is kept and the check is retried next time.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from . import texts
from .db.repo import LinkedUser, Repo
from .glpi.client import GlpiClient, GlpiError

log = logging.getLogger(__name__)


class AuthMiddleware(BaseMiddleware):
    def __init__(
        self,
        repo: Repo,
        client: GlpiClient,
        *,
        tech_group_id: int | None,
        recheck_ttl: int,
    ) -> None:
        self._repo = repo
        self._client = client
        self._tech_group_id = tech_group_id
        self._recheck_ttl = recheck_ttl

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:  # no sender to authorise (shouldn't happen for msg/cb)
            return await handler(event, data)

        link = await self._repo.get_by_tg(user.id)
        if link is not None:
            link = await self._recheck(link)

        if link is None:
            await self._deny(event)
            return None

        data["link"] = link
        return await handler(event, data)

    async def _recheck(self, link: LinkedUser) -> LinkedUser | None:
        """Re-validate a stale link against GLPI; unlink if the account is gone."""
        now = int(time.time())
        if now - link.checked_at < self._recheck_ttl:
            return link
        try:
            glpi_user = await self._client.get_user(link.glpi_users_id)
            if glpi_user is None or not glpi_user.is_usable:
                await self._repo.unlink_tg(link.tg_id)
                log.info("auto_unlink tg_id=%s glpi_id=%s", link.tg_id, link.glpi_users_id)
                return None
            is_tech = await self._is_tech(link.glpi_users_id)
        except GlpiError as exc:
            # Transient failure: keep the cached link, retry on the next event.
            log.warning("link_recheck_failed glpi_id=%s error=%s", link.glpi_users_id, exc)
            return link
        await self._repo.set_tech_checked(link.tg_id, is_tech=is_tech, now=now)
        link.is_tech = is_tech
        link.checked_at = now
        return link

    async def _is_tech(self, glpi_users_id: int) -> bool:
        if self._tech_group_id is None:
            return False
        return await self._client.user_in_group(glpi_users_id, self._tech_group_id)

    @staticmethod
    async def _deny(event: TelegramObject) -> None:
        if isinstance(event, CallbackQuery):
            await event.answer(texts.NEED_LINK, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(texts.NEED_LINK)
