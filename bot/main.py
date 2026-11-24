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
from types import SimpleNamespace

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import BotCommand

from . import texts
from .cache import TTLValue
from .config import Settings, load_settings
from .db.repo import Repo
from .glpi.client import GlpiClient, GlpiError
from .handlers.access import build_access_router, build_lead_directory
from .handlers.fallback import build_fallback_router, register_error_handler
from .handlers.intake_review import build_intake_review_router
from .handlers.linking import build_linking_router
from .handlers.my_tickets import build_my_tickets_router
from .handlers.new_ticket import build_new_ticket_router
from .handlers.stats import build_stats_router
from .handlers.tech_actions import build_tech_actions_router
from .handlers.tech_approval import build_tech_approval_router
from .handlers.tech_tickets import build_tech_tickets_router
from .logging_setup import setup_logging
from .middleware import AuthMiddleware
from .schedule import WorkSchedule
from .services.ai_shadow import OpenAIClassifier, ShadowClassifier
from .services.cards import CardService
from .services.claude_approval import ClaudeApprovalService
from .services.intake import IntakeService
from .services.intake_ui import IntakeUIMiddleware
from .services.profile_links import ProfileLinksMiddleware
from .services.sync import SyncService
from .services.tech_approval import TechApprovalService
from .services.workload import WorkloadService

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


def build_dispatcher(
    client: GlpiClient,
    repo: Repo,
    settings: Settings,
    *,
    shadow: ShadowClassifier | None = None,
    intake: IntakeService | None = None,
    workload: WorkloadService | None = None,
) -> Dispatcher:
    if settings.ai_intake_enabled and intake is None:
        raise ValueError("Enabled intake requires an IntakeService")
    active_intake = intake if settings.ai_intake_enabled else None
    repo.intake_category_ids = dict(settings.ai_intake_category_ids)
    pilot_tg_ids = frozenset(settings.ai_intake_pilot_tg_ids)
    repo.intake_recovery_filter = None
    if pilot_tg_ids:

        async def may_recover_intake(ticket_id: int) -> bool:
            requester = await client.get_ticket_requester(ticket_id)
            if requester is None:
                raise GlpiError("Cannot verify intake recovery requester")
            link = await repo.get_by_glpi(requester[0])
            return link is not None and link.tg_id in pilot_tg_ids

        repo.intake_recovery_filter = may_recover_intake
    # A second click or /new must not replace the FSM while AI/GLPI is awaiting I/O.
    dp = Dispatcher(events_isolation=SimpleEventIsolation())
    intake_ui = IntakeUIMiddleware(active_intake is not None, pilot_tg_ids)
    dp.update.outer_middleware(intake_ui)
    reserved_category_ids = set(settings.ai_intake_category_ids.values())
    allowed_legacy_ids = set(settings.ai_intake_legacy_category_ids)

    async def legacy_categories():
        return [
            category
            for category in await client.list_categories()
            if category.id not in reserved_category_ids
            and (not allowed_legacy_ids or category.id in allowed_legacy_ids)
        ]

    category_cache: TTLValue = TTLValue(legacy_categories, settings.category_cache_ttl)
    schedule = _work_schedule(settings)
    active_workload = None
    if settings.workload_notice_enabled:
        active_workload = workload or WorkloadService(client, repo, settings, schedule=schedule)

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
        client,
        tech_group_chat_id=settings.tech_group_chat_id,
        cards=cards,
        repo=repo,
        ticket_front_base=settings.glpi_front_base,
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
    tech_tickets = build_tech_tickets_router(
        client, repo=repo, ticket_front_base=settings.glpi_front_base
    )
    lead_directory = build_lead_directory(
        client, repo, settings.lead_login_list, settings.link_recheck_ttl
    )
    claude = ClaudeApprovalService(
        client,
        repo,
        approver_ids=settings.claude_approver_glpi_ids,
        front_base=settings.glpi_front_base,
        cards=cards,
        schedule=schedule,
        reminder_hours=settings.approval_remind_hours,
    )
    dp["claude_approvals"] = claude
    tech_approval_service = TechApprovalService(
        client, repo, claude, cards=cards, front_base=settings.glpi_front_base
    )
    dp["tech_approvals"] = tech_approval_service
    tech_approval = build_tech_approval_router(tech_approval_service, lead_directory)
    access = build_access_router(
        client,
        repo,
        lead_directory,
        ticket_front_base=settings.glpi_front_base,
        access_category_id=settings.access_category_id,
        claude=claude,
        cards=cards,
        shadow=shadow,
    )
    legacy = build_new_ticket_router(
        client,
        category_cache,
        repo,
        ticket_front_base=settings.glpi_front_base,
        schedule=schedule,
        shadow=shadow,
        allowed_category_ids=allowed_legacy_ids or None,
        excluded_category_ids=reserved_category_ids or None,
    )
    legacy.name = "new_ticket_legacy"

    def pilot_user(event) -> bool:
        return intake_ui.eligibility.allows(getattr(getattr(event, "from_user", None), "id", None))

    def legacy_user(event) -> bool:
        return not pilot_user(event)

    legacy.message.filter(legacy_user)
    legacy.callback_query.filter(legacy_user, F.message.chat.type == "private")
    routers = [tech_approval, tech, tech_tickets, my_tickets, stats]
    # Keep old review buttons usable when new intake is disabled for rollback.
    if settings.ai_intake_category_ids:
        review_categories = active_intake or SimpleNamespace(
            category_id=settings.ai_intake_category_ids.__getitem__
        )
        routers.append(
            build_intake_review_router(client, repo, intake=review_categories, cards=cards)
        )
    # Gate both event types before their state handlers: a forged/stale callback
    # cannot opt a legacy user into the pilot. Static closures keep the two
    # creation paths separate, while /access still owns existing lead callbacks.
    if active_intake is not None:
        pilot = build_new_ticket_router(
            client,
            category_cache,
            repo,
            ticket_front_base=settings.glpi_front_base,
            schedule=schedule,
            shadow=shadow,
            intake=active_intake,
            workload=active_workload,
        )
        pilot.name = "new_ticket_pilot"
        pilot.message.filter(pilot_user)
        pilot.callback_query.filter(pilot_user, F.message.chat.type == "private")
        routers.append(pilot)
    routers.extend((access, legacy))
    for router in routers:
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
    shadow_task: asyncio.Task | None = None
    ai_client: OpenAIClassifier | None = None
    intake: IntakeService | None = None
    # Everything after resource creation runs under the finally so a startup
    # failure (DB perms, bad schedule config) still closes the HTTP sessions.
    try:
        await repo.connect()
        if settings.ai_intake_enabled:
            intake = IntakeService(settings, reserve_call=repo.reserve_intake_call)
        bot.session.middleware(ProfileLinksMiddleware(repo, front_base=settings.glpi_front_base))
        shadow = None
        if settings.ai_shadow_enabled:
            if settings.openai_api_key.get_secret_value():
                ai_client = OpenAIClassifier(
                    settings.openai_api_key.get_secret_value(),
                    model=settings.openai_model,
                    timeout=settings.ai_shadow_timeout,
                    proxy=settings.https_proxy,
                    internal_services=settings.ai_internal_services,
                )
                shadow = ShadowClassifier(
                    repo, ai_client, daily_limit=settings.ai_shadow_daily_limit
                )
                shadow_task = asyncio.create_task(shadow.run(), name="ai_shadow")
                log.info("ai_shadow_enabled daily_limit=%s", settings.ai_shadow_daily_limit)
            else:
                log.warning("ai_shadow_disabled reason=missing_key")
        dp = build_dispatcher(client, repo, settings, shadow=shadow, intake=intake)
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
            approval_remind_hours=settings.approval_remind_hours,
            claude_approvals=dp["claude_approvals"],
            tech_approvals=dp["tech_approvals"],
            solution_remind_minutes=settings.solution_remind_minutes,
        )
        sync_task = asyncio.create_task(sync.run(), name="glpi_sync")
        sync_task.add_done_callback(_sync_task_died)
        log.info("bot_starting")
        await _set_commands(bot)
        await dp.start_polling(bot, handle_signals=True)
    finally:
        if shadow_task is not None:
            shadow_task.cancel()
            with suppress(asyncio.CancelledError):
                await shadow_task
        if ai_client is not None:
            await ai_client.close()
        if intake is not None:
            await intake.aclose()
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
