"""/stats — open-queue statistics, technicians only.

The «📊 Статистика» menu button is shown only to techs (role-aware
``main_menu_keyboard``), but hiding a button is not access control: the
handler re-checks ``link.is_tech`` itself, so a direct /stats (or the button
text typed by hand) is refused for regular users. ``link`` comes from
:class:`AuthMiddleware`, which refreshes the role from the GLPI group.
"""

from __future__ import annotations

import logging
import time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from .. import texts
from ..db.repo import LinkedUser, Repo
from ..glpi.client import GlpiClient, GlpiError
from .new_ticket import main_menu_keyboard

log = logging.getLogger(__name__)


def build_stats_router(client: GlpiClient, repo: Repo) -> Router:
    router = Router(name="stats")

    @router.message(Command("stats"))
    @router.message(F.text == texts.BTN_STATS)
    async def cmd_stats(message: Message, link: LinkedUser) -> None:
        if not link.is_tech:
            await message.answer(texts.STATS_TECH_ONLY)
            return
        try:
            counts = await client.count_open_tickets_by_status()
        except GlpiError as exc:
            log.warning("stats_failed error=%s raw=%s", exc, exc.raw)
            await message.answer(texts.GLPI_ERROR)
            return
        # Users section comes from the bot's own SQLite; a failure there must
        # not hide the ticket stats — and must not be swallowed silently either.
        try:
            total, techs, recent = await repo.user_stats(now=int(time.time()))
            users_block = texts.stats_users_block(total, techs, recent)
        except Exception:
            log.exception("stats_users_failed")
            users_block = texts.STATS_USERS_UNAVAILABLE
        await message.answer(
            f"{texts.stats_summary(counts)}\n\n{users_block}",
            reply_markup=main_menu_keyboard(is_tech=True),
        )

    return router
