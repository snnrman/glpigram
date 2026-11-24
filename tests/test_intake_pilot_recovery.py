"""Pilot recovery stays scoped without bypassing existing technician review."""

from unittest.mock import AsyncMock

import pytest

from bot.db.repo import Repo
from bot.glpi.client import GlpiError
from bot.glpi.models import Ticket
from bot.handlers.tech_actions import TechAction
from bot.services.intake import IntakeService
from bot.services.intake_snapshot import refresh_intake_snapshot
from tests import test_intake_actions
from tests.test_handoff import TECH_ID, _dm_cb, _group_cb

action_env = test_intake_actions.env

TID = 902
CATEGORY_ID = 105
TITLE = "Сбой сборки Jenkins"
DESCRIPTION = "Jenkins не собирает проект <project>."


@pytest.fixture
async def repo(tmp_path):
    repo = Repo(str(tmp_path / "recovery.sqlite3"))
    await repo.connect()
    repo.intake_category_ids = {"infrastructure": CATEGORY_ID}
    yield repo
    await repo.close()


@pytest.fixture
def ticket():
    return Ticket(TID, TITLE, DESCRIPTION, 1, 5, CATEGORY_ID)


async def test_nonpilot_reserved_category_never_creates_metadata(repo, ticket):
    guard = AsyncMock(return_value=False)
    repo.intake_recovery_filter = guard
    writes_before = repo._conn.total_changes

    assert await refresh_intake_snapshot(repo, ticket) is None

    guard.assert_awaited_once_with(TID)
    assert await repo.get_ticket_intake(TID) is None
    assert await repo.list_ticket_intake_events(TID) == []
    assert repo._conn.total_changes == writes_before


async def test_allowed_pilot_recovers_source_and_requires_manual_review(repo, ticket):
    guard = AsyncMock(return_value=True)
    repo.intake_recovery_filter = guard

    row = await refresh_intake_snapshot(repo, ticket)

    guard.assert_awaited_once_with(TID)
    assert row["review_state"] == "pending"
    assert row["metadata_recovered"] is True
    assert row["status"] == "fallback"
    assert row["title"] == TITLE
    assert row["original_description"] == DESCRIPTION
    assert row["category_id"] == CATEGORY_ID
    assert row["model"] == ""
    assert row["title_generated"] is False


@pytest.mark.parametrize("guard_error", [False, True])
async def test_existing_metadata_survives_scope_change_and_still_refreshes(
    repo, ticket, guard_error
):
    row = await refresh_intake_snapshot(repo, ticket)
    await repo.update_ticket_intake(
        TID,
        expected_version=row["version"],
        actor_tg_id=TECH_ID,
        now=2,
        event="routine",
        review_state="routine",
    )
    guard = AsyncMock(return_value=False)
    if guard_error:
        guard.side_effect = GlpiError("Requester verification unavailable")
    repo.intake_recovery_filter = guard
    ticket.content = DESCRIPTION + " Теперь известен адрес сервера."

    updated = await refresh_intake_snapshot(repo, ticket)

    guard.assert_not_awaited()
    assert updated["original_description"] == DESCRIPTION
    assert updated["display_description"] == ticket.content
    assert updated["review_state"] == "pending"
    assert updated["ai_stale"] is True


async def test_filter_failure_propagates_without_writes_or_model_call(repo, ticket, monkeypatch):
    guard = AsyncMock(side_effect=GlpiError("Requester verification unavailable"))
    repo.intake_recovery_filter = guard
    classify = AsyncMock()
    monkeypatch.setattr(IntakeService, "classify", classify)
    writes_before = repo._conn.total_changes

    with pytest.raises(GlpiError, match="Requester verification unavailable"):
        await refresh_intake_snapshot(repo, ticket)

    guard.assert_awaited_once_with(TID)
    classify.assert_not_awaited()
    assert await repo.get_ticket_intake(TID) is None
    assert await repo.list_ticket_intake_events(TID) == []
    assert repo._conn.total_changes == writes_before


async def test_no_filter_preserves_global_recovery(repo, ticket):
    assert repo.intake_recovery_filter is None

    row = await refresh_intake_snapshot(repo, ticket)

    assert row["review_state"] == "pending"
    assert row["metadata_recovered"] is True
    assert row["category_id"] == CATEGORY_ID


async def test_legacy_category_does_not_need_pilot_verification(repo, ticket):
    ticket.itilcategories_id = 8
    guard = AsyncMock(side_effect=GlpiError("Requester verification unavailable"))
    repo.intake_recovery_filter = guard

    assert await refresh_intake_snapshot(repo, ticket) is None

    guard.assert_not_awaited()
    assert await repo.list_ticket_intake_events(TID) == []


@pytest.mark.parametrize("action", ["take", "close", "handoff", "hto", "solution"])
async def test_verification_failure_blocks_real_technician_mutations(action_env, action):
    env = action_env
    env.ticket.id = TID
    guard = AsyncMock(side_effect=GlpiError("Requester verification unavailable"))
    env.repo.intake_recovery_filter = guard
    writes_before = env.repo._conn.total_changes

    with pytest.raises(GlpiError, match="Requester verification unavailable"):
        if action == "solution":
            await env.ctx.set_state(TechAction.closing)
            await env.ctx.set_data({"ticket_id": TID})
            await test_intake_actions._text(env, "Исправлено")
        else:
            data = f"ta:{action}:{TID}" if action != "hto" else f"ta:hto:{TID}:11"
            make = _dm_cb if action == "hto" else _group_cb
            await env.dp.feed_update(env.bot, make(env.bot, 1, TECH_ID, data), link=env.link)

    guard.assert_awaited_once_with(TID)
    test_intake_actions._assert_no_mutation(env)
    assert await env.repo.get_ticket_intake(TID) is None
    assert await env.repo.list_ticket_intake_events(TID) == []
    assert env.repo._conn.total_changes == writes_before


async def test_verified_nonpilot_reserved_ticket_keeps_legacy_take(action_env):
    env = action_env
    env.ticket.id = TID
    env.repo.intake_recovery_filter = AsyncMock(return_value=False)

    await env.dp.feed_update(
        env.bot, _group_cb(env.bot, 1, TECH_ID, f"ta:take:{TID}"), link=env.link
    )

    env.client.assign_ticket.assert_awaited_once_with(TID, 9)
    assert await env.repo.get_ticket_intake(TID) is None
    assert await env.repo.list_ticket_intake_events(TID) == []
