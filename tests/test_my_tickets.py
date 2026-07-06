"""Light coverage for the /tickets module and its rendering helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

from bot import texts
from bot.glpi.client import _parse_user_refs
from bot.glpi.models import TicketSummary
from bot.handlers.my_tickets import (
    _close_confirm_keyboard,
    _detail_keyboard,
    _list_keyboard,
    build_my_tickets_router,
)


def test_router_builds():
    router = build_my_tickets_router(
        MagicMock(), MagicMock(), tech_group_chat_id=-100, ticket_front_base=None
    )
    assert router.name == "my_tickets"


def test_list_keyboard_one_button_per_ticket():
    summaries = [
        TicketSummary(id=5, title="Принтер", status=1),
        TicketSummary(id=6, title="ВПН", status=2),
    ]
    kb = _list_keyboard(summaries)
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert data == ["mt:open:5", "mt:open:6"]


def test_detail_keyboard_closable_shows_close_button():
    kb = _detail_keyboard(5, closable=True)
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert data == ["mt:comment:5", "mt:close:5", "mt:list"]


def test_detail_keyboard_not_closable_hides_close_button():
    data = [
        b.callback_data for row in _detail_keyboard(5, closable=False).inline_keyboard for b in row
    ]
    assert data == ["mt:comment:5", "mt:list"]
    assert "mt:close:5" not in data


def test_close_confirm_keyboard():
    data = [b.callback_data for row in _close_confirm_keyboard().inline_keyboard for b in row]
    assert data == ["mt:close_yes", "mt:close_no"]


def test_notify_closed_by_requester_mentions_assignee_and_escapes():
    out = texts.notify_closed_by_requester(
        ticket_id=7, reason="дубль <b>x</b>", assignees=["Иван Петров"]
    )
    assert "№7" in out
    assert "<b>x</b>" not in out  # reason escaped
    assert "Был назначен: Иван Петров" in out


def test_notify_closed_by_requester_without_assignee():
    out = texts.notify_closed_by_requester(ticket_id=7, reason="не актуально", assignees=[])
    assert "Был назначен" not in out


def test_parse_user_refs_ids_names_and_junk():
    assert _parse_user_refs(None) == ([], None)
    assert _parse_user_refs("0") == ([], None)
    assert _parse_user_refs("42") == ([42], None)
    assert _parse_user_refs(["42", "7"]) == ([42, 7], None)
    assert _parse_user_refs("Иван Петров") == ([], "Иван Петров")
    assert _parse_user_refs(["42", "Иван"]) == ([42], "Иван")


def test_detail_text_escapes_title_and_uses_status_label():
    out = texts.ticket_detail(
        ticket_id=5, title="A <b>x</b>", status=2, assignee=None, followups=[]
    )
    assert "<b>x</b>" not in out  # title escaped
    assert texts.ticket_status_label(2) in out
    assert texts.MYT_NO_FOLLOWUPS in out
    assert texts.MYT_UNASSIGNED in out  # no assignee


def test_followup_line_escapes_and_cleans_html():
    line = texts.followup_line("Иван", "<p>привет &amp; пока</p>")
    assert "<p>" not in line  # tags stripped
    # entity decoded by clean, then re-escaped for safe HTML rendering
    assert "привет &amp; пока" in line
    assert "<b>Иван:</b>" in line
