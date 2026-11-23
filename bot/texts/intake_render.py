"""Rendering for the single-text intake; old ticket renderers stay unchanged."""

from __future__ import annotations

import html
import re

CATEGORY_NAMES = {
    "ru": {
        "accounts": "Доступы и учётные записи",
        "network": "Сеть и удалённое подключение",
        "hardware": "Оборудование",
        "software": "Приложения и рабочее ПО",
        "infrastructure": "Инфраструктура и сервисы",
        "purchases": "Закупки и подписки",
        "other": "Прочее",
    },
    "en": {
        "accounts": "Access and accounts",
        "network": "Network and remote access",
        "hardware": "Hardware",
        "software": "Applications and desktop software",
        "infrastructure": "Infrastructure and services",
        "purchases": "Purchases and subscriptions",
        "other": "Other",
    },
}


def plain(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]*>", "", value))


def units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def escaped_clip(value: str, budget: int) -> str:
    """Clip before escaping, never split an HTML entity or a Unicode character."""
    out: list[str] = []
    used = 0
    for char in value:
        encoded = html.escape(char)
        size = units(encoded)
        if used + size > max(0, budget - 1):
            return "".join(out) + "…"
        out.append(encoded)
        used += size
    return "".join(out)


def render(
    *,
    lang: str,
    ticket_id: int,
    title: str,
    description: str | None,
    status_label: str,
    status: int,
    urgency: int | None,
    urgency_label: str,
    requester_name: str | None,
    requester_tg_id: int | None,
    assignee: str | None,
    url: str | None,
    intake: dict,
    approval_label: str | None = None,
    history: list[str] | None = None,
    attachments_count: int = 0,
    followups: list[str] | None = None,
) -> str:
    """Keep original description once, metadata readable, total under Telegram's cap."""
    ru = lang == "ru"
    head = [f"🆕 <b>{'Заявка №' if ru else 'Ticket #'}{ticket_id}</b>"]
    if status and status != 1:
        head.append(f"{'Статус' if ru else 'Status'}: {status_label}")
    if urgency is not None:
        head.append(
            "🔴 Срочно" if ru and urgency == 5 else "🔴 Urgent" if urgency == 5 else urgency_label
        )
    if approval_label:
        head.append(
            approval_label
            if units(approval_label) <= 300
            else escaped_clip(plain(approval_label), 300)
        )
    category = CATEGORY_NAMES[lang].get(
        intake.get("category"), f"GLPI #{intake.get('category_id', '?')}"
    )
    metadata = [f"📁 {html.escape(category)}"]
    env = "unknown" if intake.get("ai_stale") else intake.get("environment", "unknown")
    environment = {
        "production": "Прод" if ru else "Production",
        "development": "Разработка" if ru else "Development",
        "unknown": "Контур не определён" if ru else "Environment not determined",
    }
    metadata.append(f"🏷 {environment.get(env, environment['unknown'])}")
    if requester_name:
        safe = escaped_clip(requester_name, 180)
        mention = (
            f'<a href="tg://user?id={requester_tg_id}">{safe}</a>' if requester_tg_id else safe
        )
        metadata.append(f"👤 {mention}")
    if assignee:
        metadata.append(f"🙋 {escaped_clip(assignee, 150)}")
    if attachments_count:
        metadata.append(f"📎 {'Вложения' if ru else 'Attachments'}: {attachments_count}")
    state = intake.get("review_state", "pending")
    review = {
        "pending": "Проверка техника: ожидается" if ru else "Technician review: pending",
        "routine": "Техник: обычная заявка" if ru else "Technician: routine request",
        "approval": "Техник: направить на согласование" if ru else "Technician: send for approval",
    }[state]
    recommendation = (
        ("нужно согласование" if intake.get("approval_required") else "обычная заявка")
        if ru
        else ("approval needed" if intake.get("approval_required") else "routine request")
    )
    if intake.get("ai_stale"):
        recommendation = (
            "текст изменён, оценка устарела" if ru else "text changed; assessment outdated"
        )
    elif intake.get("status") == "fallback":
        recommendation = (
            "оценка недоступна, проверьте вручную" if ru else "unavailable; review manually"
        )
    metadata.append(f"🤖 {'ИИ' if ru else 'AI'}: {recommendation}")
    if intake.get("reason") and not intake.get("ai_stale"):
        metadata.append(escaped_clip(intake["reason"], 280))
    metadata.append(review)
    if intake.get("approval_recheck") and state == "pending":
        metadata.append(
            "Текст изменён после согласования. Проверьте, действует ли прежнее одобрение."
            if ru
            else "Text changed after approval. Check whether the existing approval still applies."
        )
    if url:
        metadata.append(
            f'🔗 <a href="{html.escape(url, quote=True)}">'
            f"{'Открыть в GLPI' if ru else 'Open in GLPI'}</a>"
        )
    tail = "\n\n".join(metadata)
    recent = followups if followups is not None else history
    if recent:
        label = (
            ("Последние комментарии" if ru else "Recent comments")
            if followups is not None
            else ("История" if ru else "History")
        )
        tail += (
            "\n\n<b>"
            + label
            + "</b>\n"
            + "\n".join(escaped_clip(plain(line), 160) for line in recent[-5:])
        )
    prefix = "\n".join(head) + "\n\n"
    # A stored original is plain text. Do not strip user-written <...> fragments.
    source = intake.get("display_description", intake.get("original_description"))
    if source is None:
        source = plain(description or title)
    display_title = intake.get("display_title", intake.get("title", title))
    if (
        not source.strip()
        or intake.get("title_generated") is False
        or display_title != intake.get("title")
    ) and (display_title.strip() and not source.strip().startswith(display_title.strip())):
        source = display_title + "\n\n" + source
    budget = max(100, 3900 - units(prefix) - units(tail) - 4)
    return prefix + escaped_clip(source, budget) + "\n\n" + tail
