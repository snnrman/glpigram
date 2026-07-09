"""Role-based menu + /stats: techs see and may call it, regular users neither.

Dispatch tests run on the full production dispatcher (router order + auth
middleware), same harness as test_close_flows.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from aiogram.types import Chat as TgChat
from aiogram.types import Message, Update
from aiogram.types import User as TgUser

from bot import texts
from bot.db.repo import Repo
from bot.glpi.client import GlpiClient
from bot.handlers.new_ticket import main_menu_keyboard

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("GLPI_API_URL", "http://127.0.0.1/apirest.php")
os.environ.setdefault("GLPI_USER_TOKEN", "u")

from bot.config import Settings  # noqa: E402
from bot.main import build_dispatcher  # noqa: E402

BOT_ID = 42
TECH_ID = 2002
USER_ID = 1001
_DATE = datetime(2020, 1, 1, tzinfo=UTC)


# -- the menu keyboard itself -------------------------------------------------
def _labels(kb) -> list[list[str]]:
    return [[b.text for b in row] for row in kb.keyboard]


def test_menu_for_regular_user_has_no_stats():
    assert _labels(main_menu_keyboard()) == [[texts.BTN_NEW_TICKET, texts.BTN_MY_TICKETS]]


def test_menu_for_tech_adds_short_two_button_row():
    assert _labels(main_menu_keyboard(is_tech=True)) == [
        [texts.BTN_NEW_TICKET, texts.BTN_MY_TICKETS],
        [texts.BTN_TECH_TICKETS, texts.BTN_STATS],
    ]


# -- /stats via the production dispatcher -------------------------------------
class FakeBot:
    id = BOT_ID

    def __init__(self) -> None:
        self.sent: list[tuple[object, str, object]] = []  # (chat_id, text, reply_markup)

    async def __call__(self, method, **kwargs):
        if type(method).__name__ == "SendMessage":
            self.sent.append((method.chat_id, method.text, method.reply_markup))
        return MagicMock()

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent.append((chat_id, text, reply_markup))
        return MagicMock()


@pytest.fixture
async def env(tmp_path):
    repo = Repo(str(tmp_path / "stats.sqlite3"))
    await repo.connect()
    now = int(time.time())
    await repo.upsert_link(
        tg_id=TECH_ID, glpi_users_id=9, display_name="Техник", is_tech=True, now=now
    )
    await repo.upsert_link(
        tg_id=USER_ID, glpi_users_id=8, display_name="Юзер", is_tech=False, now=now
    )
    client = AsyncMock()
    client.count_open_tickets_by_status.return_value = {1: 3, 2: 5, 5: 1}
    dp = build_dispatcher(client, repo, Settings())
    yield dp, client, repo
    await repo.close()


def _dm(bot, uid, from_id, text):
    msg = Message(
        message_id=uid,
        date=_DATE,
        chat=TgChat(id=from_id, type="private"),
        from_user=TgUser(id=from_id, is_bot=False, first_name="U"),
        text=text,
    ).as_(bot)
    return Update(update_id=uid, message=msg)


async def test_tech_gets_stats_by_command(env):
    dp, client, _repo = env
    bot = FakeBot()
    await dp.feed_update(bot, _dm(bot, 1, TECH_ID, "/stats"))
    chat, text, markup = bot.sent[-1]
    assert chat == TECH_ID
    assert text.startswith(texts.stats_summary({1: 3, 2: 5, 5: 1}))
    assert "9" in text  # ticket total
    # users section: 2 linked (both just now), 1 tech
    assert text.endswith(texts.stats_users_block(2, 1, 2))
    # the reply re-renders the tech menu (role evaluated at render time)
    assert [b.text for b in markup.keyboard[-1]] == [texts.BTN_TECH_TICKETS, texts.BTN_STATS]


async def test_tech_gets_stats_by_menu_button(env):
    dp, client, _repo = env
    bot = FakeBot()
    await dp.feed_update(bot, _dm(bot, 1, TECH_ID, texts.BTN_STATS))
    assert texts.stats_users_block(2, 1, 2) in bot.sent[-1][1]


async def test_old_links_drop_out_of_the_7_day_count(env):
    dp, client, repo = env
    old = int(time.time()) - 8 * 86400
    await repo.upsert_link(
        tg_id=3003, glpi_users_id=10, display_name="Старый", is_tech=False, now=old
    )
    bot = FakeBot()
    await dp.feed_update(bot, _dm(bot, 1, TECH_ID, "/stats"))
    assert texts.stats_users_block(3, 1, 2) in bot.sent[-1][1]  # total 3, recent still 2


async def test_users_section_failure_is_logged_not_silent(env, caplog):
    dp, client, repo = env
    bot = FakeBot()

    async def boom(**kwargs):
        raise RuntimeError("no such column: linked_at")

    repo.user_stats = boom
    await dp.feed_update(bot, _dm(bot, 1, TECH_ID, "/stats"))
    text = bot.sent[-1][1]
    assert text.startswith(texts.stats_summary({1: 3, 2: 5, 5: 1}))  # tickets survive
    assert texts.STATS_USERS_UNAVAILABLE in text  # section visibly degraded
    assert any("stats_users_failed" in r.message for r in caplog.records)  # and logged


async def test_regular_user_is_refused_even_by_direct_command(env):
    dp, client, _repo = env
    bot = FakeBot()
    await dp.feed_update(bot, _dm(bot, 1, USER_ID, "/stats"))
    assert bot.sent[-1][1] == texts.STATS_TECH_ONLY
    client.count_open_tickets_by_status.assert_not_called()


async def test_regular_user_typing_the_button_text_is_refused_too(env):
    dp, client, _repo = env
    bot = FakeBot()
    await dp.feed_update(bot, _dm(bot, 1, USER_ID, texts.BTN_STATS))
    assert bot.sent[-1][1] == texts.STATS_TECH_ONLY
    client.count_open_tickets_by_status.assert_not_called()


# -- the GLPI count query ------------------------------------------------------
async def test_count_open_tickets_by_status_groups_and_paginates():
    client = GlpiClient("http://glpi.local/apirest.php", "", "u", timeout=1.0)
    try:
        with respx.mock(assert_all_called=False) as router:
            router.get(url__regex=r".*/initSession.*").respond(200, json={"session_token": "tok"})
            pages = iter(
                [
                    {"totalcount": 3, "data": [{"2": 1, "12": 1}, {"2": 2, "12": 2}]},
                    {"totalcount": 3, "data": [{"2": 3, "12": 1}]},
                ]
            )
            route = router.get(url__regex=r".*/search/Ticket.*").mock(
                side_effect=lambda req: httpx.Response(200, json=next(pages))
            )
            counts = await client.count_open_tickets_by_status()
    finally:
        await client.close()
    assert counts == {1: 2, 2: 1}
    assert route.call_count == 2
    first = route.calls[0].request.url
    assert "notclosed" in str(first)


def test_stats_summary_renders_counts_and_empty():
    out = texts.stats_summary({1: 3, 2: 5})
    assert "8" in out and "3" in out and "5" in out
    assert texts.ticket_status_label(1) in out
    empty = texts.stats_summary({})
    assert "🎉" in empty
