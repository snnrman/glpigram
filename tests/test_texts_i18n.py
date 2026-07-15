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


# --- WELCOME_MESSAGE override -------------------------------------------------
def test_custom_welcome_unset_and_blank(monkeypatch):
    from bot import texts

    monkeypatch.delenv("WELCOME_MESSAGE", raising=False)
    assert texts.custom_welcome() is None
    monkeypatch.setenv("WELCOME_MESSAGE", "   ")
    assert texts.custom_welcome() is None  # blank counts as unset


def test_custom_welcome_verbatim_with_html_and_newlines(monkeypatch):
    from bot import texts

    monkeypatch.setenv("WELCOME_MESSAGE", "Привет, <b>ACME</b>!\\nWe help.")
    assert texts.custom_welcome() == "Привет, <b>ACME</b>!\nWe help."


def test_urgency_line_full_scale_and_unknown():
    assert ru.urgency_line(4) == "🔴 Срочность: высокая"
    assert ru.urgency_line(3) == "🟡 Срочность: средняя"
    assert ru.urgency_line(2) == "🟢 Срочность: низкая"
    assert ru.urgency_line(1) == "⚪ Срочность: очень низкая"
    assert ru.urgency_line(5) == "🚨 Срочность: очень высокая"
    assert ru.urgency_line(9) == "Срочность: 9"  # unknown -> no crash
    assert en.urgency_line(4) == "🔴 Urgency: high"
    assert en.urgency_line(9) == "Urgency: 9"


def test_urgent_prod_level_label_and_card_mark():
    # The dedicated urgent (prod) level (GLPI urgency 5) has a distinct label in
    # the /new summary and an explicit, loud banner on the tech-group card.
    assert ru.urgency_label(5) == ru.URGENCY_URGENT_LABEL == "🔴 Срочно (прод)"
    assert en.urgency_label(5) == en.URGENCY_URGENT_LABEL == "🔴 Urgent (prod)"
    assert "СРОЧНО (прод)" in ru.urgency_card_line(5)
    assert "URGENT (prod)" in en.urgency_card_line(5)
    # Ordinary high (4) stays the generic scale wording — no urgent banner.
    assert "высокая" in ru.urgency_card_line(4).lower()


# --- clickable ticket links + escaping (no bare URLs in HTML mode) -------------
URL = "https://glpi.local/front/ticket.form.php?id=49"


def test_ticket_links_are_anchors_not_bare_urls():
    for mod, ref in ((ru, "№49"), (en, "#49")):
        created = mod.ticket_created(49, URL)
        assert f'<a href="{URL}">{ref}</a>' in created
        assert "\n" not in created  # no separate URL line anymore
        status = mod.notify_status_change(ticket_id=49, title="t", status=2, url=URL)
        follow = mod.notify_followup(ticket_id=49, title="t", body="x", url=URL)
        detail = mod.ticket_detail(
            ticket_id=49, title="t", status=2, assignee=None, followups=[], url=URL
        )
        for out in (created, status, follow, detail):
            assert f'href="{URL}"' in out
            assert f"\n{URL}" not in out and not out.endswith(URL)  # never bare


def test_ticket_links_degrade_without_url():
    assert ru.ticket_created(49, None) == "✅ Заявка №49 создана."
    assert en.ticket_created(49, None) == "✅ Ticket #49 created."


def test_user_supplied_text_is_escaped_in_html_messages():
    hostile = "<b>Иван & Ко</b>"
    for mod in (ru, en):
        summary = mod.confirm_summary(hostile, 3, hostile, hostile)
        request = mod.link_request(
            tg_id=1, tg_name=hostile, glpi_id=2, glpi_name=hostile, login=hostile
        )
        resolved = mod.link_request_resolved(
            approved=True, glpi_name=hostile, login=hostile, by=hostile
        )
        admin = mod.admin_link_ok(tg_id=1, glpi_name=hostile, login=hostile)
        for out in (summary, request, resolved, admin):
            assert "<b>Иван" not in out
            assert "&lt;b&gt;Иван &amp; Ко&lt;/b&gt;" in out
