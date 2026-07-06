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
