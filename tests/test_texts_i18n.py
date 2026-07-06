"""The ru/en text modules must stay perfect mirrors of each other."""

from __future__ import annotations

import inspect

from bot.texts import en, ru


def _public(module) -> dict[str, object]:
    return {
        name: obj
        for name, obj in vars(module).items()
        if not name.startswith("_") and not inspect.ismodule(obj)
    }


def test_ru_and_en_expose_identical_key_sets():
    ru_keys, en_keys = set(_public(ru)), set(_public(en))
    assert ru_keys == en_keys, (
        f"only in ru: {sorted(ru_keys - en_keys)}; only in en: {sorted(en_keys - ru_keys)}"
    )


def test_matching_kinds_and_signatures():
    ru_pub, en_pub = _public(ru), _public(en)
    for name, ru_obj in ru_pub.items():
        en_obj = en_pub[name]
        assert callable(ru_obj) == callable(en_obj), f"{name}: callable in one language only"
        if callable(ru_obj):
            assert inspect.signature(ru_obj) == inspect.signature(en_obj), (
                f"{name}: signatures differ"
            )
        else:
            assert type(ru_obj) is type(en_obj), f"{name}: types differ"


def test_default_language_is_russian():
    # BOT_LANGUAGE is unset in the test environment -> the package exports ru.
    from bot import texts

    assert texts.BTN_NEW_TICKET == ru.BTN_NEW_TICKET


def test_en_functions_render():
    # Smoke the parametric en strings (mirrors what ru tests cover elsewhere).
    assert "#7" in en.ticket_created(7, None)
    assert en.deferred_batch_header(1).endswith("1 ticket arrived outside working hours:")
    assert en.deferred_batch_header(5).endswith("5 tickets arrived outside working hours:")
    assert "Is that you?" in en.link_name_pick_one("John <b>Doe</b>")
    assert "<b>Doe</b>" not in en.link_name_pick_one("John <b>Doe</b>")  # escaped
    out = en.notify_closed_by_requester(ticket_id=7, reason=None, assignees=["Jane"])
    assert "without a comment" in out and "Jane" in out
