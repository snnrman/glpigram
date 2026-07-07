"""notify._send edge cases (flood wait, chat migration) and safe_edit."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.exceptions import TelegramMigrateToChat, TelegramRetryAfter

from bot.services import notify


def _retry_after(seconds: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(method=MagicMock(), message="flood", retry_after=seconds)


class SeqBot:
    """send_message raises/succeeds per the given script."""

    def __init__(self, script) -> None:
        self.script = list(script)
        self.sent = 0

    async def send_message(self, chat_id, text, **kwargs):
        action = self.script.pop(0)
        if isinstance(action, Exception):
            raise action
        self.sent += 1


async def test_send_waits_out_one_flood_limit(monkeypatch):
    slept: list[int] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(notify.asyncio, "sleep", fake_sleep)
    bot = SeqBot([_retry_after(7), None])  # flood once, then ok

    assert await notify._send(bot, -100, "hi") is True
    assert slept == [7]
    assert bot.sent == 1


async def test_send_gives_up_on_repeated_flood(monkeypatch):
    monkeypatch.setattr(notify.asyncio, "sleep", AsyncMock())
    bot = SeqBot([_retry_after(7), _retry_after(7)])
    assert await notify._send(bot, -100, "hi") is False


async def test_send_gives_up_on_huge_flood_wait(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(notify.asyncio, "sleep", sleep)
    bot = SeqBot([_retry_after(3600)])  # not worth blocking the loop for
    assert await notify._send(bot, -100, "hi") is False
    sleep.assert_not_awaited()


async def test_send_reports_chat_migration(caplog):
    exc = TelegramMigrateToChat(method=MagicMock(), message="moved", migrate_to_chat_id=-100999)
    bot = SeqBot([exc])
    assert await notify._send(bot, -100, "hi") is False
    assert any("-100999" in r.getMessage() for r in caplog.records)  # new id is loud


async def test_safe_edit_inaccessible_message_returns_false():
    # Old callbacks carry InaccessibleMessage (not a Message) -> no crash.
    cb = SimpleNamespace(message=SimpleNamespace(chat=None))
    assert await notify.safe_edit(cb, "x") is False
    cb_none = SimpleNamespace(message=None)
    assert await notify.safe_edit(cb_none, "x") is False


def test_tech_keyboard_take_is_full_width_bottom_row():
    from bot import texts

    kb = notify.tech_ticket_keyboard(7)
    rows = [[(b.text, b.callback_data) for b in row] for row in kb.inline_keyboard]
    assert rows[0] == [
        (texts.BTN_TECH_COMMENT, "ta:comment:7"),
        (texts.BTN_TECH_CLOSE, "ta:close:7"),
    ]
    assert rows[1] == [(texts.BTN_TECH_TAKE, "ta:take:7")]  # primary, alone = full width
