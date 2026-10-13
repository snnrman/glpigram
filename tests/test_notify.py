"""notify._send edge cases (flood wait, chat migration) and safe_edit."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramMigrateToChat, TelegramRetryAfter
from aiogram.types import Chat, Message, MessageEntity

from bot import texts
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
        from types import SimpleNamespace

        return SimpleNamespace(message_id=self.sent)


async def test_send_waits_out_one_flood_limit(monkeypatch):
    slept: list[int] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(notify.asyncio, "sleep", fake_sleep)
    bot = SeqBot([_retry_after(7), None])  # flood once, then ok

    assert await notify._send(bot, -100, "hi")
    assert slept == [7]
    assert bot.sent == 1


async def test_initial_claude_card_accepts_saved_approval_label():
    from bot.glpi.models import Ticket

    bot = SeqBot([None])
    bot.messages = []
    original = bot.send_message

    async def capture(chat_id, text, **kwargs):
        bot.messages.append(text)
        return await original(chat_id, text, **kwargs)

    bot.send_message = capture
    await notify.notify_new_ticket(
        bot,
        1,
        Ticket(
            id=7,
            name="Claude",
            content="body [Claude: lead + designated approver]",
            status=1,
            urgency=3,
        ),
        None,
        approval_label="⏳ Awaiting lead approval: Lead",
    )
    assert "Awaiting lead approval: Lead" in bot.messages[0]
    assert "[Claude: lead + designated approver]" not in bot.messages[0]
    assert "body" in bot.messages[0]


async def test_send_gives_up_on_repeated_flood(monkeypatch):
    monkeypatch.setattr(notify.asyncio, "sleep", AsyncMock())
    bot = SeqBot([_retry_after(7), _retry_after(7)])
    assert await notify._send(bot, -100, "hi") is None


async def test_send_gives_up_on_huge_flood_wait(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(notify.asyncio, "sleep", sleep)
    bot = SeqBot([_retry_after(3600)])  # not worth blocking the loop for
    assert await notify._send(bot, -100, "hi") is None
    sleep.assert_not_awaited()


async def test_send_reports_chat_migration(caplog):
    exc = TelegramMigrateToChat(method=MagicMock(), message="moved", migrate_to_chat_id=-100999)
    bot = SeqBot([exc])
    assert await notify._send(bot, -100, "hi") is None
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


@pytest.mark.parametrize(
    ("chat_id", "message_id", "expected"),
    [
        (-1001234567890, 999, "https://t.me/c/1234567890/999"),
        (-1000000000001, 1, "https://t.me/c/1/1"),
        (-1997852516352, 42, "https://t.me/c/997852516352/42"),
        (-1000000000000, 1, None),
        (-1997852516353, 1, None),
        (-123456789, 1, None),
        (123456789, 1, None),
        (-1001234567890, 0, None),
        (-1001234567890, -1, None),
    ],
)
def test_telegram_message_url_only_links_valid_supergroup_or_channel_ids(
    chat_id, message_id, expected
):
    assert notify.telegram_message_url(chat_id, message_id) == expected


async def test_mark_unassigned_taken_preserves_links_and_other_ticket():
    # Telegram returns text + UTF-16 entities, rather than the HTML we sent.
    header = "⚠️ Заявки без исполнителя:"
    first = "№7 «Канал 🧩 <мониторинг>»"
    second = "№71 «Сеть»"
    plain = f"{header}\n{first} (3ч)\n{second} (4ч)"
    urls = ["https://t.me/c/1234567890/999", "https://t.me/c/1234567890/1000"]
    entities = []
    for label, url in zip((first, second), urls, strict=True):
        entities.append(
            MessageEntity(
                type="text_link",
                offset=len(plain[: plain.index(label)].encode("utf-16-le")) // 2,
                length=len(label.encode("utf-16-le")) // 2,
                url=url,
            )
        )
    bot = AsyncMock()
    message = Message(
        message_id=555,
        date=datetime(2026, 7, 6, tzinfo=UTC),
        chat=Chat(id=-1001234567890, type="supergroup"),
        text=plain,
        entities=entities,
        reply_markup=notify.unassigned_take_keyboard([7, 71]),
    ).as_(bot)

    await notify.mark_unassigned_taken(message, 7, "Иван <админ>")

    bot.assert_awaited_once()
    edit = bot.call_args.args[0]
    lines = edit.text.split("\n")
    assert lines[1] == (
        f'<a href="{urls[0]}">№7 «Канал 🧩 &lt;мониторинг&gt;»</a> (3ч)'
        + texts.unassigned_taken_suffix("Иван <админ>")
    )
    assert lines[2] == f'<a href="{urls[1]}">{second}</a> (4ч)'
    assert [b.callback_data for row in edit.reply_markup.inline_keyboard for b in row] == [
        "ta:take:71"
    ]


# --- send_attachments: classify images / documents / oversized -----------------
class MediaBot:
    """Records the send_* calls send_attachments makes."""

    def __init__(self) -> None:
        self.photos: list[str] = []
        self.groups: list[int] = []
        self.documents: list[str] = []
        self.messages: list[str] = []

    async def send_photo(self, chat_id, photo, **kwargs):
        self.photos.append(photo.filename)

    async def send_media_group(self, chat_id, media, **kwargs):
        self.groups.append(len(media))

    async def send_document(self, chat_id, document, **kwargs):
        self.documents.append(document.filename)

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)
        return SimpleNamespace(message_id=len(self.messages))


async def test_send_attachments_image_goes_as_photo():
    bot = MediaBot()
    await notify.send_attachments(bot, 1, [("pic.jpg", "image/jpeg", b"x")])
    assert bot.photos == ["pic.jpg"]
    assert bot.documents == [] and bot.messages == []


async def test_send_attachments_non_image_goes_as_document():
    bot = MediaBot()
    await notify.send_attachments(bot, 1, [("report.pdf", "application/pdf", b"x")])
    assert bot.documents == ["report.pdf"]
    assert bot.photos == []


async def test_send_attachments_multiple_images_use_media_group():
    bot = MediaBot()
    items = [(f"p{i}.jpg", "image/jpeg", b"x") for i in range(3)]
    await notify.send_attachments(bot, 1, items)
    assert bot.groups == [3]


async def test_send_attachments_oversized_becomes_link_line():
    bot = MediaBot()
    big = b"x" * (notify.UPLOAD_MAX_BYTES + 1)
    await notify.send_attachments(
        bot, 1, [("huge.zip", "application/zip", big)], link_url="http://glpi/49"
    )
    assert bot.documents == [] and bot.photos == []
    assert bot.messages and "http://glpi/49" in bot.messages[0]


async def test_send_attachments_extra_oversized_reported_without_download():
    bot = MediaBot()
    await notify.send_attachments(
        bot, 1, [], link_url="http://glpi/49", extra_oversized=["dump.iso"]
    )
    assert bot.messages and "dump.iso" in bot.messages[0]


async def test_send_attachments_oversized_no_url_is_silent():
    bot = MediaBot()
    await notify.send_attachments(bot, 1, [], extra_oversized=["x.bin"])
    assert bot.messages == []  # nowhere to link -> nothing sent, no crash


async def test_send_attachments_large_image_falls_back_to_document():
    bot = MediaBot()
    big_img = b"x" * (notify.PHOTO_MAX_BYTES + 1)  # too big for sendPhoto, fits document
    await notify.send_attachments(bot, 1, [("wide.png", "image/png", big_img)])
    assert bot.documents == ["wide.png"] and bot.photos == []
