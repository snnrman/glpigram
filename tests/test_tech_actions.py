"""Light coverage for the tech-actions module (handlers tested minimally).

The substantive GLPI logic lives in the client (see test_client.py); here we
just pin the button wiring and the card text escaping.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from bot import texts
from bot.handlers.tech_actions import _card_keyboard_after_take, build_tech_actions_router


def test_router_builds():
    router = build_tech_actions_router(MagicMock())
    assert router.name == "tech_actions"


def test_after_take_keyboard_drops_take_keeps_comment_and_close():
    kb = _card_keyboard_after_take(5)
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert data == ["ta:comment:5", "ta:close:5"]
    assert all(not d.startswith("ta:take:") for d in data)


def test_card_text_escapes_name():
    # A crafted display name must not inject HTML into the card.
    out = texts.tech_card_taken("A <b>&</b> B")
    assert "<b>" not in out
    assert "&lt;b&gt;" in out and "&amp;" in out
