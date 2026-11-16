"""Privacy-restricted requesters keep a usable profile link in all ticket views."""

from __future__ import annotations

import asyncio
import html
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetChat, SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity

from bot.db.repo import Repo
from bot.glpi.models import Ticket
from bot.services.cards import CardService
from bot.services.profile_links import ProfileLinksMiddleware
from bot.texts import en, ru

TG_ID = 555
MENTION = f'<a href="tg://user?id={TG_ID}">User &lt;Name&gt;</a>'
PUBLIC_URL = "https://t.me/public_user"
GLPI_URL = "https://glpi.local/front/user.form.php?id=38"


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.chat = SimpleNamespace(
            username="public_user", active_usernames=None, has_private_forwards=True
        )
        self.lookups = []
        self.messages = []
        self.error = None

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, GetChat):
            self.lookups.append((method.chat_id, timeout))
            if self.error is not None:
                raise self.error
            return self.chat
        self.messages.append(method)
        return SimpleNamespace(message_id=123)

    async def close(self):
        pass

    async def stream_content(self, url, **kwargs):
        yield b""


@pytest.fixture
async def harness(tmp_path):
    repo = Repo(str(tmp_path / "profiles.sqlite3"))
    await repo.connect()
    await repo.upsert_link(
        tg_id=TG_ID, glpi_users_id=38, display_name="User <Name>", is_tech=False, now=0
    )
    clock = [0.0]
    links = ProfileLinksMiddleware(
        repo, front_base="https://glpi.local", now_provider=lambda: clock[0]
    )
    session = FakeSession()
    bot = Bot("42:test-token", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    bot.session.middleware(links)
    yield bot, session, links, repo, clock
    await bot.session.close()
    await repo.close()


@pytest.mark.parametrize("module", [ru, en])
@pytest.mark.parametrize("view", ["card", "detail", "lead", "claude"])
async def test_private_forward_setting_does_not_remove_requester_profile(harness, module, view):
    bot, session, _, _, _ = harness
    common = {"ticket_id": 7, "url": "https://glpi.local/front/ticket.form.php?id=7"}
    if view == "card":
        text = module.notify_new_ticket(
            **common,
            title="Printer",
            status=1,
            requester_name="User <Name>",
            requester_tg_id=TG_ID,
        )
    elif view == "detail":
        text = module.ticket_detail(
            **common,
            title="Printer",
            status=1,
            assignee=None,
            followups=[],
            requester="User <Name>",
            requester_tg_id=TG_ID,
        )
    elif view == "lead":
        text = module.acc_lead_prompt(
            **common,
            request="Access",
            requester="User <Name>",
            requester_tg_id=TG_ID,
        )
    else:
        text = module.claude_approver_card(
            **common,
            lead="Lead",
            request="Access",
            requester="User <Name>",
            requester_tg_id=TG_ID,
        )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Take", callback_data="ta:take:7")]]
    )
    original = SendMessage(chat_id=-100, text=text, reply_markup=keyboard)
    await bot(original)
    await bot.edit_message_text(text, chat_id=-100, message_id=123, reply_markup=keyboard)
    for sent in session.messages:
        assert f'<a href="{PUBLIC_URL}">User &lt;Name&gt;</a>' in sent.text
        assert f"tg://user?id={TG_ID}" not in sent.text
        assert sent.reply_markup == keyboard
        assert common["url"] in sent.text
    assert original.text == text  # no mutation of the caller's retryable method
    assert session.lookups == [(TG_ID, 3)]


async def test_old_saved_card_uses_public_profile_after_restart_and_on_every_edit(harness):
    bot, session, _, repo, _ = harness
    cards = CardService(repo, front_base="https://glpi.local")
    await cards.register(
        Ticket(id=7, name="Printer", content="Description", status=1, urgency=3),
        chat_id=-100,
        message_id=123,
        requester_name="User <Name>",
        requester_tg_id=TG_ID,
        attachments_count=2,
        now=0,
    )
    # This is the legacy storage format: just an ID, no saved username.
    await cards.record_event(bot, 7, "Taken", status=2, taken_by="Technician")
    await cards.record_event(bot, 7, "Solved", status=5)
    assert len(session.messages) == 2
    assert all(PUBLIC_URL in method.text for method in session.messages)
    assert "Description" in session.messages[-1].text
    assert "Taken" in session.messages[-1].text and "Solved" in session.messages[-1].text
    card = await repo.get_card(7)
    assert card.requester_tg_id == TG_ID and card.message_id == 123


async def test_no_username_with_private_forwards_links_to_glpi(harness):
    bot, session, _, _, _ = harness
    session.chat.username = None
    await bot.send_message(-100, MENTION)
    assert f'href="{GLPI_URL}"' in session.messages[-1].text


async def test_id_mention_remains_for_user_without_username_and_without_restriction(harness):
    bot, session, _, _, _ = harness
    session.chat.username = None
    session.chat.has_private_forwards = None
    await bot.send_message(-100, MENTION)
    assert session.messages[-1].text == MENTION


async def test_active_public_username_is_used(harness):
    bot, session, _, _, _ = harness
    session.chat.username = None
    session.chat.active_usernames = ["active_user"]
    await bot.send_message(-100, MENTION)
    assert 'href="https://t.me/active_user"' in session.messages[-1].text


async def test_username_changes_and_removal_are_refreshed(harness):
    bot, session, _, _, clock = harness
    await bot.send_message(-100, MENTION)
    session.chat.username = "renamed_user"
    clock[0] = 601
    await bot.edit_message_text(MENTION, chat_id=-100, message_id=123)
    assert 'href="https://t.me/renamed_user"' in session.messages[-1].text
    session.chat.username = None
    clock[0] = 1202
    await bot.edit_message_text(MENTION, chat_id=-100, message_id=123)
    assert GLPI_URL in session.messages[-1].text
    assert "renamed_user" not in session.messages[-1].text


async def test_timeout_keeps_verified_link_and_retries_later(harness):
    bot, session, _, _, clock = harness
    await bot.send_message(-100, MENTION)
    clock[0] = 601
    session.error = TimeoutError()
    await bot.edit_message_text(MENTION, chat_id=-100, message_id=123)
    assert PUBLIC_URL in session.messages[-1].text
    await bot.edit_message_text(MENTION, chat_id=-100, message_id=123)
    assert len(session.lookups) == 2
    session.error = None
    session.chat.username = "recovered_user"
    clock[0] = 632
    await bot.edit_message_text(MENTION, chat_id=-100, message_id=123)
    assert "https://t.me/recovered_user" in session.messages[-1].text


async def test_cold_lookup_failure_still_sends_a_linked_card(harness):
    bot, session, _, _, _ = harness
    session.error = TimeoutError()
    await bot.send_message(-100, MENTION)
    assert GLPI_URL in session.messages[-1].text


async def test_fallback_uses_current_glpi_mapping(harness):
    bot, session, _, repo, _ = harness
    session.chat.username = None
    await bot.send_message(-100, MENTION)
    await repo.upsert_link(tg_id=TG_ID, glpi_users_id=99, display_name="User", is_tech=False, now=1)
    await bot.send_message(-100, MENTION)
    assert "front/user.form.php?id=99" in session.messages[-1].text
    assert len(session.lookups) == 1


async def test_concurrent_messages_share_lookup_and_repeated_mentions(harness):
    bot, session, _, _, _ = harness
    await asyncio.gather(*(bot.send_message(-100, MENTION + MENTION) for _ in range(5)))
    assert len(session.lookups) == 1
    assert all(method.text.count(PUBLIC_URL) == 2 for method in session.messages)


@pytest.mark.parametrize("mode", [None, "MarkdownV2"])
async def test_non_html_and_explicit_entities_are_untouched(harness, mode):
    bot, session, _, _, _ = harness
    await bot.send_message(-100, MENTION, parse_mode=mode)
    await bot.send_message(-100, MENTION, entities=[MessageEntity(type="bold", offset=0, length=3)])
    assert session.lookups == []
    assert all(method.text == MENTION for method in session.messages)


async def test_escaped_user_text_cannot_trigger_a_profile_lookup(harness):
    bot, session, _, _, _ = harness
    escaped = html.escape(MENTION)
    await bot.send_message(-100, escaped)
    assert session.lookups == []
    assert session.messages[-1].text == escaped


async def test_missing_mapping_never_invents_a_different_profile(harness):
    bot, session, _, repo, _ = harness
    repo.get_by_tg = AsyncMock(return_value=None)
    session.chat.username = None
    await bot.send_message(-100, MENTION)
    assert session.messages[-1].text == MENTION


async def test_cancellation_propagates(harness):
    bot, session, _, _, _ = harness
    session.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await bot.send_message(-100, MENTION)
    assert session.messages == []
