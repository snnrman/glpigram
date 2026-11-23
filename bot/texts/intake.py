"""Copy for the optional single-description intake flow (RU and EN)."""

from __future__ import annotations

import html


def _choose(ru: str, en: str) -> str:
    # Follow the package's selected language, including test-time reloads.
    from . import _lang

    return en if _lang == "en" else ru


def description_prompt() -> str:
    return _choose(
        "Опиши проблему или что нужно сделать одним сообщением:",
        "Describe the problem or what needs to be done in one message:",
    )


def urgent_label() -> str:
    return _choose("🔴 Срочно", "🔴 Urgent")


def urgent_warning() -> str:
    return _choose(
        "⚠️ Срочная заявка уведомит команду в любое время суток, включая ночь и выходные.\n\n"
        "Выбирай этот уровень, если задачу нельзя отложить до рабочего времени.",
        "⚠️ An urgent ticket notifies the team at any time, including nights and weekends.\n\n"
        "Choose this level if the issue cannot wait until working hours.",
    )


def quiet_urgent_notice() -> str:
    return _choose(
        "Заявка отмечена как срочная — команда получит уведомление вне рабочего времени.",
        "The ticket is marked urgent — the team will be notified outside working hours.",
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
    from .. import texts

    line = _choose(
        f"🔄 На вас переназначена заявка №{ticket_id}: {html.escape(title)}",
        f"🔄 Ticket #{ticket_id} has been reassigned to you: {html.escape(title)}",
    )
    if urgency is not None:
        label = urgent_label() if urgency == 5 else texts.urgency_label(urgency)
        line += _choose(f", срочность {label}", f", urgency {label}")
    return line
