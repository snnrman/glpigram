"""Entrypoint: build the bot, register routers, run long polling.

Long polling only (the container has no inbound ports / webhooks). Outbound
HTTPS — including the optional ``HTTPS_PROXY`` — is honoured by both the aiogram
session and the GLPI httpx client.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from . import texts
from .cache import TTLValue
from .config import Settings, load_settings
from .db.repo import Repo
from .glpi.client import GlpiClient
from .handlers.fallback import build_fallback_router, register_error_handler
from .handlers.linking import build_linking_router
from .handlers.my_tickets import build_my_tickets_router
from .handlers.new_ticket import build_new_ticket_router
from .handlers.stats import build_stats_router
from .handlers.tech_actions import build_tech_actions_router
from .handlers.tech_tickets import build_tech_tickets_router
from .logging_setup import setup_logging
from .middleware import AuthMiddleware
from .schedule import WorkSchedule
from .services.cards import CardService
from .services.sync import SyncService

log = logging.getLogger(__name__)


async def _set_commands(bot: Bot) -> None:
    """Publish the slash-command menu shown in Telegram clients."""
    await bot.set_my_commands(
        [
            BotCommand(command="new", description=texts.CMD_NEW_DESCRIPTION),
            BotCommand(command="tickets", description=texts.CMD_TICKETS_DESCRIPTION),
        ]
    )


def _work_schedule(settings: Settings) -> WorkSchedule:
    return WorkSchedule.from_config(
        settings.work_hours, settings.work_days, tz_name=os.environ.get("TZ")
    )


def build_dispatcher(client: GlpiClient, repo: Repo, settings: Settings) -> Dispatcher:
    dp = Dispatcher()
    category_cache: TTLValue = TTLValue(client.list_categories, settings.category_cache_ttl)
    schedule = _work_schedule(settings)

    # Linking router first and un-gated: unlinked users must reach /start.
    dp.include_router(
        build_linking_router(
            repo,
            client,
            tech_group_chat_id=settings.tech_group_chat_id,
            tech_group_id=settings.tech_group_id,
        )
    )

    # Business routers: gated by AuthMiddleware (require a linked account).
    auth = AuthMiddleware(
        repo,
        client,
        tech_group_id=settings.tech_group_id,
        recheck_ttl=settings.link_recheck_ttl,
    )
    # Order matters: tech_actions and my_tickets must precede new_ticket so their
    # FSM-state and menu-button handlers win over the /new free-text fallback.
    cards = CardService(repo, front_base=settings.glpi_front_base)
    tech = build_tech_actions_router(
        client, tech_group_chat_id=settings.tech_group_chat_id, cards=cards, repo=repo
    )
    my_tickets = build_my_tickets_router(
        client,
        repo,
        tech_group_chat_id=settings.tech_group_chat_id,
        ticket_front_base=settings.glpi_front_base,
        remind_cooldown_hours=settings.remind_cooldown_hours,
        schedule=schedule,
        cards=cards,
    )
    stats = build_stats_router(client, repo)
    tech_tickets = build_tech_tickets_router(client, ticket_front_base=settings.glpi_front_base)
    business = build_new_ticket_router(
        client,
        category_cache,
        repo,
        ticket_front_base=settings.glpi_front_base,
        schedule=schedule,
    )
    for router in (tech, tech_tickets, my_tickets, stats, business):
        router.message.middleware(auth)
        router.callback_query.middleware(auth)
        dp.include_router(router)

    # Last: stale-button catch-all + generic reply on unhandled exceptions.
    dp.include_router(build_fallback_router())
    register_error_handler(dp)
    return dp


def _sync_task_died(task: asyncio.Task) -> None:
    """The sync loop must never finish on its own — scream if it does."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.critical("sync_task_died — no GLPI->TG notifications until restart!", exc_info=exc)


async def _run(settings: Settings) -> None:
    session = AiohttpSession(proxy=settings.https_proxy) if settings.https_proxy else None
    bot = Bot(
        token=settings.telegram_bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    client = GlpiClient(
        base_url=settings.glpi_api_url,
        app_token=settings.glpi_app_token,
        user_token=settings.glpi_user_token,
        timeout=settings.glpi_timeout,
        proxy=settings.https_proxy,
    )
    repo = Repo(settings.db_path)
    sync_task: asyncio.Task | None = None
    # Everything after resource creation runs under the finally so a startup
    # failure (DB perms, bad schedule config) still closes the HTTP sessions.
    try:
        await repo.connect()
        dp = build_dispatcher(client, repo, settings)
        sync = SyncService(
            bot,
            client,
            repo,
            tech_group_chat_id=settings.tech_group_chat_id,
            schedule=_work_schedule(settings),
            interval=settings.sync_interval,
            front_base=settings.glpi_front_base,
            unassigned_remind_hours=settings.unassigned_remind_hours,
            remind_interval_hours=settings.remind_interval_hours,
        )
        sync_task = asyncio.create_task(sync.run(), name="glpi_sync")
        sync_task.add_done_callback(_sync_task_died)
        log.info("bot_starting")
        await _set_commands(bot)
        await dp.start_polling(bot, handle_signals=True)
    finally:
        if sync_task is not None:
            sync_task.cancel()
            with suppress(asyncio.CancelledError):
                await sync_task
        await repo.close()
        await client.kill_session()
        await client.close()
        await bot.session.close()
        log.info("bot_stopped")


def main() -> None:
    settings = load_settings()  # fails fast if required secrets are missing
    setup_logging(settings.log_level)
    asyncio.run(_run(settings))


if __name__ == "__main__":
    main()
