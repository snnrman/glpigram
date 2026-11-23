"""New intake through the real dispatcher and SQLite; all external I/O is fake."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import ReplyKeyboardMarkup

from bot import main, texts
from bot.config import Settings
from bot.db.repo import Repo
from bot.glpi.client import GlpiHTTPError
from bot.glpi.models import ITILCategory, Ticket
from bot.handlers.new_ticket import main_menu_keyboard
from bot.schedule import WorkSchedule
from bot.services.intake import CATEGORY_NAMES, IntakeResult
from bot.services.intake_ui import single_text_mode
from bot.services.sync import SyncService
from tests.test_access import REQUESTER_ID, FakeBot, _cb, _dm

CATEGORY_IDS = {key: index for index, key in enumerate(CATEGORY_NAMES, 101)}


def settings(**kwargs):
    return Settings(
        _env_file=None,
        telegram_bot_token="test",
        glpi_api_url="https://glpi.invalid/apirest.php",
        glpi_user_token="test",
        ai_intake_category_ids=CATEGORY_IDS,
        **kwargs,
    )


@pytest.fixture
async def intake_env(tmp_path):
    repo = Repo(str(tmp_path / "integration.sqlite3"))
    await repo.connect()
    await repo.upsert_link(
        tg_id=REQUESTER_ID,
        glpi_users_id=8,
        display_name="Requester",
        is_tech=False,
        now=int(time.time()),
    )
    client = AsyncMock()
    client.list_categories.return_value = [
        ITILCategory(CATEGORY_IDS[key], name, name) for key, name in CATEGORY_NAMES.items()
    ]
    client.create_ticket.return_value = 77
    client.get_ticket_requester.return_value = (8, "Requester")
    client.list_ticket_documents.return_value = []
    intake = MagicMock()
    intake.category_id.side_effect = CATEGORY_IDS.__getitem__
    intake.classify = AsyncMock(
        return_value=IntakeResult(
            title="Не удаётся подключиться к офисному ПК",
            category="network",
            environment="unknown",
            approval_required=False,
            reason="Проблема с удалённым подключением; нового платного доступа нет.",
        )
    )
    shadow = MagicMock(observe=AsyncMock())
    config = settings(ai_intake_enabled=True)
    dp = main.build_dispatcher(client, repo, config, intake=intake, shadow=shadow)
    bot = FakeBot()
    yield dp, bot, client, repo, intake, shadow
    await dp.storage.close()
    await dp.fsm.events_isolation.close()
    await repo.close()


@pytest.mark.parametrize("entry", ["/new", "/access", texts.BTN_ACCESS])
async def test_one_text_reaches_glpi_and_sync_card_without_title_duplication(intake_env, entry):
    dp, bot, client, repo, intake, shadow = intake_env
    description = "VPN подключается, но офисный ПК недоступен.\nОшибка <connection refused>."
    await dp.feed_update(bot, _dm(bot, 1, REQUESTER_ID, entry))
    await dp.feed_update(bot, _dm(bot, 2, REQUESTER_ID, description))
    await dp.feed_update(bot, _cb(bot, 3, REQUESTER_ID, "nt:urg:3"))
    await dp.feed_update(bot, _cb(bot, 4, REQUESTER_ID, "nt:send"))
    client.create_ticket.assert_awaited_once_with(
        name="Не удаётся подключиться к офисному ПК",
        content=description,
        urgency=3,
        itilcategories_id=CATEGORY_IDS["network"],
        requester_users_id=8,
    )
    intake.classify.assert_awaited_once_with(description)
    shadow.observe.assert_not_awaited()
    client.create_validation.assert_not_awaited()
    row = await repo.get_ticket_intake(77)
    assert row["original_description"] == description
    assert row["review_state"] == "pending"
    menus = [markup for _, _, markup in bot.sent if isinstance(markup, ReplyKeyboardMarkup)]
    assert menus
    assert all(texts.BTN_ACCESS != b.text for menu in menus for r in menu.keyboard for b in r)
    # Context-local UI mode must not leak to another dispatcher or an old workflow.
    assert not single_text_mode.get()
    assert any(texts.BTN_ACCESS == b.text for r in main_menu_keyboard().keyboard for b in r)

    ticket = Ticket(77, row["title"], description, 1, 3, CATEGORY_IDS["network"])
    client.get_ticket.return_value = ticket
    sync = SyncService(
        bot,
        client,
        repo,
        tech_group_chat_id=-100123,
        schedule=WorkSchedule.from_config("09:00-18:00", "1-5"),
        front_base="https://glpi.invalid",
    )
    assert await sync._send_new_card(ticket)
    text = next(text for chat, text, _ in reversed(bot.sent) if chat == -100123)
    assert "Не удаётся подключиться к офисному ПК" not in text
    assert text.count("VPN подключается") == 1
    assert "&lt;connection refused&gt;" in text
    assert CATEGORY_NAMES["network"] in text
    assert "tg://user?id=1001" in text
    await sync._cards.record_event(bot, 77, "Взял в работу: Technician", status=2)
    updated = next(text for chat, text, _ in reversed(bot.sent) if chat == -100123)
    assert updated.count("VPN подключается") == 1
    assert "&lt;connection refused&gt;" in updated
    assert CATEGORY_NAMES["network"] in updated


async def test_free_text_is_reused_by_enabled_dispatcher(intake_env):
    dp, bot, client, _, intake, _ = intake_env
    await dp.feed_update(bot, _dm(bot, 1, REQUESTER_ID, "Офисный ПК недоступен"))
    await dp.feed_update(bot, _cb(bot, 2, REQUESTER_ID, "nt:offer:yes"))
    await dp.feed_update(bot, _cb(bot, 3, REQUESTER_ID, "nt:urg:4"))
    await dp.feed_update(bot, _cb(bot, 4, REQUESTER_ID, "nt:send"))
    intake.classify.assert_awaited_once_with("Офисный ПК недоступен")
    assert client.create_ticket.await_args.kwargs["urgency"] == 4
    ctx = FSMContext(
        storage=dp.storage,
        key=StorageKey(bot_id=bot.id, chat_id=REQUESTER_ID, user_id=REQUESTER_ID),
    )
    assert await ctx.get_state() is None


async def test_unknown_create_outcome_and_failed_telegram_reply_leave_no_submitting_draft(
    intake_env,
):
    dp, bot, client, _, _, _ = intake_env
    await dp.feed_update(bot, _dm(bot, 1, REQUESTER_ID, "/new"))
    await dp.feed_update(bot, _dm(bot, 2, REQUESTER_ID, "Офисный ПК недоступен"))
    await dp.feed_update(bot, _cb(bot, 3, REQUESTER_ID, "nt:urg:3"))
    client.create_ticket.side_effect = GlpiHTTPError(
        "ticket created but no id returned", response=httpx.Response(201, content=b"[]")
    )
    original_send = bot._send

    def broken_notice(chat_id, text, reply_markup=None):
        from bot.texts.intake import creation_uncertain

        if text == creation_uncertain():
            raise RuntimeError("Telegram unavailable")
        return original_send(chat_id, text, reply_markup)

    bot._send = broken_notice
    await dp.feed_update(bot, _cb(bot, 4, REQUESTER_ID, "nt:send"))
    client.create_ticket.assert_awaited_once()
    ctx = FSMContext(
        storage=dp.storage,
        key=StorageKey(bot_id=bot.id, chat_id=REQUESTER_ID, user_id=REQUESTER_ID),
    )
    assert await ctx.get_state() is None
    await dp.feed_update(bot, _cb(bot, 5, REQUESTER_ID, "nt:send"))
    client.create_ticket.assert_awaited_once()


@pytest.mark.parametrize("enabled", [False, True])
async def test_intake_runtime_lifecycle_is_opt_in(tmp_path, monkeypatch, enabled):
    config = settings(ai_intake_enabled=enabled, db_path=str(tmp_path / "runtime.sqlite3"))
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()
    bot.session.close = AsyncMock()
    monkeypatch.setattr(main, "Bot", MagicMock(return_value=bot))
    glpi = AsyncMock()
    monkeypatch.setattr(main, "GlpiClient", MagicMock(return_value=glpi))
    intake = MagicMock(aclose=AsyncMock())
    factory = MagicMock(return_value=intake)
    monkeypatch.setattr(main, "IntakeService", factory)
    dp = MagicMock()

    async def poll(*args, **kwargs):
        await asyncio.sleep(0)

    dp.start_polling = AsyncMock(side_effect=poll)
    dispatcher = MagicMock(return_value=dp)
    monkeypatch.setattr(main, "build_dispatcher", dispatcher)
    monkeypatch.setattr(main, "SyncService", MagicMock(return_value=AsyncMock()))
    await main._run(config)
    assert dispatcher.call_args.kwargs["intake"] is (intake if enabled else None)
    if enabled:
        factory.assert_called_once()
        assert callable(factory.call_args.kwargs["reserve_call"])
        intake.aclose.assert_awaited_once()
    else:
        factory.assert_not_called()
        intake.aclose.assert_not_awaited()
    glpi.close.assert_awaited_once()
    bot.session.close.assert_awaited_once()


async def test_switching_off_new_form_keeps_persisted_review_routes(tmp_path):
    repo = Repo(str(tmp_path / "rollback.sqlite3"))
    await repo.connect()
    try:
        dp = main.build_dispatcher(AsyncMock(), repo, settings(ai_intake_enabled=False))
        names = {router.name for router in dp.sub_routers}
        assert "intake_review" in names
        await dp.storage.close()
        await dp.fsm.events_isolation.close()
    finally:
        await repo.close()


async def test_sync_read_and_snapshot_do_not_undo_concurrent_technician_correction(intake_env):
    _, bot, client, repo, intake, _ = intake_env
    result = intake.classify.return_value.as_dict()
    await repo.save_ticket_intake(
        77,
        {**result, "category_id": CATEGORY_IDS["network"], "original_description": "Original"},
        int(time.time()),
    )
    await repo.track_ticket(
        ticket_id=77, requester_tg_id=REQUESTER_ID, requester_glpi_id=8, status=1, now=1
    )
    read_started = asyncio.Event()
    finish_read = asyncio.Event()

    async def read_ticket(tid):
        assert repo.ticket_action_lock(tid).locked()
        read_started.set()
        await finish_read.wait()
        return Ticket(77, result["title"], "Original", 1, 3, CATEGORY_IDS["network"])

    client.get_ticket.side_effect = read_ticket
    client.list_followups.return_value = []
    sync = SyncService(
        bot,
        client,
        repo,
        tech_group_chat_id=None,
        schedule=WorkSchedule.from_config("09:00-18:00", "1-5"),
    )

    async def correct_category():
        async with repo.ticket_action_lock(77):
            row = await repo.get_ticket_intake(77)
            await repo.update_ticket_intake(
                77,
                expected_version=row["version"],
                actor_tg_id=2002,
                now=2,
                event="category_changed",
                category="hardware",
                category_id=CATEGORY_IDS["hardware"],
            )

    poll = asyncio.create_task(sync._sync_ticket(await repo.get_tracked_ticket(77)))
    await asyncio.wait_for(read_started.wait(), timeout=2)
    correction = asyncio.create_task(correct_category())
    await asyncio.sleep(0)
    assert not correction.done()
    finish_read.set()
    await asyncio.wait_for(asyncio.gather(poll, correction), timeout=3)
    assert (await repo.get_ticket_intake(77))["category"] == "hardware"


async def test_technician_can_review_in_private_details_without_a_group(intake_env):
    dp, bot, client, repo, intake, _ = intake_env
    await repo.upsert_link(
        tg_id=2002, glpi_users_id=9, display_name="Technician", is_tech=True, now=int(time.time())
    )
    result = intake.classify.return_value.as_dict()
    await repo.save_ticket_intake(
        77,
        {**result, "category_id": CATEGORY_IDS["network"], "original_description": "Original"},
        int(time.time()),
    )
    client.get_ticket.return_value = Ticket(
        77, result["title"], "Original", 1, 3, CATEGORY_IDS["network"]
    )
    client.list_followups.return_value = []
    client.get_ticket_assignees.return_value = []
    await dp.feed_update(bot, _cb(bot, 1, 2002, "tt:openu:77"))
    keyboard = next(kb for _, text, kb in reversed(bot.sent) if text and kb)
    callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert any(value.startswith("ir:routine:77:") for value in callbacks if value)
    assert any(value.startswith("ir:category:77:") for value in callbacks if value)
    assert not any(value.startswith("ta:take:") for value in callbacks if value)


async def test_approval_entry_reads_snapshot_under_ticket_action_lock(intake_env):
    dp, bot, client, repo, intake, _ = intake_env
    await repo.upsert_link(
        tg_id=2002, glpi_users_id=9, display_name="Technician", is_tech=True, now=int(time.time())
    )
    result = intake.classify.return_value.as_dict()
    await repo.save_ticket_intake(
        77,
        {**result, "category_id": CATEGORY_IDS["network"], "original_description": "Original"},
        int(time.time()),
    )
    observed_locks = []

    async def read_ticket(tid):
        observed_locks.append(repo.ticket_action_lock(tid).locked())
        return Ticket(77, result["title"], "Original", 1, 3, CATEGORY_IDS["network"])

    client.get_ticket.side_effect = read_ticket
    await dp.feed_update(bot, _cb(bot, 1, 2002, "ir:approval:77:1"))
    # The first read is used to refresh metadata; later prepare reads do not
    # mutate the snapshot and are revalidated again during submit.
    assert observed_locks and observed_locks[0] is True
    client.create_validation.assert_not_awaited()
