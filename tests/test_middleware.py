"""AuthMiddleware: gate, auto-unlink (offboarding), is_tech refresh, GLPI-error
tolerance. Called directly — the middleware contract needs no dispatcher.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Message

from bot import texts
from bot.db.repo import Repo
from bot.glpi.client import GlpiHTTPError
from bot.glpi.models import User
from bot.middleware import AuthMiddleware

TG_ID = 555
GLPI_ID = 42


@pytest.fixture
async def repo(tmp_path):
    r = Repo(str(tmp_path / "mw.sqlite3"))
    await r.connect()
    yield r
    await r.close()


def _mw(repo, client, *, ttl=300, group=7):
    return AuthMiddleware(repo, client, tech_group_id=group, recheck_ttl=ttl)


def _event(kind=Message):
    """Object passing isinstance(Message/CallbackQuery) with a recorded answer()."""
    ev = AsyncMock(spec=kind)
    # aiogram's Message.answer is sync-returning-awaitable; force a real AsyncMock.
    ev.answer = AsyncMock()
    return ev


def _client(*, user_active=True, in_group=False):
    client = AsyncMock()
    client.get_user.return_value = (
        User(id=GLPI_ID, name="jdoe", is_active=user_active) if user_active is not None else None
    )
    client.user_in_group.return_value = in_group
    return client


async def _link(repo, *, checked_ago=0, is_tech=False):
    now = int(time.time())
    await repo.upsert_link(
        tg_id=TG_ID, glpi_users_id=GLPI_ID, display_name="J", is_tech=is_tech, now=now
    )
    if checked_ago:
        await repo.set_tech_checked(TG_ID, is_tech=is_tech, now=now - checked_ago)


def _data():
    return {"event_from_user": SimpleNamespace(id=TG_ID)}


async def test_linked_user_passes_and_link_injected(repo):
    await _link(repo)
    handler = AsyncMock(return_value="ok")
    data = _data()
    result = await _mw(repo, _client())(handler, _event(), data)
    assert result == "ok"
    assert data["link"].glpi_users_id == GLPI_ID


async def test_unlinked_message_denied_with_need_link(repo):
    handler = AsyncMock()
    ev = _event(Message)
    assert await _mw(repo, _client())(handler, ev, _data()) is None
    handler.assert_not_awaited()
    ev.answer.assert_awaited_once_with(texts.NEED_LINK)


async def test_unlinked_callback_denied_with_alert(repo):
    ev = _event(CallbackQuery)
    await _mw(repo, _client())(AsyncMock(), ev, _data())
    ev.answer.assert_awaited_once_with(texts.NEED_LINK, show_alert=True)


async def test_fresh_link_skips_glpi_recheck(repo):
    await _link(repo)  # checked_at == now -> inside TTL
    client = _client()
    await _mw(repo, client)(AsyncMock(), _event(), _data())
    client.get_user.assert_not_awaited()  # no GLPI round-trip


async def test_stale_link_rechecks_and_refreshes_is_tech(repo):
    await _link(repo, checked_ago=999)  # TTL 300 -> recheck due
    client = _client(user_active=True, in_group=True)
    data = _data()
    await _mw(repo, client)(AsyncMock(), _event(), data)
    client.get_user.assert_awaited_once_with(GLPI_ID)
    assert data["link"].is_tech is True
    assert (await repo.get_by_tg(TG_ID)).is_tech is True  # persisted


async def test_auto_unlink_when_glpi_account_deactivated(repo):
    await _link(repo, checked_ago=999)
    client = _client(user_active=False)  # disabled in AD/GLPI
    handler = AsyncMock()
    ev = _event(Message)
    await _mw(repo, client)(handler, ev, _data())
    handler.assert_not_awaited()  # treated as never linked
    ev.answer.assert_awaited_once_with(texts.NEED_LINK)
    assert await repo.get_by_tg(TG_ID) is None  # mapping removed


async def test_transient_glpi_error_keeps_cached_link(repo):
    await _link(repo, checked_ago=999)
    client = AsyncMock()
    client.get_user.side_effect = GlpiHTTPError("GLPI down")
    handler = AsyncMock(return_value="ok")
    # GLPI hiccup must NOT lock the user out; the cached link is used.
    assert await _mw(repo, client)(handler, _event(), _data()) == "ok"
    assert await repo.get_by_tg(TG_ID) is not None
