"""Entrypoint: build the bot, register routers, run long polling.

Long polling only (the container has no inbound ports / webhooks). Outbound
HTTPS — including the optional ``HTTPS_PROXY`` — is honoured by both the aiogram
session and the GLPI httpx client.
"""

from __future__ import annotations

import asyncio
import logging

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
from .handlers.linking import build_linking_router
from .handlers.new_ticket import build_new_ticket_router
from .logging_setup import setup_logging
from .middleware import AuthMiddleware

log = logging.getLogger(__name__)


async def _set_commands(bot: Bot) -> None:
    """Publish the slash-command menu shown in Telegram clients."""
    await bot.set_my_commands(
        [
            BotCommand(command="new", description=texts.CMD_NEW_DESCRIPTION),
            BotCommand(command="tickets", description=texts.CMD_TICKETS_DESCRIPTION),
        ]
    )


def build_dispatcher(client: GlpiClient, repo: Repo, settings: Settings) -> Dispatcher:
    dp = Dispatcher()
    category_cache: TTLValue = TTLValue(client.list_categories, settings.category_cache_ttl)

    # Linking router first and un-gated: unlinked users must reach /start.
    dp.include_router(
        build_linking_router(
            repo,
            client,
            tech_group_chat_id=settings.tech_group_chat_id,
            tech_group_id=settings.tech_group_id,
        )
    )

    # Business router: gated by AuthMiddleware (requires a linked account).
    business = build_new_ticket_router(
        client, category_cache, ticket_front_base=settings.glpi_front_base
    )
    auth = AuthMiddleware(
        repo,
        client,
        tech_group_id=settings.tech_group_id,
        recheck_ttl=settings.link_recheck_ttl,
    )
    business.message.middleware(auth)
    business.callback_query.middleware(auth)
    dp.include_router(business)
    return dp


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
    await repo.connect()
    dp = build_dispatcher(client, repo, settings)
    try:
        log.info("bot_starting")
        await _set_commands(bot)
        await dp.start_polling(bot, handle_signals=True)
    finally:
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
