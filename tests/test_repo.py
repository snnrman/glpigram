"""Repo (SQLite) tests for the account-linking mapping."""

from __future__ import annotations

import pytest

from bot.db.repo import Repo

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def repo(tmp_path):
    r = Repo(str(tmp_path / "test.sqlite3"))
    await r.connect()
    yield r
    await r.close()


async def test_missing_link_is_none(repo):
    assert await repo.get_by_tg(111) is None
    assert await repo.get_by_glpi(222) is None


async def test_upsert_and_lookup(repo):
    await repo.upsert_link(
        tg_id=111, glpi_users_id=42, display_name="Иван Петров", is_tech=True, now=1000
    )
    link = await repo.get_by_tg(111)
    assert link is not None
    assert link.glpi_users_id == 42
    assert link.display_name == "Иван Петров"
    assert link.is_tech is True
    assert link.linked_at == 1000
    # reverse lookup hits the same row
    assert (await repo.get_by_glpi(42)).tg_id == 111


async def test_upsert_same_tg_updates_in_place(repo):
    await repo.upsert_link(tg_id=111, glpi_users_id=42, display_name="a", is_tech=False, now=1000)
    await repo.upsert_link(tg_id=111, glpi_users_id=43, display_name="b", is_tech=True, now=2000)
    link = await repo.get_by_tg(111)
    assert link.glpi_users_id == 43
    assert link.is_tech is True
    # old GLPI id no longer maps anywhere
    assert await repo.get_by_glpi(42) is None


async def test_relink_transfers_glpi_account_to_new_tg(repo):
    # Same GLPI account, new Telegram id -> the old mapping must be dropped.
    await repo.upsert_link(tg_id=111, glpi_users_id=42, display_name="a", is_tech=False, now=1000)
    await repo.upsert_link(tg_id=222, glpi_users_id=42, display_name="a", is_tech=False, now=1500)
    assert await repo.get_by_tg(111) is None
    assert (await repo.get_by_glpi(42)).tg_id == 222


async def test_set_tech_checked_refreshes_flag_and_timestamp(repo):
    await repo.upsert_link(tg_id=111, glpi_users_id=42, display_name="a", is_tech=False, now=1000)
    await repo.set_tech_checked(111, is_tech=True, now=5000)
    link = await repo.get_by_tg(111)
    assert link.is_tech is True
    assert link.checked_at == 5000


async def test_unlink_by_tg_and_by_glpi(repo):
    await repo.upsert_link(tg_id=111, glpi_users_id=42, display_name="a", is_tech=False, now=1000)
    assert await repo.unlink_tg(111) is True
    assert await repo.unlink_tg(111) is False  # already gone
    assert await repo.get_by_tg(111) is None

    await repo.upsert_link(tg_id=222, glpi_users_id=43, display_name="b", is_tech=False, now=1000)
    assert await repo.unlink_glpi(43) is True
    assert await repo.unlink_glpi(43) is False
    assert await repo.get_by_glpi(43) is None


# --- tracked tickets + cursors (feature 4) ----------------------------------
async def test_track_ticket_is_idempotent(repo):
    await repo.track_ticket(
        ticket_id=10, requester_tg_id=555, requester_glpi_id=42, status=1, now=100
    )
    # A second call (e.g. retry) must not reset cursors or duplicate the row.
    await repo.set_ticket_followup_cursor(10, 7)
    await repo.track_ticket(
        ticket_id=10, requester_tg_id=999, requester_glpi_id=1, status=5, now=200
    )
    rows = await repo.active_tracked_tickets()
    assert len(rows) == 1
    assert rows[0].requester_tg_id == 555  # original preserved
    assert rows[0].last_followup_id == 7


async def test_active_tracked_tickets_excludes_inactive(repo):
    await repo.track_ticket(
        ticket_id=10, requester_tg_id=555, requester_glpi_id=42, status=1, now=100
    )
    await repo.track_ticket(
        ticket_id=11, requester_tg_id=556, requester_glpi_id=43, status=1, now=100
    )
    await repo.set_ticket_status(11, status=6, active=False)
    ids = [r.ticket_id for r in await repo.active_tracked_tickets()]
    assert ids == [10]


async def test_ticket_cursors_update(repo):
    await repo.track_ticket(
        ticket_id=10, requester_tg_id=555, requester_glpi_id=42, status=1, now=100
    )
    await repo.set_ticket_status(10, status=2, active=True)
    await repo.set_ticket_followup_cursor(10, 3)
    row = (await repo.active_tracked_tickets())[0]
    assert row.last_status == 2
    assert row.last_followup_id == 3


async def test_sync_cursor_roundtrip(repo):
    assert await repo.get_cursor("last_ticket_id") is None
    await repo.set_cursor("last_ticket_id", 42)
    assert await repo.get_cursor("last_ticket_id") == 42
    await repo.set_cursor("last_ticket_id", 99)  # upsert
    assert await repo.get_cursor("last_ticket_id") == 99
