"""Per-user rollout through the production dispatcher; no external requests."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import ReplyKeyboardMarkup
from aiogram.types import User as TgUser
from pydantic import ValidationError

from bot import main, texts
from bot.config import Settings
from bot.db.repo import Repo
from bot.glpi.client import GlpiError
from bot.glpi.models import ITILCategory, Ticket
from bot.glpi.models import User as GlpiUser
from bot.handlers.access import AccessRequest
from bot.handlers.new_ticket import NewTicket
from bot.services.intake import CATEGORY_NAMES, IntakeResult
from bot.services.intake_ui import single_text_mode
from bot.texts import intake as intake_texts
from tests.test_access import FakeBot, _cb, _dm

PILOT = 990948
OTHER = 2002
CATEGORY_IDS = {key: index for index, key in enumerate(CATEGORY_NAMES, 101)}


def _settings(**kwargs):
    values = {
        "_env_file": None,
        "telegram_bot_token": "test",
        "glpi_api_url": "https://glpi.invalid/apirest.php",
        "glpi_user_token": "test",
        "ai_intake_enabled": True,
        "ai_intake_pilot_tg_ids": {PILOT},
        "ai_intake_legacy_category_ids": {10, 11},
        "ai_intake_category_ids": CATEGORY_IDS,
    }
    values.update(kwargs)
    return Settings(**values)


@pytest.fixture
async def pilot_env(tmp_path, request):
    repo = Repo(str(tmp_path / "pilot.sqlite3"))
    await repo.connect()
    for tg_id, glpi_id in ((PILOT, 42), (OTHER, 43)):
        await repo.upsert_link(
            tg_id=tg_id,
            glpi_users_id=glpi_id,
            display_name=f"User {tg_id}",
            is_tech=True,
            now=int(time.time()),
        )
    client = AsyncMock()
    client.list_categories.return_value = [
        ITILCategory(10, "Old accounts", "Old accounts"),
        ITILCategory(11, "Old network", "Old network"),
        ITILCategory(12, "Unlisted", "Unlisted"),
        ITILCategory(100, "New parent", "New parent"),
        *[ITILCategory(CATEGORY_IDS[key], name, name) for key, name in CATEGORY_NAMES.items()],
    ]
    client.create_ticket.side_effect = iter(range(77, 100))
    client.get_ticket_requester.return_value = (42, "Pilot")
    client.user_in_group.return_value = False
    intake = MagicMock()
    intake.category_id.side_effect = CATEGORY_IDS.__getitem__
    intake.classify = AsyncMock(
        return_value=IntakeResult(
            title="Нет подключения к офисному ПК",
            category="network",
            environment="unknown",
            approval_required=False,
            reason="Ошибка подключения, новый доступ не нужен.",
        )
    )
    shadow = MagicMock(observe=AsyncMock())
    config = _settings(**getattr(request, "param", {}))
    dp = main.build_dispatcher(client, repo, config, intake=intake, shadow=shadow)
    env = SimpleNamespace(
        dp=dp, bot=FakeBot(), client=client, repo=repo, intake=intake, shadow=shadow
    )
    try:
        yield env
    finally:
        await dp.storage.close()
        await dp.fsm.events_isolation.close()
        await repo.close()


def _state(env, user_id):
    return FSMContext(
        storage=env.dp.storage,
        key=StorageKey(bot_id=env.bot.id, chat_id=user_id, user_id=user_id),
    )


async def _message(env, user_id, text):
    await env.dp.feed_update(env.bot, _dm(env.bot, 1, user_id, text))


async def _callback(env, user_id, data):
    await env.dp.feed_update(env.bot, _cb(env.bot, 2, user_id, data))


def _last(env, user_id):
    return next((text, markup) for chat, text, markup in reversed(env.bot.sent) if chat == user_id)


def _has_access(menu):
    assert isinstance(menu, ReplyKeyboardMarkup)
    return any(button.text == texts.BTN_ACCESS for row in menu.keyboard for button in row)


@pytest.mark.parametrize("entry", ["/new", texts.BTN_NEW_TICKET])
async def test_only_pilot_gets_description_and_other_user_keeps_exact_old_categories(
    pilot_env, entry
):
    env = pilot_env
    await _message(env, PILOT, entry)
    await _message(env, OTHER, entry)
    assert await _state(env, PILOT).get_state() == NewTicket.entering_description
    assert _last(env, PILOT)[0] == intake_texts.description_prompt()
    assert await _state(env, OTHER).get_state() == NewTicket.choosing_category
    buttons = _last(env, OTHER)[1].inline_keyboard
    assert [button.callback_data for row in buttons for button in row] == [
        "nt:cat:10",
        "nt:cat:11",
        "nt:cancel",
    ]
    env.intake.classify.assert_not_awaited()


async def test_interleaved_users_create_intake_and_legacy_tickets_independently(pilot_env):
    env = pilot_env
    description = "  VPN подключается, но офисный ПК недоступен.\nОшибка появилась утром.  "
    await _message(env, PILOT, "/new")
    await _message(env, OTHER, "/new")
    await _message(env, PILOT, description)
    await _callback(env, OTHER, "nt:cat:10")
    await _callback(env, PILOT, "nt:urg:4")
    await _callback(env, OTHER, "nt:urg:3")
    assert await _state(env, PILOT).get_state() == NewTicket.attaching
    assert await _state(env, OTHER).get_state() == NewTicket.entering_title
    await _message(env, OTHER, "Обычная заявка")
    await _message(env, OTHER, "Отдельное описание")
    await _callback(env, PILOT, "nt:send")
    await _callback(env, OTHER, "nt:send")

    assert [call.kwargs for call in env.client.create_ticket.await_args_list] == [
        {
            "name": "Нет подключения к офисному ПК",
            "content": description,
            "urgency": 4,
            "itilcategories_id": CATEGORY_IDS["network"],
            "requester_users_id": 42,
        },
        {
            "name": "Обычная заявка",
            "content": "Отдельное описание",
            "urgency": 3,
            "itilcategories_id": 10,
            "requester_users_id": 43,
        },
    ]
    env.intake.classify.assert_awaited_once_with(description)
    env.shadow.observe.assert_awaited_once_with(78, "new", "Обычная заявка", "Отдельное описание")
    assert (await env.repo.get_ticket_intake(77))["review_state"] == "pending"
    assert await env.repo.get_ticket_intake(78) is None
    assert await _state(env, PILOT).get_state() is None
    assert await _state(env, OTHER).get_state() is None


@pytest.mark.parametrize("entry", ["/access", texts.BTN_ACCESS, texts.BTN_ACCESS_LEGACY])
async def test_access_entry_redirect_is_exclusive_to_pilot(pilot_env, entry):
    env = pilot_env
    await _message(env, PILOT, entry)
    await _message(env, OTHER, entry)
    assert await _state(env, PILOT).get_state() == NewTicket.entering_description
    assert await _state(env, OTHER).get_state() == AccessRequest.intro
    assert _last(env, OTHER)[0] == texts.ACCESS_WARNING
    await _callback(env, OTHER, "ac:go")
    assert await _state(env, OTHER).get_state() == AccessRequest.entering_request
    env.intake.classify.assert_not_awaited()


async def test_free_text_offer_uses_each_senders_workflow(pilot_env):
    env = pilot_env
    for user_id in (PILOT, OTHER):
        await _message(env, user_id, "ПК недоступен")
        await _callback(env, user_id, "nt:offer:yes")
    assert await _state(env, PILOT).get_state() == NewTicket.entering_description
    assert await _state(env, OTHER).get_state() == NewTicket.choosing_category
    await _message(env, PILOT, "Офисный ПК снова недоступен")
    assert await _state(env, PILOT).get_state() == NewTicket.choosing_urgency
    await _callback(env, PILOT, "nt:urg:3")
    await _callback(env, PILOT, "nt:send")
    await _callback(env, OTHER, "nt:cat:11")
    await _callback(env, OTHER, "nt:urg:3")
    await _message(env, OTHER, "Подключение к ПК")
    assert await _state(env, OTHER).get_state() == NewTicket.attaching
    await _callback(env, OTHER, "nt:send")
    env.intake.classify.assert_awaited_once_with("Офисный ПК снова недоступен")
    assert env.client.create_ticket.await_args.kwargs["content"] == "ПК недоступен"
    assert await env.repo.get_ticket_intake(78) is None


@pytest.mark.parametrize("user_id", [PILOT, OTHER])
async def test_start_and_cancel_menus_follow_recipient_and_do_not_leak_context(pilot_env, user_id):
    env = pilot_env
    await _message(env, user_id, "/start")
    assert _has_access(_last(env, user_id)[1]) is (user_id != PILOT)
    await _message(env, user_id, "/new")
    await _callback(env, user_id, "nt:cancel")
    assert _has_access(_last(env, user_id)[1]) is (user_id != PILOT)
    assert not single_text_mode.get()


async def test_callback_sender_wins_over_nested_message_author_and_glpi_tech_role(pilot_env):
    env = pilot_env
    await _message(env, OTHER, "/new")
    await _callback(env, OTHER, "nt:cat:10")
    update = _cb(env.bot, 3, OTHER, "nt:urg:3")
    callback = update.callback_query
    update = update.model_copy(
        update={
            "callback_query": callback.model_copy(
                update={
                    "message": callback.message.model_copy(
                        update={
                            "from_user": TgUser(
                                id=PILOT, is_bot=False, first_name="Pilot", username="snnrman"
                            )
                        }
                    ),
                    "from_user": callback.from_user.model_copy(update={"username": "snnrman"}),
                }
            )
        }
    )
    await env.dp.feed_update(env.bot, update)
    assert (await env.repo.get_by_tg(OTHER)).is_tech
    assert await _state(env, OTHER).get_state() == NewTicket.entering_title
    env.intake.classify.assert_not_awaited()


async def test_nonpilot_cannot_select_reserved_category_or_submit_injected_intake_data(pilot_env):
    env = pilot_env
    await _message(env, OTHER, "/new")
    await _callback(env, OTHER, f"nt:cat:{CATEGORY_IDS['accounts']}")
    assert await _state(env, OTHER).get_state() == NewTicket.choosing_category
    assert "category_id" not in await _state(env, OTHER).get_data()
    state = _state(env, OTHER)
    await state.set_state(NewTicket.attaching)
    await state.set_data(
        {
            "title": "Stale pilot draft",
            "description": "Existing draft with reserved category",
            "urgency": 3,
            "category_id": CATEGORY_IDS["accounts"],
            "intake_result": env.intake.classify.return_value.as_dict(),
            "attachments": [],
        }
    )
    await _callback(env, OTHER, "nt:send")
    env.intake.classify.assert_not_awaited()
    env.client.create_ticket.assert_not_awaited()
    assert await env.repo.get_ticket_intake(77) is None


async def test_nonpilot_with_valid_legacy_draft_never_runs_intake_even_with_injected_ai_result(
    pilot_env,
):
    env = pilot_env
    state = _state(env, OTHER)
    await state.set_state(NewTicket.attaching)
    await state.set_data(
        {
            "title": "Existing draft",
            "description": "Unrelated stored data must not opt user into AI intake",
            "urgency": 3,
            "category_id": 10,
            "intake_result": env.intake.classify.return_value.as_dict(),
            "attachments": [],
        }
    )
    await _callback(env, OTHER, "nt:send")
    env.client.create_ticket.assert_awaited_once()
    env.intake.classify.assert_not_awaited()
    env.shadow.observe.assert_awaited_once()
    assert await env.repo.get_ticket_intake(77) is None


@pytest.mark.parametrize("admin,recipient,glpi_id", [(PILOT, OTHER, 43), (OTHER, PILOT, 42)])
async def test_linking_notification_menu_uses_recipient_not_admin(
    pilot_env, admin, recipient, glpi_id
):
    env = pilot_env
    env.client.find_user_by_login.return_value = GlpiUser(id=glpi_id, name="employee")
    await _message(env, admin, f"/link {recipient} employee")
    assert _last(env, recipient)[0] == texts.LINK_CONFIRMED
    assert _has_access(_last(env, recipient)[1]) is (recipient != PILOT)


async def test_concurrent_pilot_submission_does_not_change_another_users_menu(pilot_env):
    env = pilot_env
    started, release = asyncio.Event(), asyncio.Event()
    result = env.intake.classify.return_value

    async def wait_for_release(description):
        started.set()
        await release.wait()
        return result

    env.intake.classify.side_effect = wait_for_release
    await _message(env, PILOT, "/new")
    await _message(env, PILOT, "Офисный ПК снова недоступен")
    await _callback(env, PILOT, "nt:urg:3")
    submitting = asyncio.create_task(_callback(env, PILOT, "nt:send"))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        await _message(env, OTHER, "/start")
        assert _has_access(_last(env, OTHER)[1])
        assert not single_text_mode.get()
    finally:
        release.set()
        await submitting
    menus = [
        markup
        for chat, _, markup in env.bot.sent
        if chat == PILOT and isinstance(markup, ReplyKeyboardMarkup)
    ]
    assert menus and not _has_access(menus[-1])


@pytest.mark.parametrize("pilot_env", [{"ai_intake_pilot_tg_ids": set()}], indirect=True)
async def test_empty_pilot_list_preserves_existing_global_enable_semantics(pilot_env):
    for user_id in (PILOT, OTHER):
        await _message(pilot_env, user_id, "/access")
        assert await _state(pilot_env, user_id).get_state() == NewTicket.entering_description
    assert pilot_env.repo.intake_recovery_filter is None


@pytest.mark.parametrize("pilot_env", [{"ai_intake_enabled": False}], indirect=True)
async def test_disabled_intake_preserves_both_legacy_users_and_existing_review_routes(pilot_env):
    env = pilot_env
    for user_id in (PILOT, OTHER):
        await _message(env, user_id, "/new")
        assert await _state(env, user_id).get_state() == NewTicket.choosing_category
        await _message(env, user_id, "/access")
        assert await _state(env, user_id).get_state() == AccessRequest.intro
    assert "intake_review" in {router.name for router in env.dp.sub_routers}
    assert env.repo.intake_category_ids == CATEGORY_IDS
    assert env.repo.intake_recovery_filter is not None
    env.intake.classify.assert_not_awaited()


async def test_recovery_guard_verifies_requester_instead_of_triggering_technician(pilot_env):
    env = pilot_env
    guard = env.repo.intake_recovery_filter
    env.client.get_ticket_requester.return_value = (42, "Pilot")
    assert await guard(77)
    env.client.get_ticket_requester.return_value = (43, "Other technician")
    assert not await guard(77)
    env.client.get_ticket_requester.return_value = (44, "Unlinked")
    assert not await guard(77)
    env.client.get_ticket_requester.return_value = None
    with pytest.raises(GlpiError):
        await guard(77)


@pytest.mark.parametrize("pilot_env", [{}, {"ai_intake_enabled": False}], indirect=True)
async def test_nonpilot_technician_can_review_persisted_intake_including_after_rollback(pilot_env):
    env = pilot_env
    result = env.intake.classify.return_value.as_dict()
    await env.repo.save_ticket_intake(
        77,
        {**result, "category_id": CATEGORY_IDS["network"], "original_description": "Original"},
        int(time.time()),
    )
    env.client.get_ticket.return_value = Ticket(
        77, result["title"], "Original", 1, 3, CATEGORY_IDS["network"]
    )
    # Existing review metadata remains authoritative even if the requester is
    # later relinked or removed from the pilot list.
    env.client.get_ticket_requester.return_value = (43, "Other")
    await _callback(env, OTHER, "ir:routine:77:1")
    assert (await env.repo.get_ticket_intake(77))["review_state"] == "routine"
    env.intake.classify.assert_not_awaited()
    env.client.get_ticket_requester.assert_not_awaited()


@pytest.mark.parametrize("field", ["ai_intake_pilot_tg_ids", "ai_intake_legacy_category_ids"])
@pytest.mark.parametrize("invalid", [[0], [-1], [True], ["990948"]])
def test_rollout_id_configuration_rejects_nonpositive_or_noninteger_values(field, invalid):
    with pytest.raises(ValidationError):
        _settings(**{field: invalid})


def test_pilot_ids_load_from_json_environment(monkeypatch):
    monkeypatch.setenv("AI_INTAKE_PILOT_TG_IDS", "[990948]")
    monkeypatch.setenv("AI_INTAKE_LEGACY_CATEGORY_IDS", "[10,11]")
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test",
        glpi_api_url="https://glpi.invalid/apirest.php",
        glpi_user_token="test",
    )
    assert settings.ai_intake_pilot_tg_ids == {PILOT}
    assert settings.ai_intake_legacy_category_ids == {10, 11}
