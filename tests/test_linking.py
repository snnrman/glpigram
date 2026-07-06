"""Unit tests for the pure linking helpers (no aiogram runtime needed)."""

from __future__ import annotations

import pytest

from bot.handlers.linking import _parse_link_args, normalize_login


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("jdoe", "jdoe"),
        ("  jdoe  ", "jdoe"),
        ("jdoe@corp.local", "jdoe"),
        ("CORP\\jdoe", "jdoe"),
        ("CORP\\jdoe@corp.local", "jdoe"),
    ],
)
def test_normalize_login(raw, expected):
    assert normalize_login(raw) == expected


class _Msg:
    def __init__(self, reply_from_id=None):
        self.reply_to_message = None
        if reply_from_id is not None:
            user = type("U", (), {"id": reply_from_id})
            self.reply_to_message = type("R", (), {"from_user": user})


def test_parse_link_args_explicit_tg_id():
    assert _parse_link_args(_Msg(), "12345 jdoe") == (12345, "jdoe")


def test_parse_link_args_reply_form():
    # Reply to a user's message + just the login.
    assert _parse_link_args(_Msg(reply_from_id=999), "jdoe") == (999, "jdoe")


def test_parse_link_args_invalid():
    assert _parse_link_args(_Msg(), None) == (None, None)
    assert _parse_link_args(_Msg(), "jdoe") == (None, None)  # no tg_id, no reply
