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
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    MessageEntity,
)

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
    original = SendMessage(
        chat_id=-100,
        text=text,
        reply_markup=keyboard,
        link_preview_options=LinkPreviewOptions(is_disabled=False),
    )
    await bot(original)
    await bot.edit_message_text(text, chat_id=-100, message_id=123, reply_markup=keyboard)
    for sent in session.messages:
        assert f'<a href="{PUBLIC_URL}">User &lt;Name&gt;</a>' in sent.text
        assert f"tg://user?id={TG_ID}" not in sent.text
        assert sent.reply_markup == keyboard
        assert common["url"] in sent.text
        assert sent.link_preview_options.is_disabled is True
    assert original.text == text  # no mutation of the caller's retryable method
    assert original.link_preview_options.is_disabled is False
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
    assert all(method.link_preview_options.is_disabled is True for method in session.messages)
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


@pytest.mark.parametrize("module", [ru, en])
@pytest.mark.parametrize("view", ["card", "detail"])
@pytest.mark.parametrize("intake", [None, {"category": "accounts", "review_state": "pending"}])
async def test_unlinked_requester_has_glpi_profile_without_preview(harness, module, view, intake):
    bot, session, _, _, _ = harness
    common = {
        "ticket_id": 7,
        "url": "https://glpi.local/helpdesk/front/ticket.form.php?id=7",
        "title": "Request",
        "status": 1,
        "requester_glpi_id": 88,
        "intake": intake,
    }
    if view == "card":
        rendered = module.notify_new_ticket(**common, requester_name="New <Owner>")
    else:
        rendered = module.ticket_detail(
            **common, requester="New <Owner>", assignee=None, followups=[]
        )
    await bot.send_message(-100, rendered)
    await bot.edit_message_text(rendered, chat_id=-100, message_id=123)

    assert not session.lookups
    for method in session.messages:
        assert (
            '<a href="https://glpi.local/helpdesk/front/user.form.php?id=88">New &lt;Owner&gt;</a>'
        ) in method.text
        assert method.link_preview_options.is_disabled is True


async def test_unlinked_card_profile_survives_storage_and_edits(harness):
    bot, session, _, repo, _ = harness
    cards = CardService(repo, front_base="https://glpi.local")
    await cards.register(
        Ticket(id=7, name="Request", content="Description", status=1, urgency=3),
        chat_id=-100,
        message_id=123,
        requester_name="New Owner",
        requester_tg_id=None,
        requester_glpi_id=88,
        attachments_count=0,
        now=0,
    )
    await repo.close()
    await repo.connect()
    await CardService(repo, front_base="https://glpi.local").record_event(bot, 7, "Taken", status=2)
    assert (await repo.get_card(7)).requester_glpi_id == 88
    assert "front/user.form.php?id=88" in session.messages[-1].text
    assert session.messages[-1].link_preview_options.is_disabled is True


async def test_old_card_lazily_repairs_missing_glpi_id_from_live_requester(harness):
    bot, session, _, repo, _ = harness
    client = SimpleNamespace(get_ticket_requester=AsyncMock(return_value=(88, "New Owner")))
    cards = CardService(repo, front_base="https://glpi.local", client=client)
    await cards.register(
        Ticket(id=7, name="Request", content="Description", status=1, urgency=3),
        chat_id=-100,
        message_id=123,
        requester_name="Old Owner",
        requester_tg_id=TG_ID,
        attachments_count=0,
        now=0,
    )

    await cards.record_event(bot, 7, "Taken", status=2)
    await cards.record_event(bot, 7, "Updated")

    client.get_ticket_requester.assert_awaited_once_with(7)
    card = await repo.get_card(7)
    assert card.requester_glpi_id == 88 and card.requester_tg_id is None
    assert card.requester_name == "New Owner"
    assert all("front/user.form.php?id=88" in method.text for method in session.messages)
    assert all(method.link_preview_options.is_disabled for method in session.messages)
    assert not session.lookups


@pytest.mark.parametrize("lookup_error", [None, TimeoutError()])
async def test_old_card_updates_history_while_waiting_for_live_requester(harness, lookup_error):
    bot, session, _, repo, _ = harness
    client = SimpleNamespace(
        get_ticket_requester=AsyncMock(return_value=None, side_effect=lookup_error)
    )
    cards = CardService(repo, front_base="https://glpi.local", client=client)
    await cards.register(
        Ticket(id=7, name="Request", content="Description", status=1, urgency=3),
        chat_id=-100,
        message_id=123,
        requester_name="Old Owner",
        requester_tg_id=TG_ID,
        attachments_count=0,
        now=0,
    )
    await cards.record_event(bot, 7, "Taken", status=2)
    assert "Taken" in session.messages[-1].text
    assert "Old Owner" not in session.messages[-1].text
    assert "front/user.form.php" not in session.messages[-1].text
    assert not session.lookups
    assert (await repo.get_card(7)).requester_glpi_id is None
    client.get_ticket_requester.side_effect = None
    client.get_ticket_requester.return_value = (88, "New Owner")

    await cards.record_event(bot, 7, "Updated")

    assert "front/user.form.php?id=88" in session.messages[-1].text
    assert "Taken" in session.messages[-1].text and "Updated" in session.messages[-1].text
