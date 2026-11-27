"""Copy for the optional single-description intake flow (RU and EN)."""

from __future__ import annotations

import html

_URGENCY_LABELS = {
    "ru": {
        2: "🟢 Могу продолжать работу",
        3: "🟡 Работа затруднена",
        4: "🟠 Работа заблокирована",
        5: "🔴 Авария на проде",
    },
    "en": {
        2: "🟢 I can continue working",
        3: "🟡 Work is disrupted",
        4: "🟠 Work is blocked",
        5: "🔴 Production incident",
    },
}


def _choose(ru: str, en: str) -> str:
    # Follow the package's selected language, including test-time reloads.
    from . import _lang

    return en if _lang == "en" else ru


def description_prompt() -> str:
    return _choose(
        "<b>Подробно</b> опишите проблему или что нужно сделать.",
        "Describe the problem or what needs to be done in one message:",
    )


def urgency_label(value: int, *, lang: str | None = None) -> str:
    """Requester-selected impact; never infer the AI service environment from it."""
    if lang is None:
        from . import _lang

        lang = _lang
    labels = _URGENCY_LABELS["en" if lang == "en" else "ru"]
    return labels.get(value, str(value))


def urgency_prompt() -> str:
    return _choose(
        "Насколько проблема мешает работе?\n\n"
        f"{urgency_label(2, lang='ru')} — проблема почти не мешает, задача может подождать.\n\n"
        f"{urgency_label(3, lang='ru')} — могу работать, но сбои или обходные действия "
        "заметно тормозят.\n\n"
        f"{urgency_label(4, lang='ru')} — не могу продолжить задачу без помощи.\n\n"
        f"{urgency_label(5, lang='ru')} — сбой работающего продакшн-сервиса, "
        "влияющий на игроков или клиентов.",
        "How much does the problem affect your work?\n\n"
        f"{urgency_label(2, lang='en')} — the issue has little impact; the task can wait.\n\n"
        f"{urgency_label(3, lang='en')} — I can work, but failures or workarounds "
        "noticeably slow me down.\n\n"
        f"{urgency_label(4, lang='en')} — I cannot continue the task without help.\n\n"
        f"{urgency_label(5, lang='en')} — a live production service failure "
        "affecting players or customers.",
    )


def urgent_label() -> str:
    """Compatibility helper for the exceptional urgency level."""
    return urgency_label(5)


def urgent_warning() -> str:
    return _choose(
        "⚠️ Этот выбор уведомит команду сразу, включая ночь и выходные.\n\n"
        "Проблема затрагивает работающий продакшн?",
        "⚠️ This selection notifies the team immediately, including nights and weekends.\n\n"
        "Does the problem affect live production?",
    )


def urgent_confirm_label() -> str:
    return _choose("Да, авария на проде", "Yes, production incident")


def urgent_decline_label() -> str:
    return _choose("Изменить выбор", "Change selection")


def quiet_urgent_notice() -> str:
    return _choose(
        "Заявка отмечена как авария на проде — команда получит уведомление вне рабочего времени.",
        "The ticket is marked as a production incident — the team will be notified "
        "outside working hours.",
    )


def submitting_notice() -> str:
    return _choose(
        "Заявка отправляется. Дождись номера заявки.",
        "The ticket is being submitted. Please wait for its number.",
    )


def incomplete_notice() -> str:
    return _choose(
        "Не хватает описания или срочности. Начни заявку заново через /new.",
        "The description or urgency is missing. Start a new ticket with /new.",
    )


def category_unavailable() -> str:
    return _choose(
        "Не удалось проверить категорию заявки. Текст и вложения сохранены в этом диалоге.\n\n"
        "Попробуй отправить позже или обратись к технику.",
        "The ticket category could not be checked. Your text and attachments are kept "
        "in this conversation.\n\nTry sending later or contact a technician.",
    )


def creation_uncertain() -> str:
    return _choose(
        "GLPI не подтвердил результат отправки. Заявка могла быть создана.\n\n"
        "Проверь «Мои заявки» или обратись к технику перед повторной отправкой.",
        "GLPI did not confirm the submission result. The ticket may have been created.\n\n"
        "Check My tickets or contact a technician before submitting again.",
    )


def review_required() -> str:
    return _choose(
        "Сначала выберите «👨‍💼 На согласование» или «🛠 Обычная заявка».",
        "First choose “👨‍💼 Request approval” or “🛠 Routine request”.",
    )


def handoff_to_new(ticket_id: int, title: str, urgency: int | None) -> str:
    """Intake urgency is independent of the service environment, including in DMs."""
    line = _choose(
        f"🔄 На вас переназначена заявка №{ticket_id}: {html.escape(title)}",
        f"🔄 Ticket #{ticket_id} has been reassigned to you: {html.escape(title)}",
    )
    if urgency is not None:
        label = urgency_label(urgency)
        line += _choose(f", срочность {label}", f", urgency {label}")
    return line
